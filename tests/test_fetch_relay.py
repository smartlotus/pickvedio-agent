"""The keyless route: relay payload -> validated transcript, with no model at all.

A deployed agent VM normally carries no `PVC_LLM_API_KEY`, and on that VM the
browser relay is the only channel that works at all. Before this, the two
commands that could read a relay payload (`push`, and `summarize` through a
service) both *summarise*, so an agent with no key had one route left: open
relay.json and write a summary straight from whatever it found. Nothing gated
that read, so the payload a summarising path would have refused -- zero tracks,
or three cues for a forty-minute video -- became a confident paragraph instead
of a named stop signal.

`fetch --transcript` closes that hole by running the same `to_transcript` guard
and spending nothing. These tests are the proof that it really spends nothing:
they make building a model client or running the ladder an assertion failure,
rather than merely hoping no call is made.
"""
from __future__ import annotations

import json

import pytest

from pickvedio_core.errors import SparseTranscript
from pickvedio_core.sources.relay import to_transcript

URL = "https://www.bilibili.com/video/BV1pa5Ez5E2e/"


def relay(segments: list[dict], duration: int, kind: str = "up_subtitles") -> str:
    return json.dumps({
        "platform": "bilibili", "url": URL, "title": "四十分钟实测讲座",
        "durationSeconds": duration, "sourceKind": kind, "language": "zh-CN",
        "trackCount": 1, "segments": segments,
        "charCount": sum(len(item["text"]) for item in segments),
        "warnings": [], "reason": None,
    }, ensure_ascii=False)


def cues(count: int, size: int) -> list[dict]:
    return [{"start": float(i * 15), "end": float(i * 15 + 14),
             "text": "这句话是用来填满切块的字幕内容。" * size}
            for i in range(1, count + 1)]


def _fetch_args(tmp_path, text: str, *extra: str) -> list[str]:
    payload = tmp_path / "relay.json"
    payload.write_text(text, encoding="utf-8")
    return ["fetch", "--url", URL, "--transcript", str(payload), *extra]


@pytest.fixture()
def nothing_spends(monkeypatch, tmp_path):
    """Any model client or network ladder is a test failure, not a no-op."""
    from pickvedio_core.sources import ladder

    def no_client(*args, **kwargs):
        raise AssertionError("fetch --transcript must never build a model client")

    def no_resolve(*args, **kwargs):
        raise AssertionError("fetch --transcript must not run the source ladder")

    monkeypatch.setattr("pickvedio_core.pipeline.build_client", no_client)
    monkeypatch.setattr(ladder, "resolve", no_resolve)
    monkeypatch.setenv("PVC_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("PVC_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub")


def test_relay_fetch_returns_segments_and_calls_nothing(nothing_spends, tmp_path, capsys):
    from pvc import cli

    rc = cli.main(_fetch_args(tmp_path, relay(cues(120, 4), 2400)))
    assert rc == 0, capsys.readouterr().out
    document = json.loads(capsys.readouterr().out)
    data = document["data"]
    assert document["ok"] is True and data["segments"], document
    assert len(data["segments"]) == 120
    assert data["source"] == "up_subtitles"
    assert data["title"] == "四十分钟实测讲座"
    assert data["duration"] == 2400
    assert "未调用模型" in document["message"]


def test_out_writes_exactly_the_segments_the_envelope_shows(nothing_spends, tmp_path, capsys):
    from pvc import cli

    out = tmp_path / "docs" / "segments.json"
    rc = cli.main(_fetch_args(tmp_path, relay(cues(120, 4), 2400), "--out", str(out)))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert json.loads(out.read_text(encoding="utf-8")) == data["segments"]
    assert data["out"] == str(out)


def test_a_relay_too_thin_to_summarise_is_refused_before_an_agent_reads_it(tmp_path, monkeypatch, capsys):
    """The whole point of the route: the guard fires on the path that has no guard."""
    from pvc import cli

    monkeypatch.setenv("PVC_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub")
    rc = cli.main(_fetch_args(tmp_path, relay(cues(20, 1), 1800)))
    assert rc == 1, "a thin relay must not exit 0 on the read-only route"
    document = json.loads(capsys.readouterr().out)
    assert document["code"] == "sparse_transcript", document
    assert "hint" in document["data"] and document["data"]["hint"]
    assert "segments" not in document["data"], "a refused relay must not hand over text to summarise"


def test_the_declared_escape_hatch_labels_what_it_let_through(nothing_spends, tmp_path, capsys):
    """--allow-sparse must still say the transcript is thin, not quietly comply."""
    from pvc import cli

    rc = cli.main(_fetch_args(tmp_path, relay(cues(20, 1), 1800, kind="page_text"),
                              "--source-kind", "page_text", "--allow-sparse"))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["source"] == "page_text"
    # The refusal is what relaxes; the labelling never does. Checked against the
    # sentence relay.py actually appends on this branch ("文字密度异常"), not a
    # paraphrase of it.
    assert any("密度" in item for item in data["warnings"]), data


def test_asked_for_a_service_it_refuses_instead_of_submitting_a_job(tmp_path, monkeypatch, capsys):
    """`--service` means `POST /v1/transcripts`, which starts spending on contact."""
    from pvc import cli

    def no_request(self, *args, **kwargs):
        raise AssertionError("--service must be refused before any request is made")

    monkeypatch.setattr(cli._Remote, "request", no_request)
    monkeypatch.setenv("PVC_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PVC_API_TOKEN", "t" * 24)
    rc = cli.main(_fetch_args(tmp_path, relay(cues(120, 4), 2400)))
    assert rc == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False and document["code"] == "configuration"
    assert "push" in document["data"]["hint"]


def test_the_sparse_error_carries_a_remedy_an_agent_can_act_on():
    """Negative control on this file: the guard is the library's, not a copy."""
    with pytest.raises(SparseTranscript) as caught:
        to_transcript(URL, relay(cues(20, 1), 1800))
    assert caught.value.code == "sparse_transcript"
