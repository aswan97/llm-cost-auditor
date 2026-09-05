"""Errors that carry a message a user can act on.

Anything raised out of the engine and shown to a user is one of these; an
unexpected exception is a bug and is reported as such, with its traceback.
"""

from __future__ import annotations


class AuditorError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(AuditorError):
    """The workspace config, a connection, or a CLI argument is invalid."""


class SourceScopeError(ConfigError):
    """A uri falls outside the terminal-configured source scope (SPEC.md §6.1).

    This is its own class because it is a boundary violation rather than a
    typo: the API surfaces it as 403, and the CLI names the scope it checked
    against so the fix is obvious.
    """


class MissingPriceError(AuditorError):
    """No catalog rate covers this provider/model/timestamp (SPEC.md §7.1).

    Its own class because it is never fatal to a run and never a reason to
    guess: the caller records a *missing price* coverage entry, excludes the
    model from savings math, and reports it. Falling back to today's rate, to a
    sibling model, or to zero would each produce a number that looks exactly
    like a real one.

    `reason` separates the two cases, which need different fixes and must not
    be reported as one: an unknown model means the catalog has never heard of
    it, while an uncovered timestamp means the model is known and the *period*
    is missing — usually a historical row nobody backfilled. Told the same
    thing, a user goes looking in the wrong place.
    """

    UNKNOWN_MODEL = "unknown_model"
    NO_ROW_FOR_TIMESTAMP = "no_row_for_timestamp"

    def __init__(self, message: str, *, reason: str = UNKNOWN_MODEL) -> None:
        super().__init__(message)
        self.reason = reason


class DecodeError(AuditorError):
    """An object could not be decoded as its detected format (SPEC.md §6.1).

    Never a skipped line: the caller turns this into a coverage failure naming
    the object and its byte volume.
    """


class AdapterError(AuditorError):
    """A record could not be interpreted by its source adapter."""


class RunStateError(AuditorError):
    """A run is not in a state that permits the requested operation.

    A stage may run only once per run, and stages run in order (SPEC.md §5.3).
    """


class RunCancelled(AuditorError):
    """The run was cancelled while executing.

    A cancelled run is failed, never paused: work that has begun is never
    resumed (SPEC.md §5.4), so partial data is discarded rather than kept.
    """
