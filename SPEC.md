# LLM Cost & Routing Auditor — Specification

**Status:** Draft v1.2 · **Date:** 2026-09-04 · **Repo:** `llm-cost-auditor`

*Changed in v1.1: the auditor is a self-hosted platform (local web app + CLI over one run engine), not an offline CLI — §1, §3, §4, §5.3–5.4, §12, §13. Log sources are pluggable connectors (local files, S3, Azure Blob) separate from source adapters, with a credential store behind them — §6.1, §6.6, §6.7.*

*Changed in v1.2 (pre-implementation review): one run = one directory = three resumable stages, with `--run` replacing implicit state (§5.3, §13.3). Money is integer micro-USD end to end (§6.4). `Store` split into `RecordStore` and `RunStore` (§5.1). Analyzer applicability is per-slice, and findings name their slices (§5.2, §13.4). Ingest is bulk in v1; incremental deferred to v1.1 (§6.6). Encryption is scoped to credentials only — the derived store is no longer encrypted (§5.3, §12). Attribution runs waste first (§11.2). Reads are confined to a terminal-only source scope (§6.1, §13.2), and the bind interlock is re-checked on every credential write (§6.7). Adds explicit rules for timezone (§6.6), currency (§7.1), unknown cache TTL class (§6.2), retry-vs-duplicate-delivery classification (§6.5), baseline coverage gating (§6.1, §11.4), Tier A/B cache stage split (§9.2), price-increase exposure (§11.5), upload staging (§12), and the exact-vs-declared-bound test rule (§14). **The credential store is deferred to v1.1** — v1 uses ambient cloud identity only, so nothing is encrypted and no authentication is needed; its design and seven unresolved security problems move to [docs/design/credential-store.md](docs/design/credential-store.md) (§2, §4, §5.1, §6.1, §6.7, §12, §13, §15).*

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
- **Multi-tenancy, accounts, and a hosted service.** The platform is single-tenant and runs where the user puts it. No sign-up, no org/user management, no tenant isolation, no billing. There is **no authentication mechanism at all** in v1, which is coherent because the server holds nothing secret: it borrows the host's ambient cloud identity and stores no credential of its own (§6.7).
- **Collaboration features.** No comments, assignments, notifications, or finding-triage workflow in v1. Findings are exported (`findings.json`, report HTML) into whatever tracker the team already uses.

### Deferred, not excluded (roadmap)

- Fine-tuning and distillation economics ("train a small model to replace the frontier one") — often the largest lever, but requires quality modeling well beyond v1.
- Prompt-quality advice beyond mechanical redundancy detection.

---

## 3. Audience and deliverable

One set of findings, two audiences, three surfaces. The **content** below is fixed; the surfaces differ only in how it is navigated.

- **Executive section** — total spend over the observed window, waste percentage, top 5 findings with dollars and risk rating, portfolio total after overlap de-duplication, and a **price-increase exposure** panel (§11.5): the workloads and findings whose cost grows most if provider rates rise, which is where remediation has hedging value beyond its own savings.
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
| **v1** | CLI over the run engine; run store on disk (§5.3); connectors for local files, S3, and Azure Blob (§6.1) using ambient cloud identity only (§6.7); web app covering the core loop — connect a log source, run, browse findings, view coverage, download the report |
| **v1.1** | GCS connector; the credential store and the access token it requires (§6.7, [design note](docs/design/credential-store.md)); incremental re-ingest via a decoded-record cache (§6.6); in-app config editing with validation and re-run; run comparison (`verify` diff) in the UI |
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
- **No cryptography, and no crypto dependency, in v1.** Nothing in the system is encrypted: the run store holds no secrets (§5.3) and credentials are never stored (§6.7). `keyring` and `pynacl` arrive with the v1.1 credential store and with nothing else.
- **Cloud SDKs are optional extras, one per connector** (`boto3` for `s3`, `azure-storage-blob` + `azure-identity` for `az`). A local-files audit must not pull two clouds' SDKs, and a missing extra produces "install `llm-cost-auditor[s3]`", not an import error.
- **Web app: `fastapi` + `uvicorn`, server-rendered `jinja2`, HTMX for interactivity.** No JavaScript build step, no second language, no SPA. The same template layer renders both the app's pages and the exported static report, so the two cannot drift. HTMX covers the interactions v1 actually needs — polling a running job, filtering and sorting a findings table, expanding evidence, submitting config — and a page that genuinely outgrows it is the signal to reconsider, not a reason to start with React. Charts are server-rendered inline SVG for the same reason.
- **In-memory processing** targeting up to ~500k requests per run on a laptop. Storage sits behind **two distinct seams**, because they are two different abstractions and naming them once produces an interface that fits neither:

  | Seam | Responsibility | v1 backend | Later |
  |---|---|---|---|
  | **`RecordStore`** | The normalized records of one run: `write_records()`, `iter_records(filter)`, `count()`, `drop()` | Parquet + polars, in memory | DuckDB |
  | **`RunStore`** | Runs as durable objects: `create()`, `get()`, `list()`, `set_status()`, `append_event()`, `write_artifact()`, `delete()` | A filesystem directory per run (§5.3) | Unchanged; it is already the right shape |

  Analyzers see only `RecordStore`. The engine and the drivers see only `RunStore`. Nothing sees both except the run executor.
- **Rust-portability discipline.** The three hot paths — prefix trie construction, MinHash/LSH, and the discrete-event cache simulator — live behind narrow, pure interfaces with no framework coupling, so each can be replaced by a Rust extension (PyO3) independently if profiling demands it. Everything else stays Python.

### 5.2 Plugin analyzer interface

```python
class Analyzer(Protocol):
    id: str                        # "cache.prefix", "waste.retry_storm"
    required_fidelity: Fidelity    # BILLING_ONLY | HASHED | CONTENT
    required_fields: set[str]      # e.g. {"start_time", "latency_ms"}

    def applicable(self, slice: SliceProfile) -> Applicability: ...
        # -> RUN | DEGRADED(reason, confidence_penalty) | BLOCKED(missing, ceiling_estimate)

    def analyze(self, ctx: AnalysisContext) -> list[Finding]: ...
        # ctx.slice is the slice `applicable` approved; ctx.records are its records only
```

**Applicability is per-slice, never per-dataset.** A dataset is routinely mixed: Anthropic logs at Tier A alongside Bedrock billing exports at Tier C (§6.3), and a run may name several connections (§6.6). A single dataset-wide verdict has no correct answer — return `RUN` and the analyzer silently produces numbers over data that cannot support them; return `BLOCKED` and one weak source suppresses findings for strong ones.

A **slice** is therefore the unit of applicability: `(connection_id, source, fidelity_tier)`, with the workload partition available inside it. The runner evaluates every analyzer against every slice and records the verdict per slice, so:

- an analyzer runs on the slices that support it and is blocked on the ones that do not, in the same run;
- **every finding names the slices it covers** and the fidelity tier that produced it, so no finding can be read as covering traffic it never saw;
- coverage (§13.1) reports the full analyzer × slice verdict matrix, which is what makes "why is there no cache finding for Bedrock" answerable.

`BLOCKED` does not mean silence: the runner converts it into an **instrumentation finding** (§13.5), scoped to the slice that was blocked. Log adapters implement a parallel `SourceAdapter` protocol, so new providers are additive.

### 5.3 Runs and the run store

Making this a platform rather than a command means one new durable concept: **the run**. Everything the app shows is a view of a run, and nothing about a run depends on the process that started it still being alive.

A run is a directory under the workspace root (`./.llm-cost-auditor/runs/<run_id>/` by default, configurable):

