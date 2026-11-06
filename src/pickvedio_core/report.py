"""Report renderers.

Ported from ``pipeline._write_summary`` / ``services.write_transcript_exports``,
with the header made honest: the desktop build always printed
"转写：本地 Whisper · 模型 ``small``" whether or not Whisper ran. Now the header
states which channel actually produced the words and what confidence that tier
carries, because a summary of platform ASR captions and a summary of a
creator-authored subtitle track are not equally trustworthy.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Report, Segment, TranscriptChunk
from .util import format_time, subtitle_time

from .report_labels import SOURCE_ENGLISH, SOURCE_LABELS  # re-exported for callers



def _header(report: Report, *, style_label: str = "") -> str:
    lines = [
        f"# {report.title}",
        "",
        f"> 来源：{report.url}",
        f"> 时长：{format_time(report.duration)}",
        f"> 文字来源：{SOURCE_LABELS.get(report.source, report.source.value)} · {len(report.segments)} 段 · {report.chars} 字",
        f"> 生成模型：`{report.model or '未记录'}`",
    ]
    if style_label:
        lines.append(f"> 整理档位：{style_label}")
    if report.language:
        lines.append(f"> 语言：{report.language}")
    if report.warnings:
        lines.append("")
        for note in report.warnings:
            lines.append(f"> ⚠ {note}")
    return "\n".join(lines) + "\n"


def _partials(chunks: list[TranscriptChunk], chunk_summaries: list[str]) -> str:
    blocks = []
    for index, (chunk, content) in enumerate(zip(chunks, chunk_summaries, strict=True), start=1):
        blocks.append(
            f"## 片段 {index}（{format_time(chunk.start)} - {format_time(chunk.end)}）\n{content}"
        )
    return "\n\n".join(blocks)


def markdown(report: Report, *, style_label: str = "", include_partials: bool = True) -> str:
    parts = [_header(report, style_label=style_label), "", report.final_summary.strip()]
    if include_partials and report.chunk_summaries:
        parts += ["", "---", "", "# 分段整理（审计用）", "", _partials(report.chunks, report.chunk_summaries)]
    return "\n".join(parts).rstrip() + "\n"


def plain_text(report: Report, *, include_partials: bool = True) -> str:
    parts = [
        report.title,
        "",
        f"Source: {report.url}",
        f"Duration: {format_time(report.duration)}",
        f"Transcript source: {SOURCE_ENGLISH.get(report.source, 'unknown')}",
        f"Segments / chars: {len(report.segments)} / {report.chars}",
        f"Summary model: {report.model or 'n/a'}",
    ]
    for note in report.warnings:
        parts.append(f"WARNING: {note}")
    parts += ["", report.final_summary.strip()]
    if include_partials and report.chunk_summaries:
        parts += ["", "=" * 72, "Detailed segment notes", "=" * 72, ""]
        for chunk, content in zip(report.chunks, report.chunk_summaries, strict=False):
            parts += [
                f"[{format_time(chunk.start)} - {format_time(chunk.end)}]",
                content.strip(),
                "",
            ]
    return "\n".join(parts).rstrip() + "\n"


def chat_message(report: Report, *, max_chars: int = 1900, link: str = "") -> str:
    """The short form a phone chat shows first.

    A full research note is 3-8k characters and unreadable in a chat bubble, so
    the workflow sends the conclusion plus the chapter timeline and points at
    the artefact. Truncation is explicit rather than a silent mid-sentence cut.
    """
    body = report.final_summary.strip()
    if len(body) <= max_chars:
        out = body
    else:
        window = body[:max_chars]
        # Prefer a heading boundary, then a block boundary, then any newline: a
        # chat excerpt that stops mid-sentence reads like a claim about the
        # whole video rather than a preview of it.
        cut = max(
            window.rfind("\n# "),
            window.rfind("\n## "),
            window.rfind("\n\n"),
            window.rfind("\n"),
        )
        if cut < max_chars // 3:
            cut = max(window.rfind("。"), window.rfind("."), window.rfind("!"), window.rfind("！"))
            cut += 1  # keep the sentence mark; dropping it reads like a broken word
        else:
            cut += 1  # a newline boundary is dropped
        out = (window[:cut].rstrip() if cut > 0 else window.rstrip())
        out += "\n\n…（内容较长，以上为节选）"
    header = f"**{report.title}** · {format_time(report.duration)} · 文字来源 {SOURCE_LABELS.get(report.source, '')}"
    footer = f"\n\n来源：{link or report.url}"
    if report.warnings:
        footer += "\n⚠ " + "；".join(report.warnings)[:200]
    return f"{header}\n\n{out}{footer}"


def srt(segments: list[Segment]) -> str:
    rows: list[str] = []
    for index, item in enumerate(segments, start=1):
        speaker = f"[{item.speaker}] " if item.speaker else ""
        rows.extend(
            (str(index), f"{subtitle_time(item.start, ',')} --> {subtitle_time(item.end, ',')}",
             f"{speaker}{item.text}", "")
        )
    return "\n".join(rows)


def vtt(segments: list[Segment]) -> str:
    rows = ["WEBVTT", ""]
    for item in segments:
        speaker = f"<v {item.speaker}>" if item.speaker else ""
        rows.extend(
            (f"{subtitle_time(item.start, '.')} --> {subtitle_time(item.end, '.')}",
             f"{speaker}{item.text}", "")
        )
    return "\n".join(rows)


def transcript_json(segments: list[Segment]) -> str:
    return json.dumps([item.as_dict() for item in segments], ensure_ascii=False, indent=2)


def timestamped_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{format_time(item.start)}] {item.text}" for item in segments)


def payload(report: Report) -> dict[str, Any]:
    """The JSON document ``GET /v1/jobs/{id}/report`` and ``pvc`` both emit."""
    return report.as_dict(include_segments=False) | {
        "report_markdown": markdown(report),
        "report_text": plain_text(report),
        "chat_message": chat_message(report),
        "transcript_source_label": report.transcript_source_label,
        "transcript": json.loads(transcript_json(report.segments)),
    }


def write_all(report: Report, directory: Path, *, style_label: str = "") -> dict[str, str]:
    """Write every artefact and return their paths, as the desktop app did."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    outputs = {
        "summary.md": markdown(report, style_label=style_label),
        "summary.txt": plain_text(report),
        "summary.json": json.dumps(payload(report), ensure_ascii=False, indent=2),
        "transcript.json": transcript_json(report.segments),
        "transcript.txt": timestamped_text(report.segments),
        "transcript.srt": srt(report.segments),
        "transcript.vtt": vtt(report.segments),
        "chat.md": chat_message(report),
    }
    for name, content in outputs.items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written[name] = str(path)
    return written
