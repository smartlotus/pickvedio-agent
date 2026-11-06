"""HTTP service.

Shaped for how an agent platform actually consumes it:

* **asynchronous jobs**, because a 3-hour video outlives a free-tier request;
* a **relayed-transcript path** as the recommended hot route, because captions
  obtained from the caller's logged-in browser are the only reliable way past
  the platforms' bot checks -- and it needs no media pipeline in the container;
* an **OpenAPI document** at ``/openapi.json``, so an agent that can write an
  integration from a service's API can build one without reading this file;
* fail-closed inbound auth: if ``PVC_API_TOKEN`` is unset the service refuses
  ``/v1/*`` with a 503 that says why, rather than silently burning the
  operator's model quota on whoever finds the URL.

One worker per volume: job state lives in SQLite and there is no distributed
queue. ``PVC_WORKERS`` sizes the local pool.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pickvedio_core import __version__, install_spec
from pickvedio_core.config import Settings
from pickvedio_core.errors import (
    AuthRequired,
    ConfigurationError,
    DependencyError,
    EmptyTranscript,
    JobNotFound,
    PickvedioError,
    RateLimited,
    TransientNetworkError,
    UpstreamBlocked,
    UpstreamModelError,
)
from pickvedio_core.jobs import JobStore
from pickvedio_core.pipeline import run as pipeline_run
from pickvedio_core.prompts import PROFILE_LABELS, list_profiles
from pickvedio_core.report import payload as report_payload
from pickvedio_core.sources.ladder import describe_channels
from pickvedio_core.urls import classify, validate_video_url

logger = logging.getLogger("pickvedio.service")

DESCRIPTION = (
    "把视频链接变成带时间点的逐字稿与分层中文总结。支持 B 站与 YouTube；"
    "字幕优先，浏览器回传为可靠通道，音频转写为可选通道。"
)

# Each error class carries the status it should surface as, so a new error type
# cannot silently degrade into a 500 with no information.
_STATUS = {
    ConfigurationError: 400,
    AuthRequired: 401,
    JobNotFound: 404,
    DependencyError: 501,
    RateLimited: 429,
    EmptyTranscript: 422,
    UpstreamBlocked: 424,
    UpstreamModelError: 502,
    TransientNetworkError: 502,
    PickvedioError: 500,
}


def status_for(exc: PickvedioError) -> int:
    for cls, code in _STATUS.items():
        if isinstance(exc, cls):
            return code
    return 500


def create_app(settings: Settings | None = None, *, worker_count: int | None = None):
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
        from fastapi.responses import JSONResponse
        from fastapi.security import HTTPBearer
    except ImportError as exc:  # pragma: no cover
        raise DependencyError(
            f"服务依赖未安装：python3 -m pip install \"{install_spec('service')}\""
        ) from exc

    settings = settings or Settings.load()
    store = JobStore(settings.workdir / "jobs.sqlite3")
    workers = ThreadPoolExecutor(
        max_workers=worker_count or int(os.environ.get("PVC_WORKERS", "2")),
        thread_name_prefix="pvc-job",
    )
    cancel_flags: dict[str, threading.Event] = {}
    running: dict[str, Any] = {"started_at": time.time()}

    # `require_token` below is what actually rejects a request; this object exists so
    # that requirement appears in /openapi.json. Without it the document claimed an
    # unauthenticated API (measured: `securitySchemes: null`, every operation with
    # `security: false`) while /v1/* answered 401 -- an agent building a connector
    # from the spec would generate a client that could never authenticate.
    bearer_scheme = HTTPBearer(
        auto_error=False,
        scheme_name="BearerAuth",
        bearerFormat="opaque",
        description="PVC_API_TOKEN，写成 `Authorization: Bearer <token>`。"
                    "未配置令牌时服务故意对所有 /v1 请求返回 503。",
    )

    app = FastAPI(
        title="pickvedio-core",
        version=__version__,
        description=DESCRIPTION,
        contact={"name": "smartlotus", "url": "https://github.com/smartlotus/pickvedio-agent"},
        license_info={"name": "MIT"},
        openapi_tags=[
            {"name": "jobs", "description": "异步总结任务：创建、查询、报告、取消"},
            {"name": "transcripts", "description": "回传字幕直接总结（浏览器通道，最快且零媒体开销）"},
            {"name": "meta", "description": "健康检查、档位、能力自检、元数据"},
        ],
    )

    def require_token(authorization: str = Header(default="")) -> None:
        if not settings.api_token:
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "code": "configuration",
                    "message": "服务未配置 PVC_API_TOKEN，已拒绝所有 /v1 请求（避免匿名消耗模型额度）。",
                    "hint": "在部署环境里设置 PVC_API_TOKEN 后重启服务。",
                },
            )
        supplied = (authorization or "").strip()
        token = supplied[7:].strip() if supplied.lower().startswith("bearer ") else supplied
        if token != settings.api_token:
            raise HTTPException(
                status_code=401,
                detail={"ok": False, "code": "unauthorized",
                        "message": "缺少或错误的 Authorization Bearer 令牌。"},
            )

    def style_or_400(value: str) -> str:
        style = (value or settings.style).strip()
        if style not in PROFILE_LABELS:
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "code": "configuration",
                        "message": f"未知总结档位 {style!r}。可选：{'、'.join(PROFILE_LABELS)}。"},
            )
        return style

    def launch(job_id: str, url: str, style: str, *, relay_payload: Any = None,
               relay_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a queued job on the local pool and register its cancel flag."""
        flag = threading.Event()
        cancel_flags[job_id] = flag

        def work() -> None:
            try:
                pipeline_run(
                    url, settings, style=style, job_id=job_id, store=store, cancel_event=flag,
                    relay_payload=relay_payload, relay_meta=relay_meta,
                )
            except PickvedioError as exc:
                code = getattr(exc, "code", "error")
                stage = "needs_browser" if code in ("needs_browser", "needs_auth") else "failed"
                store.fail(job_id, str(exc), code=code, stage=stage)
                logger.info("job %s failed (%s): %s", job_id, code, exc)
            except Exception:
                logger.exception("job %s crashed", job_id)
                store.fail(job_id, "服务内部错误，请查看服务日志。", code="internal")
            finally:
                cancel_flags.pop(job_id, None)

        workers.submit(work)
        return {"job_id": job_id, "status": "queued", "style": style,
                "message": "任务已开始。请轮询 GET /v1/jobs/{job_id}，完成后取 "
                           "GET /v1/jobs/{job_id}/report。"}

    def submit(url: str, style: str, *, relay_payload: Any = None,
               relay_meta: dict[str, Any] | None = None, dedup_minutes: int = 60) -> dict[str, Any]:
        record = store.create(
            url, platform=classify(url).platform, style=style,
            dedup_minutes=0 if relay_payload is not None else dedup_minutes,
        )
        if record.report:
            return {"job_id": record.id, "status": "completed", "reused": True,
                    "message": record.message, "report": record.report}
        result = launch(record.id, url, style, relay_payload=relay_payload, relay_meta=relay_meta)
        if relay_payload is not None:
            result["relayed"] = True
        return result

    def relay_fields(body: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        payload_body = body.get("transcript") if body.get("transcript") is not None else body.get("segments")
        meta = {
            "title": str(body.get("title") or ""),
            "duration": body.get("duration"),
            "language": str(body.get("language") or ""),
            "source_kind": str(body.get("source_kind") or body.get("sourceKind") or ""),
            "uploader": str(body.get("uploader") or ""),
            "origin": "http-relay",
            "strict": bool(body.get("strict", True)),
        }
        return payload_body, meta

    # -- meta ----------------------------------------------------------------

    # FastAPI puts `Body(..., examples=[...])` on the *schema*. Most OpenAPI
    # readers -- including the client generators an agent-built connector leans
    # on -- look for named examples on the operation instead, so they are hoisted
    # here with labels that say which shape each one is. Without this the two
    # documented shapes were present-but-invisible to exactly the consumer they
    # were written for.
    _labels = {
        ("/v1/transcripts", "post"): ["浏览器回传的 segments", "SRT/VTT 原文"],
        ("/v1/jobs", "post"): ["最小任务", "带去重窗口"],
    }
    _generate_openapi = app.openapi

    def _openapi_with_operation_examples() -> dict:
        spec = _generate_openapi()
        for path, item in (spec.get("paths") or {}).items():
            for method, operation in item.items():
                content = ((operation.get("requestBody") or {}).get("content") or {}).get(
                    "application/json")
                if not isinstance(content, dict):
                    continue
                examples = (content.get("schema") or {}).get("examples")
                if not isinstance(examples, list) or not examples:
                    continue
                names = _labels.get((path, method)) or [f"example {i + 1}" for i in range(len(examples))]
                content["examples"] = {
                    name: {"value": value}
                    for name, value in zip(names, examples, strict=True)
                }
        return spec

    app.openapi = _openapi_with_operation_examples

    @app.get("/healthz", tags=["meta"], operation_id="health_check")
    def healthz() -> dict[str, Any]:
        """Unauthenticated liveness probe, also the deploy-verification target."""
        return {
            "ok": True,
            "service": "pickvedio-core",
            "version": __version__,
            "uptime_seconds": round(time.time() - running["started_at"], 1),
            "auth_required": bool(settings.api_token),
            "llm_configured": bool(settings.llm.api_key or settings.ollama.enabled),
            "llm_model": settings.llm.model or settings.ollama.model,
            "asr_configured": settings.asr.enabled,
            "asr_provider": settings.asr.provider,
            "workers": workers._max_workers,  # noqa: SLF001 - operator-visible sizing
            "inbound_token_missing": not settings.api_token,
        }

    @app.get("/v1/profiles", tags=["meta"], operation_id="list_profiles",
           dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def profiles() -> Any:
        return JSONResponse({"ok": True, "code": "ok", "message": "可用总结档位",
                             "profiles": list_profiles(), "labels": PROFILE_LABELS,
                             "default": settings.style})

    @app.get("/v1/capabilities", tags=["meta"], operation_id="get_capabilities",
               dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def capabilities() -> Any:
        return JSONResponse({
            "ok": True, "code": "ok", "message": "本部署当前可用的取稿通道",
            "channels": describe_channels(settings),
            "unconfigured": [
                key for key, present in (
                    ("PVC_LLM_API_KEY", bool(settings.llm.api_key)),
                    ("PVC_API_TOKEN", bool(settings.api_token)),
                    ("PVC_ASR_PROVIDER", settings.asr.enabled),
                    ("PVC_BILIBILI_SESSDATA/PVC_COOKIE_FILE", bool(settings.sessdata or settings.cookie_file)),
                ) if not present
            ],
        })

    @app.get("/v1/metadata", tags=["meta"], operation_id="get_metadata",
           dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def metadata(url: str = Query(..., description="视频链接或包含链接的分享文案")) -> Any:
        canonical = validate_video_url(url, allow_unknown_platform=settings.allow_unknown_platform)
        ref = classify(canonical)
        data: dict[str, Any] = {"url": canonical, "platform": ref.platform, "video_id": ref.video_id}
        if ref.platform == "bilibili":
            from pickvedio_core.sources.bilibili import BilibiliClient

            video = BilibiliClient(sessdata=settings.sessdata, cookie_file=settings.cookie_file).video(canonical)
            tracks = []
            try:
                tracks = video and BilibiliClient(
                    sessdata=settings.sessdata, cookie_file=settings.cookie_file
                ).subtitle_tracks(video)
            except PickvedioError:
                tracks = []
            data.update({
                "title": video.title, "duration": video.duration, "pages": len(video.pages),
                "uploader": video.uploader, "thumbnail": video.thumbnail,
                "subtitle_tracks": [{"lan": item.get("lan"), "ai_type": item.get("ai_type")} for item in tracks],
                "hint": (None if tracks else "匿名会话看不到字幕轨道；请用浏览器回传或配置登录态。"),
            })
        elif ref.platform == "youtube":
            from pickvedio_core.sources.youtube import YoutubeClient

            data.update(YoutubeClient(cookie_file=settings.cookie_file).oembed(canonical))
            data["hint"] = "oEmbed 只提供标题与作者；字幕需浏览器回传或已登录会话。"
        return JSONResponse({"ok": True, "code": "ok", "message": "元数据", **data})

    # -- jobs ----------------------------------------------------------------

    @app.post("/v1/jobs", tags=["jobs"], operation_id="create_job",
            dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def create_job(body: dict = Body(
        ...,
        examples=[
            {"url": "https://www.bilibili.com/video/BV1rjKn6CETw/", "style": "general"},
            {"url": "https://www.bilibili.com/video/BV1rjKn6CETw/", "style": "brief",
             "dedup_minutes": 120},
        ],
    )) -> Any:
        url = str(body.get("url") or "")
        style = style_or_400(str(body.get("style") or ""))
        canonical = validate_video_url(url, allow_unknown_platform=settings.allow_unknown_platform)
        relay_payload, meta = relay_fields(body)
        if relay_payload is not None:
            meta = {**meta, "origin": "http-relay-via-jobs"}
            return submit(canonical, style, relay_payload=relay_payload, relay_meta=meta)
        return submit(
            canonical, style,
            dedup_minutes=int(body.get("dedup_minutes", 60) or 0),
        )

    @app.get("/v1/jobs", tags=["jobs"], operation_id="list_jobs",
           dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def list_jobs(limit: int = Query(30, ge=1, le=200), status: str = Query("")) -> Any:
        rows = store.list(limit=limit, status=status)
        return JSONResponse({"ok": True, "code": "ok", "message": f"共 {len(rows)} 条任务",
                             "jobs": [row.as_dict(include_report=False) for row in rows]})

    @app.get("/v1/jobs/{job_id}", tags=["jobs"], operation_id="job_status",
           dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def job_status(job_id: str) -> Any:
        record = store.get(job_id)
        return JSONResponse({
            "ok": record.status != "failed",
            "code": record.error_code or "ok",
            "message": record.message,
            **record.as_dict(include_report=False),
        })

    @app.get("/v1/jobs/{job_id}/report", tags=["jobs"], operation_id="job_report",
           dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def job_report(job_id: str) -> Any:
        record = store.get(job_id)
        if not record.report:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False, "code": "not_ready", "job_id": job_id,
                    "message": f"任务仍在 {record.stage}（进度 {record.progress:.0%}）：{record.message}",
                    "progress": record.progress, "stage": record.stage, "status": record.status,
                },
            )
        return JSONResponse({"ok": True, "code": "ok", "message": "报告", **record.report})

    @app.post("/v1/jobs/{job_id}/cancel", tags=["jobs"], operation_id="cancel_job",
            dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def cancel_job(job_id: str) -> Any:
        record = store.get(job_id)
        flag = cancel_flags.get(job_id)
        if flag is None:
            return {"ok": True, "code": "not_running", "job_id": job_id,
                    "message": f"任务不在本实例运行中（当前状态 {record.stage}）。"}
        flag.set()
        store.update(job_id, status="canceled", stage="canceled", message="已请求取消")
        return {"ok": True, "code": "ok", "job_id": job_id, "message": "已请求取消。"}

    # -- relayed transcripts -------------------------------------------------

    @app.post("/v1/transcripts", tags=["transcripts"], operation_id="submit_transcript",
              dependencies=[Depends(require_token), Depends(bearer_scheme)])
    def transcripts(body: dict = Body(
        ...,
        examples=[
            {"url": "https://www.bilibili.com/video/BV1rjKn6CETw/", "style": "brief",
             "wait": True,
             "segments": [{"start": 0.0, "end": 12.0, "text": "开场给出今天要讲的三件事。"}]},
            {"url": "https://www.bilibili.com/video/BV1rjKn6CETw/",
             "transcript": "[00:00:00,000 --> 00:00:12,000]\n开场给出今天要讲的三件事。\n",
             "title": "示例视频", "duration": 60, "source_kind": "up_subtitles"},
        ],
    )) -> Any:
        """Accept a transcript obtained elsewhere and summarise it.

        Recommended default route for an agent: no media pipeline, no platform
        bot check, no ASR cost, and it completes in seconds because the only
        remaining work is the summarisation itself. Pass ``"wait": true`` to get
        the report inline instead of a job id.
        """
        url = str(body.get("url") or "")
        canonical = validate_video_url(url, allow_unknown_platform=settings.allow_unknown_platform)
        style = style_or_400(str(body.get("style") or ""))
        relay_payload, meta = relay_fields(body)
        if relay_payload is None:
            raise HTTPException(status_code=422, detail={
                "ok": False, "code": "empty_transcript", "message": "请求里没有 `transcript` 或 `segments` 字段。",
                "hint": "可提交 {\"segments\":[{\"start\":0,\"end\":4,\"text\":\"…\"}]}、"
                        "{\"transcript\":\"<SRT/VTT/JSON3 原文>\"}，"
                        "或浏览器提取脚本的完整输出对象（会自识别 title / duration / sourceKind）。",
            })
        if body.get("wait"):
            outcome = pipeline_run(
                canonical, settings, style=style, job_id="http-sync",
                relay_payload=relay_payload, relay_meta={**meta, "origin": "http-relay-sync"},
            )
            return JSONResponse({"ok": True, "code": "ok", "message": "总结完成（同步）",
                                 "seconds": round(outcome.seconds, 2),
                                 **report_payload(outcome.report)})
        return submit(canonical, style, relay_payload=relay_payload, relay_meta=meta)

    # -- error translation ---------------------------------------------------

    @app.exception_handler(PickvedioError)
    async def pickvedio_handler(request: Request, exc: PickvedioError) -> Any:
        content = {
            "ok": False,
            "code": getattr(exc, "code", "error"),
            "message": str(exc)[:1500],
            "hint": getattr(exc, "hint", "") or None,
            "retryable": bool(getattr(exc, "retryable", False)),
        }
        # A throttle additionally says how long the endpoint asked for, when it
        # said so in whole seconds. Absent otherwise, rather than a null the
        # caller has to distinguish from "zero".
        wait = getattr(exc, "retry_after_seconds", None)
        if wait:
            content["retry_after_seconds"] = wait
        return JSONResponse(status_code=status_for(exc), content=content)

    # No explicit shutdown hook: ThreadPoolExecutor registers its own atexit
    # join, and FastAPI's on_event is deprecated. A container SIGTERM therefore
    # drains in-flight jobs the same way uvicorn already drains connections.
    return app


# Railway/Render/Fly run `uvicorn service.app:app`; building the app at import
# time keeps that free of a factory call, and PVC_AUTOCREATE=0 skips it for the
# tests that construct their own settings.
app = create_app() if os.environ.get("PVC_AUTOCREATE", "1") == "1" else None
