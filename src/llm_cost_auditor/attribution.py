"""Sequential marginal attribution (SPEC.md §11.2).

Findings overlap. A 5xx-after-generation attempt is *both* a billed failure and
a superseded retry; later, a retry storm's prefix will also be genuinely
cacheable. Summing what each finding would save on its own is how these tools
produce a total nobody can reproduce on the next invoice.

So findings are applied in a fixed dependency order and each is credited only
with its **marginal** savings against the already-adjusted baseline. Concretely,
at this stage of the system, a finding claims a set of records and the billed
cost of each; a claim on a record some higher-ranked finding already claimed
adds nothing.

Two properties matter, and both are why this is integer arithmetic:

* **Marginals sum exactly to the portfolio total** — the cost of the *union* of
  every claimed record, counted once. In floats this becomes an approximate
  comparison, which is the same as no test at all (AGENTS.md).
* **Standalone is always kept alongside** (§11.2). A large standalone-versus-
  marginal gap is the report saying "this finding is mostly someone else's
  dollars", and reporting the marginal alone hides exactly that.

This module knows nothing about analyzers. It takes claims already in
dependency order and returns numbers; deciding that order is the caller's job,
and `findings.ANALYZER_FAMILY_ORDER` declares it once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Attribution:
    """What one claim is worth alone, and what it adds given everything above it."""

    standalone_usd_micros: int
    marginal_usd_micros: int

    # Which earlier claims took the difference, by index, largest share first.
    # Carried because a marginal that shrank without saying who took the
    # dollars is the one number in this report a reader cannot check: `$0.00`
    # next to a real standalone reads as "worthless" when it means "already
    # counted over there".
    absorbed_by: tuple[int, ...] = ()

    @property
    def is_wholly_claimed_above(self) -> bool:
        """True when every dollar this finding names was already credited elsewhere."""
        return self.standalone_usd_micros > 0 and self.marginal_usd_micros == 0


def attribute(claims: Sequence[Mapping[str, int]]) -> list[Attribution]:
    """Credit each claim with the cost it is the first to name.

    `claims` is a sequence of `{record key: cost in micro-USD}` mappings, in
    dependency order — highest priority first. A record appearing in more than
    one claim is credited to the earliest one only.

    A record must carry the same cost in every claim that names it: the cost is
    a property of the record, not of the finding, and two different values for
    one record means a caller computed a saving from something other than what
    the record was billed. That is a defect worth failing on rather than
    silently resolving in favour of whichever claim ran first.
    """
    credited: dict[str, tuple[int, int]] = {}
    results: list[Attribution] = []

    for index, claim in enumerate(claims):
        standalone = 0
        marginal = 0
        taken: dict[int, int] = {}
        for key, cost in claim.items():
            if cost < 0:
                raise ValueError(f"claim {index} credits record {key!r} a negative {cost} uUSD")
            previous = credited.get(key)
            if previous is not None and previous[1] != cost:
                raise ValueError(
                    f"record {key!r} is claimed at {previous[1]} uUSD by an earlier finding and "
                    f"{cost} uUSD by claim {index}. A record's cost is a property of the record, "
                    f"so the two cannot both be right (SPEC.md §11.2)."
                )
            standalone += cost
            if previous is None:
                marginal += cost
                credited[key] = (index, cost)
            else:
                taken[previous[0]] = taken.get(previous[0], 0) + cost

        results.append(
            Attribution(
                standalone_usd_micros=standalone,
                marginal_usd_micros=marginal,
                absorbed_by=tuple(
                    owner for owner, _ in sorted(taken.items(), key=lambda kv: (-kv[1], kv[0]))
                ),
            )
        )

    return results


def portfolio_total(claims: Sequence[Mapping[str, int]]) -> int:
    """The cost of the union of every claimed record, counted once.

    The number `attribute()`'s marginals must sum to exactly. Computed
    independently here rather than by summing them, so the test comparing the
    two is a real check and not a tautology.
    """
    union: dict[str, int] = {}
    for claim in claims:
        for key, cost in claim.items():
            union.setdefault(key, cost)
    return sum(union.values())
