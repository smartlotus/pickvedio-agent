"""LLM transports: OpenAI-compatible chat completions and local Ollama.

Ported from ``pickvedio/services.py`` with three changes that matter in a hosted
deployment:

* every call reports token usage, so a job can say what it cost;
* 429/5xx are retried with backoff instead of failing the video, and the retry
  budget is bounded so a stuck provider cannot hold a worker forever;
* the Ollama path no longer references an undefined ``5xx_placeholder`` name.
  In the desktop build that raised ``NameError`` and hid the real "not enough
  RAM" advice exactly when a user needed it.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import LLMSettings, OllamaSettings
from .errors import ConfigurationError, RateLimited, UpstreamModelError

logger = logging.getLogger("pickvedio.llm")

SYSTEM_PROMPT = "你是一名忠实、严谨的中文内容整理助手。"

# A summarisation pass has already paid for the expensive transcription, so one
# unlucky 5xx should not lose the whole job. Four attempts, ~45s worst case.
_RETRY_DELAYS = (2.0, 6.0, 18.0)
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class Usage:
    """Accumulated cost of a job, surfaced verbatim in the CLI/HTTP envelope."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    retried: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, prompt: int, completion: int, seconds: float, retries: int = 0) -> None:
        self.calls += 1
        self.prompt_tokens += int(prompt or 0)
        self.completion_tokens += int(completion or 0)
        self.seconds += seconds
        self.retried += retries

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_seconds": round(self.seconds, 2),
            "llm_retries": self.retried,
            "llm_errors": list(self.errors),
        }


