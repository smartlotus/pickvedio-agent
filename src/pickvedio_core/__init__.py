"""pickvedio-core -- headless video transcript + layered summary.

Ported from the Windows desktop application ``pickvedio`` (PySide6, local
faster-whisper, local Ollama) into a library, a JSON CLI (``pvc``) and an HTTP
service, so that an agent platform can deploy it and a phone user can simply
send it a video link.

Public surface:

.. code-block:: python

    from pickvedio_core import Settings, pipeline
    outcome = pipeline.run(url, Settings.load(), style="brief")
    print(outcome.markdown_text)

Nothing in this package imports Qt, and nothing writes outside ``PVC_WORKDIR``.
"""
from __future__ import annotations

from .config import ASRSettings, LLMSettings, Settings
from .errors import (
    AuthRequired,
    ConfigurationError,
    DependencyError,
    EmptyTranscript,
    JobNotFound,
    PickvedioError,
    RateLimited,
    TransientNetworkError,
    UpstreamBlocked,
    UpstreamModelError,
)
from .models import Report, Segment, SourceKind, SummaryPlan, Transcript, TranscriptChunk
from .prompts import PROFILE_LABELS, load as load_prompts
from .urls import classify, extract_video_urls, validate_video_url

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "ASRSettings",
    "AuthRequired",
    "ConfigurationError",
    "DependencyError",
    "EmptyTranscript",
    "JobNotFound",
    "LLMSettings",
    "PickvedioError",
    "PROFILE_LABELS",
    "RateLimited",
    "Report",
    "Segment",
    "Settings",
    "SourceKind",
    "SummaryPlan",
    "TransientNetworkError",
    "Transcript",
    "TranscriptChunk",
    "UpstreamBlocked",
    "UpstreamModelError",
    "classify",
    "extract_video_urls",
    "load_prompts",
    "validate_video_url",
]
