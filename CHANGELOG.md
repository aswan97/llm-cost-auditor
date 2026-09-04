# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in `pyproject.toml` is bumped by hand in the release PR; CI blocks
the merge if it is unchanged or undocumented here.

## [Unreleased]

### Changed
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
