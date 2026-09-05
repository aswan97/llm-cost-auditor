"""Ingest against hand-computed fixtures, asserted exactly.

Every expected value here is derived by hand in
`tests/fixtures/anthropic/README.md`. Nothing is asserted "within tolerance":
a tolerance hides exactly the class of bug these fixtures exist to catch — a
token class counted twice, a retry collapsed into a duplicate, an off-by-one on
a window boundary (AGENTS.md).
"""

from __future__ import annotations

import gzip
import json
import os
import stat
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from conftest import WINDOW, write_gzip

from llm_cost_auditor import config as config_module
from llm_cost_auditor import engine, window
from llm_cost_auditor.config import Connection
from llm_cost_auditor.errors import SourceScopeError
from llm_cost_auditor.ingest import pipeline
from llm_cost_auditor.record_store import RecordStore
from llm_cost_auditor.records import Fidelity, Status
from llm_cost_auditor.run_store import RECORDS_PARQUET, RunRequest, RunStatus, RunStore, Stage


def run_ingest(workspace: Path, *, window_spec: str | None = WINDOW) -> Any:
    """Drive the engine exactly as the CLI does — no harness shortcuts."""
    runs = RunStore(workspace)
    record = runs.create(
        RunRequest(
            connection_ids=["local-anthropic"],
            window=window_spec,
            timezone="UTC",
            stop_after=Stage.INGEST,
        )
    )
    result = engine.execute_ingest(workspace=workspace, runs=runs, run_id=record.run_id)
    return runs, result


def stored_records(workspace: Path, run_id: str) -> list[Any]:
    store = RecordStore(RunStore(workspace).artifact_path(run_id, RECORDS_PARQUET))
    return list(store.iter_records())


def _only(fragment: str, warnings: list[str]) -> str:
    """The one warning containing `fragment`, failing if it is absent or doubled."""
    matches = [w for w in warnings if fragment in w]
    assert len(matches) == 1, f"expected exactly one {fragment!r} warning, got {matches}"
    return matches[0]


# --- the totals ---------------------------------------------------------------


def test_record_count_and_window(workspace: Path, expected: dict[str, Any]) -> None:
    _, run = run_ingest(workspace)

    assert run.status is RunStatus.COMPLETE
    assert run.record_count == expected["records"]
    assert run.coverage is not None
    assert run.coverage.records_outside_window == expected["records_outside_window"]
    assert run.coverage.duplicates_collapsed == expected["duplicates_collapsed"]
    assert run.coverage.retry_attempts_linked == expected["retry_attempts_linked"]
    assert run.coverage.records_rejected == 0


def test_status_classification(workspace: Path, expected: dict[str, Any]) -> None:
    _, run = run_ingest(workspace)
    counts = Counter(r.status.value for r in stored_records(workspace, run.run_id))
    assert dict(counts) == expected["status_counts"]


def test_usage_totals(workspace: Path, expected: dict[str, Any]) -> None:
    """Token totals must reproduce the hand arithmetic exactly."""
    _, run = run_ingest(workspace)
    records = stored_records(workspace, run.run_id)
    totals = {
        field: sum(getattr(r.usage, field) for r in records) for field in expected["usage_totals"]
    }
    assert totals == expected["usage_totals"]


def test_out_of_window_record_is_dropped_not_counted(workspace: Path) -> None:
    """The July record contributes 9999+9999 to nothing."""
    _, windowed = run_ingest(workspace)
    _, unwindowed = run_ingest(workspace, window_spec=None)

    windowed_input = sum(r.usage.input_tokens for r in stored_records(workspace, windowed.run_id))
    all_input = sum(r.usage.input_tokens for r in stored_records(workspace, unwindowed.run_id))
    assert all_input - windowed_input == 9999
    assert unwindowed.record_count == windowed.record_count + 1


# --- retries and duplicate delivery (§6.5.1) ----------------------------------


