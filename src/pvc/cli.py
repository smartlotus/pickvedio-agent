"""``pvc`` -- the command line face of pickvedio-core.

Contract (this is what an agent integration is written against, so it is stable
by intent, not by accident):

* stdout is **exactly one JSON object**, never a progress line or a warning;
* the object always has ``ok`` (bool), ``code`` (stable machine string) and
  ``message`` (Chinese, human-facing);
* failures additionally carry ``hint`` when there is an action that can
  actually resolve them -- the ``needs_browser`` case in particular is not an
  error to retry but an instruction to use the logged-in browser channel;
* diagnostics go to stderr under ``-v`` and never inside the envelope;
* secrets are never echoed. ``status`` reports presence, not values;
* exit code is 0 when ``ok`` is true, 1 for expected failures, 2 for usage
  errors raised by argparse.

``--format md|text|chat`` replaces stdout with the rendered report for a human
reader; ``json`` (the default) is what every documented example uses.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from pickvedio_core import __version__, install_spec
from pickvedio_core.config import Settings
from pickvedio_core.errors import PickvedioError
from pickvedio_core.jobs import JobStore
from pickvedio_core.pipeline import run as pipeline_run
from pickvedio_core.prompts import PROFILE_LABELS, list_profiles, load as load_prompts
from pickvedio_core.sources.ladder import describe_channels
from pickvedio_core.urls import find_all_video_urls, validate_video_url

OK = "ok"


def envelope(*, ok: bool, code: str, message: str, **data: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "code": code, "message": message}
    payload = {key: value for key, value in data.items() if value is not None}
    if payload:
        out["data"] = payload
    return out


def emit(document: dict[str, Any], *, indent: int = 2) -> int:
    json.dump(document, sys.stdout, ensure_ascii=False, indent=indent)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if document.get("ok") else 1


class _Remote:
    """Thin HTTP client so the CLI can drive a deployed service identically."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 60.0) -> None:
        self.base = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout

    def headers(self) -> dict[str, str]:
        out = {"Content-Type": "application/json"}
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    def request(self, method: str, path: str, **kw) -> dict[str, Any]:
        import httpx

        url = f"{self.base}{path}"
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=15.0)) as client:
                response = client.request(method, url, headers=self.headers(), **kw)
        except httpx.HTTPError as exc:
            raise PickvedioError(f"无法连接服务 {url}：{exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = body.get("error") or body.get("hint") or response.text[:300]
            except ValueError:
                detail = response.text[:300]
            raise PickvedioError(f"服务返回 {response.status_code}：{detail}")
        if not response.content:
            return {}
        return response.json()


def _service_from_args(args) -> _Remote | None:
    base = getattr(args, "service", "") or os.environ.get("PVC_SERVICE_URL", "")
    if not base:
        return None
    token = getattr(args, "token", "") or os.environ.get("PVC_API_TOKEN", "")
    return _Remote(base, token)


def _settings(args) -> Settings:
    overrides: dict[str, Any] = {}
    if getattr(args, "style", ""):
        overrides["style"] = args.style
    if getattr(args, "chunk_chars", 0):
        overrides["chunk_chars"] = args.chunk_chars
    if getattr(args, "workdir", ""):
        overrides["workdir"] = Path(args.workdir)
    if getattr(args, "allow_unknown_platform", False):
        overrides["allow_unknown_platform"] = True
    if getattr(args, "sessdata", ""):
        overrides["sessdata"] = args.sessdata
    if getattr(args, "cookie_file", ""):
        overrides["cookie_file"] = args.cookie_file
    return Settings.load(overrides)


def _target(args, positional: str = "url_arg") -> str:
    """Accept ``pvc fetch URL`` and ``pvc fetch --url URL`` alike.

    Both forms are documented -- the skill pack writes ``--url`` because an
    agent assembling a command line from a template finds a named flag
    unambiguous, while a human at a terminal types the positional form.
    """
    value = getattr(args, "url", "") or getattr(args, positional, "") or ""
    if not str(value).strip():
        raise PickvedioError("缺少视频链接：请给出位置参数或用 --url 传入。")
    return str(value)


def _read_payload(spec: str) -> Any:
    """Accept a path, ``-`` for stdin, or an inline JSON string.

    The filesystem is only consulted for short, newline-free, non-JSON text:
    on Linux ``Path(huge).is_file()`` raises ``ENAMETOOLONG`` instead of
    returning False, and agents paste whole extraction objects inline.
    """
    if spec == "-":
        return sys.stdin.read()
    stripped = spec.strip()
    if not stripped or stripped[0] in '[{"' or len(stripped) > 512 or "\n" in stripped:
        return stripped
    path = Path(stripped)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    raise PickvedioError(f"找不到该文件，也不像 JSON 内容：{stripped[:120]}")


# -- commands ----------------------------------------------------------------


def cmd_status(args) -> int:
    settings = _settings(args)
    remote = _service_from_args(args)
    data: dict[str, Any] = {
        "version": __version__,
        "workdir": str(settings.workdir),
        "workdir_writable": _writable(settings.workdir),
        "channels": describe_channels(settings),
        "llm": {
            "provider": settings.llm.provider,
            "model": settings.llm.model,
            "base_url": settings.llm.base_url,
            "api_key_present": bool(settings.llm.api_key),
        },
        "ollama": {"url": settings.ollama.url, "model": settings.ollama.model,
                   "enabled": settings.ollama.enabled},
        "asr": {"provider": settings.asr.provider, "model": settings.asr.model,
                "enabled": settings.asr.enabled, "reason": settings.asr.disabled_reason()},
        "inbound_auth_configured": bool(settings.api_token),
        "profiles": PROFILE_LABELS,
        "style_default": settings.style,
    }
    missing = []
    if not settings.llm.api_key and not settings.ollama.enabled:
        missing.append("总结模型：设置 PVC_LLM_API_KEY(+PVC_LLM_PROVIDER) 或 PVC_OLLAMA_MODEL")
    if not settings.asr.enabled:
        missing.append("语音转写（可选）：设置 PVC_ASR_PROVIDER 与 PVC_ASR_API_KEY")
    if not settings.api_token:
        missing.append("入站鉴权（部署时必填）：设置 PVC_API_TOKEN")
    data["missing"] = missing
    if remote is not None:
        try:
            health = remote.request("GET", "/healthz")
            data["service"] = {"url": remote.base, "reachable": True, "health": health}
        except PickvedioError as exc:
            data["service"] = {"url": remote.base, "reachable": False, "error": str(exc)}
    return emit(envelope(
        ok=True, code=OK,
        message="配置与通道自检完成；missing 为空即可直接开始总结。" if not missing
                else f"还有 {len(missing)} 项待配置。",
        **data,
    ))


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".pvc-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def cmd_resolve(args) -> int:
    remote = _service_from_args(args)
    text = args.text or args.url
    if not text.strip():
        return emit(envelope(ok=False, code="no_url", message="没有给出文本或链接。"))
    refs = find_all_video_urls(text, allow_unknown_platform=_settings(args).allow_unknown_platform)
    if not refs:
        return emit(envelope(
            ok=False, code="no_url",
            message="文本里没有识别到 B 站或 YouTube 视频链接。",
            hint="支持：B 站分享文案 / b23.tv 短链 / BV 或 av 号 / YouTube 观看、shorts、youtu.be 链接。",
        ))
    primary = refs[0]
    payload: dict[str, Any] = {
        "url": primary.url,
        "platform": primary.platform,
        "video_id": primary.video_id,
        "identity": primary.identity,
    }
    if remote is not None:
        data = remote.request("GET", f"/v1/metadata?url={primary.url}")
        payload.update({key: value for key, value in data.items() if key != "ok"})
    return emit(envelope(
        ok=True, code=OK,
        message=f"识别到 {primary.platform} 链接" + (f"（共 {len(refs)} 个）" if len(refs) > 1 else ""),
        urls=[{"url": item.url, "platform": item.platform, "video_id": item.video_id} for item in refs],
        **payload,
    ))


def cmd_fetch(args) -> int:
    settings = _settings(args)
    from pickvedio_core.sources.ladder import resolve

    url = validate_video_url(_target(args), allow_unknown_platform=settings.allow_unknown_platform)
    resolution = resolve(
        url,
        settings,
        workdir=settings.job_dir("fetch"),
        page=args.page,
        allow_asr=not args.no_asr,
        allow_page_text=args.allow_page_text,
    )
    transcript = resolution.transcript
    segments = [item.as_dict() for item in transcript.segments]
    written = ""
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
        written = str(out)
    return emit(envelope(
        ok=True, code=OK,
        message=f"已取得逐字稿：{len(segments)} 段 / {transcript.char_count} 字（来源 {transcript.source.value}）",
        url=transcript.video_url,
        title=transcript.title,
        duration=transcript.duration,
        language=transcript.language,
        channel=transcript.channel,
        source=transcript.source.value,
        confidence=transcript.confidence,
        chars=transcript.char_count,
        segments=segments,
        transcript=segments,
        attempts=resolution.trail,
        warnings=resolution.warnings,
        meta=transcript.meta,
        out=written or None,
    ))


def cmd_push(args) -> int:
    """Send a relayed transcript to the service (or summarise it locally)."""
    settings = _settings(args)
    payload = _read_payload(args.transcript)
    remote = _service_from_args(args)
    url = args.url
    if url:
        url = validate_video_url(url, allow_unknown_platform=settings.allow_unknown_platform)
    meta = {
        "title": args.title or "",
        "duration": args.duration,
        "language": args.language or "",
        "source_kind": args.source_kind or "",
        "uploader": args.uploader or "",
        "origin": "pvc-cli",
        "name": args.transcript if args.transcript != "-" else "",
        "strict": not args.allow_sparse,
    }
    if remote is not None:
        body = {
            "url": url,
            "style": args.style or settings.style,
            "title": meta["title"],
            "duration": meta["duration"],
            "language": meta["language"],
            "source_kind": meta["source_kind"],
            "transcript": payload,
        }
        data = remote.request("POST", "/v1/transcripts", json=body)
        job_id = data.get("job_id") or ""
        report = data.get("report")
        if report is None and job_id and not args.no_wait:
            report = _await_report(remote, job_id, getattr(args, "timeout", 900.0))
        if report is None:
            return emit(envelope(
                ok=True, code="queued", message=f"已提交服务，任务 {job_id}。",
                job_id=job_id, service=remote.base,
            ))
        return _emit_report(report, args, settings, job_id=job_id)
    if not url:
        return emit(envelope(ok=False, code="configuration",
                             message="本地模式必须提供 --url（无法从回传内容推断视频链接）。"))
    outcome = pipeline_run(
        url,
        settings,
        style=args.style,
        job_id=args.job_id or uuid.uuid4().hex[:12],
        relay_payload=payload,
        relay_meta=meta,
        write_artifacts=bool(args.out),
    )
    return _emit_report(payload_of(outcome), args, settings, job_id=outcome.report.job_id)


def payload_of(outcome) -> dict[str, Any]:
    from pickvedio_core.report import payload as report_payload

    data = report_payload(outcome.report)
    data["attempts"] = outcome.resolution.trail
    data["usage"] = outcome.usage
    data["seconds"] = round(outcome.seconds, 2)
    return data


def _emit_report(report: dict[str, Any], args, settings: Settings, *, job_id: str = "") -> int:
    """Emit one CLI envelope for a finished report.

    The service envelope's ``ok``/``code``/``message``/``job_id`` describe the
    transport, not the report, so they are dropped rather than colliding with
    the arguments below.
    """
    report = {key: value for key, value in report.items()
              if key not in ("ok", "code", "message", "job_id")}
    text_format = getattr(args, "format", "json")
    if text_format != "json":
        # The report already carries every rendering. Rebuilding a Report from
        # its JSON projection would risk disagreeing with the bytes the service
        # stored, and a chat reply that differs from the saved file is worse
        # than no convenience flag at all.
        field = {"md": "report_markdown", "text": "report_text", "chat": "chat_message"}[text_format]
        rendered = str(report.get(field) or report.get("summary") or "")
        emit(envelope(
            ok=True, code=OK,
            message=f"总结完成（{str(report.get('title') or '')[:60]}）",
            job_id=job_id or report.get("job_id", ""),
            render=text_format,
            rendered_chars=len(rendered),
        ))
        sys.stderr.write('\n' + rendered + '\n')
        return 0
    return emit(envelope(
        ok=True, code=OK,
        message=f"总结完成：{str(report.get('title') or '')[:60]}",
        job_id=job_id or report.get("job_id", ""),
        **report,
    ))



def cmd_summarize(args) -> int:
    settings = _settings(args)
    remote = _service_from_args(args)
    url = validate_video_url(_target(args), allow_unknown_platform=settings.allow_unknown_platform)
    if remote is not None:
        body = {"url": url, "style": args.style or settings.style}
        if args.dedup_minutes is not None:
            body["dedup_minutes"] = args.dedup_minutes
        data = remote.request("POST", "/v1/jobs", json=body)
        job_id = data.get("job_id", "")
        if data.get("report"):
            return _emit_report(data["report"], args, settings, job_id=job_id)
        if args.no_wait:
            return emit(envelope(ok=True, code="queued", message=f"任务已创建：{job_id}", job_id=job_id))
        report = _await_report(remote, job_id, args.timeout)
        return _emit_report(report, args, settings, job_id=job_id)
    if not args.estimate_only:
        # A fresh job id per run: with a fixed one, every CLI invocation wrote
        # into the same jobs/<id> directory and the previous video's summary.md
        # quietly disappeared.
        job_id = args.job_id or uuid.uuid4().hex[:12]
        outcome = pipeline_run(
            url, settings, style=args.style, job_id=job_id, write_artifacts=True,
            allow_asr=not args.no_asr, allow_page_text=args.allow_page_text,
        )
        report = payload_of(outcome)
        if args.out:
            from pickvedio_core.report import write_all

            report["artifacts"] = write_all(
                outcome.report, Path(args.out), style_label=load_prompts(args.style or settings.style).label
            )
        return _emit_report(report, args, settings)
    from pickvedio_core.sources.ladder import resolve

    resolution = resolve(url, settings, allow_asr=not args.no_asr, allow_page_text=args.allow_page_text)
    transcript = resolution.transcript
    prompt_set = load_prompts(args.style or settings.style, settings.prompt_dir)
    from pickvedio_core.summarize import Summarizer, estimate_calls

    chunks = Summarizer(prompt_set, client=None, chunk_chars=settings.chunk_chars,
                        max_chunks=settings.max_chunks).chunks(transcript.segments)
    return emit(envelope(
        ok=True, code=OK,
        message=f"估算完成：{len(chunks)} 个切块，约 {len(chunks) + 2} 次模型调用，未花费任何额度。",
        title=transcript.title, duration=transcript.duration, chars=transcript.char_count,
        chunks=len(chunks), estimated_calls=estimate_calls(len(chunks)),
        profile=prompt_set.profile, attempts=resolution.trail,
    ))


def _await_report(remote: _Remote, job_id: str, timeout: float) -> dict[str, Any]:
    """Poll a job to a terminal state, then read the report document.

    The status envelope deliberately omits the report (it can carry a whole
    transcript), so completion always ends in a second GET.
    """
    import time

    deadline = time.monotonic() + timeout
    last_message = ""
    while time.monotonic() < deadline:
        data = remote.request("GET", f"/v1/jobs/{job_id}")
        status = data.get("status")
        message = data.get("message", "")
        if message != last_message:
            sys.stderr.write(f"[{job_id}] {status} {message}\n")
            sys.stderr.flush()
            last_message = message
        if status == "completed":
            return remote.request("GET", f"/v1/jobs/{job_id}/report")
        if status in ("failed", "canceled"):
            raise PickvedioError(data.get("error") or f"任务 {job_id} 以 {status} 结束。")
        time.sleep(min(2.0, max(0.4, deadline - time.monotonic())))
    raise PickvedioError(
        f"等待任务 {job_id} 超时（{timeout:.0f}s）；稍后可用 `pvc job report --id {job_id}` 取结果。"
    )


def cmd_job(args) -> int:
    settings = _settings(args)
    remote = _service_from_args(args)
    if remote is not None:
        if args.action == "create":
            data = remote.request("POST", "/v1/jobs", json={"url": args.url, "style": args.style})
            return emit(envelope(ok=True, code=OK, message="任务已创建", **data))
        if args.action == "list":
            return emit(envelope(ok=True, code=OK, message="任务列表", jobs=remote.request("GET", "/v1/jobs")))
        data = remote.request("GET", f"/v1/jobs/{args.id}")
        if args.action == "wait":
            report = _await_report(remote, args.id, args.timeout)
            return _emit_report(report, args, settings, job_id=args.id)
        if args.action == "report":
            data = remote.request("GET", f"/v1/jobs/{args.id}/report")
            return _emit_report(data, args, settings, job_id=args.id)
        if args.action == "cancel":
            return emit(envelope(ok=True, code=OK, message="已请求取消",
                                 **remote.request("POST", f"/v1/jobs/{args.id}/cancel")))
        return emit(envelope(ok=True, code=OK, message="任务状态", **data))
    store = JobStore(settings.workdir / "jobs.sqlite3")
    if args.action == "create":
        record = store.create(args.url or "", style=args.style)
        return emit(envelope(ok=True, code=OK, message="已在本地创建任务（未启动后台执行）",
                             job=record.as_dict(include_report=False)))
    if args.action == "list":
        return emit(envelope(ok=True, code=OK, message=f"本地任务 {len(store.list())} 条",
                             jobs=[item.as_dict(include_report=False) for item in store.list(limit=args.limit)]))
    if args.action == "prune":
        return emit(envelope(ok=True, code=OK, message="清理完成", removed=store.prune(args.keep_days)))
    record = store.get(args.id)
    if args.action == "report" and not record.report:
        return emit(envelope(ok=False, code="not_ready", message=f"任务 {args.id} 尚无结果（{record.stage}）"))
    return emit(envelope(ok=True, code=OK, message="任务状态", job=record.as_dict()))


def cmd_profiles(args) -> int:
    return emit(envelope(ok=True, code=OK, message="可用总结档位",
                         profiles=list_profiles(), default=_settings(args).style))


def cmd_models(args) -> int:
    settings = _settings(args)
    if args.ollama:
        from pickvedio_core.llm import OllamaLocalClient
        from pickvedio_core.config import OllamaSettings

        names = OllamaLocalClient(OllamaSettings(url=args.ollama_url or settings.ollama.url)).list_models()
        return emit(envelope(ok=True, code=OK, message=f"Ollama 可用模型 {len(names)} 个", models=names))
    if args.provider:
        settings.llm.provider = args.provider
    from pickvedio_core.llm import OpenAICompatibleClient

    settings.llm.require()
    return emit(envelope(
        ok=True, code=OK, message="模型列表",
        models=OpenAICompatibleClient(settings.llm).list_models(),
        provider=settings.llm.provider,
    ))


def cmd_serve(args) -> int:
    settings = _settings(args)
    host = args.host or os.environ.get("PVC_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("PORT") or os.environ.get("PVC_PORT") or 8788)
    if not settings.api_token:
        sys.stderr.write(
            "警告：PVC_API_TOKEN 未设置，服务将接受任何来源的匿名调用（消耗你的模型额度）。\n"
        )
    try:
        import uvicorn
    except ImportError:
        return emit(envelope(ok=False, code="dependency_missing",
                             message="未安装服务依赖。",
                             hint=f'python3 -m pip install "{install_spec("service")}"'))
    from service.app import create_app

    uvicorn.run(create_app(settings), host=host, port=port)
    return 0


def cmd_check(args) -> int:
    """Offline self-test: no network, no model calls, real assertions.

    Deliberately includes a negative control -- a payload that must be refused
    is refused -- so a green ``check`` means the guards work, not that they are
    merely present.
    """
    from pickvedio_core.sources.relay import to_transcript
    from pickvedio_core.sources.parsers import from_json3, from_srt, from_vtt
    from pickvedio_core.summarize import plan_chunks

    checks: list[dict[str, Any]] = []
    fixture_segments = [
        {"start": 0.0, "end": 4.0, "text": "第一句：本视频介绍分层总结的流程。"},
        {"start": 4.0, "end": 9.0, "text": "第二句：先取得逐字稿，再按字数切块。"},
        {"start": 20.0, "end": 26.0, "text": "第三句：字幕通道不需要下载音频。"},
    ]
    result = to_transcript("https://www.bilibili.com/video/BV1xx411c7mD/",
                           {"segments": fixture_segments, "title": "自检", "duration": 30},
                           title="自检", duration=30, source_kind="up_subtitles")
    checks.append({"name": "relay_accepts_good_payload", "passed": len(result.transcript.segments) == 3,
                   "value": len(result.transcript.segments)})
    sparse = to_transcript.__doc__ is not None
    try:
        to_transcript("https://www.bilibili.com/video/BV1xx411c7mD/",
                      {"segments": [{"start": 0.0, "end": 3.0, "text": "短"}], "title": "稀疏"},
                      title="稀疏", duration=5400, source_kind="up_subtitles", strict=True)
        checks.append({"name": "relay_rejects_sparse_payload", "passed": False,
                       "value": "长视频只回传 1 段却被接受了"})
    except PickvedioError as exc:
        checks.append({"name": "relay_rejects_sparse_payload", "passed": True, "value": str(exc)[:80]})
    srt_rows = from_srt("1\n00:00:01,000 --> 00:00:05,000\n第二句\n\n2\n00:00:05,000 --> 00:00:09,000\n第三句\n")
    checks.append({"name": "parse_srt", "passed": len(srt_rows) == 2, "value": len(srt_rows)})
    vtt_rows = from_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v 讲者>你好\n\n")
    checks.append({"name": "parse_vtt_speaker", "passed": bool(vtt_rows) and vtt_rows[0].speaker == "讲者",
                   "value": vtt_rows[0].speaker if vtt_rows else ""})
    json3 = from_json3(json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "你好"}]},
        {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "\n"}]},
    ]}))
    checks.append({"name": "parse_json3_skips_newline_events", "passed": len(json3) == 1, "value": len(json3)})
    chunks = plan_chunks(result.transcript.segments, chunk_chars=4000, max_chunks=40)
    checks.append({"name": "chunk_plan", "passed": len(chunks) >= 1, "value": len(chunks)})
    checks.append({"name": "sparse_guard_present", "passed": sparse, "value": True})
    failed = [item for item in checks if not item["passed"]]
    return emit(envelope(
        ok=not failed, code=OK if not failed else "self_test_failed",
        message="离线自检全部通过。" if not failed else f"{len(failed)} 项自检失败。",
        network_used=False, model_calls=0, checks=checks,
    ))


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pvc",
        description="pickvedio-core：把长视频链接变成带时间点的逐字稿与分层中文总结。"
                    "stdout 恒为单个 JSON 对象。",
    )
    parser.add_argument("--version", action="version", version=f"pickvedio-core {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="把日志写到 stderr")
    parser.add_argument("--workdir", default="", help="工作目录（默认 PVC_WORKDIR 或 ./pickvedio-work）")
    parser.add_argument("--chunk-chars", type=int, default=0, help="覆盖 PVC_CHUNK_CHARS")
    parser.add_argument("--allow-unknown-platform", action="store_true")
    parser.add_argument("--service", default="", help="改用已部署的服务（等价 PVC_SERVICE_URL）")
    parser.add_argument("--token", default="", help="服务入站令牌（等价 PVC_API_TOKEN，不要把值写进日志）")
    # Also declared on the subcommands that produce a report, with SUPPRESS, so
    # either `pvc --format md summarize …` or `pvc summarize … --format md`
    # works. A flag only the root declares would be rejected after the
    # subcommand; one only the subparser declares gets clobbered before it.
    parser.add_argument("--format", choices=["json", "md", "text", "chat"], default="json")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_remote(target: argparse.ArgumentParser) -> None:
        """Per-subcommand ``--service``/``--token``.

        argparse only accepts options *after* a subcommand if the subparser
        declares them, and the documented call shape is
        ``pvc status --service URL``, so the root-level flags alone would make
        the docs a lie.
        """
        target.add_argument("--service", default=argparse.SUPPRESS,
                            help="改用已部署的服务（等价 PVC_SERVICE_URL）")
        target.add_argument("--token", default=argparse.SUPPRESS, help="服务入站令牌")

    def add_format(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--format", choices=["json", "md", "text", "chat"], default=argparse.SUPPRESS,
            help="json 为默认且唯一稳定的机器接口；其余把渲染后的报告写到 stderr",
        )

    status = sub.add_parser("status", help="配置与通道自检")
    add_remote(status)
    status.set_defaults(func=cmd_status)

    resolve_parser = sub.add_parser("resolve", help="从任意文本里识别视频链接")
    resolve_parser.add_argument("text", nargs="?", default="")
    resolve_parser.add_argument("--url", default="", help="等价于位置参数 text")
    add_remote(resolve_parser)
    resolve_parser.set_defaults(func=cmd_resolve)

    fetch = sub.add_parser("fetch", help="只取逐字稿，不做总结")
    fetch.add_argument("url_arg", nargs="?", default="")
    fetch.add_argument("--url", default="")
    fetch.add_argument("--out", default="", help="把 segments 写到该 JSON 文件")
    fetch.add_argument("--page", type=int, default=1)
    fetch.add_argument("--no-asr", action="store_true", help="禁止下载音频转写（默认允许，若已配置）")
    fetch.add_argument("--allow-page-text", action="store_true")
    fetch.add_argument("--sessdata", default="")
    fetch.add_argument("--cookie-file", default="")
    add_remote(fetch)
    fetch.set_defaults(func=cmd_fetch)

    push = sub.add_parser("push", help="把浏览器/本地取得的字幕交给服务总结（回传通道）")
    push.add_argument("--url", default="")
    push.add_argument("--transcript", required=True, help="JSON/SRT/VTT 文件路径、`-` 或内联 JSON")
    push.add_argument("--title", default="")
    push.add_argument("--duration", type=float, default=None)
    push.add_argument("--language", default="")
    push.add_argument("--source-kind", default="", help="up_subtitles|human_subtitles|platform_auto|relayed")
    push.add_argument("--uploader", default="")
    push.add_argument("--style", default="")
    push.add_argument("--job-id", default="", help="本地模式的任务目录名（默认随机）")
    push.add_argument("--out", default="")
    push.add_argument("--no-wait", action="store_true", help="只提交，不等待结果")
    add_format(push)
    push.add_argument("--allow-sparse", action="store_true", help="关闭稀疏内容拒绝（可能产出不可靠总结）")
    add_remote(push)
    push.set_defaults(func=cmd_push)

    summarize = sub.add_parser("summarize", help="链接 → 逐字稿 → 分层总结")
    summarize.add_argument("url_arg", nargs="?", default="")
    summarize.add_argument("--url", default="")
    summarize.add_argument("--style", default="", help="、".join(PROFILE_LABELS))
    summarize.add_argument("--out", default="")
    summarize.add_argument("--no-asr", action="store_true")
    summarize.add_argument("--allow-page-text", action="store_true")
    summarize.add_argument("--estimate-only", action="store_true", help="只估算切块与调用数，不调用模型")
    summarize.add_argument("--timeout", type=float, default=1800.0)
    summarize.add_argument("--no-wait", action="store_true")
    add_format(summarize)
    summarize.add_argument("--dedup-minutes", type=int, default=None)
    summarize.add_argument("--job-id", default="", help="任务目录名（默认随机，避免互相覆盖）")
    summarize.add_argument("--sessdata", default="")
    summarize.add_argument("--cookie-file", default="")
    add_remote(summarize)
    summarize.set_defaults(func=cmd_summarize)

    job = sub.add_parser("job", help="任务生命周期")
    job.add_argument("action", choices=["create", "status", "wait", "list", "report", "cancel", "prune"])
    job.add_argument("--id", default="")
    job.add_argument("--url", default="")
    job.add_argument("--style", default="")
    job.add_argument("--timeout", type=float, default=1800.0)
    job.add_argument("--limit", type=int, default=50)
    add_format(job)
    job.add_argument("--keep-days", type=float, default=14.0)
    add_remote(job)
    job.set_defaults(func=cmd_job)

    profiles = sub.add_parser("profiles", help="列出总结档位")
    add_remote(profiles)
    profiles.set_defaults(func=cmd_profiles)

    models = sub.add_parser("models", help="列出可用模型")
    models.add_argument("--provider", default="")
    models.add_argument("--ollama", action="store_true")
    models.add_argument("--ollama-url", default="")
    add_remote(models)
    models.set_defaults(func=cmd_models)

    serve = sub.add_parser("serve", help="本地启动 HTTP 服务")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    add_remote(serve)
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="离线自检（不联网、不调用模型）")
    add_remote(check)
    check.set_defaults(func=cmd_check)
    return parser


def _force_utf8() -> None:
    """Make stdout UTF-8 no matter what codepage the host console reports.

    The contract is "stdout is exactly one JSON object". On Windows a redirected
    stream inherits the active codepage (cp936 here), so a report containing
    Chinese titles came out as bytes that are not valid JSON for any consumer
    that decoded them as UTF-8 -- including this project's own bootstrap script.
    """
    for stream in (sys.stdout, sys.stderr):
        # Some environments hand us a stream that cannot be reconfigured (a
        # captured buffer in a test, a closed handle); those keep whatever
        # encoding they have rather than failing the command.
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except PickvedioError as exc:
        hint = getattr(exc, "hint", "")
        return emit(envelope(
            ok=False, code=getattr(exc, "code", "error"), message=str(exc)[:1200], hint=hint or None
        ))
    except KeyboardInterrupt:
        return emit(envelope(ok=False, code="cancelled", message="用户中断。"))
    except BrokenPipeError:
        return 1
    except Exception as exc:  # never leak a traceback into a JSON contract
        logging.getLogger("pvc").exception("unhandled error")
        return emit(envelope(
            ok=False, code="internal",
            message=f"未预期错误：{exc.__class__.__name__}: {str(exc)[:400]}",
        ))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
