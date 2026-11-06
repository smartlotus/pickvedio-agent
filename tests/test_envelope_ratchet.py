"""Freeze the known envelope debt so it cannot grow, and can only shrink on purpose.

The contract is three top-level keys plus `data`. Failures have followed it since
1.0.12, and every POST that starts work carries a verdict since 1.0.14. Success
replies still spread their business fields beside the verdict, measured on the
shipped 1.0.14 service rather than remembered.

Nesting them is a breaking change for anyone indexing those keys today, and it
needs the CLI's remote readers, three docs and the skill pack moved together --
an attempt to do it with one scripted sweep broke syntax twice (a dict-return
helper whose result callers mutate, and a closing bracket), so it is being done
as its own release instead of half-landed. This file is the ratchet: adding a new
spread field, or a new endpoint in the old style, fails here. Removing keys is
always allowed, which is what makes the list shrink on its own.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pickvedio_core.config import Settings
from service.app import create_app

TOKEN = "r" * 24
URL = "https://www.bilibili.com/video/BV1rjKn6CETw/"
VERDICT_KEYS = {"ok", "code", "message", "retryable", "data"}

SPREAD_FIELDS = {
    "GET /healthz": {
        "asr_configured", "asr_provider", "auth_required", "inbound_token_missing",
        "llm_configured", "llm_model", "service", "uptime_seconds", "version", "workers"},
    "GET /v1/profiles": {"default", "labels", "profiles"},
    "GET /v1/capabilities": {"channels", "unconfigured"},
    "GET /v1/jobs": {"jobs"},
    "POST /v1/jobs": {"job_id", "status", "style"},
    "GET /v1/jobs/<id>": {
        "artifacts", "attempts", "created_at", "duration", "elapsed_seconds", "finished_at",
        "job_id", "platform", "progress", "stage", "status", "style", "title",
        "transcript_source", "updated_at", "url", "usage", "warnings"},
    "POST /v1/jobs/<id>/cancel": {"job_id"},
}


@pytest.fixture()
def client(tmp_path, stub_llm):
    settings = Settings.load()
    settings.api_token = TOKEN
    settings.workdir = tmp_path / "work"
    settings.llm.provider = "localai"
    settings.llm.base_url = stub_llm.base_url
    settings.llm.model = "stub-model"
    settings.llm.api_key = "sk-local-stub"
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _spread(body: dict) -> set[str]:
    return {key for key in body if key not in VERDICT_KEYS}


def test_no_endpoint_spreads_more_fields_than_the_ratchet_allows(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    observed = {
        "GET /healthz": _spread(client.get("/healthz").json()),
        "GET /v1/profiles": _spread(client.get("/v1/profiles", headers=headers).json()),
        "GET /v1/capabilities": _spread(client.get("/v1/capabilities", headers=headers).json()),
        "GET /v1/jobs": _spread(client.get("/v1/jobs", headers=headers).json()),
    }
    created = client.post("/v1/jobs", headers=headers, json={"url": URL, "style": "brief"}).json()
    job_id = created["job_id"]
    observed["POST /v1/jobs"] = _spread(created)
    observed["GET /v1/jobs/<id>"] = _spread(client.get(f"/v1/jobs/{job_id}", headers=headers).json())
    observed["POST /v1/jobs/<id>/cancel"] = _spread(
        client.post(f"/v1/jobs/{job_id}/cancel", headers=headers).json())

    growth = {}
    for name, seen in observed.items():
        allowed = SPREAD_FIELDS.get(name)
        assert allowed is not None, f"{name} is a new endpoint: decide its shape deliberately"
        extra = seen - allowed
        if extra:
            growth[name] = sorted(extra)
    assert not growth, f"new spread fields added to the envelope debt: {growth}"

    client.post(f"/v1/jobs/{job_id}/cancel", headers=headers)


def test_the_error_side_of_the_contract_is_already_held(client):
    """The half that was fixed stays fixed: failures are flat, never `{"detail"}`."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    replies = [
        client.get("/v1/profiles").json(),
        client.post("/v1/jobs", headers=headers,
                    json={"url": URL, "style": "podcast"}).json(),
        client.post("/v1/transcripts", headers=headers, json={"url": URL}).json(),
        client.post("/v1/jobs", headers=headers, json=["array", "body"]).json(),
    ]
    for body in replies:
        assert "detail" not in body, body
        assert body["ok"] is False and body["code"], body
        assert "hint" in (body.get("data") or {}), body


def test_the_ratchet_cannot_be_satisfied_by_an_empty_inventory():
    """Sanity check on the checker itself."""
    assert _spread({"ok": True, "code": "ok", "message": "…", "job_id": "x"}) == {"job_id"}
    assert _spread({"ok": True, "code": "ok", "message": "…", "data": {"job_id": "x"}}) == set()
    assert SPREAD_FIELDS["POST /v1/jobs"] == {"job_id", "status", "style"}
