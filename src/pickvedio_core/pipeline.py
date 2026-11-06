"""End-to-end orchestration: URL in, report out.

The desktop app's ``VideoPipeline`` did one thing per stage and reported
progress to Qt signals. This is the same shape with two structural changes:

* the transcript stage is the ladder (subtitles / relay / ASR / page text)
  instead of "always download and always run Whisper", which is what makes the
  hosted workflow free and fast for subtitled videos;
* progress is a plain callback plus an optional :class:`~.jobs.JobStore`, so the
  identical function serves the CLI, the worker thread and tests.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from .config import Settings
from .errors import CancelledError, EmptyTranscript, PickvedioError
from .jobs import JobRecord, JobStore
from .llm import Usage, build_client
from .models import JobStage, Report, Transcript
from .prompts import PromptSet, load as load_prompts
from .report import markdown, payload, write_all
from .sources.ladder import Resolution, resolve
from .summarize import Summarizer, estimate_calls
from .urls import classify, validate_video_url

logger = logging.getLogger("pickvedio.pipeline")

Progress = Callable[[float, str], None]


@dataclass(slots=True)
class RunOutcome:
    report: Report
    resolution: Resolution
    artifacts: dict[str, str]
    markdown_text: str
    usage: dict[str, Any]
    seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            **payload(self.report),
            "artifacts": self.artifacts,
            "attempts": self.resolution.trail,
            "seconds": round(self.seconds, 2),
            "usage": self.usage,
        }


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("任务已取消。")


def summarize_transcript(
    transcript: Transcript,
    settings: Settings,
    *,
    style: str = "",
    cancel_event=None,
    on_progress: Progress | None = None,
    client=None,
) -> tuple[Report, PromptSet, Usage]:
    """Run the layered summarisation over a transcript that already exists."""
    prompt_set = load_prompts(style or settings.style, settings.prompt_dir, settings.profile_prompts)
    usage = Usage()
    if client is None:
        client = build_client(settings, usage)
    model = getattr(settings.llm, "model", "") or getattr(
        getattr(settings, "ollama", None), "model", ""
    )
    summarizer = Summarizer(
        prompt_set=prompt_set,
        client=client,
        chunk_chars=settings.chunk_chars,
        max_chunks=settings.max_chunks,
    )
    emit = on_progress or (lambda ratio, message: None)
    plan = transcript.segments
    chunks_preview = summarizer.chunks(plan)
    logger.info(
        "开始分层总结 title=%r segments=%s chunks=%s est_calls=%s",
        transcript.title, len(plan), len(chunks_preview), estimate_calls(len(chunks_preview))["total"],
    )
    chunks, partials, final = summarizer.run(
        transcript.title,
        transcript.duration,
        plan,
        cancel=lambda: _check_cancel(cancel_event),
        on_progress=emit,
    )
    report = Report(
        url=transcript.video_url,
        title=transcript.title,
        duration=transcript.duration,
        source=transcript.source,
        language=transcript.language,
        final_summary=final,
        chunk_summaries=partials,
        chunks=chunks,
        segments=plan,
        model=str(model or ""),
        stage_seconds=dict(summarizer.timings),
        usage={**usage.as_dict(), **{f"chunk_{k}": v for k, v in estimate_calls(len(chunks)).items()}},
    )
    return report, prompt_set, usage


def run(
    url: str,
    settings: Settings,
    *,
    style: str = "",
    job_id: str = "",
    store: JobStore | None = None,
    cancel_event=None,
    on_progress: Progress | None = None,
    relay_payload: Any = None,
    relay_meta: dict[str, Any] | None = None,
    write_artifacts: bool = True,
    allow_asr: bool = True,
    allow_page_text: bool = False,
    client=None,
) -> RunOutcome:
    """Resolve a transcript, summarise it, and render every artefact.

    The single entry point every front end (CLI, HTTP service, tests) uses, so
    no front end can quietly skip a validation the others enforce.
    """
    started = time.monotonic()
    emit = on_progress or (lambda ratio, message: None)

    def stage(ratio: float, message: str, name: str | None = None) -> None:
        emit(ratio, message)
        if store is not None and job_id:
            store.progress(job_id, ratio, message, stage=name or JobStage.SUMMARIZING.value)

    canonical = validate_video_url(url, allow_unknown_platform=settings.allow_unknown_platform)
    ref = classify(canonical)
    logger.info("任务开始 url=%s platform=%s style=%s", canonical, ref.platform, style or settings.style)

    stage(0.03, "正在获取逐字稿", JobStage.RESOLVING.value)
    workdir = settings.job_dir(job_id or "adhoc")
    resolution = resolve(
        canonical,
        settings,
        relay_payload=relay_payload,
        relay_meta=relay_meta,
        workdir=workdir,
        on_progress=lambda ratio, message: stage(0.05 + ratio * 0.4, message, JobStage.TRANSCRIBING.value),
        cancel=lambda: _check_cancel(cancel_event),
        allow_asr=allow_asr,
        allow_page_text=allow_page_text,
    )
    transcript = resolution.transcript
    if not transcript.segments:
        raise EmptyTranscript("取得的逐字稿为空，拒绝生成总结。")
    if store is not None and job_id:
        store.update(
            job_id,
            title=transcript.title,
            duration=transcript.duration,
            platform=ref.platform,
            transcript_source=transcript.source.value,
            attempts=resolution.trail,
            message=f"已取得 {len(transcript.segments)} 段逐字稿，开始分层总结",
            stage=JobStage.SUMMARIZING.value,
        )

    stage(0.5, f"逐字稿就绪（{transcript.char_count} 字），开始分层总结", JobStage.SUMMARIZING.value)
    report, prompt_set, usage = summarize_transcript(
        transcript,
        settings,
        style=style,
        cancel_event=cancel_event,
        on_progress=lambda ratio, message: stage(0.5 + ratio * 0.45, message, JobStage.SUMMARIZING.value),
        client=client,
    )
    report.warnings = list(resolution.warnings) + report.warnings
    report.job_id = job_id
    report.usage = {**usage.as_dict(), **report.usage, "total_seconds": round(time.monotonic() - started, 2)}

    artifacts: dict[str, str] = {}
    if write_artifacts:
        stage(0.98, "正在写出报告与逐字稿文件", JobStage.FINALIZING.value)
        artifacts = write_all(report, workdir / "summary", style_label=prompt_set.label)

    seconds = time.monotonic() - started
    outcome = RunOutcome(
        report=report,
        resolution=resolution,
        artifacts=artifacts,
        markdown_text=markdown(report, style_label=prompt_set.label),
        usage=report.usage,
        seconds=seconds,
    )
    if store is not None and job_id:
        store.complete(job_id, payload(report), artifacts=artifacts, usage=report.usage)
    logger.info("任务完成 job=%s 用时 %.1fs 字=%s", job_id or "-", seconds, report.chars)
    return outcome


def run_recorded(
    url: str,
    settings: Settings,
    store: JobStore,
    *,
    style: str = "",
    relay_payload: Any = None,
    relay_meta: dict[str, Any] | None = None,
    dedup_minutes: int = 0,
) -> JobRecord:
    """Create a job row, run it, and guarantee the row reaches a terminal state."""
    record = store.create(
        validate_video_url(url, allow_unknown_platform=settings.allow_unknown_platform),
        platform=classify(url).platform,
        style=style or settings.style,
        dedup_minutes=dedup_minutes,
    )
    if record.report:
        return record
    try:
        outcome = run(
            url,
            settings,
            style=style,
            job_id=record.id,
            store=store,
            relay_payload=relay_payload,
            relay_meta=relay_meta,
        )
        return store.get(record.id) if outcome.artifacts else record
    except PickvedioError as exc:
        code = getattr(exc, "code", "error")
        stage = JobStage.NEEDS_BROWSER.value if code in ("needs_browser", "needs_auth") else JobStage.FAILED.value
        store.fail(record.id, str(exc), code=code, stage=stage)
        logger.warning("任务失败 job=%s code=%s：%s", record.id, code, exc)
        raise
    except Exception as exc:  # unexpected: keep a short diagnostic, as the app did
        store.fail(
            record.id,
            f"未预期错误：{exc.__class__.__name__}: {str(exc)[:400]}",
            code="internal",
        )
        logger.exception("未预期错误 job=%s", record.id)
        raise
