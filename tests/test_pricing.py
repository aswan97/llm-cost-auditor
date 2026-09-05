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


def test_a_long_context_request_is_partially_priced_not_underpriced() -> None:
    """The bundled sonnet-4-5 row has a tier it cannot price, so it says so."""
    big = RequestRecord(
        request_id="req",
        source="anthropic",
        provider="anthropic",
        model="claude-sonnet-4-5",
        connection_id="c",
        object_uri="file:///fixture",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        usage=Usage(input_tokens=250_000, output_tokens=100),
    )
    priced = pricing.cost_of(big)
    assert priced.unpriced_token_classes == ("input_above_200000_tokens",)
    assert priced.is_complete is False

    small = big.model_copy(update={"usage": Usage(input_tokens=1000, output_tokens=100)})
    assert pricing.cost_of(small).is_complete is True


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
