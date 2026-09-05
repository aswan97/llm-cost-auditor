"""Baseline aggregation over priced records (SPEC.md §11.1).

Against the test catalog, never the bundled one, so these expectations stay
stable when real prices change (AGENTS.md). Rates are round on purpose:
`test-model` is 10 uUSD per input token and 40 per output token, and
`test-cheap` is 0.05 per input token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from llm_cost_auditor import baseline
from llm_cost_auditor.errors import MissingPriceError
from llm_cost_auditor.records import RequestRecord, Usage

CATALOG = Path(__file__).parent / "fixtures" / "pricing" / "catalog.yaml"
AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
BEFORE_ANY_ROW = datetime(2020, 1, 1, tzinfo=UTC)


def record(model: str = "test-model", at: datetime = AT, **usage: int) -> RequestRecord:
    return RequestRecord(
        request_id="req",
        source="testco",
        provider="testco",
        model=model,
        connection_id="conn",
        object_uri="file:///fixture",
        start_time=at,
        usage=Usage(**usage),
    )


# The set every expectation below is derived from:
#
#   test-model  1000 in + 100 out    10,000 +  4,000 = 14,000
#   test-model   500 in +  50 out     5,000 +  2,000 =  7,000
#   test-model  1000 in + 500 image  10,000          = 10,000  (image unpriced)
#   test-model  1000 unknown-TTL     1000 x 10 x 1.25 = 12,500  (exposure 7,500)
#                                            subtotal = 43,500  over 4 records
#   test-cheap  1000 in              1000 x 0.05      =     50  over 1 record
#                                               total = 43,550  over 5 records
POPULATION = [
    record(input_tokens=1000, output_tokens=100),
    record(input_tokens=500, output_tokens=50),
    record(input_tokens=1000, image_tokens=500),
    record(cache_write_unknown_ttl_tokens=1000),
    record(model="test-cheap", input_tokens=1000),
    record(model="test-ghost", input_tokens=1000),
    record(at=BEFORE_ANY_ROW, input_tokens=1000),
]


def computed() -> baseline.Baseline:
    return baseline.compute(POPULATION, path=CATALOG)


def test_the_total_is_the_sum_of_what_was_priced() -> None:
    result = computed()
    assert result.total_usd_micros == 43_550
    assert result.priced_records == 5


def test_per_model_spend_sums_exactly_to_the_total() -> None:
    """The invariant that catches an aggregation bug: no tolerance, integers only."""
    result = computed()
    assert sum(row.spend_usd_micros for row in result.by_model) == result.total_usd_micros
    assert sum(row.records for row in result.by_model) == result.priced_records


def test_models_are_ordered_by_spend_descending() -> None:
    """The number a reader acts on is the largest; it should not have to be found."""
    result = computed()
    assert [(row.model, row.spend_usd_micros, row.records) for row in result.by_model] == [
        ("test-model", 43_500, 4),
        ("test-cheap", 50, 1),
    ]


def test_unpriced_records_are_excluded_from_the_total_not_charged_zero() -> None:
    result = computed()
    assert result.excluded_records == 2
    assert result.total_usd_micros == 43_550, "exclusions must not move the total"


def test_the_two_ways_a_price_goes_missing_are_reported_separately() -> None:
    """They need different fixes, so they are not summed into one number."""
    result = computed()
    reasons = {item.model: item.reason for item in result.excluded}
    assert reasons["test-ghost"] == MissingPriceError.UNKNOWN_MODEL
    assert reasons["test-model"] == MissingPriceError.NO_ROW_FOR_TIMESTAMP
    ghost = next(i for i in result.excluded if i.model == "test-ghost")
    assert ghost.is_unknown_model is True
    assert "no catalog row" in ghost.explanation
    stale = next(i for i in result.excluded if i.model == "test-model")
    assert "historical rate is missing" in stale.explanation


def test_partial_pricing_and_ttl_exposure_are_carried_with_the_total() -> None:
    result = computed()
    assert result.partially_priced == (("image_tokens", 1),)
    assert result.ttl_unknown_exposure_usd_micros == 7_500
    assert result.ttl_unknown_exposure == "$0.01"


def test_a_total_with_gaps_never_reports_itself_complete() -> None:
    """`is_complete` is what a caller keys 'this is the whole number' off."""
    result = computed()
    assert result.is_complete is False
    assert result.has_caveats is True

    clean = baseline.compute([record(input_tokens=1000, output_tokens=100)], path=CATALOG)
    assert clean.is_complete is True
    assert clean.has_caveats is False
    assert clean.total_usd_micros == 14_000


def test_an_empty_population_is_zero_and_complete_but_priced_nothing() -> None:
    """Distinguishable from a run that cost nothing: `priced_records` is 0."""
    result = baseline.compute([], path=CATALOG)
    assert result.total_usd_micros == 0
    assert result.priced_records == 0
    assert result.by_model == ()


def test_provenance_travels_with_the_number() -> None:
    result = computed()
    assert result.catalog_version == "test-1"
    assert result.oldest_verification is not None


def test_money_formats_once_and_only_for_display() -> None:
    assert baseline.format_usd(0) == "$0.00"
    assert baseline.format_usd(43_550) == "$0.04"
    assert baseline.format_usd(9_460_000) == "$9.46"
    assert baseline.format_usd(1_234_567_890) == "$1,234.57"


def test_every_monetary_field_serializes_as_an_integer() -> None:
    """JSON has no decimal type, so a float here defeats exact equality (§6.4)."""
    payload = computed().model_dump(mode="json")
    assert isinstance(payload["total_usd_micros"], int)
    assert isinstance(payload["ttl_unknown_exposure_usd_micros"], int)
    for row in payload["by_model"]:
        assert isinstance(row["spend_usd_micros"], int)
    assert not any(key.endswith("_usd") for key in payload)
