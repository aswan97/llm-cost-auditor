"""Runs as durable objects — the `RunStore` seam (SPEC.md §5.1, §5.3).

Making this a platform rather than a command means one new durable concept: the
run. Everything the app shows is a view of a run, and nothing about a run
depends on the process that started it still being alive.

**The store is a filesystem directory, not a database.** It is inspectable,
diffable, copyable to a colleague, and deletable with `rm -rf`. That is the
point, and it stays a directory — `RecordStore` is the separate seam DuckDB
replaces when scale demands it. Merging the two because they both say "store"
produces an interface that fits neither.

**Nothing in the run directory is encrypted** (§5.3). Inspectability is the
point, and the protections that make it safe are upstream: raw prompt text is
never written, and derived records are redacted before they are stored.
"""

from __future__ import annotations

import builtins
import json
import os
import secrets
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import RunStateError
from .findings import FindingSet
from .ingest.manifest import Coverage, ObjectEntry, SliceSummary
from .profile import ProfileSummary

RUNS_DIRNAME = "runs"

RUN_JSON = "run.json"
MANIFEST_JSON = "manifest.json"
RECORDS_PARQUET = "records.parquet"
FINDINGS_JSON = "findings.json"
LOG_JSONL = "log.jsonl"

# A run that has begun is never resumed (§5.4). A heartbeat older than this on
# a `running` run means the process that owned it is gone.
HEARTBEAT_STALE_SECONDS = 120


class RunStatus(StrEnum):
    """Lifecycle, plus the one coverage verdict that is terminal.

    `INCOMPLETE` is a completed run whose coverage failed the §6.1 gate: it has
    records and a manifest, and its savings figures are withheld. It is a
    status rather than a flag because it changes what the run may be used for.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self is not RunStatus.QUEUED and self is not RunStatus.RUNNING


class Stage(StrEnum):
    """A run advances through these in order, and records the one it reached (§5.3)."""

    INGEST = "ingest"
    PROFILE = "profile"
    AUDIT = "audit"


class RunRequest(BaseModel):
    """What the user asked for. Resolved config, recorded so a run is reproducible."""

    model_config = ConfigDict(extra="forbid")

    connection_ids: list[str] = Field(default_factory=list)
    window: str | None = None
    timezone: str = "UTC"
    stop_after: Stage | None = None


class RunRecord(BaseModel):
    """`run.json` — the only contract between the engine and the app (§5.3).

    The app reads this, `findings.json`, and `log.jsonl`; it never reaches into
    engine internals. A run produced by the CLI on a build server renders
    identically in the app.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    parent_run_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    stage: Stage | None = None
    stages_completed: list[Stage] = Field(default_factory=list)

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None

    request: RunRequest = Field(default_factory=RunRequest)
    observed_start: datetime | None = None
    observed_end: datetime | None = None

    record_count: int = 0
    coverage: Coverage | None = None
    slices: list[SliceSummary] = Field(default_factory=list)

    # The profile stage's output (§8.2). Lives here rather than in its own
    # artifact because it is small, and because everything the app reads about
    # a run's shape already comes from `run.json`.
    profile: ProfileSummary | None = None
    finding_count: int = 0

    records_purged: bool = False
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()


