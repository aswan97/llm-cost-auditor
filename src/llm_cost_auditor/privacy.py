"""Content protections that run at ingest, before anything is stored.

SPEC.md §12 layers 1 and 2:

* **Layer 1 — never persist raw content.** `fingerprint()` turns a prompt
  segment into a hash and a token count, and the caller discards the text. No
  function here returns text it was given.
* **Layer 2 — redact before persisting anything derived.** Label values come
  from user-controlled log fields and routinely carry an email or a key, so
  they are redacted on the way in.

Redaction is deliberately conservative and mechanical. It is not a claim that
the derived store is anonymous — the stronger protection is layer 1, which is
that prompt text is never written at all.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .records import Labels

__all__ = ["REDACTED", "estimate_tokens", "fingerprint", "redact", "redact_labels"]

REDACTED = "[redacted]"

# Detectors for the classes named in §12 layer 2. Ordered most specific first,
# so a key that contains a digit run is not partly rewritten as a card number.
_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # Provider key shapes: sk-..., ak_..., AKIA..., long opaque bearer strings.
    ("key", re.compile(r"\b(?:sk|ak|pk|rk)[-_][A-Za-z0-9_-]{12,}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)

# Roughly four characters per token. This is an estimate and every record built
# on it is flagged `usage_estimated` (§6.5.3) so the confidence penalty travels
# with it — it is never presented as a measured count.
_CHARS_PER_TOKEN = 4  # a tokenizer heuristic, not a rate (noqa: price-literal)


def fingerprint(text: str) -> str:
    """Hash one prompt segment. The text is not retained and is never returned."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Approximate a token count from text length.

    Used only when a log reports no per-segment count. Provider tokenizers live
    behind an optional extra (§5.1) and replace this where they are installed;
    until then every count from here carries `usage_estimated`.
    """
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def redact(value: str) -> str:
    """Replace detected secrets and identifiers in a free-text value."""
    for _, pattern in _DETECTORS:
        value = pattern.sub(REDACTED, value)
    return value


def redact_labels(labels: Labels) -> Labels:
    """Redact the label values that are free text.

    `api_key_id`, `user_id`, and `session_id` are opaque identifiers the logs
    already emit in place of the real thing, and grouping depends on them
    (§8.2), so they pass through as given — but they are still run through the
    detectors, because a log that puts a raw key in `api_key_id` is exactly the
    case this exists for.
    """
    return labels.model_copy(
        update={
            "api_key_id": _maybe(labels.api_key_id),
            "project": _maybe(labels.project),
            "user_id": _maybe(labels.user_id),
            "session_id": _maybe(labels.session_id),
            "endpoint": _maybe(labels.endpoint),
            "tags": {k: redact(v) for k, v in labels.tags.items()},
        }
    )


def _maybe(value: str | None) -> str | None:
    return None if value is None else redact(value)


def contains_secret(values: Iterable[str]) -> bool:
    """True if any value still matches a detector.

    Exists for the absence tests AGENTS.md requires: absence is invisible in
    review, so it gets asserted rather than eyeballed.
    """
    return any(pattern.search(v) for v in values for _, pattern in _DETECTORS)