def test_duplicate_delivery_collapses_and_is_flagged(workspace: Path) -> None:
    _, run = run_ingest(workspace)
    duplicates = [r for r in stored_records(workspace, run.run_id) if r.request_id == "req_dup"]

    assert len(duplicates) == 1
    assert duplicates[0].flags.duplicate_delivery is True
    assert duplicates[0].attempt_index == 0


@pytest.mark.parametrize(
    ("request_id", "first_status", "second_status"),
    [
        # A 429 was rejected before generation and cost nothing; the re-issue
        # succeeded. Collapsing these would erase a real attempt.
        ("req_retry_429", Status.ERROR_UNBILLED, Status.OK),
        # A 500 after generation was billed in full; both attempts cost money.
        ("req_retry_500", Status.ERROR_BILLED, Status.OK),
    ],
)
def test_genuine_retries_are_kept_and_linked(
    workspace: Path, request_id: str, first_status: Status, second_status: Status
) -> None:
    _, run = run_ingest(workspace)
    attempts = sorted(
        (r for r in stored_records(workspace, run.run_id) if r.request_id == request_id),
        key=lambda r: r.attempt_index,
    )

    assert len(attempts) == 2
    assert [a.status for a in attempts] == [first_status, second_status]
    assert [a.attempt_index for a in attempts] == [0, 1]
    assert attempts[0].parent_request_id is None
    assert attempts[1].parent_request_id == request_id
    assert all(a.flags.duplicate_delivery is False for a in attempts)


def test_a_duplicate_collapse_never_changes_totals_but_a_retry_does(workspace: Path) -> None:
    """The §14.3 invariant, at ingest: collapsing is free, keeping is not."""
    _, run = run_ingest(workspace)
    records = stored_records(workspace, run.run_id)

    dup_tokens = sum(
        r.usage.input_tokens + r.usage.output_tokens for r in records if r.request_id == "req_dup"
    )
    retry_tokens = sum(
        r.usage.input_tokens + r.usage.output_tokens
        for r in records
        if r.request_id == "req_retry_500"
    )
    # One delivery of a 100/50 request, not two.
    assert dup_tokens == 150
    # Both attempts of the 500-after-generation retry: 900+400 and 900+380.
    assert retry_tokens == 900 + 400 + 900 + 380


# --- fidelity and content (§6.3, §12) -----------------------------------------


def test_fidelity_mix_and_slice_tier(workspace: Path, expected: dict[str, Any]) -> None:
    _, run = run_ingest(workspace)
    records = stored_records(workspace, run.run_id)

    counts = Counter(r.fidelity.value for r in records)
    assert dict(counts) == expected["fidelity_counts"]

    assert len(run.slices) == 1
    assert run.slices[0].fidelity is Fidelity(expected["slice_fidelity"])
    assert run.slices[0].fidelity_counts == expected["fidelity_counts"]
    assert run.slices[0].records == expected["records"]


def test_tier_a_content_is_hashed_at_segment_boundaries(
    workspace: Path, expected: dict[str, Any]
) -> None:
    _, run = run_ingest(workspace)
    record = next(r for r in stored_records(workspace, run.run_id) if r.request_id == "req_tier_a")

    assert record.fidelity is Fidelity.CONTENT
    assert len(record.segments) == expected["tier_a_segment_count"]
    assert [s.kind.value for s in record.segments] == [
        "system_prompt",
        "tool_definition",
        "message",
        "message",
    ]
    assert all(s.hash.startswith("sha256:") for s in record.segments)
    # Token counts here are estimated, so the confidence penalty travels along.
    assert record.flags.usage_estimated is True


def test_tier_b_segments_keep_their_declared_token_counts(
    workspace: Path, expected: dict[str, Any]
) -> None:
    """Per-segment token counts are what make tier B exact, not degraded (§6.3)."""
    _, run = run_ingest(workspace)
    record = next(r for r in stored_records(workspace, run.run_id) if r.request_id == "req_tier_b")

    assert record.fidelity is Fidelity.HASHED
    assert len(record.segments) == expected["tier_b_segment_count"]
    assert [s.token_count for s in record.segments] == [4200, 180]
    assert record.flags.usage_estimated is False