class OpenAICompatibleClient:
    """Chat completions against any OpenAI-shaped provider.

    ``base_url`` accepts either the provider root (``https://api.deepseek.com``,
    ``.../v1``) or a full ``/chat/completions`` endpoint; both are normalised so
    a copied base URL never produces a 404.
    """

    def __init__(
        self,
        settings: LLMSettings,
        usage: Usage | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.usage = usage if usage is not None else Usage()
        self._transport = transport
        self.last_response: dict[str, Any] = {}

    @property
    def endpoint(self) -> str:
        base = self.settings.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        # DeepSeek publishes both `https://api.deepseek.com` and `.../v1`; the
        # bare root has no `/v1` suffix, so append it unless already present.
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def headers(self) -> dict[str, str]:
        out = {"Content-Type": "application/json"}
        if self.settings.api_key:
            out["Authorization"] = f"Bearer {self.settings.api_key}"
        return out

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str = SYSTEM_PROMPT,
        temperature: float | None = None,
    ) -> str:
        self.settings.require()
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": (
                self.settings.temperature if temperature is None else temperature
            ),
            "max_tokens": max_tokens or self.settings.max_tokens,
            "stream": False,
        }
        return self._post(payload)

    def _post(self, payload: dict[str, Any]) -> str:
        started = time.monotonic()
        last: Exception | None = None
        attempts = 0
        for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS):
            attempts = attempt
            if delay:
                time.sleep(delay + random.uniform(0, delay * 0.25))
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(self.settings.request_timeout, connect=20.0),
                    transport=self._transport,
                ) as client:
                    response = client.post(self.endpoint, headers=self.headers(), json=payload)
                    if response.status_code in _RETRY_STATUS:
                        if response.status_code == 429:
                            # A throttle is a different answer from a broken
                            # model: the retry ladder is the same, but what the
                            # agent is told afterwards is not. The docs branch
                            # on `rate_limited` and honour `retry_after_seconds`,
                            # which used to be reported as neither.
                            claimed = (response.headers.get("Retry-After") or "").strip()
                            last = RateLimited(
                                f"总结服务限流（429）：{response.text[:220]}",
                                retry_after_seconds=int(claimed) if claimed.isdigit() else None,
                            )
                        else:
                            last = UpstreamModelError(
                                f"总结服务返回 {response.status_code}：{response.text[:220]}"
                            )
                        logger.warning("llm retry %s status=%s", attempt + 1, response.status_code)
                        continue
                    response.raise_for_status()
                    data = response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    claimed = (exc.response.headers.get("Retry-After") or "").strip()
                    raise RateLimited(
                        f"总结服务限流（429）：{exc.response.text[:200]}",
                        retry_after_seconds=int(claimed) if claimed.isdigit() else None,
                    ) from exc
                raise UpstreamModelError(
                    f"总结服务返回 {exc.response.status_code}：{exc.response.text[:300]}"
                ) from exc
            except httpx.HTTPError as exc:
                last = UpstreamModelError(f"无法连接总结服务：{exc}")
                logger.warning("llm transport error %s: %s", attempt + 1, exc)
                continue
            elapsed = time.monotonic() - started
            content = self._extract(data)
            self.last_response = data
            usage = data.get("usage") or {}
            self.usage.add(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), elapsed, attempts
            )
            return content
        assert last is not None
        self.usage.errors.append(str(last)[:200])
        raise last

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise UpstreamModelError("总结服务返回的结构不符合 OpenAI 兼容接口。") from exc
        # Reasoning models (DeepSeek-R1, QwQ) put the answer in a separate field
        # and can return an empty `content` while still finishing normally.
        if (not isinstance(content, str) or not content.strip()) and isinstance(message, dict):
            for key in ("reasoning_content", "reasoning", "text"):
                fallback = message.get(key)
                if isinstance(fallback, str) and fallback.strip():
                    logger.info("llm used fallback field %s", key)
                    return fallback.strip()
        if not isinstance(content, str) or not content.strip():
            raise UpstreamModelError("总结服务没有返回可用文本。")
        return content.strip()

    def test_connection(self) -> str:
        return self.complete("只回复：连接成功。", max_tokens=24)

    def list_models(self) -> list[str]:
        self.settings.require()
        base = self.settings.base_url.rstrip("/")
        root = base[: -len("/chat/completions")] if base.endswith("/chat/completions") else base
        url = root if root.endswith("/models") else f"{root.rstrip('/')}/models"
        if not url.endswith("/models"):
            url = f"{url}/models"
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=8.0), transport=self._transport
            ) as client:
                response = client.get(url, headers=self.headers())
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise UpstreamModelError(f"无法获取模型列表：{exc}") from exc
        values = [
            item.get("id", "")
            for item in (data.get("data") or data.get("models") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return sorted({value for value in values if isinstance(value, str) and value.strip()}, key=str.lower)


class OllamaLocalClient:
    """Local Ollama chat. Same interface, no cloud key.

    Kept because the desktop app's whole selling point was "逐字稿不出本机"; the
    same guarantee is useful for a self-hosted deployment on the user's own box.
    """

    _RETRY_DELAYS = (5.0, 20.0)

    def __init__(
        self,
        settings: OllamaSettings,
        temperature: float = 0.2,
        usage: Usage | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.temperature = temperature
        self.usage = usage if usage is not None else Usage()
        self._transport = transport

    @property
    def base(self) -> str:
        return self.settings.url.rstrip("/")

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
                response = client.get(f"{self.base}/api/tags")
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise ConfigurationError(
                "无法连接 Ollama。请确认服务已启动（通常为 http://127.0.0.1:11434）。"
            ) from exc
        names = [
            item.get("name", "")
            for item in (data.get("models") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        return sorted(n for n in names if n)

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1600,
        system: str = SYSTEM_PROMPT,
        temperature: float | None = None,
    ) -> str:
        if not self.settings.model.strip():
            raise ConfigurationError("请先设置 PVC_OLLAMA_MODEL（可用 `pvc models --ollama` 查看列表）。")
        payload = {
            "model": self.settings.model,
            "stream": False,
            # Gemma-class models can spend the entire budget inside
            # `message.thinking`, leaving `content` empty and making a normal
            # completion look like a failure. Summarising needs the answer.
            "think": bool(self.settings.think),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_predict": max_tokens,
            },
        }
        started = time.monotonic()
        last_error: Exception | None = None
        attempts = 0
        for attempt, delay in enumerate((0.0,) + self._RETRY_DELAYS):
            attempts = attempt
            if delay:
                time.sleep(delay)
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(600.0, connect=8.0), transport=self._transport
                ) as client:
                    response = client.post(f"{self.base}/api/chat", json=payload)
                    if response.status_code == 400 and "think" in response.text.lower():
                        # Older Ollama builds reject an unknown switch; retry once
                        # without it rather than failing the job.
                        legacy = dict(payload)
                        legacy.pop("think", None)
                        response = client.post(f"{self.base}/api/chat", json=legacy)
                    if response.status_code >= 500:
                        last_error = UpstreamModelError(
                            f"Ollama 返回 {response.status_code}：{response.text[:200]}"
                        )
                        continue
                    response.raise_for_status()
                    data = response.json()
            except httpx.HTTPStatusError as exc:
                raise UpstreamModelError(
                    f"Ollama 返回 {exc.response.status_code}：{exc.response.text[:300]}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ConfigurationError(
                    "无法连接 Ollama。请确认服务已启动（通常为 http://127.0.0.1:11434）。"
                ) from exc
            elapsed = time.monotonic() - started
            content = self._extract(data)
            self.usage.add(
                data.get("prompt_eval_count", 0), data.get("eval_count", 0), elapsed, attempts
            )
            return content
        status = ""
        if isinstance(last_error, UpstreamModelError):
            status = str(last_error)[:60]
        raise UpstreamModelError(
            f"Ollama 多次返回服务端错误（{status or '未知'}）。最常见原因是空闲内存不足："
            "加载 gemma 级模型约需 6 GB 可用内存，请关闭部分程序后重试，或换用更小的本地模型。"
        ) from last_error

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        try:
            content = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise UpstreamModelError("Ollama 响应格式异常。") from exc
        if not isinstance(content, str) or not content.strip():
            reason = str(data.get("done_reason") or "未知原因")
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            thinking = message.get("thinking") if isinstance(message, dict) else ""
            if thinking:
                raise UpstreamModelError(
                    f"Ollama 只返回了推理过程，未生成最终文本（结束原因：{reason}）。"
                    "请设置 PVC_OLLAMA_THINK=false 后重试。"
                )
            raise UpstreamModelError(f"Ollama 没有返回可用文本（结束原因：{reason}）。")
        return content.strip()


def build_client(settings, usage: Usage | None = None, transport=None):
    """Pick the transport from configuration; Ollama wins if a model is chosen."""
    if settings.ollama.enabled and (
        settings.llm.provider.lower() in {"ollama", "local"} or not settings.llm.api_key
    ):
        return OllamaLocalClient(settings.ollama, settings.llm.temperature, usage, transport)
    return OpenAICompatibleClient(settings.llm, usage, transport)
