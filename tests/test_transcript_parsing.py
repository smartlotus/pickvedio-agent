"""Caption parsing and the relay channel's guards.

Every assertion here decides whether a summary is built on real words or on an
artefact of parsing: a rolling-caption duplicate halves nothing but doubles the
apparent content, an untimed payload quietly produces fake timestamps, and a
sparse relay of a 3-hour video produces a confident summary of a fragment.
"""
from __future__ import annotations

import json

import pytest

from pickvedio_core.errors import EmptyTranscript, PickvedioError
from pickvedio_core.models import SourceKind
from pickvedio_core.sources.parsers import (
    from_bilibili_body,
    from_json3,
    from_lrc,
    from_plain_text,
    from_srv3,
    from_srt,
    from_vtt,
    merge_short,
    normalise,
    sniff_format,
)
from pickvedio_core.sources.relay import segments_from_payload, to_transcript


def test_json3_flattens_segments_and_skips_newline_events():
    body = json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "你好"}, {"utf8": "，世界"}]},
        {"tStartMs": 2000, "dDurationMs": 100, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 3000, "dDurationMs": 2500, "segs": [{"utf8": "第二句"}]},
    ]})
    rows = from_json3(body)
    assert [item.text for item in rows] == ["你好，世界", "第二句"]
    assert rows[0].start == 0.0 and rows[0].end == pytest.approx(2.0)
    assert rows[1].end == pytest.approx(5.5)


def test_json3_collapses_rolling_caption_duplicates():
    body = json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "今天我们讨论角动量"}]},
        {"tStartMs": 1500, "dDurationMs": 2000, "segs": [{"utf8": "今天我们讨论角动量"}]},
        {"tStartMs": 3000, "dDurationMs": 2000, "segs": [{"utf8": "守恒定律"}]},
    ]})
    rows = from_json3(body)
    assert [item.text for item in rows] == ["今天我们讨论角动量", "守恒定律"]
    # The duplicate cue extended the first one's end rather than vanishing.
    assert rows[0].end == pytest.approx(3.5)


def test_srt_with_and_without_index_lines():
    with_index = "1\n00:00:01,000 --> 00:00:05,500\n第一句\n第二行\n\n2\n00:00:06,000 --> 00:00:09,000\n第三句\n"
    without_index = "00:00:01,000 --> 00:00:05,000\n仅时间轴\n"
    assert [item.text for item in from_srt(with_index)] == ["第一句 第二行", "第三句"]
    assert from_srt(without_index)[0].end == pytest.approx(5.0)
    assert from_srt(without_index)[0].text == "仅时间轴"


def test_srt_drops_cue_text_that_is_only_markup():
    rows = from_srt("1\n00:00:01,000 --> 00:00:02,000\n   \n\n2\n00:00:03,000 --> 00:00:04,000\n真内容\n")
    assert [item.text for item in rows] == ["真内容"]


def test_vtt_keeps_speaker_and_strips_style_tags():
    payload = (
        "WEBVTT\n\nNOTE this is a comment\n\n00:00:01.000 --> 00:00:04.000\n<v 讲者甲><c.yellow>大家好\n\n"
        "00:00:05.000 --> 00:00:08.000\n<b>下一句</b>\n"
    )
    rows = from_vtt(payload)
    assert rows[0].speaker == "讲者甲" and "大家好" in rows[0].text
    assert "<" not in rows[1].text and rows[1].text == "下一句"


def test_srv3_reads_timed_text_xml():
    payload = (
        '<?xml version="1.0"?><text><text start="1.0" dur="3.2">第一句 &amp; 符号</text>'
        '<text start="5.0" dur="2.0">第二句</text></text>'
    )
    rows = from_srv3(payload)
    assert [item.text for item in rows] == ["第一句 & 符号", "第二句"]
    assert rows[0].end == pytest.approx(4.2)


def test_bilibili_subtitle_body_maps_from_to_content():
    payload = {"body": [{"from": 0.0, "to": 3.5, "content": "句子一"},
                        {"from": 3.5, "to": 3.5, "content": "零时长行"},
                        {"from": 10.0, "to": 12.0, "content": ""}]}
    rows = from_bilibili_body(payload)
    assert [item.text for item in rows] == ["句子一", "零时长行"]
    assert rows[1].end > rows[1].start  # zero-length cue gets a floor, not a bogus range


def test_lrc_and_plain_text_fallbacks():
    rows = from_lrc("[01:02.50]第一行\n[01:05.00]第二行\n")
    assert rows[0].start == pytest.approx(62.5) and rows[0].end == pytest.approx(65.0)
    untimed = from_plain_text("a\nb\nc", duration=30.0)
    assert len(untimed) == 3 and untimed[-1].end == pytest.approx(30.0)
    assert from_plain_text("a\nb\nc")[0].end == 0.0  # no duration claimed


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("x.vtt", "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", "vtt"),
        ("x.json3", '{"events": []}', "json3"),
        ("x.xml", "<text><text start=\"1\">hi</text></text>", "srv3"),
        ("x.srt", "1\n00:00:01,000 --> 00:00:02,000\nhi\n", "srt"),
        ("x.lrc", "[00:01.00] hi", "lrc"),
        ("notes.txt", "just prose", "plain"),
        ("", "just prose", "plain"),
    ],
)
def test_sniff_format(name, payload, expected):
    assert sniff_format(name, payload) == expected


