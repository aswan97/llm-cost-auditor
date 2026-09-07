"""Waste findings — the first analyzer (SPEC.md §9.1).

Cheap to compute, uncontroversial, immediately actionable, and available even
on billing-only logs. This is the credibility beachhead: every number below is
a sum of amounts the provider already charged, so a reader can check it against
their own invoice rather than trusting a model.

A plain module against the §5.2 protocol, not a plugin behind a registry: the
registry earns its keep at three analyzers, not one, and extracting it before
the second and third exist means designing it against a shape nobody has seen
(AGENTS.md).

## What this builds, from the §9.1 table

| Finding                  | Detection                                    | Tier |
|--------------------------|----------------------------------------------|------|
| `waste.retry_storm`      | Billed attempts superseded by a later attempt | Measured |
| `waste.billed_failure`   | `error_billed` — input charged, nothing usable | Measured |
| `waste.truncation`       | `stop_reason=max_tokens`, billed in full      | Estimated |
| `waste.cancelled_stream` | Client disconnect after generation started    | Measured |
| `waste.rate_limit_churn` | 429s and the round trips they cost            | Measured |

All five run on Tier C (§6.3), which is the point of starting here: they are
the findings a first-time user gets from a raw billing export, before doing any
instrumentation work.

## What it deliberately does not build yet

* **Oversized `max_tokens`.** It is a distribution question — reserved versus
  actually generated, per workload — and the threshold that turns that
  distribution into a finding is a number with no evidence behind it until
  there is real traffic to calibrate against. It also bills `$0` on both
  providers modelled here, so shipping it early buys a magic constant and no
  dollars.
* **Duplicate in-flight requests.** Needs a request fingerprint (Tier B) *and*
  a model of which requests overlap in time. That overlap model is the same one
  §9.2's cache simulator needs for its burst behaviour, and it should be built
  once, with the analyzer that depends on it most.
* **Redundant context.** Tier A and Simulated (§9.1), and its evidence comes
  from the prefix work in §9.2.

Each is an absence stated here rather than an empty function, because a stub
that returns no findings is indistinguishable in a report from an analyzer that
looked and found nothing.

## Why the ids say `retry_storm` when the pattern may be a single retry

`waste.retry_storm` is the §9.1 row name, and the id is the stable vocabulary
the spec defines. The *title* says what was actually observed — how many billed
attempts across how many chains — so a one-retry finding never reads as a storm
just because of what its id is called.

## Overlap

These findings overlap on purpose: a 5xx-after-generation attempt is both an
`error_billed` record and a superseded retry. Each finding therefore reports
its standalone worth and its marginal contribution against everything ranked
above it (§11.2), and only the marginals are ever totalled. Ranking retries
first is deliberate — a billed failure sitting inside a retry chain is a
symptom of the retry policy, and the fix is the retry policy.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import attribution, pricing
from .errors import MissingPriceError
from .findings import (
    Confidence,
    ConfidencePenalty,
    Effort,
    Evidence,
    Finding,
    FindingSet,
    PricingProvenance,
    Realizable,
    Remediation,
    Risk,
    Savings,
    SavingsRange,
    SliceRef,
    SliceVerdict,
    Verdict,
    new_finding_set,
)
from .ingest.manifest import Coverage, SliceSummary
from .profile import ProfileSummary, workload_of
from .records import Fidelity, RequestRecord, Status

ANALYZER = "waste"

RETRY_STORM = "waste.retry_storm"
BILLED_FAILURE = "waste.billed_failure"
TRUNCATION = "waste.truncation"
CANCELLED_STREAM = "waste.cancelled_stream"
RATE_LIMIT_CHURN = "waste.rate_limit_churn"

# Attribution order within the family (§11.2). Retries first: a billed failure
# or a truncation inside a retry chain is a symptom of the retry policy, and
# crediting it to the wrong finding sends the reader to the wrong fix.
DETECTOR_ORDER = (
    RETRY_STORM,
    BILLED_FAILURE,
    TRUNCATION,
    CANCELLED_STREAM,
    RATE_LIMIT_CHURN,
)

# Every one of these runs on billing-only logs, which is the whole point of
# starting with waste (§6.3, §9.1).
REQUIRED_FIDELITY = Fidelity.BILLING

# The guard matches "rate" and reads this as a price. It is an HTTP status code
# — the "rate" is requests per minute, not dollars per token — so it is
# suppressed here rather than renamed around the check (AGENTS.md).
RATE_LIMIT_HTTP_STATUS = 429  # noqa: price-literal

# No commercial overlay exists yet (§7.2), so every figure is public list price
# and the finding says so rather than leaving a reader to assume otherwise.
PRICING_RESOLUTION = "list"

_MONTHLY_PROJECTION_UNAVAILABLE = (
    "No monthly projection is produced. A projection needs the §11.4 coverage checks — at "
    "least 7 days of logs, complete day boundaries in the configured timezone, and detected "
    "seasonality accounted for — and none of that is implemented in this release. Every "
    "figure below is over the observed window only."
)


def record_key(record: RequestRecord) -> str:
    """A key unique per billed attempt.

    `request_id` alone is not: a retry chain shares one id across its attempts
    (§6.5.1), so keying on it would let one finding's claim on attempt 0 silently
    absorb another's claim on attempt 1.
    """
    return f"{record.request_id}#{record.attempt_index}"


@dataclass
class _Claim:
    """One detector's take on one workload, before attribution.

    Three cost bands rather than one because a finding's uncertainty is about
    *how much of a billed amount the fix recovers*, not about what was billed.
    Attributing each band separately keeps every number an exact integer; a
    single band scaled by a fraction afterwards would not.
    """

    analyzer: str
    workload: str
    title: str
    confidence: Confidence
    evidence: Evidence
    risk: Risk
    effort: Effort
    remediation: Remediation
    verification: list[str]

    low: dict[str, int] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)
    high: dict[str, int] = field(default_factory=dict)
    slices: set[SliceRef] = field(default_factory=set)
    penalties: list[ConfidencePenalty] = field(default_factory=list)

    def rank(self) -> tuple[int, int, str, str]:
        """Attribution order: detector first, then size, then a stable tiebreak."""
        return (
            DETECTOR_ORDER.index(self.analyzer),
            -sum(self.expected.values()),
            self.workload,
            self.analyzer,
        )


@dataclass
class _Priced:
    """A record with its billed cost, so nothing is priced twice."""

    record: RequestRecord
    cost_usd_micros: int
    workload: str
    slice_ref: SliceRef
    last_verified: date


def analyze(
    *,
    run_id: str,
    records: Iterable[RequestRecord],
    summary: ProfileSummary,
    slices: Sequence[SliceSummary] = (),
    coverage: Coverage | None = None,
    path: Path | None = None,
) -> FindingSet:
    """Run every waste detector over one run's records and attribute the result.

    Records whose model has no catalog rate are excluded and named rather than
    priced at zero — a guessed dollar figure is indistinguishable from a real
    one once it reaches a slide (§7.1).
    """
    catalog = pricing.catalog(path)
    result = new_finding_set(run_id, catalog_version=catalog.version)

    priced, unpriced, oldest = _price(records, summary, slices, path)
    result.baseline_usd_micros = sum(item.cost_usd_micros for item in priced)

    observed = [item.record.start_time for item in priced]
    window_days = _window_days(observed)

    claims: list[_Claim] = []
    for detector in (
        _retry_storms,
        _billed_failures,
        _truncations,
        _cancelled_streams,
        _rate_limit_churn,
    ):
        claims.extend(detector(priced, window_days))

    claims.sort(key=_Claim.rank)
    result.findings = _to_findings(run_id, claims, catalog.version, oldest)
    result.applicability = _applicability(slices)

    result.withheld_reasons.append(_MONTHLY_PROJECTION_UNAVAILABLE)
    for provider, model, count in unpriced:
        result.withheld_reasons.append(
            f"{count} record(s) on {provider}/{model} have no catalog rate and are excluded "
            f"from every figure here. Their waste, if any, is not counted (§7.1)."
        )
    if coverage is not None and coverage.baseline_is_lower_bound:
        result.withheld_reasons.append(
            "Objects in this run could not be read, so these savings are a lower bound over "
            "the traffic that was: waste in the unread objects is not counted (§6.1, §11.4)."
        )
    return result


def applicable(slice_summary: SliceSummary) -> SliceVerdict:
    """Waste findings run on every slice (§5.2, §9.1).

    Deliberately ungated: a billed failure is waste at any fidelity, any blast
    radius, and any workload profile. The one thing fidelity changes is whether
    a truncation's re-request can be *confirmed*, and that is a confidence
    penalty on one finding rather than a reason to withhold the family.
    """
    reference = SliceRef(
        connection_id=slice_summary.connection_id,
        source=slice_summary.source,
        fidelity=slice_summary.fidelity,
    )
    return SliceVerdict(analyzer=ANALYZER, slice=reference, verdict=Verdict.RUN)


# --- detectors ----------------------------------------------------------------


def _retry_storms(priced: Sequence[_Priced], window_days: float | None) -> list[_Claim]:
    """Billed attempts that a later attempt superseded (§9.1, §6.5.1).

    An attempt that was billed and then re-issued is spend that bought nothing:
    the answer the caller kept came from a different attempt. Only attempts that
    were actually billed are claimed — a 429 rejected before generation costs
    nothing here and belongs to `waste.rate_limit_churn`.
    """
    chains: dict[str, list[_Priced]] = defaultdict(list)
    for item in priced:
        chains[item.record.request_id].append(item)

    per_workload: dict[str, _Claim] = {}
    chain_counts: dict[str, int] = defaultdict(int)
    longest: dict[str, int] = defaultdict(int)
    codes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for attempts in chains.values():
        if len(attempts) < 2:
            continue
        attempts.sort(key=lambda item: item.record.attempt_index)
        superseded = [item for item in attempts[:-1] if item.cost_usd_micros > 0]
        if not superseded:
            continue

        workload = superseded[0].workload
        claim = per_workload.get(workload)
        if claim is None:
            claim = per_workload[workload] = _Claim(
                analyzer=RETRY_STORM,
                workload=workload,
                title="",
                confidence=Confidence.MEASURED,
                evidence=Evidence(window_days=window_days, method="attempt_chains"),
                risk=Risk.LOW,
                effort=Effort.M,
                remediation=Remediation(
                    summary=(
                        "Retry only what a retry can fix. A 5xx that arrived after generation "
                        "was already billed, so re-issuing it pays twice for one answer: retry "
                        "on connection and pre-generation errors, and surface post-generation "
                        "failures to the caller instead. Where a retry is genuinely required, "
                        "send an idempotency key so the provider can return the original "
                        "response rather than generating a second one."
                    ),
                    files_hint=["the HTTP client or SDK retry policy for this workload"],
                ),
                verification=[
                    "Re-run the audit over a window after the policy change",
                    "Expect billed attempts per chain to fall to 1 for post-generation errors",
                ],
            )
        chain_counts[workload] += 1
        longest[workload] = max(longest[workload], len(attempts))
        for item in superseded:
            _add(claim, item, item.cost_usd_micros)
            label = item.record.error_code or str(item.record.http_status or "unknown")
            codes[workload][label] += 1

    for workload, claim in per_workload.items():
        attempts_claimed = len(claim.expected)
        claim.evidence.requests = attempts_claimed
        claim.evidence.detail = {
            "chains": chain_counts[workload],
            "billed_superseded_attempts": attempts_claimed,
            "longest_chain_attempts": longest[workload],
            "error_codes": dict(codes[workload]),
        }
        claim.title = (
            f"{attempts_claimed} billed retry attempt(s) across {chain_counts[workload]} "
            f"request chain(s) in {workload}"
        )
    return list(per_workload.values())


def _billed_failures(priced: Sequence[_Priced], window_days: float | None) -> list[_Claim]:
    """`error_billed` — input tokens charged for a request that produced nothing usable."""
    return _simple_claim(
        priced,
        window_days,
        analyzer=BILLED_FAILURE,
        match=lambda item: item.record.status is Status.ERROR_BILLED,
        method="status=error_billed",
        title=lambda count, workload: (
            f"{count} billed failure(s) in {workload} — charged for output nobody received"
        ),
        risk=Risk.LOW,
        effort=Effort.M,
        remediation=Remediation(
            summary=(
                "These requests were charged and returned an error. Find the error codes in "
                "the evidence below and fix the cause: an overlong prompt, an invalid tool "
                "schema, and a provider-side 5xx after generation each bill in full and each "
                "need a different fix. Validate what is cheap to validate before the call "
                "rather than after the bill."
            ),
        ),
        verification=[
            "Re-run the audit after the fix",
            "Expect the error_billed record count for this workload to fall",
        ],
        detail=_error_breakdown,
    )


def _truncations(priced: Sequence[_Priced], window_days: float | None) -> list[_Claim]:
    """`stop_reason=max_tokens` — billed in full, output cut off mid-thought (§6.5.2).

    Whether the caller re-requested is the thing that turns this into recovered
    dollars, and at billing fidelity it is not observable: confirming it needs
    the re-request to be matched by prompt fingerprint, which is Tier B. So the
    band is honest rather than confident — `low` assumes every truncated answer
    was accepted as-is and nothing is recovered, `expected` and `high` assume
    the usual case that a cut-off answer is asked for again.
    """
    claims = _simple_claim(
        priced,
        window_days,
        analyzer=TRUNCATION,
        match=lambda item: item.record.status is Status.TRUNCATED,
        method="stop_reason=max_tokens",
        title=lambda count, workload: (
            f"{count} truncated response(s) in {workload} — billed in full, cut off mid-answer"
        ),
        risk=Risk.LOW,
        effort=Effort.S,
        confidence=Confidence.ESTIMATED,
        remediation=Remediation(
            summary=(
                "Raise `max_tokens` for this workload to above the length its answers "
                "actually need, or shorten what you ask for. A response stopped at the limit "
                "was paid for in full and is usually asked for again, so the second call is "
                "the avoidable one."
            ),
        ),
        verification=[
            "Re-run the audit over a window after raising the limit",
            "Expect stop_reason=max_tokens to disappear for this workload",
        ],
        detail=_max_tokens_breakdown,
        assumptions=[
            "Assumes a response cut off at max_tokens was re-requested. The low bound "
            "assumes none were.",
        ],
    )
    for claim in claims:
        claim.low = dict.fromkeys(claim.expected, 0)
        claim.penalties.append(
            ConfidencePenalty(
                code="unconfirmed_rerequest",
                effect=f"cap:{Confidence.ESTIMATED.value}",
                detail=(
                    "Whether each truncated response was re-requested cannot be observed at "
                    "billing fidelity (§6.3 Tier C). Matching the re-request by prompt "
                    "fingerprint needs per-segment hashes (Tier B), which would make this "
                    "finding Measured."
                ),
            )
        )
    return claims


def _cancelled_streams(priced: Sequence[_Priced], window_days: float | None) -> list[_Claim]:
    """Client disconnects after N generated tokens — billed, and thrown away."""
    return _simple_claim(
        priced,
        window_days,
        analyzer=CANCELLED_STREAM,
        match=lambda item: item.record.status is Status.CANCELLED,
        method="status=cancelled",
        title=lambda count, workload: (
            f"{count} cancelled stream(s) in {workload} — billed for tokens nobody read"
        ),
        risk=Risk.LOW,
        effort=Effort.S,
        remediation=Remediation(
            summary=(
                "A client that disconnects does not stop the provider generating. Propagate "
                "the cancellation to the provider request — abort the HTTP request or cancel "
                "the SDK stream when the caller goes away — so generation stops with the "
                "reader rather than running to completion into a closed socket."
            ),
            files_hint=["the streaming handler for this workload"],
        ),
        verification=[
            "Re-run the audit after wiring cancellation through",
            "Expect output tokens on cancelled records to fall sharply",
        ],
        detail=_cancelled_breakdown,
    )


def _rate_limit_churn(priced: Sequence[_Priced], window_days: float | None) -> list[_Claim]:
    """429s and the round trips they cost (§9.1).

    Usually `$0`: a rate-limited request is rejected before generation and is
    not billed. It is reported anyway, and reported at `$0` rather than
    omitted, because the cost is latency and throughput — and because a reader
    who sees no 429 finding should be able to conclude there were no 429s.
    """
    return _simple_claim(
        priced,
        window_days,
        analyzer=RATE_LIMIT_CHURN,
        match=lambda item: item.record.http_status == RATE_LIMIT_HTTP_STATUS,
        method="http_status=429",
        title=lambda count, workload: (
            f"{count} rate-limited request(s) in {workload} — wasted round trips"
        ),
        risk=Risk.LOW,
        effort=Effort.M,
        remediation=Remediation(
            summary=(
                "Shape concurrency to the rate limit instead of discovering it: a client-side "
                "token-bucket limiter plus exponential backoff with jitter turns a burst of "
                "rejections into a queue. Where the work is not interactive, the same traffic "
                "is a batching candidate — which is where these requests go next (§10.1)."
            ),
            files_hint=["the client's concurrency limiter for this workload"],
        ),
        verification=[
            "Re-run the audit after limiting concurrency",
            "Expect the 429 count to fall without total throughput dropping",
        ],
        detail=_rate_limit_breakdown,
    )


# --- detector plumbing --------------------------------------------------------


def _simple_claim(
    priced: Sequence[_Priced],
    window_days: float | None,
    *,
    analyzer: str,
    match: Callable[[_Priced], bool],
    method: str,
    title: Callable[[int, str], str],
    risk: Risk,
    effort: Effort,
    remediation: Remediation,
    verification: list[str],
    detail: Callable[[Sequence[_Priced]], dict[str, Any]],
    confidence: Confidence = Confidence.MEASURED,
    assumptions: list[str] | None = None,
) -> list[_Claim]:
    """One claim per workload, over the records a predicate selects.

    Four of the five detectors are the same shape — select records, claim their
    billed cost, group by workload — so the shape lives here once. Only the
    retry detector reasons about relationships between records and needs its
    own pass.
    """
    matches: dict[str, list[_Priced]] = defaultdict(list)
    for item in priced:
        if match(item):
            matches[item.workload].append(item)

    claims: list[_Claim] = []
    for workload, items in matches.items():
        claim = _Claim(
            analyzer=analyzer,
            workload=workload,
            title=title(len(items), workload),
            confidence=confidence,
            evidence=Evidence(
                requests=len(items),
                window_days=window_days,
                method=method,
                assumptions=list(assumptions or []),
                detail=detail(items),
            ),
            risk=risk,
            effort=effort,
            remediation=remediation,
            verification=list(verification),
        )
        for item in items:
            _add(claim, item, item.cost_usd_micros)
        claims.append(claim)
    return claims


def _add(claim: _Claim, item: _Priced, amount: int) -> None:
    """Record one claimed attempt, in all three bands, with its slice."""
    key = record_key(item.record)
    claim.low[key] = amount
    claim.expected[key] = amount
    claim.high[key] = amount
    claim.slices.add(item.slice_ref)
    if item.record.flags.usage_estimated:
        _penalize_once(
            claim,
            ConfidencePenalty(
                code="estimated_usage",
                effect=f"cap:{Confidence.ESTIMATED.value}",
                detail=(
                    "At least one claimed record carries usage_estimated: its token counts "
                    "were reconstructed during streaming reassembly rather than reported by "
                    "the provider (§6.5.3), so its cost is an estimate."
                ),
            ),
        )


def _penalize_once(claim: _Claim, penalty: ConfidencePenalty) -> None:
    if not any(existing.code == penalty.code for existing in claim.penalties):
        claim.penalties.append(penalty)


def _error_breakdown(items: Sequence[_Priced]) -> dict[str, object]:
    codes: dict[str, int] = defaultdict(int)
    for item in items:
        codes[item.record.error_code or str(item.record.http_status or "unknown")] += 1
    return {
        "error_codes": dict(codes),
        "input_tokens_billed": sum(item.record.usage.input_tokens for item in items),
        "output_tokens_billed": sum(item.record.usage.output_tokens for item in items),
    }


def _max_tokens_breakdown(items: Sequence[_Priced]) -> dict[str, object]:
    limits = sorted(
        {item.record.params.max_tokens for item in items if item.record.params.max_tokens}
    )
    return {
        "output_tokens_billed": sum(item.record.usage.output_tokens for item in items),
        "max_tokens_settings_observed": limits,
    }


def _cancelled_breakdown(items: Sequence[_Priced]) -> dict[str, object]:
    return {
        "output_tokens_billed": sum(item.record.usage.output_tokens for item in items),
        "input_tokens_billed": sum(item.record.usage.input_tokens for item in items),
    }


def _rate_limit_breakdown(items: Sequence[_Priced]) -> dict[str, object]:
    latencies = [item.record.latency_ms for item in items if item.record.latency_ms is not None]
    detail: dict[str, object] = {
        "rejected_requests": len(items),
        "billed_tokens": sum(
            item.record.usage.input_tokens + item.record.usage.output_tokens for item in items
        ),
    }
    # Omitted rather than reported as `None` when the logs carry no latency: an
    # absent key reads as "not measured", where a null in a table reads as
    # "measured, and it was nothing".
    if latencies:
        detail["wasted_round_trip_ms"] = sum(latencies)
        detail["latency_measured_on_requests"] = len(latencies)
    return detail


# --- pricing and assembly -----------------------------------------------------


def _price(
    records: Iterable[RequestRecord],
    summary: ProfileSummary,
    slices: Sequence[SliceSummary],
    path: Path | None,
) -> tuple[list[_Priced], list[tuple[str, str, int]], date | None]:
    """Price every record once, and name the ones no catalog row covers."""
    tiers = {(item.connection_id, item.source): item.fidelity for item in slices}
    priced: list[_Priced] = []
    unpriced: dict[tuple[str, str], int] = defaultdict(int)
    oldest: date | None = None

    for record in records:
        try:
            cost = pricing.cost_of(record, path=path)
        except MissingPriceError:
            unpriced[(record.provider, record.model)] += 1
            continue
        oldest = cost.last_verified if oldest is None else min(oldest, cost.last_verified)
        priced.append(
            _Priced(
                record=record,
                cost_usd_micros=cost.total_usd_micros,
                workload=workload_of(record, summary),
                slice_ref=SliceRef(
                    connection_id=record.connection_id,
                    source=record.source,
                    fidelity=tiers.get((record.connection_id, record.source), record.fidelity),
                ),
                last_verified=cost.last_verified,
            )
        )

    named = [(provider, model, count) for (provider, model), count in sorted(unpriced.items())]
    return priced, named, oldest


def _window_days(observed: Sequence[datetime]) -> float | None:
    if len(observed) < 2:
        return None
    return (max(observed) - min(observed)).total_seconds() / 86400.0


def _to_findings(
    run_id: str,
    claims: Sequence[_Claim],
    catalog_version: str,
    oldest: date | None,
) -> list[Finding]:
    """Attribute the claims and turn each into a `Finding` (§11.2, §13.4)."""
    bands = {
        "low": attribution.attribute([claim.low for claim in claims]),
        "expected": attribution.attribute([claim.expected for claim in claims]),
        "high": attribution.attribute([claim.high for claim in claims]),
    }

    provenance = PricingProvenance(
        catalog_version=catalog_version,
        resolution=PRICING_RESOLUTION,
        oldest_last_verified=oldest.isoformat() if oldest else None,
    )

    results: list[Finding] = []
    for index, claim in enumerate(claims):
        standalone = SavingsRange(
            low_usd_micros=bands["low"][index].standalone_usd_micros,
            expected_usd_micros=bands["expected"][index].standalone_usd_micros,
            high_usd_micros=bands["high"][index].standalone_usd_micros,
        )
        marginal = SavingsRange(
            low_usd_micros=bands["low"][index].marginal_usd_micros,
            expected_usd_micros=bands["expected"][index].marginal_usd_micros,
            high_usd_micros=bands["high"][index].marginal_usd_micros,
        )

        confidence = claim.confidence
        for penalty in claim.penalties:
            if penalty.effect.startswith("cap:"):
                confidence = confidence.capped_at(Confidence(penalty.effect.split(":", 1)[1]))

        # Name who took the difference, so a marginal below its standalone is
        # never an unexplained number (§11.2). Without this, `$0.00 marginal`
        # against a real standalone reads as "not worth doing" when it means
        # "already counted under the finding above".
        absorbed = [_claim_id(claims[owner]) for owner in bands["expected"][index].absorbed_by]
        if absorbed:
            claim.evidence.detail["marginal_absorbed_by"] = absorbed

        results.append(
            Finding(
                id=_claim_id(claim),
                run_id=run_id,
                title=claim.title,
                analyzer=claim.analyzer,
                workloads=[claim.workload],
                slices=sorted(claim.slices, key=lambda ref: ref.label),
                confidence=confidence,
                confidence_penalties=claim.penalties,
                evidence=claim.evidence,
                pricing=provenance,
                savings=Savings(
                    gross_standalone=standalone,
                    gross_marginal=marginal,
                    # No commercial overlay exists (§7.2), so nothing suppresses
                    # or defers these dollars: with no committed floor and no
                    # prepaid credit, the marginal saving reaches the bill.
                    realizable=Realizable.YES,
                    realizable_range=marginal,
                    projection_monthly_usd_micros=None,
                ),
                risk=claim.risk,
                effort=claim.effort,
                remediation=claim.remediation,
                verification=claim.verification,
            )
        )

    # Biggest marginal first: the report's ranking is what a reader acts on.
    # Ties break on id so two runs over the same data list them identically.
    results.sort(key=lambda f: (-f.savings.gross_marginal.expected_usd_micros, f.id))
    return results


def _claim_id(claim: _Claim) -> str:
    """A finding's id: the §9.1 row name, then the workload it covers (§13.4)."""
    return f"{claim.analyzer}.{claim.workload}"


def _applicability(slices: Sequence[SliceSummary]) -> list[SliceVerdict]:
    return [applicable(item) for item in slices]
