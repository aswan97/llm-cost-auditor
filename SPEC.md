# LLM Cost & Routing Auditor — Specification

**Status:** Draft v1.1 · **Date:** 2026-09-04 · **Repo:** `llm-cost-auditor`

*Changed in v1.1: the auditor is a self-hosted platform (local web app + CLI over one run engine), not an offline CLI — §1, §3, §4, §5.3–5.4, §12, §13. Log sources are pluggable connectors (local files, S3, Azure Blob) separate from source adapters — §6.1, §6.6.*

---

## 1. Summary

A **self-hosted platform** that ingests provider API logs and produces a **ranked, evidence-backed savings report**: where prompt caching is missing or misconfigured, where requests are wasted outright, where latency-tolerant work belongs on batch endpoints, and where a cheaper model would have sufficed.

It is used two ways over one engine (§13): a **local web app** (`llm-cost-auditor serve`) where analysts and engineers upload logs, watch a run progress, browse findings, and edit the config that gates them; and a **CLI** for automation, CI gates, and scripted re-audits. Neither is a wrapper around the other — both call the same run engine, and a run started in one is visible in the other.

Four properties define the product:

1. **It never sits in the request path.** It is an auditor, not a gateway. The only outbound network calls it ever makes are opt-in, budget-capped shadow-replay calls used to *earn evidence* for routing claims.
2. **It runs on the user's infrastructure.** The web app is a local-first, single-tenant server the user starts themselves; log data never leaves their trust boundary, and there is no hosted service to send it to (§12).
3. **It degrades gracefully with log fidelity.** Every analyzer declares its minimum data requirements. Missing data becomes an *instrumentation finding* with a dollar ceiling, not a silent skip.
4. **It never overstates.** Every finding carries a confidence tier and a low/expected/high range, findings are de-overlapped before totalling, and projections are gated on data coverage.

---

## 2. Goals and non-goals

### Goals

- Explain the current bill: baseline spend decomposed by workload, model, token class, and waste category.
- Identify caching, batching, routing, and pure-waste savings with defensible numbers.
- Emit the *specific change* to make (where to place a cache breakpoint, which endpoint to migrate, which parameter to lower), not just a diagnosis.
- Let the user prove realized savings after acting, via baseline snapshots and re-audit diffs.

### Non-goals (v1, explicit)

- **Self-hosted / GPU cost modeling.** No open-weights TCO, GPU-hour math, or vLLM/deployment economics. Provider-API spend only.
- **Live enforcement.** The tool never routes, caches, batches, or proxies real traffic. It recommends; humans implement.
- **Multi-tenancy, accounts, and a hosted service.** The platform is single-tenant and runs where the user puts it. No sign-up, no org/user management, no tenant isolation, no billing. Authentication is a deployment concern, not a product feature (§13.1).
- **Collaboration features.** No comments, assignments, notifications, or finding-triage workflow in v1. Findings are exported (`findings.json`, report HTML) into whatever tracker the team already uses.

### Deferred, not excluded (roadmap)

- Fine-tuning and distillation economics ("train a small model to replace the frontier one") — often the largest lever, but requires quality modeling well beyond v1.
- Prompt-quality advice beyond mechanical redundancy detection.

---

## 3. Audience and deliverable

One set of findings, two audiences, three surfaces. The **content** below is fixed; the surfaces differ only in how it is navigated.

- **Executive section** — total spend over the observed window, waste percentage, top 5 findings with dollars and risk rating, portfolio total after overlap de-duplication, a section that would list the top 5 findings that would be hit the hardest with a token price increase.
- **Engineering section** — per finding: evidence, affected workload(s), the exact config/code change, effort estimate, risk notes, and verification steps.
- **`findings.json`** — the same findings machine-readably, for CI gates, dashboards, and diffing across runs.

Surfaces:

| Surface | For | Notes |
|---|---|---|
| **Web app** (§13.1) | Analysts and engineers doing the audit | Interactive: filter and sort findings, drill into evidence, compare runs, edit config and re-run. The primary surface. |
| **Exported report** (`report.html` / `report.md`) | Sharing the result with people who will not open the app | A static, self-contained snapshot of the two sections above. Generated from the same run record the app reads. |
| **CLI + `findings.json`** (§13.3) | Automation, CI gates, scheduled re-audits | Headless. Every operation the app performs is a CLI command first. |

The two audiences are not two products: the executive framing is the app's landing view for a run, and the engineering detail is one click down from any finding.

---

## 4. Scope by release

| Release | Analyzers |
|---|---|
| **v1** | Prefix-cache opportunity + cache-efficiency critique; waste findings (retry storms, 429 churn, truncated/cancelled-but-billed, oversized `max_tokens`, duplicate in-flight requests, redundant context) |
| **v1.1** | Batching: Batch API migration, request consolidation, cache-aware scheduling, concurrency/rate-limit shaping |
| **v1.2** | Routing: difficulty heuristics, natural experiments, cascades, per-request dynamic policy, shadow-replay validation harness |
| **v2** | Fine-tuning/distillation economics; warehouse-pushdown execution |

The data model, pricing engine, workload profiler, confidence framework, and attribution engine are built in v1 because every later analyzer depends on them. Sections 8–10 specify v1.1/v1.2 analyzers in full so the v1 foundations are built to fit them.

**Surfaces by release.** The engine and the CLI come first — they are what the analyzers are tested through, and the app has nothing to render until findings exist.

| Release | Surface |
|---|---|
| **v1** | CLI over the run engine; run store on disk (§5.3); connectors for local files, S3, and Azure Blob (§6.1); web app covering the core loop — connect a log source, run, browse findings, view coverage, download the report |
| **v1.1** | GCS connector; in-app config editing with validation and re-run; run comparison (`verify` diff) in the UI |
| **v1.2** | Replay budget approval flow in the app (a dry-run estimate the user confirms before any outbound call, §10.3) |

The app is not deferred to a "phase 2" — a run that only a CLI can start is not the product described in §1 — but within v1 it is built after the first analyzer produces real findings, not before.

---

## 5. Architecture

The engine is a pure pipeline from logs to findings. The web app and the CLI are two thin drivers over it — neither owns analysis logic, and the pipeline knows about neither.

```
   web app (§13.1)          CLI (§13.3)
        │                        │
        └──────────┬─────────────┘
                   ▼
        ┌────────────────────┐   start / observe / cancel a run
        │    Run engine      │   run record + status + artifacts → run store (§5.3)
        └──────────┬─────────┘
                   ▼
local files · S3 · Azure Blob
      │                                connectors: locate + stream bytes (§6.1)
      ▼                                decode: decompress + container format
┌───────────────┐   per-source adapters (Anthropic, OpenAI, Bedrock, Vertex, Foundry)
│   Ingest      │   normalize → canonical RequestRecord
│               │   redact → fingerprint → (optionally) discard raw text
└───────┬───────┘
        ▼
┌───────────────┐   dedupe retries · reassemble streams · classify failures
│  Normalize    │   multimodal/non-chat unit accounting
└───────┬───────┘
        ▼
┌───────────────┐   metadata grouping → template fingerprint subdivision
│   Workload    │   profile: determinism, latency tolerance, prefix stability,
│   Profiler    │            volume, declared blast radius
└───────┬───────┘
        ▼
┌───────────────┐   versioned price catalog + declared discounts/commitments
│   Pricing     │   → baseline spend, per-token-class
└───────┬───────┘
        ▼
┌───────────────┐   plugin analyzers, each declaring required fidelity
│   Analyzers   │   cache · waste · batching · routing
└───────┬───────┘
        ▼
┌───────────────┐   sequential marginal attribution · confidence tiering
│  Attribution  │   coverage gating · projection
└───────┬───────┘
        ▼
   report.html / report.md / findings.json / snapshot.json
        │
        ▼
   run store → served by the app, read by `verify`, diffed across runs
```

### 5.1 Stack

