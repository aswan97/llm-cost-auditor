# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in `pyproject.toml` is bumped by hand in the release PR; CI blocks
the merge if it is unchanged or undocumented here.

## [Unreleased]

### Added
- **Ingest, end to end, for local files and Anthropic logs.** The first working slice of
  the engine (SPEC.md §6): the local-files connector with window pruning and source-scope
  confinement; a shared decode layer detecting compression (gzip/bzip2/zstd) and container
  format (JSON Lines, JSON) per object and reporting what it chose; the Anthropic source
  adapter, including the per-TTL cache-creation breakdown and the §6.2 unknown-TTL-class
  rule; and retry-versus-duplicate-delivery classification on billing facts (§6.5.1).
  Connectors move bytes and know nothing about providers; adapters interpret records and
  know nothing about where they came from, and a test asserts both.

- **The run store, and coverage that gates on it.** Runs are directories holding
  `run.json`, `manifest.json`, `records.parquet`, and `log.jsonl` (§5.3), with stages that
  run once and in order, a threaded single-worker queue, and startup reconciliation that
  marks interrupted runs rather than resuming them (§5.4). Coverage is computed from the
  manifest, not from what succeeded: any unread object makes the baseline a stated lower
  bound and withholds the projection, and past `coverage.max_missing_pct` the run is
  `incomplete` (§6.1, §11.4).

- **The `RecordStore` and `RunStore` seams**, kept separate (§5.1). Records are Parquet +
  polars with every count column explicitly `Int64`, so no inference can make a count a
  float.

- **The CLI** — `sources`, `connections` (list/add/rm/test/peek), `ingest`, `runs`
  (list/show/records/rm), `serve` — with `--window` interpreted in a declared timezone and
  no implicit "most recent ingest" anywhere (§13.3). `ingest` exits 2 when coverage is
  incomplete, so a CI gate can tell "withheld" from "failed".

- **The web app, first iteration** (§13.1): Runs, Connections, New run with a pre-flight
  object/byte estimate, and Run detail with live progress, the coverage panel, the slice
  table, and the manifest. Server-rendered Jinja + HTMX with every asset served from the
  package, CSRF protection on state-changing routes, a CSP that forbids external origins,
  and a read-only `/api/sources` with no write counterpart. It renders **only what ingest
  produces** — no findings pages and no dollar figures, because no analyzer exists yet and
  mock data in a template is the failure this project is trying to prevent.

- **Privacy protections at ingest** (§12): prompt text is hashed at segment boundaries and
  discarded, label values are redacted before storage, and tests assert the *absence* of
  prompt text, email addresses, and credential-shaped strings in every run artifact.

- **Window coverage is stated, not inferred.** A run whose objects hold nothing inside
  the requested window, or whose observed range falls a day or more short of it, says so
  in the coverage panel (SPEC.md §6.6, §15.9). Neither is gated — every byte was read —
  but "no traffic" and "the window missed the data" are no longer the same silence.

- **Hand-computed ingest fixtures** in `tests/fixtures/anthropic/`, with the arithmetic for
  every expected value derived line by line in a README alongside them, asserted exactly.

- **Docker** as the reference environment for running and testing the platform:
  `docker compose run --rm test`, `... run --rm cli <args>`, `... up app`.

### Fixed
- **Every form button in the web app was refused as cross-origin.** The app sent
  `Referrer-Policy: no-referrer`, and per the Fetch standard a browser serializes the
  `Origin` header as `null` on a non-CORS non-GET request under that policy — which is
  precisely a form POST navigation. So `Start run`, `Cancel`, `Add connection` and
  `Delete connection` all arrived opaque and were rejected by the CSRF origin check,
  while the HTMX buttons kept working because XHR is CORS-mode and keeps its real
  origin. The policy is now `same-origin`: the referrer still never leaves this origin,
  and an opaque `null` origin is still refused.

### Changed
- CI no longer self-skips mypy and pytest; the scaffolding guards are removed now that the
  first module has landed.

