"""Error taxonomy.

Kept narrow on purpose: the HTTP service maps these onto status codes, and the
CLI maps them onto ``ok: false`` with a machine-readable ``code``. A stage that
raises something outside this set is a bug, not a user-facing failure.
"""
from __future__ import annotations


class PickvedioError(RuntimeError):
    """Base class for every expected failure."""

    code = "error"
    http_status = 500
    retryable = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.args[0] if self.args else self.__class__.__name__


class DependencyError(PickvedioError):
    """A feature needs an optional dependency or an external binary."""

    code = "dependency_missing"
    http_status = 501


class ConfigurationError(PickvedioError):
    """Settings are missing or contradictory; fix the config, not the code."""

    code = "configuration"
    http_status = 400


class UpstreamBlocked(PickvedioError):
    """The source site refused an unauthenticated datacenter request.

    This is the single most common failure in a hosted deployment, so it gets
    its own type: it is not retried blindly and it always carries the browser
    relay instructions that can actually resolve it.
    """

    code = "needs_browser"
    http_status = 424
    retryable = False

    def __init__(self, message: str, *, hint: str = "", platform: str = "") -> None:
        super().__init__(message)
        self.hint = hint
        self.platform = platform


class RateLimited(PickvedioError):
    code = "rate_limited"
    http_status = 429
    retryable = True


class AuthRequired(PickvedioError):
    """The content itself needs a logged-in session the user must supply."""

    code = "needs_auth"
    http_status = 401


class TransientNetworkError(PickvedioError):
    code = "network"
    http_status = 502
    retryable = True


class UpstreamModelError(PickvedioError):
    """An LLM or ASR provider answered, and the answer was unusable."""

    code = "model_error"
    http_status = 502
    retryable = True


class CancelledError(PickvedioError):
    code = "cancelled"
    http_status = 499


class JobNotFound(PickvedioError):
    code = "job_not_found"
    http_status = 404


class EmptyTranscript(PickvedioError):
    """No speech content could be recovered; a summary of nothing is not a result."""

    code = "empty_transcript"
    http_status = 422
