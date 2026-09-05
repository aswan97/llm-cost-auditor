"""The local web app and the JSON API (SPEC.md §13.1, §13.2).

**The app is a driver, not a layer.** Every operation it performs is a CLI
command first, and no analysis logic lives in a route handler, a template, or
JavaScript. Routes read the run store and hand run records to templates; the
work happens in the engine.

**What this build renders is what the engine produces** — runs, connections, the
source scope, live progress, the manifest, the coverage panel, and baseline
spend priced from the catalog. There are still no findings pages, because no
analyzer exists yet and a page that renders plausible fake numbers is the exact
failure this project is trying to avoid (AGENTS.md).

The cost panel is the first place two surfaces show the same money, so the
arithmetic lives in `baseline.compute()` and both this app and `runs cost`
merely format its result. Neither can drift from the other, because neither
does the sum.

Security posture, all of it deliberate (§12):

* binds loopback by default, and `serve` warns when told otherwise;
* **no authentication**, which is coherent because the server holds no secrets
  — it borrows the host's ambient identity and stores no credential;
* **CSRF-protected**: state-changing requests need a same-origin token;
* **path-confined**: connections are validated against the terminal-configured
  source scope on save *and* on use, and there is no write route for the scope
  itself;
* **no outbound calls**: every asset is served from the package, which is why
  the CSP can forbid external origins outright.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import baseline, engine
from .. import config as config_module
from .. import window as window_module
from ..cli import peek_connection
from ..config import Connection, WorkspaceConfig, check_in_scope
from ..errors import AuditorError, SourceScopeError
from ..ingest import local
from ..record_store import RecordStore
from ..run_store import (
    MANIFEST_JSON,
    RECORDS_PARQUET,
    RunRecord,
    RunRequest,
    RunStatus,
    RunStore,
    Stage,
)

HERE = Path(__file__).parent
CSRF_COOKIE = "lca_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"

# Every asset is served from the package, so the policy can name `self` and
# nothing else (§12 layer 0). `unsafe-inline` covers the small style block in
# the base template; there is no inline script.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def create_app(workspace: Path) -> FastAPI:
    """Build the application over one workspace."""
    workspace = workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    runs = RunStore(workspace)
    jobs = engine.RunQueue(workspace, runs)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    # One token per server process. There are no accounts and no sessions to
    # bind it to (§2); its job is to stop a foreign page from driving this
    # server through the browser, not to authenticate anyone.
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Reconciling the previous process's runs happens here rather than at
        # import, so a crashed server's `running` runs are marked `interrupted`
        # exactly once, when a new server takes over (§5.4).
        jobs.start()
        yield
        jobs.stop()

    application = FastAPI(
        title="llm-cost-auditor", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    application.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @application.middleware("http")
    async def _guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in SAFE_METHODS:
            problem = _csrf_problem(request, csrf_token)
            if problem:
                return JSONResponse({"error": problem}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # `same-origin` and not `no-referrer`, which is the stricter-looking
        # choice and breaks the app. Under `no-referrer` a browser sends
        # `Origin: null` on every non-CORS non-GET request — which is exactly a
        # form POST navigation — so the origin check below refused every
        # button that submits a form, while the HTMX buttons kept working
        # because XHR is CORS-mode and keeps its real origin. Nothing is
        # weakened: the referrer still never leaves this origin, and a foreign
        # page setting its own `no-referrer` still arrives as `null` and is
        # still refused.
        response.headers["Referrer-Policy"] = "same-origin"
        response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, samesite="strict", path="/")
        return response

    def settings() -> WorkspaceConfig:
        return config_module.load(workspace)

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf_token": csrf_token,
                "workspace": str(workspace),
                "sources": settings().sources,
                **context,
            },
        )

    # --- pages ---------------------------------------------------------------

    @application.get("/", response_class=HTMLResponse)
    def runs_page(request: Request) -> HTMLResponse:
        return page(request, "runs.html", runs=runs.list())

    @application.get("/runs/new", response_class=HTMLResponse)
    def new_run_page(request: Request) -> HTMLResponse:
        config = settings()
        return page(
            request,
            "new_run.html",
            connections=config.connections,
            timezone=config.timezone,
            in_scope={c.id: _in_scope(c, config) for c in config.connections},
        )

    @application.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str) -> HTMLResponse:
        record = _lookup(runs, run_id)
        return page(
            request,
            "run.html",
            run=record,
            manifest=runs.read_manifest(run_id),
            events=list(runs.read_events(run_id)),
        )

    def _baseline(run_id: str) -> baseline.Baseline | None:
        """Price a run's stored records, or `None` if there are none to price.

        Computed on read rather than stored: pricing is read-only over an
        existing ingest (§11.1), and a cached total is a total that can go stale
        against the catalog without anything saying so.
        """
        _lookup(runs, run_id)
        store = RecordStore(runs.artifact_path(run_id, RECORDS_PARQUET))
        if not store.exists():
            return None
        return baseline.compute(store.iter_records())

    @application.get("/runs/{run_id}/cost", response_class=HTMLResponse)
    def run_cost_fragment(request: Request, run_id: str) -> HTMLResponse:
        """The cost panel, loaded into the run page by HTMX.

        A fragment rather than part of the page body because pricing walks every
        record: the run page stays fast, and a large run shows its coverage and
        manifest while this is still counting.
        """
        return page(request, "_cost.html", run_id=run_id, baseline=_baseline(run_id))

    @application.get("/api/runs/{run_id}/cost")
    def api_run_cost(run_id: str) -> dict[str, Any]:
        """The same figures as `runs cost`, as JSON.

        Every monetary field is an integer of micro-USD named `_usd_micros`
        (§6.4) — the formatted dollar strings live only in the HTML, and nothing
        reads one back.
        """
        result = _baseline(run_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id} has no records.parquet (purged, or ingest did not complete)",
            )
        return result.model_dump(mode="json")

    @application.get("/runs/{run_id}/live", response_class=HTMLResponse)
    def run_live_fragment(request: Request, run_id: str) -> HTMLResponse:
        """The polled fragment behind the live run view.

        HTMX polls this while a run is active; the SSE endpoint under `/api`
        renders the same `log.jsonl` for programmatic callers. One producer,
        two renderers (§5.4).
        """
        record = _lookup(runs, run_id)
        return page(
            request,
            "_live.html",
            run=record,
            manifest=runs.read_manifest(run_id),
            events=list(runs.read_events(run_id)),
        )

    @application.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request) -> HTMLResponse:
        config = settings()
        return page(
            request,
            "connections.html",
            connections=config.connections,
            in_scope={c.id: _in_scope(c, config) for c in config.connections},
        )

    # --- HTMX fragments -------------------------------------------------------

    @application.post("/connections/add", response_class=HTMLResponse)
    def connections_add_form(
        request: Request,
        connection_id: Annotated[str, Form()],
        uri: Annotated[str, Form()],
        source: Annotated[str, Form()] = "anthropic",
        log_format: Annotated[str, Form()] = "auto",
        compression: Annotated[str, Form()] = "auto",
        partition: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            _save_connection(
                workspace,
                connection_id=connection_id,
                uri=uri,
                source=source,
                log_format=log_format,
                compression=compression,
                partition=partition or None,
            )
        except (AuditorError, ValueError) as exc:
            config = settings()
            return page(
                request,
                "connections.html",
                connections=config.connections,
                in_scope={c.id: _in_scope(c, config) for c in config.connections},
                error=str(exc),
            )
        return RedirectResponse("/connections", status_code=303)

    @application.post("/connections/{connection_id}/delete")
    def connections_delete_form(connection_id: str) -> Response:
        config = settings()
        config.connections = [c for c in config.connections if c.id != connection_id]
        config_module.save(workspace, config)
        return RedirectResponse("/connections", status_code=303)

    @application.post("/connections/{connection_id}/test", response_class=HTMLResponse)
    def connections_test_fragment(request: Request, connection_id: str) -> HTMLResponse:
        return page(request, "_connection_test.html", result=_test(settings(), connection_id))

    @application.post("/connections/{connection_id}/peek", response_class=HTMLResponse)
    def connections_peek_fragment(request: Request, connection_id: str) -> HTMLResponse:
        config = settings()
        try:
            connection = config.connection(connection_id)
            check_in_scope(connection.uri, config.sources)
            preview = peek_connection(connection, config, 20)
        except AuditorError as exc:
            return page(request, "_connection_peek.html", error=str(exc))
        return page(request, "_connection_peek.html", preview=preview, connection=connection)

    @application.post("/runs/preflight", response_class=HTMLResponse)
    def preflight_fragment(
        request: Request,
        connection_ids: Annotated[list[str] | None, Form()] = None,
        audit_window: Annotated[str, Form()] = "",
        timezone: Annotated[str, Form()] = "UTC",
    ) -> HTMLResponse:
        try:
            estimate = _preflight(settings(), connection_ids or [], audit_window, timezone)
        except AuditorError as exc:
            return page(request, "_preflight.html", error=str(exc))
        return page(request, "_preflight.html", estimate=estimate)

    @application.post("/runs/start")
    def start_run_form(
        connection_ids: Annotated[list[str] | None, Form()] = None,
        audit_window: Annotated[str, Form()] = "",
        timezone: Annotated[str, Form()] = "UTC",
    ) -> Response:
        try:
            record = _create_run(runs, settings(), connection_ids or [], audit_window, timezone)
        except AuditorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        jobs.submit(record.run_id)
        return RedirectResponse(f"/runs/{record.run_id}", status_code=303)

    @application.post("/runs/{run_id}/cancel")
    def cancel_run_form(run_id: str) -> Response:
        _lookup(runs, run_id)
        jobs.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    # --- JSON API (§13.2) -----------------------------------------------------

    @application.get("/api/runs")
    def api_runs() -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in runs.list()]

    @application.post("/api/runs", status_code=201)
    def api_create_run(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        try:
            record = _create_run(
                runs,
                settings(),
                list(payload.get("connection_ids", [])),
                str(payload.get("window") or ""),
                str(payload.get("timezone") or settings().timezone),
            )
        except AuditorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        jobs.submit(record.run_id)
        return record.model_dump(mode="json")

    @application.get("/api/runs/{run_id}")
    def api_run(run_id: str) -> dict[str, Any]:
        return _lookup(runs, run_id).model_dump(mode="json")

    @application.get("/api/runs/{run_id}/manifest")
    def api_manifest(run_id: str) -> list[dict[str, Any]]:
        _lookup(runs, run_id)
        return [entry.model_dump(mode="json") for entry in runs.read_manifest(run_id)]

    @application.post("/api/runs/{run_id}/cancel")
    def api_cancel(run_id: str) -> dict[str, Any]:
        _lookup(runs, run_id)
        return jobs.cancel(run_id).model_dump(mode="json")

    @application.get("/api/runs/{run_id}/events")
    async def api_events(run_id: str) -> StreamingResponse:
        """Progress events as SSE, tailing `log.jsonl` while the run is active."""
        _lookup(runs, run_id)

        async def stream() -> AsyncIterator[bytes]:
            offset = 0
            while True:
                events = list(runs.read_events(run_id, offset=offset))
                offset += len(events)
                for event in events:
                    yield f"data: {json.dumps(event)}\n\n".encode()
                if runs.get(run_id).status.terminal and not events:
                    yield b"event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @application.get("/api/connections")
    def api_connections() -> list[dict[str, Any]]:
        config = settings()
        return [
            {**c.model_dump(mode="json"), "in_scope": _in_scope(c, config)}
            for c in config.connections
        ]

    @application.post("/api/connections", status_code=201)
    def api_create_connection(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        return _api_save_connection(workspace, payload)

    @application.put("/api/connections/{connection_id}")
    def api_update_connection(
        connection_id: str, payload: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        return _api_save_connection(workspace, {**payload, "id": connection_id})

    @application.delete("/api/connections/{connection_id}")
    def api_delete_connection(connection_id: str) -> dict[str, Any]:
        config = settings()
        remaining = [c for c in config.connections if c.id != connection_id]
        if len(remaining) == len(config.connections):
            raise HTTPException(status_code=404, detail=f"no connection {connection_id!r}")
        config.connections = remaining
        config_module.save(workspace, config)
        referencing = [
            record.run_id
            for record in runs.list()
            if connection_id in record.request.connection_ids
        ]
        return {"deleted": connection_id, "referenced_by_runs": referencing}

    @application.post("/api/connections/{connection_id}/test")
    def api_test_connection(connection_id: str) -> dict[str, Any]:
        return _test(settings(), connection_id)

    @application.post("/api/connections/{connection_id}/peek")
    def api_peek_connection(connection_id: str, limit: int = 20) -> dict[str, Any]:
        config = settings()
        try:
            connection = config.connection(connection_id)
            check_in_scope(connection.uri, config.sources)
            preview = peek_connection(connection, config, limit)
        except SourceScopeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except AuditorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            **{k: v for k, v in preview.items() if k != "records"},
            "records": [r.model_dump(mode="json") for r in preview["records"]],
        }

    @application.get("/api/sources")
    def api_sources() -> dict[str, Any]:
        """The source scope — read-only, and there is deliberately no write route.

        The browser chooses *where within* the permitted area to look; the
        terminal chooses the permitted area (§6.1, §13.2).
        """
        return {
            "roots": [config_module.normalize_root(r) for r in settings().sources],
            "editable_from": "terminal only — `llm-cost-auditor sources add <root>`",
        }

    return application


# --- helpers used by both surfaces -------------------------------------------


def _csrf_problem(request: Request, expected: str) -> str | None:
    """Reject a state-changing request that a foreign page could have made.

    Double-submit token plus an origin check: the token proves the caller could
    read a cookie set by this origin, and the origin check catches the case
    where it could not read one at all.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = {f"http://{request.url.netloc}", f"https://{request.url.netloc}"}
        if origin not in allowed:
            return f"cross-origin request from {origin} refused"

    supplied = request.headers.get(CSRF_HEADER) or request.cookies.get(CSRF_COOKIE)
    if supplied and secrets.compare_digest(supplied, expected):
        return None
    return "missing or invalid CSRF token"


