"""The throttle contract: what an agent is told when a provider says "slow down".

Two claims in the shipped instructions were not true of the code. The
troubleshooting table and the skill's own eval pack both tell the agent to
honour ``retry_after_seconds`` and to branch on ``code == rate_limited``, but
``llm.py`` fed a provider 429 into its generic retry ladder as an
``UpstreamModelError`` -- so a long video that met a throttle reported
``model_error``, the agent went looking for a model problem, and the field it
was told to honour existed nowhere in the package.

These tests pin the contract at all three layers: the transport classifies the
status and carries the number, the CLI puts both where the envelope spec says
(``data``, never the top level), and the eval pack's fixtures are shapes the
tool can actually print.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from pickvedio_core.config import LLMSettings
from pickvedio_core.errors import RateLimited, UpstreamModelError
from pickvedio_core.llm import OpenAICompatibleClient

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "skills" / "video-summary" / "eval" / "scenarios.yaml"
SAMPLE_URL = "https://www.bilibili.com/video/BV1rjKn6CETw/"


def _throttled_client(handler, monkeypatch):
    monkeypatch.setattr("pickvedio_core.llm.time.sleep", lambda *_: None)
    return OpenAICompatibleClient(
        LLMSettings(base_url="https://api.example.com/v1", api_key="sk-test", model="m"),
        transport=httpx.MockTransport(handler),
    )


def test_provider_429_is_a_throttle_and_keeps_the_wait_it_named(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, text="rate limit exceeded")

    client = _throttled_client(handler, monkeypatch)
    with pytest.raises(RateLimited) as caught:
        client.complete("会被限流的总结")
    assert caught.value.retry_after_seconds == 120
    assert "120" in caught.value.hint
    assert calls["n"] == 4, "the retry ladder still runs; only the final label changes"


def test_provider_429_without_a_retry_after_header_still_says_back_off(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    client = _throttled_client(handler, monkeypatch)
    with pytest.raises(RateLimited) as caught:
        client.complete("会被限流的总结")
    assert caught.value.retry_after_seconds is None
    assert caught.value.hint and "并发" in caught.value.hint


def test_a_non_numeric_retry_after_is_not_turned_into_a_number(monkeypatch):
    """The header may hold an HTTP-date; inventing seconds from it would be a lie."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                              text="rate limited")

    client = _throttled_client(handler, monkeypatch)
    with pytest.raises(RateLimited) as caught:
        client.complete("会被限流的总结")
    assert caught.value.retry_after_seconds is None


