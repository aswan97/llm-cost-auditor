"""Sequential marginal attribution (SPEC.md §11.2).

The invariant these tests exist for: **marginals sum exactly to the portfolio
total**. Not approximately. In floats that comparison becomes a tolerance,
which is the same as no test at all (AGENTS.md), and it is the only check that
catches a record being credited to two findings at once.
"""

from __future__ import annotations

import pytest

from llm_cost_auditor import attribution


def test_a_claim_nobody_overlaps_is_credited_in_full() -> None:
    [only] = attribution.attribute([{"a": 100, "b": 250}])
    assert only.standalone_usd_micros == 350
    assert only.marginal_usd_micros == 350
    assert only.absorbed_by == ()


def test_the_earlier_claim_takes_the_overlap() -> None:
    """Order is the whole mechanism: the same pair of claims reversed swaps the credit."""
    first, second = attribution.attribute([{"a": 100}, {"a": 100, "b": 40}])
    assert (first.standalone_usd_micros, first.marginal_usd_micros) == (100, 100)
    assert (second.standalone_usd_micros, second.marginal_usd_micros) == (140, 40)
    assert second.absorbed_by == (0,)


def test_a_wholly_overlapping_claim_keeps_its_standalone_value() -> None:
    """§11.2: both numbers are reported, never the marginal alone.

    A finding worth `$0` marginal is not a finding worth nothing — it is the
    report saying "these are someone else's dollars", and dropping the
    standalone is how that becomes invisible.
    """
    _, second = attribution.attribute([{"a": 500}, {"a": 500}])
    assert second.standalone_usd_micros == 500
    assert second.marginal_usd_micros == 0
    assert second.is_wholly_claimed_above
    assert second.absorbed_by == (0,)


def test_absorption_names_the_largest_taker_first() -> None:
    claims = [{"a": 10}, {"b": 900}, {"a": 10, "b": 900, "c": 5}]
    results = attribution.attribute(claims)
    # Claim 1 took 900 of it and claim 0 took 10, so claim 1 is named first: a
    # reader chasing a shrunken marginal wants the finding that has most of it.
    assert results[2].absorbed_by == (1, 0)


def test_marginals_sum_exactly_to_the_portfolio_total() -> None:
    """The §11.2 property, on overlapping claims with no round numbers."""
    claims = [
        {"r1": 1_234_567, "r2": 89},
        {"r2": 89, "r3": 4_321},
        {"r1": 1_234_567, "r3": 4_321, "r4": 7},
        {"r5": 999_999},
    ]
    results = attribution.attribute(claims)
    total = sum(item.marginal_usd_micros for item in results)

    assert total == attribution.portfolio_total(claims)
    assert total == 1_234_567 + 89 + 4_321 + 7 + 999_999


def test_an_empty_claim_is_worth_nothing_and_absorbs_nothing() -> None:
    [only] = attribution.attribute([{}])
    assert only.standalone_usd_micros == 0
    assert only.marginal_usd_micros == 0
    assert not only.is_wholly_claimed_above


def test_a_zero_cost_record_is_still_claimed_once() -> None:
    """A `$0` record — a rejected 429 — must not be re-credited to a later claim."""
    first, second = attribution.attribute([{"a": 0}, {"a": 0, "b": 12}])
    assert first.marginal_usd_micros == 0
    assert second.marginal_usd_micros == 12
    assert second.absorbed_by == (0,)


def test_two_claims_disagreeing_about_a_record_is_an_error() -> None:
    """A record's cost is a property of the record, so the two cannot both be right."""
    with pytest.raises(ValueError, match="claimed at 100 uUSD"):
        attribution.attribute([{"a": 100}, {"a": 250}])


def test_a_negative_claim_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        attribution.attribute([{"a": -1}])