def test_merge_short_packs_only_adjacent_fragments():
    from pickvedio_core.models import Segment

    rows = [Segment(0, 1, "AA"), Segment(1, 2, "BB"), Segment(30, 31, "CC")]
    merged = merge_short(rows, max_chars=100, max_gap=1.2)
    assert [item.text for item in merged] == ["AA BB", "CC"]
    assert merged[0].end == 2.0


def test_normalise_clamps_and_orders():
    from pickvedio_core.models import Segment

    rows = [Segment(10, 8, "倒序"), Segment(0, 100, "超长"), Segment(-3, -1, "负时间")]
    fixed = normalise(rows, duration=12.0)
    assert fixed[0].start == 0.0
    assert all(item.end > item.start for item in fixed)
    assert all(item.end <= 12.0 for item in fixed)
    assert [item.start for item in fixed] == sorted(item.start for item in fixed)


# -- relay guards ------------------------------------------------------------

GOOD = {"segments": [{"start": 0.0, "end": 5.0, "text": "第一句内容"} for _ in range(20)],
        "title": "回传测试", "duration": 300, "sourceKind": "up_subtitles"}


def test_relay_honours_declared_caption_kind_not_generic_relay():
    result = to_transcript("https://www.bilibili.com/video/BV1rjKn6CETw/", GOOD)
    assert result.transcript.source is SourceKind.UP_SUBTITLES
    assert result.transcript.confidence == 5
    assert result.transcript.channel == "relay"


def test_relay_without_declared_kind_is_labelled_relay_with_a_warning():
    payload = dict(GOOD)
    payload.pop("sourceKind")
    result = to_transcript("https://www.bilibili.com/video/BV1rjKn6CETw/", payload)
    assert result.transcript.source is SourceKind.RELAYED
    assert any("来源类型" in note for note in result.warnings)


def test_relay_refuses_a_sparse_payload_for_a_long_video():
    """2 hours, one short sentence: must fail, not summarise a fragment."""
    with pytest.raises(PickvedioError) as caught:
        to_transcript(
            "https://www.bilibili.com/video/BV1rjKn6CETw/",
            {"segments": [{"start": 0.0, "end": 3.0, "text": "短"}], "duration": 7200},
            duration=7200,
            source_kind="up_subtitles",
        )
    assert "密度" in str(caught.value)


def test_relay_allow_sparse_turns_the_guard_into_a_warning():
    result = to_transcript(
        "https://www.bilibili.com/video/BV1rjKn6CETw/",
        {"segments": [{"start": 0.0, "end": 3.0, "text": "短"}], "duration": 7200},
        duration=7200,
        source_kind="up_subtitles",
        strict=False,
    )
    assert any("密度" in note for note in result.warnings)


def test_relay_rejects_empty_and_reports_the_reason():
    with pytest.raises(EmptyTranscript):
        to_transcript("https://www.bilibili.com/video/BV1rjKn6CETw/", {"segments": []})
    with pytest.raises(EmptyTranscript):
        to_transcript("https://www.bilibili.com/video/BV1rjKn6CETw/", "   \n  \n")


# Captured verbatim from the published browser-extract.js (v1.0.4, fetched from
# the CDN and evaluated inside a live Bilibili page, logged-out session). This
# is what a real relay looks like when there is nothing to relay.
LIVE_EMPTY_RELAY = {
    "platform": "bilibili",
    "url": "https://www.bilibili.com/video/BV1pa5Ez5E2e/",
    "title": "YouTube怎么翻译字幕成中文字幕",
    "durationSeconds": 104,
    "sourceKind": None,
    "language": "",
    "trackLabel": "",
    "trackCount": 0,
    "segments": [],
    "charCount": 0,
    "warnings": [
        "Bilibili served an empty subtitle list to this session. Either this account "
        "is not logged in (AI subtitles require a logged-in session) or the video has "
        "no subtitles at all."
    ],
    "reason": "no_subtitles_for_session",
    "extractedAt": "2026-11-06T17:07:40.229Z",
}


def test_empty_relay_keeps_the_extractors_reason_and_warnings():
    """The reason is the difference between "log in" and "this video has none".

    The old message was one generic sentence for every empty payload, so the
    agent had to open relay.json itself and interpret a field the tool had
    already read.
    """
    with pytest.raises(EmptyTranscript) as caught:
        to_transcript(LIVE_EMPTY_RELAY["url"], LIVE_EMPTY_RELAY)
    text = str(caught.value)
    assert "no_subtitles_for_session" in text
    assert "empty subtitle list" in text, text  # the extractor's own warning survives
    assert "登录" in caught.value.hint


