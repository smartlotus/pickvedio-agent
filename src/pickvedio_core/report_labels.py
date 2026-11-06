"""The one place transcript provenance is worded.

Split from ``report.py`` so ``models.Report`` can expose it without importing the
renderers (the renderers import the model), which would be an import cycle.
``page_text`` deliberately reads as a warning: it is not what was said.
"""
from __future__ import annotations

from .models import SourceKind

SOURCE_LABELS = {
    SourceKind.UP_SUBTITLES: "UP 主自制字幕（可信度高）",
    SourceKind.HUMAN_SUBTITLES: "人工字幕（可信度高）",
    SourceKind.RELAYED: "浏览器回传字幕（可信度中高）",
    SourceKind.PLATFORM_AUTO: "平台自动生成字幕（可能存在识别错误）",
    SourceKind.WHISPER: "Whisper 语音转写（可能存在识别错误）",
    SourceKind.PAGE_TEXT: "⚠ 页面简介文字，不是视频内容",
    SourceKind.UNKNOWN: "未知来源",
}

SOURCE_ENGLISH = {
    SourceKind.UP_SUBTITLES: "creator-authored subtitles",
    SourceKind.HUMAN_SUBTITLES: "human subtitles",
    SourceKind.RELAYED: "captions relayed from a logged-in browser",
    SourceKind.PLATFORM_AUTO: "platform auto-generated captions",
    SourceKind.WHISPER: "ASR transcription",
    SourceKind.PAGE_TEXT: "page description text (NOT the video's speech)",
    SourceKind.UNKNOWN: "unknown source",
}