def test_unknown_ttl_class_is_flagged_not_guessed(workspace: Path) -> None:
    _, run = run_ingest(workspace)
    record = next(
        r for r in stored_records(workspace, run.run_id) if r.request_id == "req_unknown_ttl"
    )

    assert record.flags.ttl_class_unknown is True
    assert record.usage.cache_write_unknown_ttl_tokens == 3000
    assert record.usage.cache_write_5m_tokens == 0
    assert record.usage.cache_write_1h_tokens == 0
    assert run.coverage is not None
    assert run.coverage.ttl_class_unknown_records == 1


# --- privacy: assert the absence (§12, AGENTS.md) -----------------------------


def test_no_prompt_text_or_email_reaches_any_artifact(
    workspace: Path, expected: dict[str, Any]
) -> None:
    """Absence is invisible in review, so it gets asserted rather than eyeballed."""
    _, run = run_ingest(workspace)
    directory = RunStore(workspace).directory(run.run_id)

    # A distinctive phrase from the tier-A prompt, and the two email addresses.
    forbidden = [
        "Adjudicate claim 88213",
        "claims adjudication assistant",
        *expected["redacted_values"],
    ]

    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        for needle in forbidden:
            assert needle.encode() not in blob, f"{needle!r} leaked into {path.name}"

    record = next(r for r in stored_records(workspace, run.run_id) if r.request_id == "req_tier_a")
    assert record.labels.user_id == "[redacted]"
    assert record.labels.tags["contact"] == "[redacted]"


# --- coverage gating (§6.1, §11.4) --------------------------------------------


def test_clean_run_is_complete_and_not_a_lower_bound(workspace: Path) -> None:
    _, run = run_ingest(workspace)
    assert run.coverage is not None
    assert run.coverage.missing_bytes == 0
    assert run.coverage.baseline_is_lower_bound is False
    assert run.coverage.projection_withheld is False
    assert run.coverage.incomplete is False
    assert run.status is RunStatus.COMPLETE


def test_truncated_gzip_is_a_coverage_failure_naming_the_object(
    workspace: Path, logs: Path
) -> None:
    """A partial read is a failure, not less data (§6.1)."""
    source = logs / "traffic.jsonl"
    write_gzip(logs / "extra.jsonl.gz", source, truncate_bytes=64)

    runs, run = run_ingest(workspace)
    assert run.coverage is not None
    assert run.coverage.failed_objects == 1
    assert run.coverage.missing_bytes > 0
    assert run.coverage.baseline_is_lower_bound is True
    assert run.coverage.projection_withheld is True
    # A whole extra copy of the traffic went missing, which is far over the 2%
    # default, so savings would be withheld outright.
    assert run.status is RunStatus.INCOMPLETE

    failed = [e for e in runs.read_manifest(run.run_id) if e.status == "failed"]
    assert len(failed) == 1
    assert failed[0].uri.endswith("extra.jsonl.gz")
    assert failed[0].error is not None

    # Records from the half-read object must not survive into the store.
    assert run.record_count == 12


def test_permission_denied_key_is_named_in_coverage(workspace: Path, logs: Path) -> None:
    if os.geteuid() == 0:  # pragma: no cover - root can read anything
        pytest.skip("running as root; file permissions do not apply")

    blocked = logs / "locked.jsonl"
    blocked.write_text('{"request_id":"x","timestamp":"2026-08-10T00:00:00Z","model":"m"}\n')
    blocked.chmod(0)
    try:
        runs, run = run_ingest(workspace)
    finally:
        blocked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert run.coverage is not None
    assert run.coverage.failed_objects == 1
    assert run.coverage.baseline_is_lower_bound is True
    failed = [e for e in runs.read_manifest(run.run_id) if e.status == "failed"]
    assert failed[0].uri.endswith("locked.jsonl")


