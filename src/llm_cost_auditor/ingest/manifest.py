"""The ingest manifest and the coverage panel (SPEC.md §6.1, §6.6, §11.4).

A run records the exact objects it consumed, and coverage is computed **from
the manifest, not from what succeeded**: listed bytes versus read bytes is what
drives the gating table. This matters more than it looks:

> Baseline spend computed over 87% of the logs is not a smaller number — it is
> a wrong one, and reporting it in bold with a footnote is how it gets quoted
> anyway.

So the gating applies to the baseline, not only to the projection:

| Missing share of listed bytes | Consequence                                       |
|-------------------------------|---------------------------------------------------|
| Any at all (> 0)              | Baseline is a lower bound; projection withheld    |
| Above `max_missing_pct`       | Run is `incomplete`; savings withheld entirely    |

**What the percentage is, and is not.** It is a share of *stored* bytes, on both
sides — a listing is the only thing available before a read, and a listing
reports stored size. Where compression ratios differ across objects, that
understates the share of *records* lost: one unread 700-byte gzip can hold as
many records as 14 KB of plain JSON Lines sitting next to it, and the ratio
reads as 5% rather than 50%. The threshold is deliberately low (2%) partly for
this reason, and the coverage panel names the objects as well as the
percentage — the named object is the honest signal, the ratio is the trigger.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from ..records import Fidelity
from ..window import Window

# How much of an edge of the requested window may go unobserved before the run
# says so. Logs rarely start at midnight, so warning on any gap at all would
# warn on every healthy run — and a panel that always warns is one nobody
# reads. A whole missing day is the smallest gap that is a fact about the data
# rather than about when traffic happened to start.
_EDGE_GAP = timedelta(days=1)


class ObjectEntry(BaseModel):
    """One object the run consumed, or failed to (SPEC.md §6.6).

    Recorded whether it succeeded or not, which is what makes a run
    reproducible and double-counting detectable rather than accidental.
    """

    model_config = ConfigDict(extra="forbid")

    uri: str
    connection_id: str
    etag: str
    size_bytes: int
    bytes_read: int = 0
    records_parsed: int = 0
    records_rejected: int = 0
    records_outside_window: int = 0
    compression: str | None = None
    container: str | None = None
    detected_by: str | None = None
    status: str = "ok"  # "ok" | "failed"
    error: str | None = None


class ListingFailureEntry(BaseModel):
    """A location that could not be listed at all.

    Its byte volume is unknowable by definition, which is why it forces the run
    `incomplete` rather than contributing to a percentage: an unquantifiable
    gap cannot be compared against a threshold.
    """

    model_config = ConfigDict(extra="forbid")

    uri: str
    connection_id: str
    error: str


class SliceSummary(BaseModel):
    """One `(connection_id, source, fidelity_tier)` slice (SPEC.md §5.2).

    The slice is the unit of analyzer applicability, so it is built at ingest
    even though no analyzer exists yet — a finding that cannot name the slice
    it covers can be read as covering traffic it never saw.

    `fidelity` is the highest tier the slice supports (§6.3) and
    `fidelity_counts` is the mix behind it, because "highest tier" over a
    dataset where one record in ten thousand carries content would otherwise
    read as a claim about all of them.
    """

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    source: str
    fidelity: Fidelity
    fidelity_counts: dict[str, int] = Field(default_factory=dict)
    records: int = 0
    observed_start: datetime | None = None
    observed_end: datetime | None = None


class Coverage(BaseModel):
    """What was read, what was missed, and what that costs the run."""

    model_config = ConfigDict(extra="forbid")

    listed_objects: int = 0
    listed_bytes: int = 0
    read_objects: int = 0
    read_bytes: int = 0
    failed_objects: int = 0
    missing_bytes: int = 0
    missing_pct: float = 0.0
    pruned_by_window: int = 0

    empty_listings: list[str] = Field(default_factory=list)
    listing_failures: list[ListingFailureEntry] = Field(default_factory=list)

    records_parsed: int = 0
    records_rejected: int = 0
    records_outside_window: int = 0
    duplicates_collapsed: int = 0
    retry_attempts_linked: int = 0
    ttl_class_unknown_records: int = 0
    dedupe_method: str | None = None

    warnings: list[str] = Field(default_factory=list)

    # The gating verdict (§6.1, §11.4). Named rather than derived at render
    # time, so the CLI, the API, and the app cannot disagree about it.
    baseline_is_lower_bound: bool = False
    projection_withheld: bool = False
    incomplete: bool = False
    gating_reasons: list[str] = Field(default_factory=list)

    def apply_gating(self, max_missing_pct: float) -> None:
        """Compute the §6.1 verdict from the manifest totals."""
        self.missing_pct = (
            (self.missing_bytes / self.listed_bytes * 100.0) if self.listed_bytes else 0.0
        )

        if self.missing_bytes > 0 or self.failed_objects > 0:
            self.baseline_is_lower_bound = True
            self.projection_withheld = True
            self.gating_reasons.append(
                f"{self.failed_objects} object(s) totalling {self.missing_bytes} bytes could not "
                f"be read ({self.missing_pct:.2f}% of listed bytes). The baseline is a lower "
                f"bound and the monthly projection is withheld (§6.1)."
            )

        if self.missing_pct > max_missing_pct:
            self.incomplete = True
            self.gating_reasons.append(
                f"Missing {self.missing_pct:.2f}% of listed bytes exceeds the "
                f"coverage.max_missing_pct threshold of {max_missing_pct:.2f}%. Savings figures "
                f"are withheld entirely for this run (§6.1)."
            )

        if self.listing_failures:
            # A listing that failed hides an unknown number of unknown-sized
            # objects, so there is no percentage to compare against a
            # threshold. The honest verdict is the strict one.
            self.baseline_is_lower_bound = True
            self.projection_withheld = True
            self.incomplete = True
            named = ", ".join(f.uri for f in self.listing_failures[:5])
            self.gating_reasons.append(
                f"{len(self.listing_failures)} location(s) could not be listed ({named}). The "
                f"missing byte volume is unknowable, so the run is marked incomplete rather "
                f"than gated on a percentage (§6.1)."
            )

        for uri in self.empty_listings:
            # Not a failure — but "no logs" and "no requests" look identical
            # from here (§15.9), so it is stated rather than left to inference.
            self.warnings.append(
                f"{uri} matched no objects in the requested window. The tool cannot distinguish "
                f"'no traffic' from 'no logs delivered' (§15.9)."
            )

    def describe_window_coverage(
        self,
        *,
        window: Window | None,
        observed_start: datetime | None,
        observed_end: datetime | None,
        records: int,
    ) -> None:
        """State how far the observed data actually reaches into the window (§6.6).

        An empty listing already says so, but it is only the loudest version of
        the problem. Objects that exist, decode cleanly, and hold nothing in the
        window produce the same silence, and so does a window whose last three
        weeks were never delivered — a run reporting on nine days of a
        thirty-one-day window is the §15.9 failure mode, and the number gets
        quoted as a month either way.

        None of this is a coverage *failure*: the bytes were read and the
        records are what they are, so gating is untouched. It is stated so the
        gap is visible rather than inferred from a total nobody cross-checks.
        """
        if window is None:
            return

        if records == 0 and self.read_objects > 0:
            if self.records_outside_window > 0:
                self.warnings.append(
                    f"No records fall inside {window.describe()}: all "
                    f"{self.records_outside_window} record(s) decoded from "
                    f"{self.read_objects} object(s) are outside it. The logs are readable, "
                    f"so this is a window that does not match the data rather than missing "
                    f"data (§6.6)."
                )
            else:
                self.warnings.append(
                    f"{self.read_objects} object(s) were read and yielded no records at all. "
                    f"The tool cannot distinguish 'no traffic' from 'no logs delivered' "
                    f"(§15.9)."
                )
            return

        if observed_start is None or observed_end is None:
            return

        # `window.end` is the exclusive bound, so the trailing gap is measured
        # against it directly.
        leading = observed_start - window.start
        trailing = window.end - observed_end
        gaps: list[str] = []
        if leading >= _EDGE_GAP:
            gaps.append(f"{leading.days} day(s) at the start")
        if trailing >= _EDGE_GAP:
            gaps.append(f"{trailing.days} day(s) at the end")

        if gaps:
            self.warnings.append(
                f"The observed data does not reach the requested window: "
                f"{' and '.join(gaps)} of {window.describe()} contain no records. "
                f"Observed {observed_start.isoformat()} .. {observed_end.isoformat()}. "
                f"A per-day or monthly figure derived from this run covers the observed "
                f"range, not the requested one (§6.6, §15.9)."
            )
