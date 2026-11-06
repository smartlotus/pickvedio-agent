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

__version__ = "1.0.4"

REPO_URL = "https://github.com/smartlotus/pickvedio-agent"


def install_spec(extra: str | None = None) -> str:
    """Return the pip requirement line that resolves today.

    The distribution is not published to PyPI, so a hint like
    ``pip install pickvedio-core`` sends an agent into "No matching distribution
    found" and it reads that as a bug in this tool. Everything that tells a
    machine how to install this package goes through here, so the pin cannot
    drift from ``__version__``.
    """
    name = f"pickvedio-core[{extra}]" if extra else "pickvedio-core"
    return f'{name} @ git+{REPO_URL}@v{__version__}'

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
    "REPO_URL",
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
    "install_spec",
    "load_prompts",
    "validate_video_url",
]
