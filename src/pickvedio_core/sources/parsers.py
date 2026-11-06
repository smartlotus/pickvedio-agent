"""Transcript parsers.

Every caption format that reaches this project, decoded into ``Segment``s. This
layer is where a summarisation pipeline silently invents content, so the rules
are explicit:

* a cue with no text is dropped, never turned into an empty timed row;
* YouTube auto-captions repeat the previous line inside the next cue to build a
  rolling caption -- consecutive duplicates are collapsed, otherwise a
  transcript reads as though the speaker said everything twice;
* inline tags (``<c>`` styling, ``<v Speaker>`` voice labels) are stripped but
  a speaker name is preserved into ``Segment.speaker``;
* anything with no timing information at all is marked so downstream stages can
  refuse to invent timestamps.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable

from ..models import Segment
from ..util import parse_time

_JSON3_SKIP = {"\n", "", " "}
_CUE_TIME = re.compile(
    r"(\d{1,3}(?::\d{2}){1,2}[.,]\d{1,3}|\d{1,3}(?::\d{2}){1,2})\s*-->\s*"
    r"(\d{1,3}(?::\d{2}){1,2}[.,]\d{1,3}|\d{1,3}(?::\d{2}){1,2})"
)
_INLINE_TAG = re.compile(r"<[^>]+>")
_VOICE_TAG = re.compile(r"<v\s+([^>]+?)>", re.I)
_XML_TEXT_CUE = re.compile(
    r"<text\b[^>]*?start=[\"']([^\"']+)[\"'][^>]*?(?:dur=[\"']([^\"']+)[\"'][^>]*)?>(.*?)</text>",
    re.S,
)
_XML_ENTITY = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&apos;": "'"}


def _unescape(value: str) -> str:
    for entity, char in _XML_ENTITY.items():
        value = value.replace(entity, char)
    return value.replace("&nbsp;", " ")


def _collapse_duplicates(segments: list[Segment]) -> list[Segment]:
    """Drop cues whose text is wholly contained in the previous cue.

    Rolling-caption formats re-emit the tail of the last cue; keeping both makes
    a transcript duplicate roughly half its content.
    """
    out: list[Segment] = []
    for item in segments:
        text = item.text.strip()
        if not text:
            continue
        if out:
            previous = out[-1].text.strip()
            if text == previous:
                # Same words, later clock: extend the previous cue's end instead.
                out[-1].end = max(out[-1].end, item.end)
                continue
            if previous.endswith(text) and len(text) < len(previous):
                continue
            if text.startswith(previous) and len(previous) > 8:
                # The new cue is a superset; rewrite the previous one's text.
                out[-1] = Segment(start=out[-1].start, end=item.end, text=text,
                                  speaker=out[-1].speaker)
                continue
        out.append(Segment(start=item.start, end=item.end, text=text, speaker=item.speaker))
    return out


def from_json3(payload: str | dict) -> list[Segment]:
    """YouTube's ``fmt=json3`` windowed caption body."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    events = data.get("events") or []
    segments: list[Segment] = []
    for event in events:
        segs = event.get("segs") or []
        text = "".join(str(seg.get("utf8", "")) for seg in segs)
        text = text.replace("\n", " ").strip()
        if not text or text in _JSON3_SKIP:
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        duration = float(event.get("dDurationMs") or 0) / 1000.0
        segments.append(Segment(start=start, end=start + (duration or 2.0), text=text))
    return _collapse_duplicates(segments)


def from_srt(payload: str) -> list[Segment]:
    blocks = re.split(r"\n\s*\n", payload.replace("\r\n", "\n").replace("\r", "\n").strip())
    segments: list[Segment] = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        index = 0
        if "-->" not in lines[0]:
            index = 1
            if index >= len(lines) or "-->" not in lines[index]:
                continue
        match = _CUE_TIME.search(lines[index])
        if not match:
            continue
        start, end = parse_time(match.group(1)), parse_time(match.group(2))
        if start is None or end is None:
            continue
        text = " ".join(_INLINE_TAG.sub("", line).strip() for line in lines[index + 1 :])
        text = re.sub(r"\s{2,}", " ", text).strip()
        if text:
            segments.append(Segment(start=start, end=end, text=text))
    return _collapse_duplicates(segments)


def from_vtt(payload: str) -> list[Segment]:
    body = payload.replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"^WEBVTT[^\n]*\n", "", body)
    body = re.sub(r"^NOTE[^\n]*\n(?:[^\n]*\n)*?\n", "", body)
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        cue_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if cue_index is None:
            continue
        match = _CUE_TIME.search(lines[cue_index])
        if not match:
            continue
        start, end = parse_time(match.group(1)), parse_time(match.group(2))
        if start is None or end is None:
            continue
        text_lines = lines[cue_index + 1 :]
        speaker = ""
        voice = _VOICE_TAG.search("\n".join(text_lines))
        if voice:
            speaker = voice.group(1).strip()
        text = " ".join(_INLINE_TAG.sub("", line).strip() for line in text_lines)
        text = re.sub(r"\s{2,}", " ", _unescape(text)).strip()
        if text:
            segments.append(Segment(start=start, end=end, text=text, speaker=speaker))
    return _collapse_duplicates(segments)


