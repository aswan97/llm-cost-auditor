## What and why

<!-- What changes, and what problem it solves. Link the SPEC.md section it implements. -->

## Verification

<!-- Green CI is necessary, not sufficient (AGENTS.md). For anything touching cost math: -->

- [ ] Ran the CLI end to end on sample logs (`ingest` → `profile` → `audit`), not just the test suite
- [ ] Read the generated report — findings, ranking, and evidence make sense for that traffic
- [ ] Hand-verified at least one savings figure against known token counts and the test catalog
- [ ] Totals reconcile: marginal savings sum to the portfolio total; confidence tiers match the evidence
- [ ] Checked a degraded path (billing-only logs, or a no-config first run)

## Notes for the reviewer

<!-- Assumptions, tradeoffs, anything you are unsure about. Say so plainly. -->
