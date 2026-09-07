"""The app and the API, driven the way a browser and a script drive them.

The rule these tests exist to hold: **the app never invents a number the run
record does not contain.** Both surfaces render the same run, so a figure
visible in one and not the other is a defect (SPEC.md §13.1, §15.7).
"""

from __future__ import annotations

import re
import time
from html import escape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_cost_auditor.web.app import CSRF_HEADER, create_app

pytestmark = pytest.mark.usefixtures("workspace")


@pytest.fixture
def client(workspace: Path) -> Any:
    with TestClient(create_app(workspace)) as test_client:
        yield test_client


def token(client: Any) -> dict[str, str]:
    client.get("/")
    return {CSRF_HEADER: client.cookies["lca_csrf"]}


def wait_for_terminal(client: Any, run_id: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/api/runs/{run_id}").json()
        if record["status"] not in ("queued", "running"):
            return record
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


# --- pages --------------------------------------------------------------------


def test_pages_render(client: Any) -> None:
    for path in ("/", "/connections", "/runs/new"):
        response = client.get(path)
        assert response.status_code == 200
        assert "llm-cost-auditor" in response.text


def test_every_page_states_what_this_build_does_not_do(client: Any) -> None:
    """A styled dashboard reads as fact; the honest version says what is missing (§15.6).

    The banner names the analyzers that exist and the ones that do not. It has
    to move when they do — a build note that has drifted out of date is worse
    than none, because it is read as current.
    """
    for path in ("/", "/connections", "/runs/new"):
        body = " ".join(client.get(path).text.lower().split())
        assert "waste findings" in body
        assert "no cache, batching, or routing analyzers yet" in body
        assert "no run shows a monthly projection" in body


def test_no_dollar_figure_appears_where_nothing_was_priced(client: Any) -> None:
    """Spend belongs on a run that has records, and nowhere else.

    A dollar amount on the connections page or the new-run form would not be
    read from anything — it would be decoration that looks like a measurement.
    """
    for path in ("/", "/connections", "/runs/new"):
        body = client.get(path).text
        assert not re.search(r"\$\s?\d", body), f"{path} renders a dollar amount"


def test_connections_page_shows_the_scope_it_is_bound_by(client: Any, workspace: Path) -> None:
    body = client.get("/connections").text
    assert "Source scope" in body
    assert "editable only from a terminal" in body


# --- the source scope is not writable through the API (§6.1, §13.2) -----------


def test_sources_endpoint_is_read_only(client: Any) -> None:
    response = client.get("/api/sources")
    assert response.status_code == 200
    assert response.json()["editable_from"].startswith("terminal only")

    for method in ("POST", "PUT", "DELETE", "PATCH"):
        attempt = client.request(
            method, "/api/sources", headers=token(client), json={"roots": ["/"]}
        )
        assert attempt.status_code in (404, 405), f"{method} /api/sources must not exist"


def test_a_connection_outside_the_scope_is_refused(client: Any, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    response = client.post(
        "/api/connections",
        headers=token(client),
        json={"id": "escape", "uri": str(outside), "source": "anthropic"},
    )
    assert response.status_code == 403
    assert "source scope" in response.json()["detail"]


def test_scope_check_is_re_applied_on_every_save(client: Any, logs: Path) -> None:
    """Saving over an existing id re-checks; it does not inherit the old verdict."""
    ok = client.post(
        "/api/connections",
        headers=token(client),
        json={"id": "local-anthropic", "uri": str(logs), "source": "anthropic"},
    )
    assert ok.status_code == 201

    escaped = client.put(
        "/api/connections/local-anthropic",
        headers=token(client),
        json={"uri": "/etc", "source": "anthropic"},
    )
    assert escaped.status_code == 403


# --- CSRF (§12) ---------------------------------------------------------------


def test_state_changing_requests_need_the_token(workspace: Path) -> None:
    with TestClient(create_app(workspace)) as bare:
        bare.cookies.clear()
        response = bare.post("/api/connections", json={"id": "x", "uri": "/tmp"})
        assert response.status_code == 403
        assert "CSRF" in response.json()["error"]


def test_cross_origin_requests_are_refused(client: Any) -> None:
    response = client.post(
        "/api/connections",
        headers={**token(client), "origin": "https://evil.example"},
        json={"id": "x", "uri": "/tmp"},
    )
    assert response.status_code == 403
    assert "cross-origin" in response.json()["error"]


def test_an_opaque_origin_is_refused(client: Any) -> None:
    """`null` is what a sandboxed frame or a `no-referrer` foreign page sends.

    No legitimate caller sends it: this app's own pages carry a real origin, and
    a client that sends no `Origin` at all is checked on the token instead.
    """
    response = client.post(
        "/api/connections",
        headers={**token(client), "origin": "null"},
        json={"id": "x", "uri": "/tmp"},
    )
    assert response.status_code == 403
    assert "cross-origin" in response.json()["error"]


def test_the_referrer_policy_does_not_strip_our_own_origin(client: Any) -> None:
    """Load-bearing, not cosmetic — `no-referrer` here breaks every form button.

    Per the Fetch standard, a browser serializes the `Origin` header as `null`
    on a non-CORS non-GET request whose referrer policy is `no-referrer`. A form
    POST navigation is exactly that, so the whole app's `Start run`, `Cancel`,
    `Add connection` and `Delete` submissions arrived opaque and were refused by
    the origin check above, while the HTMX buttons worked because XHR is
    CORS-mode and keeps its origin. TestClient sends whatever headers it is
    given and so cannot reproduce a browser here; the response header is the
    knob that caused it, so that is what this pins.
    """
    policy = client.get("/").headers["referrer-policy"]
    assert policy == "same-origin"
    assert policy != "no-referrer"


def test_reads_are_not_gated(client: Any) -> None:
    client.cookies.clear()
    assert client.get("/api/runs").status_code == 200


def test_csp_forbids_external_origins(client: Any) -> None:
    """The app makes no outbound calls, so the policy can say `self` and nothing else."""
    policy = client.get("/").headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "http://" not in policy and "https://" not in policy


def test_no_page_references_an_external_asset(client: Any) -> None:
    for path in ("/", "/connections", "/runs/new"):
        body = client.get(path).text
        assert "//cdn" not in body
        assert "https://" not in body.replace("https://evil.example", "")


# --- a run, end to end, through the app ---------------------------------------


def test_run_started_in_the_app_completes_and_matches_the_record(
    client: Any, expected: dict[str, Any]
) -> None:
    created = client.post(
        "/api/runs",
        headers=token(client),
        json={
            "connection_ids": ["local-anthropic"],
            "window": "2026-08-01..2026-08-31",
            "timezone": "UTC",
        },
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]

    record = wait_for_terminal(client, run_id)
    assert record["status"] == "complete"
    assert record["record_count"] == expected["records"]
    assert record["coverage"]["duplicates_collapsed"] == expected["duplicates_collapsed"]
    assert record["coverage"]["records_outside_window"] == expected["records_outside_window"]

    # The page must show the same figures as the API — one run record, two
    # renderers, and any discrepancy is a defect (§13.1).
    page = client.get(f"/runs/{run_id}").text
    assert str(expected["records"]) in page
    assert run_id in page
    assert "tier A" in page

    listing = client.get("/").text
    assert run_id in listing


def test_manifest_is_reachable_from_both_surfaces(client: Any) -> None:
    created = client.post(
        "/api/runs",
        headers=token(client),
        json={"connection_ids": ["local-anthropic"], "timezone": "UTC"},
    )
    run_id = created.json()["run_id"]
    wait_for_terminal(client, run_id)

    manifest = client.get(f"/api/runs/{run_id}/manifest").json()
    assert len(manifest) == 1
    assert manifest[0]["container"] == "jsonl"
    assert manifest[0]["status"] == "ok"

    page = client.get(f"/runs/{run_id}").text
    assert "Manifest" in page
    assert "traffic.jsonl" in page


def test_preflight_estimates_before_anything_starts(client: Any) -> None:
    response = client.post(
        "/runs/preflight",
        headers=token(client),
        data={
            "connection_ids": ["local-anthropic"],
            "audit_window": "2026-08-01..2026-08-31",
            "timezone": "UTC",
        },
    )
    assert response.status_code == 200
    assert "Pre-flight estimate" in response.text
    assert "Objects to read" in response.text

    # No run was created by estimating.
    assert client.get("/api/runs").json() == []


def test_connection_test_names_the_permissions_it_exercised(client: Any) -> None:
    response = client.post("/api/connections/local-anthropic/test", headers=token(client))
    assert response.status_code == 200
    body = response.json()
    assert body["objects"] == 1
    assert body["permissions_exercised"]
    # v1 stores no credentials, and the file connector resolves none at all.
    assert "none" in body["identity"]


def test_peek_decodes_without_starting_a_run(client: Any) -> None:
    response = client.post("/api/connections/local-anthropic/peek?limit=5", headers=token(client))
    assert response.status_code == 200
    body = response.json()
    assert len(body["records"]) == 5
    assert body["container"] == "jsonl"
    assert body["fidelity"] in ("A", "B", "C")
    assert client.get("/api/runs").json() == []


def test_peek_response_carries_no_prompt_text(client: Any) -> None:
    response = client.post("/api/connections/local-anthropic/peek?limit=20", headers=token(client))
    blob = response.text
    for needle in ("Adjudicate claim 88213", "analyst@acme.example", "ops@acme.example"):
        assert needle not in blob


def test_unknown_run_is_a_404(client: Any) -> None:
    assert client.get("/api/runs/run_does_not_exist").status_code == 404
    assert client.get("/runs/run_does_not_exist").status_code == 404


def test_run_id_traversal_is_refused(client: Any) -> None:
    response = client.get("/api/runs/..%2F..%2Fetc")
    assert response.status_code in (404, 400)


# --- baseline spend: two surfaces, one arithmetic (§11.1, §13.1) --------------


def completed_run(client: Any) -> str:
    """Start a run through the app and wait for it, the way a user would."""
    response = client.post(
        "/runs/start",
        headers=token(client),
        data={"connection_ids": ["local-anthropic"], "csrf_token": client.cookies["lca_csrf"]},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    wait_for_terminal(client, run_id)
    return run_id


def test_the_run_page_shows_what_the_traffic_cost(client: Any) -> None:
    run_id = completed_run(client)
    body = client.get(f"/runs/{run_id}/cost").text
    assert "Baseline spend" in body
    assert re.search(r"\$\s?\d", body), "a priced run should show a dollar figure"
    assert "No commercial overlay is applied" in body


def test_the_app_and_the_cli_agree_figure_by_figure(client: Any, workspace: Path) -> None:
    """The check AGENTS.md asks for: two surfaces over one run record.

    Both render `baseline.compute()`, so this asserts they are wired to the same
    result rather than that two implementations happen to agree today. A
    discrepancy here is a defect by definition, not a rounding difference.
    """
    run_id = completed_run(client)

    from llm_cost_auditor import baseline
    from llm_cost_auditor.record_store import RecordStore
    from llm_cost_auditor.run_store import RECORDS_PARQUET, RunStore

    store = RecordStore(RunStore(workspace).artifact_path(run_id, RECORDS_PARQUET))
    direct = baseline.compute(store.iter_records())

    api = client.get(f"/api/runs/{run_id}/cost").json()
    assert api["total_usd_micros"] == direct.total_usd_micros
    assert api["priced_records"] == direct.priced_records
    assert [(m["model"], m["spend_usd_micros"]) for m in api["by_model"]] == [
        (m.model, m.spend_usd_micros) for m in direct.by_model
    ]

    html = client.get(f"/runs/{run_id}/cost").text
    # Escaped, because a sub-cent figure renders as `<$0.01` and Jinja escapes
    # the `<`. The browser shows the same string the CLI prints.
    assert escape(direct.total) in html, "the rendered total must be the computed one"
    for row in direct.by_model:
        assert escape(row.spend) in html
        assert row.label in html


def test_a_run_started_in_the_app_reaches_the_audit_stage(client: Any) -> None:
    """The app submits all three stages (§5.3), and the page shows each one's output."""
    run_id = completed_run(client)
    record = client.get(f"/api/runs/{run_id}").json()
    assert record["stages_completed"] == ["ingest", "profile", "audit"]
    assert record["finding_count"] > 0

    page = client.get(f"/runs/{run_id}").text
    assert "Workloads" in page
    assert "unmapped" in page
    # What the grouping cannot see is on the page, not left to be assumed from
    # a table that looks complete (§8.2).
    assert "template fingerprinting" in page


def test_the_findings_panel_and_findings_json_agree_figure_by_figure(
    client: Any, workspace: Path
) -> None:
    """The check AGENTS.md asks for: two surfaces over one run record.

    Both render `findings.json`, so a discrepancy here is a defect by
    definition. The panel formats integers the engine wrote; it does not
    recompute anything, which is what makes that true rather than lucky.
    """
    from llm_cost_auditor.baseline import format_usd
    from llm_cost_auditor.run_store import RunStore

    run_id = completed_run(client)
    stored = RunStore(workspace).read_findings(run_id)
    assert stored is not None and stored.findings

    api = client.get(f"/api/runs/{run_id}/findings").json()
    assert api["run_id"] == run_id
    assert [f["id"] for f in api["findings"]] == [f.id for f in stored.findings]

    html = client.get(f"/runs/{run_id}/findings").text
    assert escape(format_usd(stored.portfolio_usd_micros)) in html
    for finding in stored.findings:
        assert finding.id in html
        assert escape(format_usd(finding.savings.gross_marginal.expected_usd_micros)) in html
        assert finding.confidence.value in html

    # The portfolio is the sum of marginals and nothing else (§11.2).
    assert stored.portfolio_usd_micros == sum(
        f["savings"]["gross_marginal"]["expected_usd_micros"] for f in api["findings"]
    )


def test_a_marginal_shrunk_by_another_finding_names_it_on_the_page(client: Any) -> None:
    """`$0.00` next to a real standalone must not read as "worthless" (§11.2)."""
    run_id = completed_run(client)
    html = client.get(f"/runs/{run_id}/findings").text
    assert "is already credited to" in html
    assert "waste.retry_storm" in html


def test_findings_before_an_audit_are_a_409_not_an_empty_set(client: Any) -> None:
    """ "Not yet" is a retry; "never" is not, and a caller must be able to tell."""
    response = client.post(
        "/runs/start",
        headers=token(client),
        data={"connection_ids": ["local-anthropic"], "csrf_token": client.cookies["lca_csrf"]},
        follow_redirects=False,
    )
    run_id = response.headers["location"].rsplit("/", 1)[-1]

    early = client.get(f"/api/runs/{run_id}/findings")
    assert early.status_code in (200, 409)
    if early.status_code == 409:
        assert "audit stage has not run" in early.json()["detail"]

    wait_for_terminal(client, run_id)
    assert client.get(f"/api/runs/{run_id}/findings").status_code == 200


def test_the_panel_never_reads_as_a_clean_bill_of_health_when_nothing_was_analyzed(
    client: Any, workspace: Path
) -> None:
    """A finding set with no analyzed records must not render as "no waste found".

    The empty list is identical either way, so this is a template branch that
    would be invisible in review — and the wrong branch is the tool asserting
    the traffic was fine when it never looked at it.
    """
    from llm_cost_auditor.findings import new_finding_set
    from llm_cost_auditor.run_store import RunStore

    run_id = completed_run(client)
    runs = RunStore(workspace)

    empty = new_finding_set(run_id, catalog_version="test-1")
    empty.analyzed_records = 0
    empty.withheld_reasons = ["12 record(s) on acme/ghost-model have no catalog rate."]
    runs.write_findings(run_id, empty)

    html = client.get(f"/runs/{run_id}/findings").text
    assert "Nothing was analyzed" in html
    assert "billed for work that was used" not in html
    assert "ghost-model" in html


def test_there_is_no_write_route_for_findings(client: Any) -> None:
    """Findings are engine output. Nothing on this surface may edit one."""
    run_id = completed_run(client)
    for method in (client.post, client.put, client.delete, client.patch):
        response = method(f"/api/runs/{run_id}/findings", headers=token(client))
        assert response.status_code == 405


def test_money_crosses_the_api_as_integer_micros_never_a_float(client: Any) -> None:
    """A JSON float would defeat the exact-equality rule on any round trip (§6.4)."""
    run_id = completed_run(client)
    api = client.get(f"/api/runs/{run_id}/cost").json()
    monetary = [k for k in api if k.endswith("_usd_micros")]
    assert monetary, "monetary fields must carry the _usd_micros suffix"
    for key in monetary:
        assert isinstance(api[key], int), f"{key} is {type(api[key])}"
    for row in api["by_model"]:
        assert isinstance(row["spend_usd_micros"], int)
    assert not any(k.endswith("_usd") for k in api), "no bare dollar field on the wire"


def test_a_run_with_no_records_says_so_rather_than_showing_zero(
    client: Any, workspace: Path
) -> None:
    """`$0.00` and "nothing to price" look identical in a table and are not.

    A purged run must read as absent data, never as a run that cost nothing.
    """
    from llm_cost_auditor.run_store import RECORDS_PARQUET, RunStore

    run_id = completed_run(client)
    RunStore(workspace).artifact_path(run_id, RECORDS_PARQUET).unlink()

    body = client.get(f"/runs/{run_id}/cost").text
    assert "Nothing to price" in body
    assert not re.search(r"\$\s?0\.00", body), "absence must not render as zero"
    assert client.get(f"/api/runs/{run_id}/cost").status_code == 404


def test_a_finished_run_fetches_the_panel_rather_than_waiting_on_it(client: Any) -> None:
    """Pricing walks every record, so the manifest and coverage must not wait."""
    run_id = completed_run(client)
    body = client.get(f"/runs/{run_id}").text
    assert f'hx-get="/runs/{run_id}/cost"' in body
    assert "Pricing records" in body, "the holding state names what is happening"
    assert "Baseline spend" not in body, "the panel arrives as a fragment, not inline"


def test_an_unfinished_run_shows_one_holding_state_not_two(client: Any, workspace: Path) -> None:
    """No flicker: the page renders the waiting panel the fragment would render.

    An unfinished run is waiting on ingest, not on pricing. Showing
    "Pricing records…" first and replacing it a moment later with "Waiting for
    ingest" is two messages for one situation, and the first of them is wrong.
    """
    from llm_cost_auditor.run_store import RunRequest, RunStore

    record = RunStore(workspace).create(RunRequest(connection_ids=["local-anthropic"]))
    body = client.get(f"/runs/{record.run_id}").text
    assert "Waiting for" in body, "the real state is rendered server-side"
    assert "Pricing records" not in body, "and not behind a placeholder that contradicts it"
    assert 'hx-trigger="every 2s"' in body, "it still refreshes itself into the table"


# --- the panel has to catch up with a run that was not finished yet ------------


def test_a_panel_fetched_before_the_run_finishes_keeps_refreshing(
    client: Any, workspace: Path
) -> None:
    """The reported bug: start a run, land on its page, never see a total.

    Starting a run redirects to the run page immediately, so the first render of
    this fragment happens while the run is still queued. If that render carried
    no refresh, it would be the only one, and spend would appear only to someone
    who navigated back to the run later.
    """
    from llm_cost_auditor.run_store import RunRecord, RunRequest, RunStatus, RunStore

    runs = RunStore(workspace)
    record: RunRecord = runs.create(RunRequest(connection_ids=["local-anthropic"]))
    assert record.status is RunStatus.QUEUED

    body = client.get(f"/runs/{record.run_id}/cost").text
    assert f'hx-get="/runs/{record.run_id}/cost"' in body, "an unfinished run must re-poll"
    assert 'hx-trigger="every 2s"' in body


def test_an_unfinished_run_is_not_told_its_records_were_purged(
    client: Any, workspace: Path
) -> None:
    """ "Nothing to price" describes a run that is working perfectly as a failure."""
    from llm_cost_auditor.run_store import RunRequest, RunStore

    record = RunStore(workspace).create(RunRequest(connection_ids=["local-anthropic"]))
    body = client.get(f"/runs/{record.run_id}/cost").text
    assert "purged" not in body
    assert "Waiting for" in body
    assert "queued" in body


def test_a_finished_panel_stops_polling(client: Any) -> None:
    """A terminal run has a final number; re-fetching it forever is waste."""
    run_id = completed_run(client)
    body = client.get(f"/runs/{run_id}/cost").text
    assert 'hx-trigger="every 2s"' not in body
    assert "Baseline spend" in body
    assert re.search(r"\$\s?\d", body)


def test_the_api_separates_not_yet_from_not_there(client: Any, workspace: Path) -> None:
    """409 is retryable and 404 is not, and a caller needs to tell them apart."""
    from llm_cost_auditor.run_store import RECORDS_PARQUET, RunRequest, RunStore

    runs = RunStore(workspace)
    pending = runs.create(RunRequest(connection_ids=["local-anthropic"]))
    in_progress = client.get(f"/api/runs/{pending.run_id}/cost")
    assert in_progress.status_code == 409
    assert "until it finishes" in in_progress.json()["detail"]

    run_id = completed_run(client)
    runs.artifact_path(run_id, RECORDS_PARQUET).unlink()
    assert client.get(f"/api/runs/{run_id}/cost").status_code == 404