def new_run_id() -> str:
    """`run_<ISO8601 basic UTC>_<4 hex>` — sortable, unique, and readable in a path."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"run_{stamp}_{secrets.token_hex(2)}"


class RunStore:
    """A directory per run under the workspace root."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.root = workspace / RUNS_DIRNAME

    # --- lifecycle ---------------------------------------------------------

    def create(self, request: RunRequest, *, parent_run_id: str | None = None) -> RunRecord:
        run_id = new_run_id()
        directory = self.directory(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        record = RunRecord(
            run_id=run_id,
            parent_run_id=parent_run_id,
            created_at=datetime.now(UTC),
            request=request,
        )
        self.save(record)
        return record

    def directory(self, run_id: str) -> Path:
        """Resolve a run directory, refusing anything that escapes the store.

        `run_id` reaches this from an HTTP path segment, so it is treated as
        untrusted input rather than as an identifier we happen to have issued.
        """
        candidate = (self.root / run_id).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise RunStateError(f"invalid run id {run_id!r}")
        return candidate

    def exists(self, run_id: str) -> bool:
        return (self.directory(run_id) / RUN_JSON).exists()

    def get(self, run_id: str) -> RunRecord:
        path = self.directory(run_id) / RUN_JSON
        if not path.exists():
            raise RunStateError(f"no run {run_id!r} in {self.root}")
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    # `RunStore.list()` is named by the seam (SPEC.md §5.1) and shadows the
    # builtin inside this class body, so annotations below say `builtins.list`.
    def list(self) -> builtins.list[RunRecord]:
        """Newest first. A directory that fails to parse is skipped, not fatal."""
        if not self.root.exists():
            return []
        records: list[RunRecord] = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            try:
                records.append(self.get(directory.name))
            except (RunStateError, ValueError):
                continue
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records

    def save(self, record: RunRecord) -> None:
        """Write `run.json` atomically, so a reader never sees a half-written record."""
        directory = self.directory(record.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / RUN_JSON
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)

    def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> RunRecord:
        record = self.get(run_id)
        record.status = status
        if status is RunStatus.RUNNING and record.started_at is None:
            record.started_at = datetime.now(UTC)
        if status.terminal:
            record.finished_at = datetime.now(UTC)
        if error is not None:
            record.error = error
        self.save(record)
        return record

    def heartbeat(self, run_id: str) -> None:
        record = self.get(run_id)
        record.heartbeat_at = datetime.now(UTC)
        self.save(record)

    def start_stage(self, run_id: str, stage: Stage) -> RunRecord:
        """Refuse to run a stage twice, and refuse to run one out of order (§5.3).

        Re-running a stage means a new run with a `parent_run_id`. There is no
        in-place mutation to lose, which is what makes run-to-run comparison
        honest.
        """
        record = self.get(run_id)
        if stage in record.stages_completed:
            raise RunStateError(
                f"run {run_id} already completed the {stage.value} stage. Re-running a stage "
                f"means a new run with a parent_run_id (SPEC.md §5.3)."
            )
        order = list(Stage)
        required = order[: order.index(stage)]
        missing = [s for s in required if s not in record.stages_completed]
        if missing:
            raise RunStateError(
                f"run {run_id} cannot start {stage.value}: "
                f"{', '.join(s.value for s in missing)} has not completed"
            )
        record.stage = stage
        record.status = RunStatus.RUNNING
        record.started_at = record.started_at or datetime.now(UTC)
        record.heartbeat_at = datetime.now(UTC)
        self.save(record)
        return record

    def complete_stage(self, run_id: str, stage: Stage) -> RunRecord:
        record = self.get(run_id)
        if stage not in record.stages_completed:
            record.stages_completed.append(stage)
        self.save(record)
        return record

    def reconcile_on_startup(self) -> builtins.list[str]:
        """Fail runs the last process was executing; re-queue ones that never began.

        A crashed or killed server leaves a run marked `running` with a stale
        heartbeat. **Work that has begun is never resumed** (§5.4) — a
        half-analyzed dataset must never produce a report — so such a run is
        marked `interrupted`. A `queued` run has no partial data, so it is
        re-queued rather than failed.
        """
        touched: builtins.list[str] = []
        now = datetime.now(UTC)
        for record in self.list():
            if record.status is not RunStatus.RUNNING:
                continue
            beat = record.heartbeat_at or record.started_at or record.created_at
            if (now - beat).total_seconds() < HEARTBEAT_STALE_SECONDS:
                continue
            self.set_status(
                record.run_id,
                RunStatus.INTERRUPTED,
                error=(
                    "the process executing this run exited. Work that has begun is never "
                    "resumed (SPEC.md §5.4) — start a new run."
                ),
            )
            touched.append(record.run_id)
        return touched

    def delete(self, run_id: str) -> None:
        shutil.rmtree(self.directory(run_id), ignore_errors=True)

    def purge_records(self, run_id: str) -> bool:
        """Drop `records.parquet` under the retention TTL, keeping the run readable (§5.3).

        Findings and the report survive; `run.json` records that the purge
        happened, because tier-A re-analysis and `--explain-pricing` stop
        working at that point.
        """
        path = self.artifact_path(run_id, RECORDS_PARQUET)
        if not path.exists():
            return False
        path.unlink()
        record = self.get(run_id)
        record.records_purged = True
        self.save(record)
        return True

    # --- artifacts and events ----------------------------------------------

    def artifact_path(self, run_id: str, name: str) -> Path:
        if "/" in name or "\\" in name or name.startswith("."):
            raise RunStateError(f"invalid artifact name {name!r}")
        return self.directory(run_id) / name

    def write_artifact(self, run_id: str, name: str, content: str | bytes) -> Path:
        path = self.artifact_path(run_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return path

    def write_manifest(self, run_id: str, entries: builtins.list[ObjectEntry]) -> None:
        payload = [entry.model_dump(mode="json") for entry in entries]
        self.write_artifact(run_id, MANIFEST_JSON, json.dumps(payload, indent=2))

    def read_manifest(self, run_id: str) -> builtins.list[ObjectEntry]:
        path = self.artifact_path(run_id, MANIFEST_JSON)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [ObjectEntry.model_validate(item) for item in raw]

    def write_findings(self, run_id: str, results: FindingSet) -> None:
        """Write `findings.json` — the finding set for this run (§5.3, §13.4)."""
        self.write_artifact(run_id, FINDINGS_JSON, results.model_dump_json(indent=2))

    def read_findings(self, run_id: str) -> FindingSet | None:
        """The run's findings, or `None` when the audit stage has not run.

        `None` and "no findings" are different answers and are kept different:
        an empty `FindingSet` means the analyzers looked and found nothing,
        which is a result worth showing.
        """
        path = self.artifact_path(run_id, FINDINGS_JSON)
        if not path.exists():
            return None
        return FindingSet.model_validate_json(path.read_text(encoding="utf-8"))

    def append_event(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        """Append one structured progress event to `log.jsonl`.

        One producer, two renderers (§5.4): the app tails this file and the CLI
        renders the same events as a progress bar. Nothing polls into the
        engine.

        Nothing about a credential is ever written here (§6.7) — the run record
        names the *identity* used, never a value.
        """
        line = json.dumps(
            {"ts": datetime.now(UTC).isoformat(), "event": event, **payload},
            default=str,
        )
        path = self.directory(run_id) / LOG_JSONL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_events(self, run_id: str, *, offset: int = 0) -> Iterator[dict[str, Any]]:
        """Read events from a line offset, so a reader can tail without re-reading."""
        path = self.directory(run_id) / LOG_JSONL
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < offset or not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