def _lookup(runs: RunStore, run_id: str) -> RunRecord:
    try:
        return runs.get(run_id)
    except AuditorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _in_scope(connection: Connection, config: WorkspaceConfig) -> bool:
    try:
        check_in_scope(connection.uri, config.sources)
    except AuditorError:
        return False
    return True


def _save_connection(
    workspace: Path,
    *,
    connection_id: str,
    uri: str,
    source: str,
    log_format: str,
    compression: str,
    partition: str | None,
) -> Connection:
    """Create or replace a connection, scope-checked on every save (§6.1)."""
    config = config_module.load(workspace)
    check_in_scope(uri, config.sources)
    connection = Connection(
        id=connection_id,
        uri=uri,
        source=source,  # type: ignore[arg-type]
        format=log_format,
        compression=compression,
        partition=partition,
    )
    config.connections = [c for c in config.connections if c.id != connection_id] + [connection]
    config_module.save(workspace, config)
    return connection


def _api_save_connection(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        connection = _save_connection(
            workspace,
            connection_id=str(payload.get("id", "")),
            uri=str(payload.get("uri", "")),
            source=str(payload.get("source", "anthropic")),
            log_format=str(payload.get("format", "auto")),
            compression=str(payload.get("compression", "auto")),
            partition=payload.get("partition") or None,
        )
    except SourceScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (AuditorError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return connection.model_dump(mode="json")


def _test(config: WorkspaceConfig, connection_id: str) -> dict[str, Any]:
    """Resolve identity, list the prefix, and name the permissions exercised (§13.1)."""
    try:
        connection = config.connection(connection_id)
        check_in_scope(connection.uri, config.sources)
    except SourceScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AuditorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    listing = local.list_objects(connection.uri, config.sources)
    return {
        "connection_id": connection.id,
        "connector": connection.connector,
        "source": connection.source,
        "uri": connection.uri,
        # The `file` connector resolves no credential: the OS decides what this
        # process can read, bounded by the source scope (§6.7).
        "identity": "none — the OS decides what this process can read (§6.7)",
        "permissions_exercised": ["directory listing (stat)", "file open (read)"],
        "objects": len(listing.refs),
        "bytes": listing.listed_bytes,
        "failures": [{"uri": f.uri, "error": f.error} for f in listing.failures],
        "empty": not listing.refs and not listing.failures,
    }


def _preflight(
    config: WorkspaceConfig, connection_ids: list[str], audit_window: str, timezone: str
) -> dict[str, Any]:
    """Object count and byte estimate for the window, before anything starts.

    A run nobody meant to start should be visible before it starts (§13.1), and
    the bulk-ingest trade means the byte volume is a real cost the user is about
    to pay (§15.11).
    """
    if not connection_ids:
        raise AuditorError("select at least one connection")
    window = window_module.parse(audit_window, timezone) if audit_window else None

    per_connection: list[dict[str, Any]] = []
    total_objects = 0
    total_bytes = 0
    for connection_id in connection_ids:
        connection = config.connection(connection_id)
        check_in_scope(connection.uri, config.sources)
        listing = local.list_objects(
            connection.uri,
            config.sources,
            since=window.start if window else None,
            until=window.end if window else None,
            partition=connection.partition,
        )
        total_objects += len(listing.refs)
        total_bytes += listing.listed_bytes
        per_connection.append(
            {
                "connection_id": connection_id,
                "objects": len(listing.refs),
                "bytes": listing.listed_bytes,
                "pruned_by_window": listing.pruned_by_window,
                "failures": [{"uri": f.uri, "error": f.error} for f in listing.failures],
            }
        )

    return {
        "window": window.describe() if window else "all available data",
        "timezone": timezone,
        "objects": total_objects,
        "bytes": total_bytes,
        "connections": per_connection,
    }


def _create_run(
    runs: RunStore,
    config: WorkspaceConfig,
    connection_ids: list[str],
    audit_window: str,
    timezone: str,
) -> RunRecord:
    """Validate a run request and create the run directory. The queue executes it."""
    if not connection_ids:
        raise AuditorError("a run must name at least one connection")
    for connection_id in connection_ids:
        connection = config.connection(connection_id)
        # Re-checked on use, not only on save (§6.1).
        check_in_scope(connection.uri, config.sources)
    if audit_window:
        window_module.parse(audit_window, timezone)

    return runs.create(
        RunRequest(
            connection_ids=connection_ids,
            window=audit_window or None,
            timezone=timezone,
            # The app always submits all three stages (§5.3); in this build
            # only ingest exists, so that is where it stops — stated rather
            # than implied by an empty findings page.
            stop_after=Stage.INGEST,
        )
    )


__all__ = ["MANIFEST_JSON", "RunStatus", "create_app"]