```
runs/<run_id>/
  run.json          status, stage, timings, resolved config, catalog version, connection ids, error (if any)
  manifest.json     every object consumed: uri, etag, size, bytes read, records parsed (§6.6)
  records.parquet   normalized RequestRecords for this run (derived, redacted — never raw prompt text)
  findings.json     the finding set (§13.4)
  snapshot.json     the verification baseline (§13.6)
  report.html       exported report
  log.jsonl         structured progress events, appended as the run executes
```

**One run, one directory, three stages.** A run advances through `ingest → profile → audit` in that order, and the stage it reached is recorded in `run.json`. The CLI can stop after any stage (`--stop-after ingest`) and resume the remainder against the same run id (`audit --run <id>`); the app always submits all three. This is the single mechanism behind both surfaces — there is no separate "ingest artifact" a later command hunts for, and no implicit "most recent ingest" state anywhere in the system. A stage may only be run once per run: re-running a stage means a new run with a `parent_run_id`.

**Rules:**

- **Runs are immutable once complete.** Re-running with edited config produces a *new* run that records its `parent_run_id`, which is what makes run-to-run comparison (§13.6) honest — there is no in-place mutation to lose.
- **A run's records are self-contained.** `records.parquet` holds every record in the run's window, from every connection it named. No run depends on another run's data, and no analyzer ever sees a partial window because an earlier run happened to read the same objects (v1 ingests in bulk — §6.6).
- **The store is a filesystem directory, not a database.** It is inspectable, diffable, copyable to a colleague, and deletable with `rm -rf`. This is the `RunStore` seam of §5.1, and it stays a directory; `RecordStore` is the seam DuckDB replaces when scale demands it.
- **Nothing in the run directory is encrypted.** Inspectability is the point, and the content protections that make that safe are upstream: raw prompt text is never written (§12 layer 1) and derived records are redacted before they are stored (§12 layer 2). Nothing else in v1 is encrypted either (§6.7) — the first sealed format in this project arrives with the v1.1 credential store, and reviewing it will mean reviewing one thing.
- **The run record is the only contract between the engine and the app.** The app reads `run.json`, `findings.json`, and `log.jsonl`; it never reaches into analyzer internals. A run produced by the CLI on a build server renders identically in the app.
- **Retention applies to runs** (§12): the TTL that purges ingested data purges the run's `records.parquet` while leaving its findings and report, so an old audit stays readable after its underlying data expires. `run.json` records that the purge happened, because `--explain-pricing` and any tier-A re-analysis stop working at that point (§13.6).

### 5.4 Job execution

An audit takes minutes, not milliseconds, so the app cannot run one inside a request handler.

- **One background worker in the server process**, executing runs from a queue with a configurable concurrency of 1 by default. A run holds a whole dataset in memory (§5.1); running two concurrently on a laptop is how the tool gets OOM-killed.
- **Progress is events, not polling into the engine.** The engine appends structured events to `log.jsonl` (`stage`, `pct`, `message`, `counts`); the app tails that file. The CLI renders the same events as a progress bar. One producer, two renderers.
- **A crashed or killed server leaves a run marked `running` with a stale heartbeat.** On startup the server marks such runs `interrupted` rather than resuming them — a half-analyzed dataset must never produce a report. The one resumable state is `queued`: a run that never started executing has no partial data, so it is re-queued rather than failed. **Work that has begun is never resumed**, and any later condition that can block a run mid-flight follows the same rule: fail it, do not pause it.
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
- **Prune the listing with the window, not the parser.** Connections declare a partition template (`y=%Y/m=%m/d=%d/`); an audit window is expanded into the key prefixes it can possibly touch, so a one-week audit against three years of logs lists a week of keys. Where no template applies, `last_modified` filters the listing, and records outside the window are still dropped after decode — log files do not align with audit windows. Prefix expansion is deliberately **inclusive at the edges** (the partitions either side of the boundary are always listed), because a partition written in one zone and a window expressed in another otherwise silently loses a day. The report states the requested window, the observed one, and the timezone both are expressed in (§6.6).
- **Ambient credentials, always.** A connection resolves identity through its cloud's own default chain — environment, instance profile / IRSA / managed identity, or a named local profile — and stores only a *reference* (profile name, role ARN, account and container). A credential the tool never holds cannot leak from it, and in v1 the tool never holds one (§6.7). Read-only permissions are what the docs ask for, and the connection test names the permissions it actually exercised.
- **Every read is confined to a terminal-configured source scope.** A run names a connection, never an arbitrary URI — but that alone is not a boundary if a browser session can create connections. So the boundary is one level up: the **source scope** is a list of permitted roots (filesystem prefixes, `bucket/prefix`, `account/container`) that lives in the workspace config and is editable **only from the terminal**, never through the API or the UI. Connections may be created and edited in the app for usability, and every one of them is validated against the scope on save *and* again on use. A server with an instance profile and no scope configured can read nothing remote at all. Without this, an unauthenticated local server is a credential-borrowing exfiltration primitive: anything reachable in the browser could point it at any bucket the host can reach. Path confinement for `file://` (§12) is the same rule wearing different clothes, and is expressed in the same list.
- **A partial read is a failure, not less data.** A truncated gzip member, a permission-denied key, a listing that timed out mid-page: each marks the run's coverage incomplete, naming the skipped objects and their byte volume. Gating is two-level and applies to the **baseline**, not only to the projection (§11.4):

  | Missing share of listed bytes | Consequence |
  |---|---|
  | Any at all (> 0) | Baseline is labeled a **lower bound**, the missing objects and byte volume are named in coverage, and the monthly projection is withheld |
  | Above `coverage.max_missing_pct` (default 2%) | The run is marked `incomplete`: savings figures are **withheld entirely** and replaced by the coverage failure |

  Baseline spend computed over 87% of the logs is not a smaller number — it is a wrong one, and reporting it in bold with a footnote is how it gets quoted anyway.
- **Every object consumed is recorded** (§6.6), which is what makes a run reproducible and double-counting detectable rather than accidental.

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
| **Anthropic** | Exposes `cache_creation_input_tokens` / `cache_read_input_tokens`, enabling direct measurement of realized cache savings and cache-efficiency critique. Where the usage block breaks cache creation down per TTL class, the write premium is priced exactly; where it reports a single undifferentiated total, the TTL class is **unknown** and handled per the rule below. |
| **OpenAI** | Automatic prompt caching with no explicit breakpoints; `cached_tokens` is reported but not user-controlled, so findings shift from "add cache_control" to "restructure prompt so the automatic cache can engage". |
| **Bedrock / Vertex / Foundry** | Cloud-broker billing across AWS Bedrock, Google Vertex AI, and Azure AI Foundry. Each has its own price sheet (broker prices diverge from first-party list prices and from each other), its own log shape (CloudWatch Logs / Cloud Logging / Azure Monitor diagnostic settings), and its own reserved-capacity construct — Bedrock Provisioned Throughput model units, Vertex PTUs, Foundry PTUs — all of which mark traffic as already-paid-for (§7.2). Caches are scoped per deployment/region on all three, which the simulator must partition on (§9.2). |

**Unknown cache-write TTL class.** Cache-write premiums differ materially between TTL classes, so a log that reports cache-creation tokens without saying which class they were is a pricing ambiguity, not a detail. The rule, because these tokens *were* billed and cannot simply be dropped:

- the record is priced at the **lowest-premium class** the model offers, and flagged `ttl_class_unknown=true` — so the baseline understates rather than inflates;
- the report states the **exposure range** (the same tokens priced at the highest-premium class) in the coverage panel, rather than presenting the point estimate as fact;
- any finding whose math turns on the TTL class — TTL-expiry misses, the 1-hour upgrade trade (§9.3) — is capped at **Estimated** and carries a named confidence penalty;
- it becomes an instrumentation finding (§13.5): emitting the per-class breakdown is a log change, not a code change.

Gateway logs (LiteLLM, OpenRouter, Helicone) are a post-v1 adapter that maps onto the same canonical record.

