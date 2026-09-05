"""The pricing engine (SPEC.md §7).

Every expected value here is hand-derived in `tests/fixtures/pricing/README.md`
from the test catalog and the token counts below, and asserted **exactly**
(AGENTS.md). If one of these fails, do the arithmetic in the README before
changing the number — the fixture is the specification, not the output.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from llm_cost_auditor import pricing
from llm_cost_auditor.errors import ConfigError, MissingPriceError
from llm_cost_auditor.records import RequestRecord, Status, Usage

CATALOG = Path(__file__).parent / "fixtures" / "pricing" / "catalog.yaml"

BEFORE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LAST_DAY = datetime(2026, 6, 30, 23, 59, tzinfo=UTC)
FIRST_DAY = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


def record(
    *,
    model: str = "test-model",
    at: datetime = BEFORE,
    status: Status = Status.OK,
    batch: bool = False,
    **usage: int,
) -> RequestRecord:
    return RequestRecord(
        request_id="req",
        source="testco",
        provider="testco",
        model=model,
        connection_id="conn",
        object_uri="file:///fixture",
        start_time=at,
        status=status,
        batch=batch,
        usage=Usage(**usage),
    )


def cost(rec: RequestRecord) -> int:
    return pricing.cost_of(rec, path=CATALOG).total_usd_micros


# --- the rates themselves -----------------------------------------------------


def test_rates_are_integer_micro_usd() -> None:
    """$10.00/MTok is 10,000,000 uUSD/MTok, and it is an `int` (AGENTS.md, §6.4)."""
    rate = pricing.rate("testco", "test-model", at=BEFORE, path=CATALOG)
    assert rate.input_usd_micros_per_mtok == 10_000_000
    assert rate.output_usd_micros_per_mtok == 40_000_000
    assert isinstance(rate.input_usd_micros_per_mtok, int)
    assert not isinstance(rate.input_usd_micros_per_mtok, float)


def test_thresholds_come_from_the_catalog_too() -> None:
    """AGENTS.md counts the cacheable minimum as a price literal, so it is readable here."""
    rate = pricing.rate("testco", "test-model", at=BEFORE, path=CATALOG)
    assert rate.min_cacheable_tokens == 1000
    assert rate.max_breakpoints == 4


def test_multipliers_are_decimals_not_floats() -> None:
    mult = pricing.multiplier("testco", "test-model", "cache_read", at=BEFORE, path=CATALOG)
    assert mult == Decimal("0.1")
    assert isinstance(mult, Decimal)


# --- README case 1: a cached request ------------------------------------------


def test_a_cached_request_bills_three_bases_off_one_rate() -> None:
    rec = record(
        input_tokens=500,
        cache_read_tokens=2000,
        cache_write_5m_tokens=1000,
        output_tokens=100,
    )
    priced = pricing.cost_of(rec, path=CATALOG)
    assert priced.by_class_usd_micros == {
        "input": 5_000,
        "output": 4_000,
        "cache_read": 2_000,
        "cache_write_5m": 12_500,
    }
    assert priced.total_usd_micros == 23_500


# --- README case 2: below the minimum cacheable size --------------------------


def test_a_prefix_below_the_threshold_bills_as_plain_input() -> None:
    assert cost(record(input_tokens=800, output_tokens=50)) == 10_000


# --- README case 3 and 4: batch, and batch composed with cache ----------------


def test_batch_discounts_output_as_well_as_input() -> None:
    assert cost(record(input_tokens=1000, output_tokens=1000, batch=True)) == 25_000


def test_batch_and_cache_compose_to_one_answer() -> None:
    """Both factors hit the rate before the single rounding, so order cannot matter."""
    assert cost(record(cache_read_tokens=1000, batch=True)) == 500


# --- README case 5, 6, 7: billed failures -------------------------------------


@pytest.mark.parametrize(
    ("status", "usage", "expected"),
    [
        (Status.ERROR_BILLED, {"input_tokens": 1000}, 10_000),
        (Status.TRUNCATED, {"input_tokens": 1000, "output_tokens": 5000}, 210_000),
        (Status.CANCELLED, {"input_tokens": 1000, "output_tokens": 300}, 22_000),
    ],
)
def test_failures_that_still_billed(status: Status, usage: dict[str, int], expected: int) -> None:
    assert cost(record(status=status, **usage)) == expected


# --- README case 8: the retry pair --------------------------------------------


def test_a_rejected_attempt_costs_nothing() -> None:
    assert cost(record(status=Status.ERROR_UNBILLED)) == 0


def test_both_halves_of_a_500_retry_are_billed() -> None:
    attempts = [record(input_tokens=1000, output_tokens=500) for _ in range(2)]
    assert [cost(a) for a in attempts] == [30_000, 30_000]
    assert sum(cost(a) for a in attempts) == 60_000


# --- README case 9: the price boundary ----------------------------------------


def test_each_request_is_priced_at_the_rate_in_force_at_its_own_timestamp() -> None:
    assert cost(record(input_tokens=1000, at=LAST_DAY)) == 10_000
    assert cost(record(input_tokens=1000, at=FIRST_DAY)) == 12_000


def test_a_timestamp_before_every_row_is_unpriced_not_backfilled() -> None:
    with pytest.raises(MissingPriceError, match="none covers"):
        cost(record(input_tokens=1000, at=datetime(2025, 12, 31, tzinfo=UTC)))


# --- README case 10: the unknown TTL class ------------------------------------


def test_an_unknown_ttl_class_bills_the_cheapest_and_states_the_exposure() -> None:
    priced = pricing.cost_of(record(cache_write_unknown_ttl_tokens=1000), path=CATALOG)
    assert priced.by_class_usd_micros["cache_write_unknown_ttl"] == 12_500
    assert priced.ttl_unknown_exposure_usd_micros == 7_500


# --- README case 11: multimodal -----------------------------------------------


def test_image_tokens_are_reported_unpriced_rather_than_charged_zero() -> None:
    priced = pricing.cost_of(
        record(input_tokens=1000, output_tokens=100, image_tokens=500), path=CATALOG
    )
    assert priced.total_usd_micros == 14_000
    assert priced.unpriced_token_classes == ("image_tokens",)
    assert priced.is_complete is False


def test_reasoning_tokens_are_not_billed_again_on_top_of_output() -> None:
    """Both providers count them inside `output_tokens`; charging twice would
    inflate every reasoning-heavy workload by the size of its own thinking."""
    plain = cost(record(input_tokens=1000, output_tokens=500))
    with_reasoning = cost(record(input_tokens=1000, output_tokens=500, reasoning_tokens=400))
    assert plain == with_reasoning == 30_000


# --- README case 12: rounding -------------------------------------------------


@pytest.mark.parametrize(("tokens", "expected"), [(10, 0), (30, 2), (1000, 50)])
def test_rounding_is_half_even_and_happens_once(tokens: int, expected: int) -> None:
    assert cost(record(model="test-cheap", input_tokens=tokens)) == expected


# --- refusals: the part that keeps a wrong number from being invented ---------


def test_an_absent_multiplier_is_an_error_not_a_free_token_class() -> None:
    with pytest.raises(MissingPriceError, match="cache_write_5m"):
        cost(record(model="test-nocache", cache_write_5m_tokens=1000))


def test_an_unknown_model_is_not_priced_off_a_sibling() -> None:
    with pytest.raises(MissingPriceError, match="no price row"):
        cost(record(model="test-model-turbo"))


def test_the_error_names_what_the_catalog_does_have() -> None:
    with pytest.raises(MissingPriceError, match="test-model"):
        cost(record(model="nonexistent"))


def test_a_non_usd_row_is_a_config_error_never_a_conversion(tmp_path: Path) -> None:
    bad = tmp_path / "eur.yaml"
    bad.write_text(
        CATALOG.read_text(encoding="utf-8").replace("currency: USD", "currency: EUR", 1),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="USD-only"):
        pricing.catalog(bad)


# --- provenance ---------------------------------------------------------------


def test_every_figure_carries_the_catalog_it_came_from() -> None:
    priced = pricing.cost_of(record(input_tokens=1000), path=CATALOG)
    assert priced.catalog_version == "test-1"
    assert priced.last_verified == date(2026, 1, 1)


def test_staleness_is_measured_against_the_row_that_was_used() -> None:
    fresh = pricing.rate("testco", "test-model", at=BEFORE, path=CATALOG)
    stale = pricing.rate("testco", "test-cheap", at=BEFORE, path=CATALOG)
    # 2026-01-01 -> 2026-04-01 is 31 + 28 + 31 = 90 days; 2026 is not a leap year.
    assert fresh.age_days(date(2026, 4, 1)) == 90
    assert fresh.is_stale(date(2026, 4, 1)) is False, "90 days is the threshold, not past it"
    assert fresh.is_stale(date(2026, 4, 2)) is True

    # 2025-01-01 -> 2026-04-02 is 365 + 91.
    assert stale.age_days(date(2026, 4, 2)) == 456


# --- the bundled catalog ------------------------------------------------------


def test_the_bundled_catalog_parses_and_is_usd_only() -> None:
    """It ships in the wheel, so a packaging mistake must fail here, not at a user."""
    bundled = pricing.catalog()
    assert bundled.rows
    assert {row.currency for row in bundled.rows} == {"USD"}
    assert {row.units for row in bundled.rows} == {"per_mtok"}


def test_no_bundled_model_has_overlapping_effective_periods() -> None:
    bundled = pricing.catalog()
    for row in bundled.rows:
        moment = datetime.combine(row.effective_from, datetime.min.time(), tzinfo=UTC)
        assert bundled.find(row.provider, row.model, moment) is not None


# --- the two ways a price can be missing need different fixes -----------------


def test_an_unknown_model_and_an_uncovered_period_are_told_apart() -> None:
    """Same exception, different `reason` — they send a user to different places.

    An unknown model means the catalog never heard of it. An uncovered
    timestamp means the model is priced and a *historical row* is missing.
    Reported as one thing, half the users go looking in the wrong file.
    """
    with pytest.raises(MissingPriceError) as unknown:
        cost(record(model="never-shipped"))
    assert unknown.value.reason == MissingPriceError.UNKNOWN_MODEL

    with pytest.raises(MissingPriceError) as uncovered:
        cost(record(at=datetime(2020, 1, 1, tzinfo=UTC)))
    assert uncovered.value.reason == MissingPriceError.NO_ROW_FOR_TIMESTAMP


# --- README case 13: long-context tiers ---------------------------------------


def test_a_prompt_under_the_threshold_stays_on_the_base_rate() -> None:
    assert cost(record(model="test-tiered", input_tokens=1000, output_tokens=100)) == 14_000


def test_the_threshold_is_exclusive() -> None:
    """Exactly at the boundary is still the base rate; the tier is `> threshold`."""
    assert cost(record(model="test-tiered", input_tokens=2000, output_tokens=100)) == 24_000
    assert cost(record(model="test-tiered", input_tokens=2001, output_tokens=100)) == 46_020


def test_crossing_the_threshold_reprices_the_whole_request_not_the_excess() -> None:
    """Wholesale, not marginal — the single most consequential way to get this wrong.

    3000 prompt tokens at the tier rate is 60,000. Charging the first 2000 at
    the base rate and only the excess at the tier would give 40,000, a third
    less, and would look entirely plausible in a report.
    """
    priced = pricing.cost_of(record(model="test-tiered", input_tokens=3000), path=CATALOG)
    assert priced.total_usd_micros == 60_000
    assert priced.total_usd_micros != 40_000, "that would be the marginal reading"
    assert priced.long_context_applied is True


def test_output_does_not_push_a_request_over_the_threshold() -> None:
    """A long answer to a short question is not a long-context request."""
    priced = pricing.cost_of(
        record(model="test-tiered", input_tokens=100, output_tokens=5000), path=CATALOG
    )
    assert priced.total_usd_micros == 201_000
    assert priced.long_context_applied is False


def test_cache_tokens_count_toward_the_threshold_and_reprice_with_it() -> None:
    """Cached prompt tokens are still prompt tokens, and the cache multiplier
    then applies to the *tiered* input rate rather than the base one."""
    priced = pricing.cost_of(
        record(model="test-tiered", cache_read_tokens=2500, output_tokens=100), path=CATALOG
    )
    assert priced.by_class_usd_micros["cache_read"] == 5_000
    assert priced.total_usd_micros == 11_000
    assert priced.long_context_applied is True


# --- the bundled tiers, against the real catalog ------------------------------


def bundled(model: str, at: datetime, **usage: int) -> pricing.RecordCost:
    return pricing.cost_of(
        RequestRecord(
            request_id="req",
            source="s",
            provider="anthropic" if model.startswith("claude") else "openai",
            model=model,
            connection_id="c",
            object_uri="file:///fixture",
            start_time=at,
            usage=Usage(**usage),
        )
    )


def test_sonnet_4_5_reprices_above_200k() -> None:
    """$3.00/MTok base, $6.00/MTok above 200k prompt tokens (2x), output 1.5x."""
    at = datetime(2026, 8, 1, tzinfo=UTC)
    small = bundled("claude-sonnet-4-5", at, input_tokens=199_000, output_tokens=100)
    assert small.total_usd_micros == 199_000 * 3 + 100 * 15
    assert small.long_context_applied is False

    # Tiered output is $22.50/MTok. Rounding is applied once per token *class*,
    # not per token, so 100 tokens is exactly 2,250 uUSD rather than 100 rounded
    # per-token charges — which is the whole reason the rate is held per MTok.
    large = bundled("claude-sonnet-4-5", at, input_tokens=250_000, output_tokens=100)
    assert large.total_usd_micros == 250_000 * 6 + 2_250
    assert large.by_class_usd_micros == {"input": 1_500_000, "output": 2_250}
    assert large.long_context_applied is True

    # The surcharge is the point: the same request on the base rate would be
    # a little over half as much, and nothing in the output would say why.
    assert large.total_usd_micros == pytest.approx(1_502_250)
    assert 250_000 * 3 + 1_500 == 751_500  # what the base rate would have charged


@pytest.mark.parametrize(
    ("model", "tokens", "expected_micros"),
    [
        ("gpt-5.5", 300_000, 300_000 * 10),
        ("gpt-5.5-pro", 300_000, 300_000 * 60),
    ],
)
def test_the_openai_272k_tier_applies(model: str, tokens: int, expected_micros: int) -> None:
    at = datetime(2026, 8, 1, tzinfo=UTC)
    priced = bundled(model, at, input_tokens=tokens)
    assert priced.total_usd_micros == expected_micros
    assert priced.long_context_applied is True


def test_every_bundled_row_declares_a_tokenizer() -> None:
    assert all(row.tokenizer for row in pricing.catalog().rows)


def test_untiered_rows_are_verified_absence_not_unknown() -> None:
    """Twelve of the fifteen rows have no tier, and that was checked rather than
    assumed — so a huge prompt on one of them is priced, not flagged."""
    at = datetime(2026, 8, 1, tzinfo=UTC)
    priced = bundled("claude-opus-5", at, input_tokens=900_000)
    assert priced.total_usd_micros == 900_000 * 5
    assert priced.long_context_applied is False
    assert priced.is_complete is True


# --- tokenizers: what a routing analyzer must ask before comparing ------------


def test_token_counts_are_transferable_only_inside_a_family() -> None:
    at = datetime(2026, 8, 1, tzinfo=UTC)
    assert pricing.token_counts_transferable(
        ("anthropic", "claude-opus-5"), ("anthropic", "claude-haiku-4-5"), at=at
    )
    assert pricing.token_counts_transferable(("openai", "gpt-5.5"), ("openai", "gpt-5-nano"), at=at)
    assert not pricing.token_counts_transferable(
        ("anthropic", "claude-opus-5"), ("openai", "gpt-5.5"), at=at
    )


def test_transferability_is_not_about_the_provider_name() -> None:
    """Two rows from one provider can still disagree, and the check is on the
    tokenizer rather than on who sells the model."""
    assert not pricing.token_counts_transferable(
        ("testco", "test-model"), ("testco", "test-tiered"), at=BEFORE, path=CATALOG
    )


def test_fable_5_1_cache_reads_are_deliberately_off_pattern() -> None:
    """$0.25/MTok, where the rest of the Anthropic line reads at 0.1x input.

    Pinned because the value looks like a mistake and the obvious tidy-up —
    making it 0.1 like its siblings — would quadruple the cache-read cost of
    every Fable 5.1 request, and understate what prefix caching is worth on it
    by the same factor. Confirmed against the published rate, so a future
    disagreement here is a real price change, not a typo to correct.
    """
    at = datetime(2026, 9, 5, tzinfo=UTC)
    rate = pricing.rate("anthropic", "claude-fable-5-1", at=at)
    mult = pricing.multiplier("anthropic", "claude-fable-5-1", "cache_read", at=at)
    assert rate.input_usd_micros_per_mtok == 10_000_000
    assert mult == Decimal("0.025")
    assert Decimal(rate.input_usd_micros_per_mtok) * mult == 250_000

    sibling = pricing.multiplier("anthropic", "claude-fable-5", "cache_read", at=at)
    assert sibling == Decimal("0.1"), "the rest of the line is unchanged"
