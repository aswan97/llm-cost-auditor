"""The local web app (SPEC.md §13.1).

Server-rendered Jinja + HTMX, no JavaScript build step, no SPA. The same
template layer will render both the app's pages and the exported static report,
so the two cannot drift.
"""
