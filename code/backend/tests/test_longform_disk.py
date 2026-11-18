import json
import os
import time
from pathlib import Path

from app.config import Settings
from app.longform_kit.disk_maintenance import longform_job_metrics, run_longform_cleanup
from app.longform_kit.store import job_dir


def test_longform_job_metrics_empty(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    metrics = longform_job_metrics(settings)
    assert metrics["jobs"] == 0
    assert metrics["top_failures"] == []


DAY = 86400


def _job(settings: Settings, job_id: str, state: dict, files: list[str], *, age_seconds: float) -> Path:
    work = job_dir(settings, job_id)
    for name in files:
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    (work / "job.json").write_text(json.dumps({"job_id": job_id, **state}), encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(work / "job.json", (stamp, stamp))
    return work


def _state(work: Path) -> dict:
    return json.loads((work / "job.json").read_text(encoding="utf-8"))


FINISHED_FILES = ["source.mp4", "segment_01.mp4", "short_01.mp4", "transcript.json", "render_1/a.mp4", "stt/c.json"]


def test_finished_job_keeps_results_but_drops_scratch_after_a_day(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    work = _job(settings, "a", {"status": "completed", "user_id": "u"}, FINISHED_FILES, age_seconds=2 * DAY)

    report = run_longform_cleanup(settings)

    assert not (work / "render_1").exists() and not (work / "stt").exists()
    for kept in ("source.mp4", "segment_01.mp4", "short_01.mp4", "transcript.json"):
        assert (work / kept).is_file(), kept
    assert report.expired_jobs == 0


def test_finished_job_files_go_after_the_retention_period(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    work = _job(
        settings,
        "b",
        {"status": "completed", "user_id": "u", "shorts": [{"title": "private words"}]},
        [*FINISHED_FILES, "export.zip"],
        age_seconds=15 * DAY,
    )

    report = run_longform_cleanup(settings)

    assert sorted(path.name for path in work.iterdir()) == ["job.json"]
    state = _state(work)
    assert state["status"] == "failed" and "보관 기간(14일)" in state["error"]
    assert state["user_id"] == "u" and "shorts" not in state
    assert report.expired_jobs == 1
    # A second round leaves the expired job alone.
    assert run_longform_cleanup(settings).expired_jobs == 0


def test_failed_job_files_go_after_a_day_and_keep_its_error(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    work = _job(settings, "c", {"status": "failed", "error": "음성이 없습니다"}, ["source.mp4"], age_seconds=2 * DAY)

    run_longform_cleanup(settings)

    assert not (work / "source.mp4").exists()
    assert _state(work)["error"] == "음성이 없습니다"


def test_jobs_in_progress_are_left_alone(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    works = [
        _job(settings, status, {"status": status}, ["source.mp4", "render_1/a.mp4"], age_seconds=2 * DAY)
        for status in ("queue_wait", "transcribing", "highlighting", "rendering")
    ]
    exporting = _job(
        settings, "e", {"status": "completed", "export": {"status": "running"}}, ["export/x.mp4"], age_seconds=2 * DAY
    )

    report = run_longform_cleanup(settings)

    for work in [*works, exporting]:
        assert len(list(work.rglob("*.mp4"))) >= 1, work.name
    assert report.skipped_active == 5


def test_job_nothing_touched_for_the_whole_period_is_cleared(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    work = _job(settings, "d", {"status": "transcribing"}, ["source.mp4"], age_seconds=15 * DAY)

    run_longform_cleanup(settings)

    assert not (work / "source.mp4").exists()


def test_dry_run_deletes_nothing(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    work = _job(settings, "f", {"status": "completed"}, FINISHED_FILES, age_seconds=15 * DAY)

    report = run_longform_cleanup(settings, dry_run=True)

    assert report.expired_jobs == 1 and report.freed_bytes > 0
    assert (work / "short_01.mp4").is_file() and _state(work)["status"] == "completed"


def test_expired_jobs_are_not_counted_as_failures(tmp_path) -> None:
    settings = Settings(output_dir=tmp_path)
    _job(settings, "g", {"status": "completed"}, ["short_01.mp4"], age_seconds=15 * DAY)
    run_longform_cleanup(settings)

    assert longform_job_metrics(settings)["top_failures"] == []


def test_maintenance_runs_the_longform_cleanup() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "run_longform_cleanup, current_settings" in source
