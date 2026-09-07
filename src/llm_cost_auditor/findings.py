"""The finding schema — what an analyzer produces (SPEC.md §13.4).

One data contract, shared by every analyzer and both surfaces. It is a pydantic
model from day one for the reason AGENTS.md gives: everything downstream is
validated against it, and `findings.json` is the file a user reads, diffs, and
quotes from.

Three of its shapes are load-bearing and easy to get wrong, so they are enforced
here rather than left to each analyzer's discretion:

* **Every monetary field is an integer count of micro-USD** and is named
  `_usd_micros` (§6.4). JSON has no decimal type, so a field written as
  `1420.50` comes back a float and quietly defeats the exact-equality rule the
  fixtures depend on. `SavingsRange` additionally refuses `low > expected` and
  `expected > high`, because an inverted range reads as a real number.

* **`gross` and `realizable` are always both present** (§13.4). A finding
  suppressed to `$0` realizable still states what it would have been worth, and
  `realizability_reason` names what suppressed it, so a zero is never
  unexplained.

* **`slices` is never empty.** A finding that cannot name the slices it covers
  can be read as covering traffic its analyzer never saw (§5.2), which is
  precisely the claim this tool must not make by accident.

`standalone` and `marginal` are both carried, never the marginal alone (§11.2):
a large gap between them is itself the report saying "this finding is mostly
someone else's dollars", and hiding it is how these tools produce totals nobody
can reproduce on the next invoice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .records import Fidelity

# The dependency order findings are applied in (§11.2). Waste first, because a
# wasted request is not traffic to be optimized — it is traffic that should not
# exist, and crediting a cache finding with dollars on requests the waste fix
# deletes outright is how a re-audit shows caching under-delivering for reasons
# nobody can reconstruct. Only the first family exists in this release; the
# others are listed so the ordering is declared once, in one place.
ANALYZER_FAMILY_ORDER = ("waste", "routing", "cache", "batch")


class Confidence(StrEnum):
    """How a finding's number was arrived at (§11.3).

    Report totals are given per tier, so a heuristic never silently enters a
    board deck. Penalties only ever move a finding *down* this list.
    """

    MEASURED = "Measured"
    SIMULATED = "Simulated"
    ESTIMATED = "Estimated"
    HEURISTIC = "Heuristic"

    @property
    def rank(self) -> int:
        """Lower is stronger, so `max()` over a set of tiers is the weakest one."""
        return list(Confidence).index(self)

    def capped_at(self, ceiling: Confidence) -> Confidence:
        """This tier, weakened to `ceiling` if it is currently stronger."""
        return self if self.rank >= ceiling.rank else ceiling


class Realizable(StrEnum):
    """Whether the saving actually reaches the bill (§13.4).

    An enum rather than a boolean because §7.2 needs three answers a boolean
    cannot give: savings suppressed by a committed spend floor are worth `$0`
    realizable against a real gross number, and savings against prepaid credit
    are `deferred` — a burn-rate extension, not a budget reduction.
    """

    YES = "yes"
    NO = "no"
    DEFERRED = "deferred"
    PARTIAL = "partial"


class Risk(StrEnum):
    """How much could go wrong applying the remediation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Effort(StrEnum):
    """Rough implementation size, in the usual t-shirt sizes."""

    S = "S"
    M = "M"
    L = "L"


class SliceRef(BaseModel):
    """One `(connection_id, source, fidelity)` slice a finding covers (§5.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: str
    source: str
    fidelity: Fidelity

    @property
    def label(self) -> str:
        return f"{self.connection_id}/{self.source}/tier {self.fidelity.value}"


class ConfidencePenalty(BaseModel):
    """Why a finding is not at the tier its arithmetic would otherwise support.

    A list rather than a prose note (§11.3), so "why is this only Estimated"
    is answerable mechanically rather than by reading a paragraph.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    effect: str  # e.g. "cap:Estimated"
    detail: str