### 6.3 Fidelity tiers

Every dataset is classified, per-source, into the highest tier it supports. Mixed-tier datasets are supported; findings are tagged with the tier that produced them.

| Tier | Contains | Unlocks |
|---|---|---|
| **A — Content** | Full request/response bodies: system prompt, tools, messages, completion | Everything: token-exact prefix tries, response/semantic caching, redundant-context detection |
| **B — Hashed** | No raw text. Per-segment hashes (system prompt, each tool definition, each message) **each with its own token count**, plus rolling 1k-token prefix hashes, params | Prefix caching *at segment granularity* — which is where breakpoints can be placed anyway — exact-match response caching, conversation replay waste, all waste findings. No sub-segment breakpoint optimization, no semantic caching, no content-based redundancy detection |
| **C — Billing** | Timestamps, model, token counts, latency, request id, status | Waste findings (retries, truncation, 429s), spend decomposition, latency-tolerance profiling, batching eligibility. No cache analysis |

**Design consequence:** Tier B is the recommended posture and the tool ships a reference "fingerprint sidecar" spec — a small library/logging snippet users add to their client so their logs become Tier B without ever storing prompt text. The sidecar emits a **per-segment token count alongside each segment hash**, which is what makes exact cacheable-token math possible without content: the rolling 1k-token prefix hashes exist to detect churn and shared prefixes, and are far too coarse to decide a minimum-cacheable-threshold question. Sub-segment breakpoint placement is the one cache capability that genuinely requires Tier A (§9.2).

### 6.4 Canonical record

```
RequestRecord
  request_id, parent_request_id (retries/streams), attempt_index, source, provider, model, model_version
  connection_id, object_uri                      # provenance: which slice, which object (§5.2, §6.6)
  start_time, end_time | latency_ms              # timezone-aware, stored UTC (§6.6)
  status: ok | error_billed | error_unbilled | cancelled | truncated
  stop_reason, http_status, error_code
  params: temperature, top_p, max_tokens, tools[], tool_choice, thinking/reasoning_effort, seed
  usage: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens(+ttl class),
         reasoning_tokens, image_tokens, audio_tokens, video_tokens, embedding_tokens
  flags: usage_estimated, ttl_class_unknown, duplicate_delivery  # each drives a confidence penalty
  content_ref: segments[] (hash, token_count, role, kind, volatility_flag) | raw (tier A, in-memory only)
  labels: api_key_id, project, tags{}, user_id, session_id, endpoint
  batch_flag, region/deployment_id
```

**Money is integer micro-USD, end to end.** Every monetary quantity in the system — a catalog rate, a per-record cost, a finding's savings, a portfolio total — is an integer count of millionths of a dollar (`int`, `μUSD`), from the price table through attribution to the JSON on the wire. Binary floats never touch a dollar figure at any point.

- **In the record and in `records.parquet`:** integer columns. Never `f64`, which is what polars will pick by default if nobody says otherwise.
- **In the catalog:** rates are parsed from decimal strings into `Decimal` at load and converted to μUSD once, at the boundary. `Decimal` is the *parsing* type; μUSD is the *arithmetic* type.
- **On the wire:** `findings.json` carries integer μUSD fields (`savings_usd_micros`), not JSON numbers with decimal points — JSON has no decimal type, and a float round-trip silently defeats the exact-equality fixture rule (AGENTS.md).
- **Formatting to dollars happens once**, in the presentation layer, and is never read back.
- **Rounding is stated where it is unavoidable** (a per-token rate times a token count is exact in μUSD; a percentage discount is not): round half-even at the point of application, and the property test in §14.3 asserts that per-finding marginals sum to the portfolio total *exactly*, which is only achievable in integers.

### 6.5 Normalization edge cases (all mandatory in v1)

Each of these silently corrupts cost math if ignored.

1. **Retries and duplicate records.** This is the single largest source of double-counted spend in the whole pipeline, so the rule is explicit rather than left to per-provider judgment.

   Two records sharing a `request_id` are one of two very different things, and request id alone cannot tell them apart — some clients reuse an idempotency key across attempts, and at-least-once log delivery re-emits the same event. The distinguisher is **whether the billing facts are identical**:

   | Condition on two records sharing a `request_id` | Classification | Billing |
   |---|---|---|
   | Identical `start_time`, `status`, and every `usage` field | **Duplicate delivery** — the same event logged twice | Collapse to one record, flag `duplicate_delivery` |
   | Any billing fact differs (`start_time`, usage, status, `http_status`) | **Genuine retry** — separate attempts | Keep both, assign `attempt_index`, link via `parent_request_id` |

   Adapters that carry a provider-side unique event id (most do) use it directly and skip the heuristic; the table is the fallback, and which path was taken is recorded per source in the coverage panel. Where an adapter can supply neither, the records are kept separate — **over-counting a retry is visible in reconciliation against the invoice (§14.4); silently collapsing two billed attempts is not.**

   Billing then depends on *why* the retry happened: a 429-rejected attempt costs nothing, while a 500 after generation may have been billed in full. These classification rules are per-provider and asserted in golden fixtures (AGENTS.md).

   The same-request-id-from-two-objects case is an ingest concern with its own rule (§6.6): it is a collapse when the manifest observed the same object change, and a double-count warning when it did not.
2. **Failed / truncated / cancelled requests.** Input tokens are frequently billed despite an error; `stop_reason=max_tokens` truncations often mean the response was unusable and re-requested; client-cancelled streams still bill generated tokens. These form their own finding class (§9.1), not just an ingest concern.
3. **Streaming reassembly.** Collapse SSE chunk logs into one record with final usage. When the terminal usage event is missing, estimate tokens from reassembled content (Tier A) or from chunk counts (Tier B/C) and mark the record `usage_estimated=true`, which propagates a confidence penalty to any finding relying on it.
4. **Multimodal and non-chat endpoints.** Image/audio/video token accounting per provider formula, plus embeddings, rerank, and legacy completions — each with its own pricing unit. Unknown endpoints are counted in baseline spend but excluded from analyzers, and reported in the coverage panel.

### 6.6 Connections and the ingest manifest

A **connection** is a named, saved binding of a connector to a location and a source — the thing a user configures once and then audits against repeatedly. It is config, not state, and it holds no secret material — only a reference to an ambient identity (§6.7).

