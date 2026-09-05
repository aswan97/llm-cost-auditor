# Contributing

## Branching model

```
main ────────────●───────────────────────●──────────  production, tagged releases
                 ↑                       ↑
            release PR              release PR
                 │                       │
develop ─●───●───●───●───●───●───●───●───●──────────  integration branch, always green
          ↑       ↑       ↑           ↑
      feature/  feature/ fix/     hotfix/ ──→ also merged back to develop
```

| Branch | Purpose | Merges from | Merges to |
|---|---|---|---|
| `main` | Production. Every commit is a tagged release. | `develop`, `hotfix/*` | — |
| `develop` | Integration. All development lands here first and it must stay green. | `feature/*`, `fix/*`, `chore/*`, `docs/*` | `main` (release PR) |
| `feature/<slug>` | One unit of work. | `develop` | `develop` |
| `fix/<slug>` | Non-urgent bug fix. | `develop` | `develop` |
| `hotfix/<x.y.z>` | Urgent production fix that cannot wait for the next release. | `main` | `main` **and** `develop` |
| `docs/<slug>`, `chore/<slug>` | Documentation and tooling. | `develop` | `develop` |

**Nobody commits directly to `main` or `develop`.** Both take changes through pull requests only.

### Day-to-day flow

```bash
git checkout develop && git pull
git checkout -b feature/prefix-cache-analyzer

# ... work, commit ...

git push -u origin feature/prefix-cache-analyzer
# open a PR into develop
```

Rebase on `develop` rather than merging it into your branch, so history stays linear and the diff stays honest about what you changed.

## CI gates

Every PR into `develop` or `main` must pass, on Python 3.12 and 3.13:

| Gate | What it enforces |
|---|---|
| **ruff** | `ruff check` and `ruff format --check` |
| **mypy** | Strict mode over `src/` |
| **pytest** | Tests with a coverage floor (80%), including the hand-computed fixture suite (AGENTS.md) |
| **price-literal guard** | `scripts/check_price_literals.py` — fails the build on a numeric price, rate, or multiplier bound outside the pricing module |

The gates run unconditionally — the self-skip guards that covered the empty
scaffold were removed with the first module.

Docker is the reference environment for running and testing the platform, and
it pins the same Python 3.12 as the lower CI matrix leg:

```bash
docker compose run --rm test           # ruff, mypy, and the test suite
docker compose run --rm cli <args...>  # any CLI command
docker compose up app                  # http://127.0.0.1:8787
```

## Release process (develop → main)

Releases are cut from `develop` via a release PR. There is no `release/*` branch
by default; if a release ever needs stabilization time while feature work
continues, cut one then and treat it as a temporary `develop`.

1. **Freeze.** Stop merging into `develop` once you decide to release. Confirm CI on `develop` is green.
2. **Prepare.** On a branch off `develop` (or directly in the release PR):
   - bump `version` in `pyproject.toml` (SemVer — breaking / feature / fix);
   - move the `Unreleased` entries in `CHANGELOG.md` into a `## [x.y.z] - YYYY-MM-DD` section.
3. **Open the release PR: `develop` → `main`.** The `Release check` workflow verifies the source branch, that the version was actually bumped past the latest tag, and that the CHANGELOG documents it.
4. **Verify like a user** (AGENTS.md): run the built CLI end to end on sample logs, read the report, hand-check one savings figure, then `serve` and confirm the app shows the same numbers for the same run. Green tests alone do not qualify a release of a tool whose failure mode is a confident wrong number.
5. **Merge** the PR into `main` (merge commit, not squash — `main` and `develop` must share history).
6. **Tag `main`:**
   ```bash
   git checkout main && git pull
   git tag -a v0.2.0 -m "Release v0.2.0"
   git push origin v0.2.0
   ```
   The `Release` workflow verifies the tag is on `main` and matches `pyproject.toml`, builds the sdist and wheel, extracts the notes from `CHANGELOG.md`, and publishes a GitHub Release with the artifacts attached.
7. **Sync `develop`:**
   ```bash
   git checkout develop && git merge --no-ff main && git push
   ```

### Hotfixes

```bash
git checkout main && git pull
git checkout -b hotfix/0.2.1
# fix, bump patch version, add CHANGELOG entry
# PR into main → merge → tag v0.2.1 → merge main back into develop
```

A hotfix that never reaches `develop` will be silently reverted by the next
release. Step 7 is not optional.

## Branch protection (configure once, in GitHub settings)

These rules are what make the model above real rather than advisory. Under
**Settings → Branches**, protect both branches:

**`main`**
- Require a pull request before merging (1 approval).
- Require status checks to pass: `Lint, types, tests (py3.12)`, `Lint, types, tests (py3.13)`, `Price-literal guard`, `Release readiness`.
- Require branches to be up to date before merging.
- Restrict who can push; block force pushes and deletions.

**`develop`**
- Require a pull request before merging.
- Require status checks to pass: `Lint, types, tests (py3.12)`, `Lint, types, tests (py3.13)`, `Price-literal guard`.
- Block force pushes and deletions.

With the `gh` CLI installed, the same settings can be applied from the command line:

```bash
gh api -X PUT repos/:owner/:repo/branches/develop/protection \
  -f "required_status_checks[strict]=true" \
  -f "required_status_checks[contexts][]=Price-literal guard" \
  -F "enforce_admins=false" -F "restrictions=null" \
  -f "required_pull_request_reviews[required_approving_review_count]=0"
```

## Commits

Present tense, explain *why* where it isn't obvious from the diff. Reference the
spec section a change implements (e.g. "implements SPEC.md §9.2") so the design
and the code stay tied together.
