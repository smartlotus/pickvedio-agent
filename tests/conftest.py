"""Shared fixtures.

The LLM stub is a real HTTP server on localhost rather than a monkeypatched
function, because the thing worth testing is the wire path -- endpoint joining,
headers, ``usage`` parsing, the retry ladder, and reasoning-model fallbacks. A
patched client would test the test, not the client.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from fastapi import Request  # module scope: FastAPI resolves route annotations from __globals__

STUB_STATE: dict[str, object] = {
    "requests": [],
    "fail_next": 0,
    "fail_status": 503,
    "reply_mode": "normal",
}


def _reply(prompt: str) -> str:
    mode = STUB_STATE["reply_mode"]
    if mode == "expand":
        # A model that always writes more than it is fed: the reduction loop must
        # stop rather than spin, so this is a required shape to survive.
        return "填充" * 4000
    if mode == "reasoning_only":
        return ""
    if "分段摘要" in prompt or "chunk_summaries" in prompt:
        return "# 一句话总论\n\n这是桩模型给出的最终总结。\n\n# 章节时间线\n\n- [00:00 - 01:00] 开头"
    return "## 片段要点\n\n- 桩模型整理的一条要点\n- 第二条"


def _app():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        if STUB_STATE["fail_next"] > 0:
            STUB_STATE["fail_next"] -= 1
            return JSONResponse(
                {"error": {"message": "temporary upstream failure"}},
                status_code=int(STUB_STATE["fail_status"]),
            )
        body = await request.json()
        prompt = body["messages"][-1]["content"]
        STUB_STATE["requests"].append({"prompt": prompt, "model": body.get("model"),
                                       "max_tokens": body.get("max_tokens")})
        content = _reply(prompt)
        message = {"role": "assistant", "content": content}
        if STUB_STATE["reply_mode"] == "reasoning_only":
            message = {"role": "assistant", "content": "", "reasoning_content": "这是推理过程，不是答案。"}
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "model": body.get("model", "stub"),
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(prompt) // 3, "completion_tokens": 42, "total_tokens": 42},
        })

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        return JSONResponse({"object": "list",
                             "data": [{"id": "stub-large"}, {"id": "stub-mini"}]})

    return app


def free_port() -> int:
    """Ask the OS for a bindable port.

    Windows reserves whole dynamic ranges for Hyper-V, so a hard-coded "random
    high port" is not a test flake but a test failure: on this machine the range
    49352-49451 (and neighbours) cannot be bound at all, and several 5xxxx
    candidates are similarly excluded.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class StubServer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    @property
    def requests(self) -> list[dict]:
        return list(STUB_STATE["requests"])

    def reset(self, **kwargs) -> None:
        STUB_STATE["requests"] = []
        STUB_STATE["fail_next"] = 0
        STUB_STATE["fail_status"] = 503
        STUB_STATE["reply_mode"] = "normal"
        STUB_STATE.update(kwargs)

    def fail_then_succeed(self, times: int, status: int = 503) -> None:
        STUB_STATE["fail_next"] = times
        STUB_STATE["fail_status"] = status


class RunningServer:
    def __init__(self, base_url: str, server, thread) -> None:
        self.base_url = base_url
        self.port = int(base_url.rsplit(":", 1)[1])
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


def serve(app, *, ready_path: str = "/docs", name: str = "pvc-test-server", attempts: int = 6):
    """Start uvicorn on an OS-allocated port, retrying bind failures.

    The retry is load-bearing, not decoration: Windows reserves whole dynamic
    port ranges for Hyper-V and this machine intermittently answers
    WinError 10055 (no buffer space) on a fresh socket. A single fixed port
    would make every run look broken.
    """
    import httpx

    last: Exception | None = None
    for _ in range(attempts):
        port = free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name=name, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}{ready_path}", timeout=1.0).status_code == 200:
                    return RunningServer(f"http://127.0.0.1:{port}", server, thread)
            except Exception as exc:  # transient bind / backlog pressure
                last = exc
                time.sleep(0.2)
        server.should_exit = True
        thread.join(timeout=5)
    raise RuntimeError(f"测试服务起不来：{last}")


@pytest.fixture(scope="session")
def stub_llm() -> StubServer:
    running = serve(_app(), ready_path="/docs", name="stub-llm")
    stub = StubServer(running.base_url)
    yield stub
    running.stop()


@pytest.fixture
def service_server(settings):
    """A deployed service on a real socket, for the CLI's remote code path.

    ``test_cli_push_to_service`` needs the CLI to talk HTTP: injecting a fake
    transport would test the fake instead of the client.
    """
    from service.app import create_app

    running = serve(create_app(settings), ready_path="/healthz", name="pvc-service")
    yield running
    running.stop()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "pvc-work"
    path.mkdir()
    return path


@pytest.fixture
def settings(stub_llm: StubServer, workdir: Path, monkeypatch, tmp_path):
    from pickvedio_core.config import Settings

    monkeypatch.setenv("PVC_LLM_PROVIDER", "custom")
    monkeypatch.setenv("PVC_LLM_BASE_URL", stub_llm.base_url)
    monkeypatch.setenv("PVC_LLM_API_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("PVC_LLM_MODEL", "stub-mini")
    monkeypatch.setenv("PVC_WORKDIR", str(workdir))
    monkeypatch.setenv("PVC_API_TOKEN", "service-token")
    monkeypatch.delenv("PVC_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("PVC_ASR_PROVIDER", raising=False)
    # Point the optional desktop-app seed at a path that cannot exist, so the
    # developer's own ~/.pickvedio/settings.json (real model choices, real
    # edited prompts) never leaks into a test result.
    monkeypatch.setenv("PVC_CONFIG_FILE", str(tmp_path / "no-such-settings.json"))
    loaded = Settings.load()
    stub_llm.reset()
    return loaded


@pytest.fixture
def fixture_segments():
    from pickvedio_core.models import Segment

    return [
        Segment(start=0.0, end=6.0, text="大家好，今天我们讲强磁场下的电磁波衰减。"),
        Segment(start=6.0, end=14.0, text="先看第一个公式，麦克斯韦方程组在介质中的形式。"),
        Segment(start=14.0, end=22.0, text="这里的电导率 σ 决定了衰减的快慢。"),
        Segment(start=52.0, end=60.0, text="接下来是一个反例：超导体内部电场为零。"),
        Segment(start=60.0, end=71.0, text="所以结论是角动量守恒在这个近似下仍然成立。"),
    ]


@pytest.fixture
def big_segments():
    """A 3-hour-shaped transcript, the input size the reduction loop exists for."""
    from pickvedio_core.models import Segment

    rows = []
    for index in range(900):
        start = index * 12.0
        rows.append(
            Segment(start=start, end=start + 11.0, text=f"第 {index} 句内容：" + "内容文字" * 30)
        )
    return rows


def json_body(response) -> dict:
    return json.loads(response.text)
