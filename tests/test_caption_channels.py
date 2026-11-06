"""Positive controls for the caption channels.

Everything else in the suite proves the *relay* path, because on the author's
machine the platforms refuse every caption request. That left the most important
question in the product unanswered: when a session CAN see subtitles -- which is
what happens on the user's deployed agent -- does the whole chain from platform
caption data to a finished Chinese report actually work?

These tests answer it with realistic payload shapes served over a transport the
providers' real request shapes drive, so the request-building code is exercised
too, not just the parsing.
"""
from __future__ import annotations

import json

import httpx
import pytest

from pickvedio_core.config import Settings
from pickvedio_core.models import SourceKind
from pickvedio_core.pipeline import run as pipeline_run
from pickvedio_core.sources import bilibili as bilibili_module
from pickvedio_core.sources.bilibili import BilibiliClient
from pickvedio_core.sources.youtube import CaptionTrack, YoutubeClient

BVID = "BV1rj411D75Z"
CID = 114514
BILI_URL = f"https://www.bilibili.com/video/{BVID}/"

SUBTITLE_BODY = {
    "body": [
        {"from": 0.0, "to": 4.5, "content": "今天我们讲强磁场下的电磁波衰减。"},
        {"from": 4.5, "to": 9.0, "content": "先看麦克斯韦方程组在介质中的形式。"},
        {"from": 9.0, "to": 15.0, "content": "电导率 σ 决定衰减快慢，这一点常被忽略。"},
        {"from": 40.0, "to": 46.0, "content": "反例是超导体：内部电场为零。"},
        {"from": 46.0, "to": 52.0, "content": "所以角动量守恒在这个近似下仍然成立。"},
    ]
}


def bilibili_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/x/web-interface/view":
        assert request.url.params["bvid"] == BVID
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "data": {
                    "bvid": BVID,
                    "aid": 114514,
                    "cid": CID,
                    "title": "强磁场下的电磁波衰减 | 硬核公开课",
                    "duration": 60,
                    "share_url": BILI_URL,
                    "pic": "https://i0.hdslb.com/bfs/cover/x.jpg",
                    "description": "课程简介，不是逐字稿。",
                    "owner": {"name": "硬核公开课"},
                    "pages": [{"cid": CID, "page": 1, "part": "P1"}],
                },
            },
        )
    if path in ("/x/player/v2", "/x/player/wbi/v2"):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "data": {
                    "cid": CID,
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "zh-CN",
                                "lan_doc": "中文",
                                "ai_type": 0,
                                "subtitle_url": "/x/player/v2subtitle?mid=1&lan=zh-CN",
                            }
                        ]
                    },
                },
            },
        )
    if path == "/x/player/v2subtitle":
        return httpx.Response(200, json=SUBTITLE_BODY)
    raise AssertionError(f"unexpected bilibili request: {request.url}")


def youtube_json3() -> dict:
    cues = [
        (0, "Welcome to the lecture on waveguides."),
        (2500, "First, the boundary conditions."),
        (6000, "Note the losses in the dielectric."),
        (12000, "Here is the counter-example: a hollow conductor."),
    ]
    events = []
    for start, text in cues:
        events.append({"tStartMs": start, "dDurationMs": 2400, "segs": [{"utf8": text}]})
        # json3 interleaves newline-only window events; a naive parser emits an
        # empty segment for each and the transcript looks twice as long.
        events.append({"tStartMs": start + 1200, "dDurationMs": 100, "segs": [{"utf8": "\n"}]})
    return {"events": events}


def youtube_caption_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/timedtext":
        return httpx.Response(200, json=youtube_json3())
    if request.url.path == "/oembed":
        return httpx.Response(
            200, json={"title": "Waveguides, explained", "author_name": "Lecture Channel"}
        )
    raise AssertionError(f"unexpected youtube request: {request.url}")


# -- bilibili ------------------------------------------------------------------


def test_bilibili_client_reads_an_up_subtitles_track():
    client = BilibiliClient(transport=httpx.MockTransport(bilibili_handler))
    transcript = client.transcript(BILI_URL)
    assert transcript.source is SourceKind.UP_SUBTITLES
    assert transcript.confidence == 5
    assert transcript.title.startswith("强磁场")
    assert transcript.duration == 60.0
    assert [item.text for item in transcript.segments][:2] == [
        "今天我们讲强磁场下的电磁波衰减。",
        "先看麦克斯韦方程组在介质中的形式。",
    ]
    # the protocol-relative subtitle_url was resolved against api.bilibili.com
    assert transcript.meta["tracks"] == [{"lan": "zh-CN", "ai_type": 0}]
    assert transcript.meta["authenticated_session"] is False


def test_bilibili_zero_length_cue_gets_a_floor_not_a_negative_range():
    payload = json.dumps({"body": [{"from": 5.0, "to": 5.0, "content": "只有一瞬的话"}]})
    handler = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    client = BilibiliClient(transport=handler)
    rows = client.fetch_track({"url": "https://api.bilibili.com/x/player/v2subtitle", "lan": "zh"})
    assert rows and rows[0].end > rows[0].start


