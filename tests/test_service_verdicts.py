"""Every service reply must carry the two fields the ladder branches on.

Measured on the shipped 1.0.13 build: `POST /v1/transcripts` answered
`['job_id', 'message', 'relayed', 'status', 'style']` and `POST /v1/jobs`
answered `['job_id', 'message', 'status', 'style']` -- both with HTTP 200, both
without `ok`, both without `code`. Those are the two calls that start the work,
so the reply an agent most needed to branch on ("queued" versus a finished
reused report) was the only one in the API with nothing to branch on. The
documented contract is three top-level keys plus `data`.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pickvedio_core.config import Settings
from service.app import create_app

TOKEN = "v" * 24
URL = "https://www.bilibili.com/video/BV1rjKn6CETw/"


def _settings(tmp_path, stub_llm) -> Settings:
    settings = Settings.load()
    settings.api_token = TOKEN
    settings.workdir = tmp_path / "work"
    settings.llm.provider = "localai"
    settings.llm.base_url = stub_llm.base_url
    settings.llm.model = "stub-model"
    settings.llm.api_key = "sk-local-stub"
    return settings


@pytest.fixture()
def client(tmp_path, stub_llm, monkeypatch):
    monkeypatch.delenv("PVC_LLM_PROVIDER", raising=False)
    app = create_app(_settings(tmp_path, stub_llm))
    with TestClient(app) as test_client:
        yield test_client


def _missing_verdict(reply: dict) -> list[str]:
    return [key for key in ("ok", "code") if key not in reply]


def test_every_endpoint_reply_carries_a_verdict(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    replies = {
        "GET /healthz": client.get("/healthz").json(),
        "GET /v1/profiles": client.get("/v1/profiles", headers=headers).json(),
        "GET /v1/capabilities": client.get("/v1/capabilities", headers=headers).json(),
        "GET /v1/jobs": client.get("/v1/jobs", headers=headers).json(),
        "POST /v1/jobs": client.post("/v1/jobs", headers=headers,
                                     json={"url": URL, "style": "brief"}).json(),
        "POST /v1/transcripts": client.post("/v1/transcripts", headers=headers, json={
            "url": URL, "style": "brief",
            "segments": [{"start": float(i * 15), "end": float(i * 15 + 14),
                          "text": "这一句是本地桩模型的输入内容。" * 3} for i in range(1, 9)],
        }).json(),
    }
    # The two POSTs above created jobs; poll and cancel them so nothing is left
    # running against the stub, and their replies land in the same check.
    for key in ("POST /v1/jobs", "POST /v1/transcripts"):
        job_id = replies[key].get("job_id")
        assert job_id, (key, replies[key])
        replies[f"GET /v1/jobs/{job_id}"] = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        replies[f"POST /v1/jobs/{job_id}/cancel"] = client.post(
            f"/v1/jobs/{job_id}/cancel", headers=headers).json()

    missing = {name: _missing_verdict(body) for name, body in replies.items() if body}
    missing = {name: keys for name, keys in missing.items() if keys}
    assert not missing, f"replies without ok/code: {missing}"


def test_the_job_post_says_queued_and_the_relay_post_is_accepted(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    job = client.post("/v1/jobs", headers=headers, json={"url": URL, "style": "brief"}).json()
    assert job["ok"] is True and job["code"] == "queued", job
    assert job["status"] == "queued" and job["style"] == "brief", job
    relay = client.post("/v1/transcripts", headers=headers, json={
        "url": URL, "style": "brief", "wait": False,
        "segments": [{"start": float(i * 15), "end": float(i * 15 + 14),
                      "text": "这一句是本地桩模型的输入内容。" * 3} for i in range(1, 9)],
    }).json()
    assert relay["ok"] is True and relay["code"] == "queued", relay
    assert relay.get("relayed") is True, relay
    client.post(f"/v1/jobs/{relay['job_id']}/cancel", headers=headers)
    client.post(f"/v1/jobs/{job['job_id']}/cancel", headers=headers)


def test_the_verdict_check_cannot_pass_by_construction(client):
    """The classifier is the thing under test here, so plant the old shape."""
    shipped_1_0_13 = {"job_id": "abc", "status": "queued", "style": "brief", "message": "…"}
    assert _missing_verdict(shipped_1_0_13) == ["ok", "code"]
    assert _missing_verdict({"ok": True, "code": "queued"}) == []
    assert json.dumps(shipped_1_0_13).isascii() or True
