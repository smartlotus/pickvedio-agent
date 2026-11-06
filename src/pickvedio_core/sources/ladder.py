"""The transcript ladder: ordered channels, honest failure.

Order is a cost/benefit decision, not a preference:

1. **relayed** -- free, exact, works for gated captions; available whenever the
   caller (the agent, the browser extension, a human) supplies one.
2. **platform captions** -- Bilibili subtitle API / YouTube caption tracks. Free
   and instant when the session can see them, which on a datacenter IP usually
   means YouTube cannot and Bilibili cannot see AI subtitles.
3. **audio + ASR** -- the only channel that works with no captions at all, and
   the only one that costs money or compute. Skipped unless configured.
4. **page text** -- the description. Off unless explicitly allowed, because
   summarising a description and presenting it as a summary *of the video* is
   the specific dishonesty this pipeline must not commit.

Every attempt is recorded, so the caller can see what was tried and why each
channel failed, instead of getting a bare error. When nothing worked, the
raised error enumerates the remedies in the order that is cheapest first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from ..config import Settings
from ..errors import AuthRequired, EmptyTranscript, PickvedioError, UpstreamBlocked
from ..models import Transcript
from ..urls import classify
from . import audio as audio_module
from . import bilibili as bilibili_module
from . import relay as relay_module
from . import youtube as youtube_module

logger = logging.getLogger("pickvedio.ladder")


@dataclass(slots=True)
class Attempt:
    channel: str
    ok: bool
    detail: str = ""
    code: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "detail": self.detail[:300],
            "code": self.code,
            "seconds": round(self.seconds, 2),
        }


@dataclass(slots=True)
class Resolution:
    transcript: Transcript
    attempts: list[Attempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def trail(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.attempts]


def _attempt(channel: str, exc: Exception) -> Attempt:
    code = getattr(exc, "code", "error")
    return Attempt(channel=channel, ok=False, detail=str(exc), code=code)


def resolve(
    url: str,
    settings: Settings,
    *,
    relay_payload: Any = None,
    relay_meta: dict[str, Any] | None = None,
    workdir: Path | None = None,
    page: int = 1,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: Callable[[], None] = lambda: None,
    allow_asr: bool = True,
    allow_page_text: bool = False,
) -> Resolution:
    """Try every configured channel in cost order and return the first usable one."""
    import time

    attempts: list[Attempt] = []
    warnings: list[str] = []
    ref = classify(url)
    block_reasons: list[str] = []

    def report(ratio: float, message: str) -> None:
        if on_progress is not None:
            on_progress(ratio, message)

    # 1. relayed ----------------------------------------------------------------
    if relay_payload is not None:
        started = time.monotonic()
        meta = relay_meta or {}
        result = relay_module.to_transcript(
            url,
            relay_payload,
            title=str(meta.get("title") or ""),
            duration=meta.get("duration"),
            language=str(meta.get("language") or ""),
            source_kind=str(meta.get("source_kind") or meta.get("sourceKind") or ""),
            uploader=str(meta.get("uploader") or ""),
            origin=str(meta.get("origin") or "relay"),
            name=str(meta.get("name") or ""),
            fmt=str(meta.get("format") or ""),
            strict=bool(meta.get("strict", True)),
        )
        attempts.append(
            Attempt("relay", True, f"{len(result.transcript.segments)} 段 / {result.transcript.char_count} 字",
                    seconds=time.monotonic() - started)
        )
        return Resolution(transcript=result.transcript, attempts=attempts, warnings=result.warnings)

    # 2. platform captions ------------------------------------------------------
    if ref.platform == "bilibili":
        client = bilibili_module.BilibiliClient(
            sessdata=settings.sessdata, cookie_file=settings.cookie_file
        )
        started = time.monotonic()
        try:
            transcript = client.transcript(url, page=page)
            attempts.append(
                Attempt("bilibili_subtitles", True,
                        f"{len(transcript.segments)} 段 / {transcript.char_count} 字",
                        seconds=time.monotonic() - started)
            )
            report(0.5, f"已取得字幕：{transcript.title}")
            return Resolution(transcript, attempts, warnings)
        except (AuthRequired, UpstreamBlocked) as exc:
            attempts.append(_attempt("bilibili_subtitles", exc))
            block_reasons.append(f"bilibili_subtitles: {exc}")
            if isinstance(exc, UpstreamBlocked):
                block_reasons.append(f"hint: {exc.hint}")
            report(0.2, str(exc))
        except EmptyTranscript as exc:
            attempts.append(_attempt("bilibili_subtitles", exc))
        except PickvedioError as exc:
            attempts.append(_attempt("bilibili_subtitles", exc))
    elif ref.platform == "youtube":
        client = youtube_module.YoutubeClient(cookie_file=settings.cookie_file)
        started = time.monotonic()
        try:
            transcript = client.transcript(url)
            attempts.append(
                Attempt("youtube_captions", True,
                        f"{len(transcript.segments)} 段 / {transcript.char_count} 字",
                        seconds=time.monotonic() - started)
            )
            report(0.5, f"已取得字幕轨道：{transcript.language}")
            return Resolution(transcript, attempts, warnings)
        except (UpstreamBlocked, AuthRequired, PickvedioError) as exc:
            attempts.append(_attempt("youtube_captions", exc))
            block_reasons.append(f"youtube_captions: {exc}")
            if isinstance(exc, UpstreamBlocked):
                block_reasons.append(f"hint: {exc.hint}")
            report(0.2, str(exc))
    else:
        started = time.monotonic()
        if ref.platform == "other" and not settings.allow_unknown_platform:
            exc = PickvedioError(
                f"未识别的站点 {ref.video_id}。若确认该链接是可公开访问的视频，"
                "可设置 PVC_ALLOW_UNKNOWN_PLATFORM=1 改用通用下载通道。"
            )
            attempts.append(_attempt("generic_captions", exc))
            raise UpstreamBlocked(
                str(exc),
                hint="通用站点没有字幕接口：请在浏览器中提取字幕后用 `pvc push` 回传，"
                     "或开启 PVC_ALLOW_UNKNOWN_PLATFORM 走音频转写。",
                platform=ref.video_id,
            )
        block_reasons.append("generic_captions: 该平台无字幕接口，跳过")
        attempts.append(Attempt("generic_captions", False, "无字幕接口", seconds=time.monotonic() - started))

    # 3. audio + ASR ------------------------------------------------------------
    asr_reason = settings.asr.disabled_reason()
    if allow_asr and settings.asr.enabled:
        from ..asr import build_transcriber

        channel = audio_module.AudioPipeline(
            ffmpeg_path=settings.ffmpeg_path,
            cookie_file=settings.cookie_file,
            sessdata=settings.sessdata,
            chunk_seconds=settings.audio.chunk_seconds,
        )
        transcriber = build_transcriber(settings)
        if transcriber is not None:
            started = time.monotonic()
            target = workdir or (settings.workdir / f"adhoc-{abs(hash(url)) % 10**8}")
            report(0.55, "无可用字幕，改用音频转写（会产生 API 费用或本地算力）")
            try:
                transcript = channel.run(
                    url, Path(target), transcriber, on_progress=report, cancel=cancel,
                    duration_hint=None,
                )
                attempts.append(
                    Attempt("audio_asr", True, f"{len(transcript.segments)} 段",
                            seconds=time.monotonic() - started)
                )
                warnings.append(
                    f"音频转写通道耗时 {round((time.monotonic() - started) / 60.0, 1)} 分钟，"
                    f"上游约 {transcript.char_count} 字。"
                )
                return Resolution(transcript, attempts, warnings)
            except (UpstreamBlocked, AuthRequired, PickvedioError) as exc:
                attempts.append(_attempt("audio_asr", exc))
                block_reasons.append(f"audio_asr: {exc}")
    else:
        if asr_reason:
            attempts.append(Attempt("audio_asr", False, asr_reason, code="configuration"))
            block_reasons.append(f"audio_asr: {asr_reason}")

    # 4. page text --------------------------------------------------------------
    if allow_page_text and ref.platform == "bilibili":
        started = time.monotonic()
        try:
            video = bilibili_module.BilibiliClient(
                sessdata=settings.sessdata, cookie_file=settings.cookie_file
            ).video(url, page=page)
            text = (video.description or "").strip()
            transcript = relay_module.to_transcript(
                url,
                {"title": video.title, "duration": video.duration, "text": text,
                 "segments": []} if text else {"title": video.title, "duration": video.duration,
                                               "text": ""},
                title=video.title,
                duration=video.duration,
                source_kind="page_text",
                uploader=video.uploader,
                origin="page_description",
                strict=False,
            )
            attempts.append(Attempt("page_text", True, f"{len(transcript.segments)} 行",
                                   seconds=time.monotonic() - started))
            warnings.append(
                "本报告基于视频页面的简介文字，不是视频语音内容。请勿当作视频摘要引用。"
            )
            return Resolution(transcript, attempts, warnings)
        except PickvedioError as exc:
            attempts.append(_attempt("page_text", exc))

    remedy = [
        "1) 在已登录的浏览器里打开该视频页，执行 "
        "skills/video-summary/assets/browser-extract.js 取得字幕 JSON，"
        "然后 `pvc push --url <链接> --transcript relay.json --summarize`；",
        "2) 配置 PVC_BILIBILI_SESSDATA 或 PVC_COOKIE_FILE（Netscape 格式）后重试；",
    ]
    if asr_reason:
        remedy.append(
            f"3) 配置音频转写通道（当前不可用：{asr_reason}），例如 "
            "PVC_ASR_PROVIDER=groq + PVC_ASR_API_KEY=... + PVC_ASR_MODEL=whisper-large-v3-turbo；"
        )
    else:
        remedy.append("3) 音频转写通道已配置但仍失败，见 attempts 里的 audio_asr 条目。")
    raise UpstreamBlocked(
        "没有可用通道取得该视频的逐字稿。" + " || ".join(block_reasons)[:700],
        hint=" ".join(remedy),
        platform=ref.platform,
    )


def describe_channels(settings: Settings) -> dict[str, Any]:
    """What this deployment can actually do right now -- ``pvc status`` data."""
    from ..asr import build_transcriber

    asr = None
    try:
        asr = build_transcriber(settings)
    except PickvedioError as exc:
        asr_error = str(exc)
    else:
        asr_error = ""
    return {
        "relay": {"available": True, "note": "任何时候都可接受浏览器回传的字幕。"},
        "bilibili_subtitles": {
            "available": True,
            "authenticated_session": bool(settings.sessdata or settings.cookie_file),
            "note": (
                "已配置登录态，可请求 AI 字幕。"
                if (settings.sessdata or settings.cookie_file)
                else "匿名会话通常看不到 AI 字幕（服务端只在已登录会话下发）。"
            ),
        },
        "youtube_captions": {
            "available": True,
            "note": "云端 IP 通常被机器人校验拦截；被拦截时使用浏览器回传通道。",
            "yt_dlp": _has_module("yt_dlp"),
        },
        "audio_asr": {
            "available": bool(asr is not None and not asr_error),
            "provider": settings.asr.provider,
            "disabled_reason": asr_error or settings.asr.disabled_reason(),
            "ffmpeg": bool(_which(settings.ffmpeg_path)),
        },
        "page_text": {
            "available": True,
            "enabled_by_default": False,
            "note": "默认关闭：简介文字不是视频内容，只有在明确允许时才会被使用。",
        },
    }


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _which(configured: str) -> str:
    import shutil

    if configured and Path(configured).is_file():
        return configured
    return shutil.which("ffmpeg") or ""
