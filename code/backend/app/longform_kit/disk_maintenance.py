"""Longform upload/job disk usage and TTL cleanup (AN)."""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.longform_kit.store import longform_root, uploads_root

logger = logging.getLogger(__name__)

# A job is finished only in these states; anything else (queue_wait, transcribing, highlighting,
# rendering — also a caption re-burn) is still being worked on and is left alone.
FINISHED = frozenset({"completed", "failed"})
# Scratch a finished job no longer needs. Kept: short_*.mp4, segment_*.mp4 (caption re-burn),
# source.mp4 (FCPXML export), transcript and job.json.
SCRATCH_DIRS = ("_work", "export", "*_prep", "*_broll", "render_*", "stt")
SCRATCH_FILES = ("*.crop.txt", "*.trf", "*.stabpass.mp4", "concat.txt")
# What job.json keeps once its files are gone: enough for the page to say why, nothing of the video.
EXPIRED_KEEP_KEYS = ("job_id", "upload_id", "user_id", "language")


@dataclass
class LongformCleanupReport:
    removed_dirs: int = 0
    removed_files: int = 0
    freed_bytes: int = 0
    skipped_active: int = 0
    expired_jobs: int = 0
    targets: list[str] = field(default_factory=list)


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def longform_disk_summary(settings: Settings) -> dict:
    root = longform_root(settings)
    jobs = root / "jobs"
    uploads = uploads_root(settings)
    job_bytes = _dir_bytes(jobs)
    upload_bytes = _dir_bytes(uploads)
    free_gb = round(shutil.disk_usage(root).free / (1024**3), 2)
    return {
        "longform_jobs_bytes": job_bytes,
        "longform_uploads_bytes": upload_bytes,
        "disk_free_gb": free_gb,
        "min_free_gb": settings.longform_min_free_disk_gb,
    }


