# LLM Cost & Routing Auditor — Specification

**Status:** Draft v1.0 · **Date:** 2026-09-04 · **Repo:** `llm-cost-auditor`

---

## 1. Summary

An offline CLI that ingests provider API logs and produces a **ranked, evidence-backed savings report**: where prompt caching is missing or misconfigured, where requests are wasted outright, where latency-tolerant work belongs on batch endpoints, and where a cheaper model would have sufficed.

Three properties define the product:

1. **It never sits in the request path.** It is an auditor, not a gateway. The only outbound network calls it ever makes are opt-in, budget-capped shadow-replay calls used to *earn evidence* for routing claims.
2. **It degrades gracefully with log fidelity.** Every analyzer declares its minimum data requirements. Missing data becomes an *instrumentation finding* with a dollar ceiling, not a silent skip.
3. **It never overstates.** Every finding carries a confidence tier and a low/expected/high range, findings are de-overlapped before totalling, and projections are gated on data coverage.

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

### Deferred, not excluded (roadmap)

- Fine-tuning and distillation economics ("train a small model to replace the frontier one") — often the largest lever, but requires quality modeling well beyond v1.
- Prompt-quality advice beyond mechanical redundancy detection.

---

## 3. Audience and deliverable

One report artifact, two audiences, plus machine-readable output:

- **Executive section** — total spend over the observed window, waste percentage, top 5 findings with dollars and risk rating, portfolio total after overlap de-duplication, a section that would list the top 5 findings that would be hit the hardest with a token price increase.
- **Engineering section** — per finding: evidence, affected workload(s), the exact config/code change, effort estimate, risk notes, and verification steps.
- **`findings.json`** — the same findings machine-readably, for CI gates, dashboards, and diffing across runs.

---

## 4. Scope by release

| Release | Analyzers |
|---|---|
| **v1** | Prefix-cache opportunity + cache-efficiency critique; waste findings (retry storms, 429 churn, truncated/cancelled-but-billed, oversized `max_tokens`, duplicate in-flight requests, redundant context) |
| **v1.1** | Batching: Batch API migration, request consolidation, cache-aware scheduling, concurrency/rate-limit shaping |
| **v1.2** | Routing: difficulty heuristics, natural experiments, cascades, per-request dynamic policy, shadow-replay validation harness |
| **v2** | Fine-tuning/distillation economics; warehouse-pushdown execution |

The data model, pricing engine, workload profiler, confidence framework, and attribution engine are built in v1 because every later analyzer depends on them. Sections 8–10 specify v1.1/v1.2 analyzers in full so the v1 foundations are built to fit them.

---

## 5. Architecture

```
log files / exports
      │
      ▼
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
```

### 5.1 Stack

- **Python 3.12+**, `polars` (in-memory frames), `pydantic` v2 (models/config), `typer` (CLI), `jinja2` (report), `datasketch`-style MinHash/LSH (vendored or dependency), provider tokenizers behind an optional extra.
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

`BLOCKED` does not mean silence: the runner converts it into an **instrumentation finding** (§13.3). Log adapters implement a parallel `SourceAdapter` protocol, so new providers are additive.

---

## 6. Ingest

### 6.1 Sources (v1)

| Source | Notes |
|---|---|
| **Anthropic** | Exposes `cache_creation_input_tokens` / `cache_read_input_tokens`, enabling direct measurement of realized cache savings and cache-efficiency critique. Distinguishes 5-minute vs 1-hour cache TTL. |
| **OpenAI** | Automatic prompt caching with no explicit breakpoints; `cached_tokens` is reported but not user-controlled, so findings shift from "add cache_control" to "restructure prompt so the automatic cache can engage". |
| **Bedrock / Vertex / Foundry** | Cloud-broker billing across AWS Bedrock, Google Vertex AI, and Azure AI Foundry. Each has its own price sheet (broker prices diverge from first-party list prices and from each other), its own log shape (CloudWatch Logs / Cloud Logging / Azure Monitor diagnostic settings), and its own reserved-capacity construct — Bedrock Provisioned Throughput model units, Vertex PTUs, Foundry PTUs — all of which mark traffic as already-paid-for (§7.2). Caches are scoped per deployment/region on all three, which the simulator must partition on (§9.2). |

Gateway logs (LiteLLM, OpenRouter, Helicone) are a post-v1 adapter that maps onto the same canonical record.

### 6.2 Fidelity tiers

Every dataset is classified, per-source, into the highest tier it supports. Mixed-tier datasets are supported; findings are tagged with the tier that produced them.

| Tier | Contains | Unlocks |
|---|---|---|
| **A — Content** | Full request/response bodies: system prompt, tools, messages, completion | Everything: token-exact prefix tries, response/semantic caching, redundant-context detection |
| **B — Hashed** | No raw text. Per-segment hashes (system prompt, each tool definition, each message) + rolling 1k-token prefix hashes, token counts, params | Prefix caching, exact-match response caching, conversation replay waste, all waste findings. No semantic caching, no content-based redundancy detection |
| **C — Billing** | Timestamps, model, token counts, latency, request id, status | Waste findings (retries, truncation, 429s), spend decomposition, latency-tolerance profiling, batching eligibility. No cache analysis |