```yaml
connections:
  - id: prod-bedrock-s3
    connector: s3
    uri: s3://acme-llm-logs/bedrock/invocation-logs/
    region: us-east-1
    auth: { profile: llm-audit-readonly }   # ambient chain (§6.7); config never holds a secret
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

**A run may name several connections.** Auditing Bedrock traffic in S3 alongside first-party Anthropic traffic on disk is one run over two connections, not two runs someone adds up by hand — cross-provider baseline spend is the interesting number. Each connection contributes a *slice* (§5.2), analyzers are applied per slice, and every finding names the slices it covers. `run.json` records the connection ids; the New run page (§13.1) accepts more than one.

**Ingest is bulk in v1.** Every run reads the full window it was given, from every connection it names, every time. This is slower on a repeat audit and it is the right v1 default: a run whose records depend on what a *previous* run happened to read is not self-contained (§5.3), its baseline silently covers less than it claims when a window shifts or a retention TTL fires, and the failure is invisible in the output. Incremental re-ingest returns in **v1.1** as a cache of *decoded records* keyed by `(uri, etag)` in a workspace-level store — so the run still materializes its whole window and only the parsing is skipped — not as "skip the object."

**The ingest manifest.** Every run records the exact objects it consumed — uri, etag, size, byte count read, records parsed, records rejected — in its run record (§5.3):

- **A run is reproducible.** The manifest names exactly what was read, so a disputed number can be traced back to the objects that produced it — the first question anyone asks when a savings figure looks wrong.
- **Mutable objects are handled explicitly.** A still-being-appended `today.jsonl` read once per run poses no problem within a run; across runs, an object whose etag changed between runs is noted so a re-audit's differing total has a stated cause.
- **Double counting is detectable, not accidental.** Two connections whose prefixes overlap, or an export re-uploaded under a new key, show up as the same request ids arriving from **different uris**. That is reported as an ingest warning naming both objects, rather than quietly inflating baseline spend. This is the one signal that distinguishes it from a genuine retry (§6.5.1), which is why `object_uri` is on the record.
- **Coverage is computed from the manifest**, not from what succeeded: listed bytes versus read bytes is what drives the §6.1 gating table.

**Time is explicit, everywhere.** A window with no timezone is a day-boundary bug waiting to be found by whoever quotes the number.

- The workspace declares a `timezone` (IANA name, default `UTC`). Audit windows, "complete-day boundaries" (§11.4), seasonality detection, and every date in the report are expressed in it.
- Records are stored UTC and converted for display and bucketing — never the reverse.
- Object partition templates are expanded in the **storage's own zone** (UTC for every v1 connector), which is why prefix expansion is inclusive at the edges (§6.1).
- The report states the window, the timezone, and the observed data range together. A run whose observed range does not reach its requested window boundaries says so — that gap is §15.9's failure mode, and it is only visible if the timezone is pinned.

### 6.7 Credentials (v1: ambient only)

**v1 stores no credentials.** Every connection resolves identity through its cloud's own default chain, and the config holds a reference to that identity — never secret material:

| Connector | v1 identity sources |
|---|---|
| **s3** | Environment (`AWS_*`), instance profile / IRSA / ECS task role, a named profile from `~/.aws/credentials`, or a role ARN assumed from any of those |
| **az** | `DefaultAzureCredential` — environment, managed identity, Azure CLI login — or `AZURE_STORAGE_SAS_TOKEN` from the environment |
| **file** | None; the OS decides what the process can read, bounded by the source scope (§6.1) |

This covers every deployed case, and it covers the laptop case through the same mechanism the cloud CLIs already use: a handed-over read-only key goes in `~/.aws/credentials` under a named profile, and the connection names the profile. Same key, same disk, same `0600` file permissions.

**A stored credential store is v1.1**, with the design and its unresolved problems in [docs/design/credential-store.md](docs/design/credential-store.md). It was cut from v1 deliberately: an adversarial review found that most of its remaining work is in whether its *stated protections actually hold* — a config file that steers a key with nothing authenticating it, rollback detection that cannot detect rollback, and an idle re-lock with no way back — rather than in the plumbing. This is the one part of the system whose failure mode is not a wrong number, so it lands against a working tool with those problems solved first, not alongside them.

Two consequences that hold for all of v1, and are why §12's posture stays coherent:

- **The server holds no secrets, so it needs no authentication.** There is no bind interlock, no access token, and no `Credentials` page — §2's no-accounts stance is simply true rather than propped up by a control. Ambient identity means the process's permissions are the host's permissions, which the operator already granted deliberately.
- **Secret handling rules still apply**, because a resolved ambient credential is a secret in memory like any other. Credential objects mask `__str__` and `__repr__`; nothing about a value reaches `run.json`, `manifest.json`, `log.jsonl`, an event stream, or a report; the run record names the *identity* used (profile name, role ARN, managed identity client id), never a value. Every resolution emits a `credential_used` event naming the connection and the operation, so "what did this run do with my permissions" has an answer.

**The one real gap is Azure**, stated rather than papered over: there is no clean ambient-file equivalent to a named AWS profile for a container-scoped SAS token, so a v1 user with only a SAS supplies it via `AZURE_STORAGE_SAS_TOKEN` and re-exports it when it expires. That friction is the strongest argument for v1.1, and it is a friction rather than a blocker.

**Config shape** — identity by reference, in a file that is safe to commit:

```yaml
connections:
  - id: prod-bedrock-s3
    connector: s3
    uri: s3://acme-llm-logs/bedrock/
    region: us-east-1
    auth: { profile: llm-audit-readonly }               # or: { role_arn: arn:aws:iam::...:role/... }

  - id: foundry-diagnostics
    connector: az
    uri: az://insights-logs-requestresponse/
    account: acmellmlogs
    auth: { credential: default }                        # DefaultAzureCredential chain
```

Read-only permissions are what every setup instruction asks for, and `connections test` names the permissions it actually exercised.

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
  currency: USD                  # v1: the catalog is USD-only; see below
  units: per_mtok
  input: <price>                 # decimal string, parsed to Decimal, stored as μUSD (§6.4)
  output: <price>
  cache_write_5m_multiplier: <multiplier>
  cache_write_1h_multiplier: <multiplier>
  cache_read_multiplier: <multiplier>
  batch_multiplier: <multiplier>
  min_cacheable_tokens: <provider/model specific>
  max_breakpoints: <provider/model specific>
  last_verified: 2026-09-04
  source_url: https://...
```

**Currency is USD-only in v1, stated rather than assumed.** Every row carries `currency: USD`, the commercial overlay must declare the same, and a mismatch is a **config error** — not a conversion. No exchange rate is applied anywhere in the system, because a rate that moves between the audit and the invoice would put a moving number underneath every finding and there is no honest way to pin it. Brokers that bill an account in another currency are a v1.1 question (§16), and the error message says so rather than silently pricing euros as dollars.

**Staleness is surfaced, never silent.** Every report states the catalog version and the oldest `last_verified` date among the rows actually used. Rows older than a configurable threshold (default 90 days) raise a warning on the affected findings; rows with no matching entry for a model produce a *missing price* coverage entry rather than a guessed number — an unpriced model is excluded from savings math and reported as such.

**Loaded on demand, not per session.** The table is not parsed at import or CLI startup. The pricing module lazily loads and memoizes only the rows it is asked for — keyed by `(provider, model, timestamp)` — on first lookup, so a run that touches four models never reads the rest of the catalog. A normal `audit` run never fetches anything.

**Refresh reports drift; it never adopts it.** `prices check` fetches a public pricing feed, derives the same figures our rows hold, and prints where the two disagree, exiting non-zero so a maintenance job can gate on it. It does not write a rate. Auto-adopting a feed would stamp `last_verified` fresh — asserting that a human had checked the provider's page when none had — and a single upstream typo would reprice every finding in every report at once, confidently and invisibly. A human resolves each disagreement against the row's own `source_url` and edits the catalog by hand.

Seeding a row works the same way: transcribe only what more than one independent feed agrees on, and treat a lone source as unverified. The rows shipped in v1 were cross-checked across three (LiteLLM, models.dev, OpenRouter) and matched exactly on every value all three carry.

**Where the public feeds run out**, each stated rather than guessed. Only the first is a genuine dead end; the other two are answerable with the right source, which is why they are written down as findings rather than as limits:

- **No feed carries history.** They publish today's list, so `effective_from` / `effective_to` is ours to maintain, accumulated as changes are observed. A request whose timestamp falls outside every row for its model is *unpriced and reported*, never repriced at today's rate.
- **Batch multipliers need a second feed.** Neither provider's machine-readable pricing carries one, and the primary feed does not either — but a feed that lists every model twice, standard and `:batch`, shows the multiplier as the ratio between the two listings. `prices check` uses both feeds for that reason, and the model id in the second feed is trusted only once its *standard* rates corroborate the row, so a wrong id declines to answer rather than comparing the wrong model. Without the second feed the field is reported unverifiable; absence never reads as agreement.
- **Long-context tiers move, and the feeds name them inconsistently.** The tier rates themselves are carried (see below), but the *threshold* is spelled into the feed's field names, so a provider moving the boundary makes the tier fields go quiet rather than disagree. `prices check` reports that as unverifiable, which is honest — it is not a matching price.

