"""Workload discovery — the profile stage (SPEC.md §8).

Everything downstream depends on this grouping being right, so it is worth
being exact about how much of §8 this release actually implements.

**What runs here: §8.2 step 2 — grouping by declared labels.** Requests are
grouped by the labels the logs already carry (project, `tag.service`, endpoint,
API key), which is the zero-config path of §8.4. A request carrying none of them
lands in `unmapped`, which is reported rather than hidden, because an audit
where most traffic is one anonymous bucket is one whose per-workload findings
mean very little.

**What does not run here, and why not now:**

* **The declared architecture map (§8.1).** It is a set of priors over this same
  grouping; without analyzers that consume `blast_radius`, `latency_slo`, or
  `shared_assets`, loading it would validate config nothing reads.
* **Template-fingerprint subdivision (§8.2 step 3).** It needs per-segment
  hashes — Tier B (§6.3) — and the normalization of variable slots that the
  prefix analyzer builds anyway. Written now it would be written twice.
* **The four profile axes (§8.3).** Every one of them *gates* something:
  determinism gates response and semantic caching, latency tolerance gates
  batching and slower-cheaper routing, prefix stability gates prefix caching,
  and blast radius suppresses downgrades and cascades. None of those analyzers
  exists in this release. Waste findings (§9.1) are deliberately ungated — a
  billed failure is waste at any blast radius — so an axis built now would be a
  gate with nothing behind it, scored against no consumer that could show it
  wrong.

That is stated in `limitations` on the result and printed by the CLI, so a
reader is told what the grouping is rather than left to assume it is the full
hierarchy §8.2 describes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .records import RequestRecord

# The bucket for traffic carrying none of the grouping labels. Named by §8.1,
# which also requires it be reported rather than quietly folded into a total.
UNMAPPED = "unmapped"

# Labels that describe *what a request is for*, in the order they read best in a
# workload name. Deliberately excludes `user_id` and `session_id`: those
# identify who made a request, not what it does, and grouping on them produces
# one workload per user — thousands of clusters with one request each, which is
# not a partition of the traffic but a copy of it.
GROUPING_LABELS = ("project", "tag.service", "endpoint", "api_key_id")

METHOD = "declared_labels"


class Workload(BaseModel):
    """One discovered cluster of traffic (§8.2).

    `matcher` carries the exact label values that define the cluster, so the
    grouping is reproducible from the run record alone and a user can paste it
    into `workloads.yaml` to pin or override it (§8.2 step 4).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    matcher: dict[str, str] = Field(default_factory=dict)
    records: int = Field(default=0, ge=0)
    models: list[str] = Field(default_factory=list)
    observed_start: datetime | None = None
    observed_end: datetime | None = None

    @property
    def is_unmapped(self) -> bool:
        return self.id == UNMAPPED


class ProfileSummary(BaseModel):
    """The profile stage's output, stored on the run record.

    Cluster quality is part of the result rather than a log line (§8.2): the
    unmapped share is what tells a reader whether the per-workload breakdown
    below it is worth reading.
    """

    model_config = ConfigDict(extra="forbid")

    method: str = METHOD
    workloads: list[Workload] = Field(default_factory=list)
    total_records: int = Field(default=0, ge=0)
    unmapped_records: int = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list)

    @property
    def unmapped_pct(self) -> float:
        if not self.total_records:
            return 0.0
        return self.unmapped_records / self.total_records * 100.0

    def workload_ids(self) -> list[str]:
        return [workload.id for workload in self.workloads]


LIMITATIONS = (
    "Grouping is by declared labels only (SPEC.md §8.2 step 2). Traffic that shares an API "
    "key but serves several prompt shapes is one workload here; splitting it needs template "
    "fingerprinting over per-segment hashes (§8.2 step 3, Tier B).",
    "No architecture map is applied (§8.1) and no profile axes are scored (§8.3). The axes "
    "gate routing, batching, and cache findings, none of which exist in this release; waste "
    "findings (§9.1) are ungated by design.",
)


@dataclass
class _Bucket:
    """One cluster under construction. Not part of the contract — see `Workload`."""

    matcher: dict[str, str]
    records: int = 0
    models: set[str] = field(default_factory=set)
    start: datetime | None = None
    end: datetime | None = None

    def add(self, record: RequestRecord) -> None:
        self.records += 1
        self.models.add(f"{record.provider}/{record.model}")
        seen_end = record.end_time or record.start_time
        self.start = record.start_time if self.start is None else min(self.start, record.start_time)
        self.end = seen_end if self.end is None else max(self.end, seen_end)


def _label_values(record: RequestRecord) -> dict[str, str]:
    """The grouping labels this record carries, in `GROUPING_LABELS` order."""
    labels = record.labels
    available = {
        "project": labels.project,
        "tag.service": labels.tags.get("service"),
        "endpoint": labels.endpoint,
        "api_key_id": labels.api_key_id,
    }
    return {name: value for name in GROUPING_LABELS if (value := available[name])}


def discover(records: Iterable[RequestRecord]) -> ProfileSummary:
    """Group records into workloads by the labels they carry.

    Workload ids read as the label values joined by `/`. When two different
    label combinations would produce the same id — `project: claims` and
    `endpoint: claims` both reading as `claims` — every id in the run switches
    to the fully qualified `name=value` form instead. Doing it for the whole run
    rather than only the colliding pair keeps the ids consistent with each other,
    which matters because they are what findings reference.
    """
    buckets: dict[tuple[tuple[str, str], ...], _Bucket] = {}
    total = 0

    for record in records:
        total += 1
        values = _label_values(record)
        key = tuple(values.items())
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = _Bucket(matcher=values)
        bucket.add(record)

    ids = {key: _slug(dict(key)) for key in buckets}
    if len(set(ids.values())) != len(ids):
        ids = {key: _qualified_slug(dict(key)) for key in buckets}

    workloads = [
        Workload(
            id=ids[key],
            matcher=bucket.matcher,
            records=bucket.records,
            models=sorted(bucket.models),
            observed_start=bucket.start,
            observed_end=bucket.end,
        )
        for key, bucket in buckets.items()
    ]
    # Largest first: the workload a reader acts on is usually the biggest one,
    # and it should not have to be found. Ties break on id so the order is
    # stable across runs over the same data.
    workloads.sort(key=lambda w: (-w.records, w.id))

    return ProfileSummary(
        method=METHOD,
        workloads=workloads,
        total_records=total,
        unmapped_records=sum(w.records for w in workloads if w.is_unmapped),
        limitations=list(LIMITATIONS),
    )


def _slug(values: dict[str, str]) -> str:
    return "/".join(values[name] for name in GROUPING_LABELS if name in values) or UNMAPPED


def _qualified_slug(values: dict[str, str]) -> str:
    joined = "/".join(f"{name}={values[name]}" for name in GROUPING_LABELS if name in values)
    return joined or UNMAPPED


def workload_of(record: RequestRecord, summary: ProfileSummary) -> str:
    """Which discovered workload a record belongs to.

    Recomputed from the record's labels rather than stored on it: the record is
    the ingest stage's output and is immutable once written, and a workload id
    is a property of the profile, which a later run may produce differently.
    """
    values = _label_values(record)
    for workload in summary.workloads:
        if workload.matcher == values:
            return workload.id
    return UNMAPPED