def test_prefix_with_nothing_in_the_window_says_so(workspace: Path, tmp_path: Path) -> None:
    """ "No traffic" and "no logs delivered" look identical, so it is stated (§15.9)."""
    empty = tmp_path / "logs" / "empty"
    empty.mkdir()
    settings = config_module.load(workspace)
    settings.connections.append(Connection(id="empty", uri=str(empty), source="anthropic"))
    config_module.save(workspace, settings)

    runs = RunStore(workspace)
    record = runs.create(
        RunRequest(connection_ids=["empty"], window=WINDOW, timezone="UTC", stop_after=Stage.INGEST)
    )
    run = engine.execute_ingest(workspace=workspace, runs=runs, run_id=record.run_id)

    assert run.record_count == 0
    assert run.coverage is not None
    assert any("matched no objects" in w for w in run.coverage.warnings)


def test_objects_that_hold_nothing_in_the_window_say_so(workspace: Path) -> None:
    """The quiet version of the same failure: readable logs, wrong window (§6.6).

    An empty listing is loud. Objects that list, decode, and hold nothing in the
    window are silent — same zero records, same `complete` status — and the run
    would otherwise report on nothing without ever saying the window missed the
    data that was sitting right there.
    """
    _, run = run_ingest(workspace, window_spec="2026-01-01..2026-01-31")

    assert run.record_count == 0
    assert run.coverage is not None
    # Readable bytes throughout, so this is emphatically not a coverage failure.
    assert run.coverage.read_objects == 1
    assert run.coverage.missing_bytes == 0
    assert run.coverage.baseline_is_lower_bound is False
    # All 14 raw lines, not the 13 the August window keeps: `req_out_of_window`
    # sits in July and is outside this window too.
    assert run.coverage.records_outside_window == 14

    warning = _only("No records fall inside", run.coverage.warnings)
    assert "2026-01-01..2026-01-31 (UTC)" in warning
    assert "14 record(s)" in warning


def test_a_window_the_data_does_not_reach_is_stated(workspace: Path) -> None:
    """Nine days of a thirty-one-day window still gets quoted as a month (§15.9).

    The fixture traffic is a single August day, so all but one day of the
    requested month is unobserved at the trailing edge.
    """
    _, run = run_ingest(workspace, window_spec=WINDOW)

    assert run.record_count > 0
    assert run.coverage is not None
    warning = _only("does not reach the requested window", run.coverage.warnings)
    assert "at the end" in warning
    assert "at the start" not in warning
    # Stated, never gated: every listed byte was read.
    assert run.coverage.baseline_is_lower_bound is False
    assert run.status is not RunStatus.INCOMPLETE


def test_a_window_the_data_fills_warns_about_nothing(workspace: Path) -> None:
    """A panel that warns on every healthy run is one nobody reads.

    Logs do not start at midnight, so a same-day window has hours of gap at both
    edges and must stay quiet. The threshold is a whole missing day.
    """
    _, run = run_ingest(workspace, window_spec="2026-08-01..2026-08-01")

    assert run.record_count > 0
    assert run.coverage is not None
    assert not [w for w in run.coverage.warnings if "requested window" in w]


def test_unlistable_location_forces_incomplete(workspace: Path, tmp_path: Path) -> None:
    """An unknowable byte volume cannot be compared against a percentage (§6.1)."""
    missing = tmp_path / "logs" / "not-there"
    settings = config_module.load(workspace)
    settings.connections.append(Connection(id="ghost", uri=str(missing), source="anthropic"))
    config_module.save(workspace, settings)

    runs = RunStore(workspace)
    record = runs.create(
        RunRequest(connection_ids=["ghost"], window=WINDOW, timezone="UTC", stop_after=Stage.INGEST)
    )
    run = engine.execute_ingest(workspace=workspace, runs=runs, run_id=record.run_id)

    assert run.status is RunStatus.INCOMPLETE
    assert run.coverage is not None
    assert run.coverage.listing_failures
    assert any("could not be listed" in r for r in run.coverage.gating_reasons)


# --- decoding (§6.1) ----------------------------------------------------------


