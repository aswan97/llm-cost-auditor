"""Drift detection against a public pricing feed (SPEC.md §7.1).

No test here touches the network. The feed is a dict, because what needs
pinning is the comparison and the refusal to write — not urllib.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_cost_auditor import pricing
from llm_cost_auditor.pricing import refresh

CATALOG = Path(__file__).parent / "fixtures" / "pricing" / "catalog.yaml"
AT = datetime(2026, 8, 1, tzinfo=UTC)


def feed(**overrides: Any) -> dict[str, Any]:
    """A feed that agrees with the test catalog's in-force rows, unless told not to."""
    base: dict[str, Any] = {
        "test-model": {
            "litellm_provider": "testco",
            "input_cost_per_token": 1.2e-05,
            "output_cost_per_token": 4.8e-05,
            "cache_read_input_token_cost": 1.2e-06,
            "cache_creation_input_token_cost": 1.5e-05,
            "cache_creation_input_token_cost_above_1hr": 2.4e-05,
            "prompt_cache_min_tokens": 1000,
        },
        "test-nocache": {
            "litellm_provider": "testco",
            "input_cost_per_token": 1e-05,
            "output_cost_per_token": 4e-05,
            "cache_read_input_token_cost": 1e-06,
        },
        "test-tiered": {
            "litellm_provider": "testco",
            "input_cost_per_token": 1e-05,
            "output_cost_per_token": 4e-05,
            "cache_read_input_token_cost": 1e-06,
            "cache_creation_input_token_cost": 1.25e-05,
            "cache_creation_input_token_cost_above_1hr": 2e-05,
            "input_cost_per_token_above_2k_tokens": 2e-05,
            "output_cost_per_token_above_2k_tokens": 6e-05,
            "cache_read_input_token_cost_above_2k_tokens": 2e-06,
            "prompt_cache_min_tokens": 1000,
        },
        "test-cheap": {
            "litellm_provider": "testco",
            "input_cost_per_token": 5e-08,
            "output_cost_per_token": 4e-07,
            "cache_read_input_token_cost": 5e-09,
            "cache_creation_input_token_cost": 6.25e-08,
            "cache_creation_input_token_cost_above_1hr": 1e-07,
            "prompt_cache_min_tokens": 1000,
        },
    }
    for model, fields in overrides.items():
        base[model.replace("_", "-")].update(fields)
    return base


def test_an_agreeing_feed_reports_no_drift() -> None:
    report = refresh.check(pricing.catalog(CATALOG), feed(), at=AT)
    assert report.clean
    assert report.drifts == ()
    assert report.rows_checked == 4


def test_only_in_force_rows_are_compared() -> None:
    """The feed is today's price list with no history, so a superseded row
    disagreeing with it is correct behaviour rather than drift."""
    report = refresh.check(pricing.catalog(CATALOG), feed(), at=AT)
    # test-model has two rows; only the open-ended one is in force on 2026-08-01,
    # so four rows are compared rather than five.
    assert report.rows_checked == 4
    assert len(pricing.catalog(CATALOG).rows) == 5


def test_a_moved_base_rate_is_reported_with_both_values() -> None:
    report = refresh.check(
        pricing.catalog(CATALOG),
        feed(test_model={"input_cost_per_token": 1.3e-05}),
        at=AT,
    )
    assert not report.clean
    (drift,) = report.drifts
    assert drift.field == "input"
    assert drift.catalog == "12"
    assert drift.feed == "13"
    assert "example.invalid" in drift.source_url


def test_a_moved_multiplier_is_derived_the_same_way_ours_was() -> None:
    """A feed publishes an absolute cache price; the ratio to its own input rate
    is what our catalog stores, so that is what gets compared."""
    report = refresh.check(
        pricing.catalog(CATALOG),
        feed(test_model={"cache_read_input_token_cost": 6e-06}),  # 0.5x, not 0.1x
        at=AT,
    )
    (drift,) = report.drifts
    assert drift.field == "cache_read_multiplier"
    assert drift.catalog == "0.1"
    assert drift.feed == "0.5"


def test_a_value_the_feed_omits_is_silence_not_disagreement() -> None:
    """An aggregator's gap is an incomplete copy of someone else's page. Treating
    it as a contradiction would bury the real drifts in noise."""
    thin = feed()
    del thin["test-model"]["cache_creation_input_token_cost_above_1hr"]
    assert refresh.check(pricing.catalog(CATALOG), thin, at=AT).clean


def test_a_model_the_feed_lacks_is_named_rather_than_passed() -> None:
    thin = feed()
    del thin["test-cheap"]
    report = refresh.check(pricing.catalog(CATALOG), thin, at=AT)
    assert report.clean
    assert report.unmatched == ("testco/test-cheap",)


def test_a_provider_mismatch_does_not_borrow_another_broker_rate() -> None:
    """Same model name under a different broker is a different price (§7.1)."""
    wrong = feed()
    wrong["test-model"]["litellm_provider"] = "somebroker"
    report = refresh.check(pricing.catalog(CATALOG), wrong, at=AT)
    assert "testco/test-model" in report.unmatched
    assert report.clean


def test_batch_is_reported_as_unverifiable_rather_than_silently_unchecked() -> None:
    report = refresh.check(pricing.catalog(CATALOG), feed(), at=AT)
    assert any("batch_multiplier" in item for item in report.unverifiable)


def test_checking_never_writes_to_the_catalog() -> None:
    """The whole point of the design: drift is reported, never adopted."""
    before = CATALOG.read_bytes()
    refresh.check(pricing.catalog(CATALOG), feed(test_model={"input_cost_per_token": 9e-05}), at=AT)
    assert CATALOG.read_bytes() == before


def test_a_normal_import_pulls_in_no_network_code() -> None:
    """§7.1: a normal audit run never fetches anything. The module that can
    reach the network must not be imported by simply pricing something."""
    proof = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from llm_cost_auditor import pricing; "
            "print('llm_cost_auditor.pricing.refresh' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proof.stdout.strip() == "False"


def test_the_catalog_is_not_parsed_at_import() -> None:
    """Prices load lazily and are memoized; nothing reads the table at startup."""
    proof = subprocess.run(
        [
            sys.executable,
            "-c",
            "from llm_cost_auditor import pricing; print(pricing.table.load.cache_info().currsize)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proof.stdout.strip() == "0"


def test_a_moved_tier_rate_is_drift_like_any_other() -> None:
    """A long-context surcharge is priced data, so it drifts like priced data."""
    report = refresh.check(
        pricing.catalog(CATALOG),
        feed(test_tiered={"input_cost_per_token_above_2k_tokens": 2.5e-05}),
        at=AT,
    )
    (drift,) = report.drifts
    assert drift.field == "long_context.input (>2000)"
    assert drift.catalog == "20"
    assert drift.feed == "25"


def test_a_moved_threshold_shows_up_as_the_tier_going_quiet() -> None:
    """The feed spells the threshold into its field names, so a provider moving
    the boundary makes our tier fields vanish rather than disagree. That reads as
    unverifiable, which is the honest answer — it is not a matching price."""
    moved = feed()
    entry = moved["test-tiered"]
    for key in [k for k in list(entry) if k.endswith("_above_2k_tokens")]:
        entry[key.replace("_above_2k_tokens", "_above_4k_tokens")] = entry.pop(key)
    report = refresh.check(pricing.catalog(CATALOG), moved, at=AT)
    assert report.clean, "no false disagreement"
