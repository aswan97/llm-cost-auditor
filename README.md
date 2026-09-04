# llm-cost-auditor

An offline CLI that ingests LLM provider API logs and produces a ranked, evidence-backed savings report — where prompt caching is missing or misconfigured, where requests are wasted outright, where latency-tolerant work belongs on batch endpoints, and where a cheaper model would have sufficed.

> **Status: design phase.** [SPEC.md](SPEC.md) is complete; no code has been written yet. The CLI below describes the intended v1 interface, not working software.

## Why

Most LLM spend analysis stops at "which model cost the most last month". The expensive problems are structural and invisible on a dashboard: a 4k-token system prompt re-sent uncached on every call, an agent loop resending its full history, a cache configured but placed behind volatile content so it never hits, a nightly batch job running against the interactive endpoint at 2× the price, retry storms billing for responses nobody used.

This tool finds those, quantifies them, and tells you the specific change to make.

## Design principles

- **Never in the request path.** It is an auditor, not a gateway. It never routes, caches, or proxies live traffic. The only outbound calls it ever makes are opt-in, budget-capped shadow-replay calls used to earn evidence for routing claims.
- **Degrades with log fidelity.** Every analyzer declares what data it needs. Full prompt content unlocks everything; hashed segments unlock most of it; billing-metadata-only still finds real waste. Missing data becomes an *instrumentation finding* with a dollar ceiling — never a silent skip.
- **Never overstates.** Findings carry a confidence tier (Measured / Simulated / Estimated / Heuristic) and a low–expected–high range. Overlapping findings are de-duplicated by sequential marginal attribution before totalling, and monthly projections are gated on data coverage. Savings that only push spend below a committed contract floor are reported as $0 realizable.

## What it finds

| Category | Examples |
|---|---|
| **Waste** | Retry storms, 429 churn, billed failures, truncation re-requests, cancelled streams, duplicate in-flight requests |
| **Caching** | Uncached shared prefixes, badly placed breakpoints, TTL-expiry misses, cache writes never read, prefix churn from volatile leading content, agent-loop replay waste |
| **Batching** *(v1.1)* | Batch API migration for latency-tolerant workloads, request consolidation, cache-aware scheduling |
| **Routing** *(v1.2)* | Static downgrades, cascades with escalation, overkill parameters (reasoning effort, `max_tokens`, redundant sampling) |

## Sources

Anthropic, OpenAI, and cloud brokers (AWS Bedrock, Google Vertex AI, Azure AI Foundry) — each with its own price sheet, log shape, and reserved-capacity handling. Gateway logs (LiteLLM, OpenRouter, Helicone) are planned.

## Intended usage

```bash
llm-cost-auditor ingest  ./logs/*.jsonl --source anthropic
llm-cost-auditor profile --emit-config workloads.yaml --emit-architecture architecture.yaml
llm-cost-auditor audit   --config workloads.yaml --architecture architecture.yaml \
                         --out report.html --json findings.json --snapshot snapshot.json

# after acting on the findings:
llm-cost-auditor verify  --baseline snapshot.json    # realized vs projected
```

The first run needs no config at all: it discovers workloads, assumes the most conservative risk posture, reports what survives that gating, and writes starter config files — including a draft map of your architecture reverse-engineered from the logs — for you to correct.

Two config files shape the analysis:

- **`workloads.yaml`** — workload matchers and declared correctness/blast-radius tiers. Risk tier is never inferred; guessing low on a regulated workload is the expensive failure.
- **`architecture.yaml`** — an optional description of your GenAI system (components, shared prompt assets, agent traces, environments, unit of work) that seeds workload discovery with priors instead of making it guess, and enables per-unit economics like "$0.41 per claim processed, of which $0.17 is re-sent context".

Enterprise pricing — negotiated rates, discounts, prepaid credits, volume tiers, provisioned capacity — is declared in config and applied as an overlay on the public price table, because auditing a discounted account at list price overstates every finding.

## Documentation

- **[SPEC.md](SPEC.md)** — full design: architecture, data model, analyzers, pricing engine, attribution and confidence, replay harness, privacy posture, validation strategy, and stated known tensions.
- **[CLAUDE.md](CLAUDE.md)** — engineering guidelines and hard rules for contributors.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — branching model (`develop` / `feature/*` / `hotfix/*`), CI gates, and the release process.
- **[CHANGELOG.md](CHANGELOG.md)** — release history.

## Privacy

Runs entirely on your infrastructure. Raw prompt content is never persisted — hashing and fingerprinting happen at ingest; redaction runs before anything is stored; evidence excerpts in reports are opt-in; the derived store is encrypted with a retention TTL. Any outbound call is gated per-workload and itemized in the report.

## Non-goals

No self-hosted/GPU cost modeling, and no live enforcement — this tool recommends, humans implement.
