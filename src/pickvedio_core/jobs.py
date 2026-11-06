"""Job store.

Free-tier hosts restart, sleep and evict containers, and a 3-hour video takes
longer than a request lives. So job state is durable and readable from outside
the process that created it: a single SQLite file under ``PVC_WORKDIR``.

Deliberate limits, stated so nobody mistakes them for features: this is not a
queue, not a scheduler and not multi-instance safe beyond local file locking.
Two workers on the same database will both pick up work; a deployment that
needs real concurrency should run one worker per volume, which is also what
Railway/Render/Fly free tiers give you.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import JobNotFound
from .models import JobStage

logger = logging.getLogger("pickvedio.jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    platform TEXT DEFAULT '',
    style TEXT DEFAULT 'general',
    title TEXT DEFAULT '',
    duration REAL,
    stage TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0.0,
    message TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    error_code TEXT DEFAULT '',
    error TEXT DEFAULT '',
    transcript_source TEXT DEFAULT '',
    report_json TEXT DEFAULT '',
    artifacts_json TEXT DEFAULT '',
    usage_json TEXT DEFAULT '',
    attempts_json TEXT DEFAULT '[]',
    warnings_json TEXT DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_url_idx ON jobs (url, created_at DESC);
"""

TERMINAL = {JobStage.COMPLETED.value, JobStage.FAILED.value, JobStage.CANCELED.value}


@dataclass(slots=True)
class JobRecord:
    url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    platform: str = ""
    style: str = "general"
    title: str = ""
    duration: float | None = None
    stage: str = JobStage.QUEUED.value
    progress: float = 0.0
    message: str = "排队中"
    status: str = "queued"
    error_code: str = ""
    error: str = ""
    transcript_source: str = ""
    report: dict[str, Any] | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self, *, include_report: bool = True) -> dict[str, Any]:
        out = {
            "job_id": self.id,
            "url": self.url,
            "platform": self.platform,
            "style": self.style,
            "title": self.title,
            "duration": self.duration,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "message": self.message,
            "status": self.status,
            "transcript_source": self.transcript_source,
            "artifacts": self.artifacts,
            "usage": self.usage,
            "attempts": self.attempts,
            "warnings": self.warnings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.created_at, 1),
        }
        if self.error:
            out["error"] = self.error
            out["error_code"] = self.error_code
        if include_report and self.report:
            out["report"] = self.report
        return out


