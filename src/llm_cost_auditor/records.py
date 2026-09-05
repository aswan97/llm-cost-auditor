"""The canonical request record (SPEC.md §6.4).

Every source adapter normalizes into `RequestRecord`, and everything
downstream — normalization, profiling, pricing, analyzers — reads only this.
It is a pydantic model from day one because everything else is validated
against it (AGENTS.md).

Two things this module deliberately does not contain:

* **Money.** No monetary field exists yet because pricing is not in this
  release. When it arrives, every such field is an integer count of micro-USD
  (SPEC.md §6.4) and a binary float never touches it.
* **Prompt text.** `segments` carries a hash and a token count per segment and
  nothing else, so the record is safe to persist (SPEC.md §12 layer 1). Raw
  content exists only in memory, inside the adapter, for the request being
  processed.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Fidelity(StrEnum):
    """What a source's logs contain, and therefore what can be analyzed (§6.3)."""

    CONTENT = "A"
    HASHED = "B"
    BILLING = "C"

    @property
    def rank(self) -> int:
        """Higher is more capable. A dataset is classified at the highest tier it supports."""
        return {"A": 3, "B": 2, "C": 1}[self.value]


class Status(StrEnum):
    """Outcome, as it affects billing (§6.4, §6.5.2).

    `error_billed` and `error_unbilled` are separate because the distinction is
    the whole point: input tokens are frequently billed for a request that
    produced nothing usable.
    """

    OK = "ok"
    ERROR_BILLED = "error_billed"
    ERROR_UNBILLED = "error_unbilled"
    CANCELLED = "cancelled"
    TRUNCATED = "truncated"


class SegmentKind(StrEnum):
    """Where a segment sits in the prompt, which is where a breakpoint can go (§9.2)."""

    SYSTEM_PROMPT = "system_prompt"
    TOOL_DEFINITION = "tool_definition"
    MESSAGE = "message"
    OTHER = "other"


class Segment(BaseModel):
    """One hashed prompt segment with its own token count.

    The per-segment token count is what makes exact cacheable-token math
    possible without content (§6.3), so it is required rather than optional.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hash: str
    token_count: int = Field(ge=0)
    role: str | None = None
    kind: SegmentKind = SegmentKind.OTHER
    volatile: bool = False


class Usage(BaseModel):
    """Billed units, by token class (§6.4).

    Cache-write tokens are split per TTL class. When a log reports a single
    undifferentiated total, it lands in `cache_write_unknown_ttl_tokens` and
    the record is flagged `ttl_class_unknown` — that is a pricing ambiguity to
    be surfaced, not a detail to be guessed at (§6.2).
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    cache_write_unknown_ttl_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    image_tokens: int = Field(default=0, ge=0)
    audio_tokens: int = Field(default=0, ge=0)
    video_tokens: int = Field(default=0, ge=0)
    embedding_tokens: int = Field(default=0, ge=0)

    @property
    def cache_write_tokens(self) -> int:
        return (
            self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_write_unknown_ttl_tokens
        )


