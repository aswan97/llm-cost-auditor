"""Baseline spend over a run's records (SPEC.md §11.1).

This module exists because there are two surfaces. The CLI prints these numbers
and the web app renders them, and AGENTS.md is explicit that the app is a driver
rather than a layer: no analysis lives in a route handler or a template. If the
aggregation lived in either one, the other would grow its own copy, and two
copies of a money calculation disagree eventually — silently, and in a way that
looks like a rounding difference rather than a bug.

So the arithmetic happens exactly once, here, and both surfaces format the same
`Baseline`. A discrepancy between them is then impossible by construction rather
than by discipline.

Read-only: computing a baseline starts no stage and writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import pricing
from .errors import MissingPriceError
from .pricing.table import MICROS_PER_USD
from .records import RequestRecord


def format_usd(micros: int) -> str:
    """Format integer micro-USD as dollars, for display only.

    The one place μUSD becomes a string, shared by both surfaces so neither can
    round differently from the other. Never parsed back (AGENTS.md).

    An amount that is real but smaller than a cent renders `<$0.01` rather than
    `$0.00`. Two decimal places is the right precision for a bill and the wrong
    one for a single finding on a short window, and a figure that reads as
    exactly nothing when it is not is the same class of quiet wrongness this
    tool exists to catch — a reader skips the row instead of noticing the unit.
    """
    rendered = f"${micros / MICROS_PER_USD:,.2f}"
    if micros and rendered in ("$0.00", "$-0.00"):
        return "<$0.01" if micros > 0 else ">-$0.01"
    return rendered


class ModelSpend(BaseModel):
    """What one provider/model cost across a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    records: int = Field(ge=0)
    spend_usd_micros: int = Field(ge=0)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def spend(self) -> str:
        return format_usd(self.spend_usd_micros)


class Excluded(BaseModel):
    """Records left out of the total, and why they could not be priced.

    The reason is carried rather than flattened into prose because the two cases
    need different fixes: an unknown model means the catalog never heard of it,
    an uncovered timestamp means a historical row is missing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    reason: str
    records: int = Field(ge=0)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def is_unknown_model(self) -> bool:
        return self.reason == MissingPriceError.UNKNOWN_MODEL

    @property
    def explanation(self) -> str:
        if self.is_unknown_model:
            return "no catalog row for that model at all. Excluded entirely."
        return (
            "the model is priced, but no row covers their timestamps. "
            "A historical rate is missing, not the model. Excluded."
        )


class Baseline(BaseModel):
    """Total spend for a run, with everything the total does not include.

    The exclusions are part of the result rather than a warning printed beside
    it, because a total that has quietly dropped records is the failure this
    tool exists to prevent. A caller cannot render the number without also
    having been handed what is missing from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: str
    oldest_verification: date | None = None

    total_usd_micros: int = Field(default=0, ge=0)
    priced_records: int = Field(default=0, ge=0)
    by_model: tuple[ModelSpend, ...] = ()

    ttl_unknown_exposure_usd_micros: int = Field(default=0, ge=0)
    partially_priced: tuple[tuple[str, int], ...] = ()
    excluded: tuple[Excluded, ...] = ()

    @property
    def total(self) -> str:
        return format_usd(self.total_usd_micros)

    @property
    def ttl_unknown_exposure(self) -> str:
        return format_usd(self.ttl_unknown_exposure_usd_micros)

    @property
    def excluded_records(self) -> int:
        return sum(item.records for item in self.excluded)

    @property
    def is_complete(self) -> bool:
        """Whether the total covers every record, priced on every dimension."""
        return not self.excluded and not self.partially_priced

    @property
    def has_caveats(self) -> bool:
        return bool(self.excluded or self.partially_priced or self.ttl_unknown_exposure_usd_micros)


def compute(records: Iterable[RequestRecord], *, path: Path | None = None) -> Baseline:
    """Price every record and aggregate, at public list prices (§7.1, §11.1).

    A record whose model has no rate is counted and named rather than priced at
    zero. Excluding it from the total is the honest choice; charging it nothing
    would move the same record into the total at a rate nobody chose.
    """
    total = 0
    exposure = 0
    priced = 0
    spend: dict[tuple[str, str], list[int]] = {}
    excluded: dict[tuple[str, str, str], int] = {}
    partial: dict[str, int] = {}
    oldest: date | None = None

    for record in records:
        key = (record.provider, record.model)
        try:
            cost = pricing.cost_of(record, path=path)
        except MissingPriceError as exc:
            gap = (record.provider, record.model, exc.reason)
            excluded[gap] = excluded.get(gap, 0) + 1
            continue

        priced += 1
        total += cost.total_usd_micros
        exposure += cost.ttl_unknown_exposure_usd_micros
        bucket = spend.setdefault(key, [0, 0])
        bucket[0] += cost.total_usd_micros
        bucket[1] += 1
        for name in cost.unpriced_token_classes:
            partial[name] = partial.get(name, 0) + 1
        oldest = cost.last_verified if oldest is None else min(oldest, cost.last_verified)

    return Baseline(
        catalog_version=pricing.catalog(path).version,
        oldest_verification=oldest,
        total_usd_micros=total,
        priced_records=priced,
        # Descending spend: the number a reader acts on is the largest one, and
        # it should not have to be found.
        by_model=tuple(
            ModelSpend(provider=p, model=m, records=count, spend_usd_micros=amount)
            for (p, m), (amount, count) in sorted(spend.items(), key=lambda kv: -kv[1][0])
        ),
        ttl_unknown_exposure_usd_micros=exposure,
        partially_priced=tuple(sorted(partial.items())),
        excluded=tuple(
            Excluded(provider=p, model=m, reason=reason, records=count)
            for (p, m, reason), count in sorted(excluded.items())
        ),
    )
