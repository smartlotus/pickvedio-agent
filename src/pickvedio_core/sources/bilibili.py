"""Bilibili provider.

Verified behaviour this module is written against (probed on 2026-11-06 from the
user's own network, captured in ``docs/verification.md``):

* ``/x/web-interface/view`` answers anonymously -- HTTP 200, ``code: 0`` -- and
  yields title, duration, cid and the page list. Metadata is therefore free.
* ``/x/player/v2`` and ``/x/player/wbi/v2`` answer ``code: 0`` with an **empty**
  subtitle list for an anonymous session, including after a buvid3 handshake.
  Six different videos were sampled this way (``docs/verification.md``) and not
  one returned a track, so the honest statement is: an anonymous session sees
  nothing. This code deliberately does NOT claim to know whether a given video
  has creator-authored captions -- "zero tracks" is treated as *this session
  cannot see them*, which is the weaker claim that is also the actionable one,
  because the remedy (read them from a logged-in browser) works either way.
  See :class:`AuthRequired` and :mod:`.relay`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..errors import AuthRequired, PickvedioError, TransientNetworkError, UpstreamBlocked
from ..models import Segment, SourceKind, Transcript
from ..util import format_time
from .net import BILIBILI, RetryingClient, load_cookie_header
from .parsers import from_bilibili_body, normalise

logger = logging.getLogger("pickvedio.bilibili")

API = "https://api.bilibili.com"
_BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")
_AVID_RE = re.compile(r"\bav(\d+)\b", re.IGNORECASE)

# Bilibili business codes that mean "the platform refused this session", not
# "the video does not exist".
_RISK_CODES = {-352, -403, 62002, -412, 3, -400}
_MESSAGE_HINTS = {
    -352: "B 站风控拦截（-352）：该出口 IP 或请求指纹被判定为脚本访问。",
    -403: "B 站拒绝访问（-403）：签名失效、频率超限或需要登录。",
    -412: "B 站前置校验失败（-412）：需要真实浏览器会话。",
    62002: "视频不存在或已下架（62002）。",
}

_RELAY_HINT = (
    "B 站字幕（含 AI 字幕）只向已登录会话下发。匿名 API 返回 0 条轨道不代表视频没有字幕。"
    "解法：由已登录的浏览器同源抓取后回传——"
    "在视频页执行 skills/video-summary/assets/browser-extract.js，"
    "再执行 `pvc push --url <链接> --transcript relay.json`。"
    "或配置 PVC_BILIBILI_SESSDATA / PVC_COOKIE_FILE 后重试。"
)


def absolute_subtitle_url(url: str) -> str:
    """Turn whatever form Bilibili put in `subtitle_url` into a fetchable URL.

    Three shapes have been observed: protocol-relative (`//`-prefixed),
    root-relative (`/x/player/v2subtitle?...`, which crashed httpx with
    "unknown url type" the first time a real payload used it), and absolute.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/"):
        return API + raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return API + "/" + raw.lstrip("./")


@dataclass(slots=True)
class BilibiliVideo:
    bvid: str
    aid: int | None
    cid: int
    title: str
    duration: float
    pages: list[dict[str, Any]]
    uploader: str
    description: str
    webpage_url: str
    thumbnail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "aid": self.aid,
            "cid": self.cid,
            "title": self.title,
            "duration": self.duration,
            "pages": len(self.pages),
            "uploader": self.uploader,
            "webpage_url": self.webpage_url,
            "thumbnail": self.thumbnail,
        }


