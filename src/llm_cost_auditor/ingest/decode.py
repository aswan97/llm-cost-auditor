"""Compression and container format — the layer between bytes and meaning.

Every connector yields bytes and every adapter interprets records, so
decompression and container parsing belong to neither and live here once
(SPEC.md §6.1). Detection is auto by default and **always reported**: the run
record states the format and compression chosen per object, and an object that
does not parse as its detected format is a coverage failure rather than a
skipped line.
"""

from __future__ import annotations

import bz2
import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any, BinaryIO, cast

from ..errors import DecodeError

COMPRESSIONS = ("none", "gzip", "bzip2", "zstd")
CONTAINERS = ("jsonl", "json")

# Container formats the spec defines but that arrive with the sources producing
# them: CSV and Parquet with warehouse extracts, the CloudWatch and Azure
# Monitor envelopes with Bedrock and Foundry. Detecting them and saying so
# beats detecting them and guessing.
DEFERRED_CONTAINERS = {
    "csv": "CSV arrives with the warehouse/billing-export sources (SPEC.md §6.1)",
    "parquet": "Parquet arrives with the warehouse/billing-export sources (SPEC.md §6.1)",
    "cloudwatch_export": "the CloudWatch envelope arrives with the Bedrock adapter (SPEC.md §6.1)",
    "azure_monitor": "the Azure Monitor envelope arrives with the Foundry adapter (SPEC.md §6.1)",
}

# A JSON array or single object has to be materialized to be parsed, unlike
# JSON Lines. That is acceptable for the small exports and API dumps this
# format shows up in (§6.1), and refusing past a bound is better than an
# out-of-memory kill halfway through a run.
MAX_WHOLE_DOCUMENT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class Detection:
    """What an object was determined to be, and how."""

    compression: str
    container: str
    by: str  # "declared" | "extension" | "magic"


def detect(
    name: str,
    head: bytes,
    *,
    compression: str = "auto",
    container: str = "auto",
) -> Detection:
    """Detect compression and container from the key and the first bytes."""
    by = "declared"

    if compression == "auto":
        compression, compression_by = _detect_compression(name, head)
        by = compression_by

    if container == "auto":
        container, container_by = _detect_container(name, head, compression)
        by = container_by if by == "declared" else by

    if compression not in COMPRESSIONS:
        raise DecodeError(f"unknown compression {compression!r}; expected one of {COMPRESSIONS}")
    if container in DEFERRED_CONTAINERS:
        raise DecodeError(
            f"{name}: detected container {container!r}, which is not decodable yet — "
            f"{DEFERRED_CONTAINERS[container]}"
        )
    if container not in CONTAINERS:
        raise DecodeError(f"unknown container {container!r}; expected one of {CONTAINERS}")

    return Detection(compression=compression, container=container, by=by)


def _detect_compression(name: str, head: bytes) -> tuple[str, str]:
    if head.startswith(b"\x1f\x8b"):
        return "gzip", "magic"
    if head.startswith(b"BZh"):
        return "bzip2", "magic"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd", "magic"
    lowered = name.lower()
    for suffix, value in ((".gz", "gzip"), (".bz2", "bzip2"), (".zst", "zstd")):
        if lowered.endswith(suffix):
            return value, "extension"
    return "none", "magic"


def _detect_container(name: str, head: bytes, compression: str) -> tuple[str, str]:
    lowered = name.lower()
    for suffix in (".gz", ".bz2", ".zst"):
        lowered = lowered.removesuffix(suffix)

    for suffix, value in (
        (".jsonl", "jsonl"),
        (".ndjson", "jsonl"),
        (".json", "json"),
        (".csv", "csv"),
        (".parquet", "parquet"),
    ):
        if lowered.endswith(suffix):
            return value, "extension"

    if compression == "none":
        stripped = head.lstrip()
        if stripped.startswith(b"["):
            return "json", "magic"
        if stripped.startswith(b"{"):
            # A JSON Lines file also starts with `{`. The difference is whether
            # the first line is a complete document, which is cheap to test and
            # far more reliable than the extension alone.
            first_line = stripped.split(b"\n", 1)[0]
            try:
                json.loads(first_line)
            except ValueError:
                return "json", "magic"
            return "jsonl", "magic"
        if head.startswith(b"PAR1"):
            return "parquet", "magic"

    # Compressed with no useful extension: JSON Lines is the common case, and
    # being wrong surfaces as a decode failure naming the object, not a silent
    # skip (§6.1).
    return "jsonl", "extension"