class Evidence(BaseModel):
    """What the finding was computed from, in the analyzer's own terms.

    The common fields are promoted because every consumer wants them; the rest
    is analyzer-specific and lands in `detail`. Assumptions are stated inline
    rather than in the remediation prose, because they are what a reader
    disputes first.
    """

    model_config = ConfigDict(extra="forbid")

    requests: int = Field(default=0, ge=0)
    window_days: float | None = None
    method: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class PricingProvenance(BaseModel):
    """Which catalog produced the money, and how stale it is.

    Travels with the finding because a figure whose catalog version and
    verification date got separated from it is one nobody can audit later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: str
    resolution: str
    oldest_last_verified: str | None = None


class SavingsRange(BaseModel):
    """A low / expected / high band, in integer micro-USD (§11.3, §6.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    low_usd_micros: int = Field(ge=0)
    expected_usd_micros: int = Field(ge=0)
    high_usd_micros: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not (self.low_usd_micros <= self.expected_usd_micros <= self.high_usd_micros):
            raise ValueError(
                f"savings range is inverted: low={self.low_usd_micros} "
                f"expected={self.expected_usd_micros} high={self.high_usd_micros}"
            )
        return self

    @classmethod
    def exact(cls, amount: int) -> SavingsRange:
        """A range with no spread — what a Measured finding produces."""
        return cls(low_usd_micros=amount, expected_usd_micros=amount, high_usd_micros=amount)

    @property
    def is_zero(self) -> bool:
        return self.high_usd_micros == 0


class Savings(BaseModel):
    """What a finding is worth, gross and realizable, standalone and marginal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str = "USD"
    basis: str = "observed_window"

    gross_standalone: SavingsRange
    gross_marginal: SavingsRange

    realizable: Realizable
    realizable_range: SavingsRange
    realizability_reason: str | None = None

    # Withheld unless the §11.4 coverage checks pass. `None` means "not
    # produced", which the report must render as a stated absence rather than
    # as zero.
    projection_monthly_usd_micros: int | None = None

    @model_validator(mode="after")
    def _zero_is_explained(self) -> Self:
        """A `$0` realizable against a non-zero gross must name what suppressed it."""
        if (
            self.realizable is not Realizable.YES
            and self.realizable_range.is_zero
            and not self.gross_marginal.is_zero
            and not self.realizability_reason
        ):
            raise ValueError(
                "a finding whose realizable savings are zero against a non-zero gross must "
                "set realizability_reason (SPEC.md §13.4)"
            )
        return self


class Verdict(StrEnum):
    """What an analyzer can do with one slice (§5.2).

    Applicability is per-slice, never per-dataset: a dataset is routinely mixed,
    and a single dataset-wide verdict has no correct answer — `RUN` produces
    numbers over data that cannot support them, `BLOCKED` lets one weak source
    suppress findings for strong ones.
    """

    RUN = "run"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class SliceVerdict(BaseModel):
    """One cell of the analyzer x slice verdict matrix (§5.2, §13.1).

    Recorded for every pair, including the ones that produced nothing, because
    this is what makes "why is there no cache finding for Bedrock" answerable
    rather than a silence a reader has to interpret.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyzer: str
    slice: SliceRef
    verdict: Verdict
    reason: str | None = None


class Remediation(BaseModel):
    """What to actually do about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    snippet: str | None = None
    files_hint: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """One finding, as written to `findings.json` (SPEC.md §13.4)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    title: str
    analyzer: str
    workloads: list[str] = Field(default_factory=list)
    slices: list[SliceRef]

    confidence: Confidence
    confidence_penalties: list[ConfidencePenalty] = Field(default_factory=list)

    evidence: Evidence
    pricing: PricingProvenance
    savings: Savings

    risk: Risk
    effort: Effort
    remediation: Remediation
    verification: list[str] = Field(default_factory=list)

    # Filled by `verify` (§13.6): implemented | partially_implemented |
    # not_detected. `None` until then, and never inferred.
    verification_status: str | None = None

    @model_validator(mode="after")
    def _names_its_slices(self) -> Self:
        if not self.slices:
            raise ValueError(
                f"finding {self.id!r} names no slices. A finding that cannot say which traffic "
                f"it covers can be read as covering traffic it never saw (SPEC.md §5.2)."
            )
        return self

    @property
    def family(self) -> str:
        """The analyzer family, which is what §11.2 orders findings by."""
        return self.analyzer.split(".", 1)[0]


class FindingSet(BaseModel):
    """`findings.json` — every finding for one run, plus the portfolio total.

    The total is the sum of **marginals only**, and the model asserts it rather
    than trusting the caller: the property that per-finding marginals sum
    exactly to the portfolio total is the invariant that catches attribution
    bugs, and it is only achievable in integers (§6.4, §11.2).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    generated_at: datetime
    catalog_version: str
    baseline_usd_micros: int = Field(default=0, ge=0)

    findings: list[Finding] = Field(default_factory=list)

    # Analyzer x slice verdicts, so "why is there no cache finding for Bedrock"
    # is answerable (§5.2). Every analyzer that ran, was degraded, or was
    # blocked on a slice appears here, whether or not it produced a finding.
    applicability: list[SliceVerdict] = Field(default_factory=list)

    # Named rather than derived at render time, so the CLI, the API, and the app
    # cannot disagree about whether savings were withheld (§11.4).
    savings_withheld: bool = False
    withheld_reasons: list[str] = Field(default_factory=list)

    @property
    def portfolio_usd_micros(self) -> int:
        """The sum of marginals only — the one number that may be totalled."""
        return sum(f.savings.gross_marginal.expected_usd_micros for f in self.findings)

    def by_confidence(self) -> dict[Confidence, int]:
        """Marginal expected savings per tier, so a heuristic never enters a total silently."""
        totals: dict[Confidence, int] = {}
        for finding in self.findings:
            amount = finding.savings.gross_marginal.expected_usd_micros
            totals[finding.confidence] = totals.get(finding.confidence, 0) + amount
        return totals

    def digest(self) -> str:
        """A SHA-256 content digest over the finding set (§13.6).

        It detects accidental edits and identifies the exact document a diff was
        taken against. It is **not a signature** and makes no claim about who
        produced it — there is no signing key anywhere in this project.
        """
        payload = json.dumps(
            self.model_dump(mode="json", exclude={"generated_at"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_finding_set(run_id: str, *, catalog_version: str) -> FindingSet:
    return FindingSet(
        run_id=run_id,
        generated_at=datetime.now(UTC),
        catalog_version=catalog_version,
    )
