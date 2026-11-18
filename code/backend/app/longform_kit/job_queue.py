"""Persistent longform job queue: per-user and global render limits (BG)."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
_AVG_JOB_SECONDS = 420.0


@dataclass(frozen=True)
class QueueStatus:
    position: int
    ahead: int
    estimated_start_at: float
    cancelable: bool


class LongformJobQueue:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS longform_jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    render_active INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_lf_jobs_status ON longform_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_lf_jobs_user ON longform_jobs(user_id, status);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(longform_jobs)").fetchall()}
            # Which process runs the job and when it last said so: with several workers, only a
            # job whose runner went quiet goes back to the queue (not one another worker runs).
            if "worker_id" not in columns:
                conn.execute("ALTER TABLE longform_jobs ADD COLUMN worker_id TEXT")
            if "heartbeat_at" not in columns:
                conn.execute("ALTER TABLE longform_jobs ADD COLUMN heartbeat_at REAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def register(self, job_id: str, user_id: str) -> None:
        uid = user_id or "anonymous"
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO longform_jobs (job_id, user_id, status, created_at) VALUES (?, ?, ?, ?)",
                (job_id, uid, QUEUED, time.time()),
            )

    def _running_user(self, conn: sqlite3.Connection, user_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM longform_jobs WHERE user_id = ? AND status = ?",
            (user_id or "anonymous", RUNNING),
        ).fetchone()
        return int(row[0])

    def _running_global(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) FROM longform_jobs WHERE status = ?", (RUNNING,)).fetchone()
        return int(row[0])

    def _render_active_global(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM longform_jobs WHERE status = ? AND render_active = 1",
            (RUNNING,),
        ).fetchone()
        return int(row[0])

    def queue_status(self, settings: Settings, job_id: str) -> QueueStatus | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT user_id, status, created_at FROM longform_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            _user, status, created_at = row
            if status != QUEUED:
                return QueueStatus(position=0, ahead=0, estimated_start_at=time.time(), cancelable=False)
            ahead = conn.execute(
                "SELECT COUNT(*) FROM longform_jobs WHERE status = ? AND created_at < ?",
                (QUEUED, created_at),
            ).fetchone()[0]
            position = int(ahead) + 1
            eta = time.time() + position * _AVG_JOB_SECONDS / max(1, settings.longform_max_concurrent_jobs_global)
            return QueueStatus(
                position=position,
                ahead=int(ahead),
                estimated_start_at=eta,
                cancelable=True,
            )

    def cancel_queued(self, job_id: str) -> bool:
        now = time.time()
        with closing(self._connect()) as conn:
            updated = conn.execute(
                "UPDATE longform_jobs SET status = ?, finished_at = ?, cancel_requested = 1 "
                "WHERE job_id = ? AND status = ?",
                (CANCELLED, now, job_id, QUEUED),
            ).rowcount
        return bool(updated)

    def claim_next(self, settings: Settings, owner: str = "") -> str | None:
        per_user = max(1, int(settings.longform_max_concurrent_jobs_per_user))
        global_cap = max(1, int(settings.longform_max_concurrent_jobs_global))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._running_global(conn) >= global_cap:
                    return None
                rows = conn.execute(
                    "SELECT job_id, user_id FROM longform_jobs WHERE status = ? ORDER BY created_at",
                    (QUEUED,),
                ).fetchall()
                for job_id, user_id in rows:
                    if self._running_user(conn, user_id) >= per_user:
                        continue
                    now = time.time()
                    updated = conn.execute(
                        "UPDATE longform_jobs SET status = ?, started_at = ?, worker_id = ?, heartbeat_at = ? "
                        "WHERE job_id = ? AND status = ?",
                        (RUNNING, now, owner or None, now, job_id, QUEUED),
                    ).rowcount
                    if updated:
                        return str(job_id)
                return None
            finally:
                conn.execute("COMMIT")

    def mark_done(self, job_id: str, *, failed: bool = False) -> None:
        status = FAILED if failed else DONE
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE longform_jobs SET status = ?, render_active = 0, finished_at = ? WHERE job_id = ?",
                (status, time.time(), job_id),
            )

    def try_acquire_render_slot(self, settings: Settings, job_id: str) -> bool:
        cap = max(1, int(settings.longform_max_concurrent_renders))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._render_active_global(conn) >= cap:
                conn.execute("COMMIT")
                return False
            conn.execute(
                "UPDATE longform_jobs SET render_active = 1 WHERE job_id = ? AND status = ?",
                (job_id, RUNNING),
            )
            conn.execute("COMMIT")
        return True

    def release_render_slot(self, job_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE longform_jobs SET render_active = 0 WHERE job_id = ?", (job_id,))

    def heartbeat(self, owner: str, job_ids: list[str]) -> None:
        """The runner is alive: keep its running jobs from being taken back."""
        if not job_ids:
            return
        marks = ",".join("?" for _ in job_ids)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE longform_jobs SET heartbeat_at = ? WHERE status = ? AND worker_id = ? AND job_id IN ({marks})",
                (time.time(), RUNNING, owner, *job_ids),
            )

    def requeue_stale(self, stale_seconds: float, *, keep: list[str] | None = None) -> list[str]:
        """Running jobs whose runner stopped checking in (a worker that died or was stopped) go back
        to the queue; the pipeline resumes from its job.json checkpoints. `keep`: this runner's own."""
        cutoff = time.time() - max(1.0, stale_seconds)
        kept = set(keep or [])
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT job_id FROM longform_jobs WHERE status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                    (RUNNING, cutoff),
                ).fetchall()
                ids = [str(row[0]) for row in rows if str(row[0]) not in kept]
                for job_id in ids:
                    conn.execute(
                        "UPDATE longform_jobs SET status = ?, render_active = 0, started_at = NULL, worker_id = NULL "
                        "WHERE job_id = ? AND status = ?",
                        (QUEUED, job_id, RUNNING),
                    )
            finally:
                conn.execute("COMMIT")
        return ids

    def requeue_running_as_queued(self) -> list[str]:
        """After restart: running rows go back to queued (AG resume uses job.json checkpoints)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT job_id FROM longform_jobs WHERE status = ?",
                (RUNNING,),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            if ids:
                conn.execute(
                    "UPDATE longform_jobs SET status = ?, render_active = 0, started_at = NULL "
                    "WHERE status = ?",
                    (QUEUED, RUNNING),
                )
        return ids


_queues: dict[Path, LongformJobQueue] = {}


def get_longform_queue(settings: Settings) -> LongformJobQueue:
    path = settings.longform_queue_db_path
    queue = _queues.get(path)
    if queue is None:
        queue = _queues[path] = LongformJobQueue(path)
    return queue


def attach_queue_fields(settings: Settings, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.longform_queue_enabled:
        return payload
    status = get_longform_queue(settings).queue_status(settings, job_id)
    if status and status.position > 0:
        payload = {
            **payload,
            "queue_position": status.position,
            "queue_ahead": status.ahead,
            "queue_estimated_start_at": round(status.estimated_start_at, 3),
            "queue_cancelable": status.cancelable,
        }
    return payload
