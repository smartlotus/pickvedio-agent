"""Transcript source channels.

``ladder.resolve`` is the only entry point callers should use; the individual
modules are exposed for tests and for ``pvc fetch --channel`` diagnostics.
"""
from __future__ import annotations

from .ladder import Attempt, Resolution, describe_channels, resolve
from .relay import RelayResult, segments_from_payload, to_transcript
from .youtube import video_id as youtube_video_id

__all__ = [
    "Attempt",
    "RelayResult",
    "Resolution",
    "describe_channels",
    "resolve",
    "segments_from_payload",
    "to_transcript",
    "youtube_video_id",
]
