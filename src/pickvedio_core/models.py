"""Data model, kept deliberately close to the desktop app's shapes.

``Segment``/``TranscriptChunk`` have the same fields as ``pickvedio/models.py`` so
exports, stored jobs and prompt templates remain interchangeable with projects
the desktop app already produced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}
        if self.speaker:
            out["speaker"] = self.speaker
        return out


@dataclass(slots=True)
class TranscriptChunk:
    index: int
    start: float
    end: float
    text: str


class SourceKind(str, Enum):
    """How the words were obtained. Changes the confidence of everything downstream."""

    UP_SUBTITLES = "up_subtitles"          # creator-authored, most reliable
    HUMAN_SUBTITLES = "human_subtitles"    # subtitle site / community translation
    PLATFORM_AUTO = "platform_auto"        # bilibili AI / YouTube ASR captions
    WHISPER = "whisper"                    # our own ASR over the downloaded audio
    RELAYED = "relayed"                    # pulled from a logged-in browser by the agent
    PAGE_TEXT = "page_text"                # description / article fallback, not a transcript
    UNKNOWN = "unknown"


# Ranked by how much the summary may lean on them. Used by the ladder to stop at
# the first channel that produced usable content, and by the report to state
# honestly which tier a summary came from.
SOURCE_CONFIDENCE: dict[SourceKind, int] = {
    SourceKind.UP_SUBTITLES: 5,
    SourceKind.HUMAN_SUBTITLES: 4,
    SourceKind.RELAYED: 4,
    SourceKind.PLATFORM_AUTO: 3,
    SourceKind.WHISPER: 3,
    SourceKind.PAGE_TEXT: 1,
    SourceKind.UNKNOWN: 0,
}


@dataclass(slots=True)
class Transcript:
    video_url: str
    title: str
    duration: float | None
    segments: list[Segment]
    source: SourceKind
    language: str = ""
    channel: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence(self) -> int:
        return SOURCE_CONFIDENCE.get(self.source, 0)

    @property
    def char_count(self) -> int:
        return sum(len(item.text) for item in self.segments)

    @property
    def plain_text(self) -> str:
        return "\n".join(item.text for item in self.segments)


class JobStage(str, Enum):
    QUEUED = "queued"
    METADATA = "metadata"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    PREPARING_AUDIO = "preparing_audio"
    TRANSCRIBING = "transcribing"
    CHUNKING = "chunking"
    SUMMARIZING = "summarizing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    NEEDS_BROWSER = "needs_browser"


@dataclass(slots=True)
class SummaryPlan:
    """What the user asked for, resolved into concrete knobs."""

    style: str = "general"        # a prompts.PROFILE_LABELS key
    chunk_chars: int = 18000
    max_chunks: int = 40
    language: str = "zh"
    include_timestamps: bool = True
    template_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Report:
    url: str
    title: str
    duration: float | None
    source: SourceKind
    language: str
    final_summary: str
    chunk_summaries: list[str]
    chunks: list[TranscriptChunk]
    segments: list[Segment]
    model: str = ""
    stage_seconds: dict[str, float] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    job_id: str = ""

    @property
    def chars(self) -> int:
        return sum(len(item.text) for item in self.segments)

    def as_dict(self, *, include_segments: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "url": self.url,
            "title": self.title,
            "duration": self.duration,
            "transcript_source": self.source.value,
            "transcript_confidence": self.confidence,
            "language": self.language,
            "model": self.model,
            "summary": self.final_summary,
            "chunk_summaries": self.chunk_summaries,
            "warnings": self.warnings,
            "usage": self.usage,
            "stage_seconds": self.stage_seconds,
            "segments_count": len(self.segments),
            "chars": sum(len(item.text) for item in self.segments),
        }
        if include_segments:
            data["segments"] = [item.as_dict() for item in self.segments]
        return data

    @property
    def confidence(self) -> int:
        return SOURCE_CONFIDENCE.get(self.source, 0)
