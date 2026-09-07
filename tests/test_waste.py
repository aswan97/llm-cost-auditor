"""The waste analyzer against hand-computed fixtures, asserted exactly.

Every expected value here is derived by hand in
`tests/fixtures/waste/README.md`, from a test catalog whose rates are round
numbers, and asserted **exactly** — never within a tolerance (AGENTS.md). If
one of these fails, do the arithmetic in the README before changing the number:
the fixture is the specification, not the output.

The records are produced by the real ingest pipeline — decode, the Anthropic
adapter, retry/duplicate normalization, and `records.parquet` — rather than
constructed in the test, so a change to any of those shows up here as a wrong
dollar figure rather than passing unnoticed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import WASTE_CATALOG, WINDOW

from llm_cost_auditor import attribution, engine, profile, waste
from llm_cost_auditor.findings import Confidence, FindingSet, Realizable, SliceRef, Verdict
from llm_cost_auditor.ingest.manifest import Coverage, SliceSummary
from llm_cost_auditor.record_store import RecordStore
from llm_cost_auditor.records import Fidelity
from llm_cost_auditor.run_store import RECORDS_PARQUET, RunRequest, RunStore, Stage


@pytest.fixture
def analyzed(waste_workspace: Path) -> FindingSet:
    """Ingest the fixture traffic for real, then analyze it at the test rates."""
    runs = RunStore(waste_workspace)
    record = runs.create(
        RunRequest(connection_ids=["waste"], window=WINDOW, timezone="UTC", stop_after=Stage.INGEST)
    )
    engine.execute_ingest(workspace=waste_workspace, runs=runs, run_id=record.run_id)
    record = runs.get(record.run_id)

    store = RecordStore(runs.artifact_path(record.run_id, RECORDS_PARQUET))
    records = list(store.iter_records())
    summary = profile.discover(records)

    return waste.analyze(
        run_id=record.run_id,
        records=records,
        summary=summary,
        slices=record.slices,
        coverage=record.coverage,
        path=WASTE_CATALOG,
    )


def by_id(results: FindingSet, finding_id: str) -> Any:
    matches = [f for f in results.findings if f.id == finding_id]
    assert len(matches) == 1, f"expected exactly one {finding_id!r}, got {[f.id for f in matches]}"
    return matches[0]


# --- the numbers, exactly -----------------------------------------------------


def test_the_baseline_matches_the_hand_computed_total(
    analyzed: FindingSet, waste_expected: dict[str, Any]
) -> None:
    assert analyzed.baseline_usd_micros == waste_expected["baseline_usd_micros"]
    assert analyzed.catalog_version == waste_expected["catalog_version"]


def test_every_finding_matches_the_fixture_figure_for_figure(
    analyzed: FindingSet, waste_expected: dict[str, Any]
) -> None:
    """Ranked order, ids, tiers, and all six money figures per finding."""
    expected = waste_expected["findings"]
    assert [f.id for f in analyzed.findings] == [item["id"] for item in expected]

    for item in expected:
        finding = by_id(analyzed, item["id"])
        assert finding.analyzer == item["analyzer"]
        assert finding.confidence == Confidence(item["confidence"])
        assert finding.evidence.requests == item["requests"]

        standalone = finding.savings.gross_standalone
        assert standalone.low_usd_micros == item["standalone"]["low"]
        assert standalone.expected_usd_micros == item["standalone"]["expected"]
        assert standalone.high_usd_micros == item["standalone"]["high"]

        marginal = finding.savings.gross_marginal
        assert marginal.low_usd_micros == item["marginal"]["low"]
        assert marginal.expected_usd_micros == item["marginal"]["expected"]
        assert marginal.high_usd_micros == item["marginal"]["high"]

        assert finding.evidence.detail.get("marginal_absorbed_by", []) == item["absorbed_by"]


def test_the_portfolio_is_the_sum_of_marginals_and_nothing_else(
    analyzed: FindingSet, waste_expected: dict[str, Any]
) -> None:
    """§11.2. Standalone sums to more, and that gap is the double-count avoided."""
    assert analyzed.portfolio_usd_micros == waste_expected["portfolio_usd_micros"]

    standalone_total = sum(
        f.savings.gross_standalone.expected_usd_micros for f in analyzed.findings
    )
    assert standalone_total == 40_500
    assert standalone_total - analyzed.portfolio_usd_micros == 5_000


def test_confidence_tier_totals_partition_the_portfolio(
    analyzed: FindingSet, waste_expected: dict[str, Any]
) -> None:
    """§11.3: totals are per tier, so a heuristic never enters a total silently."""
    totals = {tier.value: amount for tier, amount in analyzed.by_confidence().items()}
    assert totals == waste_expected["by_confidence_usd_micros"]
    assert sum(totals.values()) == analyzed.portfolio_usd_micros


# --- the attribution the fixture was built to pin ------------------------------


def test_a_billed_failure_inside_a_retry_chain_is_credited_once(analyzed: FindingSet) -> None:
    """Lines 2 and 3a are both `error_billed`; only 3a is also a superseded retry.

    The billed-failure finding must lose 3a's dollars and keep line 2's — a
    subtraction of whole findings rather than of overlapping records would take
    both.
    """
    retry = by_id(analyzed, "waste.retry_storm.claims/extract")
    failure = by_id(analyzed, "waste.billed_failure.claims/extract")

    assert retry.savings.gross_marginal.expected_usd_micros == 5_000
    assert failure.savings.gross_standalone.expected_usd_micros == 7_000
    assert failure.savings.gross_marginal.expected_usd_micros == 2_000
    assert failure.evidence.detail["marginal_absorbed_by"] == [retry.id]


def test_a_retry_whose_superseded_attempt_cost_nothing_claims_nothing(
    analyzed: FindingSet,
) -> None:
    """`w1_429` is a genuine two-attempt chain whose first attempt was free.

    Claiming every superseded attempt rather than every *billed* one would put
    a `$0` row in the retry finding that nobody can account for.
    """
    retry = by_id(analyzed, "waste.retry_storm.claims/extract")
    assert retry.evidence.detail["chains"] == 1
    assert retry.evidence.detail["billed_superseded_attempts"] == 1
    assert retry.evidence.detail["longest_chain_attempts"] == 2


def test_the_429_is_still_reported_at_zero_rather_than_omitted(analyzed: FindingSet) -> None:
    """A reader who sees no 429 finding should be able to conclude there were none."""
    churn = by_id(analyzed, "waste.rate_limit_churn.claims/extract")
    assert churn.savings.gross_marginal.expected_usd_micros == 0
    assert churn.evidence.detail["rejected_requests"] == 1
    assert churn.evidence.detail["billed_tokens"] == 0
    assert churn.evidence.detail["wasted_round_trip_ms"] == 120


def test_marginals_sum_exactly_to_the_union_of_claimed_records(analyzed: FindingSet) -> None:
    """The §11.2 invariant, recomputed independently from the record ids."""
    claims = [
        {"w1_retry#0": 5_000},
        {"w1_billed_fail#0": 2_000, "w1_retry#0": 5_000},
        {"w2_billed_fail#0": 800},
        {"w1_truncated#0": 24_000},
        {"w2_cancelled#0": 3_700},
        {"w1_429#0": 0},
    ]
    assert attribution.portfolio_total(claims) == analyzed.portfolio_usd_micros


# --- what the analyzer refuses to claim ---------------------------------------


def test_truncation_is_estimated_and_says_why(analyzed: FindingSet) -> None:
    """§11.3: "why is this only Estimated" is answerable mechanically."""
    finding = by_id(analyzed, "waste.truncation.claims/extract")
    assert finding.confidence is Confidence.ESTIMATED
    [penalty] = finding.confidence_penalties
    assert penalty.code == "unconfirmed_rerequest"
    assert penalty.effect == "cap:Estimated"
    assert "Tier B" in penalty.detail


def test_truncations_low_bound_recovers_nothing(analyzed: FindingSet) -> None:
    """The band is the honest part: `low` is "every truncated answer was kept"."""
    finding = by_id(analyzed, "waste.truncation.claims/extract")
    assert finding.savings.gross_marginal.low_usd_micros == 0
    assert finding.savings.gross_marginal.expected_usd_micros == 24_000
    assert finding.evidence.assumptions


def test_no_monthly_projection_is_produced_and_the_absence_is_stated(
    analyzed: FindingSet,
) -> None:
    """§11.4. A withheld projection must be visible, not merely missing."""
    assert all(f.savings.projection_monthly_usd_micros is None for f in analyzed.findings)
    assert any("projection" in reason for reason in analyzed.withheld_reasons)


def test_savings_are_list_price_and_wholly_realizable_until_an_overlay_exists(
    analyzed: FindingSet,
) -> None:
    for finding in analyzed.findings:
        assert finding.pricing.resolution == "list"
        assert finding.pricing.catalog_version == "waste-test-1"
        assert finding.savings.realizable is Realizable.YES
        assert finding.savings.realizable_range == finding.savings.gross_marginal


def test_every_finding_names_the_slice_it_covers(analyzed: FindingSet) -> None:
    """§5.2: a finding that cannot name its slices can be read as covering
    traffic it never saw."""
    for finding in analyzed.findings:
        assert finding.slices
        for reference in finding.slices:
            assert reference.connection_id == "waste"
            assert reference.source == "anthropic"


def test_the_applicability_matrix_records_a_verdict_per_slice(analyzed: FindingSet) -> None:
    """§5.2: "why is there no finding for this source" must be answerable."""
    assert [item.verdict for item in analyzed.applicability] == [Verdict.RUN]
    assert analyzed.applicability[0].analyzer == waste.ANALYZER


def test_an_unpriced_model_is_named_rather_than_priced_at_zero(
    analyzed: FindingSet, waste_workspace: Path
) -> None:
    """A record the catalog cannot price is excluded and reported (§7.1).

    Analyzing the same records against a catalog that has never heard of this
    model must produce no findings and say so, rather than a confident `$0`.
    """
    runs = RunStore(waste_workspace)
    record = runs.create(
        RunRequest(connection_ids=["waste"], window=WINDOW, timezone="UTC", stop_after=Stage.INGEST)
    )
    engine.execute_ingest(workspace=waste_workspace, runs=runs, run_id=record.run_id)
    store = RecordStore(runs.artifact_path(record.run_id, RECORDS_PARQUET))
    records = list(store.iter_records())

    results = waste.analyze(
        run_id=record.run_id,
        records=records,
        summary=profile.discover(records),
        path=Path(__file__).parent / "fixtures" / "pricing" / "catalog.yaml",
    )
    assert results.findings == []
    assert results.baseline_usd_micros == 0
    assert any("no catalog rate" in reason for reason in results.withheld_reasons)
    assert any("anthropic/test-flat" in reason for reason in results.withheld_reasons)

    # The discriminator, without which this empty result is indistinguishable
    # from a run whose traffic was genuinely clean.
    assert results.analyzed_records == 0
    assert results.analyzed_nothing


def test_unread_objects_make_the_savings_a_stated_lower_bound(analyzed: FindingSet) -> None:
    """§6.1, §11.4: waste in an object nobody read is waste nobody counted."""
    coverage = Coverage(listed_bytes=100, read_bytes=90, missing_bytes=10, failed_objects=1)
    coverage.apply_gating(max_missing_pct=50.0)

    results = waste.analyze(
        run_id="run",
        records=[],
        summary=profile.discover([]),
        coverage=coverage,
        path=WASTE_CATALOG,
    )
    assert any("lower bound" in reason for reason in results.withheld_reasons)


# --- the seam ------------------------------------------------------------------


def test_waste_runs_on_billing_only_logs(analyzed: FindingSet) -> None:
    """The point of starting here: no instrumentation is required (§6.3, §9.1)."""
    assert waste.REQUIRED_FIDELITY is Fidelity.BILLING
    verdict = waste.applicable(
        SliceSummary(connection_id="c", source="anthropic", fidelity=Fidelity.BILLING)
    )
    assert verdict.verdict is Verdict.RUN
    assert verdict.slice == SliceRef(
        connection_id="c", source="anthropic", fidelity=Fidelity.BILLING
    )


def test_the_finding_set_digest_changes_with_the_findings(analyzed: FindingSet) -> None:
    """§13.6: a content digest over the document, not a signature."""
    before = analyzed.digest()
    assert before == analyzed.digest()

    analyzed.findings[0].title = "edited by hand"
    assert analyzed.digest() != before


def test_no_records_produces_an_empty_finding_set_not_an_error() -> None:
    """ "The analyzers looked and found nothing" is a result worth showing."""
    results = waste.analyze(
        run_id="run", records=[], summary=profile.discover([]), path=WASTE_CATALOG
    )
    assert results.findings == []
    assert results.portfolio_usd_micros == 0
    assert results.analyzed_nothing


def test_an_analyzed_run_is_not_reported_as_unanalyzed(analyzed: FindingSet) -> None:
    """The other half of the discriminator: a real run must not read as empty."""
    assert not analyzed.analyzed_nothing
    assert analyzed.analyzed_records == 10