**`null` is never zero.** A multiplier absent from a row means the provider does not sell that token class — OpenAI's prompt caching is automatic and has no write to bill — and asking for it is an error. Defaulting it to zero would price a whole token class at nothing and read as a saving.

**Long-context tiers are part of the row.** Several models reprice above a prompt-size threshold — `claude-sonnet-4-5` above 200k, `gpt-5.5` and `gpt-5.5-pro` above 272k, all at input x2 and output x1.5. A row may carry a `long_context` block holding the tier's own rates and multipliers, and two rules govern it:

- **The reprice is wholesale, not marginal.** Crossing the threshold prices the *entire* request at the tier rate rather than charging only the excess tokens. Both providers bill it this way, and the marginal reading understates a 300k-token request by roughly a third — a plausible-looking number with nothing in the output to flag it.
- **Only the prompt counts toward the threshold**: input, cache reads and cache writes. Output is billed at the tier's output rate but never pushes a request across, so a long answer to a short question stays on the base rate.

A row with no `long_context` block has **verified absence** of a tier, not an unknown one: every bundled row was checked against all three feeds and, for OpenAI, against the provider's own pricing page. Cache multipliers inside a tier are re-derived off the *tiered* input rate, not the base rate. Batch composes on top of whichever tier applies, and the threshold itself is unaffected by batching.

**On OpenAI, assume the tier and verify its absence — the reverse of the usual default.** The 272k surcharge is a family property from `gpt-5.4` onward rather than a per-model quirk: every OpenAI frontier model from that point carries it at input x2 / output x1.5. On Anthropic the opposite holds — only Sonnet 4.x carries a 200k tier, and the 1M-context generation (Opus 5, Sonnet 5, Sonnet 4.6, Fable 5) is explicitly flat-rated at any prompt size.

The pattern is not confined to these two providers, which matters when broker rows arrive: xAI bills 2x/2x above 200k and Google 2x/1.5x above 200k, both wholesale. A `long_context` block is therefore expected to be the normal case for a frontier row, not an exception carved out for three of them.

**Token counts are not portable across tokenizer families.** Every row records the `tokenizer` that produced the counts it prices. The same text tokenizes differently on Claude and on GPT, so multiplying one model's logged token counts by another model's rates is wrong by whatever the two tokenizers disagree by — silently, in the direction of whichever is more compact, on every request at once. `pricing.token_counts_transferable()` is the check, and a routing analyzer (§10.2) must call it before any cross-model comparison. Crossing families requires re-tokenizing the prompt with the target model's tokenizer, which needs Tier A content (§6.3) and is therefore impossible on billing-only logs — an analyzer that cannot re-tokenize must stay inside a family or decline the comparison and say why. The same applies to the tier thresholds above: whether a prompt crosses 272k is itself a tokenizer-dependent question.

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
        cache_read_multiplier: <mult>    # negotiated cache/batch terms differ from public ones
        batch_multiplier: <mult>

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
- **A currency mismatch between the overlay and the catalog is a config error**, never a conversion (§7.1).
- The overlay is the **one place a rate is legitimately a literal**, because it is the user's own contract expressed as data. It is still read exclusively through `pricing.rate()` / `pricing.multiplier()`; no analyzer, template, or test ever reads an overlay field directly (AGENTS.md).
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
2. **Token-aware prefix trie (refinement, Tier A only, top-N workloads by spend).** Build a trie over tokenized prefixes to find breakpoints *inside* a segment, respecting per-model minimum-cacheable-token thresholds and the provider's maximum breakpoint count. Applied only to the heaviest workloads because it is the expensive path.

**Which stage runs at which tier**, since this is where the fidelity tiers actually bite:

| Tier | Stage 1 (segment boundaries) | Stage 2 (sub-segment trie) |
|---|---|---|
| **A — Content** | Yes | Yes |
| **B — Hashed** | Yes, with **exact** cacheable token counts from per-segment token counts (§6.3) | No — reported as an available refinement, not silently skipped |
| **C — Billing** | No (blocked → instrumentation finding, §13.5) | No |

Tier B is not a degraded cache analysis: breakpoints can only be placed at segment boundaries anyway, so segment-granular math is the *implementable* answer. What Tier B cannot do is tell you that a breakpoint placed mid-segment would clear a minimum-cacheable-token threshold that the segment boundary misses. Threshold-adjacent cases are therefore reported as **indeterminate at this fidelity**, naming the token gap, rather than resolved by guessing.

**Discrete-event cache simulator.** Requests are replayed in timestamp order against a modeled cache:

