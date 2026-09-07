# `traffic.jsonl` — hand-computed waste-analyzer fixture

Twelve raw log lines and one test catalog, weighted toward the cases where
waste arithmetic is easy to get wrong (AGENTS.md). Every expected value in
`expected.json` is derived below by hand, and the tests assert them **exactly**
— not within a tolerance, because a tolerance hides exactly the class of bug
this fixture exists to catch: a record counted by two findings, a superseded
attempt credited twice, an off-by-one on which attempt in a chain was billed.

The audit window is **`2026-08-01..2026-08-31` UTC** and the catalog is
`catalog.yaml`, never the bundled one.

## The rates, so every number below is one multiplication

| Token class | Rate | Per token |
|---|---|---|
| input | `$10.00`/MTok | **10 μUSD** |
| output | `$40.00`/MTok | **40 μUSD** |

No caching, no batching, and one price period, because this fixture is about
*attribution*, not about pricing — the composition of multipliers is pinned
exactly once, in `tests/fixtures/pricing/`.

## The lines, and what each is for

| # | `request_id` | Why it is here | Classification | Cost (μUSD) |
|---|---|---|---|---|
| 1 | `w1_ok` | Ordinary traffic, so the baseline is not all waste | `ok` | 100×10 + 10×40 = **1400** |
| 2 | `w1_billed_fail` | A billed failure **outside** any retry chain | `error_billed` | 200×10 + 0 = **2000** |
| 3a | `w1_retry` | 5xx **after** generation — billed, then superseded | `error_billed`, attempt 0 | 300×10 + 50×40 = **5000** |
| 3b | `w1_retry` | The successful re-issue | `ok`, attempt 1 | 300×10 + 60×40 = **5400** |
| 4a | `w1_429` | Rate-limited before generation — free, but a wasted round trip | `error_unbilled`, attempt 0 | **0** |
| 4b | `w1_429` | The successful re-issue | `ok`, attempt 1 | 150×10 + 20×40 = **2300** |
| 5 | `w1_truncated` | `stop_reason=max_tokens` — billed in full, answer unusable | `truncated` | 400×10 + 500×40 = **24000** |
| 6a, 6b | `w1_dup` ×2 | Identical billing facts — at-least-once delivery | Collapsed to one `ok` | 50×10 + 5×40 = **700** |
| 7 | `w2_cancelled` | Client disconnect after generation started | `cancelled` | 250×10 + 30×40 = **3700** |
| 8 | `w2_billed_fail` | A billed failure in the *unlabelled* workload | `error_billed` | 80×10 + 0 = **800** |
| 9 | `out_of_window` | 2026-07-10, outside the August window | Dropped after decode | — |

Lines 2 and 3a are the pair that matters most. Both are `error_billed`, but
only 3a sits inside a retry chain — so `waste.billed_failure` and
`waste.retry_storm` both name 3a, and only one of them may be credited with its
dollars (§11.2). Line 2 is what proves the *rest* of the billed-failure finding
survives that: if marginal attribution were subtracting a whole finding rather
than the overlapping records, line 2's 2000 μUSD would vanish with 3a's.

Lines 4a/4b are the mirror image: a genuine retry chain in which the superseded
attempt cost nothing, so `waste.retry_storm` must claim **nothing** from it. A
detector that claims every superseded attempt rather than every *billed* one
would still produce a finding here, and it would be worth `$0` — a finding that
is only visible as an extra row nobody can explain.

## Records after normalization

```
raw lines                       12
  − 1 outside the window        11   (line 9)
  − 1 duplicate delivery        10   (line 6b collapsed into 6a)
records analyzed                10
```

## Baseline spend

Every in-window record, priced at its own timestamp:

```
1400   (1)
2000   (2)
5000   (3a)
5400   (3b)
   0   (4a)   ← the rejected 429 contributes nothing, which is the point
2300   (4b)
24000  (5)
 700   (6)
3700   (7)
 800   (8)
-----
45300 μUSD   = $0.0453
```

## Workloads (§8.2 step 2)

Grouping is by declared labels. Lines 1–6 carry `project: claims` and
`tag.service: extract`, so their workload id is `claims/extract`. Lines 7 and 8
carry no grouping labels at all and land in `unmapped`.

```
claims/extract    8 records   (1, 2, 3a, 3b, 4a, 4b, 5, 6)
unmapped          2 records   (7, 8)
                 --
                 10   ✓ matches the record count
```

`unmapped` is 2/10 = **20%** of traffic, which the profile reports rather than
hides.

## The findings, and the attribution between them

Detectors are applied in the fixed order `retry_storm → billed_failure →
truncation → cancelled_stream → rate_limit_churn` (§11.2), and each is credited
only with the records no higher-ranked finding already claimed.

**1. `waste.retry_storm.claims/extract`** — chains with more than one attempt
are `w1_retry` and `w1_429`. Superseded attempts that were *billed*: 3a only.

```
standalone = marginal = 5000
```

**2. `waste.billed_failure.claims/extract`** — `error_billed` in this workload
is lines 2 and 3a.

```
standalone = 2000 + 5000 = 7000
marginal   = 7000 − 5000 = 2000     (3a already credited to retry_storm)
```

**3. `waste.billed_failure.unmapped`** — line 8 only, claimed by nothing above.

```
standalone = marginal = 800
```

**4. `waste.truncation.claims/extract`** — line 5. The `low` band assumes the
truncated answer was accepted as-is and nothing is recovered; `expected` and
`high` assume it was asked for again. Nothing above claims line 5.

```
standalone = marginal = low 0 / expected 24000 / high 24000
```

**5. `waste.cancelled_stream.unmapped`** — line 7.

```
standalone = marginal = 3700
```

**6. `waste.rate_limit_churn.claims/extract`** — line 4a, which cost nothing.

```
standalone = marginal = 0
```

## The portfolio total, two ways

Summing the marginals:

```
24000  (truncation)
 5000  (retry_storm)
 3700  (cancelled_stream)
 2000  (billed_failure, claims/extract)
  800  (billed_failure, unmapped)
    0  (rate_limit_churn)
-----
35500 μUSD
```

Independently, the cost of the **union** of every claimed record, counted once:

```
w1_retry#0        5000
w1_billed_fail#0  2000
w2_billed_fail#0   800
w1_truncated#0   24000
w2_cancelled#0    3700
w1_429#0             0
-----
35500 μUSD   ✓
```

The two agreeing **exactly** is the §11.2 invariant. It is achievable only in
integers, which is why money is μUSD end to end (§6.4) — in floats this becomes
an approximate comparison, which is the same as no test at all.

Note what the total is *not*: the sum of standalone values is
`5000 + 7000 + 800 + 24000 + 3700 + 0 = 40500`, which double-counts line 3a.
That 5000 μUSD gap between 40500 and 35500 is exactly what naive summation
would have overstated the report by — on a fixture of ten records.

## Confidence tiers (§11.3)

```
Measured    5000 + 3700 + 2000 + 800 + 0 = 11500
Estimated                          24000 = 24000
                                          -----
                                          35500   ✓
```

Only `waste.truncation` is below `Measured`, and it says why in a
`confidence_penalty` rather than in prose: whether a truncated response was
re-requested is not observable at billing fidelity.
