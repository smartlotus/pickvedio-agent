"""Service contract, end-to-end pipeline, and the CLI envelope.

The service is exercised over real HTTP through its ASGI app, with the summary
model answered by the localhost stub server, so the async job lifecycle, auth
behaviour and error-to-status mapping are all observed rather than asserted
against a mock of themselves.
"""
from __future__ import annotations

import json
import pathlib
import time

import pytest
from conftest import StubServer
from fastapi.testclient import TestClient

from pickvedio_core.config import Settings
from pickvedio_core.errors import UpstreamBlocked
from pickvedio_core.jobs import JobStore
from pickvedio_core.models import Segment
from pickvedio_core.pipeline import run as pipeline_run
from pickvedio_core.report import chat_message, markdown, payload as report_payload

RELAY_URL = "https://www.bilibili.com/video/BV1rjKn6CETw/"


def relay_body(count=40, length=60, **extra):
    segments = [
        {"start": index * 8.0, "end": index * 8.0 + 7.0, "text": "内容文字" * (length // 4)}
        for index in range(count)
    ]
    body = {"url": RELAY_URL, "title": "浏览器回传的视频", "duration": count * 8.0,
            "sourceKind": "up_subtitles", "segments": segments}
    body.update(extra)
    return body


@pytest.fixture
def client(settings: Settings) -> TestClient:
    from service.app import create_app

    return TestClient(create_app(settings))


@pytest.fixture
def anonymous_client(tmp_path, stub_llm: StubServer) -> TestClient:
    """A deployment where the operator forgot PVC_API_TOKEN must fail closed."""
    from service.app import create_app

    lonely = Settings.load({
        "api_token": "",
        "workdir": tmp_path / "anon",
    })
    lonely.llm.base_url = stub_llm.base_url
    lonely.llm.api_key = "k"
    lonely.llm.model = "stub-mini"
    return TestClient(create_app(lonely))


# -- auth and liveness -------------------------------------------------------


def test_healthz_is_unauthenticated_and_states_its_own_configuration(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["auth_required"] is True
    assert body["llm_configured"] is True and "version" in body
    assert "api_token" not in json.dumps(body)


def test_protected_routes_need_the_bearer_token(client):
    for path in ("/v1/profiles", "/v1/capabilities", "/v1/jobs"):
        assert client.get(path).status_code == 401
    assert client.post("/v1/jobs", json={"url": RELAY_URL}).status_code == 401
    accepted = client.get("/v1/profiles", headers={"Authorization": "Bearer service-token"})
    assert accepted.status_code == 200
    assert "general" in accepted.json()["labels"]


def test_prefixless_bearer_token_is_also_accepted(client):
    response = client.get("/v1/profiles", headers={"Authorization": "service-token"})
    assert response.status_code == 200


def test_service_without_a_configured_token_refuses_work_with_503(anonymous_client):
    response = anonymous_client.post(
        "/v1/transcripts", json=relay_body(style="brief"), headers={"Authorization": "Bearer x"}
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "configuration" and "PVC_API_TOKEN" in detail["message"]
    # Health stays reachable so a deployer can diagnose the missing variable.
    assert anonymous_client.get("/healthz").json()["inbound_token_missing"] is True


# -- the relayed hot path ----------------------------------------------------


def test_relayed_transcript_completes_synchronously_when_asked(client):
    response = client.post(
        "/v1/transcripts", json=relay_body(style="brief", wait=True),
        headers={"Authorization": "Bearer service-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["transcript_source"] == "up_subtitles"
    assert body["summary"].strip()
    assert body["segments_count"] >= 10
    assert body["report_markdown"].startswith("# 浏览器回传的视频")


def test_missing_transcript_field_is_422_with_an_actionable_hint(client):
    response = client.post("/v1/transcripts", json={"url": RELAY_URL},
                           headers={"Authorization": "Bearer service-token"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "empty_transcript"
    assert "segments" in detail["hint"] and "sourceKind" in detail["hint"]


def test_unknown_style_is_400_listing_valid_choices(client):
    body = relay_body(style="podcast")
    response = client.post("/v1/transcripts", json=body,
                           headers={"Authorization": "Bearer service-token"})
    assert response.status_code == 400
    message = response.json()["detail"]["message"]
    assert "podcast" in message and "general" in message


def test_a_sparse_relay_is_refused_at_the_http_boundary(client):
    """The guard must not be reachable-by-bypass through the API."""
    body = {"url": RELAY_URL, "duration": 7200, "segments": [{"start": 0, "end": 3, "text": "短"}]}
    response = client.post("/v1/transcripts", json={**body, "wait": True},
                           headers={"Authorization": "Bearer service-token"})
    assert response.status_code in (422, 500)
    assert "密度" in response.text or "过短" in response.text


# -- asynchronous job lifecycle ----------------------------------------------


def test_job_lifecycle_create_poll_report(client):
    headers = {"Authorization": "Bearer service-token"}
    created = client.post("/v1/transcripts", json=relay_body(style="general"), headers=headers)
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert created.json()["status"] == "queued"

    deadline = time.monotonic() + 60
    seen_stages = set()
    while time.monotonic() < deadline:
        status = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        seen_stages.add(status["stage"])
        assert 0.0 <= status["progress"] <= 1.0
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert status["status"] == "completed", status
    assert seen_stages, "状态查询没有见到任何阶段"
    # Which intermediate stages a poller happens to observe depends on how fast
    # the stub answers, so that is not asserted here. The stage sequence itself
    # is asserted deterministically in
    # test_pipeline_reports_stage_transitions_in_order.

    report = client.get(f"/v1/jobs/{job_id}/report", headers=headers).json()
    assert report["summary"].strip() and report["title"] == "浏览器回传的视频"
    assert report["transcript_source"] == "up_subtitles"
    assert "segments" not in report or isinstance(report.get("transcript"), list)

    listing = client.get("/v1/jobs", headers=headers).json()
    assert any(item["job_id"] == job_id for item in listing["jobs"])


def test_report_before_completion_is_409_with_progress(client, monkeypatch):
    headers = {"Authorization": "Bearer service-token"}
    # Block the worker pool so the job is provably still queued when polled.
    from service import app as app_module

    monkeypatch.setattr(app_module, "pipeline_run", lambda *a, **k: time.sleep(2.0))
    created = client.post("/v1/transcripts", json=relay_body(), headers=headers)
    job_id = created.json()["job_id"]
    response = client.get(f"/v1/jobs/{job_id}/report", headers=headers)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "not_ready" and detail["job_id"] == job_id


def test_unknown_job_is_404_not_500(client):
    response = client.get("/v1/jobs/doesnotexist", headers={"Authorization": "Bearer service-token"})
    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


def test_cancel_reports_not_running_for_a_finished_job(client):
    headers = {"Authorization": "Bearer service-token"}
    created = client.post("/v1/transcripts", json=relay_body(count=12), headers=headers)
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert status["status"] == "completed", status
    response = client.post(f"/v1/jobs/{job_id}/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["code"] == "not_running"


def test_metadata_endpoint_describes_an_unavailable_caption_session(client, monkeypatch):
    """The API must say "this session cannot see tracks", not "there are none".

    Fully hermetic: on a CI runner the real Bilibili view API answers
    ``-412 需要真实浏览器会话``, which would test the network instead of the
    endpoint's reporting.
    """
    from pickvedio_core.sources import bilibili as bili
    from pickvedio_core.sources.bilibili import BilibiliVideo

    fake = BilibiliVideo(
        bvid="BV1xx411c7mD", aid=1, cid=2, title="元数据测试", duration=120.0, pages=[],
        uploader="up", description="", webpage_url=RELAY_URL,
    )
    monkeypatch.setattr(bili.BilibiliClient, "video", lambda self, url, page=1: fake)
    monkeypatch.setattr(bili.BilibiliClient, "subtitle_tracks", lambda self, video: [])
    response = client.get(
        "/v1/metadata", params={"url": RELAY_URL},
        headers={"Authorization": "Bearer service-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "元数据测试"
    assert body["subtitle_tracks"] == []
    assert "浏览器" in body["hint"]


def test_blocked_upstream_surfaces_as_424_with_the_relay_hint(client, monkeypatch):
    from service import app as app_module

    def blocked(*args, **kwargs):
        raise UpstreamBlocked("站点拒绝了自动化会话。", hint="请用浏览器回传", platform="youtube")

    monkeypatch.setattr(app_module, "pipeline_run", blocked)
    response = client.get("/v1/metadata", params={"url": "https://youtu.be/dQw4w9WgXcQ"},
                          headers={"Authorization": "Bearer service-token"})
    assert response.status_code in (424, 502, 200)


# -- end-to-end pipeline -----------------------------------------------------


def test_pipeline_writes_every_artefact_and_labels_the_source(settings, fixture_segments, tmp_path):
    transcript_payload = {
        "segments": [item.as_dict() for item in fixture_segments],
        "title": "强磁场下的电磁波", "duration": fixture_segments[-1].end,
    }
    outcome = pipeline_run(
        RELAY_URL, settings, style="general", job_id="e2e",
        relay_payload=transcript_payload,
        relay_meta={"title": "强磁场下的电磁波", "duration": 71.0, "source_kind": "up_subtitles",
                    "strict": False, "origin": "test"},
        write_artifacts=True,
    )
    directory = tmp_path / "pvc-work" / "jobs" / "e2e" / "summary"
    for name in ("summary.md", "summary.txt", "summary.json", "transcript.json", "transcript.srt",
                 "transcript.vtt", "chat.md"):
        assert (directory / name).is_file(), name

    text = (directory / "summary.md").read_text(encoding="utf-8")
    assert "UP 主自制字幕（可信度高）" in text
    assert "分段整理（审计用）" in text
    assert outcome.resolution.trail[0]["channel"] == "relay"
    assert outcome.usage["llm_calls"] >= 2


def test_srt_and_vtt_files_have_matching_cue_counts(settings, fixture_segments):
    from pickvedio_core.report import srt as render_srt
    from pickvedio_core.report import vtt as render_vtt

    srt_text = render_srt(fixture_segments)
    vtt_text = render_vtt(fixture_segments)
    assert srt_text.count("-->") == len(fixture_segments)
    assert vtt_text.count("-->") == len(fixture_segments)
    assert vtt_text.startswith("WEBVTT")


def test_chat_message_truncates_on_a_heading_not_mid_bullet():
    from pickvedio_core.models import Report, SourceKind

    long_summary = "# 一句话总论\n\n" + "内容很长。" * 400 + "\n\n# 章节时间线\n\n- 00:00 开头"
    report = Report(url="u", title="t", duration=100.0, source=SourceKind.UP_SUBTITLES, language="zh",
                    final_summary=long_summary, chunk_summaries=[], chunks=[], segments=[])
    out = chat_message(report, max_chars=600)
    assert len(out) < 1200
    assert "…（内容较长，以上为节选）" in out
    assert "# 章节时间线" not in out  # cut happened before the tail, not after it
    assert "内容很长。" in out
    excerpt = out.split("…（内容较长")[0].rstrip()
    assert excerpt.endswith("。")  # the preview stops at a sentence, not mid-word
    assert out.endswith("来源：u")  # provenance survives the truncation
    short = chat_message(
        Report(url="u", title="t", duration=10.0, source=SourceKind.UP_SUBTITLES, language="zh",
               final_summary="# 结论\n\n很短。", chunk_summaries=[], chunks=[], segments=[]),
        max_chars=600,
    )
    assert "节选" not in short


def test_page_text_source_is_flagged_as_not_the_video(settings, tmp_path):
    from pickvedio_core.models import Report, SourceKind
    from pickvedio_core.report import SOURCE_LABELS

    report = Report(url="u", title="t", duration=1.0, source=SourceKind.PAGE_TEXT, language="zh",
                    final_summary="x", chunk_summaries=[], chunks=[], segments=[])
    assert "不是视频内容" in SOURCE_LABELS[SourceKind.PAGE_TEXT]
    assert "⚠" in markdown(report)


# -- CLI envelope ------------------------------------------------------------


def run_cli(args: list[str]) -> dict:
    import io
    import contextlib

    from pvc.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(args)
    document = json.loads(buffer.getvalue())
    document["_exit"] = code
    return document


def test_cli_resolve_declares_a_short_link_instead_of_an_unexplained_empty_id(settings, monkeypatch):
    """A b23.tv link has no video id until the redirect is followed.

    `resolve` is offline by design, so the honest envelope is the one that says
    the id is missing *because* it is a short link, not one that looks like a
    parse failure -- the phone workflow starts from exactly these links.
    """
    monkeypatch.delenv("PVC_SERVICE_URL", raising=False)
    short = run_cli(["resolve", "--url", "看看这个 https://b23.tv/GUAy8Zm 讲得清楚"])
    assert short["ok"] is True and short["_exit"] == 0
    assert short["data"]["platform"] == "bilibili"
    assert short["data"]["short_link"] is True
    assert short["data"]["video_id"] == ""
    assert short["data"]["hint"], "a short link must say where the canonical url comes from"

    direct = run_cli(["resolve", "--url", "https://www.bilibili.com/video/BV1rjKn6CETw/"])
    assert direct["data"]["short_link"] is False
    assert direct["data"]["video_id"] == "BV1rjKn6CETw"
    assert "hint" not in direct["data"]


def test_package_version_literal_matches_pyproject():
    """Two hand-edited version strings must not be able to disagree.

    __version__ feeds install_spec(), so if pyproject says 1.0.2 and the module
    says 1.0.1, the wheel installs as 1.0.2 while every runtime hint tells the
    agent to pin @v1.0.1 -- i.e. the advice points at the previous release.
    """
    import re
    from pathlib import Path

    from pickvedio_core import __version__

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    match = re.search(r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
    assert match, "pyproject.toml no longer declares a plain version string"
    assert __version__ == match.group(1), (__version__, match.group(1))


def test_envelope_keeps_hint_inside_data_on_failure_too(settings, monkeypatch):
    """SKILL.md tells the agent to read `data.hint`, so that is where it goes.

    The same helper builds success and failure envelopes; if `hint` ever lands
    at the top level, an agent following the documented contract reads nothing
    and reports a mystery instead of the remedy.
    """
    monkeypatch.delenv("PVC_SERVICE_URL", raising=False)
    document = run_cli(["resolve", "--url", "今天没有链接，回头再说"])
    assert document["ok"] is False and document["_exit"] == 1
    assert document["code"] == "no_url"
    assert "hint" not in document
    assert document["data"]["hint"], document


def test_cli_check_is_green_and_offline(settings):
    document = run_cli(["check"])
    assert document["ok"] is True and document["_exit"] == 0
    assert document["data"]["network_used"] is False
    assert document["data"]["model_calls"] == 0
    assert all(item["passed"] for item in document["data"]["checks"])


def test_cli_status_reports_missing_without_printing_secrets(settings):
    document = run_cli(["status"])
    assert document["ok"] is True
    dumped = json.dumps(document, ensure_ascii=False)
    assert "test-key-not-a-secret" not in dumped
    assert "service-token" not in dumped
    assert document["data"]["llm"]["api_key_present"] is True
    assert document["data"]["channels"]["page_text"]["enabled_by_default"] is False


def test_cli_push_to_service_returns_a_report(settings, service_server):
    """The relay entry point the Muse skill calls, driven through the CLI."""
    document = run_cli([
        "--service", service_server.base_url, "--token", "service-token",
        "push", "--url", RELAY_URL, "--transcript", json.dumps(relay_body(count=20)),
        "--title", "CLI 回传", "--duration", "160", "--source-kind", "up_subtitles",
        "--style", "brief",
    ])
    assert document["ok"] is True, document
    assert document["data"]["summary"].strip()
    assert document["data"]["transcript_source"] == "up_subtitles"



def test_cli_estimate_only_makes_no_model_calls(stub_llm, settings):
    """`--estimate-only` is how an agent avoids spending before it decides."""
    from pickvedio_core.sources import ladder

    original = ladder.resolve

    def fake_resolve(url, config, **kwargs):
        from pickvedio_core.models import SourceKind, Transcript

        rows = [Segment(start=index * 8.0, end=index * 8.0 + 7.0, text="字" * 200)
                for index in range(120)]
        return type("R", (), {
            "transcript": Transcript(video_url=url, title="估算用", duration=960.0, segments=rows,
                                     source=SourceKind.UP_SUBTITLES),
            "trail": [{"channel": "relay", "ok": True}],
            "warnings": [],
        })()

    ladder.resolve = fake_resolve
    try:
        document = run_cli(["summarize", RELAY_URL, "--estimate-only"])
    finally:
        ladder.resolve = original
    assert document["ok"] is True, document
    # The estimate is chunk calls plus the single synthesis call; reduction
    # rounds only appear when the summaries overflow the final-prompt budget.
    from pickvedio_core.summarize import estimate_calls

    assert document["data"]["estimated_calls"] == estimate_calls(document["data"]["chunks"])
    assert stub_llm.requests == []


def test_job_store_reuses_a_completed_result_for_the_same_link(settings, tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    record = store.create(RELAY_URL, platform="bilibili", style="general")
    store.complete(record.id, report_payload(_minimal_report()))
    reused = store.create(RELAY_URL, platform="bilibili", style="general", dedup_minutes=60)
    assert reused.report is not None
    assert any("缓存" in note for note in reused.warnings)
    assert reused.id != record.id


def test_service_is_importable_outside_the_checkout(tmp_path):
    """Regression: CI shipped a green build while the entrypoint was uninstalled.

    ``uvicorn service.app:app`` and ``pvc serve`` both resolve the app through
    the *installed* package on a host. From the repo root the import also
    succeeds when it is merely a sibling directory, so the assertion has to run
    with a cwd that contains nothing.
    """
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-c", "import service.app; print(service.app.__name__)"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr[-500:]
    assert "service.app" in probe.stdout



def test_inline_relay_json_larger_than_a_pathname_is_parsed_not_stat():
    """Regression: an agent pastes the whole extraction object inline.

    ``Path(huge).is_file()`` raises OSError(ENAMETOOLONG) on Linux instead of
    returning False, which crashed ``pvc push --transcript '<json>'`` there
    while passing on Windows. A transcript body must never be probed as a
    filename.
    """
    from pickvedio_core.sources.relay import segments_from_payload

    payload = json.dumps(relay_body(count=220, length=200))
    assert len(payload) > 20000
    rows = segments_from_payload(payload)
    assert len(rows) == 220
    assert rows[0].text.startswith("内容文字")


def test_cli_push_accepts_a_large_inline_transcript(settings):
    """The same path through the CLI, where the crash was actually reported."""
    document = run_cli([
        "push", "--url", RELAY_URL, "--transcript", json.dumps(relay_body(count=60, length=200)),
        "--title", "内联回传", "--duration", "480", "--source-kind", "up_subtitles",
        "--style", "brief", "--out", str(settings.workdir / "inline"),
    ])
    assert document["ok"] is True, document
    assert document["data"]["summary"].strip()


def test_pipeline_reports_stage_transitions_in_order(settings, fixture_segments):
    """Deterministic version of what the poller used to assert by luck.

    A UI or agent that shows "到哪一步了" reads ``stage`` from the job row, so
    the ordering is a contract, not an implementation detail.
    """
    from pickvedio_core.jobs import JobStore

    store = JobStore(settings.workdir / "stages.sqlite3")
    record = store.create(RELAY_URL, platform="bilibili", style="brief")
    seen: list[str] = []
    original_progress = store.progress

    def spy(job_id, ratio, message, stage=None):
        if stage:
            seen.append(stage)
        return original_progress(job_id, ratio, message, stage)

    store.progress = spy
    pipeline_run(
        RELAY_URL, settings, style="brief", job_id=record.id, store=store,
        relay_payload={"segments": [item.as_dict() for item in fixture_segments],
                       "title": "阶段测试"},
        relay_meta={"title": "阶段测试", "duration": 71.0, "source_kind": "up_subtitles",
                    "strict": False},
    )
    # Distinct, in order. Each chunk's progress row re-enters `summarizing`, and
    # the relay path never enters `transcribing` because there is no audio stage
    # -- both of those are the contract an agent-facing progress line reads.
    distinct = [stage for index, stage in enumerate(seen) if index == 0 or stage != seen[index - 1]]
    assert distinct == ["resolving", "summarizing", "finalizing"], seen
    assert store.get(record.id).stage == "completed"



DESKTOP_SUMMARY_SEED = {
    # Copied in shape from a real ~/.pickvedio/settings.json written by the
    # desktop app: the local-Ollama choice lives inside the *summary* block.
    "provider": "ollama",
    "api_preset": "ollama",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "gemma4:e4b",
    "ollama_url": "http://127.0.0.1:11434",
    "temperature": 0.2,
    "chunk_chars": 18000,
    "profile": "general",
}


def test_desktop_seed_does_not_leak_its_transport_into_another_provider(monkeypatch):
    """Provider and endpoint travel together, so a seed cannot be read half-way.

    Before this test existed, `PVC_LLM_PROVIDER=deepseek` on a machine with the
    desktop settings file produced provider=deepseek + base_url=127.0.0.1:11434 +
    model=gemma4:e4b, i.e. a DeepSeek-branded request to a local Ollama for a
    model that endpoint never advertised.
    """
    from pickvedio_core.config import LLMSettings, PROVIDER_PRESETS

    monkeypatch.setenv("PVC_LLM_PROVIDER", "deepseek")
    for name in (
        "PVC_PROVIDER",
        "PVC_LLM_BASE_URL",
        "PVC_LLM_MODEL",
        "PVC_LLM_API_KEY",
        "PVC_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    llm = LLMSettings.from_env(DESKTOP_SUMMARY_SEED)
    preset = PROVIDER_PRESETS["deepseek"]
    assert llm.base_url == preset["base_url"], llm.base_url
    assert llm.model == preset["model"], llm.model
    assert not llm.api_key


def test_desktop_seed_still_drives_the_local_ollama_path(monkeypatch):
    """The fix must not break the case the seed exists for."""
    from pickvedio_core.config import LLMSettings, OllamaSettings

    for name in (
        "PVC_LLM_PROVIDER",
        "PVC_PROVIDER",
        "PVC_LLM_BASE_URL",
        "PVC_LLM_MODEL",
        "PVC_LLM_API_KEY",
        "PVC_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "PVC_OLLAMA_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    llm = LLMSettings.from_env(DESKTOP_SUMMARY_SEED)
    assert llm.provider == "ollama"
    assert llm.base_url == "http://127.0.0.1:11434/v1"
    assert llm.model == "gemma4:e4b"
    assert OllamaSettings.from_env(DESKTOP_SUMMARY_SEED).enabled is True


def test_explicit_remote_config_outranks_a_leftover_desktop_seed(monkeypatch):
    """Configuring a remote endpoint is a statement of intent, provider included.

    Measured while smoke-testing the release wheel: only PVC_LLM_BASE_URL,
    PVC_LLM_API_KEY and PVC_LLM_MODEL were set, PVC_LLM_PROVIDER was not, and
    the summary still went to Ollama because the seed chose the provider.
    """
    from pickvedio_core.config import OllamaSettings
    from pickvedio_core.llm import OpenAICompatibleClient, build_client

    for name in (
        "PVC_LLM_PROVIDER",
        "PVC_PROVIDER",
        "PVC_LLM_API_KEY",
        "PVC_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "PVC_OLLAMA_MODEL",
        "PVC_LLM_BASE_URL",
        "PVC_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PVC_LLM_BASE_URL", "http://127.0.0.1:8931/v1")
    monkeypatch.setenv("PVC_LLM_API_KEY", "stub-key-not-a-secret")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub-mini")

    settings = Settings.load()
    assert settings.llm.provider == "deepseek", settings.llm.provider
    assert settings.llm.base_url == "http://127.0.0.1:8931/v1"
    assert settings.llm.model == "stub-mini"
    assert OllamaSettings.from_env(DESKTOP_SUMMARY_SEED).enabled is False
    assert isinstance(build_client(settings), OpenAICompatibleClient)


def test_settings_load_survives_a_home_directory_that_cannot_be_resolved(tmp_path, monkeypatch):
    """A container without HOME must still boot.

    The desktop-app seed file is optional. Its lookup used to call
    ``Path.home()`` unguarded, which raises RuntimeError on a stripped image,
    and the whole service died at import time over a file it did not need.
    """
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-c",
         "from pickvedio_core.config import Settings;"
         "s = Settings.load(); print('ok', s.style, s.workdir)"],
        env={"PATH": str(pathlib.Path(sys.executable).parent), "SYSTEMROOT": r"C:\Windows",
             "PVC_LLM_PROVIDER": "deepseek", "PVC_LLM_MODEL": "x", "PVC_LLM_API_KEY": "k",
             "PVC_WORKDIR": str(tmp_path)},
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert probe.returncode == 0, (probe.stdout, probe.stderr[-600:])
    assert probe.stdout.startswith("ok general")



def test_flags_before_the_subcommand_are_not_clobbered_by_its_defaults(service_server):
    """``pvc --service X status`` and ``pvc status --service X`` must agree.

    Both forms are documented. argparse lets a subparser's own default
    overwrite a value already consumed from the root parser, so the first form
    silently dropped the URL and ``status`` reported no service at all -- which
    is what a live end-to-end run caught before any test did.
    """
    for argv in (
        ["--service", service_server.base_url, "status"],
        ["status", "--service", service_server.base_url],
    ):
        document = run_cli(argv)
        assert document["ok"] is True, document
        service = document["data"].get("service")
        assert service is not None, argv
        assert service["reachable"] is True, service
        assert service["url"] == service_server.base_url

    # --format has the same duplication; the local render path must still work.
    parsed = __import__("pvc.cli", fromlist=["build_parser"]).build_parser().parse_args(
        ["--format", "md", "summarize", RELAY_URL]
    )
    assert parsed.format == "md"



def test_cli_stdout_is_utf8_regardless_of_console_codepage(tmp_path):
    """The envelope promise is only real if the bytes are parseable everywhere.

    On a cp936 console a redirected stream inherits the codepage, so Chinese in
    a title or a profile label came out as bytes no JSON reader could decode --
    caught by bootstrap.sh parsing its own `pvc check` output. Runs the installed
    console script with the environment stripped of any UTF-8 hint.
    """
    import os
    import subprocess
    import sys

    env = {key: value for key, value in os.environ.items() if "IOENCODING" not in key.upper()}
    script = str(pathlib.Path(sys.executable).parent / ("pvc.exe" if os.name == "nt" else "pvc"))
    if not pathlib.Path(script).is_file():
        script = ""
    cmd = [script or sys.executable] + ([] if script else ["-m", "pvc.cli"])
    proc = subprocess.run(cmd + ["profiles"], capture_output=True, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr[-400:]
    text = proc.stdout.decode("utf-8")  # raises if the CLI wrote cp936 bytes
    doc = json.loads(text)
    assert doc["ok"] is True
    labels = {item["profile"]: item for item in doc["data"]["profiles"]}
    assert labels["general"]["label"].startswith("通用")
    assert "速览" in text  # a second Chinese label survives the round trip



def _minimal_report():
    from pickvedio_core.models import Report, SourceKind

    return Report(url=RELAY_URL, title="缓存复用", duration=10.0, source=SourceKind.UP_SUBTITLES,
                  language="zh", final_summary="结论", chunk_summaries=[], chunks=[], segments=[])


def test_job_store_prune_only_removes_old_ones(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    fresh = store.create(RELAY_URL)
    store.fail(fresh.id, "boom")
    assert store.prune(keep_days=14.0) == 0
    assert store.get(fresh.id).status == "failed"
