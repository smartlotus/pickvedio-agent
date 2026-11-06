"""Relay channel: a transcript obtained elsewhere, by an authenticated session.

This is the channel that makes the whole hosted design work. A datacenter
request cannot see Bilibili's AI subtitles and YouTube bot-gates it, but a
logged-in browser can read both same-origin. The agent (Muse, or a human with
the browser extension the desktop app already ships) extracts captions in that
session and posts them here.

The trust question is handled explicitly rather than assumed away: a relayed
transcript is *claimed* content, so it is validated against the video's own
duration and line count, and any caption format the submitter declared is
honoured as the source kind -- but an implausible payload is rejected with the
reason, not summarised into a confident-looking wrong answer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import EmptyTranscript, PickvedioError
from ..models import Segment, SourceKind, Transcript
from ..util import format_time
from .parsers import normalise, parse

logger = logging.getLogger("pickvedio.relay")

# A real spoken-language video carries roughly 100-250 characters per minute.
# Anything far below that is a truncated caption track, a single description
# paragraph, or a bot-gate page that was mistaken for content.
MIN_CHARS_PER_MINUTE = 12
MIN_LINES_FOR_SUMMARY = 4

_SOURCE_ALIASES = {
    "up_subtitles": SourceKind.UP_SUBTITLES,
    "human_subtitles": SourceKind.HUMAN_SUBTITLES,
    "platform_auto": SourceKind.PLATFORM_AUTO,
    "whisper": SourceKind.WHISPER,
    "relayed": SourceKind.RELAYED,
    "page_text": SourceKind.PAGE_TEXT,
}


@dataclass(slots=True)
class RelayResult:
    transcript: Transcript
    warnings: list[str]
    checks: dict[str, Any]


def _source_kind(value: str | None) -> SourceKind:
    key = (value or "").strip().lower().replace("-", "_")
    if key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[key]
    # The browser extractor distinguishes creator-authored from platform ASR; a
    # relay of either is still that tier of evidence, so the declared kind wins
    # and `relayed` is recorded as the channel instead.
    return SourceKind.RELAYED


def segments_from_payload(
    payload: Any, *, name: str = "", fmt: str = "", duration: float | None = None
) -> list[Segment]:
    """Accept structured segments or any caption body, in one entry point."""
    if isinstance(payload, list):
        # The jobs API hands over `segments` directly, already as a list.
        return segments_from_payload({"segments": payload})
    if isinstance(payload, dict):
        rows = payload.get("segments") or payload.get("body") or []
        if isinstance(rows, list) and rows:
            out: list[Segment] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or row.get("content") or "").strip()
                if not text:
                    continue
                start = float(row.get("start") or row.get("from") or 0.0)
                end = float(row.get("end") or row.get("to") or 0.0)
                out.append(
                    Segment(
                        start=start,
                        end=end if end > start else start,
                        text=text,
                        speaker=str(row.get("speaker") or ""),
                    )
                )
            return out
        for key in ("transcript", "text", "srt", "vtt", "json3"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return parse(payload[key], name=name or key, fmt=fmt, duration=duration)
        return []
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        if payload and not Path(payload).is_file():
            stripped = payload.strip()
            if stripped.startswith("{"):
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict):
                    return segments_from_payload(decoded, name=name, fmt=fmt, duration=duration)
            return parse(payload, name=name, fmt=fmt, duration=duration)
        return parse(Path(payload).read_text(encoding="utf-8", errors="replace"),
                     name=name or Path(payload).name, fmt=fmt, duration=duration)
    raise PickvedioError(f"无法解析回传内容：{type(payload).__name__}")


def to_transcript(
    url: str,
    payload: Any,
    *,
    title: str = "",
    duration: float | None = None,
    language: str = "",
    source_kind: str = "",
    uploader: str = "",
    origin: str = "relay",
    name: str = "",
    fmt: str = "",
    strict: bool = True,
) -> RelayResult:
    """Validate and wrap a relayed transcript.

    ``strict`` refuses a payload that cannot plausibly be the speech of a video
    of that length. Callers that genuinely want to summarise a description pass
    ``source_kind="page_text"`` explicitly, which is honoured and labelled.
    """
    if duration is None and isinstance(payload, dict):
            raw = payload.get("duration") or payload.get("durationSeconds")
            try:
                duration = float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                duration = None
    if not title and isinstance(payload, dict):
        title = str(payload.get("title") or "")
    if not language and isinstance(payload, dict):
        language = str(payload.get("language") or payload.get("lang") or "")
    if not source_kind and isinstance(payload, dict):
        source_kind = str(payload.get("sourceKind") or payload.get("source") or "")
    key = (source_kind or "").strip().lower().replace("-", "_")

    segments = segments_from_payload(payload, name=name, fmt=fmt, duration=duration)
    # Judged before normalise: that step gives a zero-length cue a 2-second
    # floor, which would make an entirely untimed payload look timed.
    timed = any(item.end > item.start or item.start > 0 for item in segments)
    segments = normalise(segments, duration)
    warnings: list[str] = []
    if not segments:
        raise EmptyTranscript(
            "回传内容里没有可用的文字。可能是页面正文被误当成字幕、字幕轨道为空，"
            "或提交的结构与 `segments` / `body` / `text` 都不匹配。"
        )

    chars = sum(len(item.text) for item in segments)
    checks = {
        "lines": len(segments),
        "chars": chars,
        "timed": timed,
        "duration_claimed": duration,
        "span_observed": segments[-1].end if segments else 0.0,
        "chars_per_minute": None,
    }

    if duration:
        per_minute = chars / max(1.0, duration / 60.0)
        checks["chars_per_minute"] = round(per_minute, 1)
        if per_minute < MIN_CHARS_PER_MINUTE:
            message = (
                f"文字密度异常：{format_time(duration)} 的视频只有 {chars} 字"
                f"（每分钟约 {per_minute:.0f} 字，正常口语在 100 字以上）。"
            )
            if strict and key != "page_text":
                raise PickvedioError(
                    message
                    + "请确认在字幕轨道加载后重新提取，或改用音频转写。"
                    " 若确实只有简介文字，请显式声明 source=page_text。"
                )
            warnings.append(message)

    if len(segments) < MIN_LINES_FOR_SUMMARY:
        message = f"只有 {len(segments)} 段文字，内容过短，分层总结的意义有限。"
        warnings.append(message)
        if strict and key != "page_text" and duration and duration > 600:
            raise PickvedioError(message + "（长视频只回传几段，通常是提取时机太早。）")

    if not timed:
        warnings.append("回传内容没有时间信息，报告中的时间点均为估计值，不可作为定位依据。")

    kind = _source_kind(source_kind)
    if not source_kind:
        warnings.append("未声明字幕来源类型，按 `relayed` 记录（可信度中等）。")
    transcript = Transcript(
        video_url=url,
        title=title or "未命名视频",
        duration=duration,
        segments=segments,
        source=kind,
        language=language,
        channel=origin,
        meta={
            "uploader": uploader,
            "declared_source_kind": source_kind,
            "checks": checks,
            "timed": timed,
        },
    )
    return RelayResult(transcript=transcript, warnings=warnings, checks=checks)