- per-entry **TTL** (5-minute refresh-on-read, 1-hour variants), with expiry driving realistic misses;
- **minimum cacheable token thresholds** — sub-threshold prefixes cannot cache at all;
- the **write premium** for the entry's TTL class, always paid in full before any read benefit (multipliers from the catalog, never inline — AGENTS.md);
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
| **Batch API migration** | Workloads scoring latency-tolerant, with no interactive session signal; models the catalog's batch multiplier against the turnaround SLA | Must prove hours of tolerance — the claims case, never the chatbot |
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
- **Persistent replay cache** — results keyed by `(prompt_hash, model, params, backend)`, so re-runs and repeat audits never pay twice. This cache holds **model output, which is content**, so it is the one exception to §12 layer 1 and is governed accordingly: it lives outside the run store, is opt-in with the replay feature itself, carries its own TTL, is excluded from every export path, and is listed in the report alongside what was sent. A user who declines replay never has one.
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
waste → routing → caching → batching
```

**Waste first**, because a wasted request is not traffic to be optimized — it is traffic that should not exist. A retry storm's prefix is genuinely cacheable, so a cache-first ordering credits caching with real dollars on requests that the waste fix deletes outright, and then the re-audit shows the cache finding under-delivering for reasons nobody can reconstruct. Removing phantom tokens from the baseline before anything else claims them is both more defensible and the ordering that survives verification (§13.6).

Routing next, because a model swap changes the unit price every later saving is computed against. Caching after routing, because it changes the token mix batching operates on. Batching last, as the operation applied to whatever traffic survives all three.

Both numbers are reported for every finding: **standalone** (what it would save alone) and **marginal** (what it adds given everything ranked above it). A large standalone-versus-marginal gap is itself informative — it is the report saying "this finding is mostly someone else's dollars" — so both are shown together, never the marginal alone. The portfolio total is the sum of marginals only, and the report says so explicitly next to the total.

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
- A monthly projection is shown **separately and clearly labeled**, and is produced only when coverage checks pass: **≥7 days** of logs, complete-day boundaries **in the configured timezone** (§6.6), and detected weekly/daily seasonality accounted for.
- Traffic trend is estimated and stated (a workload growing 20%/month changes the projection materially).
- When coverage fails, the projection is withheld and replaced by an explicit warning naming what is missing.
- **Read failures gate the baseline, not only the projection** (§6.1): any unread object makes the baseline a stated lower bound, and a missing share above `coverage.max_missing_pct` withholds savings figures entirely. A number that is wrong is not improved by a footnote.

### 11.5 Price-increase exposure

Findings are ranked by what they save today. The executive section also answers the adjacent question — *what happens to this bill if rates go up* — because it changes which remediation is worth doing first.

The pricing pass is re-run over the same records under a uniform rate increase (`--price-sensitivity`, default +25%), and two things are reported:

- **Exposure by workload** — spend growth under the increase, which is simply concentration: a workload whose cost sits in output tokens on one frontier model moves more than a diversified one.
- **Hedging value by finding** — the increase in each finding's *avoided* spend. A cache finding worth $1.4k/month today is worth more under a rate rise, and that difference is the argument for doing it now rather than next quarter.

It is a re-pricing of observed traffic, not a forecast: no probability is attached to the increase, the multiplier is stated next to every figure, and the panel is labeled as a sensitivity. It introduces no new analyzer and no new evidence — which is exactly why it carries the confidence tier of the finding it re-prices, and never a higher one.

---

## 12. Privacy

The tool runs on user infrastructure and may see prompt content. Becoming a server changes the threat model — there is now a listening port and an upload endpoint — without changing the posture, because the server is theirs.

**Layer 0 — the server is local and unauthenticated by design.**

- `serve` **binds `127.0.0.1` by default.** Binding to any other interface requires an explicit `--host`, and the app prints a warning naming what is being exposed. A loopback bind limits exposure to processes on the host — **not to the invoking user**: on a multi-user machine every local account can reach the port, and it is the workspace's file permissions, not the bind address, that bound what they can read.
- **There is no authentication in v1, and that is a deliberate scope decision, not an oversight.** A single-tenant local server with no accounts has nothing to authenticate; adding a homegrown login would create the illusion of a security boundary without the substance of one. Teams deploying it beyond one machine put it behind whatever they already use — an SSO reverse proxy, a VPN, an SSH tunnel. The docs say this plainly rather than shipping a password field.
- **The app makes no outbound calls of its own** — no CDN assets, no telemetry, no update check. Every asset is served from the package, which is also why the CSP can forbid external origins outright. The only outbound calls in the entire system remain the opt-in replay calls of §10.3.
- **Uploaded logs are treated as ingest input, not as stored files** — but they are briefly on disk, and pretending otherwise would be the kind of claim this project does not make. A run can sit queued (§5.4), so the bytes must survive until a worker takes it:
  - the upload is buffered to a **staging path inside the workspace**, mode `0600` in a `0700` directory, named by run id;
  - it is **deleted when the run consumes it, when the run is cancelled or fails, and on server startup** for any run no longer queued or running — three paths, so no single failure leaks the file;
  - the **maximum upload is 25 MB** per file, enforced before the body is read, with the limit stated in the UI; larger log sets go through a connection (§6.1), which streams and never stages;
  - the staging file is raw log content, so it is excluded from every export path and is the one place §12 layer 1 is relaxed — bounded to a single run's lifetime and stated here rather than discovered later.
- **The API is CSRF-protected and path-confined.** State-changing endpoints require a same-origin token, and any server-side path the UI accepts (log locations, output directories) is resolved and confined to configured roots — a browser-reachable process that reads arbitrary filesystem paths is a file-disclosure bug regardless of who is on the other end. The same confinement governs remote reads: runs name a **configured connection**, and every connection is validated against the terminal-configured **source scope** on save and on use (§6.1). Connections are editable in the app; the scope that bounds them is not.
- **The server stores no credentials** (§6.7), which is what keeps the no-authentication position honest rather than convenient. It borrows the host's ambient cloud identity for the duration of an operation; there is no secret in the workspace to steal, and correspondingly nothing for an access token to protect. A resolved credential is still masked in every string form and never reaches a run record, log, event stream, or report.
- **Storing credentials would change this, which is why it is v1.1 and not a flag.** The v1.1 credential store arrives together with the access token that a non-loopback bind then requires ([design note](docs/design/credential-store.md)); the two are one change. "Unauthenticated" and "holds cloud keys on a shared interface" is a combination the tool will not let a user assemble, in this release by not offering the first half.

The four content layers are unchanged:

1. **Never persist raw content.** Hashing and fingerprinting happen at ingest; raw text exists only in memory for the chunk being processed. The derived store contains no prompt text.
2. **Redact before persisting anything derived.** Configurable detectors for emails, keys/tokens, card numbers, national ids, and user-supplied patterns; redaction runs before any storage and before any replay transmission.
3. **Evidence samples are opt-in.** By default findings cite fingerprints, counts, and token statistics. Real prompt excerpts appear only when explicitly enabled.
4. **Retention, and no encryption of derived data.** A TTL purges ingested records after N days (default 30), leaving findings and reports readable (§5.3). The derived store is deliberately **not** encrypted, reversing an earlier position: the run store's value is that it is inspectable, diffable, and copyable (§5.3), and sealing it would have meant either a key sitting next to it — filing, not encryption — or a passphrase prompt on every headless `audit` and every CI re-audit, on a box with no OS keyring. What makes that safe is upstream and stronger: raw prompt text is never written at all (layer 1), and what is written has been redacted (layer 2). **Nothing in v1 is encrypted at all** (§6.7); the first sealed format in this project is the v1.1 credential store — one format, one module, one place to review.

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
| **Connections** | The saved log sources (§6.6): add or edit a connection, **test** it (credentials resolve, prefix lists, permissions exercised are named), and **preview** — the first N records decoded, with the detected format, provider, fidelity tier, and observed time range, before committing to a full run. Every connection is validated against the terminal-configured source scope (§6.1); the page shows the scope it is bound by and cannot edit it. |
| **New run** | Pick **one or more connections** (or upload files, or point at a local path within the source scope), set the window and timezone, attach `workloads.yaml` / `architecture.yaml` / commercial terms if any, pre-flight validation — including an object count and byte estimate for the window, so a run nobody meant to start is visible before it starts — then submit. |
| **Run overview** | The executive section (§3) for one run: baseline spend decomposed by workload, model, token class and status; waste percentage; portfolio total with the "sum of marginals" statement next to it; totals broken out per confidence tier; the price-increase exposure panel (§11.5). |
| **Findings** | Sortable, filterable table — by analyzer, workload, slice, confidence tier, risk, effort, realizability. Each row expands into the engineering detail: evidence, the exact change, verification steps, and both standalone and marginal savings. |
| **Coverage** | What was skipped and why (§11.4): objects that failed to read and their byte volume (§6.1), whether the baseline is a lower bound or savings were withheld outright, unpriced models, the **analyzer × slice verdict matrix** (§5.2) with instrumentation findings for what was blocked, fidelity tier per source, price-catalog staleness, records with unknown cache TTL class and their exposure range (§6.2), cluster-quality warnings, whether the monthly projection was withheld. |
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
POST   /api/connections           create — rejected unless the uri falls inside the source scope (§6.1)
PUT    /api/connections/{id}      update — same scope check, re-applied on every save
DELETE /api/connections/{id}      delete, naming the runs that referenced it
POST   /api/connections/{id}/test resolve credentials, list the prefix, report permissions exercised
POST   /api/connections/{id}/peek decode the first N objects: detected format, source, fidelity, time range

GET    /api/sources               the terminal-configured source scope — read-only, no write endpoint
```

Connections are writable through the API because a tool that requires a YAML edit to point at a bucket is a tool people abandon at the first step. What makes that safe is that the **source scope is not writable through the API at all** (§6.1): the browser chooses *where within* the permitted area to look, and the terminal chooses the permitted area. `GET /api/sources` exists so the UI can show the boundary it is working inside; there is no corresponding write.

There are no credential endpoints, because v1 stores no credentials (§6.7). When the store arrives in v1.1 it is write-only from every direction: a `PUT` to set or rotate, a `GET` that returns metadata only, and deliberately no route that returns a value.

It is deliberately thin: the API exposes runs and their artifacts, not analyzer internals. The HTML pages are server-rendered rather than built on top of this API (§5.1) — the API exists for programmatic callers, so it does not have to grow an endpoint for every UI affordance.

### 13.3 CLI

