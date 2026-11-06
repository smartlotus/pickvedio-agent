"""Small shared helpers: time formatting, subtitle clocks, progress protocol."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from collections.abc import Callable


class Progress(Protocol):
    def __call__(self, ratio: float, message: str) -> None: ...


# Kept as the desktop app spelled it, so ports of its call sites stay readable.
ProgressCallback = Callable[[float, str], None]


class NullProgress:
    def __call__(self, ratio: float, message: str) -> None:  # noqa: D401 - protocol stub
        return None


@dataclass(slots=True)
class CancelToken:
    """Cooperative cancellation shared by the CLI, the worker and the service."""

    event_flag: bool = False
    reason: str = ""

    def cancel(self, reason: str = "用户取消了任务。") -> None:
        self.event_flag = True
        self.reason = reason

    def is_set(self) -> bool:
        return self.event_flag

    def check(self) -> None:
        if self.event_flag:
            from .errors import CancelledError

            raise CancelledError(self.reason or "用户取消了任务。")


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_time(value: str | float | None) -> float | None:
    """Accept ``92``, ``1:32``, ``01:02:03`` and return seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return None
        seconds = 0.0
        for number in numbers:
            seconds = seconds * 60 + number
        return seconds
    try:
        return float(text)
    except ValueError:
        return None


def subtitle_time(seconds: float, separator: str) -> str:
    milliseconds = round(max(0.0, seconds) * 1000)
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


_TIME_LINE = re.compile(
    r"^\s*\[?\s*(\d{1,4}(?::\d{1,2}){0,2}(?:[.,]\d{1,3})?)\s*[-–—>]{1,3}\s*"
    r"(\d{1,4}(?::\d{1,2}){0,2}(?:[.,]\d{1,3})?)\s*\]?\s*(.+)$"
)


def strip_speaker_prefix(text: str) -> tuple[str, str]:
    """Split a ``- 说话人：内容`` line into (speaker, text)."""
    match = re.match(r"^\s*[-–\u3000]?\s*([\u4e00-\u9fffA-Za-z0-9 _]{1,20})[：:]\s*(.+)$", text)
    if match and len(match.group(2).strip()) > 1:
        return match.group(1).strip(), match.group(2).strip()
    return "", text
