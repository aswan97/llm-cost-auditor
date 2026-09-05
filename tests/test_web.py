"""The app and the API, driven the way a browser and a script drive them.

The rule these tests exist to hold: **the app never invents a number the run
record does not contain.** Both surfaces render the same run, so a figure
visible in one and not the other is a defect (SPEC.md §13.1, §15.7).
"""

from __future__ import annotations

import re
import time
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
    """A styled dashboard reads as fact; the honest version says what is missing (§15.6)."""
    for path in ("/", "/connections", "/runs/new"):
        body = " ".join(client.get(path).text.lower().split())
        assert "no pricing engine and there are no analyzers yet" in body
        assert "no page here shows a dollar figure or a finding" in body


def test_no_page_shows_a_dollar_figure(client: Any) -> None:
    """Nothing is priced yet, so a `$1,234.56` anywhere would be invented."""
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