```
llm-cost-auditor run      <connection-id...>          # the whole pipeline: ingest → profile → audit
                          [--window 2026-08-01..2026-08-31] [--timezone <IANA>]   # §6.6
                          [--config workloads.yaml] [--architecture architecture.yaml]
                          [--stop-after ingest|profile]      # halt; resume later against the same run
llm-cost-auditor ingest   <uri...|connection-id> --source anthropic|openai|bedrock|vertex|foundry
                          # sugar for `run --stop-after ingest`; uri must fall inside the source scope
                          # uri: ./logs/*.jsonl | file:///path | s3://bucket/prefix/ | az://container/prefix/
                          [--fidelity auto] [--format auto] [--compression auto]
                          [--window 2026-08-01..2026-08-31]   # prunes the object listing (§6.1)
                          [--profile <aws-profile> | --role-arn <arn>] [--account <azure-account>]
llm-cost-auditor sources  [list | add <root> | rm <root>]     # the source scope (§6.1) — terminal only
llm-cost-auditor connections [list | add | edit <id> | rm <id> | test <id> | peek <id> [-n 100]]
llm-cost-auditor profile  --run <run_id>              # the run to profile; must have completed ingest
                          [--emit-config workloads.yaml] [--emit-architecture architecture.yaml]
llm-cost-auditor audit    --run <run_id>              # the run to analyze; must have completed profile
                          [--config workloads.yaml] [--architecture architecture.yaml]
                          [--price-sensitivity 25]           # rate-increase exposure panel (§11.5)
                          [--replay --replay-backend openrouter|bedrock|foundry|vertex
                           --replay-budget-usd 50 | --replay-budget-pct 0.5 --dry-run]
                          [--out report.html --json findings.json --snapshot snapshot.json]
                          [--explain-pricing <request_id>]   # print list → effective rate resolution chain
llm-cost-auditor verify   --baseline snapshot.json | <run_id>   # realized vs projected
llm-cost-auditor refresh-prices

llm-cost-auditor serve    [--host 127.0.0.1] [--port 8787] [--workspace ./.llm-cost-auditor]
llm-cost-auditor runs     [list | show <run_id> | rm <run_id>]   # the run store (§5.3)
```

**`--run <id>` is how stages compose, and it is not optional.** `run` executes all three stages against one run directory; `--stop-after` halts it; `profile --run` and `audit --run` continue that same run. There is no implicit "most recent ingest" anywhere in the CLI — a command that guesses which data it is analyzing is one that silently analyzes the wrong data, and the failure surfaces as a plausible number rather than an error. A stage that has already run on a given run id is refused: re-running means a new run with a `parent_run_id` (§5.3).

Everything writes into the run store, so work started at a terminal is visible in the app and vice versa. `--out` / `--json` / `--snapshot` additionally copy artifacts to a chosen path, for pipelines that want the file where they want it.

### 13.4 Finding schema

All monetary fields are **integer micro-USD** (§6.4) and named `_usd_micros`, so no consumer can round-trip a dollar figure through a float.

```json
{
  "id": "cache.prefix.workload-7",
  "run_id": "run_2026-09-04T18-20-11Z_a91f",
  "title": "Cache the 4.2k-token system prompt + tool definitions for claims-extraction",
  "analyzer": "cache.prefix",
  "workloads": ["claims-extraction"],
  "slices": [{ "connection_id": "prod-bedrock-s3", "source": "bedrock", "fidelity": "B" }],
  "confidence": "Simulated",
  "confidence_penalties": [
    { "code": "approximated_overlap", "effect": "cap:Estimated", "detail": "..." }
  ],
  "evidence": { "requests": 18422, "window_days": 30, "simulated_hit_rate": 0.87,
                "current_hit_rate": 0.0, "method": "exact_overlap",
                "assumptions": ["5m TTL", "single region"] },
  "pricing": { "catalog_version": "2026.09.1", "resolution": "list → anthropic.discount_pct",
               "oldest_last_verified": "2026-08-02" },
  "savings": {
    "currency": "USD", "basis": "observed_window",
    "gross":    { "standalone_usd_micros": {"low": 1180000000, "expected": 1420000000, "high": 1610000000},
                  "marginal_usd_micros":   {"low": 1180000000, "expected": 1420000000, "high": 1610000000} },
    "realizable": "yes",
    "realizable_usd_micros": {"low": 1180000000, "expected": 1420000000, "high": 1610000000},
    "realizability_reason": null,
    "projection_monthly_usd_micros": 1420000000,
    "price_sensitivity": { "multiplier_pct": 25, "hedged_usd_micros": 355000000 }
  },
  "risk": "low",
  "effort": "S",
  "remediation": { "summary": "...", "snippet": "...", "files_hint": [] },
  "verification": ["Re-run audit after deploy", "Expect cache_read_tokens > 0 within 1h"],
  "verification_status": null
}
```

Field rules that the prose elsewhere depends on:

- **`realizable` is an enum, not a boolean** — `yes` | `no` | `deferred` | `partial` — because §7.2 needs three answers a boolean cannot give: floor-suppressed savings are worth `$0` realizable against a real gross number, and prepaid-credit savings are `deferred` (burn-rate extension, not budget reduction). `realizability_reason` names the committed floor or the credit balance responsible, so `$0` is never unexplained.
- **`gross` and `realizable` are always both present.** The report shows them side by side; a finding suppressed to zero still states what it would have been worth.
- **`slices` is what §5.2's per-slice applicability produces** — a finding can never be read as covering traffic its analyzer never saw.
- **`confidence_penalties` is a list, not a prose note** (§11.3), so "why is this only Estimated" is answerable mechanically.
- **`verification_status`** is filled by `verify` (§13.6): `implemented` | `partially_implemented` | `not_detected`, and `null` until then.

### 13.5 Instrumentation findings

When an analyzer is `BLOCKED`, the gap becomes a finding rather than silence:

> *"Prefix-cache analysis is blocked: your OpenAI logs lack per-segment hashes. Based on token-volume patterns in this workload, prefix caching could plausibly address **$3–9k/month**. To unblock, emit segment hashes using the sidecar snippet below — no prompt text is stored."*

The dollar ceiling is explicitly bounded and tier-**Heuristic**; it exists to justify the instrumentation work, and the report says so.

### 13.6 Verification loop

Each `audit` writes a **snapshot**: traffic mix, unit costs, workload profiles, catalog version, resolved config, and every finding, with a **SHA-256 content digest** over the whole document. The digest detects accidental edits and identifies the exact snapshot a diff was taken against; it is **not a signature** and makes no claim about who produced it — there is no signing key anywhere in this project, and inventing one here would be exactly the drift AGENTS.md forbids.

`verify --baseline` diffs a later run against it and reports **realized vs projected** savings per finding, **normalized for volume change** so that traffic growth cannot mask a win (or manufacture one). Findings get a `verification_status` of `implemented` / `partially_implemented` / `not_detected` based on observable signals (cache-read tokens appearing, batch flags appearing, model mix shifting).

**What a re-run can and cannot do.** A re-run with edited config replays from the run's `records.parquet`, which is Tier B–equivalent by construction (raw content is never stored, §12 layer 1). Analyzers that require Tier A — sub-segment breakpoint refinement (§9.2), content-based redundancy detection (§9.1) — must therefore **re-read from the source**, and if the source is no longer available, or the run's records have been purged by the retention TTL, those findings are withheld with a coverage entry rather than recomputed from what is left. This is a real cost of never persisting prompt text, and it is stated here rather than discovered when a re-run quietly returns fewer findings than the original.

---

## 14. Validation strategy

No external oracle exists for "you would have saved $X", so correctness is established four ways.

**Two regimes, and the difference is not a detail.** AGENTS.md requires exact equality; §14.1 recovers savings from a simulator, which cannot be exact. Both are true, of different things, and conflating them produces either an unenforceable rule or a test that always passes:

| What is asserted | Rule |
|---|---|
| **Pricing and attribution arithmetic** — per-record cost, baseline totals, multiplier composition, marginal sums | **Exact equality**, in integer μUSD (§6.4). No tolerance, ever. This is the hand-computed fixture suite (AGENTS.md). |
| **Recovered planted savings** — what a simulator or estimator reconstructs from generated traffic | A **per-case declared bound**, written into the fixture next to the expected value with a one-line justification for why that case cannot be exact. A shared global tolerance is forbidden: it is how an off-by-one in one analyzer hides inside another's slack. |