- **Python 3.12+**, `polars` (in-memory frames), `pydantic` v2 (models/config), `typer` (CLI), `jinja2` (report), `datasketch`-style MinHash/LSH (vendored or dependency), provider tokenizers behind an optional extra.
- **Cloud SDKs are optional extras, one per connector** (`boto3` for `s3`, `azure-storage-blob` + `azure-identity` for `az`). A local-files audit must not pull two clouds' SDKs, and a missing extra produces "install `llm-cost-auditor[s3]`", not an import error.
- **Web app: `fastapi` + `uvicorn`, server-rendered `jinja2`, HTMX for interactivity.** No JavaScript build step, no second language, no SPA. The same template layer renders both the app's pages and the exported static report, so the two cannot drift. HTMX covers the interactions v1 actually needs — polling a running job, filtering and sorting a findings table, expanding evidence, submitting config — and a page that genuinely outgrows it is the signal to reconsider, not a reason to start with React. Charts are server-rendered inline SVG for the same reason.
- **In-memory processing** targeting up to ~500k requests per run on a laptop. Storage is behind a `Store` seam (`load()`, `iter_records()`, `persist_derived()`) so it can be swapped for DuckDB/Parquet without touching analyzers.
- **Rust-portability discipline.** The three hot paths — prefix trie construction, MinHash/LSH, and the discrete-event cache simulator — live behind narrow, pure interfaces with no framework coupling, so each can be replaced by a Rust extension (PyO3) independently if profiling demands it. Everything else stays Python.

### 5.2 Plugin analyzer interface

```python
class Analyzer(Protocol):
    id: str                        # "cache.prefix", "waste.retry_storm"
    required_fidelity: Fidelity    # BILLING_ONLY | HASHED | CONTENT
    required_fields: set[str]      # e.g. {"start_time", "latency_ms"}

    def applicable(self, dataset: DatasetProfile) -> Applicability: ...
        # -> RUN | DEGRADED(reason, confidence_penalty) | BLOCKED(missing, ceiling_estimate)

    def analyze(self, ctx: AnalysisContext) -> list[Finding]: ...
```

`BLOCKED` does not mean silence: the runner converts it into an **instrumentation finding** (§13.5). Log adapters implement a parallel `SourceAdapter` protocol, so new providers are additive.

### 5.3 Runs and the run store

Making this a platform rather than a command means one new durable concept: **the run**. Everything the app shows is a view of a run, and nothing about a run depends on the process that started it still being alive.

A run is a directory under the workspace root (`./.llm-cost-auditor/runs/<run_id>/` by default, configurable):

```
runs/<run_id>/
  run.json          status, timings, config digest, catalog version, connection ids, error (if any)
  manifest.json     every object consumed: uri, etag, size, bytes read, records parsed (§6.6)
  records.parquet   normalized RequestRecords for this run (derived, redacted — never raw prompt text)
  findings.json     the finding set (§13.4)
  snapshot.json     the verification baseline (§13.6)
  report.html       exported report
  log.jsonl         structured progress events, appended as the run executes
```

**Rules:**

- **Runs are immutable once complete.** Re-running with edited config produces a *new* run that records its `parent_run_id`, which is what makes run-to-run comparison (§13.6) honest — there is no in-place mutation to lose.
- **The store is a filesystem directory, not a database.** It is inspectable, diffable, copyable to a colleague, and deletable with `rm -rf`. This is the same `Store` seam as §5.1: DuckDB replaces the parquet/JSON files behind it when scale demands, and nothing above the seam changes.
- **The run record is the only contract between the engine and the app.** The app reads `run.json`, `findings.json`, and `log.jsonl`; it never reaches into analyzer internals. A run produced by the CLI on a build server renders identically in the app.
- **Retention applies to runs** (§12): the TTL that purges ingested data purges the run's `records.parquet` while leaving its findings and report, so an old audit stays readable after its underlying data expires.

### 5.4 Job execution

An audit takes minutes, not milliseconds, so the app cannot run one inside a request handler.

- **One background worker in the server process**, executing runs from a queue with a configurable concurrency of 1 by default. A run holds a whole dataset in memory (§5.1); running two concurrently on a laptop is how the tool gets OOM-killed.
- **Progress is events, not polling into the engine.** The engine appends structured events to `log.jsonl` (`stage`, `pct`, `message`, `counts`); the app tails that file. The CLI renders the same events as a progress bar. One producer, two renderers.
- **A crashed or killed server leaves a run marked `running` with a stale heartbeat.** On startup the server marks such runs `interrupted` rather than resuming them — a half-analyzed dataset must never produce a report.
- **No Celery, no Redis, no external broker.** A threaded queue over the run store is sufficient for a single-tenant local server, and the queue is behind a narrow interface (`submit()`, `status()`, `cancel()`) so a real broker can replace it if the shared-deployment case ever arrives.

---

## 6. Ingest

Ingest has two orthogonal questions, and conflating them is how log tooling ends up with `s3_anthropic_gzip_reader`:

- **Where do the bytes live?** — a **connector** (§6.1). Local disk, S3, Azure Blob.
- **What do the bytes mean?** — a **source adapter** (§6.2). Anthropic, OpenAI, Bedrock, Vertex, Foundry.

They compose freely: Bedrock logs in S3, Bedrock logs on a laptop, and Anthropic logs in Azure Blob are three combinations of two connectors and two adapters, not three integrations. Between them sit two format concerns — compression and container format — that belong to neither.

```
connector (locate + fetch)  →  decode (decompress + container)  →  adapter (interpret)  →  RequestRecord
   file:// s3:// az://           .gz/.zst · jsonl/json/csv/           anthropic/openai/
                                 parquet/cloudwatch/azure-monitor      bedrock/vertex/foundry
```

### 6.1 Connectors — where the logs live

Nobody audits a bill from logs that are already on their laptop. Real provider logs land in object storage — an S3 bucket fed by Bedrock model-invocation logging, an Azure Blob container fed by Foundry diagnostic settings, a nightly export dropped on a share — and the tool is useless if getting at them is the user's problem.

**v1 connectors:**

| Connector | URI | Notes |
|---|---|---|
| **Local** | `file:///path`, plain paths, globs | Files and directories, recursive. The development and first-run path, and the one every fixture uses. |
| **Amazon S3** | `s3://bucket/prefix/` | Bedrock model-invocation logs and CloudWatch Logs exports both land here. Also S3-compatible endpoints (MinIO, R2) via an explicit `endpoint_url`. |
| **Azure Blob Storage** | `az://container/prefix/` | Where Azure AI Foundry diagnostic settings and Azure Monitor archives land, in the `y=/m=/d=/h=/` layout. |

Google Cloud Storage (`gs://`) follows in v1.1. The cloud **log query APIs** — CloudWatch Logs, Azure Monitor / Log Analytics, Google Cloud Logging — are deliberately *not* v1 connectors: they are paginated query interfaces with their own quotas, retention, and per-GB scan costs, not object stores, and every one of them can export to a bucket the tool already reads. The blob path is cheaper for the user and simpler for us; the query APIs are revisited only if export turns out not to be an option in practice (§16).

**Build order** (AGENTS.md): local first, S3 second, and the `Connector` protocol is *extracted* once S3 exists — the interface that fits a local directory will be wrong about pagination, listing cost, retries, and credentials. Azure Blob is the third and is written against the extracted protocol as the test of whether it generalized.

**The protocol is narrow on purpose.** Listing and byte streams, nothing else — no parsing, no provider knowledge, no caching policy:

```python
class Connector(Protocol):
    scheme: str

    def list(self, prefix: str, since: datetime | None, until: datetime | None) -> Iterator[ObjectRef]: ...
        # ObjectRef: uri, size_bytes, last_modified, etag

    def open(self, ref: ObjectRef) -> BinaryIO: ...
        # streaming; the caller never holds a whole object in memory
```

**Rules:**

