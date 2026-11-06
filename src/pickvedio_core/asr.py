"""Speech-to-text channels.

Two transports, one contract (``transcribe(path) -> list[Segment]``):

* :class:`ApiTranscriber` -- any OpenAI-shaped ``/audio/transcriptions`` endpoint.
  Covers OpenAI ``whisper-1``/``gpt-4o-transcribe``, Groq
  ``whisper-large-v3-turbo``, and a self-hosted faster-whisper server exposing
  the same route. ``verbose_json`` is requested so segment timestamps survive;
  providers that ignore it and return only a ``text`` field degrade to
  evenly-spread timings, which is recorded as a warning rather than passed off
  as real alignment.
* :class:`LocalWhisperTranscriber` -- faster-whisper in-process, the desktop
  app's original behaviour, kept for self-hosted and "audio never leaves the
  machine" deployments.

The 25 MB / ~27 minute upload ceiling is handled in :mod:`.sources.audio`, which
slices the normalised WAV; this module never receives an oversized file.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

import httpx

from .config import ASRSettings, AudioSettings
from .errors import ConfigurationError, DependencyError, RateLimited, UpstreamModelError
from .models import Segment
from .util import ProgressCallback, format_time

logger = logging.getLogger("pickvedio.asr")


@dataclass(slots=True)
class AsrUsage:
    seconds: float = 0.0
    requests: int = 0
    audio_seconds: float = 0.0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "asr_requests": self.requests,
            "asr_seconds": round(self.seconds, 2),
            "asr_audio_seconds": round(self.audio_seconds, 1),
            "asr_errors": list(self.errors or []),
        }


class ApiTranscriber:
    RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

    def __init__(
        self,
        settings: ASRSettings,
        audio: AudioSettings | None = None,
        usage: AsrUsage | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.audio = audio or AudioSettings.from_env()
        self.usage = usage if usage is not None else AsrUsage()
        self._transport = transport

    @property
    def endpoint(self) -> str:
        base = self.settings.base_url.rstrip("/")
        if base.endswith("/audio/transcriptions"):
            return base
        return f"{base}/audio/transcriptions"

    def transcribe(
        self,
        audio_path: Path,
        on_progress: ProgressCallback | None = None,
        cancel: Callable[[], None] = lambda: None,
    ) -> list[Segment]:
        if not (self.settings.base_url and self.settings.model):
            raise ConfigurationError(
                "PVC_ASR_PROVIDER 需要 PVC_ASR_BASE_URL 与 PVC_ASR_MODEL 才能工作。"
            )
        path = Path(audio_path)
        if not path.is_file() or path.stat().st_size <= 44:
            raise UpstreamModelError(f"待转写的音频不存在或为空：{path.name}")
        payload_size = path.stat().st_size
        # 16 kHz mono PCM16 is exactly 32 000 bytes per second.
        duration = max(0.0, (payload_size - 44) / 32000.0)
        data = {
            "model": self.settings.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if self.audio.language:
            data["language"] = self.audio.language
        last: Exception | None = None
        for _attempt, delay in enumerate((0.0, 3.0, 9.0, 21.0)):
            cancel()
            if delay:
                time.sleep(delay)
            started = time.monotonic()
            try:
                with path.open("rb") as handle:
                    files = {"file": (path.name, handle, "audio/wav")}
                    client_kwargs: dict[str, Any] = {
                        "timeout": httpx.Timeout(self.settings.request_timeout, connect=20.0)
                    }
                    if self._transport is not None:
                        client_kwargs["transport"] = self._transport
                    with httpx.Client(**client_kwargs) as client:
                        headers = {}
                        if self.settings.api_key:
                            headers["Authorization"] = f"Bearer {self.settings.api_key}"
                        response = client.post(self.endpoint, data=data, files=files, headers=headers)
                if response.status_code in self.RETRY_STATUS:
                    claimed = (response.headers.get("Retry-After") or "").strip()
                    last = RateLimited(
                        f"转写服务返回 {response.status_code}：{response.text[:200]}",
                        retry_after_seconds=int(claimed) if claimed.isdigit() else None,
                    )
                    self.usage.errors.append(str(last)[:160])
                    continue
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 413:
                    raise UpstreamModelError(
                        f"音频过大被拒（413，{payload_size / 1e6:.1f} MB）。"
                        "请调小 PVC_ASR_CHUNK_SECONDS。"
                    ) from exc
                last = UpstreamModelError(f"转写服务返回 {exc.response.status_code}：{exc.response.text[:240]}")
                self.usage.errors.append(str(last)[:160])
                continue
            except httpx.HTTPError as exc:
                last = UpstreamModelError(f"无法连接转写服务：{exc}")
                self.usage.errors.append(str(last)[:160])
                continue
            elapsed = time.monotonic() - started
            self.usage.requests += 1
            self.usage.seconds += elapsed
            self.usage.audio_seconds += duration
            segments, timed = self._segments(body, duration)
            if not segments:
                raise UpstreamModelError("转写服务返回了空结果。")
            if not timed and on_progress is not None:
                on_progress(1.0, "转写完成（该服务未返回分段时间点，已按长度均匀估计）")
            return segments
        assert last is not None
        raise last

    @staticmethod
    def _segments(body: dict[str, Any], duration: float) -> tuple[list[Segment], bool]:
        rows = body.get("segments") or []
        segments: list[Segment] = []
        for row in rows:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            start = float(row.get("start") or 0.0)
            end = float(row.get("end") or start)
            segments.append(Segment(start=start, end=max(end, start + 0.2), text=text))
        if segments:
            return segments, True
        text = str(body.get("text") or "").strip()
        if not text:
            return [], False
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return [], False
        step = (duration or len(text) / 16.0) / len(lines)
        return [
            Segment(start=index * step, end=(index + 1) * step, text=line)
            for index, line in enumerate(lines)
        ], False


class LocalWhisperTranscriber:
    """faster-whisper in-process, ported from the desktop app.

    The app split long recordings so faster-whisper's one-shot log-mel STFT
    stayed inside memory; that constraint is real (it OOM'd at ~400 MiB of
    intermediates on a desktop with a model resident) and is kept here, but the
    slicing is done with the stdlib ``wave`` module instead of numpy so the
    dependency stays optional.
    """

    SEGMENT_THRESHOLD_SECONDS = 20 * 60
    SEGMENT_SECONDS = 10 * 60

    def __init__(self, model_name: str, audio: AudioSettings | None = None) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise DependencyError(
                "未安装 faster-whisper。本地转写请执行：pip install '.[local-asr]'"
            ) from exc
        if not model_name.strip():
            raise ConfigurationError("本地转写需要 PVC_ASR_MODEL 指定模型名（如 small / large-v3）。")
        self.model = WhisperModel(model_name)
        self.audio = audio or AudioSettings.from_env()
        self.model_name = model_name

    def transcribe(
        self,
        audio_path: Path,
        on_progress: ProgressCallback | None = None,
        cancel: Callable[[], None] = lambda: None,
    ) -> list[Segment]:
        path = Path(audio_path)
        kwargs: dict[str, Any] = {
            "language": self.audio.language or None,
            "vad_filter": self.audio.use_vad,
            "beam_size": self.audio.beam_size,
            "condition_on_previous_text": True,
            "word_timestamps": False,
        }
        layout = self._wav_layout(path)
        emit = on_progress or (lambda ratio, message: None)
        if layout and layout[0] / layout[1] > self.SEGMENT_THRESHOLD_SECONDS:
            return self._transcribe_blockwise(path, layout, kwargs, emit, cancel)
        iterator, info = self.model.transcribe(str(path), **kwargs)
        duration = float(getattr(info, "duration", 0) or 0)
        segments: list[Segment] = []
        for index, item in enumerate(iterator, start=1):
            cancel()
            text = str(item.text).strip()
            if text:
                segments.append(Segment(start=float(item.start), end=float(item.end), text=text))
            if duration:
                emit(min(0.99, float(item.end) / duration), f"已转写 {format_time(item.end)} · {index} 段")
        if not segments:
            raise UpstreamModelError("没有识别到语音。请检查音频、语言与 Whisper 模型。")
        emit(1.0, f"转写完成 · 共 {len(segments)} 段")
        return segments

    def _transcribe_blockwise(
        self,
        path: Path,
        layout: tuple[int, int],
        kwargs: dict[str, Any],
        emit: ProgressCallback,
        cancel: Callable[[], None],
    ) -> list[Segment]:
        total_frames, rate = layout
        total_duration = total_frames / rate
        block_frames = self.SEGMENT_SECONDS * rate
        collected: list[Segment] = []
        for start_frame in range(0, total_frames, block_frames):
            cancel()
            end_frame = min(start_frame + block_frames, total_frames)
            block = self._slice_wav(path, start_frame, end_frame)
            try:
                iterator, _ = self.model.transcribe(str(block), **kwargs)
                for item in iterator:
                    text = str(item.text).strip()
                    if text:
                        collected.append(
                            Segment(
                                start=float(item.start) + start_frame / rate,
                                end=float(item.end) + start_frame / rate,
                                text=text,
                            )
                        )
            finally:
                block.unlink(missing_ok=True)
            emit(
                min(0.99, end_frame / total_frames),
                f"已转写 {format_time(end_frame / rate)} / {format_time(total_duration)} · 共 {len(collected)} 段",
            )
        if not collected:
            raise UpstreamModelError("没有识别到语音。请检查音频、语言与 Whisper 模型。")
        emit(1.0, f"转写完成 · 共 {len(collected)} 段")
        return collected

    @staticmethod
    def _wav_layout(path: Path) -> tuple[int, int] | None:
        import wave

        try:
            with wave.open(str(path), "rb") as wav:
                if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                    return None
                return wav.getnframes(), wav.getframerate()
        except (OSError, Exception):  # noqa: BLE001 - a non-WAV input simply has no layout
            return None

    @staticmethod
    def _slice_wav(path: Path, start_frame: int, end_frame: int) -> Path:
        import wave

        destination = path.with_name(f"{path.stem}_slice_{start_frame}.wav")
        with wave.open(str(path), "rb") as source:
            params = source.getparams()
            source.setpos(start_frame)
            frames = source.readframes(end_frame - start_frame)
        with wave.open(str(destination), "wb") as target:
            target.setparams(params)
            target.writeframes(frames)
        return destination


def build_transcriber(settings, transport=None) -> ApiTranscriber | LocalWhisperTranscriber | None:
    """Return the configured transcriber, or ``None`` when ASR is off.

    ``None`` is a legitimate, useful answer: the subtitle and relay channels do
    not need ASR, and a deployment that skips it costs nothing per request.
    """
    provider = (settings.asr.provider or "").strip().lower()
    if not provider:
        return None
    if provider in {"faster_whisper", "local", "whisper_local"}:
        return LocalWhisperTranscriber(settings.asr.model, settings.audio)
    return ApiTranscriber(settings.asr, settings.audio, transport=transport)


def estimated_cost_minutes(seconds: float) -> float:
    return round(seconds / 60.0, 2)


class CancelFlag(threading.Event):
    """A cancellation channel shared by the CLI, the worker thread and the API."""

    def raise_if_set(self) -> None:
        if self.is_set():
            from .errors import CancelledError

            raise CancelledError("任务已取消。")
