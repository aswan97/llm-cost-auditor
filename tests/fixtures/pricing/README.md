# Hand-computed pricing fixtures

Every expected value in `test_pricing.py` is derived here, line by line, from
`catalog.yaml` and the token counts in the test. Nothing in this file was
produced by running the code it checks — that is the entire point. A fixture
whose expected value came out of the implementation proves only that the
implementation is self-consistent.

The tests assert these numbers **exactly**. No tolerance: a tolerance hides the
class of bug these fixtures exist to catch — a token class counted twice, a
multiplier applied to the wrong base, an off-by-one on a TTL boundary.

## The rates, in uUSD per token

`test-model`, in force `2026-01-01 .. 2026-06-30`:

| Class | Derivation | uUSD per token |
|---|---|---|
| input | $10.00/MTok | 10 |
| output | $40.00/MTok | 40 |
| cache read | 10 x 0.1 | 1 |
| cache write 5m | 10 x 1.25 | 12.5 |
| cache write 1h | 10 x 2.0 | 20 |
| batch | x 0.5 on whatever it composes with | — |

From `2026-07-01` the same model is $12.00 / $48.00, so input is 12 uUSD/token.

`test-cheap` is $0.05/MTok input, i.e. **0.05 uUSD per token** — a fraction of
the smallest unit money is stored in, which is what makes it the rounding case.

## 1. A cached request

`input 500, cache_read 2000, cache_write_5m 1000, output 100` at 2026-03-01.

```
input             500 x 10                = 5,000
cache read      2,000 x 10 x 0.1          = 2,000
cache write 5m  1,000 x 10 x 1.25         = 12,500
output            100 x 40                = 4,000
                                    total = 23,500
```

The uncached remainder bills at the **full** input rate, the read at the
discount, and the write at the premium — three different bases off one rate,
which is the arithmetic most easily got wrong.

## 2. A prefix below the minimum cacheable size

`min_cacheable_tokens` is 1000, and the request carries 800 input tokens with no
cache usage at all: below the threshold, caching was never configured and the
prefix bills as ordinary input.

```
input             800 x 10                = 8,000
output             50 x 40                = 2,000
                                    total = 10,000
```

The threshold itself is read from the catalog, never written into a heuristic.

## 3. A batch request

`input 1000, output 1000, batch=True`. The multiplier applies to **both**
directions, which is the half that gets forgotten.

```
input           1,000 x 10 x 0.5          = 5,000
output          1,000 x 40 x 0.5          = 20,000
                                    total = 25,000
```

## 4. Batch and cache together

`cache_read 1000, batch=True`. This pins multiplier **composition order**:

```
cache read      1,000 x 10 x 0.1 x 0.5    = 500
```

Both factors are applied to the rate before the single rounding, so
`batch(cache(rate))` and `cache(batch(rate))` are the same integer. The fixture
exists to keep it that way.

## 5. A failed call that still billed input

`status=error_billed, input 1000, output 0`:

```
input           1,000 x 10                = 10,000
                                    total = 10,000
```

## 6. A max_tokens truncation

Fully billed, output unusable — `status=truncated, input 1000, output 5000`:

```
input           1,000 x 10                = 10,000
output          5,000 x 40                = 200,000
                                    total = 210,000
```

## 7. A cancelled stream

Billed for what was generated before the disconnect — `input 1000, output 300`:

```
input           1,000 x 10                = 10,000
output            300 x 40                = 12,000
                                    total = 22,000
```

## 8. A retry pair

The two halves bill differently, and this is the most common source of
double-counting.

**429, rejected before generation** — `status=error_unbilled`, no usage:

```
                                    total = 0
```

**500 after generation** — both attempts billed, `input 1000, output 500` each:

```
per attempt     1,000 x 10 + 500 x 40     = 30,000
two attempts                              = 60,000
```

## 9. A price boundary

`input 1000` either side of the `effective_from` on 2026-07-01:

```
2026-06-30      1,000 x 10                = 10,000
2026-07-01      1,000 x 12                = 12,000
```

Each is priced at the rate in force at **its own** timestamp. A run spanning the
boundary contains both, and neither is repriced to match the other.

## 10. An unknown cache-write TTL class

`cache_write_unknown_ttl 1000`. Priced at the **lowest** premium class, with the
range stated rather than a TTL guessed (SPEC.md §6.2):

```
charged (5m)    1,000 x 10 x 1.25         = 12,500
if all 1h       1,000 x 10 x 2.0          = 20,000
exposure                                  = 7,500
```

## 11. A multimodal request

`input 1000, image 500, output 100`. The per-MTok catalog has no image rate, so
the text is priced and the image tokens are reported as an unpriced dimension —
the total is a lower bound and says so.

```
input           1,000 x 10                = 10,000
output            100 x 40                = 4,000
                                    total = 14,000   (unpriced: image_tokens)
```

Charging zero for the images would have produced the same total with no warning
attached, which is the difference between a lower bound and a wrong number.

## 12. Rounding, at half a uUSD

`test-cheap` input is 0.05 uUSD/token, so the total lands on an exact half and
the rounding mode becomes visible. Half-even, applied once:

```
10 tokens       10 x 0.05 = 0.5           -> 0    (ties to even)
30 tokens       30 x 0.05 = 1.5           -> 2    (ties to even)
1,000 tokens    1,000 x 0.05 = 50         -> 50   (no tie)
```

Half-up would give 1 and 2; banker's rounding gives 0 and 2. The pair is what
distinguishes them, which is why both are asserted rather than just one.

## 13. A long-context tier

`test-tiered` is $10.00 / $40.00 per MTok, and above **2000 prompt tokens** it
reprices to $20.00 / $60.00 — the same 2x input / 1.5x output shape both real
providers use.

Two rules do all the work here, and both are easy to get backwards.

**The threshold is exclusive, and only the prompt counts toward it.** Input,
cache reads and cache writes are prompt; output is not.

```
2,000 in + 100 out    2,000 x 10 + 100 x 40    = 24,000   (base: at, not above)
2,001 in + 100 out    2,001 x 20 + 100 x 60    = 46,020   (tier)
  100 in + 5,000 out    100 x 10 + 5,000 x 40  = 201,000  (base: output does not count)
```

**Crossing reprices the whole request, not the excess.** This is the single most
consequential way to get long-context billing wrong:

```
3,000 in   wholesale   3,000 x 20               = 60,000   <- correct
3,000 in   marginal    2,000 x 10 + 1,000 x 20  = 40,000   <- wrong, and plausible
```

A third less, with nothing in the output to suggest it. The test asserts the
wholesale figure *and* asserts the marginal one is not produced.

Cache tokens count toward the threshold and then take the multiplier off the
**tiered** input rate, not the base one:

```
2,500 cache read   2,500 x 20 x 0.1             = 5,000
  100 output         100 x 60                   = 6,000
                                          total = 11,000
```

## 14. Rounding is per token class, not per token

Worth stating because it is what makes fractional per-token rates exact. The
bundled `claude-sonnet-4-5` tier bills output at $22.50/MTok — 22.5 uUSD per
token, which is not representable in whole uUSD:

```
100 output tokens   22,500,000 uUSD/MTok x 100 / 1,000,000 = 2,250   exactly
```

Rounding once per class gives 2,250. Rounding per token would give 100 charges
of 22 or 23 and a total that depends on the rounding mode — which is why the
rate is held per MTok and the multiplication happens before the single
`quantize`.
