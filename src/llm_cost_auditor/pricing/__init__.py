"""The pricing engine (SPEC.md §7).

Analyzers never see a raw number. They call `rate()`, `multiplier()`, and
`cost_of()`, so a price change, a new broker, or a new discount class never
requires touching analyzer code (AGENTS.md).

**What this module refuses to do is the point of it.** It will not price a
model it has no row for, will not price a request at today's rate because the
rate for its own timestamp is missing, and will not treat an absent multiplier
as a free token class. Each of those raises `MissingPriceError`, which the
caller turns into a coverage entry — an unpriced model is excluded from savings
math and reported as such, because a guessed dollar figure is
indistinguishable from a real one once it reaches a slide.

The commercial overlay (§7.2) is the next slice. Until it lands, every figure
here is public list price, and a report built on it must say so.
"""

from __future__ import annotations

import functools
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..errors import MissingPriceError
from ..records import RequestRecord, Status
from .table import (
    DEFAULT_STALENESS_DAYS,
    TOKENS_PER_MTOK,
    Catalog,
    MultiplierKind,
    PriceRow,
    load,
)

__all__ = [
    "DEFAULT_STALENESS_DAYS",
    "Catalog",
    "MissingPriceError",
    "MultiplierKind",
    "PriceRow",
    "Rate",
    "RecordCost",
    "catalog",
    "cost_of",
    "multiplier",
    "rate",
    "token_counts_transferable",
]

# Token classes that are a *breakdown of* output tokens rather than an addition
# to them: both Anthropic and OpenAI already count reasoning tokens inside
# `output_tokens`. Pricing them again would inflate every reasoning-heavy
# workload by the size of its own thinking, which is the largest single
# double-count available in this data.
_INCLUDED_IN_OUTPUT = ("reasoning_tokens",)

# Token classes the per-MTok catalog genuinely cannot price. Each has its own
# provider-specific unit (§6.5.4), so a record carrying them is reported as
# partially priced rather than charged zero for the part we cannot value.
_UNPRICED = (
    "image_tokens",
    "audio_tokens",
    "video_tokens",
    "embedding_tokens",
)


class Rate(BaseModel):
    """The base rates in force for one provider/model/instant, with provenance.

    Deliberately narrow: multipliers are not exposed here, so a caller that
    wants a cache or batch discount has to go through `multiplier()` and cannot
    quietly read a raw field. The provenance travels with the rate because a
    figure whose catalog version and verification date got separated from it is
    a figure nobody can audit later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    input_usd_micros_per_mtok: int = Field(ge=0)
    output_usd_micros_per_mtok: int = Field(ge=0)
    effective_from: date
    effective_to: date | None
    last_verified: date
    source_url: str
    catalog_version: str

    # Not monetary, but catalog data all the same: AGENTS.md names the minimum
    # cacheable threshold and the breakpoint ceiling as price literals, so an
    # analyzer reads them here rather than writing 1024 into a heuristic.
    min_cacheable_tokens: int | None = None
    max_breakpoints: int | None = None

    # The tokenizer that produced the counts this rate prices. A token count is
    # only portable within one family: the same text tokenizes differently on
    # Claude and GPT, so multiplying one model's counts by another's rates is
    # wrong by whatever the two disagree by. Call `token_counts_transferable()`
    # before any cross-model comparison.
    tokenizer: str = ""

    # Present when the model reprices the whole request above a prompt size.
    long_context_threshold_tokens: int | None = None

    def age_days(self, today: date) -> int:
        return (today - self.last_verified).days

    def is_stale(self, today: date, threshold_days: int = DEFAULT_STALENESS_DAYS) -> bool:
        return self.age_days(today) > threshold_days


class RecordCost(BaseModel):
    """What one request cost, by token class, in integer μUSD.

    Every field is an `int` count of millionths of a dollar (§6.4) — a binary
    float never touches a dollar figure — and the JSON names carry the
    `_usd_micros` suffix so a round-trip cannot silently turn one into a float.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    total_usd_micros: int = Field(ge=0)
    by_class_usd_micros: dict[str, int] = Field(default_factory=dict)

    # §6.2: an undifferentiated cache-write total is priced at the *lowest*
    # premium class and the range is stated, rather than a TTL being guessed.
    # This is the extra cost if every one of those tokens were in fact the
    # highest class — the exposure a reader needs to see next to the total.
    ttl_unknown_exposure_usd_micros: int = Field(default=0, ge=0)

    # Token classes the record carries that this catalog cannot value. Non-empty
    # means the total is a lower bound, and the caller must say so.
    unpriced_token_classes: tuple[str, ...] = ()

    # True when the prompt crossed the model's long-context threshold and the
    # whole request was repriced at the higher tier. Stated because a reader
    # comparing two similar requests needs to know why one cost twice the other.
    long_context_applied: bool = False

    last_verified: date
    catalog_version: str

    @property
    def is_complete(self) -> bool:
        return not self.unpriced_token_classes


def token_counts_transferable(
    a: tuple[str, str], b: tuple[str, str], *, at: datetime, path: Path | None = None
) -> bool:
    """Whether one model's token counts may be priced with another's rates.

    Only within a tokenizer family. The same text tokenizes differently on
    Claude and on GPT, so a routing comparison that takes a logged Claude token
    count and multiplies it by a GPT rate is wrong by whatever the two
    tokenizers disagree by — silently, in the direction of whichever tokenizer
    is more compact, on every request at once.

    Crossing families needs the prompt re-tokenized with the target model's
    tokenizer, which requires Tier A content (§6.3) and is therefore impossible
    on billing-only logs. An analyzer that cannot re-tokenize must either stay
    inside a family or decline the comparison and say why.

    Each argument is `(provider, model)`.
    """
    left = _row(a[0], a[1], at, path)
    right = _row(b[0], b[1], at, path)
    return bool(left.tokenizer) and left.tokenizer == right.tokenizer


