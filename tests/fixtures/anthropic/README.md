# `traffic.jsonl` — hand-computed ingest fixture

Fourteen raw log lines, weighted toward the cases where ingest arithmetic is
easy to get wrong (AGENTS.md). Every expected value in `expected.json` is
derived below by hand, so a human can check each one by inspection.

No monetary value appears here. Pricing is not in this release, so these
fixtures pin **record counts, classification, and token totals** — the inputs
every later dollar figure is computed from. The cache/batch/boundary pricing
fixtures AGENTS.md requires land with the pricing module.

The audit window used by the tests is **`2026-08-01..2026-08-31` UTC**.

## The lines, and what each one is for

| # | `request_id` | Why it is here | Expected classification |
|---|---|---|---|
| 1 | `req_ok_1` | Cache write with the per-TTL breakdown present | `ok`, `cache_write_5m=4200`, `ttl_class_unknown=false` |
| 2 | `req_ok_2` | Cache read with no write — reuse inside the TTL | `ok`, all writes zero |
| 3 | `req_unknown_ttl` | `cache_creation_input_tokens` with **no** breakdown | `ok`, `cache_write_unknown_ttl=3000`, `ttl_class_unknown=true` |
| 4a, 4b | `req_dup` ×2 | Identical billing facts — at-least-once log delivery | Collapsed to **one** record, `duplicate_delivery=true` |
| 5a | `req_retry_429` | Rate-limit rejection: no generation, no billing | `error_unbilled`, `attempt_index=0` |
| 5b | `req_retry_429` | The successful re-issue | `ok`, `attempt_index=1`, `parent_request_id` set |
| 6a | `req_retry_500` | 5xx **after** generation — billed in full | `error_billed`, `attempt_index=0` |
| 6b | `req_retry_500` | The successful re-issue | `ok`, `attempt_index=1` |
| 7 | `req_truncated` | `stop_reason=max_tokens` — billed, output unusable | `truncated` |
| 8 | `req_cancelled` | Client disconnect after generation started | `cancelled` |
| 9 | `req_tier_a` | Full request content present | Fidelity **A**, 4 segments, `usage_estimated=true` |
| 10 | `req_tier_b` | Pre-hashed segments with per-segment token counts | Fidelity **B**, 2 segments, `usage_estimated=false` |
| 11 | `req_out_of_window` | 2026-07-15, outside the August window | Dropped after decode |

Lines 5a/5b and 6a/6b are the pair that matters most: they share a
`request_id` but differ in billing facts, so collapsing them would understate
the bill by two whole attempts. Lines 4a/4b share a `request_id` *and* every
billing fact, so keeping both would overstate it. One rule, opposite outcomes
(SPEC.md §6.5.1).

## Hand arithmetic

**Record counts.**

```
raw lines                       14
  − 1 outside the window        13   (line 11)
  − 1 duplicate delivery        12   (line 4b collapsed into 4a)
records after normalize         12
```

**Status counts** — `ok` is lines 1, 2, 3, 4, 5b, 6b, 9, 10:

```
ok               8
error_unbilled   1   (5a)
error_billed     1   (6a)
truncated        1   (7)
cancelled        1   (8)
                ---
                12   ✓ matches the record count
```

**Fidelity mix.** Only lines 9 and 10 carry anything beyond billing metadata:

```
A (content)   1   (line 9)
B (hashed)    1   (line 10)
C (billing)  10   (lines 1, 2, 3, 4, 5a, 5b, 6a, 6b, 7, 8)
             ---
             12   ✓
```

The slice's tier is the **highest** it supports — A — and the mix is reported
next to it, because "tier A" over a dataset where one record in twelve carries
content would otherwise read as a claim about all twelve (SPEC.md §6.3).

**Input tokens**, summed over the 12 normalized in-window records:

```
1200 + 800 + 500 + 100 + 0 + 600 + 900 + 900 + 700 + 300 + 250 + 450
= 2000
+ 500 = 2500
+ 100 = 2600
+   0 = 2600
+ 600 = 3200
+ 900 = 4100
+ 900 = 5000
+ 700 = 5700
+ 300 = 6000
+ 250 = 6250
+ 450 = 6700
```

Note the `0` — line 5a's rejected 429 contributes nothing, which is the whole
point of classifying it `error_unbilled`.

**Output tokens:**

```
350 + 220 + 100 + 50 + 0 + 150 + 400 + 380 + 4096 + 120 + 90 + 130
=  570
+ 100 =  670
+  50 =  720
+   0 =  720
+ 150 =  870
+ 400 = 1270
+ 380 = 1650
+4096 = 5746
+ 120 = 5866
+  90 = 5956
+ 130 = 6086
```

**Cache tokens:**

```
cache_read           = 4000 (line 1) + 4000 (line 2)          = 8000
cache_write_5m       = 4200 (line 1)                          = 4200
cache_write_1h       =                                          0
cache_write_unknown  = 3000 (line 3)                          = 3000
```

`req_out_of_window` contributes `9999 + 9999` to nothing — if either total
moves by 9999, the window filter has stopped working.

## Redaction

Line 9 carries `user_id: "analyst@acme.example"` and a tag
`contact: "ops@acme.example"`. Both are email-shaped and are redacted before
storage (SPEC.md §12 layer 2), so `[redacted]` is the expected stored value and
neither address may appear anywhere in `records.parquet`, `run.json`,
`manifest.json`, or `log.jsonl`.

Line 9's prompt text is hashed and dropped: the four segment hashes are stored,
the text never is (§12 layer 1).