def test_hint_changes_with_the_reason():
    """Proves the hint is a lookup, not a constant sentence."""
    hints = set()
    for reason in ("no_subtitles_for_session", "not_playable", "rate_limited", "extraction_failed"):
        payload = dict(LIVE_EMPTY_RELAY, reason=reason)
        with pytest.raises(EmptyTranscript) as caught:
            to_transcript(LIVE_EMPTY_RELAY["url"], payload)
        hints.add(caught.value.hint)
    assert len(hints) == 4, hints


def test_relay_reason_hints_cover_every_reason_the_extractor_can_emit():
    """No invented vocabulary, and no reason the shipped script can emit is unmapped.

    The extractor is 31 KB of browser JavaScript whose only consumer on the
    server side is this dict; a reason added there and not here silently degrades
    to the generic fallback for the user's phone.
    """
    import re
    from pathlib import Path

    from pickvedio_core.sources.relay import RELAY_REASON_HINTS

    root = Path(__file__).resolve().parents[1]
    js = (root / "skills/video-summary/assets/browser-extract.js").read_text(encoding="utf-8")
    emitted: set[str] = set()
    for line in js.splitlines():
        if "reason" in line:
            emitted.update(re.findall(r"[\"']([a-z][a-z_]{3,40})[\"']", line))
    assert len(emitted) >= 12, f"only {len(emitted)} reasons parsed out of the JS; the extractor changed shape"

    missing = sorted(emitted - set(RELAY_REASON_HINTS))
    assert not missing, f"extractor emits reasons with no hint: {missing}"

    docs = (root / "skills/video-summary/references/browser-extract.md").read_text(encoding="utf-8")
    invented = sorted(key for key in RELAY_REASON_HINTS if key not in emitted and f"`{key}`" not in docs)
    assert not invented, f"hint map cites reasons nothing produces: {invented}"
    undocumented = sorted(key for key in emitted if f"`{key}`" not in docs)
    assert not undocumented, f"extractor emits reasons the reason table does not explain: {undocumented}"


def test_relay_reads_metadata_from_json_text_not_only_from_dicts():
    """`--transcript '<inline JSON>'` and `--transcript relay.json` arrive as text.

    The metadata lookups used to run only when the payload was already a dict,
    so on the most common invocation the title, the duration, the declared
    source kind and the extractor's reason all vanished -- the report silently
    lost its provenance label and the sparse guard lost the duration it needs.
    """
    payload = json.dumps({
        "platform": "bilibili",
        "title": "三小时讲座",
        "durationSeconds": 300,
        "sourceKind": "up_subtitles",
        "segments": [{"start": idx * 10.0, "end": idx * 10.0 + 9.0, "text": f"第 {idx} 句内容足够长。" * 4}
                     for idx in range(1, 6)],
    }, ensure_ascii=False)
    result = to_transcript("https://www.bilibili.com/video/BV1rjKn6CETw/", payload)
    assert result.transcript.title == "三小时讲座"
    assert result.transcript.duration == 300.0
    assert result.transcript.source is SourceKind.UP_SUBTITLES
    assert "未声明字幕来源类型" not in " ".join(result.warnings)


def test_relay_detects_untimed_content_and_warns():
    payload = {"segments": [{"text": f"第 {index} 句，内容足够长以避免密度门禁生效。" * 3}
                            for index in range(12)], "duration": 600}
    result = to_transcript("https://www.bilibili.com/video/BV1rjKn6CETw/", payload, duration=600,
                           source_kind="relayed")
    assert any("没有时间信息" in note for note in result.warnings)
    assert result.checks["timed"] is False


def test_segments_from_payload_accepts_srt_vtt_json3_text_and_files(tmp_path):
    srt = "1\n00:00:01,000 --> 00:00:05,000\n来自 SRT 的一句\n"
    assert segments_from_payload(srt, name="a.srt")[0].text == "来自 SRT 的一句"
    path = tmp_path / "cap.json3"
    path.write_text(json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 2000,
                                            "segs": [{"utf8": "来自文件"}]}]}), encoding="utf-8")
    assert segments_from_payload(str(path))[0].text == "来自文件"
    inline = json.dumps({"segments": [{"start": 1, "end": 4, "text": "内联"}]})
    assert segments_from_payload(inline)[0].text == "内联"


def test_unrecognised_caption_body_degrades_to_plain_text_not_an_error():
    """Prose with no cue markers is still words; the *source label* carries the doubt."""
    rows = segments_from_payload("这是一段没有任何时间标记的普通中文文字。\n第二行文字也很长。")
    assert len(rows) == 2
    assert all(item.end >= item.start for item in rows)
    with pytest.raises(PickvedioError):
        segments_from_payload(12345)
