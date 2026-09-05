#!/usr/bin/env python3
"""Generate the sample log set used by `docker compose` and by hand-verification.

This is **not** the correctness harness. The hand-computed fixtures in
`tests/fixtures/` are what the test suite asserts against; this produces a
larger, more realistic set so the app has something to render and so a human can
read a run's output and judge whether it makes sense (AGENTS.md).

It is deterministic — a fixed seed, no wall-clock — so two people looking at
"the sample data" are looking at the same bytes.

Regenerate with:

    python scripts/make_sample_logs.py
"""

from __future__ import annotations

import gzip
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "examples" / "logs" / "anthropic"
SEED = 20260904
START = datetime(2026, 8, 1, tzinfo=UTC)
DAYS = 31

# Three workloads with deliberately different shapes, so the fidelity mix,
# status mix, and cache telemetry in a run are worth looking at.
WORKLOADS = [
    {
        "name": "claims-extract",
        "model": "claude-sonnet-4-5",
        "api_key_id": "ak_live_7f2",
        "project": "claims",
        "per_day": 9,
        "system_tokens": 4200,
        # Caching is configured and mostly working: a write on the first call
        # of a session, reads afterwards.
        "cache": "configured",
        "tier": "B",
    },
    {
        "name": "support-chat",
        "model": "claude-haiku-4-5",
        "api_key_id": "ak_live_9aa",
        "project": "support",
        "per_day": 14,
        "system_tokens": 900,
        # No caching at all, and a long shared preamble — the shape a prefix
        # finding would eventually be about.
        "cache": "none",
        "tier": "C",
    },
    {
        "name": "research-agent",
        "model": "claude-opus-4-5",
        "api_key_id": "ak_live_c31",
        "project": "research",
        "per_day": 4,
        "system_tokens": 2600,
        # Cache creation is reported without a per-class breakdown, which is
        # the §6.2 unknown-TTL case.
        "cache": "unknown_ttl",
        "tier": "A",
    },
]


def build() -> list[dict[str, object]]:
    rng = random.Random(SEED)
    records: list[dict[str, object]] = []

    for day in range(DAYS):
        for workload in WORKLOADS:
            for index in range(int(workload["per_day"])):
                moment = START + timedelta(
                    days=day, hours=8 + index % 10, minutes=rng.randrange(0, 59)
                )
                records += emit(rng, workload, moment, day, index)

    records.sort(key=lambda r: str(r["timestamp"]))
    return records


def emit(
    rng: random.Random, workload: dict[str, object], moment: datetime, day: int, index: int
) -> list[dict[str, object]]:
    name = str(workload["name"])
    request_id = f"req_{name}_{day:02d}_{index:02d}"
    system_tokens = int(workload["system_tokens"])
    input_tokens = system_tokens + rng.randrange(120, 900)
    output_tokens = rng.randrange(80, 700)

    usage: dict[str, object] = {"input_tokens": input_tokens, "output_tokens": output_tokens}

    if workload["cache"] == "configured":
        if index == 0:
            usage["cache_creation_input_tokens"] = system_tokens
            usage["cache_creation"] = {
                "ephemeral_5m_input_tokens": system_tokens,
                "ephemeral_1h_input_tokens": 0,
            }
            usage["input_tokens"] = input_tokens - system_tokens
        else:
            usage["cache_read_input_tokens"] = system_tokens
            usage["input_tokens"] = input_tokens - system_tokens
    elif workload["cache"] == "unknown_ttl" and index == 0:
        # Cache creation reported with no per-class breakdown (§6.2).
        usage["cache_creation_input_tokens"] = system_tokens
        usage["input_tokens"] = input_tokens - system_tokens

    record: dict[str, object] = {
        "request_id": request_id,
        "timestamp": moment.isoformat().replace("+00:00", "Z"),
        "model": workload["model"],
        "http_status": 200,
        "stop_reason": "end_turn",
        "latency_ms": rng.randrange(700, 5200),
        "max_tokens": 4096,
        "temperature": 0.0 if name == "claims-extract" else 0.7,
        "usage": usage,
        "labels": {
            "api_key_id": workload["api_key_id"],
            "project": workload["project"],
            "endpoint": "/v1/messages",
            "tags": {"env": "prod", "service": name},
        },
    }

    if workload["tier"] == "B":
        record["segments"] = [
            {
                "hash": f"sha256:{name}-system",
                "token_count": system_tokens,
                "kind": "system_prompt",
                "role": "system",
            },
            {
                "hash": f"sha256:{request_id}-user",
                "token_count": int(usage["input_tokens"]),
                "kind": "message",
                "role": "user",
            },
        ]
    elif workload["tier"] == "A":
        record["request"] = {
            "system": f"You are the {name} assistant. " + "Follow the operating rules. " * 40,
            "tools": [{"name": "search_corpus", "description": "Search the internal corpus."}],
            "messages": [{"role": "user", "content": f"Task {day}-{index}: summarize findings."}],
        }

    out = [record]

    # A 429 rejected before generation, followed by the successful re-issue.
    # The rejected attempt costs nothing (§6.5.1).
    if rng.random() < 0.05:
        out.insert(
            0,
            {
                "request_id": request_id,
                "timestamp": (moment - timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
                "model": workload["model"],
                "http_status": 429,
                "error": {"type": "rate_limit_error"},
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )

    # A truncation: billed in full, output unusable.
    if rng.random() < 0.04:
        record["stop_reason"] = "max_tokens"
        usage["output_tokens"] = 4096

    # A 5xx after generation: billed, and the retry is billed too.
    if rng.random() < 0.03:
        out.insert(
            0,
            {
                "request_id": request_id,
                "timestamp": (moment - timedelta(seconds=8)).isoformat().replace("+00:00", "Z"),
                "model": workload["model"],
                "http_status": 500,
                "error": {"type": "api_error"},
                "usage": {"input_tokens": input_tokens, "output_tokens": rng.randrange(50, 400)},
            },
        )

    # A cancelled stream: billed for what was generated before the disconnect.
    if rng.random() < 0.03:
        record["cancelled"] = True
        del record["stop_reason"]

    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.json*"):
        stale.unlink()

    records = build()
    # Split by half-month, and gzip one part, so a run exercises both the plain
    # and the compressed decode path.
    midpoint = len(records) // 2
    plain = OUT / "2026-08-a.jsonl"
    plain.write_text("".join(json.dumps(r) + "\n" for r in records[:midpoint]), encoding="utf-8")

    body = "".join(json.dumps(r) + "\n" for r in records[midpoint:]).encode("utf-8")
    (OUT / "2026-08-b.jsonl.gz").write_bytes(gzip.compress(body))

    print(f"{len(records)} raw records → {plain.name} + 2026-08-b.jsonl.gz in {OUT}")


if __name__ == "__main__":
    main()