def test_gzip_and_plain_decode_to_identical_records(workspace: Path, logs: Path) -> None:
    """Compression is a transport detail and must not change a single record."""
    _, plain = run_ingest(workspace)
    plain_records = [
        r.model_dump(exclude={"object_uri"}) for r in stored_records(workspace, plain.run_id)
    ]

    source = logs / "traffic.jsonl"
    (logs / "traffic.jsonl").rename(logs / "traffic.jsonl.gz.tmp")
    write_gzip(logs / "traffic.jsonl.gz", logs / "traffic.jsonl.gz.tmp")
    (logs / "traffic.jsonl.gz.tmp").unlink()
    assert not source.exists()

    _, zipped = run_ingest(workspace)
    zipped_records = [
        r.model_dump(exclude={"object_uri"}) for r in stored_records(workspace, zipped.run_id)
    ]

    assert plain_records == zipped_records


def test_json_array_container_is_detected(workspace: Path, logs: Path) -> None:
    lines = [json.loads(line) for line in (logs / "traffic.jsonl").read_text().splitlines()]
    (logs / "traffic.jsonl").unlink()
    (logs / "traffic.json").write_text(json.dumps(lines))

    runs, run = run_ingest(workspace)
    assert run.record_count == 12
    entry = runs.read_manifest(run.run_id)[0]
    assert entry.container == "json"
    assert entry.compression == "none"


def test_detection_is_recorded_per_object(workspace: Path, logs: Path) -> None:
    write_gzip(logs / "more.jsonl.gz", logs / "traffic.jsonl")
    runs, run = run_ingest(workspace)
    detected = {
        Path(e.uri).name: (e.compression, e.container) for e in runs.read_manifest(run.run_id)
    }
    assert detected["traffic.jsonl"] == ("none", "jsonl")
    assert detected["more.jsonl.gz"] == ("gzip", "jsonl")


def test_an_object_that_is_not_its_detected_format_is_a_coverage_failure(
    workspace: Path, logs: Path
) -> None:
    """Not a skipped line — a named failure (§6.1)."""
    (logs / "bogus.json.gz").write_bytes(gzip.compress(b"this is not json at all"))
    runs, run = run_ingest(workspace)

    assert run.coverage is not None
    assert run.coverage.failed_objects == 1
    failed = [e for e in runs.read_manifest(run.run_id) if e.status == "failed"]
    assert failed[0].uri.endswith("bogus.json.gz")


# --- the source scope (§6.1, §12) ---------------------------------------------