def catalog(path: Path | None = None) -> Catalog:
    """The loaded catalog. Parsed on first use and memoized, never at import."""
    return load(path)


@functools.lru_cache(maxsize=4096)
def _row(provider: str, model: str, at: datetime, path: Path | None) -> PriceRow:
    """Memoized per `(provider, model, timestamp)` exactly as §7.1 requires."""
    return load(path).find(provider, model, at)


def rate(provider: str, model: str, *, at: datetime, path: Path | None = None) -> Rate:
    """The base rates for this provider/model at this request's own timestamp."""
    row = _row(provider, model, at, path)
    return Rate(
        provider=row.provider,
        model=row.model,
        input_usd_micros_per_mtok=row.input_micros_per_mtok,
        output_usd_micros_per_mtok=row.output_micros_per_mtok,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        last_verified=row.last_verified,
        source_url=row.source_url,
        catalog_version=load(path).version,
        min_cacheable_tokens=row.min_cacheable_tokens,
        max_breakpoints=row.max_breakpoints,
        tokenizer=row.tokenizer,
        long_context_threshold_tokens=(
            None if row.long_context is None else row.long_context.threshold_tokens
        ),
    )


def multiplier(
    provider: str,
    model: str,
    kind: MultiplierKind | str,
    *,
    at: datetime,
    path: Path | None = None,
) -> Decimal:
    """A discount or premium factor, at the rate in force at `at`.

    Raises `MissingPriceError` when the model has no such class, rather than
    returning 1 (which would bill a discount at full price) or 0 (which would
    make a whole token class free).
    """
    return _row(provider, model, at, path).multiplier(MultiplierKind(kind))


def _amount(base_micros_per_mtok: int, factors: tuple[Decimal, ...], tokens: int) -> int:
    """Rate x factors x tokens, rounded to whole μUSD exactly once.

    All multipliers are applied to the rate in `Decimal` *before* the single
    rounding, which is what makes composition order irrelevant: batch-on-cache
    and cache-on-batch produce the same integer, so a fixture combining the two
    pins one answer rather than two nearly-equal ones.
    """
    effective = Decimal(base_micros_per_mtok)
    for factor in factors:
        effective *= factor
    exact = effective * tokens / TOKENS_PER_MTOK
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def cost_of(record: RequestRecord, *, path: Path | None = None) -> RecordCost:
    """Price one normalized record at list rates (§7.1, §11.1).

    An `error_unbilled` record costs nothing: normalization derives that status
    from having no billed usage in the first place (§6.5.2), so there is no
    case where a rejected attempt carries tokens to charge for.
    """
    row = _row(record.provider, record.model, record.start_time, path)
    usage = record.usage
    by_class: dict[str, int] = {}
    exposure = 0

    # Only the prompt counts toward a long-context threshold, so a long answer
    # to a short question stays on the base rate. Crossing it reprices the
    # *whole* request rather than only the excess tokens — that is how both
    # providers bill it, and the two readings differ by far more than rounding.
    prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    rates = row.rates_for(prompt_tokens)
    tiered = rates is not row

    if record.status is not Status.ERROR_UNBILLED:
        batch: tuple[Decimal, ...] = (
            (row.multiplier(MultiplierKind.BATCH, label=row.label),) if record.batch else ()
        )
        inp = rates.input_micros_per_mtok
        out = rates.output_micros_per_mtok

        by_class["input"] = _amount(inp, batch, usage.input_tokens)
        by_class["output"] = _amount(out, batch, usage.output_tokens)

        if usage.cache_read_tokens:
            read = rates.multiplier(MultiplierKind.CACHE_READ, label=row.label)
            by_class["cache_read"] = _amount(inp, (read, *batch), usage.cache_read_tokens)

        if usage.cache_write_5m_tokens:
            five = rates.multiplier(MultiplierKind.CACHE_WRITE_5M, label=row.label)
            by_class["cache_write_5m"] = _amount(inp, (five, *batch), usage.cache_write_5m_tokens)

        if usage.cache_write_1h_tokens:
            hour = rates.multiplier(MultiplierKind.CACHE_WRITE_1H, label=row.label)
            by_class["cache_write_1h"] = _amount(inp, (hour, *batch), usage.cache_write_1h_tokens)

        if usage.cache_write_unknown_ttl_tokens:
            classes = (
                rates.multiplier(MultiplierKind.CACHE_WRITE_5M, label=row.label),
                rates.multiplier(MultiplierKind.CACHE_WRITE_1H, label=row.label),
            )
            tokens = usage.cache_write_unknown_ttl_tokens
            cheapest = _amount(inp, (min(classes), *batch), tokens)
            dearest = _amount(inp, (max(classes), *batch), tokens)
            by_class["cache_write_unknown_ttl"] = cheapest
            exposure = dearest - cheapest

    unpriced = [name for name in _UNPRICED if getattr(usage, name)]

    return RecordCost(
        provider=row.provider,
        model=row.model,
        total_usd_micros=sum(by_class.values()),
        by_class_usd_micros=by_class,
        ttl_unknown_exposure_usd_micros=exposure,
        unpriced_token_classes=tuple(unpriced),
        long_context_applied=tiered,
        last_verified=row.last_verified,
        catalog_version=load(path).version,
    )
