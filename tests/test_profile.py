"""Workload discovery — the profile stage (SPEC.md §8.2).

Everything downstream depends on this grouping being right, and the failure
mode is not a crash: it is a per-workload finding attributed to the wrong
workload, which reads exactly like a correct one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from llm_cost_auditor import profile
from llm_cost_auditor.records import Labels, RequestRecord

AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def record(request_id: str = "req", at: datetime = AT, **labels: object) -> RequestRecord:
    tags = labels.pop("tags", {})
    return RequestRecord(
        request_id=request_id,
        source="anthropic",
        provider="anthropic",
        model="test-flat",
        connection_id="conn",
        object_uri="file:///fixture",
        start_time=at,
        labels=Labels(tags=dict(tags), **labels),  # type: ignore[arg-type]
    )


def test_labels_become_a_readable_workload_id() -> None:
    summary = profile.discover([record(project="claims", tags={"service": "extract"})])
    [workload] = summary.workloads
    assert workload.id == "claims/extract"
    assert workload.matcher == {"project": "claims", "tag.service": "extract"}
    assert workload.records == 1


def test_unlabelled_traffic_is_named_and_counted_not_hidden() -> None:
    """§8.1: the unmapped bucket is reported, because it is what tells a reader
    whether the per-workload breakdown above it is worth reading."""
    summary = profile.discover([record("a"), record("b"), record("c", project="claims")])
    assert summary.unmapped_records == 2
    assert summary.unmapped_pct == 2 / 3 * 100
    assert profile.UNMAPPED in summary.workload_ids()


def test_user_and_session_ids_do_not_split_a_workload() -> None:
    """Grouping on who made a request produces one cluster per user, which is
    not a partition of the traffic but a copy of it."""
    summary = profile.discover(
        [
            record("a", project="claims", user_id="u1", session_id="s1"),
            record("b", project="claims", user_id="u2", session_id="s2"),
        ]
    )
    assert [w.id for w in summary.workloads] == ["claims"]
    assert summary.workloads[0].records == 2


def test_colliding_ids_switch_the_whole_run_to_the_qualified_form() -> None:
    """`project: claims` and `endpoint: claims` both read as `claims`.

    Every id switches, not only the pair that collided: ids are what findings
    reference, and half a run in one naming scheme is worse than either scheme.
    """
    summary = profile.discover(
        [record("a", project="claims"), record("b", endpoint="claims"), record("c", project="x")]
    )
    ids = set(summary.workload_ids())
    assert ids == {"project=claims", "endpoint=claims", "project=x"}


def test_workloads_are_ordered_largest_first() -> None:
    records = [record(f"s{i}", project="small") for i in range(2)]
    records += [record(f"b{i}", project="big") for i in range(5)]
    summary = profile.discover(records)
    assert summary.workload_ids() == ["big", "small"]


def test_a_workload_records_its_models_and_observed_span() -> None:
    later = datetime(2026, 8, 9, 17, 0, tzinfo=UTC)
    summary = profile.discover([record("a", project="p"), record("b", at=later, project="p")])
    [workload] = summary.workloads
    assert workload.models == ["anthropic/test-flat"]
    assert workload.observed_start == AT
    assert workload.observed_end == later


def test_the_summary_states_what_the_grouping_cannot_see() -> None:
    """A grouping whose limits are not stated reads as the full §8.2 hierarchy."""
    summary = profile.discover([record()])
    assert summary.method == profile.METHOD
    assert len(summary.limitations) == 2
    assert any("fingerprint" in text for text in summary.limitations)
    assert any("§8.3" in text for text in summary.limitations)


def test_workload_of_recomputes_from_the_record_labels() -> None:
    """Findings name a workload; a record must resolve to the same one it was counted in."""
    records = [record("a", project="claims"), record("b")]
    summary = profile.discover(records)
    assert profile.workload_of(records[0], summary) == "claims"
    assert profile.workload_of(records[1], summary) == profile.UNMAPPED


def test_no_records_is_no_workloads_rather_than_an_empty_bucket() -> None:
    summary = profile.discover([])
    assert summary.workloads == []
    assert summary.total_records == 0
    assert summary.unmapped_pct == 0.0
