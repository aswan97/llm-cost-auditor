"""The CLI (SPEC.md §13.3).

Every operation the app performs is a CLI command first, and both call the same
run engine. Nothing is reachable only through a browser.

Two rules from the spec show up directly in the argument shapes here:

* **`--run <id>` is how stages compose, and it is not optional.** There is no
  implicit "most recent ingest" anywhere — a command that guesses which data it
  is analyzing is one that silently analyzes the wrong data, and the failure
  surfaces as a plausible number rather than an error.
* **The source scope is terminal-only.** `sources add` exists here and has no
  counterpart in the API (§6.1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from . import config as config_module
from . import engine
from . import window as window_module
from .config import DEFAULT_WORKSPACE, Connection, WorkspaceConfig, check_in_scope
from .errors import AuditorError
from .ingest import decode, local
from .ingest.adapters import anthropic
from .record_store import RecordStore
from .records import RequestRecord
from .run_store import RECORDS_PARQUET, RunRequest, RunStatus, RunStore, Stage

app = typer.Typer(
    name="llm-cost-auditor",
    help="Audit LLM provider API logs for caching, batching, and routing savings.",
    no_args_is_help=True,
    add_completion=False,
)
sources_app = typer.Typer(help="The source scope (SPEC.md §6.1) — terminal only.")
connections_app = typer.Typer(help="Saved log sources (SPEC.md §6.6).")
runs_app = typer.Typer(help="The run store (SPEC.md §5.3).")
app.add_typer(sources_app, name="sources")
app.add_typer(connections_app, name="connections")
app.add_typer(runs_app, name="runs")

# `LCA_WORKSPACE` exists so a container (or a shell profile) can pin the
# workspace once instead of repeating it on every command. It is a default, not
# an override: an explicit `--workspace` always wins.
WorkspaceOption = Annotated[
    Path,
    typer.Option(
        "--workspace",
        "-w",
        envvar="LCA_WORKSPACE",
        help="Workspace root holding config and runs.",
    ),
]


def _out(message: str = "") -> None:
    typer.echo(message)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


# --- sources ------------------------------------------------------------------


@sources_app.command("list")
def sources_list(workspace: WorkspaceOption = DEFAULT_WORKSPACE) -> None:
    """Show the permitted roots every read is confined to."""
    settings = config_module.load(workspace)
    if not settings.sources:
        _out("Source scope is empty — nothing can be read.")
        _out("Add a root with: llm-cost-auditor sources add ./logs")
        return
    _out("Permitted roots (editable only from a terminal):")
    for root in settings.sources:
        _out(f"  {config_module.normalize_root(root)}")


@sources_app.command("add")
def sources_add(
    root: Annotated[str, typer.Argument(help="A path, or bucket/container prefix.")],
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Widen the source scope. This is the one place it can be widened."""
    settings = config_module.load(workspace)
    normalized = config_module.normalize_root(root)
    if normalized in {config_module.normalize_root(r) for r in settings.sources}:
        _out(f"{normalized} is already in scope.")
        return
    if normalized in ("file:///", "file://"):
        typer.secho(
            "warning: adding the filesystem root restores exactly the exfiltration primitive "
            "the source scope exists to prevent (SPEC.md §15.12). Prefer a narrow root.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    settings.sources.append(normalized)
    config_module.save(workspace, settings)
    _out(f"Added {normalized}")


@sources_app.command("rm")
def sources_rm(
    root: Annotated[str, typer.Argument(help="The root to remove.")],
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Narrow the source scope. Connections outside it stop working immediately."""
    settings = config_module.load(workspace)
    normalized = config_module.normalize_root(root)
    remaining = [r for r in settings.sources if config_module.normalize_root(r) != normalized]
    if len(remaining) == len(settings.sources):
        _fail(f"{normalized} is not in the source scope")
    settings.sources = remaining
    config_module.save(workspace, settings)
    _out(f"Removed {normalized}")

    orphaned = [c.id for c in settings.connections if not _in_scope(c, settings)]
    if orphaned:
        _out(f"These connections are now outside the scope: {', '.join(orphaned)}")


def _in_scope(connection: Connection, settings: WorkspaceConfig) -> bool:
    try:
        check_in_scope(connection.uri, settings.sources)
    except AuditorError:
        return False
    return True


# --- connections --------------------------------------------------------------


@connections_app.command("list")
def connections_list(workspace: WorkspaceOption = DEFAULT_WORKSPACE) -> None:
    """List saved connections and whether each is still inside the source scope."""
    settings = config_module.load(workspace)
    if not settings.connections:
        _out("No connections configured.")
        return
    for connection in settings.connections:
        mark = "ok " if _in_scope(connection, settings) else "OUT"
        _out(f"[{mark}] {connection.id:<24} {connection.source:<10} {connection.uri}")
    _out()
    _out("[OUT] means the uri is outside the source scope and the connection cannot be used.")


@connections_app.command("add")
def connections_add(
    connection_id: Annotated[str, typer.Argument(metavar="ID")],
    uri: Annotated[str, typer.Option("--uri", help="file:///path, a path, or a glob.")],
    source: Annotated[str, typer.Option("--source", help="Log source adapter.")] = "anthropic",
    log_format: Annotated[str, typer.Option("--format")] = "auto",
    compression: Annotated[str, typer.Option("--compression")] = "auto",
    partition: Annotated[
        str | None, typer.Option("--partition", help="e.g. 'y=%Y/m=%m/d=%d/'")
    ] = None,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Save a connection. Rejected unless the uri falls inside the source scope."""
    settings = config_module.load(workspace)
    try:
        check_in_scope(uri, settings.sources)
        connection = Connection(
            id=connection_id,
            uri=uri,
            source=source,  # type: ignore[arg-type]
            format=log_format,
            compression=compression,
            partition=partition,
        )
    except (AuditorError, ValueError) as exc:
        _fail(str(exc))
        return

    settings.connections = [c for c in settings.connections if c.id != connection_id] + [connection]
    config_module.save(workspace, settings)
    _out(f"Saved connection {connection_id}")


@connections_app.command("rm")
def connections_rm(
    connection_id: Annotated[str, typer.Argument(metavar="ID")],
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Delete a connection, naming the runs that referenced it."""
    settings = config_module.load(workspace)
    remaining = [c for c in settings.connections if c.id != connection_id]
    if len(remaining) == len(settings.connections):
        _fail(f"no connection named {connection_id!r}")
    settings.connections = remaining
    config_module.save(workspace, settings)

    referencing = [
        record.run_id
        for record in RunStore(workspace).list()
        if connection_id in record.request.connection_ids
    ]
    _out(f"Deleted connection {connection_id}")
    if referencing:
        _out(f"Referenced by {len(referencing)} run(s): {', '.join(referencing[:10])}")


@connections_app.command("test")
def connections_test(
    connection_id: Annotated[str, typer.Argument(metavar="ID")],
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Resolve identity, list the prefix, and name the permissions exercised."""
    settings = config_module.load(workspace)
    try:
        connection = settings.connection(connection_id)
        check_in_scope(connection.uri, settings.sources)
    except AuditorError as exc:
        _fail(str(exc))
        return

    listing = local.list_objects(connection.uri, settings.sources)
    _out(f"Connection : {connection.id} ({connection.connector} → {connection.source})")
    _out(f"URI        : {connection.uri}")
    # The `file` connector resolves no credential at all: the OS decides what
    # the process can read, bounded by the source scope (§6.7).
    _out("Identity   : none — the OS decides what this process can read (§6.7)")
    _out(f"Objects    : {len(listing.refs)}")
    _out(f"Bytes      : {listing.listed_bytes}")
    _out("Exercised  : directory listing (stat), file open (read)")
    for failure in listing.failures:
        typer.secho(f"  unreadable: {failure.uri}: {failure.error}", fg=typer.colors.YELLOW)
    if not listing.refs and not listing.failures:
        typer.secho(
            "  matched no objects — 'no traffic' and 'no logs' look identical from here (§15.9)",
            fg=typer.colors.YELLOW,
        )


@connections_app.command("peek")
def connections_peek(
    connection_id: Annotated[str, typer.Argument(metavar="ID")],
    limit: Annotated[int, typer.Option("-n", "--limit")] = 20,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Decode the first N records: detected format, fidelity, and time range."""
    settings = config_module.load(workspace)
    try:
        connection = settings.connection(connection_id)
        check_in_scope(connection.uri, settings.sources)
        preview = peek_connection(connection, settings, limit)
    except AuditorError as exc:
        _fail(str(exc))
        return

    if not preview["records"]:
        _out("No records decoded.")
        for problem in preview["problems"]:
            typer.secho(f"  {problem}", fg=typer.colors.YELLOW)
        return

    records: list[RequestRecord] = preview["records"]
    _out(f"Decoded {len(records)} record(s) from {preview['objects']} object(s)")
    _out(
        f"Detected   : {preview['compression']} / {preview['container']} ({preview['detected_by']})"
    )
    _out(f"Source     : {connection.source}")
    _out(f"Fidelity   : {preview['fidelity']} (per-record mix: {preview['fidelity_counts']})")
    _out(f"Time range : {preview['observed_start']} .. {preview['observed_end']} (UTC)")
    _out()
    _out(f"{'start_time':<26}{'model':<26}{'status':<15}{'in':>9}{'out':>9}{'c-read':>9}")
    for record in records[:limit]:
        _out(
            f"{record.start_time.isoformat():<26}{record.model[:25]:<26}"
            f"{record.status.value:<15}{record.usage.input_tokens:>9}"
            f"{record.usage.output_tokens:>9}{record.usage.cache_read_tokens:>9}"
        )
    for problem in preview["problems"]:
        typer.secho(f"  {problem}", fg=typer.colors.YELLOW)


def peek_connection(
    connection: Connection, settings: WorkspaceConfig, limit: int
) -> dict[str, Any]:
    """Decode the first N records of a connection without starting a run.

    Shared by the CLI and the API so the two cannot disagree about what a
    connection contains (§13.1).
    """
    listing = local.list_objects(connection.uri, settings.sources)
    records: list[RequestRecord] = []
    problems: list[str] = [f"{f.uri}: {f.error}" for f in listing.failures]
    detection = None
    objects = 0
    counts: dict[str, int] = {}

    for ref in listing.refs:
        if len(records) >= limit:
            break
        objects += 1
        try:
            detected = decode.detect(
                ref.uri,
                local.peek(ref),
                compression=connection.compression,
                container=connection.format,
            )
            detection = detection or detected
            result = decode.DecodeResult()
            with local.open_object(ref) as handle:
                stream = decode.decompress(handle, detected.compression)
                for raw in decode.iter_records(stream, detected.container, result, uri=ref.uri):
                    try:
                        records.append(
                            anthropic.parse(raw, connection_id=connection.id, object_uri=ref.uri)
                        )
                    except AuditorError as exc:
                        problems.append(f"{ref.uri}: {exc}")
                    if len(records) >= limit:
                        break
        except (AuditorError, OSError) as exc:
            problems.append(f"{ref.uri}: {exc}")

    for record in records:
        counts[record.fidelity.value] = counts.get(record.fidelity.value, 0) + 1
    best = max(counts, key=lambda tier: {"A": 3, "B": 2, "C": 1}[tier]) if counts else "-"

    return {
        "records": records,
        "objects": objects,
        "problems": problems,
        "compression": detection.compression if detection else "-",
        "container": detection.container if detection else "-",
        "detected_by": detection.by if detection else "-",
        "fidelity": best,
        "fidelity_counts": counts,
        "observed_start": min((r.start_time for r in records), default=None),
        "observed_end": max((r.start_time for r in records), default=None),
    }


# --- run stages ---------------------------------------------------------------


@app.command("ingest")
def ingest_command(
    connection_ids: Annotated[list[str], typer.Argument(metavar="CONNECTION_ID...")],
    audit_window: Annotated[
        str | None, typer.Option("--window", help="2026-08-01..2026-08-31, in the workspace zone.")
    ] = None,
    timezone: Annotated[str | None, typer.Option("--timezone", help="IANA name.")] = None,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Read the named connections into a new run. Sugar for `run --stop-after ingest`."""
    _start_run(connection_ids, audit_window, timezone, workspace, Stage.INGEST)


@app.command("run")
def run_command(
    connection_ids: Annotated[list[str], typer.Argument(metavar="CONNECTION_ID...")],
    audit_window: Annotated[str | None, typer.Option("--window")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone")] = None,
    stop_after: Annotated[
        Stage | None, typer.Option("--stop-after", help="Halt after this stage.")
    ] = None,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Run the pipeline over one or more connections."""
    if stop_after is not Stage.INGEST:
        _fail(
            "only the ingest stage exists in this build. Use `--stop-after ingest` (or the "
            "`ingest` command). The profile and audit stages, and the analyzers they feed, "
            "are the next slice (SPEC.md §4)."
        )
    _start_run(connection_ids, audit_window, timezone, workspace, Stage.INGEST)


def _start_run(
    connection_ids: list[str],
    audit_window: str | None,
    timezone: str | None,
    workspace: Path,
    stop_after: Stage,
) -> None:
    settings = config_module.load(workspace)
    zone = timezone or settings.timezone
    try:
        for connection_id in connection_ids:
            settings.connection(connection_id)
        if audit_window:
            window_module.parse(audit_window, zone)
    except AuditorError as exc:
        _fail(str(exc))
        return

    runs = RunStore(workspace)
    record = runs.create(
        RunRequest(
            connection_ids=connection_ids,
            window=audit_window,
            timezone=zone,
            stop_after=stop_after,
        )
    )
    _out(f"Run {record.run_id}")

    seen = 0
    result = engine.execute_ingest(workspace=workspace, runs=runs, run_id=record.run_id)
    for event in runs.read_events(record.run_id, offset=seen):
        message = event.get("message", "")
        colour = typer.colors.RED if event["event"] == "error" else None
        typer.secho(f"  [{event['event']}] {message}", fg=colour)

    _out()
    _print_run(result, runs)
    if result.status in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise typer.Exit(code=1)
    if result.status is RunStatus.INCOMPLETE:
        raise typer.Exit(code=2)


# --- runs ---------------------------------------------------------------------


@runs_app.command("list")
def runs_list(workspace: WorkspaceOption = DEFAULT_WORKSPACE) -> None:
    """Every run in the store, newest first."""
    records = RunStore(workspace).list()
    if not records:
        _out("No runs yet.")
        return
    _out(f"{'run id':<32}{'status':<13}{'stage':<10}{'records':>9}  window")
    for record in records:
        _out(
            f"{record.run_id:<32}{record.status.value:<13}"
            f"{(record.stage.value if record.stage else '-'):<10}"
            f"{record.record_count:>9}  {record.request.window or 'all'}"
        )


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json", help="Print run.json verbatim.")] = False,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Show one run: status, coverage, slices, and the manifest summary."""
    runs = RunStore(workspace)
    try:
        record = runs.get(run_id)
    except AuditorError as exc:
        _fail(str(exc))
        return
    if as_json:
        _out(record.model_dump_json(indent=2))
        return
    _print_run(record, runs)


@runs_app.command("rm")
def runs_rm(
    run_id: Annotated[str, typer.Argument()],
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Delete a run directory."""
    runs = RunStore(workspace)
    if not runs.exists(run_id):
        _fail(f"no run {run_id!r}")
    runs.delete(run_id)
    _out(f"Deleted {run_id}")


@runs_app.command("records")
def runs_records(
    run_id: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("-n", "--limit")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Print records from a run's store, for hand-checking what ingest produced."""
    runs = RunStore(workspace)
    store = RecordStore(runs.artifact_path(run_id, RECORDS_PARQUET))
    if not store.exists():
        _fail(f"run {run_id} has no records.parquet (purged, or ingest did not complete)")
    records = store.head(limit)
    if as_json:
        _out(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
        return
    _out(f"{'start_time':<26}{'request_id':<22}{'status':<15}{'att':>4}{'in':>9}{'out':>9}")
    for record in records:
        _out(
            f"{record.start_time.isoformat():<26}{record.request_id[:21]:<22}"
            f"{record.status.value:<15}{record.attempt_index:>4}"
            f"{record.usage.input_tokens:>9}{record.usage.output_tokens:>9}"
        )


def _print_run(record: Any, runs: RunStore) -> None:
    _out(f"Run        : {record.run_id}")
    _out(f"Status     : {record.status.value}")
    _out(f"Stages     : {', '.join(s.value for s in record.stages_completed) or '-'}")
    _out(f"Connections: {', '.join(record.request.connection_ids)}")
    _out(f"Window     : {record.request.window or 'all available'} ({record.request.timezone})")
    _out(f"Observed   : {record.observed_start} .. {record.observed_end} (UTC)")
    _out(f"Records    : {record.record_count}")
    if record.error:
        typer.secho(f"Error      : {record.error}", fg=typer.colors.RED)

    coverage = record.coverage
    if coverage is None:
        return
    _out()
    _out("Coverage")
    _out(f"  objects listed / read : {coverage.listed_objects} / {coverage.read_objects}")
    _out(f"  bytes listed / read   : {coverage.listed_bytes} / {coverage.read_bytes}")
    _out(f"  missing               : {coverage.missing_bytes} bytes ({coverage.missing_pct:.2f}%)")
    _out(f"  records parsed        : {coverage.records_parsed}")
    _out(f"  records rejected      : {coverage.records_rejected}")
    _out(f"  outside window        : {coverage.records_outside_window}")
    _out(f"  pruned by window      : {coverage.pruned_by_window} objects")
    _out(f"  duplicates collapsed  : {coverage.duplicates_collapsed} ({coverage.dedupe_method})")
    _out(f"  retry attempts linked : {coverage.retry_attempts_linked}")
    _out(f"  unknown cache TTL     : {coverage.ttl_class_unknown_records} records")
    _out(f"  baseline lower bound  : {coverage.baseline_is_lower_bound}")
    _out(f"  projection withheld   : {coverage.projection_withheld}")

    for reason in coverage.gating_reasons:
        typer.secho(f"  ! {reason}", fg=typer.colors.RED)
    for warning in coverage.warnings:
        typer.secho(f"  ~ {warning}", fg=typer.colors.YELLOW)

    failures = [entry for entry in runs.read_manifest(record.run_id) if entry.status == "failed"]
    for entry in failures:
        typer.secho(
            f"  ! unread {entry.uri} ({entry.size_bytes} bytes): {entry.error}", fg=typer.colors.RED
        )

    if record.slices:
        _out()
        _out("Slices")
        for item in record.slices:
            _out(
                f"  {item.connection_id} / {item.source} / tier {item.fidelity.value} "
                f"— {item.records} records, mix {item.fidelity_counts}"
            )


# --- serve --------------------------------------------------------------------


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8787,
    workspace: WorkspaceOption = DEFAULT_WORKSPACE,
) -> None:
    """Start the local web app (SPEC.md §13.1). Single tenant, no accounts."""
    try:
        import uvicorn
    except ImportError:
        _fail("the web app needs the web extra: install `llm-cost-auditor[web]`")
        return

    from .web.app import create_app

    if host != "127.0.0.1":
        typer.secho(
            f"warning: binding {host} exposes this server beyond loopback. There is no "
            f"authentication in v1 (SPEC.md §12) and the process can read everything in the "
            f"source scope using the host's ambient identity. Put it behind an SSO proxy, a "
            f"VPN, or an SSH tunnel.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    application = create_app(workspace)
    _out(f"llm-cost-auditor → http://{host}:{port}  (workspace: {workspace})")
    uvicorn.run(application, host=host, port=port, log_level="info")


def main() -> None:
    try:
        app()
    except AuditorError as exc:  # pragma: no cover - typer handles command-level errors
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
