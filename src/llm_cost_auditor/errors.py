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
