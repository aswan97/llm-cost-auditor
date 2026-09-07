"""The run engine, and the worker that executes runs (SPEC.md §5.3, §5.4).

One engine, two drivers. The CLI calls `execute_ingest` directly and renders
progress from the events it appends; the server hands runs to `RunQueue`, whose
worker calls the same function. Neither owns analysis logic, and a run started
in one is visible in the other because both go through the run store.

An audit takes minutes, not milliseconds, so the app cannot run one inside a
request handler — but a threaded queue over the run store is sufficient for a
single-tenant local server. **No Celery, no Redis, no external broker**, and
the queue sits behind a narrow interface so a real broker could replace it if
the shared-deployment case ever arrives.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config as config_module
from . import profile as profile_module
from . import waste
from . import window as window_module
from .errors import AuditorError, RunCancelled
from .ingest import pipeline
from .record_store import RecordStore
from .run_store import RECORDS_PARQUET, RunRecord, RunStatus, RunStore, Stage

# A run holds a whole dataset in memory (§5.1); running two concurrently on a
# laptop is how the tool gets OOM-killed (§15.10).
DEFAULT_CONCURRENCY = 1


def execute_ingest(
    *,
    workspace: Path,
    runs: RunStore,
    run_id: str,
    should_cancel: Callable[[], bool] | None = None,
) -> RunRecord:
    """Run the ingest stage for one run, writing every artifact it produces.

    Everything it learns lands in the run store: the manifest, the coverage
    verdict, the slice summaries, `records.parquet`, and a `log.jsonl` trail.
    Nothing is returned that is not also on disk, because the run record is the
    only contract the app reads (§5.3).
    """
    settings = config_module.load(workspace)
    record = runs.start_stage(run_id, Stage.INGEST)

    def emit(event: str, payload: dict[str, Any]) -> None:
        runs.append_event(run_id, event, payload)

    window = None
    try:
        if record.request.window:
            window = window_module.parse(record.request.window, record.request.timezone)

        connections = [settings.connection(cid) for cid in record.request.connection_ids]
        if not connections:
            raise AuditorError("a run must name at least one connection")

        emit(
            "stage",
            {
                "stage": "ingest",
                "pct": 0,
                "message": (
                    f"ingesting {len(connections)} connection(s)"
                    + (f" over {window.describe()}" if window else " over all available data")
                ),
            },
        )

        result = pipeline.ingest(
            connections=connections,
            roots=settings.sources,
            window=window,
            max_missing_pct=settings.coverage.max_missing_pct,
            emit=emit,
            should_cancel=should_cancel,
        )

        store = RecordStore(runs.artifact_path(run_id, RECORDS_PARQUET))
        store.write_records(result.records)
        runs.write_manifest(run_id, result.manifest)

        record = runs.get(run_id)
        record.record_count = len(result.records)
        record.coverage = result.coverage
        record.slices = result.slices
        record.observed_start = min(
            (s.observed_start for s in result.slices if s.observed_start), default=None
        )
        record.observed_end = max(
            (s.observed_end for s in result.slices if s.observed_end), default=None
        )
        record.heartbeat_at = datetime.now(UTC)
        runs.save(record)
        runs.complete_stage(run_id, Stage.INGEST)

        # `incomplete` is a completed run whose coverage failed the §6.1 gate,
        # not a failed one: it has records and a manifest, and what it withholds
        # is the savings figures — which do not exist yet in this release, so
        # the status is what carries the verdict forward.
        status = RunStatus.INCOMPLETE if result.coverage.incomplete else RunStatus.COMPLETE
        for reason in result.coverage.gating_reasons:
            emit("coverage", {"message": reason})
        emit("stage", {"stage": "ingest", "pct": 100, "message": f"ingest {status.value}"})
        return runs.set_status(run_id, status)

    except RunCancelled as exc:
        emit("cancelled", {"message": str(exc)})
        _discard_partial_records(runs, run_id)
        return runs.set_status(run_id, RunStatus.CANCELLED, error=str(exc))

    except AuditorError as exc:
        emit("error", {"message": str(exc)})
        _discard_partial_records(runs, run_id)
        return runs.set_status(run_id, RunStatus.FAILED, error=str(exc))

    except Exception as exc:
        emit("error", {"message": f"{type(exc).__name__}: {exc}", "traceback": _tb()})
        _discard_partial_records(runs, run_id)
        return runs.set_status(run_id, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


def execute_profile(
    *,
    workspace: Path,
    runs: RunStore,
    run_id: str,
    should_cancel: Callable[[], bool] | None = None,
) -> RunRecord:
    """Discover workloads over an ingested run's records (SPEC.md §8.2).

    Read-only over `records.parquet`: the stage produces a grouping, never a
    changed record. What it can and cannot see is stated on the result rather
    than assumed — see `profile.ProfileSummary.limitations`.
    """
    del workspace  # the stage needs nothing outside the run directory

    def body(record: RunRecord, emit: _Emit) -> RunRecord:
        store = _records_of(runs, run_id)
        emit("stage", {"stage": "profile", "pct": 0, "message": "discovering workloads"})

        summary = profile_module.discover(store.iter_records())
        record = runs.get(run_id)
        record.profile = summary
        runs.save(record)

        emit(
            "stage",
            {
                "stage": "profile",
                "pct": 100,
                "message": (
                    f"{len(summary.workloads)} workload(s) over {summary.total_records} "
                    f"record(s); {summary.unmapped_pct:.1f}% unmapped"
                ),
                "counts": {"workloads": len(summary.workloads)},
            },
        )
        return record

    return _stage(runs, run_id, Stage.PROFILE, body, should_cancel)


def execute_audit(
    *,
    workspace: Path,
    runs: RunStore,
    run_id: str,
    should_cancel: Callable[[], bool] | None = None,
) -> RunRecord:
    """Run the analyzers and write `findings.json` (SPEC.md §9, §13.4).

    Refuses to run at all when coverage failed the §6.1 gate. Savings figures
    are withheld entirely for such a run (§11.4), and every finding an analyzer
    produces *is* a savings figure — so there is nothing to emit but a number
    that would be wrong, and a footnote does not improve a wrong number.

    That check runs *before* the stage is even started, ahead of the ordering
    check. Both refuse the same request, but only one of them tells the user
    something they can act on: "profile has not completed" sends them to run
    `profile`, which succeeds, and only then do they learn the audit was never
    going to happen.
    """
    del workspace

    record = runs.get(run_id)
    if record.coverage is not None and record.coverage.incomplete:
        raise AuditorError(
            "this run's coverage failed the §6.1 gate, so savings figures are withheld "
            "entirely (§11.4) and the audit stage will not run. Fix the unread sources "
            "and start a new run: " + " ".join(record.coverage.gating_reasons)
        )

    def body(record: RunRecord, emit: _Emit) -> RunRecord:
        if record.profile is None:
            raise AuditorError(
                f"run {run_id} has no profile. The audit stage analyzes discovered workloads, "
                f"so the profile stage must complete first (§5.3)."
            )

        store = _records_of(runs, run_id)
        emit("stage", {"stage": "audit", "pct": 0, "message": "running waste analyzers"})

        results = waste.analyze(
            run_id=run_id,
            records=store.iter_records(),
            summary=record.profile,
            slices=record.slices,
            coverage=record.coverage,
        )
        runs.write_findings(run_id, results)

        record = runs.get(run_id)
        record.finding_count = len(results.findings)
        runs.save(record)

        for reason in results.withheld_reasons:
            emit("coverage", {"message": reason})
        emit(
            "stage",
            {
                "stage": "audit",
                "pct": 100,
                "message": (
                    f"{len(results.findings)} finding(s), "
                    f"{results.portfolio_usd_micros} uUSD marginal over the observed window"
                ),
                "counts": {"findings": len(results.findings)},
            },
        )
        return record

    return _stage(runs, run_id, Stage.AUDIT, body, should_cancel)


def execute_run(
    *,
    workspace: Path,
    runs: RunStore,
    run_id: str,
    should_cancel: Callable[[], bool] | None = None,
) -> RunRecord:
    """Advance a run through `ingest → profile → audit`, honouring `--stop-after`.

    Stages run once and in order (§5.3). A stage that does not complete stops
    the pipeline where it is — the run's status already says why, and running
    the next stage over data the previous one could not finish is how a
    half-analyzed dataset produces a report (§5.4).
    """
    record = runs.get(run_id)
    stop_after = record.request.stop_after
    stages = (
        (Stage.INGEST, execute_ingest),
        (Stage.PROFILE, execute_profile),
        (Stage.AUDIT, execute_audit),
    )

    for stage, execute in stages:
        if stage in record.stages_completed:
            continue
        record = execute(workspace=workspace, runs=runs, run_id=run_id, should_cancel=should_cancel)
        if stage not in record.stages_completed:
            return record
        if stop_after is not None and stage is stop_after:
            return record
        if stage is Stage.INGEST and record.coverage is not None and record.coverage.incomplete:
            # Not a failure — the ingest succeeded and its manifest and coverage
            # are worth keeping. But savings are withheld entirely for this run
            # (§11.4), so there is nothing for the later stages to produce.
            runs.append_event(
                run_id,
                "coverage",
                {
                    "message": (
                        "Stopping after ingest: coverage failed the §6.1 gate, so savings "
                        "figures are withheld entirely for this run (§11.4)."
                    )
                },
            )
            return record

    return record


_Emit = Callable[[str, dict[str, Any]], None]


def _stage(
    runs: RunStore,
    run_id: str,
    stage: Stage,
    body: Callable[[RunRecord, _Emit], RunRecord],
    should_cancel: Callable[[], bool] | None,
) -> RunRecord:
    """Shared lifecycle for the stages that read what ingest already wrote.

    Ingest has its own copy because it alone has partial data to discard on
    failure; these two derive from `records.parquet` and leave it untouched, so
    a failure here costs the derived artifact and nothing else.
    """

    def emit(event: str, payload: dict[str, Any]) -> None:
        runs.append_event(run_id, event, payload)

    record = runs.start_stage(run_id, stage)
    try:
        if should_cancel is not None and should_cancel():
            raise RunCancelled(f"cancelled before the {stage.value} stage")
        record = body(record, emit)
        runs.complete_stage(run_id, stage)
        status = (
            RunStatus.INCOMPLETE
            if record.coverage is not None and record.coverage.incomplete
            else RunStatus.COMPLETE
        )
        return runs.set_status(run_id, status)

    except RunCancelled as exc:
        emit("cancelled", {"message": str(exc)})
        return runs.set_status(run_id, RunStatus.CANCELLED, error=str(exc))

    except AuditorError as exc:
        emit("error", {"message": str(exc)})
        return runs.set_status(run_id, RunStatus.FAILED, error=str(exc))

    except Exception as exc:
        emit("error", {"message": f"{type(exc).__name__}: {exc}", "traceback": _tb()})
        return runs.set_status(run_id, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


def _records_of(runs: RunStore, run_id: str) -> RecordStore:
    store = RecordStore(runs.artifact_path(run_id, RECORDS_PARQUET))
    if not store.exists():
        raise AuditorError(
            f"run {run_id} has no records.parquet — it was purged under retention, or ingest "
            f"did not complete. There is nothing to analyze (§5.3)."
        )
    return store


def _discard_partial_records(runs: RunStore, run_id: str) -> None:
    """A half-ingested dataset must never be analyzable (§5.4)."""
    RecordStore(runs.artifact_path(run_id, RECORDS_PARQUET)).drop()


def _tb() -> str:
    return "".join(traceback.format_exc()).strip()


class RunQueue:
    """One background worker in the server process, executing runs from a queue.

    The interface is deliberately narrow — `submit`, `status`, `cancel` — so a
    real broker can replace it without the app noticing (§5.4).
    """

    def __init__(self, workspace: Path, runs: RunStore, *, concurrency: int = DEFAULT_CONCURRENCY):
        self.workspace = workspace
        self.runs = runs
        self.concurrency = max(1, concurrency)
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._stopping = threading.Event()

    def start(self) -> None:
        """Reconcile the previous process's runs, then start the worker(s)."""
        for run_id in self.runs.reconcile_on_startup():
            self.runs.append_event(
                run_id,
                "interrupted",
                {"message": "server restarted while this run was executing"},
            )
        # A `queued` run never started executing, so it has no partial data and
        # is safely re-queued rather than failed (§5.4).
        for record in self.runs.list():
            if record.status is RunStatus.QUEUED:
                self._queue.put(record.run_id)

        for index in range(self.concurrency):
            worker = threading.Thread(target=self._work, name=f"run-worker-{index}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def stop(self) -> None:
        self._stopping.set()

    def submit(self, run_id: str) -> None:
        with self._lock:
            self._cancels[run_id] = threading.Event()
        self._queue.put(run_id)

    def status(self, run_id: str) -> RunStatus:
        return self.runs.get(run_id).status

    def cancel(self, run_id: str) -> RunRecord:
        """Cancel a queued or running run.

        A queued run is cancelled immediately. A running one is signalled and
        stops at the next object boundary — then it is *failed*, never paused:
        any condition that blocks a run mid-flight fails it (§5.4).
        """
        record = self.runs.get(run_id)
        if record.status.terminal:
            return record
        with self._lock:
            event = self._cancels.setdefault(run_id, threading.Event())
        event.set()
        if record.status is RunStatus.QUEUED:
            return self.runs.set_status(run_id, RunStatus.CANCELLED, error="cancelled while queued")
        return record

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                run_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._execute(run_id)
            finally:
                self._queue.task_done()

    def _execute(self, run_id: str) -> None:
        with self._lock:
            event = self._cancels.setdefault(run_id, threading.Event())

        record = self.runs.get(run_id)
        if record.status.terminal:
            return
        if event.is_set():
            self.runs.set_status(run_id, RunStatus.CANCELLED, error="cancelled while queued")
            return

        heartbeat = _Heartbeat(self.runs, run_id)
        heartbeat.start()
        try:
            execute_run(
                workspace=self.workspace,
                runs=self.runs,
                run_id=run_id,
                should_cancel=event.is_set,
            )
        finally:
            heartbeat.stop()
            with self._lock:
                self._cancels.pop(run_id, None)


class _Heartbeat:
    """Keeps `heartbeat_at` fresh so a crash is distinguishable from a slow run (§5.4)."""

    INTERVAL_SECONDS = 15.0

    def __init__(self, runs: RunStore, run_id: str) -> None:
        self.runs = runs
        self.run_id = run_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _beat(self) -> None:
        while not self._stop.wait(self.INTERVAL_SECONDS):
            try:
                self.runs.heartbeat(self.run_id)
            except Exception:
                return
