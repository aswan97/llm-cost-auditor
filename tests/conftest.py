"""Shared fixtures. Everything here builds a real workspace on a real filesystem.

No internals are monkeypatched: the tests drive the same code paths the CLI and
the app do, because this tool's failure mode is not a crash — it is a
confident, plausible, wrong number, and a mocked pipeline cannot catch that
(AGENTS.md).
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from llm_cost_auditor import config as config_module
from llm_cost_auditor.config import Connection, WorkspaceConfig

FIXTURES = Path(__file__).parent / "fixtures"
TRAFFIC = FIXTURES / "anthropic" / "traffic.jsonl"
WINDOW = "2026-08-01..2026-08-31"


@pytest.fixture
def expected() -> dict[str, Any]:
    """The hand-computed totals, derived line by line in the fixture README."""
    return json.loads((FIXTURES / "anthropic" / "expected.json").read_text())


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    """A log directory containing the fixture traffic."""
    directory = tmp_path / "logs"
    directory.mkdir()
    shutil.copy(TRAFFIC, directory / "traffic.jsonl")
    return directory


@pytest.fixture
def workspace(tmp_path: Path, logs: Path) -> Path:
    """A workspace whose source scope permits the log directory and nothing else."""
    root = tmp_path / "workspace"
    root.mkdir()
    config_module.save(
        root,
        WorkspaceConfig(
            timezone="UTC",
            sources=[str(logs)],
            connections=[
                Connection(id="local-anthropic", uri=str(logs), source="anthropic"),
            ],
        ),
    )
    return root


def write_gzip(path: Path, source: Path, *, truncate_bytes: int = 0) -> Path:
    """Write a gzip of `source`, optionally chopping the tail off.

    A truncated member is generated rather than checked in so the corruption is
    visible in the test rather than opaque in a binary blob.
    """
    data = gzip.compress(source.read_bytes())
    if truncate_bytes:
        data = data[:-truncate_bytes]
    path.write_bytes(data)
    return path
