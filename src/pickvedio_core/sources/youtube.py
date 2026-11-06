"""YouTube provider: caption tracks first, media download only as a last resort.

Verified behaviour (2026-11-06, user's own egress, captured in
``docs/verification.md``):

* the watch page returns **HTTP 429** and yt-dlp 2026.07.04 fails with
  "Sign in to confirm you're not a bot";
* hard-coded InnerTube static API keys answer 400 "Precondition check failed";
* oEmbed (``/oembed``) still answers anonymously for title and author.

So this provider never pretends a datacenter deployment can transcribe YouTube.
It reports the block precisely, and the browser-relay channel is what makes the
workflow succeed. ``PVC_YOUTUBE_COOKIE_FILE`` (a Netscape file exported from a
logged-in browser -- *not* ``--cookies-from-browser``, which fails on modern
Chrome with "Failed to decrypt with DPAPI") can unblock the direct path.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..errors import PickvedioError, TransientNetworkError, UpstreamBlocked
from ..models import Segment, SourceKind, Transcript
from .net import YOUTUBE, RetryingClient, load_cookie_header
from .parsers import normalise, parse

logger = logging.getLogger("pickvedio.youtube")

OEMBED = "https://www.youtube.com/oembed"
_BOT_HINT = (
    "YouTube 判定该出口 IP 为自动化流量并拒绝（\"Sign in to confirm you're not a bot\"）。"
    "云端 IP 通常无法绕过，也不应绕过。解法：在已登录的浏览器里打开视频页，"
    "执行 skills/video-summary/assets/browser-extract.js 取得字幕，"
    "再用 `pvc push --url <链接> --transcript relay.json` 交给服务总结；"
    "或配置 PVC_YOUTUBE_COOKIE_FILE（Netscape 格式，需从浏览器插件导出）。"
)


@dataclass(slots=True)
class CaptionTrack:
    language: str
    label: str
    generated: bool
    url: str = ""
    fmt: str = "json3"
    segments: list[Segment] | None = None

    @property
    def source_kind(self) -> SourceKind:
        return SourceKind.PLATFORM_AUTO if self.generated else SourceKind.HUMAN_SUBTITLES


def video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/", 1)[0]
    if parsed.path.rstrip("/") == "/watch":
        return parse_qs(parsed.query).get("v", [""])[0]
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in {"shorts", "live", "embed", "v"}:
        return parts[1]
    return parse_qs(parsed.query).get("v", [""])[0]


class YoutubeClient:
    def __init__(
        self,
        *,
        cookie_file: str = "",
        timeout: float = 30.0,
        preferred_languages: tuple[str, ...] = ("zh", "en", "ja"),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookie_file = cookie_file
        self.cookie_header = load_cookie_header(cookie_file) if cookie_file else ""
        self.client = RetryingClient(
            YOUTUBE, timeout=timeout, cookie_header=self.cookie_header, transport=transport
        )
        self.preferred = preferred_languages

    # -- metadata ------------------------------------------------------------

    def oembed(self, url: str) -> dict[str, Any]:
        """Title/author without touching the bot-gated player endpoints."""
        try:
            response = self.client.get(OEMBED, params={"url": url, "format": "json"})
        except TransientNetworkError as exc:
            logger.info("oembed unreachable: %s", exc)
            return {}
        if response.status_code != 200:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # -- ranking -------------------------------------------------------------

    def _rank(self, tracks: list[CaptionTrack]) -> list[CaptionTrack]:
        def score(track: CaptionTrack) -> tuple[int, int, int]:
            language = (track.language or "").lower()
            # 0 first, so a creator-authored track always beats the platform's
            # automatic speech recognition: ASR mishears names and numbers, and
            # a summary inherits that silently.
            manual = 0 if not track.generated else 1
            preference = len(self.preferred)
            for index, wanted in enumerate(self.preferred):
                if language.startswith(wanted.lower()):
                    preference = index
                    break
            # A "raw" auto track beats a translated one at equal rank.
            translated = 1 if "-" in language and language.split("-", 1)[1] in {"en", "zh"} else 0
            return (manual, preference, translated)

        return sorted(tracks, key=score)

    @staticmethod
    def _error_for(exc: Exception) -> Exception:
        text = str(exc)
        lowered = text.lower()
        if "sign in to confirm" in lowered or "not a bot" in lowered:
            return UpstreamBlocked("YouTube 拒绝了自动化会话。", hint=_BOT_HINT, platform="youtube")
        if "precondition check failed" in lowered:
            return UpstreamBlocked("YouTube InnerTube 拒绝了该请求指纹。", hint=_BOT_HINT, platform="youtube")
        if "video unavailable" in lowered or "private video" in lowered or "members-only" in lowered:
            return PickvedioError("视频不可访问（私密、会员专属、下架或地区限制）。")
        return TransientNetworkError(f"YouTube 提取失败：{text[:280]}")

    # -- caption tracks ------------------------------------------------------

    def tracks(self, url: str) -> list[CaptionTrack]:
        """Caption tracks with their parsed segments, media never downloaded.

        Files are parsed *inside* the temp directory's lifetime: yt-dlp writes
        them there and the directory is removed on exit, so a track that points
        at a path would be a dangling reference the moment it was returned.
        """
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise PickvedioError(
                "未安装 yt-dlp，无法直接读取 YouTube 字幕轨道。"
                "安装：`pip install '.[media]'`；或走浏览器回传通道。"
            ) from exc

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "json3/srv3/best",
            "sublangs": ",".join(self.preferred) + ",.*,orig",
            "socket_timeout": int(self.client.timeout),
            "logger": logging.getLogger("pickvedio.yt-dlp"),
        }
        if self.cookie_file:
            options["cookiefile"] = self.cookie_file

        with tempfile.TemporaryDirectory(prefix="pvc-yt-") as tmp:
            options["outtmpl"] = str(Path(tmp) / "cap.%(ext)s")
            info: dict[str, Any] = {}
            try:
                with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore[attr-defined]
                    # download=True with skip_download still writes subtitle files
                    # and returns the full info dict; the media itself is not fetched.
                    info = ydl.extract_info(url, download=True) or {}
                    written = sorted(Path(tmp).glob("cap.*"))
                    manual_langs = {key.lower() for key in (info.get("subtitles") or {})}
                    tracks: list[CaptionTrack] = []
                    for path in written:
                        stem = path.name[len("cap.") :]
                        parts = stem.split(".")
                        language = parts[0] if parts else ""
                        ext = (parts[-1] if len(parts) > 1 else "json3").lower()
                        fmt = {"vtt": "vtt", "xml": "srv3", "srv3": "srv3", "json3": "json3"}.get(ext, "json3")
                        try:
                            payload = path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        segments = parse(payload, fmt=fmt, name=path.name)
                        if not segments:
                            continue
                        tracks.append(
                            CaptionTrack(
                                language=language,
                                label=language,
                                generated=language.lower() not in manual_langs,
                                fmt=fmt,
                                segments=segments,
                            )
                        )
                    if tracks:
                        return self._rank(tracks)
                    listed = self._tracks_from_info(info)
                    fetched: list[CaptionTrack] = []
                    for track in listed[:6]:
                        try:
                            track.segments = self.fetch(track)
                        except (TransientNetworkError, PickvedioError) as exc:
                            logger.info("caption fetch skipped %s: %s", track.language, exc)
                            continue
                        if track.segments:
                            fetched.append(track)
                    return self._rank(fetched)
            except Exception as exc:  # yt-dlp raises its own DownloadError
                if isinstance(exc, (UpstreamBlocked, PickvedioError)):
                    raise
                raise self._error_for(exc) from exc

    def _tracks_from_info(self, info: dict[str, Any]) -> list[CaptionTrack]:
        out: list[CaptionTrack] = []
        for group, generated in ((info.get("subtitles"), False), (info.get("automatic_captions"), True)):
            for language, variants in (group or {}).items():
                for variant in variants or []:
                    target = variant.get("url") or ""
                    ext = (variant.get("ext") or "").lower()
                    if not target:
                        continue
                    out.append(
                        CaptionTrack(
                            language=language,
                            label=variant.get("name") or language,
                            generated=generated,
                            url=target,
                            fmt={"vtt": "vtt", "xml": "srv3", "srt": "srt"}.get(ext, "json3"),
                        )
                    )
        return self._rank(out)

    def fetch(self, track: CaptionTrack) -> list[Segment]:
        if not track.url:
            return []
        response = self.client.get(track.url)
        if response.status_code == 429:
            raise UpstreamBlocked("字幕接口限流（429）。", hint=_BOT_HINT, platform="youtube")
        if response.status_code >= 400:
            raise TransientNetworkError(f"字幕下载失败（{response.status_code}）。")
        return parse(response.text, fmt=track.fmt or "json3", name=track.fmt)

    # -- transcript ----------------------------------------------------------

    def transcript(self, url: str) -> Transcript:
        identifier = video_id(url)
        if not identifier:
            raise PickvedioError(f"无法从链接解析 YouTube 视频号：{url}")
        meta = self.oembed(url)
        tracks = self.tracks(url)
        if not tracks:
            raise UpstreamBlocked(
                "YouTube 未返回任何可读的字幕轨道（可能确实没有字幕，也可能会话被拒）。",
                hint=_BOT_HINT,
                platform="youtube",
            )
        for track in tracks:
            segments = track.segments
            if segments is None and track.url:
                # A listed track can arrive without a body (the caller only had
                # the manifest). Fetching here keeps `transcript()` correct for
                # any track source, instead of silently reporting "too short"
                # for a caption track it never tried to read.
                try:
                    segments = self.fetch(track)
                    track.segments = segments
                except (TransientNetworkError, PickvedioError) as exc:
                    logger.info("caption fetch skipped %s: %s", track.language, exc)
                    continue
            segments = segments or []
            if len(segments) < 3:
                continue
            duration = segments[-1].end if segments else None
            return Transcript(
                video_url=f"https://www.youtube.com/watch?v={identifier}",
                title=meta.get("title") or "未命名视频",
                duration=duration,
                segments=normalise(segments, duration),
                source=track.source_kind,
                language=track.language,
                channel=f"youtube:{track.fmt}",
                meta={
                    "video_id": identifier,
                    "uploader": meta.get("author_name") or "",
                    "tracks": [
                        {"language": item.language, "generated": item.generated,
                         "lines": len(item.segments or [])}
                        for item in tracks[:12]
                    ],
                },
            )
        raise UpstreamBlocked(
            "YouTube 字幕轨道内容过短，不足以生成逐字稿。", hint=_BOT_HINT, platform="youtube"
        )
