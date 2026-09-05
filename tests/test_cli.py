"""The CLI, driven as a user drives it (SPEC.md §13.3).

Every operation the app performs is a CLI command first, so this is the surface
the app's behaviour is checked against. Nothing is monkeypatched: these
invocations read real files, write a real run store, and print what a user sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from llm_cost_auditor import config as config_module
from llm_cost_auditor.cli import app

runner = CliRunner()


def run(*args: str, workspace: Path) -> Any:
    return runner.invoke(app, [*args, "--workspace", str(workspace)])


# --- the source scope ---------------------------------------------------------


def test_sources_round_trip(tmp_path: Path, logs: Path) -> None:
    workspace = tmp_path / "fresh"

    empty = run("sources", "list", workspace=workspace)
    assert empty.exit_code == 0
    assert "Source scope is empty" in empty.stdout

    added = run("sources", "add", str(logs), workspace=workspace)
    assert added.exit_code == 0

    listed = run("sources", "list", workspace=workspace)
    assert str(logs.resolve()) in listed.stdout

    again = run("sources", "add", str(logs), workspace=workspace)
    assert "already in scope" in again.stdout

    removed = run("sources", "rm", str(logs), workspace=workspace)
    assert removed.exit_code == 0
    assert "Removed" in removed.stdout

    missing = run("sources", "rm", str(logs), workspace=workspace)
    assert missing.exit_code == 1


def test_removing_a_root_names_the_connections_it_orphans(workspace: Path, logs: Path) -> None:
    result = run("sources", "rm", str(logs), workspace=workspace)
    assert result.exit_code == 0
    assert "now outside the scope" in result.stdout
    assert "local-anthropic" in result.stdout


def test_widening_to_the_filesystem_root_warns(tmp_path: Path) -> None:
    """`sources add /` restores the primitive the scope exists to prevent (§15.12)."""
    result = run("sources", "add", "/", workspace=tmp_path / "fresh")
    assert result.exit_code == 0
    assert "exfiltration primitive" in result.stderr


# --- connections ---------------------------------------------------------------


def test_connection_outside_the_scope_is_refused(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = run("connections", "add", "escape", "--uri", str(outside), workspace=workspace)
    assert result.exit_code == 1
    assert "outside the configured source scope" in result.stderr


def test_connections_list_marks_scope(workspace: Path) -> None:
    result = run("connections", "list", workspace=workspace)
    assert result.exit_code == 0
    assert "local-anthropic" in result.stdout
    assert "[ok ]" in result.stdout


def test_connections_test_names_permissions(workspace: Path) -> None:
    result = run("connections", "test", "local-anthropic", workspace=workspace)
    assert result.exit_code == 0
    assert "Objects    : 1" in result.stdout
    assert "directory listing" in result.stdout
    # v1 stores no credentials, and `file` resolves none at all (§6.7).
    assert "Identity   : none" in result.stdout


def test_connections_peek_reports_detection_and_fidelity(workspace: Path) -> None:
    result = run("connections", "peek", "local-anthropic", "-n", "5", workspace=workspace)
    assert result.exit_code == 0
    assert "none / jsonl" in result.stdout
    assert "Fidelity" in result.stdout
    # Nothing peeked is priced, and no prompt text is shown.
    assert "$" not in result.stdout
    assert "Adjudicate claim" not in result.stdout


def test_connections_rm_names_referencing_runs(workspace: Path) -> None:
    ingested = run(
        "ingest", "local-anthropic", "--window", "2026-08-01..2026-08-31", workspace=workspace
    )
    assert ingested.exit_code == 0

    removed = run("connections", "rm", "local-anthropic", workspace=workspace)
    assert removed.exit_code == 0
    assert "Referenced by 1 run" in removed.stdout

    missing = run("connections", "rm", "local-anthropic", workspace=workspace)
    assert missing.exit_code == 1


def test_unknown_connection_is_an_error(workspace: Path) -> None:
    result = run("connections", "test", "nope", workspace=workspace)
    assert result.exit_code == 1
    assert "no connection named" in result.stderr


# --- ingest --------------------------------------------------------------------


def test_ingest_prints_the_coverage_panel(workspace: Path, expected: dict[str, Any]) -> None:
    result = run(
        "ingest", "local-anthropic", "--window", "2026-08-01..2026-08-31", workspace=workspace
    )
    assert result.exit_code == 0
    assert f"Records    : {expected['records']}" in result.stdout
    assert "Coverage" in result.stdout
    assert "baseline lower bound  : False" in result.stdout
    assert f"duplicates collapsed  : {expected['duplicates_collapsed']}" in result.stdout
    assert "tier A" in result.stdout
    # No analyzer has run, so nothing here is a dollar figure.
    assert "$" not in result.stdout


def test_a_small_unread_object_is_a_lower_bound_not_a_failure(workspace: Path, logs: Path) -> None:
    """Under `max_missing_pct`, the run stands — with the caveat stated (§6.1)."""
    # 22 bytes against ~3.8 KB of listed bytes: 0.57%, under the 2% default.
    (logs / "broken.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x00 truncated garbage")
    result = run("ingest", "local-anthropic", workspace=workspace)

    assert result.exit_code == 0
    assert "baseline lower bound  : True" in result.stdout
    assert "projection withheld   : True" in result.stdout
    assert "broken.jsonl.gz" in result.stdout
    assert "withheld entirely" not in result.stdout


def test_ingest_exits_2_when_coverage_is_incomplete(workspace: Path, logs: Path) -> None:
    """A distinct exit code, so a CI gate can tell 'withheld' from 'failed'."""
    # A corrupt object larger than 2% of the listed bytes crosses the threshold,
    # so savings would be withheld entirely rather than published with a
    # footnote — a baseline over part of the logs is wrong, not smaller.
    corrupt = b"\x1f\x8b\x08\x00" + b"garbage" * 200
    (logs / "broken.jsonl.gz").write_bytes(corrupt)
    result = run("ingest", "local-anthropic", workspace=workspace)

    assert result.exit_code == 2
    assert "withheld entirely" in result.stdout
    assert "broken.jsonl.gz" in result.stdout


def test_ingest_rejects_an_unknown_connection(workspace: Path) -> None:
    result = run("ingest", "ghost", workspace=workspace)
    assert result.exit_code == 1
    assert "no connection named 'ghost'" in result.stderr


def test_ingest_rejects_a_malformed_window(workspace: Path) -> None:
    result = run("ingest", "local-anthropic", "--window", "august", workspace=workspace)
    assert result.exit_code == 1
    assert "START..END" in result.stderr


def test_run_command_refuses_stages_that_do_not_exist(workspace: Path) -> None:
    """A command that pretends to analyze would be worse than one that refuses."""
    result = run("run", "local-anthropic", workspace=workspace)
    assert result.exit_code == 1
    assert "only the ingest stage exists" in result.stderr

    explicit = run("run", "local-anthropic", "--stop-after", "ingest", workspace=workspace)
    assert explicit.exit_code == 0


# --- runs -----------------------------------------------------------------------


def test_runs_list_show_records_and_rm(workspace: Path, expected: dict[str, Any]) -> None:
    empty = run("runs", "list", workspace=workspace)
    assert "No runs yet" in empty.stdout

    run("ingest", "local-anthropic", "--window", "2026-08-01..2026-08-31", workspace=workspace)
    listed = run("runs", "list", workspace=workspace)
    assert "complete" in listed.stdout
    run_id = next(line.split()[0] for line in listed.stdout.splitlines() if line.startswith("run_"))

    shown = run("runs", "show", run_id, workspace=workspace)
    assert f"Records    : {expected['records']}" in shown.stdout

    as_json = run("runs", "show", run_id, "--json", workspace=workspace)
    payload = json.loads(as_json.stdout)
    assert payload["record_count"] == expected["records"]
    assert payload["status"] == "complete"

    records = run("runs", "records", run_id, "-n", "3", workspace=workspace)
    assert records.exit_code == 0
    assert len(records.stdout.strip().splitlines()) == 4  # header + three rows

    records_json = run("runs", "records", run_id, "-n", "2", "--json", workspace=workspace)
    assert len(json.loads(records_json.stdout)) == 2

    removed = run("runs", "rm", run_id, workspace=workspace)
    assert removed.exit_code == 0
    assert run("runs", "rm", run_id, workspace=workspace).exit_code == 1


def test_runs_show_rejects_an_unknown_id(workspace: Path) -> None:
    assert run("runs", "show", "run_nope", workspace=workspace).exit_code == 1


def test_records_command_refuses_a_purged_run(workspace: Path) -> None:
    from llm_cost_auditor.run_store import RunStore

    run("ingest", "local-anthropic", workspace=workspace)
    run_id = RunStore(workspace).list()[0].run_id
    RunStore(workspace).purge_records(run_id)

    result = run("runs", "records", run_id, workspace=workspace)
    assert result.exit_code == 1
    assert "purged" in result.stderr


# --- the CLI and the app see the same store -------------------------------------


def test_a_run_started_at_a_terminal_is_visible_to_the_app(workspace: Path) -> None:
    from fastapi.testclient import TestClient

    from llm_cost_auditor.web.app import create_app

    run("ingest", "local-anthropic", "--window", "2026-08-01..2026-08-31", workspace=workspace)

    with TestClient(create_app(workspace)) as client:
        listed = client.get("/api/runs").json()
        assert len(listed) == 1
        assert listed[0]["record_count"] == 12
        assert listed[0]["run_id"] in client.get("/").text


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Tokyo", "America/New_York"])
def test_window_is_interpreted_in_the_given_zone(workspace: Path, timezone: str) -> None:
    result = run(
        "ingest",
        "local-anthropic",
        "--window",
        "2026-08-01..2026-08-31",
        "--timezone",
        timezone,
        workspace=workspace,
    )
    assert result.exit_code == 0
    assert f"({timezone})" in result.stdout


def test_an_unknown_timezone_is_a_config_error(workspace: Path) -> None:
    settings = config_module.load(workspace)
    settings.timezone = "UTC"
    config_module.save(workspace, settings)

    result = run(
        "ingest",
        "local-anthropic",
        "--window",
        "2026-08-01..2026-08-31",
        "--timezone",
        "Mars/Olympus",
        workspace=workspace,
    )
    assert result.exit_code == 1