def decompress(stream: BinaryIO, compression: str) -> IO[bytes]:
    """Wrap a byte stream in the right decompressor. Nothing is buffered whole."""
    if compression == "none":
        return stream
    if compression == "gzip":
        return cast("IO[bytes]", gzip.GzipFile(fileobj=stream, mode="rb"))
    if compression == "bzip2":
        return bz2.BZ2File(stream, mode="rb")
    if compression == "zstd":
        try:
            import zstandard
        except ImportError as exc:  # pragma: no cover - exercised by the extras path
            raise DecodeError(
                "zstd-compressed object needs the zstd extra: install `llm-cost-auditor[zstd]`"
            ) from exc
        return cast("IO[bytes]", zstandard.ZstdDecompressor().stream_reader(stream))
    raise DecodeError(f"unknown compression {compression!r}")


@dataclass
class DecodeResult:
    """Counts from decoding one object, for the manifest (§6.6)."""

    parsed: int = 0
    rejected: int = 0
    bytes_read: int = 0


def iter_records(
    stream: IO[bytes], container: str, result: DecodeResult, *, uri: str
) -> Iterator[dict[str, Any]]:
    """Yield raw record dicts from a decoded stream.

    A line that is not valid JSON is counted as rejected and reported; a stream
    that fails structurally — a truncated gzip member, a document that is not
    the container it was detected as — raises `DecodeError`, which the caller
    turns into a coverage failure naming the object (§6.1).
    """
    if container == "jsonl":
        yield from _iter_jsonl(stream, result, uri=uri)
    elif container == "json":
        yield from _iter_json(stream, result, uri=uri)
    else:  # pragma: no cover - `detect` rejects anything else first
        raise DecodeError(f"unknown container {container!r}")


def _iter_jsonl(stream: IO[bytes], result: DecodeResult, *, uri: str) -> Iterator[dict[str, Any]]:
    line_number = 0
    while True:
        try:
            line = stream.readline()
        except Exception as exc:
            # A truncated or corrupt member surfaces here, mid-object.
            # Deliberately broad: each decompressor raises its own error type
            # and none of them share a base — gzip raises `zlib.error` (which
            # is not an `OSError`), bz2 raises `OSError`, zstd raises
            # `ZstdError`. A stream that fails to decompress is always a
            # coverage failure naming the object (§6.1), never a dead run, so
            # the classification cannot depend on getting that list right.
            raise DecodeError(
                f"{uri}: stream failed after {result.parsed} records: {type(exc).__name__}: {exc}"
            ) from exc
        if not line:
            return
        line_number += 1
        result.bytes_read += len(line)
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except ValueError:
            result.rejected += 1
            continue
        if not isinstance(value, dict):
            result.rejected += 1
            continue
        result.parsed += 1
        yield value


def _iter_json(stream: IO[bytes], result: DecodeResult, *, uri: str) -> Iterator[dict[str, Any]]:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = stream.read(1 << 20)
        except Exception as exc:  # see `_iter_jsonl`: decompressors share no base class
            raise DecodeError(
                f"{uri}: stream failed mid-object: {type(exc).__name__}: {exc}"
            ) from exc
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_WHOLE_DOCUMENT_BYTES:
            raise DecodeError(
                f"{uri}: JSON document exceeds {MAX_WHOLE_DOCUMENT_BYTES} bytes. "
                f"JSON Lines streams; a single document cannot."
            )
        chunks.append(chunk)

    result.bytes_read += total
    try:
        document = json.loads(b"".join(chunks))
    except ValueError as exc:
        raise DecodeError(f"{uri}: not valid JSON: {exc}") from exc

    if isinstance(document, dict):
        # `{"records": [...]}` is the shape small exports and API dumps use.
        for key in ("records", "data", "items", "logEvents"):
            if isinstance(document.get(key), list):
                document = document[key]
                break
        else:
            document = [document]

    if not isinstance(document, list):
        raise DecodeError(f"{uri}: JSON document is neither an array nor a record wrapper")

    for value in document:
        if isinstance(value, dict):
            result.parsed += 1
            yield value
        else:
            result.rejected += 1