def test_bilibili_ai_track_is_labelled_platform_auto():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return bilibili_handler(request)
        if request.url.path == "/x/player/v2":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "subtitle": {
                            "subtitles": [
                                {"lan": "ai-zh", "ai_type": 1, "subtitle_url": "/x/player/v2subtitle"}
                            ]
                        }
                    },
                },
            )
        return bilibili_handler(request)

    client = BilibiliClient(sessdata="fake-sessdata", transport=httpx.MockTransport(handler))
    transcript = client.transcript(BILI_URL)
    assert transcript.source is SourceKind.PLATFORM_AUTO
    assert transcript.confidence == 3
    assert transcript.meta["authenticated_session"] is True


def test_bilibili_risk_control_surfaces_as_needs_browser():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -352, "message": "风控拦截"})

    client = BilibiliClient(transport=httpx.MockTransport(handler))
    with pytest.raises(bilibili_module.UpstreamBlocked) as caught:
        client.video(BILI_URL)
    assert caught.value.code == "needs_browser"


# -- youtube -------------------------------------------------------------------


def test_youtube_transcript_prefers_manual_over_asr_and_parses_json3():
    client = YoutubeClient(transport=httpx.MockTransport(youtube_caption_handler))
    info = {
        "subtitles": {"en": [{"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=x",
                             "name": "English"}]},
        "automatic_captions": {"en-orig": [{"ext": "json3",
                                            "url": "https://www.youtube.com/api/timedtext?v=y",
                                            "name": "auto"}]},
    }
    tracks = client._rank(client._tracks_from_info(info))
    assert [t.language for t in tracks] == ["en", "en-orig"]
    assert tracks[0].generated is False and tracks[1].generated is True

    monkeypatched = YoutubeClient.tracks
    YoutubeClient.tracks = lambda self, url: tracks
    try:
        transcript = client.transcript("https://youtu.be/dQw4w9WgXcQ")
    finally:
        YoutubeClient.tracks = monkeypatched
    assert transcript.source is SourceKind.HUMAN_SUBTITLES
    assert len(transcript.segments) == 4  # the "\n" window events were dropped
    assert transcript.segments[0].text.startswith("Welcome")
    assert transcript.title == "Waveguides, explained"
    assert transcript.duration == pytest.approx(14.4)


def test_youtube_translated_auto_track_is_marked_generated():
    client = YoutubeClient(transport=httpx.MockTransport(youtube_caption_handler))
    track = CaptionTrack(
        language="zh-Hans", label="Chinese (auto)", generated=True,
        url="https://www.youtube.com/api/timedtext?v=z", fmt="json3",
    )
    rows = client.fetch(track)
    assert rows and track.source_kind is SourceKind.PLATFORM_AUTO


# -- the whole chain, captions to report ---------------------------------------


def test_pipeline_summarises_platform_captions_end_to_end(settings: Settings, monkeypatch):
    """The path that fires on the user's deployment, proven without a relay.

    Ladder, chunking, per-chunk prompts, the reduction decision, synthesis and
    every renderer all run for real against a stub provider; only the caption
    source is a transport stand-in, because on the author's network the platforms
    refuse it.
    """
    transport = httpx.MockTransport(bilibili_handler)
    real_init = BilibiliClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(BilibiliClient, "__init__", patched)

    outcome = pipeline_run(BILI_URL, settings, style="general", job_id="captions-e2e")
    report = outcome.report
    assert report.source is SourceKind.UP_SUBTITLES
    assert report.transcript_source_label == "UP 主自制字幕（可信度高）"
    assert len(report.segments) == 5
    assert report.final_summary.strip()
    first = outcome.resolution.trail[0]
    # `code` is a failure-only field: an attempt that worked carries no code.
    assert (first["channel"], first["ok"], first["code"]) == ("bilibili_subtitles", True, "")
    assert first["detail"].startswith("5 段 /")
    assert "文字来源：UP 主自制字幕（可信度高） · 5 段" in outcome.markdown_text
    assert "分段整理（审计用）" in outcome.markdown_text
    # 91 chars fits one chunk, so no reduction layer should have been used.
    assert outcome.usage["chunk_calls"] == 1 and outcome.usage["final_calls"] == 1
    assert (settings.workdir / "jobs" / "captions-e2e" / "summary" / "summary.md").is_file()
    assert "page_text" not in json.dumps(outcome.resolution.trail)


def test_pipeline_uses_ai_captions_and_says_so(settings: Settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/player/v2":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"subtitle": {"subtitles": [
                    {"lan": "ai-zh", "ai_type": 1, "subtitle_url": "/x/player/v2subtitle"}]}}},
            )
        return bilibili_handler(request)

    real_init = BilibiliClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(BilibiliClient, "__init__", patched)
    outcome = pipeline_run(BILI_URL, settings, style="brief", job_id="ai-captions")
    assert outcome.report.source is SourceKind.PLATFORM_AUTO
    assert "自动生成" in outcome.markdown_text
    assert outcome.report.transcript_source_label == "平台自动生成字幕（可能存在识别错误）"
