"""HTTP helpers carrying the desktop app's network lessons.

``pickvedio/services.py`` learned, the hard way, that a TLS handshake cut in
half by a proxy or TUN chain looks like a hundred different exceptions
(``UNEXPECTED_EOF_WHILE_READING``, WSA 10053/10054, "unable to handle request",
and yt-dlp's own wrapper text), and that a job should not die from one of them.
That classifier is ported here and applied to the plain-HTTP providers too,
because a hosted deployment behind a corporate egress proxy hits the same set.

The retry plan is the app's: normal attempt, then two direct-connection retries
with growing delay. ``PVC_FORCE_DIRECT=1`` skips the proxied attempt, which is
what a container with a misconfigured ``HTTP_PROXY`` needs.
"""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

import httpx

from ..errors import RateLimited, TransientNetworkError

logger = logging.getLogger("pickvedio.net")

NETWORK_MARKERS = (
    "ssl", "unexpected_eof_while_reading", "connection_reset", "connection_aborted",
    "connection reset", "connection aborted", "timed out", "timeout", "10053", "10054",
    "10060", "10061", "getaddrinfo failed", "temporary failure in name resolution",
    "x509", "bio lib", "unable to handle request", "unexpected error(s)", "eof occurred",
    "errno 101", "errno 110", "network is unreachable",
)

BOT_MARKERS = (
    "sign in to confirm", "not a bot", "unusual traffic", "captcha", "verify your identity",
    "risk control", "访问已熔断", "h5_403", "412", "precondition check failed",
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RETRY_PLAN = ((0.0, None), (1.5, False), (4.0, True))  # delay, force-direct override


def is_network_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in NETWORK_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def looks_like_bot_block(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in BOT_MARKERS)


@dataclass(slots=True)
class HttpProfile:
    """Per-site request identity. Bilibili and YouTube both check referrer/origin."""

    name: str
    user_agent: str = DEFAULT_UA
    referer: str = ""
    origin: str = ""
    extra_headers: dict[str, str] | None = None

    def headers(self) -> dict[str, str]:
        out = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if self.referer:
            out["Referer"] = self.referer
        if self.origin:
            out["Origin"] = self.origin
        out.update(self.extra_headers or {})
        return out


BILIBILI = HttpProfile(
    name="bilibili",
    referer="https://www.bilibili.com/",
    origin="https://www.bilibili.com",
    extra_headers={"Accept-Language": "zh-CN,zh;q=0.9", "Sec-Fetch-Site": "same-site"},
)
YOUTUBE = HttpProfile(
    name="youtube",
    referer="https://www.youtube.com/",
    extra_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "Cookie": "SOCS=CAI"},
)


def load_cookie_header(path: str | Path) -> str:
    """Render a Netscape cookie file as a ``Cookie`` header value."""
    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        return ""
    return "; ".join(f"{item.name}={item.value}" for item in jar)


def cookie_header_from_pairs(pairs: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in pairs.items() if value)


class RetryingClient:
    """A small ``httpx`` wrapper with the app's retry plan and proxy awareness."""

    def __init__(
        self,
        profile: HttpProfile,
        *,
        timeout: float = 30.0,
        cookie_header: str = "",
        attempts: tuple[tuple[float, Any], ...] = _RETRY_PLAN,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self.timeout = timeout
        self.cookie_header = cookie_header
        self.attempts = attempts
        self._transport = transport

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = self.profile.headers()
        if self.cookie_header:
            existing = headers.get("Cookie", "")
            headers["Cookie"] = f"{existing}; {self.cookie_header}" if existing else self.cookie_header
        headers.update(extra or {})
        return headers

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        headers = self._headers(kw.pop("headers", None))
        last: Exception | None = None
        direct_env = os.environ.get("PVC_FORCE_DIRECT", "").strip()
        for index, (delay, direct) in enumerate(self.attempts):
            if delay:
                time.sleep(delay + random.uniform(0, delay * 0.3))
            kwargs = dict(kw)
            kwargs["headers"] = headers
            client_kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout, connect=min(15.0, self.timeout)),
                "follow_redirects": True,
            }
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            elif bool(direct_env) or direct is True:
                # Empty proxy string means "ignore env and system proxies" in httpx.
                client_kwargs["trust_env"] = False
            try:
                with httpx.Client(**client_kwargs) as client:
                    response = client.request(method, url, **kwargs)
                if response.status_code in (429, 503) and index < len(self.attempts) - 1:
                    last = RateLimited(f"上游限流（{response.status_code}）：{url}")
                    logger.warning("retry %s after %s for %s", index + 1, response.status_code, url)
                    continue
                return response
            except httpx.HTTPError as exc:
                last = exc
                if not is_network_error(exc) and index == 0:
                    raise TransientNetworkError(f"请求失败：{exc}") from exc
                logger.warning("network attempt %s failed for %s: %s", index + 1, url, exc)
        if isinstance(last, RateLimited):
            raise last
        raise TransientNetworkError(f"多次尝试均失败：{last}") from last

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)
