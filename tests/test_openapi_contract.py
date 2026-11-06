"""The OpenAPI document is an API contract for a machine, not a README.

Muse can build a connector from `/openapi.json`, and both the runbook and the
Railway prompt tell it to fetch that document. So whatever the spec fails to say
is a feature the agent cannot use: measured here, the document advertised
`securitySchemes: null` and `security: false` on every operation while `/v1/*`
answered 401, and the two POST bodies had no example at all. A generated client
would have shipped without the token header and with a guessed body.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from service.app import create_app
from pickvedio_core.config import Settings
from pickvedio_core.prompts import PROFILE_LABELS
from pickvedio_core.urls import validate_video_url

TOKEN = "t" * 24


def _settings(tmp_path, stub_llm, monkeypatch) -> Settings:
    # Configure through the environment, which is the documented precedence
    # lever: Settings.load() alone lets a leftover desktop-app seed pick
    # `provider=ollama` (this box runs no Ollama), and mutating afterwards
    # cannot undo a transport chosen during loading. Ledger section 5f.
    monkeypatch.setenv("PVC_LLM_BASE_URL", stub_llm.base_url)
    monkeypatch.setenv("PVC_LLM_MODEL", "stub-model")
    monkeypatch.delenv("PVC_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("PVC_PROVIDER", raising=False)
    monkeypatch.delenv("PVC_OLLAMA_URL", raising=False)
    settings = Settings.load()
    # `build_client` hands the request to local Ollama whenever no API key is
    # set, and this box carries a desktop-app seed that enables it. The stub
    # speaks the OpenAI-compatible protocol, so state the key the way a real
    # deployment would instead of letting a leftover file pick the transport.
    settings.llm.api_key = "sk-for-the-local-stub"
    settings.api_token = TOKEN
    settings.workdir = tmp_path / "work"
    return settings


@pytest.fixture()
def client(tmp_path, stub_llm, monkeypatch):
    app = create_app(_settings(tmp_path, stub_llm, monkeypatch))
    with TestClient(app) as test_client:
        yield test_client


def _contract_gaps(spec: dict) -> list[str]:
    """Every way this document fails an agent building a client from it."""
    gaps: list[str] = []
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    protected = any(path.startswith("/v1/") for path in (spec.get("paths") or {}))
    bearer = [name for name, value in schemes.items()
              if value.get("type") == "http" and value.get("scheme") == "bearer"]
    if not bearer and protected:
        gaps.append("no bearer security scheme, although /v1/* demands a token")
    named = bearer[0] if bearer else "BearerAuth"
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if not operation.get("operationId"):
                gaps.append(f"{method.upper()} {path}: no operationId")
            elif operation["operationId"].endswith(("_get", "_post")):
                gaps.append(f"{method.upper()} {path}: auto-generated operationId "
                            f"{operation['operationId']!r}")
            secured = path.startswith("/v1/")
            has = any(named in entry for entry in (operation.get("security") or []))
            if secured and not has:
                gaps.append(f"{method.upper()} {path}: /v1 route without a security entry")
            if not secured and has:
                gaps.append(f"{method.upper()} {path}: public route marked secure")
            content = ((operation.get("requestBody") or {}).get("content") or {}).get("application/json")
            if content is not None and not (content.get("examples") or content.get("example")):
                gaps.append(f"{method.upper()} {path}: request body with no example")
    return gaps


def test_openapi_contract_has_no_gaps(client):
    spec = client.get("/openapi.json").json()
    assert _contract_gaps(spec) == [], _contract_gaps(spec)


def test_the_contract_checker_catches_the_document_this_repo_shipped(client):
    """A gate that cannot go red is not a gate.

    The planted spec is the shape this repository actually published until
    today: no security scheme, per-operation security missing, auto ids, and
    bodies without examples.
    """
    planted = {
        "paths": {
            "/healthz": {"get": {"operationId": "healthz_healthz_get"}},
            "/v1/profiles": {"get": {"operationId": "profiles_v1_profiles_get"}},
            "/v1/jobs": {"post": {"operationId": "create_job_v1_jobs_post",
                                  "requestBody": {"content": {"application/json": {"schema": {}}}}}},
        }
    }
    gaps = _contract_gaps(planted)
    kinds = {
        "no scheme": any("security scheme" in gap for gap in gaps),
        "missing security entry": any("without a security entry" in gap for gap in gaps),
        "auto-generated ids": sum(1 for gap in gaps if "auto-generated operationId" in gap),
        "no example": any("no example" in gap for gap in gaps),
    }
    assert kinds["no scheme"] and kinds["missing security entry"] and kinds["no example"], gaps
    assert kinds["auto-generated ids"] == 3, gaps
    assert len(gaps) >= 6, gaps
    assert _contract_gaps({"paths": {}}) == []


def test_bearer_scheme_is_declared_and_applied(client):
    spec = client.get("/openapi.json").json()
    scheme = spec["components"]["securitySchemes"]["BearerAuth"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    assert "PVC_API_TOKEN" in scheme["description"]
    assert any("503" in scheme["description"] for _ in [0])
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            secured = any("BearerAuth" in entry for entry in (operation.get("security") or []))
            assert secured is path.startswith("/v1/"), f"{method.upper()} {path}"


def test_healthz_stays_unauthenticated_but_v1_does_not(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/profiles").status_code == 401
    assert client.get("/v1/profiles", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_the_transcript_examples_are_bodies_the_api_accepts(client):
    """Both posted verbatim: a decorative example is a defect.

    No platform access is involved -- the transcript is in the body -- so this is
    an honest end-to-end proof that the shape documented in the spec is the shape
    the handler takes. Note the bodies are sent *unchanged*; rewriting them here
    would only prove the test's own body works.
    """
    spec = client.get("/openapi.json").json()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    content = spec["paths"]["/v1/transcripts"]["post"]["requestBody"]["content"]["application/json"]
    assert sorted(content["examples"]) == sorted(["浏览器回传的 segments", "SRT/VTT 原文"])
    for name, entry in content["examples"].items():
        response = client.post("/v1/transcripts", json=entry["value"], headers=headers)
        assert response.status_code == 200, f"{name}: {response.text}"
        body = response.json()
        # `wait: true` returns the report inline; the other shape gives a job id.
        assert body.get("job_id") or body.get("report_markdown"), f"{name}: {body}"


def test_the_job_examples_advertise_values_the_service_accepts(client):
    """Not posted: those bodies make the service fetch from the platform itself.

    The transcripts case above proves the handler accepts relay bodies. For the
    URL-shaped jobs, what is provable without touching a website is that every
    value the example advertises really parses -- a wrong URL or a profile name
    nobody has would otherwise surface only as a 4xx in the user's session.
    """
    spec = client.get("/openapi.json").json()
    content = spec["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]
    assert sorted(content["examples"]) == sorted(["最小任务", "带去重窗口"])
    for name, entry in content["examples"].items():
        body = entry["value"]
        assert validate_video_url(body["url"]) == body["url"], name
        assert body["style"] in PROFILE_LABELS, name
        if "dedup_minutes" in body:
            assert isinstance(body["dedup_minutes"], int), name


def test_operation_ids_are_named_for_machines_not_for_fastapi(client):
    spec = client.get("/openapi.json").json()
    ids = {operation["operationId"]
           for item in spec["paths"].values() for operation in item.values()}
    assert {"health_check", "list_profiles", "get_capabilities", "get_metadata",
            "create_job", "list_jobs", "job_status", "job_report", "cancel_job",
            "submit_transcript"} == ids
    assert json.dumps(sorted(ids)).isascii()