1. **Synthetic log generator with known ground truth.** Generate traffic with injected, quantified inefficiencies — a known-cacheable prefix at a known reuse rate, a retry storm of known size, a latency-tolerant workload of known volume — and assert the auditor recovers the planted savings within that case's declared bound. Cases whose arithmetic *is* deterministic (a retry storm's billed tokens, a billed failure) are asserted exactly even here — a declared bound is permitted only where the estimation is genuinely inexact, and a bound that could have been exact is a defect in the test. This is the core correctness harness and gates every release. It also generates the adversarial cases: bursty arrivals, TTL-boundary reuse, sub-threshold prefixes, prefix churn.
2. **Golden fixtures per provider.** Small anonymized real-shaped log samples per source with checked-in expected outputs, so provider schema drift and price-catalog changes break loudly rather than silently shifting dollar figures.
3. **Property-based simulator tests.** Invariants that must never be violated: savings ≤ baseline spend; hit rate monotonic non-decreasing in TTL; write premium always paid before any read benefit; **marginal attributions sum exactly to the portfolio total** (achievable only in integer μUSD, §6.4); no finding's marginal exceeds its standalone; sub-threshold prefixes never produce savings; simulated savings ≤ theoretical maximum (all reads free); a duplicate-delivery collapse never changes baseline spend while a genuine retry always does (§6.5.1).
4. **Invoice cross-check.** When a provider bill is supplied, computed baseline spend must match within **2%**; a larger gap is treated as a bug in pricing or ingest, not a rounding note. This is also how declared discounts are validated.

---

## 15. Known tensions (stated, not hidden)

1. **In-memory scale vs. analysis depth.** The v1 target (~500k requests, in-memory) coexists with a ≥7-day coverage requirement, token tries, and MinHash/LSH. High-volume users will exceed it. Mitigation: spend-weighted sampling with stated sampling error for the fast pass, exact analysis on top-N workloads, and the `RecordStore` seam ready for a DuckDB backend.
2. **Per-request dynamic routing on sampled evidence.** The strongest routing shape rests on the weakest evidence base. Mitigation: capped at the **Estimated** tier, always presented alongside the static-downgrade alternative, and never recommended for `high`/`regulated` workloads without replay validation.
3. **Semantic caching is a correctness risk sold as savings.** Mitigation: MinHash default (near-duplicate, not paraphrase), embeddings opt-in with an explicit threshold and estimated false-hit rate, suppressed entirely above `medium` blast radius.
4. **Template fingerprinting drives everything and can be wrong.** Over-merging inflates cache findings; over-splitting hides them. Mitigation: cluster-quality metrics in the report, user override via config, and a warning when intra-cluster variance is extreme.
5. **Committed spend can make every finding worth $0.** Mitigation: realizable-vs-gross reported separately, always.
6. **A UI makes numbers look more certain than they are.** A dollar figure in a styled dashboard reads as fact in a way the same figure in a terminal does not, and confidence tiers are exactly what users skim past. Mitigation: the tier and the low–high range are part of every savings figure's presentation, not a column users can hide; the portfolio total never appears without the "sum of marginals" statement next to it; withheld projections show the warning in place of the number rather than omitting the panel.
7. **Two surfaces can drift into two truths.** Mitigation: the app and the exported report render the same run record through the same template layer, and a figure computed in a view rather than by the engine is treated as a defect (§13.1).
8. **An auditing tool that can reach cloud storage is a target, whether or not it holds keys.** Reading logs from S3 or Azure Blob means inheriting the host's permissions, so a browser-reachable process becomes a way to exercise them. v1 declines the larger version of this risk by storing no credentials (§6.7), which leaves the smaller one: ambient identity is still identity, and a server bound beyond loopback can be asked to use it. Mitigations are the source scope (§6.1), read-only permissions in every setup instruction, `credential_used` events per run, and the loopback default. The residual risk is a compromised host, where nothing the app does helps — and, until v1.1, an operator who binds publicly anyway.
9. **Incomplete log delivery looks exactly like less traffic.** Object storage is where logs go to be silently incomplete — a delivery lag, a lifecycle rule that expired last month's keys, a prefix nobody granted access to. The tool cannot tell "no requests" from "no logs". Mitigation: read failures are coverage failures with named objects and byte volumes, gaps in the observed timeline are reported against the requested window, and projections are gated on both — but a clean-looking run over quietly truncated data remains the residual risk, which is why the manifest names every object read.
10. **A long-lived server process contradicts the in-memory design.** A run holds a whole dataset in memory; a server that accepts concurrent runs will be OOM-killed on the laptop this is meant to run on. Mitigation: concurrency 1 by default (§5.4), and the run store — not process memory — is what outlives a run.
11. **Bulk ingest makes repeat audits expensive.** Every run re-reads its whole window (§6.6), so a daily re-audit against a month of S3 logs pays a month of listing and transfer each time — including egress the user is billed for. This is a deliberate v1 trade: the incremental design that avoided it produced runs whose records depended on what an earlier run happened to read, which is a silently-wrong baseline rather than a slow one. Mitigation: window pruning keeps the listing proportional to the window (§6.1), and v1.1's decoded-record cache removes the parsing cost while still materializing the full window. The residual cost is real and is stated in the run's byte estimate before it starts.
12. **The source scope is a boundary the user can widen to nothing.** `sources add /` restores exactly the exfiltration primitive §6.1 exists to prevent, and nothing stops a user doing it. Mitigation: it is a terminal-only operation, the UI displays the scope it is bound by on every connection, and the setup docs never demonstrate a broad root. This is a boundary against a browser-side attacker and an accident, not against the operator — who could equally widen the host's own IAM permissions.

---

## 16. Open questions

- Reference fingerprint-sidecar: shipped as a Python package only, or also TS/Go, given Tier B is the recommended posture?
- Non-USD billing (some Vertex/Foundry accounts) is a config error in v1 (§7.1). Does v1.1 add a declared fixed rate per audit window, or stay USD-only and document the limit?
- The retention TTL destroys `--explain-pricing` and any Tier-A re-analysis for old runs (§5.3, §13.6). Is a longer default, or a records-only opt-out, worth the privacy cost?
- Azure is the one connector with no clean ambient-file fallback for a handed-over SAS token (§6.7). Does the v1.1 credential store arrive early enough, or does v1 need a smaller stopgap — a SAS token in a `0600` file the connection points at, which is `~/.aws/credentials` for Azure and nothing more?
- Do we support a warehouse-pushdown execution mode (BigQuery/Snowflake) before or after the DuckDB backend?
- Cascade findings require an escalation *signal* to exist; do we recommend one, or only surface cascades where a validator is already present in the logs?
- Licensing and distribution model for the price catalog updates.
- Does the app ever need to write config back to the user's repo, or only offer generated config for download? Writing files a browser session chose the contents of is a meaningfully larger trust ask.
- Is a packaged container image part of the v1 deliverable, or is `pip install` + `serve` enough for the first users?
- Do the cloud log **query** APIs (CloudWatch Logs, Log Analytics, Cloud Logging) ever need to be connectors, or is "export to a bucket" always available in practice? The bet in §6.1 is that it is; the first user who cannot export settles it.
- Gateway logs (LiteLLM, OpenRouter, Helicone) are a post-v1 *source* adapter, but several of them expose a database or an API rather than files. Does that make them a source, a connector, or both?
- The shared-deployment case (a team pointing one instance at a shared log export) is out of scope for v1 but keeps being asked for. What is the smallest thing that would make it defensible — a reverse-proxy deployment guide, or actual identity in the product?
