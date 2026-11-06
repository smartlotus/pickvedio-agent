"""URL recognition and canonicalisation.

Ported from the pickvedio desktop app (``pickvedio/services.py``) so that share
texts pasted from a phone keep working: Bilibili copy-messages with a title and
brackets, ``b23.tv`` short links, ``?bvid=`` query forms, bare BV/av ids,
YouTube watch/share/Shorts/live/embed links.

The desktop app rejected anything it did not recognise. This build additionally
classifies *any other* URL as ``platform="other"``, because the hosted service
runs yt-dlp, which supports hundreds of sites -- but ``other`` is only accepted
by callers that opt in (``allow_unknown_platform=True``). That keeps a typo'd
link from silently becoming a long-running job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from .errors import PickvedioError

_URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；;、]+", re.IGNORECASE)
_BVID_PATTERN = re.compile(r"\b(BV[0-9A-Za-z]{10})\b", re.IGNORECASE)
_AVID_PATTERN = re.compile(r"\b(av\d+)\b", re.IGNORECASE)
_BILIBILI_HOSTS = {"www.bilibili.com", "bilibili.com", "b23.tv", "www.b23.tv"}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_YOUTUBE_ID_PATTERN = re.compile(r"^[0-9A-Za-z_-]{11}$")


def canonical_bvid(identifier: str) -> str:
    """Repair the ``bv...`` prefix a phone share sometimes emits.

    Only the two-character prefix is normalised: the remainder of a BV id is a
    case-sensitive base-58 payload, so lowercasing ``BV1rjKn6CETw`` to
    ``bv1rjkn6cetw`` points at a video that does not exist.
    """
    if identifier[:2].lower() == "bv" and len(identifier) > 2:
        return "BV" + identifier[2:]
    return identifier


def canonical_identifier(identifier: str) -> str:
    if identifier.lower().startswith("av"):
        return identifier.lower()
    return canonical_bvid(identifier)


@dataclass(slots=True)
class VideoRef:
    """A normalised target plus enough identity for a provider to dispatch on."""

    url: str
    platform: str
    video_id: str = ""
    extra: dict | None = None

    @property
    def identity(self) -> str:
        return f"{self.platform}:{self.video_id}" if self.video_id else self.platform


def _strip_noise(candidate: str) -> str:
    # The URL regex reads through Markdown closing punctuation, so
    # `[title](https://youtu.be/ID)` arrives with a trailing `)`.
    return candidate.rstrip(".,!?;:，。！？；：、）】》>}\"'") .rstrip(")]}")


def extract_bilibili_urls(text: str) -> list[str]:
    """Extract and normalise Bilibili video links from any copied text."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = _strip_noise(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    for match in _URL_PATTERN.finditer(text.replace("\u200b", "")):
        raw = match.group(0).rstrip(")]}")
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host not in _BILIBILI_HOSTS:
            continue
        if host in {"b23.tv", "www.b23.tv"}:
            add(raw)
            continue
        video_match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", parsed.path, re.IGNORECASE)
        if video_match:
            add(f"https://www.bilibili.com/video/{canonical_identifier(video_match.group(1))}/")

    if candidates:
        return candidates

    # Pasting a page title plus a BV/av id is also common in notes or chat.
    bvid = _BVID_PATTERN.search(text)
    if bvid:
        return [f"https://www.bilibili.com/video/{canonical_bvid(bvid.group(1))}/"]
    avid = _AVID_PATTERN.search(text)
    if avid:
        return [f"https://www.bilibili.com/video/{avid.group(1).lower()}/"]
    return []


def _normalize_youtube_url(parsed) -> str | None:
    """Return a stable watch URL for YouTube watch, share, Shorts and live links."""
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.path.rstrip("/").lower().startswith(("/shorts/", "/live/", "/embed/")):
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2:
            video_id = path_parts[1]
    if not _YOUTUBE_ID_PATTERN.fullmatch(video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_video_urls(text: str) -> list[str]:
    """Bilibili and YouTube video URLs, in the order they occur in the text."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = _strip_noise(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    for match in _URL_PATTERN.finditer(text.replace("\u200b", "")):
        raw = match.group(0).rstrip(")]}")
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host in _BILIBILI_HOSTS:
            if host in {"b23.tv", "www.b23.tv"}:
                add(raw)
                continue
            video_match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", parsed.path, re.IGNORECASE)
            if video_match:
                add(f"https://www.bilibili.com/video/{canonical_identifier(video_match.group(1))}/")
                continue
            bvid = parse_qs(parsed.query).get("bvid", [""])[0]
            if _BVID_PATTERN.fullmatch(bvid):
                add(f"https://www.bilibili.com/video/{canonical_bvid(bvid)}/")
        else:
            youtube_url = _normalize_youtube_url(parsed)
            if youtube_url:
                add(youtube_url)

    if candidates:
        return candidates
    # Preserve the friendly B站 share-text handling for a note that contains
    # only a title plus BV/av identifier and no explicit URL.
    return extract_bilibili_urls(text)


def classify(url: str) -> VideoRef:
    """Map any HTTP(S) URL onto a platform tag and a stable video id."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _BILIBILI_HOSTS:
        if host in {"b23.tv", "www.b23.tv"}:
            return VideoRef(url=url, platform="bilibili", extra={"short_link": True})
        video_match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", parsed.path, re.IGNORECASE)
        identifier = video_match.group(1) if video_match else parse_qs(parsed.query).get("bvid", [""])[0]
        return VideoRef(url=url, platform="bilibili",
                        video_id=canonical_identifier(identifier) if identifier else "")
    youtube_url = _normalize_youtube_url(parsed)
    if youtube_url:
        video_id = parse_qs(urlparse(youtube_url).query)["v"][0]
        return VideoRef(url=youtube_url, platform="youtube", video_id=video_id)
    return VideoRef(url=url, platform="other", video_id=host)


def validate_video_url(url: str, *, allow_unknown_platform: bool = False) -> str:
    matches = extract_video_urls(url)
    if matches:
        return matches[0]
    if allow_unknown_platform:
        found = _URL_PATTERN.search(url.replace("\u200b", ""))
        if found:
            return _strip_noise(found.group(0))
    raise PickvedioError(
        "未识别到有效视频链接。支持 B 站分享文案、b23 短链、YouTube 链接、Shorts 与 youtu.be 短链。"
    )


def find_all_video_urls(text: str, *, allow_unknown_platform: bool = False) -> list[VideoRef]:
    """Every distinct video in a blob of text, in reading order.

    Batch mode and the agent workflow both need this: a pasted note frequently
    carries several links, and the caller has to decide which one to run.
    """
    refs: list[VideoRef] = []
    seen: set[str] = set()
    for raw in extract_video_urls(text):
        if raw not in seen:
            seen.add(raw)
            refs.append(classify(raw))
    if not allow_unknown_platform:
        return refs
    # Additive when unknown hosts are allowed: one note can carry a Bilibili
    # link plus a site only yt-dlp knows how to speak to.
    for match in _URL_PATTERN.finditer(text.replace("\u200b", "")):
        cleaned = _strip_noise(match.group(0))
        ref = classify(cleaned)
        if ref.platform != "other" or cleaned in seen:
            continue
        seen.add(cleaned)
        refs.append(ref)
    return refs
