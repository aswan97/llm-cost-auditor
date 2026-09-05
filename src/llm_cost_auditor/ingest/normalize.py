"""Retry and duplicate-delivery classification (SPEC.md §6.5.1).

This is the single largest source of double-counted spend in the whole
pipeline, so the rule is explicit rather than left to per-provider judgment.

Two records sharing a `request_id` are one of two very different things, and
the request id alone cannot tell them apart — some clients reuse an idempotency
key across attempts, and at-least-once log delivery re-emits the same event.
The distinguisher is **whether the billing facts are identical**:

| Condition                                     | Classification    | Billing              |
|-----------------------------------------------|-------------------|----------------------|
| Identical start time, status, and every usage  | Duplicate delivery| Collapse to one      |
| Any billing fact differs                       | Genuine retry     | Keep both, link them |

The asymmetry is deliberate: over-counting a retry is visible in
reconciliation against the invoice, while silently collapsing two billed
attempts is not. So anything ambiguous is kept.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..records import RequestRecord

# Which path the classification took, reported per source in the coverage panel
# (§6.5.1). An adapter that carries a provider-side unique event id would
# report `event_id` here and skip the heuristic; none does yet.
DEDUPE_METHOD = "billing_facts"


@dataclass
class NormalizeResult:
    """Normalized records plus what the classification had to decide."""

    records: list[RequestRecord] = field(default_factory=list)
    duplicates_collapsed: int = 0
    retry_attempts_linked: int = 0
    cross_object_warnings: list[str] = field(default_factory=list)
    dedupe_method: str = DEDUPE_METHOD


def normalize(records: list[RequestRecord]) -> NormalizeResult:
    """Collapse duplicate deliveries, link genuine retries, keep the rest."""
    result = NormalizeResult()

    by_request_id: dict[str, list[RequestRecord]] = defaultdict(list)
    for record in records:
        by_request_id[record.request_id].append(record)

    for request_id, group in by_request_id.items():
        if len(group) == 1:
            result.records.append(group[0])
            continue

        # One bucket per distinct set of billing facts. Each bucket is one
        # billed attempt; more than one record in a bucket is the same event
        # delivered more than once.
        buckets: dict[tuple[Any, ...], list[RequestRecord]] = defaultdict(list)
        for record in group:
            buckets[record.billing_facts()].append(record)

        attempts: list[RequestRecord] = []
        for duplicates in buckets.values():
            kept = duplicates[0]
            if len(duplicates) > 1:
                result.duplicates_collapsed += len(duplicates) - 1
                kept = kept.model_copy(
                    update={"flags": kept.flags.model_copy(update={"duplicate_delivery": True})}
                )
                uris = sorted({d.object_uri for d in duplicates})
                if len(uris) > 1:
                    # The same event from two different objects is an ingest
                    # concern, not a provider one: overlapping connection
                    # prefixes, or an export re-uploaded under a new key
                    # (§6.6). It is still a collapse — but it is reported,
                    # because the alternative is quietly inflated spend.
                    result.cross_object_warnings.append(
                        f"request {request_id} was delivered by {len(uris)} objects "
                        f"({', '.join(uris)}); collapsed as a duplicate delivery. "
                        f"Overlapping connection prefixes are the usual cause (§6.6)."
                    )
            attempts.append(kept)

        if len(attempts) == 1:
            result.records.append(attempts[0])
            continue

        # Genuine retries. Ordered by start time so `attempt_index` means what
        # it says; billing then depends on why each attempt happened, which is
        # the status the adapter already classified (§6.5.1).
        attempts.sort(key=lambda r: r.start_time)
        result.retry_attempts_linked += len(attempts)
        for index, attempt in enumerate(attempts):
            result.records.append(
                attempt.model_copy(
                    update={
                        "attempt_index": index,
                        "parent_request_id": request_id if index else None,
                    }
                )
            )

    result.records.sort(key=lambda r: (r.start_time, r.request_id, r.attempt_index))
    return result
