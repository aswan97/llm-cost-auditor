"""The normalized records of one run — the `RecordStore` seam (SPEC.md §5.1).

v1 backend is Parquet + polars, in memory, targeting ~500k requests per run on
a laptop. DuckDB replaces this backend when a real dataset misses that target
(AGENTS.md: profile before choosing what to change) — the seam exists so that
swap touches this file and nothing else.

**Analyzers see only `RecordStore`.** They never learn that the backend is a
frame, which is what makes the swap possible.

Columns are explicitly typed. polars infers `f64` for a numeric column when
nobody says otherwise, and that inference is silent — which is the rule money
columns will depend on when pricing lands (§6.4).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import polars as pl

from .records import RequestRecord, from_row, to_row

# Every integer column is declared. Token counts are exact counts, never
# measurements — a float here would be a defect, not an approximation.
_INT_COLUMNS = (
    "attempt_index",
    "latency_ms",
    "http_status",
    "param_max_tokens",
    "param_tool_count",
    "param_seed",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_cache_read_tokens",
    "usage_cache_write_5m_tokens",
    "usage_cache_write_1h_tokens",
    "usage_cache_write_unknown_ttl_tokens",
    "usage_reasoning_tokens",
    "usage_image_tokens",
    "usage_audio_tokens",
    "usage_video_tokens",
    "usage_embedding_tokens",
)

_SCHEMA_OVERRIDES: dict[str, pl.DataType] = {
    **{name: pl.Int64() for name in _INT_COLUMNS},
    "start_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "end_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "param_temperature": pl.Float64(),
    "param_top_p": pl.Float64(),
    "batch": pl.Boolean(),
    "flag_usage_estimated": pl.Boolean(),
    "flag_ttl_class_unknown": pl.Boolean(),
    "flag_duplicate_delivery": pl.Boolean(),
}


class RecordStore:
    """Parquet-backed record storage for one run."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write_records(self, records: list[RequestRecord]) -> int:
        """Persist the run's records. Returns how many were written."""
        frame = to_frame(records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(self.path)
        return frame.height

    def exists(self) -> bool:
        return self.path.exists()

    def count(self) -> int:
        if not self.exists():
            return 0
        return int(pl.scan_parquet(self.path).select(pl.len()).collect().item())

    def iter_records(
        self, predicate: Callable[[RequestRecord], bool] | None = None
    ) -> Iterator[RequestRecord]:
        """Yield records, optionally filtered.

        The predicate is a Python callable rather than an expression because
        analyzers reason about `RequestRecord`, not about columns. When the
        DuckDB backend arrives it may grow a pushdown path; until then, keeping
        the seam honest matters more than the round trip.
        """
        if not self.exists():
            return
        for row in pl.read_parquet(self.path).iter_rows(named=True):
            record = from_row(dict(row))
            if predicate is None or predicate(record):
                yield record

    def head(self, limit: int) -> list[RequestRecord]:
        """The first N records, for the connection preview (§13.1)."""
        if not self.exists():
            return []
        frame = pl.read_parquet(self.path).head(limit)
        return [from_row(dict(row)) for row in frame.iter_rows(named=True)]

    def drop(self) -> None:
        self.path.unlink(missing_ok=True)


def to_frame(records: list[RequestRecord]) -> pl.DataFrame:
    """Build an explicitly typed frame from records.

    An empty run still produces a frame with the full schema, so a downstream
    reader never has to special-case "no records" into "no columns".
    """
    rows = [to_row(record) for record in records]
    if not rows:
        template = _empty_row()
        frame = pl.DataFrame([template], schema_overrides=_SCHEMA_OVERRIDES).clear()
        return frame
    return pl.DataFrame(rows, schema_overrides=_SCHEMA_OVERRIDES)


def _empty_row() -> dict[str, object]:
    from datetime import UTC, datetime

    from .records import RequestRecord as _Record

    return to_row(
        _Record(
            request_id="",
            source="",
            provider="",
            model="",
            connection_id="",
            object_uri="",
            start_time=datetime.now(UTC),
        )
    )
