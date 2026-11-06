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
    # Declared on the base because every consumer reads it with
    # ``getattr(exc, "hint", "")``: the CLI puts it in ``data.hint`` and the
    # service in ``error.hint``. Subclasses that take it in __init__ overwrite
    # it per instance; the default keeps a bare error honest (empty, not absent).
    hint: str = ""

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
    """A provider or upstream endpoint said "slow down" and the retries ran out.

    The docs and the skill's eval pack both branch on this code and honour
    ``retry_after_seconds``, so the number has to be real: it is the integer
    from a ``Retry-After`` header and nothing otherwise. A date-form header is
    deliberately not converted -- guessing seconds would be worse than silence.
    """

    code = "rate_limited"
    http_status = 429
    retryable = True
    hint = (
        "上游限流：不要并发发起别的任务，也不要紧密轮询同一个接口。"
        "等一段时间后单独重试一次；仍然被限流就如实报告进度并停下，"
        "字幕类请求可以改走已登录浏览器的回传通道。"
    )

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds:
            self.hint = (
                f"上游要求等 {retry_after_seconds} 秒再试：这段时间里不要并发发起别的任务，"
                "也不要紧密轮询；等够之后再单独试一次，仍被限流就如实报告进度并停下。"
            )


class SparseTranscript(PickvedioError):
    """Text arrived, but far too little of it for a video this long.

    This used to be a bare ``PickvedioError``, so the envelope said ``error``
    with no hint and the ladder -- which branches on ``code`` -- could not tell
    "you extracted before the cues loaded" from an internal bug. Both sparse
    failures (density and line count) carry a remedy in their message, and the
    message is exactly what an agent on a phone cannot re-derive.
    """

    code = "sparse_transcript"
    http_status = 422


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
