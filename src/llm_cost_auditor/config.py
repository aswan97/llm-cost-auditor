"""Workspace config: the source scope, saved connections, and run settings.

Two of these are load-bearing for reasons beyond convenience:

* **The source scope** (SPEC.md §6.1) is the boundary that makes an
  unauthenticated local server safe. It is editable **only from the terminal** —
  there is no write path to it from the API or the UI — and every connection is
  validated against it on save *and* again on use. Without it, a browser-
  reachable process that borrows the host's ambient cloud identity is an
  exfiltration primitive.
* **The timezone** (§6.6) is declared once and every window, boundary, and
  displayed date is expressed in it. A window with no timezone is a
  day-boundary bug waiting to be found by whoever quotes the number.

The config file holds **no secret material** — only a reference to an ambient
identity (§6.7), which is why it is safe to commit.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError, SourceScopeError

CONFIG_FILENAME = "config.yaml"
DEFAULT_WORKSPACE = Path(".llm-cost-auditor")

# Schemes the spec defines (§6.1). Only `file` is implemented in this release;
# the others are listed so an attempt at one gets the build-order answer rather
# than "unknown scheme".
KNOWN_SCHEMES = ("file", "s3", "az")
IMPLEMENTED_SCHEMES = ("file",)

Connector = Literal["file", "s3", "az"]
SourceName = Literal["anthropic", "openai", "bedrock", "vertex", "foundry"]
IMPLEMENTED_SOURCES: tuple[str, ...] = ("anthropic",)


class AmbientAuth(BaseModel):
    """A *reference* to an ambient identity, never a secret (SPEC.md §6.7).

    v1 stores no credentials: identity resolves through the cloud's own default
    chain and the config names which one to use. There is deliberately no field
    here that could hold a value.
    """

    model_config = ConfigDict(extra="forbid")

    profile: str | None = None
    role_arn: str | None = None
    credential: str | None = None


class Connection(BaseModel):
    """A named binding of a connector to a location and a source (SPEC.md §6.6)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    connector: Connector = "file"
    uri: str
    source: SourceName = "anthropic"
    format: str = "auto"
    compression: str = "auto"
    partition: str | None = None
    region: str | None = None
    account: str | None = None
    auth: AmbientAuth = Field(default_factory=AmbientAuth)

    @model_validator(mode="after")
    def _check_supported(self) -> Self:
        if self.connector not in IMPLEMENTED_SCHEMES:
            raise ValueError(
                f"connector {self.connector!r} is specified but not implemented in this "
                f"release. Build order is local, then S3, then the extracted protocol "
                f"(SPEC.md §6.1)."
            )
        if self.source not in IMPLEMENTED_SOURCES:
            raise ValueError(
                f"source {self.source!r} is specified but has no adapter yet. "
                f"Implemented: {', '.join(IMPLEMENTED_SOURCES)} (SPEC.md §6.2)."
            )
        return self


class CoverageConfig(BaseModel):
    """Thresholds for the §6.1 gating table.

    `max_missing_pct` is a share of *listed bytes*, not of objects: one
    unreadable multi-gigabyte object matters more than fifty empty ones.
    """

    model_config = ConfigDict(extra="forbid")

    max_missing_pct: float = Field(default=2.0, ge=0.0, le=100.0)