def test_a_connection_outside_the_scope_cannot_be_read(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.jsonl").write_text("{}\n")

    with pytest.raises(SourceScopeError):
        config_module.check_in_scope(str(outside), config_module.load(workspace).sources)


def test_scope_is_re_checked_on_use_not_only_on_save(workspace: Path, tmp_path: Path) -> None:
    """A connection saved when the scope was wider must stop working when it narrows."""
    settings = config_module.load(workspace)
    settings.sources = []  # as if `sources rm` had been run at a terminal
    config_module.save(workspace, settings)

    _, run = run_ingest(workspace)
    assert run.status is RunStatus.INCOMPLETE
    assert run.record_count == 0
    assert run.coverage is not None
    assert any("source scope" in f.error for f in run.coverage.listing_failures)


def test_a_symlink_out_of_scope_is_refused(workspace: Path, logs: Path, tmp_path: Path) -> None:
    """The fixed prefix of a path says nothing about where it resolves to."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.jsonl").write_text(
        '{"request_id":"leak","timestamp":"2026-08-10T00:00:00Z","model":"m"}\n'
    )
    (logs / "link.jsonl").symlink_to(outside / "secret.jsonl")

    _, run = run_ingest(workspace)
    assert all(r.request_id != "leak" for r in stored_records(workspace, run.run_id))
    assert run.coverage is not None
    assert any("source scope" in f.error for f in run.coverage.listing_failures)


# --- windows and timezones (§6.6) ---------------------------------------------


def test_window_end_date_is_inclusive_of_its_whole_day() -> None:
    parsed = window.parse("2026-08-01..2026-08-31", "UTC")
    assert parsed.start.isoformat() == "2026-08-01T00:00:00+00:00"
    assert parsed.end.isoformat() == "2026-09-01T00:00:00+00:00"
    assert parsed.days == 31.0


def test_a_window_is_expressed_in_the_declared_zone() -> None:
    """The same dates in two zones are different instants — that is the point."""
    utc = window.parse("2026-08-01..2026-08-01", "UTC")
    tokyo = window.parse("2026-08-01..2026-08-01", "Asia/Tokyo")
    assert tokyo.start < utc.start
    assert (utc.start - tokyo.start).total_seconds() == 9 * 3600
    assert "Asia/Tokyo" in tokyo.describe()


# --- the run store (§5.3, §5.4) -----------------------------------------------


def test_a_stage_runs_only_once_per_run(workspace: Path) -> None:
    runs, run = run_ingest(workspace)
    with pytest.raises(Exception, match="already completed"):
        engine.execute_ingest(workspace=workspace, runs=runs, run_id=run.run_id)


def test_run_directory_holds_the_expected_artifacts(workspace: Path) -> None:
    runs, run = run_ingest(workspace)
    directory = runs.directory(run.run_id)
    names = {p.name for p in directory.iterdir()}
    assert {"run.json", "manifest.json", "records.parquet", "log.jsonl"} <= names


def test_run_id_cannot_escape_the_store(workspace: Path) -> None:
    runs = RunStore(workspace)
    with pytest.raises(Exception, match="invalid run id"):
        runs.directory("../../etc")


def test_purge_keeps_the_run_readable(workspace: Path) -> None:
    """Retention purges records and leaves the audit readable (§5.3, §12)."""
    runs, run = run_ingest(workspace)
    assert runs.purge_records(run.run_id) is True

    after = runs.get(run.run_id)
    assert after.records_purged is True
    assert after.record_count == 12
    assert after.coverage is not None
    assert not runs.artifact_path(run.run_id, RECORDS_PARQUET).exists()


def test_an_interrupted_run_is_never_resumed(workspace: Path) -> None:
    """Work that has begun is failed, not resumed (§5.4)."""
    from datetime import UTC, datetime, timedelta

    runs = RunStore(workspace)
    record = runs.create(RunRequest(connection_ids=["local-anthropic"], timezone="UTC"))
    record.status = RunStatus.RUNNING
    record.started_at = datetime.now(UTC) - timedelta(hours=1)
    record.heartbeat_at = record.started_at
    runs.save(record)

    assert runs.reconcile_on_startup() == [record.run_id]
    assert runs.get(record.run_id).status is RunStatus.INTERRUPTED


def test_coverage_is_computed_from_the_manifest_not_from_what_succeeded(
    workspace: Path, logs: Path
) -> None:
    write_gzip(logs / "broken.jsonl.gz", logs / "traffic.jsonl", truncate_bytes=64)
    runs, run = run_ingest(workspace)

    manifest = runs.read_manifest(run.run_id)
    assert run.coverage is not None
    assert run.coverage.listed_objects == len(manifest)
    assert run.coverage.listed_bytes == sum(e.size_bytes for e in manifest)
    assert run.coverage.read_bytes == sum(e.size_bytes for e in manifest if e.status == "ok")
    assert run.coverage.missing_bytes == sum(e.size_bytes for e in manifest if e.status != "ok")


# --- the pipeline seams --------------------------------------------------------


def test_connectors_know_nothing_about_providers() -> None:
    """The seam that keeps `s3_anthropic_gzip_reader` from ever existing (§6)."""
    from llm_cost_auditor.ingest import local as connector

    source = Path(connector.__file__).read_text()
    for provider in ("anthropic", "openai", "bedrock", "vertex", "foundry"):
        assert provider not in source.lower()


def test_adapters_know_nothing_about_where_bytes_came_from() -> None:
    from llm_cost_auditor.ingest.adapters import anthropic as adapter

    source = Path(adapter.__file__).read_text()
    for transport in ("s3://", "boto3", "open(", "Path("):
        assert transport not in source


def test_ingest_is_bulk_and_self_contained(workspace: Path) -> None:
    """Two runs over the same window read the same objects and produce the same records."""
    _, first = run_ingest(workspace)
    _, second = run_ingest(workspace)

    assert first.record_count == second.record_count
    first_manifest = {e.uri: e.etag for e in RunStore(workspace).read_manifest(first.run_id)}
    second_manifest = {e.uri: e.etag for e in RunStore(workspace).read_manifest(second.run_id)}
    assert first_manifest == second_manifest


def test_cross_object_duplicate_delivery_is_reported(workspace: Path, logs: Path) -> None:
    """Overlapping sources are named, never quietly folded into the totals (§6.6)."""
    (logs / "copy.jsonl").write_text((logs / "traffic.jsonl").read_text())
    _, run = run_ingest(workspace)

    assert run.record_count == 12  # the copy collapses entirely
    assert run.coverage is not None
    # 13 in-window lines per file, 26 in total, collapsing to 12 records — so 14
    # collapses, not 13: `req_dup` was already doubled inside each file, and its
    # four identical deliveries collapse three times on their own.
    #   8 singleton ids, 1 collapse each   =  8
    #   req_dup: four identical records collapse to one   =  3
    #   two retry ids, two attempts each, each doubled   =  4
    #                                                    ------
    #                                                      14
    assert run.coverage.duplicates_collapsed == 26 - 12
    assert any("delivered by 2 objects" in w for w in run.coverage.warnings)


def test_empty_run_still_writes_a_typed_record_store(workspace: Path, tmp_path: Path) -> None:
    """No records must not mean no columns."""
    empty = tmp_path / "logs" / "nothing"
    empty.mkdir()
    settings = config_module.load(workspace)
    settings.connections.append(Connection(id="nothing", uri=str(empty), source="anthropic"))
    config_module.save(workspace, settings)

    runs = RunStore(workspace)
    record = runs.create(RunRequest(connection_ids=["nothing"], timezone="UTC"))
    run = engine.execute_ingest(workspace=workspace, runs=runs, run_id=record.run_id)

    store = RecordStore(runs.artifact_path(run.run_id, RECORDS_PARQUET))
    assert store.exists()
    assert store.count() == 0
    assert list(store.iter_records()) == []


def test_no_float_column_holds_a_count(workspace: Path) -> None:
    """polars infers f64 silently; token counts are integers and say so."""
    import polars as pl

    runs, run = run_ingest(workspace)
    frame = pl.read_parquet(runs.artifact_path(run.run_id, RECORDS_PARQUET))
    for name, dtype in frame.schema.items():
        if name.startswith("usage_") or name in {"attempt_index", "latency_ms"}:
            assert dtype == pl.Int64, f"{name} is {dtype}, not an integer"


def test_pipeline_emits_progress_events(workspace: Path) -> None:
    runs, run = run_ingest(workspace)
    events = list(runs.read_events(run.run_id))
    assert events
    assert any(e["event"] == "stage" for e in events)
    assert events[-1]["message"].startswith("ingest ")


def test_no_credential_material_reaches_a_run_artifact(workspace: Path) -> None:
    """v1 stores no credentials, and a run never records one either (§6.7)."""
    runs, run = run_ingest(workspace)
    directory = runs.directory(run.run_id)
    blob = b"".join(p.read_bytes() for p in directory.rglob("*") if p.is_file())
    for marker in (b"AKIA", b"aws_secret", b"SAS", b"password", b"Bearer "):
        assert marker not in blob


def test_ingest_result_slices_name_their_connection(workspace: Path) -> None:
    settings = config_module.load(workspace)
    result = pipeline.ingest(
        connections=settings.connections,
        roots=settings.sources,
        window=window.parse(WINDOW, "UTC"),
        max_missing_pct=settings.coverage.max_missing_pct,
    )
    assert [s.connection_id for s in result.slices] == ["local-anthropic"]
    assert all(r.connection_id == "local-anthropic" for r in result.records)
    assert all(r.object_uri.endswith("traffic.jsonl") for r in result.records)