class Params(BaseModel):
    """Request parameters that gate findings (§6.4, §8.3)."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tool_count: int = Field(default=0, ge=0)
    tool_choice: str | None = None
    thinking: str | None = None
    seed: int | None = None


class Flags(BaseModel):
    """Per-record caveats. Each one drives a confidence penalty downstream (§11.3)."""

    model_config = ConfigDict(extra="forbid")

    usage_estimated: bool = False
    ttl_class_unknown: bool = False
    duplicate_delivery: bool = False


class Labels(BaseModel):
    """Grouping keys the logs carry (§8.2). Redacted before storage (§12 layer 2)."""

    model_config = ConfigDict(extra="forbid")

    api_key_id: str | None = None
    project: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    endpoint: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class RequestRecord(BaseModel):
    """One normalized provider request (SPEC.md §6.4)."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    parent_request_id: str | None = None
    attempt_index: int = Field(default=0, ge=0)

    source: str
    provider: str
    model: str
    model_version: str | None = None

    # Provenance: which slice, which object (§5.2, §6.6). `object_uri` is what
    # makes overlapping sources a reported warning rather than silent
    # double-counted spend (§6.6).
    connection_id: str
    object_uri: str

    # Timezone-aware, stored UTC, converted only for display (§6.6).
    start_time: datetime
    end_time: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)

    status: Status = Status.OK
    stop_reason: str | None = None
    http_status: int | None = None
    error_code: str | None = None

    params: Params = Field(default_factory=Params)
    usage: Usage = Field(default_factory=Usage)
    flags: Flags = Field(default_factory=Flags)
    labels: Labels = Field(default_factory=Labels)

    fidelity: Fidelity = Fidelity.BILLING
    segments: list[Segment] = Field(default_factory=list)

    batch: bool = False
    region: str | None = None
    deployment_id: str | None = None

    @model_validator(mode="after")
    def _require_utc(self) -> Self:
        """Reject a naive timestamp rather than assume a zone for it (§6.6).

        A window with no timezone is a day-boundary bug waiting to be found by
        whoever quotes the number, so the adapter has to have decided.
        """
        for name in ("start_time", "end_time"):
            value: datetime | None = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware; records are stored UTC (§6.6)")
        return self

    def billing_facts(self) -> tuple[Any, ...]:
        """The facts that distinguish a genuine retry from a duplicate delivery (§6.5.1).

        Two records sharing a `request_id` whose billing facts are identical are
        the same event logged twice. If any of these differs, they are separate
        billed attempts and collapsing them would understate the bill.
        """
        return (
            self.start_time,
            self.status,
            self.http_status,
            self.usage.model_dump_json(),
        )


# --- Flat row form, for the record store -------------------------------------
#
# `RecordStore` (SPEC.md §5.1) is the seam DuckDB replaces later, so the flat
# shape lives here next to the model rather than leaking into analyzer code.
# Nested structures are carried as JSON text for now: polars can hold a list of
# structs, but an explicit text column keeps the parquet schema readable and
# the seam is what makes that choice reversible.


def to_row(record: RequestRecord) -> dict[str, Any]:
    """Flatten a record into one storable row."""
    row: dict[str, Any] = {
        "request_id": record.request_id,
        "parent_request_id": record.parent_request_id,
        "attempt_index": record.attempt_index,
        "source": record.source,
        "provider": record.provider,
        "model": record.model,
        "model_version": record.model_version,
        "connection_id": record.connection_id,
        "object_uri": record.object_uri,
        "start_time": record.start_time,
        "end_time": record.end_time,
        "latency_ms": record.latency_ms,
        "status": record.status.value,
        "stop_reason": record.stop_reason,
        "http_status": record.http_status,
        "error_code": record.error_code,
        "fidelity": record.fidelity.value,
        "batch": record.batch,
        "region": record.region,
        "deployment_id": record.deployment_id,
        "segments_json": json.dumps([s.model_dump(mode="json") for s in record.segments]),
        "labels_json": record.labels.model_dump_json(),
    }
    row.update({f"param_{k}": v for k, v in record.params.model_dump().items()})
    row.update({f"usage_{k}": v for k, v in record.usage.model_dump().items()})
    row.update({f"flag_{k}": v for k, v in record.flags.model_dump().items()})
    return row


def from_row(row: dict[str, Any]) -> RequestRecord:
    """Rebuild a record from a stored row. Inverse of `to_row`."""
    prefixed: dict[str, dict[str, Any]] = {"param_": {}, "usage_": {}, "flag_": {}}
    plain: dict[str, Any] = {}
    for key, value in row.items():
        for prefix, bucket in prefixed.items():
            if key.startswith(prefix):
                bucket[key[len(prefix) :]] = value
                break
        else:
            plain[key] = value

    segments = [Segment(**s) for s in json.loads(plain.pop("segments_json") or "[]")]
    labels = Labels.model_validate_json(plain.pop("labels_json") or "{}")
    return RequestRecord(
        **plain,
        params=Params(**prefixed["param_"]),
        usage=Usage(**prefixed["usage_"]),
        flags=Flags(**prefixed["flag_"]),
        segments=segments,
        labels=labels,
    )