- **Stream, never download-and-parse.** Objects are read as bounded-buffer streams and decompressed on the fly, with a small prefetch pool so network latency overlaps parsing. A day of logs is routinely larger than RAM, and the in-memory budget (§5.1) is for normalized records, not raw bytes.
- **Prune the listing with the window, not the parser.** Connections declare a partition template (`y=%Y/m=%m/d=%d/`); an audit window is expanded into the key prefixes it can possibly touch, so a one-week audit against three years of logs lists a week of keys. Where no template applies, `last_modified` filters the listing, and records outside the window are still dropped after decode — log files do not align with audit windows. The report states the requested window and the observed one.
- **Credentials are referenced, never stored.** Each connector uses its cloud's own default credential chain — environment, instance profile / IRSA / managed identity, or a named local profile. A connection stores a *reference* (profile name, role ARN to assume, account and container), and **the UI has no secret field at all**. This is a direct consequence of §12: the server is unauthenticated by design, so it must not become a place where secrets live. Read-only permissions are what the docs ask for, and the connection test says which permissions were actually exercised.
- **Only configured connections are readable.** A run names a connection; it cannot name an arbitrary URI. Without that rule an unauthenticated local server with an instance profile is a credential-borrowing exfiltration primitive — anything reachable in the browser could ask it to read any bucket the host can reach, and write nothing down. Path confinement for `file://` (§12) is the same rule wearing different clothes.
- **A partial read is a failure, not less data.** A truncated gzip member, a permission-denied key, a listing that timed out mid-page: each marks the run's coverage incomplete, naming the skipped objects and their byte volume, and gates projections exactly as §11.4 does. Baseline spend computed over 87% of the logs is not a smaller number — it is a wrong one, and the failure mode this whole tool exists to avoid.
- **Every object consumed is recorded** (§6.6), so re-ingest is incremental and double-counting is detectable rather than accidental.

**Formats are a separate, shared layer** — every connector yields bytes, and the same decode stack handles all of them. Compression (`.gz`, `.zst`, `.bz2`) and container format are auto-detected from the key and the first bytes, always with an explicit override:

| Container | Where it shows up |
|---|---|
| JSON Lines | The common case; one request per line |
| JSON array / single object | Small exports and API dumps |
| CloudWatch Logs export envelope | Gzipped objects whose records are wrapped in `logEvents[].message`, each message itself JSON |
| Azure Monitor diagnostic blobs | `{"records": [...]}` per blob, in the hour-partitioned layout |
| CSV, Parquet | Warehouse and billing-export extracts |

Detection is reported, never assumed silently: the run record states the format and compression chosen per object, and an object that does not parse as its detected format is a coverage failure rather than a skipped line.

**Connector conformance suite.** One shared test suite that every connector must pass — listing pagination, window pruning, an empty prefix, a truncated object, a permission-denied key, an object mutated between listing and read, and an object whose content type contradicts its extension. It runs against a local fake implementing the protocol, and against the cloud emulators (MinIO, Azurite) in CI. A connector is not done until it fails these the same way local does.

### 6.2 Sources — what the logs mean (v1)

| Source | Notes |
|---|---|
| **Anthropic** | Exposes `cache_creation_input_tokens` / `cache_read_input_tokens`, enabling direct measurement of realized cache savings and cache-efficiency critique. Distinguishes 5-minute vs 1-hour cache TTL. |
| **OpenAI** | Automatic prompt caching with no explicit breakpoints; `cached_tokens` is reported but not user-controlled, so findings shift from "add cache_control" to "restructure prompt so the automatic cache can engage". |
| **Bedrock / Vertex / Foundry** | Cloud-broker billing across AWS Bedrock, Google Vertex AI, and Azure AI Foundry. Each has its own price sheet (broker prices diverge from first-party list prices and from each other), its own log shape (CloudWatch Logs / Cloud Logging / Azure Monitor diagnostic settings), and its own reserved-capacity construct — Bedrock Provisioned Throughput model units, Vertex PTUs, Foundry PTUs — all of which mark traffic as already-paid-for (§7.2). Caches are scoped per deployment/region on all three, which the simulator must partition on (§9.2). |

Gateway logs (LiteLLM, OpenRouter, Helicone) are a post-v1 adapter that maps onto the same canonical record.

### 6.3 Fidelity tiers

Every dataset is classified, per-source, into the highest tier it supports. Mixed-tier datasets are supported; findings are tagged with the tier that produced them.

| Tier | Contains | Unlocks |
|---|---|---|
| **A — Content** | Full request/response bodies: system prompt, tools, messages, completion | Everything: token-exact prefix tries, response/semantic caching, redundant-context detection |
| **B — Hashed** | No raw text. Per-segment hashes (system prompt, each tool definition, each message) + rolling 1k-token prefix hashes, token counts, params | Prefix caching, exact-match response caching, conversation replay waste, all waste findings. No semantic caching, no content-based redundancy detection |
| **C — Billing** | Timestamps, model, token counts, latency, request id, status | Waste findings (retries, truncation, 429s), spend decomposition, latency-tolerance profiling, batching eligibility. No cache analysis |

**Design consequence:** Tier B is the recommended posture and the tool ships a reference "fingerprint sidecar" spec — a small library/logging snippet users add to their client so their logs become Tier B without ever storing prompt text.

### 6.4 Canonical record

```
RequestRecord
  request_id, parent_request_id (retries/streams), source, provider, model, model_version
  start_time, end_time | latency_ms
  status: ok | error_billed | error_unbilled | cancelled | truncated
  stop_reason, http_status, error_code
  params: temperature, top_p, max_tokens, tools[], tool_choice, thinking/reasoning_effort, seed
  usage: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens(+ttl class),
         reasoning_tokens, image_tokens, audio_tokens, video_tokens, embedding_tokens
  content_ref: segments[] (hash, token_count, role, kind, volatility_flag) | raw (tier A, in-memory only)
  labels: api_key_id, project, tags{}, user_id, session_id, endpoint
  batch_flag, region/deployment_id
```

### 6.5 Normalization edge cases (all mandatory in v1)

Each of these silently corrupts cost math if ignored.

1. **Retries and duplicate records.** Deduplicate by request id across at-least-once log delivery. Distinguish *logged twice* (one billing event) from *genuinely retried* (multiple billing events). Critically: a 429-triggered retry costs nothing, while a 500 after generation may have been billed in full. Classification rules are per-provider and asserted in golden fixtures.
2. **Failed / truncated / cancelled requests.** Input tokens are frequently billed despite an error; `stop_reason=max_tokens` truncations often mean the response was unusable and re-requested; client-cancelled streams still bill generated tokens. These form their own finding class (§9.1), not just an ingest concern.
3. **Streaming reassembly.** Collapse SSE chunk logs into one record with final usage. When the terminal usage event is missing, estimate tokens from reassembled content (Tier A) or from chunk counts (Tier B/C) and mark the record `usage_estimated=true`, which propagates a confidence penalty to any finding relying on it.
4. **Multimodal and non-chat endpoints.** Image/audio/video token accounting per provider formula, plus embeddings, rerank, and legacy completions — each with its own pricing unit. Unknown endpoints are counted in baseline spend but excluded from analyzers, and reported in the coverage panel.

### 6.6 Connections and the ingest manifest

A **connection** is a named, saved binding of a connector to a location and a source — the thing a user configures once and then audits against repeatedly. It is config, not state, and it holds no secrets (§6.1).

```yaml
connections:
  - id: prod-bedrock-s3
    connector: s3
    uri: s3://acme-llm-logs/bedrock/invocation-logs/
    region: us-east-1
    auth: { profile: llm-audit-readonly }   # or: role_arn — a reference, never a secret
    partition: "y=%Y/m=%m/d=%d/"            # prunes listing to the audit window
    format: auto                            # jsonl | json | csv | parquet |
    compression: auto                       #   cloudwatch_export | azure_monitor | auto
    source: bedrock

  - id: foundry-diagnostics
    connector: az
    uri: az://insights-logs-requestresponse/
    account: acmellmlogs
    auth: { credential: default }           # DefaultAzureCredential chain
    partition: "y=%Y/m=%m/d=%d/h=%H/"
    source: foundry

  - id: local-export
    connector: file
    uri: ./logs/anthropic/*.jsonl.gz
    source: anthropic
```

**The ingest manifest.** Every run records the exact objects it consumed — uri, etag, size, byte count read, records parsed, records rejected — in its run record (§5.3). This is what makes a platform's repeat runs behave:

- **Incremental re-ingest.** An object already ingested at the same `(uri, etag, size)` is skipped. A weekly re-audit reads the new week, not the year, and the run states how many objects were reused versus read.
- **Mutable objects are handled explicitly.** A still-being-appended `today.jsonl` whose etag changed is re-read in full, and its overlapping records are collapsed by request id (§6.5.1). The manifest records that this happened, because "the same request appeared in two objects" is otherwise indistinguishable from a genuine retry — and that distinction is the difference between a real finding and a double-counted one.
- **Double counting is detectable, not accidental.** Two connections whose prefixes overlap, or an export re-uploaded under a new key, show up as the same request ids arriving from different objects. That is reported as an ingest warning naming both objects, rather than quietly inflating baseline spend.
- **A run is reproducible.** The manifest names exactly what was read, so a disputed number can be traced back to the objects that produced it — the first question anyone asks when a savings figure looks wrong.