- **Design: the auditor is a self-hosted platform, not an offline CLI.** A local-first,
  single-tenant web app (FastAPI + Jinja + HTMX) and the CLI are two drivers over one run
  engine, with runs as durable artifacts in a filesystem run store. SPEC.md §1, §3, §4,
  §5.3–5.4, §12, §13 rewritten accordingly; §13 subsections renumbered.

- **Design: log sources are pluggable connectors.** Ingest is split into a connector
  (where the bytes are — local files, S3, Azure Blob) and a source adapter (what they
  mean), with a shared decode layer for compression and container formats, saved
  connections that hold no credentials, and an ingest manifest making re-audits
  incremental and double counting detectable. SPEC.md §6.1 and §6.6 added; §6.2–6.5
  renumbered.

- **Design: credential store for cloud connectors.** Connections may reference secrets held
  in the OS keyring or a passphrase-sealed file: write-only (no read path anywhere),
  short-lived kinds preferred, use audited per run, and never serialized into run records,
  logs, or config. A non-empty store plus a non-loopback bind now makes an access token
  mandatory — `serve` refuses to start without one. Secrets are sealed per record with
  XChaCha20-Poly1305 under an HKDF-derived key, the root key held in the OS keyring or
  derived from a passphrase with Argon2id, with algorithm parameters carried in a versioned
  envelope and `credentials rekey` to re-seal. SPEC.md §6.7 added; §2, §5.1, §6.1, §12,
  §13.1–13.3, §15 updated.

- **Design: pre-implementation spec review (SPEC.md v1.2).** Resolved contradictions and gaps found reviewing
  the design before the first module. Structural: one run is one directory with three resumable stages and an
  explicit `--run`, replacing implicit inter-command state (§5.3, §13.3); `Store` split into `RecordStore` and
  `RunStore` (§5.1); analyzer applicability is per-slice and findings name their slices (§5.2, §13.4); money is
  integer micro-USD end to end (§6.4); ingest is bulk in v1 with incremental deferred to v1.1 (§6.6);
  attribution runs waste first (§11.2); encryption is scoped to credentials only, and the derived store is no
  longer encrypted (§5.3, §12). Security: reads are confined to a terminal-only source scope so connections stay
  editable in the app (§6.1, §13.2), the bind interlock is re-checked on every credential write (§6.7), and the
  loopback and "signed snapshot" claims are corrected to what they actually provide (§12, §13.6). New explicit
  rules for timezone, currency, unknown cache TTL class, retry-vs-duplicate-delivery classification, baseline
  coverage gating, the Tier A/B cache stage split, price-increase exposure (§11.5), upload staging with a 25 MB
  limit, and exact-vs-declared-bound testing (§14). AGENTS.md gains the integer-money rule.

- **Design: the credential store is deferred to v1.1.** v1 resolves cloud identity from the host's
  ambient chain only and stores no secrets, so nothing in the system is encrypted, there is no crypto
  dependency, and §12's no-authentication posture is true rather than propped up by a bind interlock.
  An adversarial review found five of its significant remaining problems were in whether its *stated
  protections hold* — a plaintext config that steers a credential with nothing authenticating the
  destination (a SAS token, which travels in a URL, is exfiltrated outright), rollback detection whose
  counter lives in the file being rolled back, and an idle re-lock a running server cannot recover
  from — rather than in the plumbing. The design and all seven problems move to
  `docs/design/credential-store.md` as v1.1 blocking work. Removes the Credentials page, three API
  endpoints, the `credentials` CLI command, `--auth-token-file`, and the `keyring`/`pynacl`
  dependencies. SPEC.md §2, §4, §5.1, §6.1, §6.7, §12, §13.1–13.3, §15 updated.

### Added
- `docs/design/credential-store.md` — v1.1 credential store design and its unresolved security problems.
- Branching model (`develop` / `feature/*` / `hotfix/*`) and release process — see CONTRIBUTING.md.
- CI: ruff lint and format, strict mypy, pytest with coverage, and a price-literal guard.
- Release automation: readiness checks on the release PR, GitHub Release on a `v*` tag.

## [0.1.0] - unreleased

Design phase. No runtime code yet.

### Added
- SPEC.md — full v1 design for the cost and routing auditor.
- AGENTS.md — engineering guidelines and hard rules.
- README.md — project overview and intended usage.
