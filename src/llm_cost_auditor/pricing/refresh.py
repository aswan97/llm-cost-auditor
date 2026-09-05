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

# A second feed, for the one thing the first cannot check. LiteLLM carries no
# batch pricing for first-party Anthropic or OpenAI rows, but OpenRouter lists
# every model twice — `openai/gpt-5.5` and `openai/gpt-5.5:batch` — so the
# multiplier is the ratio between the two, observed rather than assumed.
BATCH_FEED_URL = "https://openrouter.ai/api/v1/models"
BATCH_FEED_NAME = "OpenRouter model list (`:batch` variants)"

# The feeds quote dollars per token; the catalog quotes dollars per MTok.
_TOKENS_PER_MTOK = 1_000_000

# Price classes whose std/batch ratio should all agree on the batch multiplier.
_BATCH_RATIO_KEYS = ("prompt", "completion", "input_cache_read")


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
    batch_feed_name: str | None = None
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


def fetch_batch_feed(url: str = BATCH_FEED_URL, *, timeout: int = 30) -> dict[str, Any]:
    """Fetch the batch feed, reduced to `{model id: pricing}`."""
    payload = fetch(url, timeout=timeout)
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise AuditorError(f"batch feed at {url} has no `data` list.")
    return {
        entry["id"]: entry.get("pricing", {})
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _batch_feed_ids(row: PriceRow) -> list[str]:
    """Candidate ids for this row in the batch feed, most likely first.

    The feed writes versions with a dot where the catalog uses a hyphen
    (`claude-haiku-4-5` against `anthropic/claude-haiku-4.5`), so a trailing
    `-<digit>-<digit>` gets its last hyphen swapped. Nothing rests on the guess
    being right: a candidate is only used once its *base* prices corroborate the
    row, so a wrong match is rejected rather than silently compared.
    """
    names = [row.model]
    parts = row.model.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        names.append(f"{parts[0]}-{parts[1]}.{parts[2]}")
    return [f"{row.provider}/{name}" for name in names]


def _batch_multiplier(standard: dict[str, Any], batched: dict[str, Any]) -> Decimal | None:
    """The batch multiplier implied by one model's two listings.

    Every price class that both listings carry must imply the *same* ratio. If
    they disagree, the pair is not a clean discount and no multiplier is
    returned — a half-verified number here would be worse than an unverified
    one, because it would look checked.
    """
    ratios: set[Decimal] = set()
    for key in _BATCH_RATIO_KEYS:
        std, bat = _per_mtok(standard.get(key)), _per_mtok(batched.get(key))
        if std is None or bat is None or std == 0:
            continue
        ratios.add((bat / std).normalize())
    if len(ratios) != 1:
        return None
    return ratios.pop()


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


def _compare_batch(row: PriceRow, batch_feed: dict[str, Any]) -> tuple[Drift | None, str | None]:
    """Check this row's batch multiplier against the two listings of the model.

    Returns `(drift, unverifiable_reason)`, at most one of which is set. The
    candidate id is only trusted once the feed's *standard* input and output
    rates match the row's own — price agreement is what establishes that the two
    sides are talking about the same model, so a bad id guess declines to answer
    instead of comparing the wrong thing.
    """
    if row.batch_multiplier is None:
        return None, None
    label = f"{row.provider}/{row.model} batch_multiplier"

    for candidate in _batch_feed_ids(row):
        standard = batch_feed.get(candidate)
        batched = batch_feed.get(f"{candidate}:batch")
        if not isinstance(standard, dict) or not isinstance(batched, dict):
            continue
        if (
            _per_mtok(standard.get("prompt")) != row.input.normalize()
            or _per_mtok(standard.get("completion")) != row.output.normalize()
        ):
            return None, f"{label} — {candidate} base rates disagree with the catalog"

        theirs = _batch_multiplier(standard, batched)
        if theirs is None:
            return None, f"{label} — {candidate} listings imply no single ratio"
        ours = row.batch_multiplier.normalize()
        if ours != theirs:
            return (
                Drift(
                    provider=row.provider,
                    model=row.model,
                    field="batch_multiplier",
                    catalog=_plain(ours),
                    feed=_plain(theirs),
                    source_url=row.source_url,
                ),
                None,
            )
        return None, None

    return None, f"{label} — no `:batch` listing found"


def check(
    catalog: Catalog,
    feed: dict[str, Any],
    *,
    batch_feed: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> Report:
    """Compare every currently-in-force row against the feed.

    Only open-ended rows are checked: the feed is today's price list and has no
    history, so a superseded row disagreeing with it is correct behaviour, not
    drift.

    `batch_feed` is optional because it is a second network call for one field.
    Without it, batch multipliers are reported as unverifiable exactly as
    before — never as agreeing.
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

        if batch_feed is None:
            if row.batch_multiplier is not None:
                unverifiable.append(f"{row.provider}/{row.model} batch_multiplier")
        else:
            drift, reason = _compare_batch(row, batch_feed)
            if drift is not None:
                drifts.append(drift)
            if reason is not None:
                unverifiable.append(reason)

    return Report(
        catalog_version=catalog.version,
        feed_name=FEED_NAME,
        batch_feed_name=None if batch_feed is None else BATCH_FEED_NAME,
        rows_checked=checked,
        drifts=tuple(drifts),
        unmatched=tuple(unmatched),
        unverifiable=tuple(unverifiable),
    )