def from_srv3(payload: str) -> list[Segment]:
    """YouTube's ``fmt=srv1``/``srv3`` timed-text XML."""
    segments: list[Segment] = []
    for match in _XML_TEXT_CUE.finditer(payload):
        start = parse_time(match.group(1))
        duration = parse_time(match.group(2))
        raw = _unescape(match.group(3))
        text = re.sub(r"\s+", " ", _INLINE_TAG.sub(" ", raw)).strip()
        if not text or start is None:
            continue
        segments.append(Segment(start=start, end=start + (duration or 2.0), text=text))
    return _collapse_duplicates(segments)


def from_bilibili_body(payload: str | dict) -> list[Segment]:
    """Bilibili's subtitle JSON: ``{"body": [{"from","to","content"}]}``."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    rows = data.get("body") or data.get("segments") or []
    segments: list[Segment] = []
    for row in rows:
        text = str(row.get("content") or row.get("text") or "").strip()
        if not text:
            continue
        start = float(row.get("from") or 0.0)
        end = float(row.get("to") or start)
        if end <= start:
            end = start + 2.0
        segments.append(Segment(start=start, end=end, text=text))
    return _collapse_duplicates(segments)


def from_lrc(payload: str) -> list[Segment]:
    """``[mm:ss.xx] line`` lyric/subtitle rows; end time is the next cue."""
    marks = re.findall(
        r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]\s*([^\[\n]*)", payload.replace("\r", "")
    )
    rows = [
        (
            int(mm) * 60 + int(ss) + (int((frac or "0").ljust(3, "0")[:3]) / 1000.0),
            text.strip(),
        )
        for mm, ss, frac, text in marks
    ]
    segments = [
        Segment(start=start, end=(rows[i + 1][0] if i + 1 < len(rows) else start + 5.0), text=text)
        for i, (start, text) in enumerate(rows)
        if text
    ]
    return _collapse_duplicates(segments)


def from_plain_text(payload: str, duration: float | None = None) -> list[Segment]:
    """Untimed text (a description, an article). Timings are *not* real."""
    lines = [line.strip() for line in payload.replace("\r", "").split("\n") if line.strip()]
    if not lines:
        return []
    if not duration or duration <= 0:
        return [Segment(start=0.0, end=0.0, text=line) for line in lines]
    step = duration / len(lines)
    return [
        Segment(start=index * step, end=(index + 1) * step, text=line)
        for index, line in enumerate(lines)
    ]


def merge_short(segments: list[Segment], max_chars: int = 260, max_gap: float = 1.2) -> list[Segment]:
    """Pack caption fragments into utterances.

    ASR and caption tracks emit 5-15 token cues; one prompt line per cue wastes
    the model's budget on timestamps. Merging keeps order and takes the end time
    of the last fragment.
    """
    out: list[Segment] = []
    for item in segments:
        if out:
            last = out[-1]
            if (
                len(last.text) + 1 + len(item.text) <= max_chars
                and item.start - last.end <= max_gap
                and (not last.speaker or last.speaker == item.speaker)
            ):
                last.text = f"{last.text} {item.text}".strip()
                last.end = max(last.end, item.end)
                continue
        out.append(Segment(start=item.start, end=item.end, text=item.text, speaker=item.speaker))
    return out


PARSERS = {
    "json3": from_json3,
    "srv3": from_srv3,
    "srv1": from_srv3,
    "srt": from_srt,
    "vtt": from_vtt,
    "bilibili": from_bilibili_body,
    "lrc": from_lrc,
    "plain": from_plain_text,
}


def sniff_format(name: str, payload: str) -> str:
    key = (name or "").lower().lstrip(".")
    if key in {"srt", "vtt", "json", "json3", "lrc", "txt", "xml", "srv3"}:
        return {"json": "json3", "xml": "srv3", "txt": "plain"}.get(key, key)
    head = payload.lstrip("\ufeff")[:400]
    if head.startswith("WEBVTT"):
        return "vtt"
    if head.startswith("{"):
        return "json3"
    if "<tt" in head or "<text" in head:
        return "srv3"
    if "-->" in head:
        return "srt"
    if re.match(r"^\s*\[\d{1,3}:\d{2}", head):
        return "lrc"
    return "plain"


def parse(payload: str, *, name: str = "", fmt: str = "", duration: float | None = None) -> list[Segment]:
    """Parse a caption body, guessing the format from ``name`` then content."""
    key = fmt or sniff_format(name, payload)
    parser = PARSERS.get(key, from_plain_text)
    if parser is from_plain_text:
        return from_plain_text(payload, duration)
    try:
        return parser(payload)  # type: ignore[arg-type]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, AttributeError):
        return from_plain_text(payload, duration)


def normalise(segments: Iterable[Segment], duration: float | None = None) -> list[Segment]:
    """Sanity-check a transcript before it costs money to summarise."""
    rows = sorted((item for item in segments if item.text.strip()), key=lambda item: item.start)
    fixed: list[Segment] = []
    for item in rows:
        start = max(0.0, float(item.start or 0.0))
        end = float(item.end or start)
        if end <= start:
            end = start + 2.0
        if duration and start > duration + 5:
            continue
        fixed.append(Segment(start=start, end=min(end, duration) if duration else end,
                             text=item.text.strip(), speaker=item.speaker))
    return fixed
