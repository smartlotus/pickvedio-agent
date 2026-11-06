"""Environment-driven settings.

The desktop app kept configuration in ``~/.pickvedio/settings.json`` and the key
in the Windows credential store. Neither exists in a container, so every setting
is now an environment variable, with the JSON file kept as an optional seed for
local use. Two consequences worth stating plainly:

* secrets never appear in a log, an export, or a CLI response -- see
  :func:`Settings.redact`;
* a hosted deployment with no API key is *not* an error at import time. It is a
  precisely worded ``configuration`` failure raised when the summariser is
  actually reached, so ``pvc status`` can tell the operator what to set.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-air"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct"},
    "localai": {"base_url": "http://127.0.0.1:8080/v1", "model": "local"},
}

ASR_PRESETS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "whisper-1"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "whisper-large-v3-turbo"},
    "local_server": {"base_url": "http://127.0.0.1:8000/v1", "model": "whisper-1"},
    "faster_whisper": {"base_url": "", "model": "small"},
}

_SECRET_KEYS = ("api_key", "token", "sessdata", "cookie")


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_float(*names: str, default: float | None = None) -> float | None:
    raw = _env(*names)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(*names: str, default: int) -> int:
    raw = _env(*names)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(*names: str, default: bool = False) -> bool:
    raw = _env(*names).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class LLMSettings:
    provider: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 2600
    request_timeout: float = 300.0

    @classmethod
    def from_env(cls, seed: dict[str, Any] | None = None) -> LLMSettings:
        seed = seed or {}
        provider = _env("PVC_LLM_PROVIDER", "PVC_PROVIDER", default="") or str(
            seed.get("provider") or "deepseek"
        )
        preset = PROVIDER_PRESETS.get(provider.lower(), {})
        # A seeded base_url / api_key / model belongs to the provider that wrote
        # them. The desktop app keeps its local-Ollama choice in this very block
        # (`gemma4:e4b` on 127.0.0.1:11434), so honouring it under
        # PVC_LLM_PROVIDER=deepseek sent a DeepSeek-branded request to a local
        # Ollama URL for a model that endpoint never heard of -- and
        # `pvc status` cheerfully reported provider=deepseek, model=gemma4:e4b.
        # The seed only counts as a transport source when the provider agrees.
        transport = seed if str(seed.get("provider") or "").strip().lower() == provider.strip().lower() else {}
        return cls(
            provider=provider,
            base_url=_env("PVC_LLM_BASE_URL", default="")
            or str(transport.get("base_url") or "")
            or preset.get("base_url", ""),
            api_key=_env("PVC_LLM_API_KEY", "PVC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
            or str(transport.get("api_key") or ""),
            model=_env("PVC_LLM_MODEL", default="") or str(transport.get("model") or "") or preset.get("model", ""),
            temperature=_env_float("PVC_LLM_TEMPERATURE", default=None)
            if _env("PVC_LLM_TEMPERATURE")
            else float(seed.get("temperature", 0.2)),
            max_tokens=_env_int("PVC_LLM_MAX_TOKENS", default=2600),
            request_timeout=_env_float("PVC_LLM_TIMEOUT", default=300.0),
        )

    def require(self) -> None:
        if not self.base_url:
            raise ConfigurationError(
                f"未配置总结服务地址。请设置 PVC_LLM_BASE_URL，或把 PVC_LLM_PROVIDER 设为 "
                f"{'/'.join(sorted(PROVIDER_PRESETS))} 之一。"
            )
        if not self.model:
            raise ConfigurationError("未配置总结模型名称，请设置 PVC_LLM_MODEL。")
        if not self.api_key and "127.0.0.1" not in self.base_url and "localhost" not in self.base_url:
            raise ConfigurationError(
                "未配置总结服务的 API Key。请设置 PVC_LLM_API_KEY（本地模型服务不需要）。"
            )


@dataclass(slots=True)
class OllamaSettings:
    url: str = ""
    model: str = ""
    think: bool = False

    @classmethod
    def from_env(cls, seed: dict[str, Any] | None = None) -> OllamaSettings:
        """Seeded from the desktop app's settings.json when it chose Ollama.

        The app stores its local-model choice inside the *summary* block, so a
        user who picked `gemma4:e4b` in the UI would otherwise get an empty
        model list here and a confusing "no API key" failure at the summary
        stage. Provider "ollama" is only meaningful to the app's own transport.
        """
        seed = seed or {}
        chose_ollama = str(seed.get("provider") or "").strip().lower() in {"ollama", "local"}
        return cls(
            url=_env("PVC_OLLAMA_URL", default="") or str(seed.get("ollama_url") or "")
            or "http://127.0.0.1:11434",
            model=_env("PVC_OLLAMA_MODEL", default="")
            or (str(seed.get("model") or "") if chose_ollama else ""),
            think=_env_bool("PVC_OLLAMA_THINK", default=False),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.model)


@dataclass(slots=True)
class AudioSettings:
    language: str = ""
    chunk_seconds: int = 600
    use_vad: bool = True
    beam_size: int = 5

    @classmethod
    def from_env(cls) -> AudioSettings:
        return cls(
            language=_env("PVC_ASR_LANGUAGE", default=""),
            chunk_seconds=_env_int("PVC_ASR_CHUNK_SECONDS", default=600),
            use_vad=_env_bool("PVC_ASR_VAD", default=True),
            beam_size=_env_int("PVC_WHISPER_BEAM_SIZE", default=5),
        )


@dataclass(slots=True)
class ASRSettings:
    """Speech-to-text channel. ``provider`` may be empty: then ASR is disabled."""

    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_minutes: float = 180.0
    request_timeout: float = 600.0

    @classmethod
    def from_env(cls) -> ASRSettings:
        provider = _env("PVC_ASR_PROVIDER", default="")
        preset = ASR_PRESETS.get(provider.lower(), {})
        return cls(
            provider=provider,
            base_url=_env("PVC_ASR_BASE_URL", default="") or preset.get("base_url", ""),
            api_key=_env("PVC_ASR_API_KEY", "PVC_ASR_TOKEN") or preset.get("api_key", "")
            or _env("PVC_LLM_API_KEY", "PVC_API_KEY"),
            model=_env("PVC_ASR_MODEL", default="") or preset.get("model", ""),
            max_minutes=_env_float("PVC_ASR_MAX_MINUTES", default=180.0),
            request_timeout=_env_float("PVC_ASR_TIMEOUT", default=600.0),
        )

    @property
    def enabled(self) -> bool:
        if not self.provider:
            return False
        if self.provider.lower() in {"faster_whisper", "local"}:
            return True
        return bool(self.base_url and self.model and self.api_key)

    def disabled_reason(self) -> str:
        if not self.provider:
            return "未设置 PVC_ASR_PROVIDER：没有字幕的视频将无法转写。"
        if self.provider.lower() in {"faster_whisper", "local"}:
            return ""
        missing = [
            name
            for name, value in (
                ("PVC_ASR_BASE_URL", self.base_url),
                ("PVC_ASR_MODEL", self.model),
                ("PVC_ASR_API_KEY", self.api_key),
            )
            if not value
        ]
        return f"PVC_ASR_PROVIDER={self.provider} 但缺少 {'、'.join(missing)}。" if missing else ""


def _desktop_seed_path() -> Path | None:
    """The desktop app's optional settings file, or None when it has no home.

    A minimal container image can lack HOME/USERPROFILE entirely, and
    ``Path.home()`` then raises RuntimeError. Losing the whole service at import
    time because an *optional* seed file could not be located is not an
    acceptable failure, so the lookup is contained here.
    """
    try:
        return Path.home() / ".pickvedio" / "settings.json"
    except (RuntimeError, OSError):
        return None


@dataclass(slots=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    asr: ASRSettings = field(default_factory=ASRSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    workdir: Path = field(default_factory=lambda: Path("pickvedio-work"))
    ffmpeg_path: str = ""
    cookie_file: str = ""
    youtube_cookie_file: str = ""
    sessdata: str = ""
    api_token: str = ""
    chunk_chars: int = 18000
    max_chunks: int = 40
    max_minutes: float = 300.0
    allow_unknown_platform: bool = False
    profile_prompts: dict[str, dict[str, str]] = field(default_factory=dict)
    profile: str = "general"
    keep_media: bool = False
    style: str = "general"
    prompt_dir: Path | None = None

    @classmethod
    def load(cls, overrides: dict[str, Any] | None = None) -> Settings:
        seed: dict[str, Any] = {}
        configured = _env("PVC_CONFIG_FILE", default="")
        config_path = Path(configured) if configured else _desktop_seed_path()
        if config_path is not None and config_path.is_file():
            try:
                seed = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                seed = {}
        summary_seed = seed.get("summary") if isinstance(seed.get("summary"), dict) else seed
        settings = cls(
            llm=LLMSettings.from_env(summary_seed),
            ollama=OllamaSettings.from_env(summary_seed),
            asr=ASRSettings.from_env(),
            audio=AudioSettings.from_env(),
            workdir=Path(_env("PVC_WORKDIR", "PVC_WORK_DIR", default="") or "pickvedio-work"),
            ffmpeg_path=_env("PVC_FFMPEG", "FFMPEG_PATH"),
            cookie_file=_env("PVC_COOKIE_FILE", "PVC_COOKIES"),
            youtube_cookie_file=_env("PVC_YOUTUBE_COOKIE_FILE", default="")
            or _env("PVC_COOKIE_FILE", "PVC_COOKIES"),
            sessdata=_env("PVC_BILIBILI_SESSDATA"),
            api_token=_env("PVC_API_TOKEN", "PVC_AUTH_TOKEN"),
            chunk_chars=_env_int("PVC_CHUNK_CHARS", default=int(summary_seed.get("chunk_chars") or 18000)),
            max_chunks=_env_int("PVC_MAX_CHUNKS", default=40),
            max_minutes=_env_float("PVC_MAX_MINUTES", default=300.0),
            allow_unknown_platform=_env_bool("PVC_ALLOW_UNKNOWN_PLATFORM", default=False),
            keep_media=_env_bool("PVC_KEEP_MEDIA", default=False),
            style=_env("PVC_STYLE", default="") or str(seed.get("profile") or summary_seed.get("profile") or "general"),
            profile=str(summary_seed.get("profile") or "general"),
            profile_prompts={
                key: {slot: value for slot, value in slots.items() if isinstance(value, str)}
                for key, slots in (summary_seed.get("profile_prompts") or {}).items()
                if isinstance(slots, dict)
            },
            prompt_dir=Path(_env("PVC_PROMPT_DIR")) if _env("PVC_PROMPT_DIR") else None,
        )
        for key, value in (overrides or {}).items():
            if value is not None and hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    def save(self, path: Path) -> None:
        """Persist non-secret settings. Secrets stay in the environment."""
        payload = {
            key: value
            for key, value in asdict(self).items()
            if not any(secret in key for secret in _SECRET_KEYS)
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def redact(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("api_token", None)
        data["workdir"] = str(self.workdir)
        if isinstance(data.get("prompt_dir"), str) or data.get("prompt_dir") is not None:
            data["prompt_dir"] = str(data.get("prompt_dir") or "")
        for section in ("llm", "asr"):
            if isinstance(data.get(section), dict):
                data[section].pop("api_key", None)
                data[section][f"{section}_key_present"] = bool(
                    (self.llm if section == "llm" else self.asr).api_key
                )
        return data

    def job_dir(self, job_id: str) -> Path:
        path = self.workdir / "jobs" / job_id
        for sub in ("source", "audio", "transcript", "summary"):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return path
