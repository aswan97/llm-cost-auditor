# AGENTS.md

LLM cost & routing auditor. Full design: [SPEC.md](SPEC.md). Python 3.12+.

## Engineering guidelines

### Don't over-engineer it early

SPEC.md describes the finished system. It is not a build order, and building it top-down will produce a lot of scaffolding before a single dollar figure exists. Build the narrow path first: ingest one source, price it, run one analyzer, print a report. Then widen.

Concretely, for this project:

- **v1 is cache + waste findings only** (SPEC §4). Batching and routing are specified so the foundations fit them later — not so they get stubbed now. No empty `routing/` package, no placeholder replay harness.
- **Seams, not implementations.** The `Store` seam exists so DuckDB can replace in-memory later; write the seam, keep polars in memory, don't write the DuckDB backend. Same for the Rust-portable hot paths — keep the interfaces narrow and pure, but they stay Python until profiling says otherwise.
- **The plugin analyzer interface earns its keep at three analyzers, not one.** Write the first analyzer as a plain module against the protocol; extract the registry when the second and third exist and the shape is known.
- **Prefer a function to a class, a dict to a model, and a module to a package** until something forces the upgrade. The exceptions are the data contracts — `RequestRecord`, `Finding`, config, price rows — which are pydantic models from day one because everything else is validated against them.
- **Two provider adapters before generalizing adapters.** The abstraction that fits Anthropic alone will be wrong for Bedrock.
- **Same for connectors: local, then S3, then extract the protocol** (SPEC §6.1). A local directory teaches you nothing about pagination, listing cost, credentials, or retries, so a `Connector` interface designed before S3 exists will be designed wrong. Azure Blob is written against the extracted protocol and is the test of whether it generalized. Connectors move bytes and know nothing about providers; adapters interpret records and know nothing about where they came from — anything named like `s3_anthropic_reader` is the seam collapsing.
- **No premature performance work.** In-memory at ~500k records is the stated target; optimize when a real dataset misses it, and profile before choosing what to change.
- **The engine before the app** (SPEC §4, §13). The web app is part of v1, but it renders run records — it cannot be built against findings that don't exist yet. Build the run store and one analyzer's real output first, then the pages. No mock data in templates, ever: a page that renders plausible fake numbers is the exact failure this project is trying to avoid.
- **The app is a driver, not a layer.** Every operation it performs is a CLI command first, and no analysis logic lives in a route handler, a template, or JavaScript. Server-rendered Jinja + HTMX; reach for a frontend framework only when a page has genuinely outgrown it, and say why.

When a piece of the spec turns out to be harder or less valuable than it looked, say so and adjust the spec — don't build an elaborate version of something that shouldn't exist.

### Test as a user would, and verify the result yourself

Green unit tests are necessary and not sufficient. This tool's failure mode is not a crash — it is producing a confident, plausible, wrong number. Nothing catches that except looking at the output.

For any meaningful change:

1. **Run the real CLI end to end** on a synthetic or sample log set — `ingest` → `profile` → `audit` — the same commands a user runs, with no test harness shortcuts and no monkeypatched internals.
2. **Read the report it produced.** Do the top findings make sense for that traffic? Is the ranking sane? Does the evidence support the claim? Would the remediation actually be actionable by someone who didn't write the analyzer?
3. **Verify at least one number by hand.** Take a finding, recompute its savings from the fixture's known token counts and the test catalog rates, and confirm it matches. A finding nobody has ever hand-checked is a guess with formatting.
4. **Check the boring parts too**: totals reconcile against baseline spend, marginal savings sum to the portfolio total, confidence tiers are what the evidence justifies, and the coverage panel honestly reports what was skipped.
5. **Try the degraded paths.** Run against billing-only logs and against a dataset with no config at all — those are the common real first runs, and they must produce something useful rather than an empty report or a stack trace. For ingest, that includes the broken-source paths: a permission-denied key, a truncated gzip, a prefix with nothing in the window. Each must land in the coverage panel naming what was missed — a run that quietly reports on the objects it happened to read is the worst possible outcome.
6. **If the change touches the app, drive the app** — `serve`, start a run from the browser, watch it progress, open the findings. Then confirm the numbers on screen match the exported report for the same run, figure by figure. Two surfaces over one run record means any discrepancy is a defect, and the only way to see it is to look at both.

Report what actually happened. If a run produced a suspicious number, say so with the output rather than moving on because the tests passed.

## Rules

### Branch from develop, never commit to main or develop

All work happens on `feature/*`, `fix/*`, `docs/*`, or `chore/*` branched from `develop`, and reaches `develop` through a PR. `main` is production and only ever receives a release PR from `develop` (or a `hotfix/*` from `main` itself, which must then be merged back to `develop`). Full model and release steps: [CONTRIBUTING.md](CONTRIBUTING.md).

Before starting work: `git checkout develop && git pull && git checkout -b feature/<slug>`. Rebase on `develop` rather than merging it in.

### A stored secret has exactly one exit

The credential store (SPEC §6.7) hands a secret to the connector that needs it and to nothing else. There is no read API, no CLI command that prints one, no template that renders one, no debug flag that logs one, and no test fixture that round-trips a real value. Treat every one of those as a defect, not a convenience.