class BilibiliClient:
    def __init__(
        self,
        *,
        sessdata: str = "",
        cookie_file: str = "",
        cookie_header: str = "",
        timeout: float = 25.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = []
        if cookie_header:
            parts.append(cookie_header)
        if sessdata:
            parts.append(f"SESSDATA={sessdata}; bili_jct=dummy; DedeUserID=0")
        if cookie_file:
            loaded = load_cookie_header(cookie_file)
            if loaded:
                parts.append(loaded)
        self.client = RetryingClient(BILIBILI, timeout=timeout, cookie_header="; ".join(parts),
                                     transport=transport)
        self.authenticated = bool(sessdata or cookie_file or cookie_header)

    def resolve(self, url: str) -> str:
        """Follow ``b23.tv`` short links to the canonical video URL."""
        host = (urlparse(url).hostname or "").lower()
        if host not in {"b23.tv", "www.b23.tv"}:
            return url
        response = self.client.get(url)
        return str(response.url) or url

    def video(self, url: str, page: int = 1) -> BilibiliVideo:
        url = self.resolve(url)
        bvid_match = _BVID_RE.search(url)
        av_match = _AVID_RE.search(url)
        params: dict[str, Any] = {}
        if bvid_match:
            params["bvid"] = bvid_match.group(1)
        elif av_match:
            params["aid"] = int(av_match.group(1))
        else:
            cid = parse_qs(urlparse(url).query).get("cid", [""])[0]
            if cid:
                params["cid"] = int(cid)
            else:
                raise PickvedioError(f"无法从链接中解析 B 站视频号：{url}")
        response = self.client.get(f"{API}/x/web-interface/view", params=params)
        payload = self._json(response)
        data = payload.get("data") or {}
        pages = data.get("pages") or []
        if not data:
            raise PickvedioError(f"B 站未返回视频信息：{payload.get('message')}")
        cid = data.get("cid")
        if pages:
            index = max(1, min(page, len(pages)))
            cid = pages[index - 1].get("cid", cid)
        return BilibiliVideo(
            bvid=data.get("bvid") or (bvid_match.group(1) if bvid_match else ""),
            aid=data.get("aid"),
            cid=int(cid or 0),
            title=data.get("title") or "未命名视频",
            duration=float(data.get("duration") or 0),
            pages=pages,
            uploader=data.get("owner", {}).get("name", "") if isinstance(data.get("owner"), dict) else "",
            description=data.get("description") or "",
            webpage_url=data.get("share_url") or url,
            thumbnail=data.get("pic") or "",
        )

    def subtitle_tracks(self, video: BilibiliVideo) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for endpoint in ("/x/player/v2", "/x/player/wbi/v2"):
            try:
                response = self.client.get(
                    f"{API}{endpoint}", params={"bvid": video.bvid, "cid": video.cid}
                )
                payload = self._json(response, soft=True)
            except (TransientNetworkError, PickvedioError) as exc:
                last_error = exc
                continue
            data = payload.get("data") or {}
            rows = ((data.get("subtitle") or {}).get("subtitles")) or []
            for row in rows:
                tracks.append(
                    {
                        "lan": row.get("lan") or "",
                        "lan_doc": row.get("lan_doc") or "",
                        "ai_type": int(row.get("ai_type") or 0),
                        "url": absolute_subtitle_url(row.get("subtitle_url") or ""),
                        "endpoint": endpoint,
                    }
                )
            if tracks:
                return tracks
        if not tracks and last_error is not None:
            logger.info("subtitle track discovery failed outright: %s", last_error)
        return tracks

    def fetch_track(self, track: dict[str, Any]) -> list[Segment]:
        url = absolute_subtitle_url(str(track.get("url") or ""))
        if not url:
            return []
        response = self.client.get(url)
        return from_bilibili_body(response.text)

    def transcript(self, url: str, *, page: int = 1, prefer_ai: bool = False) -> Transcript:
        video = self.video(url, page=page)
        tracks = self.subtitle_tracks(video)
        if not tracks:
            # Two honest possibilities, and they need different remedies.
            if not self.authenticated:
                raise AuthRequired(
                    "B 站未向当前匿名会话下发任何字幕轨道；AI 字幕需要已登录 Cookie。"
                    f"（{video.title}，时长 {format_time(video.duration)}）",
                )
            raise PickvedioError(
                f"该视频没有可用的 B 站字幕轨道（{video.title}），需要改用音频转写或浏览器回传。"
            )
        manual = [item for item in tracks if not item.get("ai_type")]
        chosen = manual[0] if manual else tracks[0]
        if prefer_ai and tracks:
            chosen = tracks[0]
        segments = self.fetch_track(chosen)
        if not segments:
            raise PickvedioError(f"字幕轨道 {chosen.get('lan')} 返回空内容。")
        source = SourceKind.UP_SUBTITLES if not chosen.get("ai_type") else SourceKind.PLATFORM_AUTO
        return Transcript(
            video_url=video.webpage_url,
            title=video.title,
            duration=video.duration or None,
            segments=normalise(segments, video.duration or None),
            source=source,
            language=chosen.get("lan") or "",
            channel=f"bilibili:{chosen.get('endpoint', 'player/v2')}",
            meta={
                **video.as_dict(),
                "tracks": [{"lan": t["lan"], "ai_type": t["ai_type"]} for t in tracks],
                "authenticated_session": self.authenticated,
            },
        )

    @staticmethod
    def _json(response: httpx.Response, *, soft: bool = False) -> dict[str, Any]:
        if response.status_code == 412 or looks_risky(response.text):
            raise UpstreamBlocked(
                _MESSAGE_HINTS.get(-412, "B 站前置校验失败。"), hint=_RELAY_HINT, platform="bilibili"
            )
        if response.status_code >= 400:
            raise TransientNetworkError(
                f"B 站接口返回 {response.status_code}：{response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientNetworkError(f"B 站接口返回了非 JSON 内容：{response.text[:160]}") from exc
        code = int(payload.get("code") or 0)
        if code != 0:
            hint = _MESSAGE_HINTS.get(code, payload.get("message") or "")
            if code in _RISK_CODES and not soft:
                raise UpstreamBlocked(
                    hint or f"B 站接口返回 code={code}。", hint=_RELAY_HINT, platform="bilibili"
                )
            if code in _RISK_CODES and soft:
                # `player/v2` legitimately answers non-zero for a page it will not
                # describe to this session; the caller treats "no tracks" as a
                # signal to escalate, and the reason is logged.
                logger.info("bilibili soft code %s: %s", code, hint)
                return {}
            raise PickvedioError(f"{hint}（code={code}）")
        return payload


def looks_risky(text: str) -> bool:
    lowered = (text or "").lower()
    return "risk control" in lowered or "访问已熔断" in (text or "") or '"code":-412' in lowered.replace(" ", "")
