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


RELAY_REASON_HINTS: dict[str, str] = {
    # Mirrors the table in skills/video-summary/references/browser-extract.md,
    # which is the authority; test_relay_reason_hints_cover_every_reason_the_
    # extractor_can_emit fails if either side drifts or a key is invented.
    "no_subtitles_for_session": (
        "当前会话看不到字幕轨道——B 站的 AI 字幕只对已登录会话下发。"
        "让用户在同一台机器上登录该站点后重跑一次提取，或改走已登录浏览器的页面本身；"
        "这不是解析 bug，也不要因此去凑总结。"
    ),
    "subtitle_track_without_body": (
        "有字幕轨道但一条正文都没取到：正文请求被拒或被截断。"
        "重新打开页面再取一次；仍然为空就按失败上报，并提出 ASR 那条花钱的路（需用户同意）。"
    ),
    "subtitle_body_forbidden": "签名后的字幕地址被拒绝：重新打开页面取一次，再失败就上报。",
    "subtitle_body_failed": "取字幕正文时网络失败：重新打开页面取一次，再失败就上报。",
    "caption_body_empty": "字幕正文为空，等于没有逐字稿；按失败上报，不要改用简介。",
    "empty_segments": "抽取到 0 段，多半是提取时机太早（播放器还没加载字幕）。等页面真正开始播放后再取一次。",
    "extraction_failed": (
        "提取器自己走到了兜底分支（没有更具体的 reason）。"
        "重新打开规范观看地址、等播放器真正开始播放后再执行一次脚本；"
        "两次都这样就把 warnings 原样上报，不要继续重试。"
    ),
    "subtitle_body_empty": "字幕正文为空，等于没有逐字稿；按失败上报，不要改用简介。",
    "caption_track_without_baseurl": "YouTube 只给了清单没给可取正文的地址：重新打开观看页再取一次。",
    "no_captions": "这个 YouTube 视频一条 caption 轨道都没有：只剩 ASR（花钱，需同意）或如实失败。",
    "captions_translate_only": "页面只有翻译元数据、没有轨道清单：带 translateTo 重取一次，仍不行就走 ASR 或如实失败。",
    "rate_limited": "timedtext 返回 429：退避，让页面自己播一次后再取一次，最多重试一次。",
    "caption_forbidden": "该会话无权取这套字幕（401/403）：如实上报，不要尝试绕过。",
    "no_player_response": "页面状态不可读（开错页或求值太早）：用规范观看地址重开一次，等播放器就绪再取。",
    "no_video_id": "没能从页面拿到 BV/av 号：多半是列表页或搜索页，请让用户发视频本页。",
    "no_cid": "没能解析出 cid，因此无法请求字幕轨道：用规范观看地址重开一次。",
    "view_api_failed": "web-interface/view 请求失败：检查是否被地区/风控拦截，重开一次后如实上报。",
    "not_playable": "内容不可播放（地区限制、需登录、会员专享）：停止，说明原因，不要试图绕过。",
    "unsupported_page": "这不是 B 站/YouTube 页面：可以用 `pvc fetch` 的通用提取器试，或如实说明不支持。",
}


def _source_kind(value: str | None) -> SourceKind:
    key = (value or "").strip().lower().replace("-", "_")
    if key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[key]
    # The browser extractor distinguishes creator-authored from platform ASR; a
    # relay of either is still that tier of evidence, so the declared kind wins
    # and `relayed` is recorded as the channel instead.
    return SourceKind.RELAYED


def _looks_like_path(spec: str) -> bool:
    """Decide whether a string is meant to be a filename, without touching the OS.

    ``Path(huge).is_file()`` is not a safe probe: on Linux an over-long path
    raises ``OSError(ENAMETOOLONG)`` out of ``stat()`` rather than returning
    False, so a relayed transcript pasted inline -- the normal way an agent
    calls this -- crashed the CLI. Only short, newline-free strings that are not
    JSON can be filenames here.
    """
    if len(spec) > 512 or "\n" in spec:
        return False
    stripped = spec.strip()
    return bool(stripped) and stripped[0] not in '[{"'


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
        if not _looks_like_path(payload):
            stripped = payload.strip()
            if stripped.startswith("{"):
                # A whole browser-extraction object pasted inline: still a
                # document, not a caption body.
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict):
                    return segments_from_payload(decoded, name=name, fmt=fmt, duration=duration)
            return parse(payload, name=name, fmt=fmt, duration=duration)
        path = Path(payload)
        if not path.is_file():
            raise PickvedioError(f"找不到字幕文件：{payload[:120]}")
        return parse(path.read_text(encoding="utf-8", errors="replace"),
                     name=name or path.name, fmt=fmt, duration=duration)
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
    # `pvc push --transcript '<inline JSON>'` and --transcript relay.json both
    # arrive as *text*: _read_payload hands over the raw string, and only
    # segments_from_payload parses it. Reading the metadata before that parse
    # meant every field the extractor worked to obtain -- reason, warnings,
    # title, durationSeconds, sourceKind -- silently disappeared for the
    # most-common invocation, which is exactly the phone path.
    document: Any = payload
    if isinstance(document, str):
        text = document.strip()
        if text[:1] in "[{":
            try:
                document = json.loads(text)
            except ValueError:
                document = payload
    if duration is None and isinstance(document, dict):
            raw = document.get("duration") or document.get("durationSeconds")
            try:
                duration = float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                duration = None
    if not title and isinstance(document, dict):
        title = str(document.get("title") or "")
    if not language and isinstance(document, dict):
        language = str(document.get("language") or document.get("lang") or "")
    if not source_kind and isinstance(document, dict):
        source_kind = str(document.get("sourceKind") or document.get("source") or "")
    key = (source_kind or "").strip().lower().replace("-", "_")

    segments = segments_from_payload(payload, name=name, fmt=fmt, duration=duration)
    # Judged before normalise: that step gives a zero-length cue a 2-second
    # floor, which would make an entirely untimed payload look timed.
    timed = any(item.end > item.start or item.start > 0 for item in segments)
    segments = normalise(segments, duration)
    warnings: list[str] = []
    if not segments:
        # The extractor knows *why* it came back empty, and that reason is the
        # whole difference between "ask the user to log in", "retry later" and
        # "this video is not playable -- stop". Dropping it turned every empty
        # relay into the same generic complaint and made the agent guess.
        reason = str(document.get("reason") or "") if isinstance(document, dict) else ""
        relay_warnings = [str(item) for item in ((document.get("warnings") or []) if isinstance(document, dict) else []) if item]
        detail = f"提取器给出 reason={reason}" if reason else "提交内容里没有 reason 字段，无法判断是哪一层空的"
        message = (
            "回传内容里没有可用的文字（0 段）。" + detail
            + "；常见原因是字幕轨道对当前会话为空、正文请求被拒，"
            + "或提交的结构与 `segments` / `body` / `text` 都不匹配。"
        )
        if relay_warnings:
            message += " 提取器的警告：" + " / ".join(relay_warnings[:2])
        error = EmptyTranscript(message)
        error.hint = RELAY_REASON_HINTS.get(reason) or (
            "把 relay.json 里的 reason 与 warnings 原样转达用户并停下。"
            "绝对不要改用视频简介、标题、评论或搜索结果来凑一份总结。"
        )
        raise error

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