Concretely:

- Credential objects mask their `__str__` and `__repr__`, so a secret cannot reach a log line, an exception message, or a traceback by accident.
- Nothing about a secret is written to `run.json`, `manifest.json`, `log.jsonl`, an event stream, a report, or a config file — those carry the `secret_ref` only.
- Secrets are read from stdin or a prompt, never from argv, and never from a query string.
- **Every change here needs a test that asserts the absence**, because absence is invisible in review: serialize a run record and a config with a credential attached and assert the value appears nowhere in the output; format the credential object and assert it is masked; call the API surface and assert no route returns it.

Adding a "just for debugging" print of a resolved credential is the kind of change that gets shipped and then found by someone else, so it does not get written in the first place.

### Don't write cryptography, and don't let it drift

The sealed store (SPEC §6.7) uses libsodium primitives — Argon2id, XChaCha20-Poly1305, HKDF-SHA-256, HMAC-SHA-256 — through one module. Rules that follow from that:

- **No primitive is implemented here**, and none is composed ad hoc elsewhere. Encrypting something new means calling that module, not importing a cipher.
- **Parameters travel with the ciphertext.** KDF cost, salt, nonce, and algorithm names live in the envelope header, never as an assumption about what the current build uses. A reader that encounters an unknown envelope version fails; it never infers one. Raising a cost parameter must leave existing stores openable, which is what `credentials rekey` is for.
- **Nothing is encrypted with a key stored next to it.** That is filing, not encryption, and it is worse than plaintext because it reads as safe.
- **Fail closed, always.** Authentication failure returns an error and no bytes — never partial plaintext, never a fallback path, never a "decrypt without verifying" branch for recovery.
- **Test the failures, not just the success.** Tamper with ciphertext, nonce, associated data, and header; use the wrong passphrase; swap a record between refs; roll back the index. Each must fail closed and say so. A round-trip test alone proves only that the code can talk to itself.
- **Never invent a threat claim.** The store protects a stolen file. It does not protect a compromised host or an unlocked process, and no doc, log line, or UI string should imply otherwise.

### Never write a price as a literal

No monetary rate, cost multiplier, or discount factor may appear as a literal anywhere outside the price table (§7.1). This includes:

- token prices (input, output, cache read/write, batch);
- discount and premium multipliers (`0.1`, `1.25`, `2.0`, `0.5` — cache-read, cache-write TTL classes, batch);
- minimum cacheable token thresholds and max breakpoint counts;
- "just for the test" values in test fixtures, docstrings, example configs, and report templates.

Always read them through the pricing module:

```python
rate = pricing.rate(provider, model, at=record.start_time)      # not: 3.00
mult = pricing.multiplier(provider, model, "cache_read", at=ts)  # not: 0.1
```

**Why:** prices change, differ per broker (Anthropic vs Bedrock vs Vertex vs Foundry rates diverge for the same model), and every request must be priced at the rate in force at *its own* timestamp. A literal anywhere silently desynchronizes from the catalog and corrupts savings math with no test failure. Tests use fixture catalogs, not inline numbers.

Prices load lazily and are memoized per `(provider, model, timestamp)` — never eager-load the catalog at import or CLI startup.

This rule is enforced in CI by `scripts/check_price_literals.py`, which fails the build on a numeric literal bound to a price-shaped name outside the pricing module. If something genuinely is not a price, append `# noqa: price-literal` with a justification rather than renaming around the check.

### Test against hand-computed fixtures, and assert exact equality

Every analyzer change must be validated against a small set of **synthetic log fixtures whose correct spend was computed by hand**, checked into `tests/fixtures/` alongside the expected totals. The test suite asserts the analyzer reproduces those numbers **exactly** — not "within tolerance". A tolerance hides exactly the class of bug these fixtures exist to catch: a token class counted twice, a multiplier applied to the wrong base, an off-by-one on a TTL boundary.

Keep the set small enough that a human can verify each expected value by inspection, and keep it weighted toward the cases where the arithmetic is easy to get wrong:

- **A cached request** — cache write billed at the TTL-appropriate premium on the first call, cache read at the discounted rate on subsequent calls, uncached remainder at full input rate. Include a request whose prefix is *below* the minimum cacheable threshold and therefore bills as if caching were never configured.
- **A batch request** — batch multiplier applied to both input and output, and a fixture combining batch *and* cache to pin down multiplier composition order.
- **A failed call that still billed input tokens** — `error_billed` with input charged and zero output; plus a `max_tokens` truncation (fully billed, output unusable) and a cancelled stream (billed for tokens generated before disconnect).
- **A retry pair** — one 429 retry (no billing on the rejected attempt) and one 500-after-generation retry (both attempts billed). These bill differently and are the most common source of double-counting.
- **A price-boundary request** — timestamped either side of an `effective_from` date, asserting each is priced at the rate in force at its own timestamp.
- **A multimodal request** — image/audio tokens under the provider's own accounting formula.

Fixtures declare their own pricing catalog (a test catalog, never the bundled one) so expected values stay stable when real prices change. Because equality is exact, money is computed in `Decimal` or integer micro-units end to end — never binary floats.

These fixtures complement, and do not replace, the generated synthetic corpus with planted inefficiencies described in SPEC.md §14.
