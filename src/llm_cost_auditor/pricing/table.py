"""The price table: parsing, validation, and lookup (SPEC.md §7.1).

Three rules from the spec and AGENTS.md shape everything here.

**`Decimal` parses, integers do arithmetic.** Catalog rates are decimal strings
so they survive YAML without becoming binary floats. They are parsed to
`Decimal` once, at this boundary, and converted to an integer count of μUSD per
MTok. Nothing downstream sees a `Decimal` rate or a `float` anywhere.

**Multipliers stay `Decimal`.** They are ratios, not money, and applying them to
the base rate before the single rounding is what keeps composed discounts
(batch on top of cache) exact rather than twice-rounded.

**`null` is not zero.** A missing multiplier means the provider does not sell
that token class — OpenAI has no cache *write* to bill — so a lookup for it
fails loudly. Defaulting it to zero would price a whole token class at nothing
and look like a saving.
"""

from __future__ import annotations

import functools
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ConfigError, MissingPriceError

DATA = Path(__file__).parent / "data"
DEFAULT_CATALOG = DATA / "catalog.yaml"

# Unit conversions, not prices: a dollar is a million μUSD (§6.4) and an MTok is
# a million tokens. Both are definitions, so they are the one kind of number
# that is allowed to be written down.
MICROS_PER_USD = 1_000_000
TOKENS_PER_MTOK = 1_000_000

# How stale a row may be before findings that use it carry a warning (§7.1).
DEFAULT_STALENESS_DAYS = 90


class MultiplierKind(StrEnum):
    """The discount and premium classes a row can carry (§7.1)."""

    CACHE_READ = "cache_read"
    CACHE_WRITE_5M = "cache_write_5m"
    CACHE_WRITE_1H = "cache_write_1h"
    BATCH = "batch"


class PriceRow(BaseModel):
    """One provider/model/period rate, as stored in the catalog.

    Frozen because a row is a fact about a past instant: a request is priced at
    the rate in force at *its own* timestamp, and a mutable row is how one run
    ends up pricing two identical requests differently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    provider: str
    effective_from: date
    effective_to: date | None = None
    currency: str = "USD"
    units: str = "per_mtok"

    input: Decimal
    output: Decimal

    cache_read_multiplier: Decimal | None = None
    cache_write_5m_multiplier: Decimal | None = None
    cache_write_1h_multiplier: Decimal | None = None
    batch_multiplier: Decimal | None = None

    min_cacheable_tokens: int | None = Field(default=None, ge=0)
    max_breakpoints: int | None = Field(default=None, ge=0)

    # Some models bill a higher rate once the input context passes a threshold.
    # This row carries no tier rates, so the threshold exists only to *refuse*:
    # a request above it is reported as partially priced rather than charged at
    # the base rate, which would understate the largest requests by the width of
    # the tier. Modelling the tiers properly is a schema change, and it should
    # wait until more than one model needs it.
    long_context_threshold_tokens: int | None = Field(default=None, ge=0)

    last_verified: date
    source_url: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.currency != "USD":
            raise ValueError(
                f"{self.provider}/{self.model}: currency is {self.currency}. The catalog is "
                f"USD-only in v1 and no exchange rate is applied anywhere (SPEC.md §7.1)."
            )
        if self.units != "per_mtok":
            raise ValueError(
                f"{self.provider}/{self.model}: units {self.units!r} are not supported. "
                f"Per-image and per-second units are a v1.1 question (SPEC.md §6.5.4)."
            )
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(f"{self.provider}/{self.model}: effective_to precedes effective_from.")
        return self

    @property
    def input_micros_per_mtok(self) -> int:
        return _to_micros(self.input)

    @property
    def output_micros_per_mtok(self) -> int:
        return _to_micros(self.output)

    def covers(self, moment: datetime) -> bool:
        """Whether this row is the rate in force at `moment`.

        `effective_to` is inclusive of its whole day, because a provider
        announces "prices change on the 1st", not "at 00:00:00 on the 1st".
        """
        day = moment.date()
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to

    def multiplier(self, kind: MultiplierKind) -> Decimal:
        value = getattr(self, f"{kind.value}_multiplier")
        if value is None:
            raise MissingPriceError(
                f"{self.provider}/{self.model} has no {kind.value} rate: the provider does "
                f"not sell that token class for this model, so there is nothing to price it "
                f"at. This is reported as an unpriced dimension, never charged as zero."
            )
        return Decimal(value)

    def age_days(self, today: date) -> int:
        return (today - self.last_verified).days


def _to_micros(dollars: Decimal) -> int:
    """Dollars per MTok to μUSD per MTok, exactly.

    Catalog rates are given to at most six decimal places, so this is a shift
    rather than a rounding; a rate finer than a μUSD is rejected rather than
    quietly truncated, because that silently changes every figure derived from
    it.
    """
    scaled = dollars * MICROS_PER_USD
    if scaled != scaled.to_integral_value():
        raise ConfigError(
            f"rate {dollars} is finer than one μUSD per MTok and cannot be stored exactly "
            f"(SPEC.md §6.4). Prices are integers of μUSD end to end."
        )
    return int(scaled)


class Catalog(BaseModel):
    """A parsed price table, indexed for lookup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    rows: tuple[PriceRow, ...]
    path: Path

    def find(self, provider: str, model: str, at: datetime) -> PriceRow:
        """The row in force for this provider/model at this instant.

        Raises rather than returning `None`: the caller turns a missing price
        into a coverage entry and excludes the model from savings math (§7.1).
        A guessed number would be indistinguishable from a real one.
        """
        candidates = [row for row in self.rows if row.provider == provider and row.model == model]
        if not candidates:
            known = sorted({r.model for r in self.rows if r.provider == provider})
            hint = f" Known {provider} models: {', '.join(known)}." if known else ""
            raise MissingPriceError(
                f"no price row for {provider}/{model} in catalog {self.version}.{hint}",
                reason=MissingPriceError.UNKNOWN_MODEL,
            )

        covering = [row for row in candidates if row.covers(at)]
        if not covering:
            spans = ", ".join(f"{r.effective_from}..{r.effective_to or 'open'}" for r in candidates)
            raise MissingPriceError(
                f"{provider}/{model} has rows in catalog {self.version}, but none covers "
                f"{at.date()} (have: {spans}). A request is priced at the rate in force at "
                f"its own timestamp, so this is not priced at today's rate instead.",
                reason=MissingPriceError.NO_ROW_FOR_TIMESTAMP,
            )
        if len(covering) > 1:
            raise ConfigError(
                f"{provider}/{model} has {len(covering)} overlapping rows covering "
                f"{at.date()} in {self.path}. Effective periods must not overlap."
            )
        return covering[0]

    def oldest_verification(self) -> date:
        return min(row.last_verified for row in self.rows)


def _parse(path: Path) -> Catalog:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"price catalog {path} could not be read: {exc}") from exc
    if not isinstance(raw, dict) or "rows" not in raw:
        raise ConfigError(f"price catalog {path} has no `rows` key.")
    try:
        rows = tuple(PriceRow.model_validate(row) for row in raw["rows"])
    except Exception as exc:  # pydantic validation, surfaced with the file name
        raise ConfigError(f"price catalog {path}: {exc}") from exc
    return Catalog(version=str(raw.get("version", "unknown")), rows=rows, path=path)


@functools.lru_cache(maxsize=8)
def load(path: Path | None = None) -> Catalog:
    """Load and memoize a catalog.

    Lazy by construction: nothing calls this at import or at CLI startup, so a
    run that prices four models never parses the rest of the table (§7.1).
    """
    return _parse(path or DEFAULT_CATALOG)