def test_a_provider_500_is_still_a_model_error(monkeypatch):
    """Reclassifying every retryable status would send agents down the wrong row."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    client = _throttled_client(handler, monkeypatch)
    with pytest.raises(UpstreamModelError) as caught:
        client.complete("服务自己坏了")
    assert not isinstance(caught.value, RateLimited)


def test_cli_puts_the_wait_and_the_hint_inside_data(monkeypatch, capsys, tmp_path):
    transcript = tmp_path / "relay.json"
    transcript.write_text(json.dumps({
        "sourceKind": "up_subtitles",
        "durationSeconds": 300,
        "segments": [{"start": idx * 15.0, "end": idx * 15.0 + 14.0,
                      "text": "这是一句足够长的中文字幕内容，用来通过稀疏门禁。" * 2}
                     for idx in range(1, 7)],
    }, ensure_ascii=False), encoding="utf-8")

    class Throttled:
        def complete(self, *args, **kwargs):
            raise RateLimited("总结服务限流（429）：slow down", retry_after_seconds=77)

        def close(self):  # pragma: no cover - only if the pipeline closes it
            pass

    monkeypatch.setattr("pickvedio_core.pipeline.build_client", lambda *a, **k: Throttled())
    from pvc import cli

    rc = cli.main(["push", "--url", SAMPLE_URL, "--transcript", str(transcript), "--style", "brief"])
    assert rc == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False and document["code"] == "rate_limited", document
    assert document["data"]["retry_after_seconds"] == 77, document
    assert "77" in document["data"]["hint"], document
    assert "hint" not in document, "hint belongs in data, never beside it"


def _spec_violation(parsed: dict) -> str:
    """Return why this envelope is not something ``pvc`` could print, or ""."""
    if not set(parsed) <= {"ok", "code", "message", "data"}:
        return f"unexpected top-level keys: {sorted(parsed)}"
    if parsed.get("ok") is False:
        if "hint" in parsed:
            return "hint sits at the top level, where the CLI never puts it"
        data = parsed.get("data") or {}
        if "retry_after_seconds" in data and not isinstance(data["retry_after_seconds"], int):
            return "retry_after_seconds must be a whole number of seconds"
        if "retry_after_seconds" in data and not data.get("hint"):
            return "a throttle that names a wait must also say what to do"
    return ""


def test_eval_pack_envelopes_are_shapes_the_cli_can_print():
    """Every fixture that looks like an envelope must be valid JSON and spec-shaped.

    One of them read ``{"ok":false,"code":"rate_limited","hint":"retry_after_seconds":120}``
    -- unparseable, naming a field that did not exist, and putting hint at the
    top level where the CLI never puts it. A grader written against a made-up
    shape teaches the agent to look for something that can never arrive.
    """
    document = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    envelopes: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            text = node.strip()
            if text.startswith("{") and '"ok"' in text:
                envelopes.append(text)

    walk(document)
    assert len(envelopes) >= 4, f"only {len(envelopes)} envelope fixtures; the pack moved"
    for text in envelopes:
        assert not _spec_violation(json.loads(text)), text

    # The classifier has to be able to go red: the two shapes the pack used to
    # carry, and a non-integer wait, are all rejected.
    planted = [
        '{"ok":false,"code":"rate_limited","hint":"retry_after_seconds":120}',
        '{"ok":false,"code":"needs_browser","hint":"用浏览器回传","data":{}}',
        '{"ok":false,"code":"rate_limited","data":{"retry_after_seconds":"soon"}}',
    ]
    rejected = 0
    for text in planted:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            rejected += 1
            continue
        if _spec_violation(parsed):
            rejected += 1
    assert rejected == len(planted), f"only {rejected} of {len(planted)} planted shapes were rejected"


def test_eval_pack_relay_reasons_execute_and_match_the_hint_map():
    """Each relay fixture is fed through the real code path, as text, not as prose.

    The empty-relay fixtures are the shape the browser extractor actually
    returns, so ``to_transcript`` must raise carrying the mapped hint -- and the
    control that grades the reply has to require following ``data.hint``, or the
    pack is still grading the pre-1.0.5 behaviour.
    """
    from pickvedio_core.errors import EmptyTranscript
    from pickvedio_core.sources.relay import RELAY_REASON_HINTS, to_transcript

    document = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    controls = list(document.get("negative_controls", [])) + list(document.get("scenarios", []))
    checked = 0
    for control in controls:
        fixture = ((control.get("simulated_state") or {}).get("browser_extract") or "").strip()
        if not fixture.startswith("{") or '"reason"' not in fixture:
            continue
        payload = json.loads(fixture)
        reason = payload.get("reason")
        assert "data.hint" in " ".join(control.get("required_behaviour") or []), control["id"]
        if reason is None:
            # A relay that returned *some* text but far too little for the length
            # is the other refusal the pack has to grade, and it must not be
            # confused with "nothing usable arrived".
            from pickvedio_core.errors import SparseTranscript

            with pytest.raises(SparseTranscript) as caught:
                to_transcript(SAMPLE_URL, fixture)
            assert caught.value.hint, control["id"]
            checked += 1
            continue
        with pytest.raises(EmptyTranscript) as caught:
            to_transcript(SAMPLE_URL, fixture)
        assert caught.value.hint == RELAY_REASON_HINTS[reason], control["id"]
        checked += 1
    assert checked >= 4, f"only {checked} relay fixtures executed; the pack lost its teeth"
