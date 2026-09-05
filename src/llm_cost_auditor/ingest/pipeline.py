"""The ingest stage: connections in, records and a manifest out (SPEC.md §6).

The composition the whole section exists to keep separate:

    connector (locate + fetch) → decode (decompress + container)
        → adapter (interpret) → normalize → RequestRecord

Connectors move bytes and know nothing about providers; adapters interpret
records and know nothing about where they came from. Anything named like
`s3_anthropic_reader` is that seam collapsing.

**Ingest is bulk in v1** (§6.6): every run reads the full window it was given,
from every connection it names, every time. A run whose records depend on what
a previous run happened to read is not self-contained, and that failure is
invisible in the output.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Connection, check_in_scope
from ..errors import AdapterError, DecodeError, RunCancelled, SourceScopeError
from ..records import Fidelity, RequestRecord
from ..window import Window
from . import decode, local
from .adapters import anthropic
from .manifest import Coverage, ListingFailureEntry, ObjectEntry, SliceSummary
from .normalize import normalize

# Adapters by source name. A dict, not a registry class: the plugin interface
# earns its keep at three implementations, not one (AGENTS.md).
ADAPTERS: dict[str, Callable[..., RequestRecord]] = {anthropic.SOURCE: anthropic.parse}

Emit = Callable[[str, dict[str, Any]], None]

# Checked once per object, which is the granularity a cancel actually lands at:
# an object is read whole or not at all (§6.1), so there is no useful place to
# interrupt inside one.
ShouldCancel = Callable[[], bool]


@dataclass
class IngestResult:
    """Everything one ingest stage produced."""

    records: list[RequestRecord] = field(default_factory=list)
    manifest: list[ObjectEntry] = field(default_factory=list)
    slices: list[SliceSummary] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)


def ingest(
    *,
    connections: list[Connection],
    roots: list[str],
    window: Window | None = None,
    max_missing_pct: float = 2.0,
    emit: Emit | None = None,
    should_cancel: ShouldCancel | None = None,
) -> IngestResult:
    """Read every named connection over the window and normalize what is there."""
    report: Emit = emit or (lambda event, payload: None)
    cancelled: ShouldCancel = should_cancel or (lambda: False)
    result = IngestResult()
    raw_records: list[RequestRecord] = []

    for index, connection in enumerate(connections):
        report(
            "stage",
            {
                "stage": "ingest",
                "message": f"reading connection {connection.id}",
                "pct": _pct(index, len(connections)),
            },
        )
        raw_records += _ingest_connection(
            connection=connection,
            roots=roots,
            window=window,
            result=result,
            report=report,
            cancelled=cancelled,
        )

    report("stage", {"stage": "ingest", "message": "classifying retries", "pct": 90})
    normalized = normalize(raw_records)
    result.records = normalized.records

    coverage = result.coverage
    coverage.duplicates_collapsed = normalized.duplicates_collapsed
    coverage.retry_attempts_linked = normalized.retry_attempts_linked
    coverage.dedupe_method = normalized.dedupe_method
    coverage.warnings += normalized.cross_object_warnings
    coverage.ttl_class_unknown_records = sum(
        1 for record in result.records if record.flags.ttl_class_unknown
    )
    coverage.apply_gating(max_missing_pct)

    result.slices = _summarize_slices(result.records)
    # Needs the slices, which is why it follows gating rather than joining it:
    # the observed range is derived from them, and it is the half of the
    # window/observed comparison the manifest cannot supply on its own.
    coverage.describe_window_coverage(
        window=window,
        observed_start=min(
            (s.observed_start for s in result.slices if s.observed_start), default=None
        ),
        observed_end=max((s.observed_end for s in result.slices if s.observed_end), default=None),
        records=len(result.records),
    )
    report(
        "stage",
        {
            "stage": "ingest",
            "message": f"{len(result.records)} records from {coverage.read_objects} objects",
            "pct": 100,
            "counts": {"records": len(result.records), "objects": coverage.read_objects},
        },
    )
    return result


def _ingest_connection(
    *,
    connection: Connection,
    roots: list[str],
    window: Window | None,
    result: IngestResult,
    report: Emit,
    cancelled: ShouldCancel,
) -> list[RequestRecord]:
    coverage = result.coverage
    records: list[RequestRecord] = []

    # Re-checked on every use, not only on save (§6.1). A connection saved when
    # the scope was wider must not keep working after it narrows.
    try:
        check_in_scope(connection.uri, roots)
    except SourceScopeError as exc:
        coverage.listing_failures.append(
            ListingFailureEntry(uri=connection.uri, connection_id=connection.id, error=str(exc))
        )
        report("error", {"connection_id": connection.id, "message": str(exc)})
        return records

    listing = local.list_objects(
        connection.uri,
        roots,
        since=window.start if window else None,
        until=window.end if window else None,
        partition=connection.partition,
    )
    coverage.pruned_by_window += listing.pruned_by_window
    for failure in listing.failures:
        coverage.listing_failures.append(
            ListingFailureEntry(uri=failure.uri, connection_id=connection.id, error=failure.error)
        )

    if not listing.refs and not listing.failures:
        coverage.empty_listings.append(connection.uri)

    coverage.listed_objects += len(listing.refs)
    coverage.listed_bytes += listing.listed_bytes

    adapter = ADAPTERS.get(connection.source)
    if adapter is None:  # pragma: no cover - the config model rejects this first
        raise AdapterError(f"no adapter for source {connection.source!r}")

    for position, ref in enumerate(listing.refs):
        if cancelled():
            raise RunCancelled(
                f"cancelled after {position} of {len(listing.refs)} objects on "
                f"connection {connection.id}"
            )
        entry, object_records = _read_object(
            ref=ref,
            connection=connection,
            adapter=adapter,
            window=window,
            coverage=coverage,
        )
        result.manifest.append(entry)
        records += object_records
        if entry.status == "ok":
            coverage.read_objects += 1
            coverage.read_bytes += ref.size_bytes
        else:
            coverage.failed_objects += 1
            coverage.missing_bytes += ref.size_bytes
            report("error", {"uri": entry.uri, "message": entry.error or "read failed"})

    return records


def _read_object(
    *,
    ref: local.ObjectRef,
    connection: Connection,
    adapter: Callable[..., RequestRecord],
    window: Window | None,
    coverage: Coverage,
) -> tuple[ObjectEntry, list[RequestRecord]]:
    """Decode and interpret one object.

    A partial read is a failure, not less data (§6.1): whatever went wrong, the
    object is marked failed and its full byte volume counts as missing. Records
    parsed before the failure are discarded rather than kept, because a half-read
    object is exactly the "quietly truncated data" case that produces a
    clean-looking run over an incomplete baseline.
    """
    entry = ObjectEntry(
        uri=ref.uri,
        connection_id=connection.id,
        etag=ref.etag,
        size_bytes=ref.size_bytes,
    )
    records: list[RequestRecord] = []

    try:
        detection = decode.detect(
            ref.uri,
            local.peek(ref),
            compression=connection.compression,
            container=connection.format,
        )
    except Exception as exc:
        entry.status = "failed"
        entry.error = _describe(exc)
        return entry, records

    entry.compression = detection.compression
    entry.container = detection.container
    entry.detected_by = detection.by

    counts = decode.DecodeResult()
    try:
        with local.open_object(ref) as handle:
            stream = decode.decompress(handle, detection.compression)
            for raw in decode.iter_records(stream, detection.container, counts, uri=ref.uri):
                try:
                    record = adapter(raw, connection_id=connection.id, object_uri=ref.uri)
                except AdapterError:
                    counts.rejected += 1
                    counts.parsed -= 1
                    continue
                # Log files do not align with audit windows, so records outside
                # the window are dropped after decode (§6.1).
                if window is not None and not window.contains(record.start_time):
                    entry.records_outside_window += 1
                    continue
                records.append(record)
    except RunCancelled:
        raise
    except Exception as exc:
        # One unreadable object is a coverage failure naming it, never a failed
        # run (§6.1). The exception type is kept in the message so a genuine
        # bug is still visible in the manifest rather than reading as bad data.
        entry.status = "failed"
        entry.error = _describe(exc)
        entry.bytes_read = counts.bytes_read
        return entry, []

    entry.bytes_read = counts.bytes_read
    entry.records_parsed = counts.parsed
    entry.records_rejected = counts.rejected
    coverage.records_parsed += counts.parsed
    coverage.records_rejected += counts.rejected
    coverage.records_outside_window += entry.records_outside_window
    return entry, records


def _summarize_slices(records: list[RequestRecord]) -> list[SliceSummary]:
    """Build one summary per `(connection_id, source)` with its fidelity mix (§5.2)."""
    grouped: dict[tuple[str, str], list[RequestRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.connection_id, record.source)].append(record)

    summaries: list[SliceSummary] = []
    for (connection_id, source), group in sorted(grouped.items()):
        counts = Counter(record.fidelity.value for record in group)
        best = max((Fidelity(value) for value in counts), key=lambda f: f.rank)
        summaries.append(
            SliceSummary(
                connection_id=connection_id,
                source=source,
                fidelity=best,
                fidelity_counts=dict(sorted(counts.items())),
                records=len(group),
                observed_start=min(record.start_time for record in group),
                observed_end=max(record.start_time for record in group),
            )
        )
    return summaries


def _describe(exc: Exception) -> str:
    """A message that names what failed without pretending it was expected."""
    if isinstance(exc, DecodeError | AdapterError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _pct(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(index / total * 85)
