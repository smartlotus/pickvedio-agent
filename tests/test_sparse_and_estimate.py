"""The two stop signals a phone user actually hits, and the pre-spend estimate.

Rehearsing the published wheel end to end showed:

* a 30-minute relay with three lines was refused with ``code:"error"`` and an
  empty hint -- indistinguishable, to a ladder that branches on ``code``, from
  an internal bug, even though the remedy ("extract after the cues loaded") is
  exactly what an agent cannot work out from a phone;
* ``--estimate-only`` existed only on ``summarize``, which must obtain a
  transcript through the ladder first -- on the browser-relay route that means
  it answers ``needs_browser`` and estimates nothing, while ``push`` (the command
  the route really uses) rejected the flag outright;
* the estimate said ``chunks + 2`` in prose while ``data`` carried a different
  ``estimate_calls``, so the number a user decides on depended on which field
  they read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pickvedio_core.errors import SparseTranscript
from pickvedio_core.sources.relay import to_transcript

URL = "https://www.bilibili.com/video/BV1pa5Ez5E2e/"


def relay(segments: list[dict], duration: int, kind: str = "up_subtitles") -> str:
    return json.dumps({
        "platform": "bilibili", "url": URL, "title": "三十分钟测试讲座",
        "durationSeconds": duration, "sourceKind": kind, "language": "zh-CN",
        "trackCount": 1, "segments": segments,
        "charCount": sum(len(item["text"]) for item in segments),
        "warnings": [], "reason": None,
    }, ensure_ascii=False)


def cues(count: int, size: int) -> list[dict]:
    return [{"start": float(i * 15), "end": float(i * 15 + 14),
             "text": "这句话是用来填满切块的字幕内容。" * size}
            for i in range(1, count + 1)]


def test_thin_text_for_a_long_video_is_a_named_code_with_a_remedy():
    """Density branch: enough lines, far too few characters for the length."""
    with pytest.raises(SparseTranscript) as caught:
        to_transcript(URL, relay(cues(20, 1), 1800))
    error = caught.value
    assert error.code == "sparse_transcript"
    assert error.http_status == 422
    assert "提取时机太早" in error.hint
    assert "page_text" in error.hint or "ASR" in error.hint


def test_three_lines_for_a_long_video_is_the_same_named_code():
    """Line-count branch: dense enough to pass density, still far too short."""
    with pytest.raises(SparseTranscript) as caught:
        to_transcript(URL, relay(cues(3, 12), 1800))
    assert "段" in str(caught.value)
    assert "重新提取" in caught.value.hint
    assert caught.value.code == "sparse_transcript"


def test_a_page_text_declaration_is_still_only_a_warning():
    """The opt-in escape hatch must not become a second hard failure."""
    result = to_transcript(URL, relay(cues(3, 12), 1800, kind="page_text"),
                           source_kind="page_text")
    assert len(result.transcript.segments) == 3
    assert any("过短" in item for item in result.warnings)


def _push_args(tmp_path, text: str, *extra: str):
    payload = tmp_path / "relay.json"
    payload.write_text(text, encoding="utf-8")
    argv = ["push", "--url", URL, "--transcript", str(payload), "--style", "brief", *extra]
    return argv


def test_push_estimate_only_refuses_to_spend_and_agrees_with_itself(monkeypatch, tmp_path, capsys):
    from pvc import cli

    def no_client(*args, **kwargs):
        raise AssertionError("an estimate must never build a model client")

    monkeypatch.setattr("pickvedio_core.pipeline.build_client", no_client)
    monkeypatch.setenv("PVC_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub")

    # 240 cues at a realistic ~256 characters per minute over an hour: enough
    # text that the plan is genuinely more than one chunk, so `chunks` cannot be
    # satisfied by a payload the real run would have put in a single call.
    rc = cli.main(_push_args(tmp_path, relay(cues(240, 4), 3600), "--estimate-only"))
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    data = document["data"]
    assert document["ok"] is True and data["chunks"] >= 2, data
    total = data["estimated_calls"]["total"]
    quoted = int(re.search(r"至少 (\d+) 次", document["message"]).group(1))
    assert quoted == total, document["message"]
    assert data["source"] == "up_subtitles"
    assert "未花费任何额度" in document["message"]


def test_push_estimate_only_applies_the_same_guard(tmp_path, monkeypatch, capsys):
    """An estimate may not promise a summary the real run would refuse."""
    from pvc import cli

    monkeypatch.setenv("PVC_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub")
    rc = cli.main(_push_args(tmp_path, relay(cues(20, 1), 1800), "--estimate-only"))
    assert rc == 1
    document = json.loads(capsys.readouterr().out)
    assert document["code"] == "sparse_transcript", document
    assert document["data"]["hint"], "the estimate must carry the same remedy"


def test_estimate_only_against_a_service_is_refused_before_any_post(tmp_path, monkeypatch, capsys):
    from pvc import cli

    monkeypatch.setenv("PVC_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub")
    calls = []
    monkeypatch.setattr(cli._Remote, "request",
                        lambda self, *a, **k: calls.append((a, k)) or {"job_id": "x"})
    rc = cli.main(_push_args(tmp_path, relay(cues(120, 4), 1800),
                             "--estimate-only", "--service", "http://127.0.0.1:9",
                             "--token", "t"))
    assert rc == 1
    document = json.loads(capsys.readouterr().out)
    assert document["code"] == "configuration", document
    assert calls == [], "an estimate must never reach the spending endpoint"


def test_the_sparse_code_is_documented_where_the_agent_reads_it():
    """A new code is only real once the ladder's own reference lists it."""
    root = Path(__file__).resolve().parents[1]
    for relative in ("skills/video-summary/SKILL.md",
                     "skills/video-summary/references/troubleshooting.md"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "sparse_transcript" in text, f"{relative} never mentions the code"