---

## 7. Pricing engine

### 7.1 Price table

All prices live in a **single versioned price table** — data, never code. It is the only place a monetary rate exists anywhere in the system (see `AGENTS.md`: no price literals, ever).

**Contents.** Base rates transcribed from **publicly available provider pricing pages**, per model and per provider/broker (first-party Anthropic and OpenAI rates differ from the same model's Bedrock, Vertex, and Foundry rates, so each is its own row):

- input and output cost per unit (per-MTok, or per-image/per-second/per-character where the provider bills that way);
- cache-read discount and cache-write premium multipliers, per TTL class;
- batch-endpoint discount multiplier;
- minimum cacheable token threshold and maximum breakpoint count;
- `effective_from` / `effective_to` so each request is priced at the rate in force at **its own timestamp**, not today's rate;
- **`last_verified`** — the date a human last checked this row against the provider's published pricing page, plus `source_url`.

```yaml
- model: claude-sonnet-4-5
  provider: anthropic            # or: bedrock | vertex | foundry — separate rows, separate rates
  effective_from: 2025-09-29
  effective_to: null
  units: per_mtok
  input: <price>
  output: <price>
  cache_write_5m_multiplier: 1.25
  cache_write_1h_multiplier: 2.0
  cache_read_multiplier: 0.1
  batch_multiplier: 0.5
  min_cacheable_tokens: <provider/model specific>
  last_verified: 2026-09-04
  source_url: https://...
```

**Staleness is surfaced, never silent.** Every report states the catalog version and the oldest `last_verified` date among the rows actually used. Rows older than a configurable threshold (default 90 days) raise a warning on the affected findings; rows with no matching entry for a model produce a *missing price* coverage entry rather than a guessed number — an unpriced model is excluded from savings math and reported as such.

**Loaded on demand, not per session.** The table is not parsed at import or CLI startup. The pricing module lazily loads and memoizes only the rows it is asked for — keyed by `(provider, model, timestamp)` — on first lookup, so a run that touches four models never reads the rest of the catalog. `refresh-prices` is an explicit, separate command that updates the bundled table and bumps `last_verified`; a normal `audit` run never fetches anything.

**Consumption rule.** Analyzers never see raw numbers. They call `pricing.rate(provider, model, at=timestamp)` and `pricing.multiplier(...)`, so a price change, a new broker, or a new discount class never requires touching analyzer code.

### 7.2 User-declared commercial terms

Public list prices are the *default*, not the truth. Anyone with a bill large enough to audit is likely paying something else: an enterprise discount, negotiated per-model rates, prepaid credits, volume tiers, or reserved capacity. Auditing them at list price overstates every finding — precisely for the customers with the most at stake — so the config supports a **commercial overlay** on top of the price table.

The overlay never edits the catalog; it is applied at lookup time by the pricing module, so provenance stays intact and the report can show list vs effective rates side by side.

```yaml
commercial:
  currency: USD

  anthropic:
    discount_pct: 15                     # blanket enterprise discount off list

    # Per-model overrides win over the blanket discount.
    model_overrides:
      - model: claude-opus-4-5
        discount_pct: 22
      - model: claude-sonnet-4-5
        rates:                           # absolute negotiated rates, not a percentage
          input_per_mtok: <rate>
          output_per_mtok: <rate>
        cache_read_multiplier: 0.08      # negotiated cache/batch terms differ from public ones
        batch_multiplier: 0.4

    # Discounts are frequently time-boxed; each entry carries its own validity window.
    effective_from: 2026-01-01
    effective_to: 2026-12-31

    committed_monthly_usd: 40000         # savings below this floor are non-realizable
    prepaid_credits_usd: 250000          # spend drawn from credits, not new cash
    volume_tiers:                        # marginal-rate tiers, applied on monthly cumulative spend
      - up_to_usd: 100000
        discount_pct: 10
      - up_to_usd: null
        discount_pct: 18

  bedrock:
    discount_pct: 0
    provisioned_throughput:
      - model: <model>
        model_units: 4
        hourly_usd: <rate>
        window: 2026-08-01..2026-08-31   # traffic here is already paid for

  foundry:
    ptu:
      - deployment: <deployment-id>
        units: 300
        monthly_usd: <rate>
```

**Precedence**, most specific wins: per-model absolute rates → per-model discount → volume tier → provider blanket discount → public list price. Every step is recorded, and `--explain-pricing` prints the resolution chain for any request.

**Rules:**

- Savings that merely move spend below a **committed floor** are reported as **$0 realizable**, with the gross number shown separately. The report says which findings are floor-suppressed and what the floor is — because the right action there may be renegotiating the commitment, not caching.
- Spend covered by **prepaid credits** is real cost but not new cash; findings against it are labeled `realizable: deferred` so finance can distinguish burn-rate extension from budget reduction.
- **Volume tiers** make savings self-limiting: cutting spend can drop the account into a worse tier, partly offsetting the win. The optimizer accounts for the marginal rate at the projected post-savings volume, not the current one.
- Traffic served by **provisioned capacity** (Bedrock model units, Vertex/Foundry PTUs) is marked *already paid for*; caching or downgrading it saves nothing until utilization drops far enough to retire a unit. The report states the utilization headroom and the unit-retirement threshold explicitly.
- **Expired terms are not applied.** An overlay entry outside its validity window falls back to list price with a warning, so a lapsed contract cannot silently deflate a whole audit.
- If no `commercial` block exists, the run uses list prices and the report says so prominently — an audit at list price against a discounted account is the most common way these numbers get quietly inflated.
- **Invoice reconciliation** (§14.4) works in the opposite direction: given a real bill, it *solves for* the effective discount and flags unexplained spend, which is also how a user validates that their declared overlay is correct. A >2% gap between declared terms and the invoice is reported as a config error.

---

## 8. Workload discovery and profiling

Everything downstream depends on this grouping being right.

### 8.1 Declared architecture map (optional, strongly recommended)

Clustering has to reverse-engineer a system the user already knows the shape of. The config therefore accepts an **architecture map** — a description of the GenAI system that produced the logs — which seeds discovery with priors instead of making it guess. It is optional: without it the tool falls back to pure clustering (§8.2), and with it, cluster quality, cache analysis, and cost attribution all improve materially.

```yaml
architecture:
  environments:                       # dev/staging traffic must not drive prod recommendations
    prod:   { match: { tag.env: prod } }
    staging:{ match: { tag.env: [staging, dev] }, exclude_from_findings: true }

  # Shared prompt assets: the same scaffold used by several components.
  shared_assets:
    - id: claims_system_prompt
      kind: system_prompt
      used_by: [claims-extract, claims-adjudicate]
      expected_tokens: ~4200
      volatility: static             # static | per-tenant | per-request
    - id: core_toolset
      kind: tool_definitions
      used_by: [orchestrator, research-agent]
      volatility: static

  components:
    - id: claims-extract
      type: pipeline_stage           # chat | agent_loop | pipeline_stage | batch_job |
                                     # rag_retrieval | eval_harness | background_task | subagent
      match:                         # any combination; all matchers are optional
        api_key_id: [ak_live_7f2]
        endpoint: /v1/messages
        model: [claude-sonnet-4-5]
        tag.service: claims-extract
        prompt_contains: "Extract the following fields"
      invocation: webhook            # user_interactive | cron | webhook | queue | fan_out
      latency_slo: none              # none | seconds | sub_second  (a prior, not a fact)
      blast_radius: regulated        # feeds the §8.3 correctness axis
      uses_assets: [claims_system_prompt]
      deployment: { provider: bedrock, region: us-east-1, deployment_id: dep-3 }

    - id: orchestrator
      type: agent_loop
      children: [research-agent, claims-extract]   # sub-agent calls attributed up to the parent
      trace_key: tag.trace_id                      # links a multi-request trace into one unit

  # Business unit economics: what one "unit of work" is, so cost per unit can be reported.
  unit_of_work:
    id: claim
    counted_by: { root_component: claims-extract, key: tag.claim_id }
```

**How discovery uses it (priors, not overrides):**

1. **Components become the top level of the hierarchy** (§8.2), ahead of raw labels. Traffic matching no component lands in an `unmapped` bucket which is reported, clustered normally, and offered back as suggested `components:` entries to paste into the map.
2. **Declared values are priors the data can contradict, and disagreements are reported.** If a component is declared `latency_slo: none` but 92% of its requests carry a session id and show interactive arrival patterns, the report says so rather than silently trusting either side. Two exceptions are hard rules, never overridden by inference: `blast_radius` (guessing low on a regulated workload is the expensive failure, §8.3) and `exclude_from_findings`.
3. **Topology mismatches become findings.** When fingerprint clusters cut across declared component boundaries — one declared component serving three prompt shapes, or three components sharing one skeleton — that is surfaced. Both directions are useful: the first usually means an undeclared branch, the second means an unrecognized shared prefix and therefore an unrealized cache opportunity.
4. **`shared_assets` directly seeds cache analysis.** A declared shared system prompt or toolset tells the prefix analyzer where to look before any trie is built, and — combined with `deployment` — reveals the case clustering cannot see on its own: the same 4k-token scaffold cached separately in three regions or deployments, paying the write premium three times (§9.2 partitioning).
5. **`children` / `trace_key` enable cross-request analysis.** Agent-loop and pipeline traces are linked into one logical unit, so sub-agent spend is attributed to the orchestrating workload, conversation-replay waste is measured across the whole trace, and per-stage cost is rolled up end to end.
6. **`unit_of_work` converts findings into unit economics** — "$0.41 per claim processed, of which $0.17 is re-sent context" — which is both the more actionable framing and the one that survives traffic growth in the verification diff (§13.6).
7. **`environments` keeps non-prod out of the numbers**, while still reporting non-prod spend separately; eval-harness and staging traffic otherwise distort both baseline spend and determinism profiling.

The map is validated on load: unknown component references, unmatched matchers (declared components that match zero requests), and overlapping matchers all produce config errors rather than silent misgrouping.

### 8.2 Hierarchical discovery

1. **Group by declared architecture components** (§8.1) where a map is supplied.
2. **Group by declared labels** — API key, project, tags, endpoint — for anything unmapped, wherever the logs carry them.
3. **Subdivide each group by template fingerprint.** Normalize variable slots (numbers, UUIDs, dates, injected context blocks, retrieved chunks) and cluster on the invariant skeleton. One API key serving three prompt shapes is analyzed as three workloads.
4. **User override.** `workloads.yaml` matchers (regex, model, key, endpoint) take precedence over all of the above.

Cluster quality is reported: cluster count, coverage %, unmapped-traffic share, and a "possible over-merge/over-split" warning when intra-cluster prompt-length variance is extreme.

### 8.3 Profile axes

Each workload is scored on four axes; **recommendations are gated on the profile, never issued globally.** The motivating contrast: insurance-claim processing (low volume, latency-tolerant, highly templated, deterministic, correctness-critical) wants near-opposite advice from a consumer chatbot (high volume, latency-bound, volatile prefixes, tolerant of variation).

| Axis | Derived from | Gates |
|---|---|---|
| **Determinism** | `temperature`/`top_p`, tool-use presence, observed output variance across near-identical inputs | Whether response-level or semantic caching may be recommended *at all* |
| **Latency tolerance** | Arrival pattern, session/interactive signals, observed latency distribution, presence of a human in the loop | Batch API eligibility (hours of turnaround), slower-but-cheaper routing |
| **Prefix stability & volume** | Invariant prompt fraction, volatility of leading tokens, reuse rate within TTL, requests/hour | Whether prefix caching pays for its write premium at this volume at all |
| **Correctness / blast radius** | **User-declared** — per component in the architecture map (§8.1) or in `workloads.yaml` (`low` / `medium` / `high` / `regulated`) | Suppresses model downgrades, cascades, and semantic-cache findings regardless of what the math says |

Blast radius is deliberately *not* inferred from content or from the declared component type: guessing low on a regulated workload is the expensive failure.

### 8.4 Zero-config first run

The first run requires no config. It:
- discovers workloads automatically,
- assumes the **most conservative correctness tier** everywhere (suppressing downgrade, cascade, and semantic-cache findings),
- produces all findings that survive that gating, and
- writes a starter config containing **both** a `workloads.yaml` (discovered clusters, inferred latency tolerance, sample fingerprints, **blank risk tiers**) and a draft `architecture:` map (§8.1) reverse-engineered from the logs — one `components:` entry per discovered cluster with its observed matchers, inferred type and invocation pattern, detected deployments, and candidate `shared_assets` where a prefix is shared across clusters.

The draft map is a starting point to be corrected, not a description to be trusted: the tool cannot see parent/child agent relationships, environments, or units of work that the logs do not label. The report states plainly which findings are withheld pending risk declaration, their combined dollar ceiling, and what additional analysis a completed architecture map would unlock — so editing the config has visible, quantified value.

---

## 9. Analyzers — v1

### 9.1 Waste findings (Tier C and up)

Cheap to compute, uncontroversial, immediately actionable, and available even on billing-only logs. This is the credibility beachhead.

| Finding | Detection | Savings basis |
|---|---|---|
| **Retry storms** | Bursts of same-fingerprint requests within a short window, correlated with error codes | Sum of billed tokens on redundant attempts (Measured) |
| **429 churn** | Rate-limit responses followed by immediate re-issue; quantifies wasted round trips and any billed partials | Measured + latency impact |
| **Billed failures** | `error_billed` records: input tokens charged for requests that produced nothing usable | Measured |
| **Truncation waste** | `stop_reason=max_tokens` followed by a re-request of the same fingerprint | Measured (full cost of the truncated attempt) |
| **Cancelled streams** | Client disconnects after N generated tokens | Measured |
| **Oversized `max_tokens`** | Reserved vs actually-generated output distribution per workload | Not directly billed, but flagged where it forces smaller batch windows or blocks provisioned capacity; reported as a risk/limit finding, **$0 unless the provider bills reservation** |
| **Duplicate in-flight** | Identical (fingerprint, params) requests overlapping in time — coalescing candidates | Measured |
| **Redundant context** (Tier A) | Context blocks re-sent every turn that provably never vary and never influence output structure | Simulated |

### 9.2 Prefix-cache opportunity (Tier B and up)

**Two-stage detection.**

1. **Segment-boundary hashing (fast pass, all traffic).** Hash at natural boundaries — system prompt, each tool definition, each message — and find shared leading segment runs per (model, workload). This aligns with *where users can actually place a cache breakpoint*, so findings are directly implementable.
2. **Token-aware prefix trie (refinement, top-N workloads by spend).** Build a trie over tokenized prefixes to compute the optimal breakpoint set and exact cacheable token counts, respecting per-model minimum-cacheable-token thresholds and the provider's maximum breakpoint count. Applied only to the heaviest workloads because it is the expensive path.

**Discrete-event cache simulator.** Requests are replayed in timestamp order against a modeled cache:

- per-entry **TTL** (5-minute refresh-on-read, 1-hour variants), with expiry driving realistic misses;
- **minimum cacheable token thresholds** — sub-threshold prefixes cannot cache at all;
- **write premium** (1.25× / 2.0×) always paid before any read benefit;
- **eviction** and cache-size limits where the provider documents them;
- **burst / concurrency effects**: N requests in flight against a cold prefix all pay the write premium. This is not a refinement — for bursty traffic it is the difference between a credible number and a fantasy;
- **partitioning** by region/deployment: caches do not span deployments, so per-region traffic is simulated separately.

**Getting request overlap** (needed for the burst model) is chosen per dataset and labeled on every finding:
- *Exact overlap* when `start_time` and duration/`latency_ms` are available — the accurate path;
- *Poisson arrival approximation* otherwise, using observed per-minute arrival rate and a per-model latency prior. Findings derived this way are capped at the **Simulated** confidence tier and state the assumption inline.

**Output per finding:** current vs simulated hit rate, breakpoint placement instruction, expected monthly savings range, and the break-even reuse rate below which caching *loses* money.

### 9.3 Cache-efficiency critique (Anthropic/OpenAI cache telemetry)

Existing caching is treated as a baseline, and the report quantifies **the gap** — because most real waste is a half-configured cache, not an absent one:

- **TTL-expiry misses** — reuse that arrived just outside the window; quantifies the 1-hour TTL upgrade trade (2× write premium vs recovered reads).
- **Badly placed breakpoints** — cacheable tokens left outside the breakpoint, or breakpoints below the minimum threshold.
- **Write-never-read** — cache creations that were never subsequently read. Pure loss, and a strong, easily verified finding.
- **Prefix churn** — volatile content (timestamps, session ids, retrieved chunks, shuffled tool definitions) early in the prompt invalidating an otherwise stable prefix. The fix — reorder so volatile content comes last — is usually a few lines.
- **Realized-savings scoreboard** — what caching already saved, so the report is not purely critical.

### 9.4 Response-level and near-duplicate caching

- **Exact-match response cache** (Tier B+): identical `(model, params, full prompt)` → reuse the prior completion. Gated on the determinism axis and blast radius; time-sensitive prompts (detected date/now/"current" markers, or observed output drift for identical inputs) are excluded and reported as such.
- **Conversation / agent replay waste** (Tier B+): multi-turn threads and agent loops resending full history uncached — the quadratic token-growth case, typically the single largest cache finding in agentic workloads. Reported as tokens re-sent per thread and the savings from incremental caching at turn boundaries.
- **Retrieval / context dedup** (Tier A, degraded on B): the same RAG chunks, file contents, or tool schemas re-sent across requests.
- **Semantic / near-duplicate cache**: **MinHash + LSH over token shingles** is the default engine — no ML dependency, deterministic, explainable, and it catches near-identical prompts. **Embedding-based clustering is an opt-in extra** for paraphrase-level findings, using either a local model or the configured replay backend (§10.3); it carries its own, lower confidence tier and always states the similarity threshold and the estimated false-hit rate. Suppressed entirely for `high`/`regulated` blast radius.

---

## 10. Analyzers — v1.1 / v1.2 (specified now, built later)

### 10.1 Batching (v1.1)

| Mechanism | Detection | Risk |
|---|---|---|
| **Batch API migration** | Workloads scoring latency-tolerant, with no interactive session signal; models the 50% discount against turnaround SLA | Must prove hours of tolerance — the claims case, never the chatbot |
| **Request consolidation** | Many small same-template requests → one multi-item prompt, amortizing the system prompt across N items | Item interference, partial failure, output parsing; recommended only for `low`/`medium` blast radius and deterministic workloads |
| **Cache-aware scheduling** | Reorder/group prefix-sharing requests so they land inside one TTL window instead of each paying a write | Directly coupled to the cache simulator; savings computed by re-running the simulation under the proposed schedule |
| **Concurrency & rate-limit shaping** | Retry storms, 429-driven waste, duplicate in-flight coalescing | Low risk; overlaps §9.1 and is de-duplicated by the attribution engine |

### 10.2 Routing (v1.2)

**Evidence pillars, in ascending strength:**

1. **Difficulty heuristics** — context length, reasoning markers, tool-call depth, output length, structured-vs-open-ended output. Zero cost, zero data exposure, but an *opinion*. Capped at the **Heuristic** tier and never sufficient on its own for a downgrade recommendation on `high`/`regulated` workloads.
2. **Natural experiments in logs** — workloads that already ran on multiple models. Compare observed downstream outcomes: retry rate, follow-up turns, user regeneration, tool-failure rate, thread length. Free, real evidence, but confounded — the report names the confounds it could not control for.
3. **Shadow replay** (§10.3) — the only pillar that produces direct evidence.

**Recommendation shapes:**

- **Static per-workload downgrade** — whole workload moves to a cheaper model. Easiest to implement, verify, and roll back.
- **Cascade with escalation** — cheap model first, escalate on a validation signal. The analyzer models the escalation rate explicitly and **reports when cascading costs more than not cascading** (the common failure at high escalation rates).
- **Per-request dynamic routing** — a policy keyed on request features. Highest ceiling; because its evidence is heuristic-plus-sampled-replay rather than exhaustive, it is capped at the **Estimated** tier and always presented with the static downgrade alternative alongside.
- **Overkill parameters** — not model swaps: unnecessary extended thinking / high reasoning effort, redundant self-consistency sampling, needless retries, oversized `max_tokens` reservations. Frequently the highest-confidence routing-adjacent money.

### 10.3 Shadow-replay harness

Opt-in, budget-governed re-execution of sampled requests against a candidate cheaper model.

**Backend is pluggable.** Default is **OpenRouter** (breadth of models behind one key). Alternatively the replayer targets **the customer's existing provider or platform** — Bedrock, Azure AI Foundry, Vertex, or a direct provider key — so prompt content never leaves the trust boundary they have already approved, and open-weights models can be skipped entirely if they prefer.

**Sampling: stratified + sequential stopping.**
- Stratify candidate requests by workload cluster, weighted by spend.
- Start at ~20 samples per candidate downgrade; after each block, run a sequential test on the agreement rate against the acceptance threshold.
- Stop early when the result is decisive in either direction. Budget flows to the highest-value, most-uncertain candidates; obvious losers are abandoned after a handful of calls.
- Within a stratum, oversample requests the difficulty heuristic flags as borderline — those decide whether the downgrade is safe.

**Budget governance:**
- **Pre-flight estimate** — a dry run prints projected replay cost (token counts × backend prices) and requires explicit confirmation.
- **Hard ceiling** — aborts mid-run when hit, preserving partial results and marking untested candidates as unvalidated.
- **Percentage cap** — default budget scales with the bill being audited (default 0.5% of analyzed spend), so the audit is always cheap relative to the prize.
- **Persistent replay cache** — results keyed by `(prompt_hash, model, params, backend)`, so re-runs and repeat audits never pay twice.
- Judge calls default to a small/cheap model with a token cap.

**Judging (no ground truth available), in cost order:**
1. **Deterministic checks first** — free and unambiguous: JSON-schema validity, tool-call name/argument match, numeric/enum equality, length sanity. For structured workloads (claims extraction) these alone often decide it.
2. **Embedding similarity** — a cheap continuous agreement score for free-text outputs, used as a screen.
3. **LLM judge on disagreement only** — invoked when the first two are inconclusive. Scores semantic equivalence against a rubric and is **required to abstain rather than guess**; abstentions count against confidence, not toward agreement.

**Privacy gate:** off by default; requires an explicit flag **and** per-workload consent in config; content passes through the redaction pipeline before transmission; the report records exactly what was sent, to which backend, and what it cost.

---

## 11. Cost model, attribution, and confidence

### 11.1 Baseline

Baseline spend = Σ over records of (tokens per class × catalog price at request timestamp × commercial adjustments). Reported decomposed by workload, model, token class, and status — with billed-but-wasted spend broken out as its own slice.

### 11.2 Sequential marginal attribution

Findings overlap: caching a prefix, downgrading the model, and batching the same requests all claim overlapping dollars, and naive summation is how these tools produce numbers nobody can reproduce on the next invoice.

Findings are applied in a fixed dependency order and each is credited only with its **marginal** savings against the already-adjusted baseline:

```
routing → caching → batching → waste
```

Routing first because a model swap changes the unit price every later saving is computed against; caching next because it changes the token mix batching operates on; waste last because it is independent of the rest and must not absorb credit that belongs to the others.

Both numbers are reported for every finding: **standalone** (what it would save alone) and **marginal** (what it adds given everything ranked above it). The portfolio total is the sum of marginals only, and the report says so explicitly next to the total.

### 11.3 Confidence tiers

Every finding carries a tier plus a **low / expected / high** dollar range. Report totals are given **per tier** so a heuristic never silently enters a board deck.

| Tier | Meaning | Example |
|---|---|---|
| **Measured** | Directly observed in the logs; arithmetic only | Billed failures, cache writes never read, retry waste |
| **Simulated** | Produced by the cache simulator or schedule model from real traffic, under stated assumptions | Prefix-cache savings with exact request overlap |
| **Estimated** | Model-based with material assumptions (approximated overlap, sampled replay, estimated usage) | Prefix savings under Poisson arrivals; validated downgrades |
| **Heuristic** | Informed judgment, no direct evidence | Difficulty-based routing candidates, paraphrase-level semantic caching |

Confidence penalties are additive and traceable: a finding built on `usage_estimated` records, an approximated overlap model, or an over-merged cluster carries each penalty and lists them.

### 11.4 Coverage gating and projection

Extrapolating a month of savings from a Tuesday afternoon is the classic credibility loss. Therefore:

- Savings are reported **over the observed window** as the primary number.
- A monthly projection is shown **separately and clearly labeled**, and is produced only when coverage checks pass: **≥7 days** of logs, complete-day boundaries, and detected weekly/daily seasonality accounted for.
- Traffic trend is estimated and stated (a workload growing 20%/month changes the projection materially).
- When coverage fails, the projection is withheld and replaced by an explicit warning naming what is missing.

---

## 12. Privacy

The tool runs on user infrastructure and may see prompt content. Becoming a server changes the threat model — there is now a listening port and an upload endpoint — without changing the posture, because the server is theirs.

**Layer 0 — the server is local and unauthenticated by design.**

- `serve` **binds `127.0.0.1` by default.** Binding to any other interface requires an explicit `--host`, and the app prints a warning naming what is being exposed.
- **There is no authentication in v1, and that is a deliberate scope decision, not an oversight.** A single-tenant local server with no accounts has nothing to authenticate; adding a homegrown login would create the illusion of a security boundary without the substance of one. Teams deploying it beyond one machine put it behind whatever they already use — an SSO reverse proxy, a VPN, an SSH tunnel. The docs say this plainly rather than shipping a password field.
- **The app makes no outbound calls of its own** — no CDN assets, no telemetry, no update check. Every asset is served from the package, which is also why the CSP can forbid external origins outright. The only outbound calls in the entire system remain the opt-in replay calls of §10.3.
- **Uploaded logs are treated as ingest input, not as stored files.** They pass through the same redaction and fingerprinting pipeline (layers 1–2) and the upload is discarded; the run keeps derived records only.
- **The API is CSRF-protected and path-confined.** State-changing endpoints require a same-origin token, and any server-side path the UI accepts (log locations, output directories) is resolved and confined to configured roots — a browser-reachable process that reads arbitrary filesystem paths is a file-disclosure bug regardless of who is on the other end. The same confinement governs remote reads: runs name a **configured connection**, never an arbitrary URI (§6.1).
- **The app stores no credentials.** Cloud connectors resolve identity from the host's own credential chain — instance profile, managed identity, or a named local profile — so the app holds a reference, not a secret, and has no secret input field to type one into. An unauthenticated server that also holds cloud keys is a much worse thing to leave running than one that does not.

The four content layers are unchanged:

1. **Never persist raw content.** Hashing and fingerprinting happen at ingest; raw text exists only in memory for the chunk being processed. The derived store contains no prompt text.
2. **Redact before persisting anything derived.** Configurable detectors for emails, keys/tokens, card numbers, national ids, and user-supplied patterns; redaction runs before any storage and before any replay transmission.
3. **Evidence samples are opt-in.** By default findings cite fingerprints, counts, and token statistics. Real prompt excerpts appear only when explicitly enabled.
4. **Encrypted local store with retention.** At-rest encryption for the derived database plus a TTL that purges ingested data after N days (default 30).

Any outbound call (replay, embedding, judging) is gated per §10.3 and fully itemized in the report.

---

## 13. Interfaces — web app, API, CLI

One engine (§5), three drivers. The rule that keeps them honest: **every operation the app can perform exists as a CLI command first**, and the app calls the same run engine the CLI calls. Nothing is reachable only through a browser.

### 13.1 Web app

```
llm-cost-auditor serve [--host 127.0.0.1] [--port 8787] [--workspace ./.llm-cost-auditor]
```

Starts the local server and prints the URL. Single tenant, no accounts (§12).

**Pages, and nothing more in v1:**

| Page | Contents |
|---|---|
| **Runs** | Every run in the store: status, window, source, baseline spend, portfolio savings, timestamp. Start a new run from here. |
| **Connections** | The saved log sources (§6.6): add or edit a connection, **test** it (credentials resolve, prefix lists, permissions exercised are named), and **preview** — the first N records decoded, with the detected format, provider, fidelity tier, and observed time range, before committing to a full run. No secret fields; credentials resolve from the host's own chain. |
| **New run** | Pick a connection (or upload files, or point at a local path within the configured roots), set the window, attach `workloads.yaml` / `architecture.yaml` / commercial terms if any, pre-flight validation — including an object count and byte estimate for the window, so a run nobody meant to start is visible before it starts — then submit. |
| **Run overview** | The executive section (§3) for one run: baseline spend decomposed by workload, model, token class and status; waste percentage; portfolio total with the "sum of marginals" statement next to it; totals broken out per confidence tier. |
| **Findings** | Sortable, filterable table — by analyzer, workload, confidence tier, risk, effort, realizability. Each row expands into the engineering detail: evidence, the exact change, verification steps, and both standalone and marginal savings. |
| **Coverage** | What was skipped and why (§11.4): objects that failed to read and their byte volume (§6.1), unpriced models, blocked analyzers and their instrumentation findings, fidelity tier per source, price-catalog staleness, cluster-quality warnings, whether the monthly projection was withheld. |
| **Workloads** | Discovered clusters with their four profile axes (§8.3), which findings each produced, and which are withheld pending a declared blast radius — with the withheld dollar ceiling shown, so editing config has visible value. |
| **Run detail** | Live progress while running (stage, records processed, elapsed), the structured log, and the downloadable artifacts. |

**Interaction rules:**

- **The app never invents a number the report does not contain.** Both render the same run record; a figure visible only in the browser is a bug.
- **A running run is watchable, not blocking.** The run page streams progress from `log.jsonl` (§5.4); navigating away does not affect the run.
- **Config edits create a new run** (§5.3), never mutate the current one, and the UI shows which run a comparison is against.
- **Every finding links to its evidence and its provenance** — the pricing resolution chain (§7.2), the confidence penalties applied (§11.3), and the assumptions the analyzer declared.

**Deliberately absent in v1:** accounts, roles, comments, saved views, dashboards over multiple runs, scheduling, and anything resembling a ticketing workflow.

### 13.2 HTTP API

The app is a client of a small JSON API; the same API is what a dashboard or a script would use if the CLI is the wrong shape for it.

```
POST   /api/runs                  start a run (config in body; returns run_id)
GET    /api/runs                  list runs
GET    /api/runs/{id}             run record: status, timings, totals
GET    /api/runs/{id}/events      progress event stream (SSE) while running
GET    /api/runs/{id}/findings    findings.json
GET    /api/runs/{id}/report      report.html
POST   /api/runs/{id}/cancel      cancel a queued or running run
GET    /api/runs/{id}/diff/{base} realized vs projected against a baseline run (§13.6)
POST   /api/config/validate       validate workloads/architecture/commercial config without running

GET    /api/connections           list saved connections (§6.6)
POST   /api/connections/{id}/test resolve credentials, list the prefix, report permissions exercised
POST   /api/connections/{id}/peek decode the first N objects: detected format, source, fidelity, time range
```

It is deliberately thin: the API exposes runs and their artifacts, not analyzer internals. The HTML pages are server-rendered rather than built on top of this API (§5.1) — the API exists for programmatic callers, so it does not have to grow an endpoint for every UI affordance.

### 13.3 CLI

```
llm-cost-auditor ingest   <uri...|connection-id> --source anthropic|openai|bedrock|vertex|foundry
                          # uri: ./logs/*.jsonl | file:///path | s3://bucket/prefix/ | az://container/prefix/
                          [--fidelity auto] [--format auto] [--compression auto]
                          [--window 2026-08-01..2026-08-31]   # prunes the object listing (§6.1)
                          [--profile <aws-profile> | --role-arn <arn>] [--account <azure-account>]
                          [--full]                            # ignore the manifest, re-read everything (§6.6)
llm-cost-auditor connections [list | test <id> | peek <id> [-n 100]]
llm-cost-auditor profile  [--emit-config workloads.yaml] [--emit-architecture architecture.yaml]
llm-cost-auditor audit    [--config workloads.yaml] [--architecture architecture.yaml]
                          [--window 2026-08-01..2026-08-31]
                          [--replay --replay-backend openrouter|bedrock|foundry|vertex
                           --replay-budget-usd 50 | --replay-budget-pct 0.5 --dry-run]
                          [--out report.html --json findings.json --snapshot snapshot.json]
                          [--explain-pricing <request_id>]   # print list → effective rate resolution chain
llm-cost-auditor verify   --baseline snapshot.json | <run_id>   # realized vs projected
llm-cost-auditor refresh-prices

llm-cost-auditor serve    [--host 127.0.0.1] [--port 8787] [--workspace ./.llm-cost-auditor]
llm-cost-auditor runs     [list | show <run_id> | rm <run_id>]   # the run store (§5.3)
```

`ingest`, `profile` and `audit` write into the run store like any other run, so work started at a terminal is visible in the app and vice versa. `--out` / `--json` / `--snapshot` additionally copy artifacts to a chosen path, for pipelines that want the file where they want it.

### 13.4 Finding schema

```json
{
  "id": "cache.prefix.workload-7",
  "title": "Cache the 4.2k-token system prompt + tool definitions for claims-extraction",
  "analyzer": "cache.prefix",
  "workloads": ["claims-extraction"],
  "confidence": "Simulated",
  "evidence": { "requests": 18422, "window_days": 30, "simulated_hit_rate": 0.87,
                "current_hit_rate": 0.0, "method": "exact_overlap",
                "assumptions": ["5m TTL", "single region"] },
  "savings": { "standalone": {"low": 1180, "expected": 1420, "high": 1610},
               "marginal":   {"low": 1180, "expected": 1420, "high": 1610},
               "currency": "USD", "basis": "observed_window",
               "projection_monthly": 1420, "realizable": true },
  "risk": "low",
  "effort": "S",
  "remediation": { "summary": "...", "snippet": "...", "files_hint": [] },
  "verification": ["Re-run audit after deploy", "Expect cache_read_tokens > 0 within 1h"]
}
```

### 13.5 Instrumentation findings

When an analyzer is `BLOCKED`, the gap becomes a finding rather than silence:

> *"Prefix-cache analysis is blocked: your OpenAI logs lack per-segment hashes. Based on token-volume patterns in this workload, prefix caching could plausibly address **$3–9k/month**. To unblock, emit segment hashes using the sidecar snippet below — no prompt text is stored."*

The dollar ceiling is explicitly bounded and tier-**Heuristic**; it exists to justify the instrumentation work, and the report says so.

### 13.6 Verification loop

Each `audit` writes a signed **snapshot**: traffic mix, unit costs, workload profiles, catalog version, and every finding. `verify --baseline` diffs a later run against it and reports **realized vs projected** savings per finding, **normalized for volume change** so that traffic growth cannot mask a win (or manufacture one). Findings are marked `implemented` / `partially implemented` / `not detected` based on observable signals (cache-read tokens appearing, batch flags appearing, model mix shifting).

---

## 14. Validation strategy

No external oracle exists for "you would have saved $X", so correctness is established four ways.

1. **Synthetic log generator with known ground truth.** Generate traffic with injected, quantified inefficiencies — a known-cacheable prefix at a known reuse rate, a retry storm of known size, a latency-tolerant workload of known volume — and assert the auditor recovers the planted savings within tolerance. This is the core correctness harness and gates every release. It also generates the adversarial cases: bursty arrivals, TTL-boundary reuse, sub-threshold prefixes, prefix churn.
2. **Golden fixtures per provider.** Small anonymized real-shaped log samples per source with checked-in expected outputs, so provider schema drift and price-catalog changes break loudly rather than silently shifting dollar figures.
3. **Property-based simulator tests.** Invariants that must never be violated: savings ≤ baseline spend; hit rate monotonic non-decreasing in TTL; write premium always paid before any read benefit; marginal attributions sum exactly to the portfolio total; sub-threshold prefixes never produce savings; simulated savings ≤ theoretical maximum (all reads free).
4. **Invoice cross-check.** When a provider bill is supplied, computed baseline spend must match within **2%**; a larger gap is treated as a bug in pricing or ingest, not a rounding note. This is also how declared discounts are validated.

---

## 15. Known tensions (stated, not hidden)

1. **In-memory scale vs. analysis depth.** The v1 target (~500k requests, in-memory) coexists with a ≥7-day coverage requirement, token tries, and MinHash/LSH. High-volume users will exceed it. Mitigation: spend-weighted sampling with stated sampling error for the fast pass, exact analysis on top-N workloads, and the `Store` seam ready for a DuckDB backend.
2. **Per-request dynamic routing on sampled evidence.** The strongest routing shape rests on the weakest evidence base. Mitigation: capped at the **Estimated** tier, always presented alongside the static-downgrade alternative, and never recommended for `high`/`regulated` workloads without replay validation.
3. **Semantic caching is a correctness risk sold as savings.** Mitigation: MinHash default (near-duplicate, not paraphrase), embeddings opt-in with an explicit threshold and estimated false-hit rate, suppressed entirely above `medium` blast radius.
4. **Template fingerprinting drives everything and can be wrong.** Over-merging inflates cache findings; over-splitting hides them. Mitigation: cluster-quality metrics in the report, user override via config, and a warning when intra-cluster variance is extreme.
5. **Committed spend can make every finding worth $0.** Mitigation: realizable-vs-gross reported separately, always.
6. **A UI makes numbers look more certain than they are.** A dollar figure in a styled dashboard reads as fact in a way the same figure in a terminal does not, and confidence tiers are exactly what users skim past. Mitigation: the tier and the low–high range are part of every savings figure's presentation, not a column users can hide; the portfolio total never appears without the "sum of marginals" statement next to it; withheld projections show the warning in place of the number rather than omitting the panel.
7. **Two surfaces can drift into two truths.** Mitigation: the app and the exported report render the same run record through the same template layer, and a figure computed in a view rather than by the engine is treated as a defect (§13.1).
8. **The tool inherits whatever permissions its host has.** Reading logs from S3 or Azure Blob means running somewhere with credentials, next to an unauthenticated local server. Mitigation: credentials are never stored by the app (§12), only configured connections are readable (§6.1), read-only permissions are what the docs ask for, and the connection test names the permissions it actually exercised so over-broad grants are visible.
9. **Incomplete log delivery looks exactly like less traffic.** Object storage is where logs go to be silently incomplete — a delivery lag, a lifecycle rule that expired last month's keys, a prefix nobody granted access to. The tool cannot tell "no requests" from "no logs". Mitigation: read failures are coverage failures with named objects and byte volumes, gaps in the observed timeline are reported against the requested window, and projections are gated on both — but a clean-looking run over quietly truncated data remains the residual risk, which is why the manifest names every object read.
10. **A long-lived server process contradicts the in-memory design.** A run holds a whole dataset in memory; a server that accepts concurrent runs will be OOM-killed on the laptop this is meant to run on. Mitigation: concurrency 1 by default (§5.4), and the run store — not process memory — is what outlives a run.

---

## 16. Open questions

- Reference fingerprint-sidecar: shipped as a Python package only, or also TS/Go, given Tier B is the recommended posture?
- Do we support a warehouse-pushdown execution mode (BigQuery/Snowflake) before or after the DuckDB backend?
- Cascade findings require an escalation *signal* to exist; do we recommend one, or only surface cascades where a validator is already present in the logs?
- Licensing and distribution model for the price catalog updates.
- Does the app ever need to write config back to the user's repo, or only offer generated config for download? Writing files a browser session chose the contents of is a meaningfully larger trust ask.
- Is a packaged container image part of the v1 deliverable, or is `pip install` + `serve` enough for the first users?
- Do the cloud log **query** APIs (CloudWatch Logs, Log Analytics, Cloud Logging) ever need to be connectors, or is "export to a bucket" always available in practice? The bet in §6.1 is that it is; the first user who cannot export settles it.
- Gateway logs (LiteLLM, OpenRouter, Helicone) are a post-v1 *source* adapter, but several of them expose a database or an API rather than files. Does that make them a source, a connector, or both?
- The shared-deployment case (a team pointing one instance at a shared log export) is out of scope for v1 but keeps being asked for. What is the smallest thing that would make it defensible — a reverse-proxy deployment guide, or actual identity in the product?
