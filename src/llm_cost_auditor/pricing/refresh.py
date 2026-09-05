"""Drift detection against a public pricing feed (SPEC.md §7.1).

This module **never writes a rate**. It fetches a public catalog, derives the
same figures our rows hold, and reports where the two disagree. A human then
checks the disagreement against the provider's own page — the `source_url` on
the row — and edits the catalog by hand.

That asymmetry is the whole design. An aggregator is the right tool for
noticing that a price moved and the wrong tool for deciding what it moved to:
a single upstream typo adopted automatically would repriced every finding in
every report at once, with `last_verified` stamped fresh to say a human had
checked. The failure would be invisible in review and confident in output,
which is the exact failure this project exists to catch.

Network access lives here and nowhere else in the pricing engine. A normal
`audit` run fetches nothing (§7.1), and the web app never calls this at all.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..errors import AuditorError
from .table import Catalog, PriceRow

FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
FEED_NAME = "LiteLLM model_prices_and_context_window.json"

# The feed quotes dollars per token; the catalog quotes dollars per MTok.
_TOKENS_PER_MTOK = 1_000_000


class Drift(BaseModel):
    """One disagreement between a catalog row and the feed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    field: str
    catalog: str
    feed: str
    source_url: str

    def describe(self) -> str:
        return (
            f"{self.provider}/{self.model} {self.field}: catalog {self.catalog} vs feed {self.feed}"
        )


class Report(BaseModel):
    """What a check found, including what it could not check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: str
    feed_name: str
    rows_checked: int
    drifts: tuple[Drift, ...] = ()
    unmatched: tuple[str, ...] = ()
    unverifiable: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.drifts


def fetch(url: str = FEED_URL, *, timeout: int = 30) -> dict[str, Any]:
    """Fetch the feed. The only outbound call in the pricing engine."""
    request = urllib.request.Request(url, headers={"User-Agent": "llm-cost-auditor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AuditorError(
            f"could not fetch the pricing feed from {url}: {exc}. This is a maintenance "
            f"command; no audit depends on it, so the catalog is unaffected."
        ) from exc
    if not isinstance(payload, dict):
        raise AuditorError(f"pricing feed at {url} is not a JSON object.")
    return payload


def _plain(value: Decimal) -> str:
    """A decimal a person can read: no trailing zeros, and never an exponent.

    `Decimal("20.00").normalize()` is `2E+1` — a correct number and a terrible
    thing to print beside another number in a drift report, where the whole job
    is letting someone see at a glance which one moved.
    """
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


def _per_mtok(value: Any) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(str(value)) * _TOKENS_PER_MTOK).normalize()


def _ratio(numerator: Any, denominator: Decimal) -> Decimal | None:
    """A feed multiplier, derived from its absolute price the way ours was."""
    absolute = _per_mtok(numerator)
    if absolute is None or denominator == 0:
        return None
    return (absolute / denominator).normalize()


def _compare_row(row: PriceRow, entry: dict[str, Any]) -> list[Drift]:
    base = row.input
    checks: list[tuple[str, Decimal | None, Decimal | None]] = [
        ("input", row.input.normalize(), _per_mtok(entry.get("input_cost_per_token"))),
        ("output", row.output.normalize(), _per_mtok(entry.get("output_cost_per_token"))),
        (
            "cache_read_multiplier",
            None if row.cache_read_multiplier is None else row.cache_read_multiplier.normalize(),
            _ratio(entry.get("cache_read_input_token_cost"), base),
        ),
        (
            "cache_write_5m_multiplier",
            None
            if row.cache_write_5m_multiplier is None
            else row.cache_write_5m_multiplier.normalize(),
            _ratio(entry.get("cache_creation_input_token_cost"), base),
        ),
        (
            "cache_write_1h_multiplier",
            None
            if row.cache_write_1h_multiplier is None
            else row.cache_write_1h_multiplier.normalize(),
            _ratio(entry.get("cache_creation_input_token_cost_above_1hr"), base),
        ),
    ]

    drifts: list[Drift] = []
    for field, ours, theirs in checks:
        # The feed not carrying a value is silence, not disagreement: it is an
        # incomplete aggregation of someone else's pricing page, and treating a
        # gap as a contradiction would bury the real drifts in noise.
        if theirs is None or ours is None or ours == theirs:
            continue
        drifts.append(
            Drift(
                provider=row.provider,
                model=row.model,
                field=field,
                catalog=_plain(ours),
                feed=_plain(theirs),
                source_url=row.source_url,
            )
        )

    # A tier is priced data like any other, so it drifts like any other. The
    # feed spells its threshold into the field name (`..._above_272k_tokens`),
    # which means a *moved threshold* shows up here as the tier fields silently
    # going missing — reported as silence rather than as a matching price.
    tier = row.long_context
    if tier is not None:
        suffix = f"_above_{tier.threshold_tokens // 1000}k_tokens"
        tier_base = tier.input
        tier_checks: list[tuple[str, Decimal | None, Decimal | None]] = [
            (
                f"long_context.input (>{tier.threshold_tokens})",
                tier.input.normalize(),
                _per_mtok(entry.get(f"input_cost_per_token{suffix}")),
            ),
            (
                f"long_context.output (>{tier.threshold_tokens})",
                tier.output.normalize(),
                _per_mtok(entry.get(f"output_cost_per_token{suffix}")),
            ),
            (
                f"long_context.cache_read_multiplier (>{tier.threshold_tokens})",
                None
                if tier.cache_read_multiplier is None
                else tier.cache_read_multiplier.normalize(),
                _ratio(entry.get(f"cache_read_input_token_cost{suffix}"), tier_base),
            ),
        ]
        for field, ours, theirs in tier_checks:
            if theirs is None or ours is None or ours == theirs:
                continue
            drifts.append(
                Drift(
                    provider=row.provider,
                    model=row.model,
                    field=field,
                    catalog=_plain(ours),
                    feed=_plain(theirs),
                    source_url=row.source_url,
                )
            )

    ours_min = row.min_cacheable_tokens
    theirs_min = entry.get("prompt_cache_min_tokens")
    if ours_min is not None and theirs_min is not None and ours_min != theirs_min:
        drifts.append(
            Drift(
                provider=row.provider,
                model=row.model,
                field="min_cacheable_tokens",
                catalog=str(ours_min),
                feed=str(theirs_min),
                source_url=row.source_url,
            )
        )
    return drifts


def check(catalog: Catalog, feed: dict[str, Any], *, at: datetime | None = None) -> Report:
    """Compare every currently-in-force row against the feed.

    Only open-ended rows are checked: the feed is today's price list and has no
    history, so a superseded row disagreeing with it is correct behaviour, not
    drift.
    """
    moment = at or datetime.now(tz=None).astimezone()
    drifts: list[Drift] = []
    unmatched: list[str] = []
    unverifiable: list[str] = []
    checked = 0

    for row in catalog.rows:
        if not row.covers(moment):
            continue
        checked += 1
        entry = feed.get(row.model)
        if not isinstance(entry, dict) or entry.get("litellm_provider") != row.provider:
            unmatched.append(f"{row.provider}/{row.model}")
            continue
        drifts += _compare_row(row, entry)
        if row.batch_multiplier is not None:
            unverifiable.append(f"{row.provider}/{row.model} batch_multiplier")

    return Report(
        catalog_version=catalog.version,
        feed_name=FEED_NAME,
        rows_checked=checked,
        drifts=tuple(drifts),
        unmatched=tuple(unmatched),
        unverifiable=tuple(unverifiable),
    )
