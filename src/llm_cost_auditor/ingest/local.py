"""The local-files connector (SPEC.md §6.1).

This is the first of three connectors and it is written as a plain module, not
against an abstract protocol. The `Connector` interface is *extracted* once S3
exists, because an interface designed around a local directory would be wrong
about pagination, listing cost, retries, and credentials (AGENTS.md).

What it does know is the part every connector shares: locate objects, prune the
listing with the audit window, and hand back byte streams. It knows nothing
about providers, formats, or what a record means.
"""

from __future__ import annotations

import glob as globlib
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from ..config import check_path_in_scope, split_scheme

# Prefix expansion is deliberately inclusive at the edges (§6.1): a partition
# written in one zone and a window expressed in another otherwise silently
# loses a day.
EDGE_MARGIN = timedelta(days=1)


@dataclass(frozen=True)
class ObjectRef:
    """One locatable object. The shape every connector will yield."""

    uri: str
    size_bytes: int
    last_modified: datetime
    etag: str

    @property
    def path(self) -> Path:
        _, remainder = split_scheme(self.uri)
        return Path(remainder)


@dataclass(frozen=True)
class ListingFailure:
    """A location that could not be listed.

    A listing that failed is missing data, not less data (§6.1), so it travels
    with the listing rather than being raised — the run needs to report it in
    coverage alongside what did succeed.
    """

    uri: str
    error: str


@dataclass
class Listing:
    """What a connector found, and what it could not reach."""

    refs: list[ObjectRef] = field(default_factory=list)
    failures: list[ListingFailure] = field(default_factory=list)
    pruned_by_window: int = 0

    @property
    def listed_bytes(self) -> int:
        return sum(ref.size_bytes for ref in self.refs)


def _etag(stat_size: int, mtime_ns: int) -> str:
    """A stable identity for a local file.

    Object stores hand out an etag; a filesystem does not, so this stands in for
    one. It is derived from size and mtime rather than content because hashing
    every byte of every object to list it would cost more than reading it —
    which means it detects a rewritten file, not a byte-identical touch. The
    manifest records it either way, so a re-audit whose total differs has a
    stated cause (§6.6).
    """
    digest = hashlib.sha256(f"{stat_size}:{mtime_ns}".encode()).hexdigest()[:32]
    return f"mtime-size:{digest}"


def _partition_prefixes(
    template: str, since: datetime | None, until: datetime | None
) -> set[str] | None:
    """Expand an audit window into the key prefixes it can possibly touch.

    Returns None when no pruning is possible, so a caller cannot mistake "no
    template" for "nothing matches".
    """
    if since is None or until is None:
        return None
    step = timedelta(hours=1) if "%H" in template else timedelta(days=1)
    cursor = (since - EDGE_MARGIN).astimezone(UTC)
    end = (until + EDGE_MARGIN).astimezone(UTC)
    prefixes: set[str] = set()
    # Object partition templates are expanded in the storage's own zone, which
    # is UTC for every v1 connector (§6.6).
    while cursor <= end:
        prefixes.add(cursor.strftime(template))
        cursor += step
    return prefixes


def list_objects(
    uri: str,
    roots: list[str],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    partition: str | None = None,
) -> Listing:
    """List the objects a uri names, pruned to the window.

    `uri` may be a file, a directory (walked recursively), or a glob. Every
    resolved path is re-checked against the source scope, because a symlink or
    a `..` inside a glob can leave the permitted area that the pattern's fixed
    prefix appeared to stay inside (§6.1).
    """
    _, remainder = split_scheme(uri)
    listing = Listing()
    prefixes = _partition_prefixes(partition, since, until) if partition else None

    for path in _walk(remainder, listing):
        try:
            check_path_in_scope(path, roots)
        except Exception as exc:
            listing.failures.append(ListingFailure(uri=f"file://{path}", error=str(exc)))
            continue

        try:
            stat = path.stat()
        except OSError as exc:
            listing.failures.append(ListingFailure(uri=f"file://{path}", error=str(exc)))
            continue

        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        object_uri = f"file://{path}"

        if prefixes is not None and not any(p in object_uri for p in prefixes):
            listing.pruned_by_window += 1
            continue

        # Only the lower edge is safe to prune on modification time: a file last
        # written before the window began cannot contain a record inside it.
        # The upper edge is not pruned, because delivery lag routinely writes
        # in-window records to an object long after the window closed — records
        # outside the window are dropped after decode instead (§6.1).
        if since is not None and modified < since - EDGE_MARGIN:
            listing.pruned_by_window += 1
            continue

        listing.refs.append(
            ObjectRef(
                uri=object_uri,
                size_bytes=stat.st_size,
                last_modified=modified,
                etag=_etag(stat.st_size, stat.st_mtime_ns),
            )
        )

    listing.refs.sort(key=lambda ref: ref.uri)
    return listing


def _walk(pattern: str, listing: Listing) -> Iterator[Path]:
    """Yield candidate files for a path, directory, or glob."""
    expanded = str(Path(pattern).expanduser())

    if any(char in expanded for char in "*?["):
        for match in sorted(globlib.glob(expanded, recursive=True)):
            candidate = Path(match)
            if candidate.is_file():
                yield candidate
        return

    root = Path(expanded)
    if root.is_file():
        yield root
        return
    if not root.exists():
        listing.failures.append(
            ListingFailure(uri=f"file://{root}", error="no such file or directory")
        )
        return

    try:
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        listing.failures.append(ListingFailure(uri=f"file://{root}", error=str(exc)))
        return

    for entry in entries:
        try:
            if entry.is_file():
                yield entry
        except OSError as exc:
            listing.failures.append(ListingFailure(uri=f"file://{entry}", error=str(exc)))


def open_object(ref: ObjectRef) -> BinaryIO:
    """Open an object as a byte stream.

    Streaming, never download-and-parse (§6.1): a day of logs is routinely
    larger than RAM, and the in-memory budget is for normalized records.
    """
    return ref.path.open("rb")


def peek(ref: ObjectRef, size: int = 512) -> bytes:
    """Read the first bytes of an object, for format detection (§6.1)."""
    with ref.path.open("rb") as handle:
        return handle.read(size)