def _load_state(work: Path) -> dict | None:
    try:
        state = json.loads((work / "job.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _job_running(work: Path) -> bool:
    state = _load_state(work)
    if state is None:
        return False
    status = str(state.get("status") or "").lower()
    export_status = str((state.get("export") or {}).get("status") or "").lower()
    return status not in FINISHED or export_status == "running"


def _has_result(work: Path, state: dict | None) -> bool:
    if state is not None and str(state.get("status") or "").lower() == "completed":
        return True
    return any(work.glob("short_*.mp4")) or (work / "export.zip").is_file()


def _remove(path: Path, job: Path, report: LongformCleanupReport, *, dry_run: bool) -> None:
    report.targets.append(f"{job.name}/{path.name}")
    if path.is_dir():
        report.freed_bytes += _dir_bytes(path)
        report.removed_dirs += 1
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
        return
    try:
        report.freed_bytes += path.stat().st_size
    except OSError:
        pass
    report.removed_files += 1
    if not dry_run:
        path.unlink(missing_ok=True)


def expired_message(settings: Settings) -> str:
    return (
        f"보관 기간({settings.longform_retention_final_days}일)이 지나 결과 파일을 지웠어요. "
        "영상을 다시 올려 만들어 주세요."
    )


def _expire_job(
    settings: Settings, job: Path, state: dict | None, report: LongformCleanupReport, *, dry_run: bool
) -> None:
    """Delete everything but job.json, and leave job.json saying the files are gone."""
    for path in job.iterdir():
        if path.name != "job.json":
            _remove(path, job, report, dry_run=dry_run)
    report.expired_jobs += 1
    if dry_run:
        return
    previous = state or {}
    failed_before = str(previous.get("status") or "").lower() == "failed" and previous.get("error")
    kept = {key: previous[key] for key in EXPIRED_KEEP_KEYS if key in previous}
    kept.update(
        {
            "job_id": kept.get("job_id") or job.name,
            "status": "failed",
            "error": str(previous["error"]) if failed_before else expired_message(settings),
            "expired_at": int(time.time()),
        }
    )
    (job / "job.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_job(settings: Settings, job: Path, now: float, report: LongformCleanupReport, *, dry_run: bool) -> None:
    state = _load_state(job)
    if state is not None and state.get("expired_at"):
        return
    job_json = job / "job.json"
    age = now - (job_json.stat().st_mtime if job_json.is_file() else job.stat().st_mtime)
    final_ttl = settings.longform_retention_final_days * 86400
    if _job_running(job):
        # Nothing has touched a live job for the whole retention period: the run is dead
        # (a dead worker's job is picked up again within minutes).
        if age < final_ttl:
            report.skipped_active += 1
            return
        _expire_job(settings, job, state, report, dry_run=dry_run)
        return
    has_result = _has_result(job, state)
    keep_for = final_ttl if has_result else settings.longform_retention_work_hours * 3600
    if age >= keep_for:
        _expire_job(settings, job, state, report, dry_run=dry_run)
        return
    if age < settings.longform_retention_work_hours * 3600:
        return
    for pattern in (*SCRATCH_DIRS, *SCRATCH_FILES):
        for path in job.glob(pattern):
            _remove(path, job, report, dry_run=dry_run)


def run_longform_cleanup(settings: Settings, *, dry_run: bool = False) -> LongformCleanupReport:
    """Uploads after `longform_retention_upload_days`. A finished job's scratch after
    `longform_retention_work_hours`; its videos and zip after `longform_retention_final_days`
    (a failed job's files after the scratch time). Running jobs are never touched."""
    report = LongformCleanupReport()
    now = time.time()
    upload_ttl = settings.longform_retention_upload_days * 86400

    for upload in uploads_root(settings).iterdir():
        if not upload.is_dir():
            continue
        age = now - upload.stat().st_mtime
        if age < upload_ttl:
            continue
        size = _dir_bytes(upload)
        report.targets.append(f"upload {upload.name} ~{size // 1024}KB")
        if not dry_run:
            shutil.rmtree(upload, ignore_errors=True)
        report.removed_dirs += 1
        report.freed_bytes += size

    jobs_root = longform_root(settings) / "jobs"
    if jobs_root.is_dir():
        for job in jobs_root.iterdir():
            if job.is_dir():
                _clean_job(settings, job, now, report, dry_run=dry_run)
    logger.info(
        "Longform cleanup dry_run=%s dirs=%s files=%s freed=%s skipped=%s expired=%s",
        dry_run,
        report.removed_dirs,
        report.removed_files,
        report.freed_bytes,
        report.skipped_active,
        report.expired_jobs,
    )
    return report


def longform_job_metrics(settings: Settings) -> dict:
    """AO: aggregate job.json under longform/jobs (empty-safe)."""
    jobs_root = longform_root(settings) / "jobs"
    totals = {"jobs": 0, "done": 0, "failed": 0, "running": 0}
    failures: dict[str, int] = {}
    durations: list[float] = []
    if not jobs_root.is_dir():
        return {**totals, "avg_seconds": None, "max_seconds": None, "top_failures": []}
    for job in jobs_root.iterdir():
        if not job.is_dir():
            continue
        job_json = job / "job.json"
        if not job_json.is_file():
            continue
        try:
            state = json.loads(job_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        totals["jobs"] += 1
        status = str(state.get("status") or "unknown").lower()
        if state.get("expired_at"):
            continue  # files removed after the retention period: not a failure
        if status in {"done", "completed", "success"}:
            totals["done"] += 1
        elif status in {"failed", "error"}:
            totals["failed"] += 1
            reason = str(state.get("error") or "unknown")[:80]
            failures[reason] = failures.get(reason, 0) + 1
        elif status != "unknown":
            totals["running"] += 1
        timings = state.get("stage_timings") or {}
        total = float(timings.get("total_seconds") or timings.get("render_total") or 0)
        if total > 0:
            durations.append(total)
    top = sorted(failures.items(), key=lambda item: -item[1])[:5]
    return {
        **totals,
        "avg_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "max_seconds": round(max(durations), 1) if durations else None,
        "top_failures": [{"reason": r, "count": c} for r, c in top],
    }
