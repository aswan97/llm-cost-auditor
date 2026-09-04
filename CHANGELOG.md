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

### Added
- Branching model (`develop` / `feature/*` / `hotfix/*`) and release process — see CONTRIBUTING.md.
- CI: ruff lint and format, strict mypy, pytest with coverage, and a price-literal guard.
- Release automation: readiness checks on the release PR, GitHub Release on a `v*` tag.

## [0.1.0] - unreleased

Design phase. No runtime code yet.

### Added
- SPEC.md — full v1 design for the cost and routing auditor.
- AGENTS.md — engineering guidelines and hard rules.
- README.md — project overview and intended usage.
