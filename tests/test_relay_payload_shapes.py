"""Every shape an agent can actually paste as a relay, and what it must return.

Measured against the shipped 1.0.19 code before this file existed:

```
empty_string / whitespace / {}   -> rc=1  empty_transcript      (correct)
[]                                -> rc=0  ok                   (WRONG: silent success)
null / undefined / missing file   -> rc=1  error, hint empty     (unnameable)
relay with reason=no_subtitles…   -> rc=1  empty_transcript + reason hint
a real 120-cue relay              -> rc=0  ok
```

The `[]` row is the dangerous one: to_transcript decoded the pasted text into
`document` for the metadata, then handed the *raw string* to
segments_from_payload, whose string branch only re-parses payloads starting with
`{`. So an empty array fell into the caption-body parser and came back as one
cue of literal text -- zero real speech, exit code 0. An agent following the
skill would then summarise nothing and report success.

These tests are the regression gate for that class, not just for that input:
the last one asserts the invariant that was violated -- nothing may exit 0 while
carrying zero segments.
"""
from __future__ import annotations

import json

import pytest

from pickvedio_core.sources.relay import to_transcript

URL = "https://www.bilibili.com/video/BV1pa5Ez5E2e/"


def good_relay(count: int = 120, duration: int = 2400) -> str:
    segments = [{"start": float(i * 15), "end": float(i * 15 + 14),
                 "text": "这句话是用来填满切块的字幕内容。" * 4} for i in range(1, count + 1)]
    return json.dumps({
        "platform": "bilibili", "url": URL, "title": "四十分钟讲座",
        "durationSeconds": duration, "sourceKind": "up_subtitles", "language": "zh-CN",
        "trackCount": 1, "segments": segments,
        "charCount": sum(len(item["text"]) for item in segments),
        "warnings": [], "reason": None,
    }, ensure_ascii=False)


NO_CAPTURES = json.dumps({
    "platform": "bilibili", "url": URL, "title": "小视频", "durationSeconds": 104,
    "sourceKind": None, "language": "", "trackCount": 0, "segments": [], "charCount": 0,
    "warnings": ["Bilibili served an empty subtitle list to this session."],
    "reason": "no_subtitles_for_session",
}, ensure_ascii=False)


def run(text: str):
    """Return (ok, code, hint, segment_count) the way the CLI would see it."""
    try:
        result = to_transcript(URL, text)
    except Exception as exc:  # noqa: BLE001 - the taxonomy *is* the contract here
        return False, getattr(exc, "code", "error"), getattr(exc, "hint", ""), 0
    return True, "ok", "", len(result.transcript.segments)


@pytest.mark.parametrize("shape", ["", "   ", "{}", "[]", "[[]]", '{"segments": []}',
                                   '{"segments": [{"text": ""}]}'])
def test_a_relay_with_no_speech_is_refused_never_accepted(shape):
    ok, code, hint, count = run(shape)
    assert not ok, f"{shape!r} came back as a usable transcript ({count} segments)"
    assert code == "empty_transcript", (shape, code)
    assert count == 0
    assert hint, f"a refusal without a remedy is what an agent cannot act on: {shape!r}"


def test_a_live_no_captions_relay_names_the_login_remedy():
    ok, code, hint, count = run(NO_CAPTURES)
    assert not ok and code == "empty_transcript" and count == 0
    assert "登录" in hint, hint


def test_the_good_relay_still_works_as_text_and_as_a_decoded_list():
    ok, code, hint, count = run(good_relay())
    assert ok and count == 120, (code, hint, count)

    rows = json.loads(good_relay(count=40))["segments"]
    ok2, _, _, count2 = run(json.dumps(rows))
    assert ok2 and count2 == 40, "a bare cue array pasted inline is a relay too"

    from pickvedio_core.sources.relay import segments_from_payload
    assert len(segments_from_payload(rows)) == 40


def test_nothing_may_exit_zero_while_carrying_no_segments():
    """The invariant the 1.0.19 behaviour violated, stated as a gate."""
    for shape in ["", "[]", "{}", "[[]]", '{"segments": []}', NO_CAPTURES]:
        ok, _code, _hint, count = run(shape)
        assert not (ok and count == 0), f"{shape!r} accepted an empty transcript as success"