class JobStore:
    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.database), timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            self._local.connection = connection
        return connection

    # -- create / update -----------------------------------------------------

    def create(self, url: str, *, platform: str = "", style: str = "general",
               dedup_minutes: int = 0) -> JobRecord:
        """Create a job, optionally reusing a completed one for the same link.

        ``dedup_minutes`` exists because a phone user re-sending the same link
        ten minutes later should not pay for the same summarisation twice.
        """
        if dedup_minutes > 0:
            cutoff = time.time() - dedup_minutes * 60
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE url = ? AND status = 'completed' "
                    "AND finished_at > ? ORDER BY finished_at DESC LIMIT 1",
                    (url, cutoff),
                ).fetchone()
            if row is not None:
                record = self._row_to_record(row)
                record.id = uuid.uuid4().hex[:16]
                record.message = f"复用 {record.id} 的既有结果（同链接 {dedup_minutes} 分钟内）"
                record.warnings = list(record.warnings) + [
                    f"结果来自缓存任务 {row['id']}，非本次重新生成。"
                ]
                return record
        record = JobRecord(url=url, platform=platform, style=style)
        self._write(record)
        return record

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        record = self.get(job_id)
        for key, value in changes.items():
            if key in ("report", "artifacts", "usage", "attempts", "warnings", "status", "stage") or hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = time.time()
        if record.stage in TERMINAL and record.finished_at is None:
            record.finished_at = record.updated_at
        self._write(record)
        return record

    def progress(self, job_id: str, ratio: float, message: str, stage: str | None = None) -> None:
        changes: dict[str, Any] = {"progress": max(0.0, min(1.0, float(ratio))), "message": message}
        if stage:
            changes["stage"] = stage
        self.update(job_id, **changes)

    def complete(self, job_id: str, report: dict[str, Any], *, artifacts: dict[str, str] | None = None,
                 usage: dict[str, Any] | None = None) -> JobRecord:
        return self.update(
            job_id,
            status="completed",
            stage=JobStage.COMPLETED.value,
            progress=1.0,
            message="已完成",
            report=report,
            artifacts=artifacts or {},
            usage=usage or {},
            transcript_source=str(report.get("transcript_source") or ""),
            title=str(report.get("title") or ""),
            duration=report.get("duration"),
            warnings=list(report.get("warnings") or []),
        )

    def fail(self, job_id: str, error: str, *, code: str = "error",
             stage: str = JobStage.FAILED.value, attempts: list[dict[str, Any]] | None = None) -> JobRecord:
        return self.update(
            job_id, status="failed", stage=stage, error=str(error)[:2000], error_code=code,
            message="失败", attempts=attempts or [],
        )

    # -- read ----------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(f"没有任务 {job_id}。")
        return self._row_to_record(row)

    def list(self, *, status: str = "", limit: int = 50) -> list[JobRecord]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def find_by_url(self, url: str) -> JobRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE url = ? ORDER BY created_at DESC LIMIT 1", (url,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def prune(self, keep_days: float = 14.0) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM jobs WHERE updated_at < ? AND status IN ('failed','canceled','completed')",
                (cutoff,),
            )
        return cursor.rowcount or 0

    # -- helpers -------------------------------------------------------------

    def _write(self, record: JobRecord) -> None:
        payload = {
            "id": record.id, "url": record.url, "platform": record.platform, "style": record.style,
            "title": record.title, "duration": record.duration, "stage": record.stage,
            "progress": record.progress, "message": record.message, "status": record.status,
            "error_code": record.error_code, "error": record.error,
            "transcript_source": record.transcript_source,
            "report_json": json.dumps(record.report, ensure_ascii=False) if record.report else "",
            "artifacts_json": json.dumps(record.artifacts, ensure_ascii=False),
            "usage_json": json.dumps(record.usage, ensure_ascii=False),
            "attempts_json": json.dumps(record.attempts, ensure_ascii=False),
            "warnings_json": json.dumps(record.warnings, ensure_ascii=False),
            "created_at": record.created_at, "updated_at": record.updated_at,
            "finished_at": record.finished_at,
        }
        columns = ", ".join(payload)
        marks = ", ".join("?" * len(payload))
        updates = ", ".join(f"{key}=excluded.{key}" for key in payload if key != "id")
        statement = (
            f"INSERT INTO jobs ({columns}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        with self._lock, self._connect() as connection:
            connection.execute(statement, list(payload.values()))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        def loads(value: str, default: Any) -> Any:
            if not value:
                return default
            try:
                return json.loads(value)
            except ValueError:
                return default

        return JobRecord(
            id=row["id"], url=row["url"], platform=row["platform"] or "", style=row["style"] or "general",
            title=row["title"] or "", duration=row["duration"], stage=row["stage"],
            progress=float(row["progress"] or 0.0), message=row["message"] or "",
            status=row["status"], error_code=row["error_code"] or "", error=row["error"] or "",
            transcript_source=row["transcript_source"] or "",
            report=loads(row["report_json"], None), artifacts=loads(row["artifacts_json"], {}),
            usage=loads(row["usage_json"], {}), attempts=loads(row["attempts_json"], []),
            warnings=loads(row["warnings_json"], []),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            finished_at=row["finished_at"],
        )


def utc_stamp(seconds: float | None = None) -> str:
    return datetime.fromtimestamp(seconds or time.time(), timezone.utc).isoformat(timespec="seconds")
