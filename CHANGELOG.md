# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in `pyproject.toml` is bumped by hand in the release PR; CI blocks
the merge if it is unchanged or undocumented here.

## [Unreleased]

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