class WorkspaceConfig(BaseModel):
    """Everything the workspace declares. Lives at `<workspace>/config.yaml`."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = "UTC"
    sources: list[str] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    retention_days: int = Field(default=30, ge=1)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _unique_connection_ids(self) -> Self:
        seen: set[str] = set()
        for connection in self.connections:
            if connection.id in seen:
                raise ValueError(f"duplicate connection id {connection.id!r}")
            seen.add(connection.id)
        return self

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def connection(self, connection_id: str) -> Connection:
        for connection in self.connections:
            if connection.id == connection_id:
                return connection
        known = ", ".join(c.id for c in self.connections) or "none configured"
        raise ConfigError(f"no connection named {connection_id!r} (known: {known})")


# --- The source scope ---------------------------------------------------------


def normalize_root(root: str) -> str:
    """Canonicalize a scope root so comparisons are not string-prefix guesswork."""
    scheme, remainder = split_scheme(root)
    if scheme == "file":
        return "file://" + str(Path(remainder).expanduser().resolve())
    return f"{scheme}://" + remainder.strip("/")


def split_scheme(uri: str) -> tuple[str, str]:
    """Split `scheme://rest`, defaulting a bare path to the `file` scheme."""
    for scheme in KNOWN_SCHEMES:
        prefix = f"{scheme}://"
        if uri.startswith(prefix):
            return scheme, uri[len(prefix) :]
    if "://" in uri:
        scheme = uri.split("://", 1)[0]
        raise ConfigError(
            f"unsupported uri scheme {scheme!r}. Supported: "
            f"{', '.join(f'{s}://' for s in KNOWN_SCHEMES)} (SPEC.md §6.1)."
        )
    return "file", uri


def literal_prefix(uri: str) -> str:
    """The part of a uri before any glob metacharacter.

    A glob is checked against the scope by its fixed prefix, and every path it
    actually resolves to is checked again — the second check is what matters,
    because `*` cannot be reasoned about statically.
    """
    for index, char in enumerate(uri):
        if char in "*?[":
            return uri[:index].rsplit("/", 1)[0]
    return uri


def check_in_scope(uri: str, roots: list[str]) -> str:
    """Return the canonical uri, or raise if it is outside every permitted root.

    Called on connection save and again on every use (SPEC.md §6.1, §12). A
    workspace with no roots configured permits nothing, which is the safe
    direction: a server that can read everything by default is the failure this
    prevents.
    """
    scheme, remainder = split_scheme(uri)
    if scheme == "file":
        candidate = "file://" + str(Path(literal_prefix(remainder)).expanduser().resolve())
    else:
        candidate = f"{scheme}://" + literal_prefix(remainder).strip("/")

    normalized = [normalize_root(root) for root in roots]
    for root in normalized:
        if candidate == root or candidate.startswith(root.rstrip("/") + "/"):
            return candidate
    scope = ", ".join(normalized) or "empty — add one with `llm-cost-auditor sources add <root>`"
    raise SourceScopeError(
        f"{uri!r} is outside the configured source scope. Permitted roots: {scope}. "
        f"The scope is editable only from the terminal (SPEC.md §6.1)."
    )


def check_path_in_scope(path: Path, roots: list[str]) -> None:
    """Second-stage check for a concrete path a glob or listing produced."""
    resolved = path.expanduser().resolve()
    for root in (normalize_root(r) for r in roots):
        if not root.startswith("file://"):
            continue
        root_path = Path(root[len("file://") :])
        if resolved == root_path or resolved.is_relative_to(root_path):
            return
    raise SourceScopeError(
        f"{path} resolves outside the configured source scope (SPEC.md §6.1). "
        f"A symlink or `..` in a glob is the usual cause."
    )


def matches_root(uri: str, root: str) -> bool:
    """Whether a uri sits under a root, for display purposes only."""
    try:
        check_in_scope(uri, [root])
    except SourceScopeError:
        return False
    return True


def glob_matches(pattern: str, candidate: str) -> bool:
    """fnmatch with `/` treated literally, so `*` does not cross directories."""
    if "/" in pattern:
        pattern_parts = pattern.split("/")
        candidate_parts = candidate.split("/")
        if len(pattern_parts) != len(candidate_parts):
            return False
        pairs = zip(pattern_parts, candidate_parts, strict=True)
        return all(fnmatch.fnmatch(c, p) for p, c in pairs)
    return fnmatch.fnmatch(candidate, pattern)


# --- Load and save ------------------------------------------------------------


def config_path(workspace: Path) -> Path:
    return workspace / CONFIG_FILENAME


def load(workspace: Path) -> WorkspaceConfig:
    """Load the workspace config, or return defaults if it does not exist yet.

    Defaults mean an empty source scope, so a fresh workspace can read nothing
    until someone at a terminal says otherwise.
    """
    path = config_path(workspace)
    if not path.exists():
        return WorkspaceConfig()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    try:
        return WorkspaceConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} is invalid:\n{exc}") from exc


def save(workspace: Path, config: WorkspaceConfig) -> None:
    """Write the workspace config back, atomically."""
    workspace.mkdir(parents=True, exist_ok=True)
    path = config_path(workspace)
    payload = config.model_dump(mode="json", exclude_defaults=False)
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    temporary.replace(path)