**Design consequence:** Tier B is the recommended posture and the tool ships a reference "fingerprint sidecar" spec — a small library/logging snippet users add to their client so their logs become Tier B without ever storing prompt text.

### 6.3 Canonical record

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

### 6.4 Normalization edge cases (all mandatory in v1)

Each of these silently corrupts cost math if ignored.

1. **Retries and duplicate records.** Deduplicate by request id across at-least-once log delivery. Distinguish *logged twice* (one billing event) from *genuinely retried* (multiple billing events). Critically: a 429-triggered retry costs nothing, while a 500 after generation may have been billed in full. Classification rules are per-provider and asserted in golden fixtures.
2. **Failed / truncated / cancelled requests.** Input tokens are frequently billed despite an error; `stop_reason=max_tokens` truncations often mean the response was unusable and re-requested; client-cancelled streams still bill generated tokens. These form their own finding class (§9.1), not just an ingest concern.
3. **Streaming reassembly.** Collapse SSE chunk logs into one record with final usage. When the terminal usage event is missing, estimate tokens from reassembled content (Tier A) or from chunk counts (Tier B/C) and mark the record `usage_estimated=true`, which propagates a confidence penalty to any finding relying on it.
4. **Multimodal and non-chat endpoints.** Image/audio/video token accounting per provider formula, plus embeddings, rerank, and legacy completions — each with its own pricing unit. Unknown endpoints are counted in baseline spend but excluded from analyzers, and reported in the coverage panel.

---

## 7. Pricing engine

### 7.1 Price table

All prices live in a **single versioned price table** — data, never code. It is the only place a monetary rate exists anywhere in the system (see `CLAUDE.md`: no price literals, ever).

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
6. **`unit_of_work` converts findings into unit economics** — "$0.41 per claim processed, of which $0.17 is re-sent context" — which is both the more actionable framing and the one that survives traffic growth in the verification diff (§13.4).
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

The tool runs on user infrastructure and may see prompt content. Posture, all four layers:

1. **Never persist raw content.** Hashing and fingerprinting happen at ingest; raw text exists only in memory for the chunk being processed. The derived store contains no prompt text.
2. **Redact before persisting anything derived.** Configurable detectors for emails, keys/tokens, card numbers, national ids, and user-supplied patterns; redaction runs before any storage and before any replay transmission.
3. **Evidence samples are opt-in.** By default findings cite fingerprints, counts, and token statistics. Real prompt excerpts appear only when explicitly enabled.
4. **Encrypted local store with retention.** At-rest encryption for the derived database plus a TTL that purges ingested data after N days (default 30).

Any outbound call (replay, embedding, judging) is gated per §10.3 and fully itemized in the report.

---

## 13. Report and CLI

### 13.1 CLI

```
llm-cost-auditor ingest   <paths...> --source anthropic|openai|bedrock|vertex|foundry [--fidelity auto]
llm-cost-auditor profile  [--emit-config workloads.yaml] [--emit-architecture architecture.yaml]
llm-cost-auditor audit    [--config workloads.yaml] [--architecture architecture.yaml]
                          [--window 2026-08-01..2026-08-31]
                          [--replay --replay-backend openrouter|bedrock|foundry|vertex
                           --replay-budget-usd 50 | --replay-budget-pct 0.5 --dry-run]
                          [--out report.html --json findings.json --snapshot snapshot.json]
                          [--explain-pricing <request_id>]   # print list → effective rate resolution chain
llm-cost-auditor verify   --baseline snapshot.json   # realized vs projected
llm-cost-auditor refresh-prices
```

### 13.2 Finding schema

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

### 13.3 Instrumentation findings

When an analyzer is `BLOCKED`, the gap becomes a finding rather than silence:

> *"Prefix-cache analysis is blocked: your OpenAI logs lack per-segment hashes. Based on token-volume patterns in this workload, prefix caching could plausibly address **$3–9k/month**. To unblock, emit segment hashes using the sidecar snippet below — no prompt text is stored."*

The dollar ceiling is explicitly bounded and tier-**Heuristic**; it exists to justify the instrumentation work, and the report says so.

### 13.4 Verification loop

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

---

## 16. Open questions

- Reference fingerprint-sidecar: shipped as a Python package only, or also TS/Go, given Tier B is the recommended posture?
- Do we support a warehouse-pushdown execution mode (BigQuery/Snowflake) before or after the DuckDB backend?
- Cascade findings require an escalation *signal* to exist; do we recommend one, or only surface cascades where a validator is already present in the logs?
- Licensing and distribution model for the price catalog updates.
