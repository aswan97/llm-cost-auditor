# llm-cost-auditor

A self-hosted platform that ingests LLM provider API logs and produces a ranked, evidence-backed savings report — where prompt caching is missing or misconfigured, where requests are wasted outright, where latency-tolerant work belongs on batch endpoints, and where a cheaper model would have sufficed.

It runs on your own infrastructure, two ways over one engine: a **local web app** for analysts and engineers to run audits and browse findings, and a **CLI** for automation and CI gates. A run started in one is visible in the other.

> **Status: design phase.** [SPEC.md](SPEC.md) is complete; no code has been written yet. The interfaces below describe the intended v1, not working software.

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

Two independent axes — where the logs live, and what they mean — so any combination works without a bespoke integration.

**Where** (connectors): local files and globs, **Amazon S3** (`s3://`, including S3-compatible endpoints), **Azure Blob Storage** (`az://`). Google Cloud Storage follows in v1.1. Compression and container format — gzip/zstd, JSONL, JSON, CSV, Parquet, CloudWatch export envelopes, Azure Monitor diagnostic blobs — are detected and reported, never assumed silently.

Connections are saved by name and config files hold **no secret material** — only a reference. Credentials resolve from the host's own chain (instance profile, managed identity, named profile) where one exists, and otherwise come from the built-in credential store: each secret sealed individually with XChaCha20-Poly1305 under its own derived key, the root key held in your OS keyring or derived from a passphrase with Argon2id (libsodium throughout — no cryptography is implemented here). The store is write-only — there is no API, command, or page that reads a secret back — short-lived kinds (assumed roles, container SAS tokens) are preferred over long-lived keys, and every use is recorded in the run. Storing credentials makes authentication mandatory for any non-loopback bind: `serve` refuses to start without an access token rather than warning about it. Objects are streamed rather than downloaded, the audit window prunes the listing, and every object read is recorded so re-audits are incremental and double counting is detectable. An object that cannot be read is a *coverage failure* naming the gap — never silently less data.

**What** (source adapters): Anthropic, OpenAI, and cloud brokers (AWS Bedrock, Google Vertex AI, Azure AI Foundry) — each with its own price sheet, log shape, and reserved-capacity handling. Gateway logs (LiteLLM, OpenRouter, Helicone) are planned.

## Intended usage

### The app

```bash
llm-cost-auditor serve          # http://127.0.0.1:8787
```

Point it at your logs (or upload them), start a run, watch it progress, and browse the findings — sorted and filtered by analyzer, workload, confidence tier, risk, and effort, each expanding into the evidence and the exact change to make. Coverage and workload pages state what was skipped and which findings are withheld pending a declared risk tier.

It is single-tenant and binds localhost by default: there are no accounts, because there is nothing to authenticate on a server that is yours. Deploying it for a team means putting it behind the SSO proxy or VPN you already have.

### The CLI

Every operation the app performs is a command first, so audits fit in CI and cron:

```bash
llm-cost-auditor ingest  s3://acme-llm-logs/bedrock/ --source bedrock --window 2026-08-01..2026-08-31
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
- **[AGENTS.md](AGENTS.md)** — engineering guidelines and hard rules for contributors.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — branching model (`develop` / `feature/*` / `hotfix/*`), CI gates, and the release process.
- **[CHANGELOG.md](CHANGELOG.md)** — release history.

## Privacy

Runs entirely on your infrastructure — the web app is a server you start, not a service you send logs to. The app makes no outbound calls of its own: no CDN assets, no telemetry, no update check. Raw prompt content is never persisted — hashing and fingerprinting happen at ingest; redaction runs before anything is stored; evidence excerpts in reports are opt-in; the derived store is encrypted with a retention TTL. Any outbound call is gated per-workload and itemized in the report.

## Non-goals

No self-hosted/GPU cost modeling, and no live enforcement — this tool recommends, humans implement. Not multi-tenant: no accounts, no orgs, no hosted service, and no finding-triage workflow — findings export to whatever tracker you already use.
