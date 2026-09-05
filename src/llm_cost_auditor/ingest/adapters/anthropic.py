"""Anthropic source adapter — what the bytes mean (SPEC.md §6.2).

Anthropic exposes `cache_creation_input_tokens` and `cache_read_input_tokens`
in the usage block, which is what makes realized cache savings directly
measurable rather than inferred. This adapter normalizes that into
`RequestRecord` and knows nothing about where the bytes came from.

**Accepted shape.** Anthropic publishes no log export format, so users produce
these themselves — from a client-side logging hook, a gateway, or a warehouse
export. The adapter accepts a per-request JSON object shaped like the Messages
API response plus request metadata, and is tolerant about the envelope: a
record may be at the top level, or nested under `response`/`body`/`message`
with the request under `request`.

This is the first of the source adapters and is deliberately written as a plain
module. The `SourceAdapter` protocol gets extracted once a second adapter
exists, because the abstraction that fits Anthropic alone will be wrong for
Bedrock (AGENTS.md).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...errors import AdapterError
from ...privacy import estimate_tokens, fingerprint, redact_labels
from ...records import (
    Fidelity,
    Flags,
    Labels,
    Params,
    RequestRecord,
    Segment,
    SegmentKind,
    Status,
    Usage,
)

SOURCE = "anthropic"
PROVIDER = "anthropic"

# The two TTL classes the API offers (`cache_control: {"type": "ephemeral",
# "ttl": "1h"}`; 5 minutes is the default). Where a log reports the per-class
# breakdown these are the keys it uses; where it reports only the
# undifferentiated `cache_creation_input_tokens` total, the class is unknown and
# handled per §6.2 rather than guessed at.
TTL_5M_KEY = "ephemeral_5m_input_tokens"
TTL_1H_KEY = "ephemeral_1h_input_tokens"

# `stop_reason: "max_tokens"` means the response was cut off mid-thought and is
# usually re-requested — billed in full, output unusable (§6.5.2).
TRUNCATION_STOP_REASONS = frozenset({"max_tokens"})


def parse(raw: dict[str, Any], *, connection_id: str, object_uri: str) -> RequestRecord:
    """Normalize one raw log record into a `RequestRecord`.

    Raises `AdapterError` for a record that cannot be interpreted. The caller
    counts it as rejected and reports it — a record silently dropped here is
    spend that vanishes from the baseline.
    """
    body = _unwrap(raw)
    request = _as_dict(raw.get("request")) or _as_dict(body.get("request"))

    request_id = _first_str(raw, body, keys=("request_id", "id", "requestId"))
    if not request_id:
        raise AdapterError("record has no request id")

    model = _first_str(raw, body, request, keys=("model",))
    if not model:
        raise AdapterError(f"record {request_id} has no model")

    start_time = _timestamp(raw, body)
    if start_time is None:
        raise AdapterError(f"record {request_id} has no usable timestamp")

    latency_ms = _int(_first(raw, body, keys=("latency_ms", "duration_ms")))
    end_time = _parse_time(_first(raw, body, keys=("end_time", "completed_at")))

    http_status = _int(_first(raw, body, keys=("http_status", "status_code", "statusCode")))
    stop_reason = _first_str(raw, body, keys=("stop_reason", "stopReason"))
    error = _as_dict(raw.get("error")) or _as_dict(body.get("error"))
    usage, ttl_unknown = _usage(_as_dict(raw.get("usage")) or _as_dict(body.get("usage")))

    status = _status(
        raw=raw,
        body=body,
        http_status=http_status,
        stop_reason=stop_reason,
        error=error,
        usage=usage,
    )

    segments, fidelity, estimated = _segments(raw, request)

    record = RequestRecord(
        request_id=request_id,
        source=SOURCE,
        provider=PROVIDER,
        model=model,
        model_version=_first_str(raw, body, keys=("model_version",)),
        connection_id=connection_id,
        object_uri=object_uri,
        start_time=start_time,
        end_time=end_time,
        latency_ms=latency_ms,
        status=status,
        stop_reason=stop_reason,
        http_status=http_status,
        error_code=_error_code(error),
        params=_params(raw, body, request),
        usage=usage,
        flags=Flags(usage_estimated=estimated, ttl_class_unknown=ttl_unknown),
        labels=redact_labels(_labels(raw, body)),
        fidelity=fidelity,
        segments=segments,
        batch=bool(_first(raw, body, keys=("batch", "batch_flag", "is_batch")) or False),
        region=_first_str(raw, body, keys=("region",)),
        deployment_id=_first_str(raw, body, keys=("deployment_id", "deployment")),
    )
    return record


# --- Usage --------------------------------------------------------------------


def _usage(raw: dict[str, Any]) -> tuple[Usage, bool]:
    """Read the usage block, splitting cache writes by TTL class where reported.

    Returns the usage and whether the TTL class is unknown. Unknown-class
    tokens are kept, not dropped — they *were* billed — and the flag is what
    drives the §6.2 handling downstream: priced at the lowest-premium class so
    the baseline understates, with the exposure range reported in coverage.
    """
    creation = _as_dict(raw.get("cache_creation"))
    total_write = _int(raw.get("cache_creation_input_tokens")) or 0

    write_5m = _int(creation.get(TTL_5M_KEY)) or 0
    write_1h = _int(creation.get(TTL_1H_KEY)) or 0
    classified = write_5m + write_1h

    # A breakdown that does not add up to the reported total leaves a remainder
    # of unknown class rather than being silently rebalanced.
    unknown = max(total_write - classified, 0) if classified else total_write

    usage = Usage(
        input_tokens=_int(raw.get("input_tokens")) or 0,
        output_tokens=_int(raw.get("output_tokens")) or 0,
        cache_read_tokens=_int(raw.get("cache_read_input_tokens")) or 0,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        cache_write_unknown_ttl_tokens=unknown,
        reasoning_tokens=_int(raw.get("reasoning_tokens")) or 0,
        image_tokens=_int(raw.get("image_tokens")) or 0,
        audio_tokens=_int(raw.get("audio_tokens")) or 0,
        video_tokens=_int(raw.get("video_tokens")) or 0,
        embedding_tokens=_int(raw.get("embedding_tokens")) or 0,
    )
    return usage, unknown > 0


def _status(
    *,
    raw: dict[str, Any],
    body: dict[str, Any],
    http_status: int | None,
    stop_reason: str | None,
    error: dict[str, Any],
    usage: Usage,
) -> Status:
    """Classify the outcome as it affects billing (§6.5.2).

    The distinctions that matter for cost:

    * a **429** was rejected before generation and costs nothing, so it is
      `error_unbilled` — counting it as billed is the classic double-count;
    * a **5xx after generation** may have been billed in full, so an error
      carrying non-zero usage is `error_billed`;
    * a **`max_tokens` truncation** succeeded as far as the API is concerned
      but is billed for output nobody can use, which is its own finding class;
    * a **cancelled stream** still bills for the tokens generated before the
      client disconnected.
    """
    if _truthy(_first(raw, body, keys=("cancelled", "canceled", "client_disconnected"))):
        return Status.CANCELLED

    explicit = _first_str(raw, body, keys=("status",))
    if explicit in {s.value for s in Status}:
        return Status(explicit)

    if http_status == 429:
        return Status.ERROR_UNBILLED

    failed = bool(error) or (http_status is not None and http_status >= 400)
    if failed:
        billed = usage.input_tokens > 0 or usage.output_tokens > 0 or usage.cache_write_tokens > 0
        return Status.ERROR_BILLED if billed else Status.ERROR_UNBILLED

    if stop_reason in TRUNCATION_STOP_REASONS:
        return Status.TRUNCATED

    return Status.OK


def _error_code(error: dict[str, Any]) -> str | None:
    if not error:
        return None
    code = error.get("type") or error.get("code")
    return str(code) if code is not None else None


def _params(raw: dict[str, Any], body: dict[str, Any], request: dict[str, Any]) -> Params:
    source = {**body, **raw, **request} if request else {**body, **raw}
    tools = source.get("tools")
    thinking = source.get("thinking")
    return Params(
        temperature=_float(source.get("temperature")),
        top_p=_float(source.get("top_p")),
        max_tokens=_int(source.get("max_tokens")),
        tool_count=len(tools) if isinstance(tools, list) else 0,
        tool_choice=_stringify(source.get("tool_choice")),
        thinking=_stringify(thinking.get("type") if isinstance(thinking, dict) else thinking),
        seed=_int(source.get("seed")),
    )


def _labels(raw: dict[str, Any], body: dict[str, Any]) -> Labels:
    explicit = _as_dict(raw.get("labels")) or _as_dict(raw.get("metadata"))
    source = {**body, **raw, **explicit}
    raw_tags = _as_dict(source.get("tags"))
    return Labels(
        api_key_id=_stringify(source.get("api_key_id")),
        project=_stringify(source.get("project")),
        user_id=_stringify(source.get("user_id")),
        session_id=_stringify(source.get("session_id") or source.get("conversation_id")),
        endpoint=_stringify(source.get("endpoint")),
        tags={str(k): str(v) for k, v in raw_tags.items()},
    )


# --- Fidelity and segments ----------------------------------------------------


def _segments(raw: dict[str, Any], request: dict[str, Any]) -> tuple[list[Segment], Fidelity, bool]:
    """Derive hashed segments, and with them the record's fidelity tier (§6.3).

    Tier A input is hashed here and the text is dropped on the way out: raw
    content exists only in memory, for the record being processed (§12 layer 1).
    Tier B input arrives already hashed with per-segment token counts, which is
    the recommended posture and needs no content at all.
    """
    declared = raw.get("segments")
    if isinstance(declared, list) and declared:
        return _declared_segments(declared), Fidelity.HASHED, False

    if request:
        segments, estimated = _hash_request(request)
        if segments:
            return segments, Fidelity.CONTENT, estimated

    # Billing metadata only. Still finds real waste (§6.3 tier C); cache
    # analysis is blocked and becomes an instrumentation finding instead.
    return [], Fidelity.BILLING, False


def _declared_segments(declared: list[Any]) -> list[Segment]:
    segments: list[Segment] = []
    for index, item in enumerate(declared):
        if not isinstance(item, dict):
            raise AdapterError(f"segment {index} is not an object")
        digest = item.get("hash")
        if not digest:
            raise AdapterError(f"segment {index} has no hash")
        token_count = _int(item.get("token_count"))
        if token_count is None:
            # Without a per-segment token count the sidecar cannot support
            # exact cacheable-token math (§6.3), which is the whole reason
            # tier B is not a degraded analysis. Refusing beats inventing one.
            raise AdapterError(f"segment {index} has a hash but no token_count (§6.3)")
        segments.append(
            Segment(
                hash=str(digest),
                token_count=token_count,
                role=_stringify(item.get("role")),
                kind=_segment_kind(item.get("kind")),
                volatile=bool(item.get("volatile", False)),
            )
        )
    return segments


def _hash_request(request: dict[str, Any]) -> tuple[list[Segment], bool]:
    """Hash a tier-A request at its natural breakpoint boundaries.

    Boundaries are the system prompt, each tool definition, and each message —
    which is exactly where a `cache_control` breakpoint can be placed, so the
    segmentation matches what a user can act on (§9.2 stage 1).
    """
    segments: list[Segment] = []
    estimated = False

    system = request.get("system")
    if system:
        text = _flatten_text(system)
        if text:
            segments.append(
                Segment(
                    hash=fingerprint(text),
                    token_count=estimate_tokens(text),
                    role="system",
                    kind=SegmentKind.SYSTEM_PROMPT,
                )
            )
            estimated = True

    tools = request.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            text = _flatten_text(tool)
            if not text:
                continue
            segments.append(
                Segment(
                    hash=fingerprint(text),
                    token_count=estimate_tokens(text),
                    role="tool",
                    kind=SegmentKind.TOOL_DEFINITION,
                )
            )
            estimated = True

    messages = request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            text = _flatten_text(message.get("content") if isinstance(message, dict) else message)
            if not text:
                continue
            segments.append(
                Segment(
                    hash=fingerprint(text),
                    token_count=estimate_tokens(text),
                    role=str(message.get("role")) if isinstance(message, dict) else None,
                    kind=SegmentKind.MESSAGE,
                )
            )
            estimated = True

    return segments, estimated


def _segment_kind(value: Any) -> SegmentKind:
    try:
        return SegmentKind(str(value))
    except ValueError:
        return SegmentKind.OTHER


def _flatten_text(value: Any) -> str:
    """Reduce a content structure to the text it contains.

    The result is hashed and discarded by the caller; it is never stored and
    never returned past this module.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_flatten_text(item) for item in value) if part)
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        return "\n".join(
            f"{key}:{_flatten_text(item)}"
            for key, item in sorted(value.items())
            if item is not None
        )
    return str(value)


# --- Small readers ------------------------------------------------------------


def _unwrap(raw: dict[str, Any]) -> dict[str, Any]:
    for key in ("response", "body", "message"):
        nested = _as_dict(raw.get(key))
        if nested:
            return nested
    return raw


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(*sources: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
    return None


def _first_str(*sources: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return _stringify(_first(*sources, keys=keys))


def _stringify(value: Any) -> str | None:
    if value is None or isinstance(value, dict | list):
        return None
    text = str(value)
    return text or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _timestamp(raw: dict[str, Any], body: dict[str, Any]) -> datetime | None:
    return _parse_time(
        _first(raw, body, keys=("start_time", "timestamp", "created_at", "time", "@timestamp"))
    )


def _parse_time(value: Any) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime.

    A naive timestamp is assumed UTC and *only* here, at the adapter boundary,
    because the record model refuses one outright (§6.6). Epoch seconds and
    milliseconds are distinguished by magnitude, which is unambiguous for any
    date this tool will ever see.
    """
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
