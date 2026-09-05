"""LLM cost & routing auditor.

The engine is a pure pipeline from logs to findings (SPEC.md §5). This release
implements its first stage — ingest — plus the run store and the surfaces that
render a run.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
