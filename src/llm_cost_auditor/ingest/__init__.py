"""Ingest: where the bytes live, what they mean, and what got missed.

Two orthogonal questions, kept apart on purpose (SPEC.md §6):

* **Where do the bytes live?** — a connector (`local`). Moves bytes, knows
  nothing about providers.
* **What do the bytes mean?** — a source adapter (`adapters/`). Interprets
  records, knows nothing about where they came from.

Between them sit two format concerns — compression and container — that belong
to neither, in `decode`.
"""
