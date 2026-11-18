"""Backup and restore backend .env plus SQLite databases.

Operational helper — not used by routers or command parsers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.config import BASE_DIR, Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 10
STARTUP_MAX_AGE_SECONDS = 24 * 3600
ZIP_PREFIX = "backup_"
PRE_RESTORE_PREFIX = "pre_restore_"
_SQLITE_MAGIC = b"SQLite format 3\x00"

README_TEXT = """Flow backend env/DB backup
===========================

This zip is a portable snapshot of secrets and SQLite state.

Included
--------
- .env
  JWT_SECRET / SECURITY_API_TOKEN cannot be reissued: losing them
  invalidates every existing login session. Provider keys
  (OPENAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY) can be pasted
  again from the vendor dashboard.
- SQLite files under data/ (and any extra *.db next to them)
  * ai_memory.db — users, refresh tokens, chat memory, edit history,
    agent conversation / long-term memory (one shared file)
  * renders.db — timeline render jobs
  * any leftover *.db in data/ (for example e2e copies)

Not included
------------
Job media under outputs/, caches, and this backups/ folder.
Copy outputs/ separately if you need the actual mp4/png files.

Restore (server must be stopped)
--------------------------------
  cd backend
  python scripts/restore.py backups/THIS_FILE.zip

The restore script snapshots the current .env/DB first, then asks
for confirmation before overwriting.

Moving to another PC
--------------------
Take this zip. After clone + venv, restore it, then start the API.
"""


def backups_dir(root: Path | None = None) -> Path:
    return (root or BASE_DIR) / "backups"


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def collect_backup_sources(settings: Settings, root: Path | None = None) -> list[Path]:
    """Configured DB paths plus every *.db under data/. Skip missing files."""
    base = root or BASE_DIR
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append(path)

    add(base / ".env")
    add(Path(settings.ai_db_path))
    add(Path(settings.render_db_path))
    data_dir = base / "data"
    if data_dir.is_dir():
        for extra in sorted(data_dir.glob("*.db")):
            add(extra)
        for extra in sorted(data_dir.glob("*.sqlite")):
            add(extra)
        for extra in sorted(data_dir.glob("*.sqlite3")):
            add(extra)
    return found


def _copy_sqlite_snapshot(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    # closing(): a plain `with sqlite3.connect()` only commits; an open target file stays locked
    # on Windows and the next step (zip, unlink) can fail.
    with (
        closing(sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(dest)) as target,
    ):
        source.backup(target)


def _copy_plain(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def prune_old_backups(directory: Path, *, keep: int = DEFAULT_KEEP) -> list[Path]:
    if keep < 1 or not directory.is_dir():
        return []
    archives = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".zip"
            and (path.name.startswith(ZIP_PREFIX) or path.name.startswith(PRE_RESTORE_PREFIX))
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for stale in archives[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError as exc:
            logger.warning("Could not remove old backup %s: %s", stale, exc)
    return removed


def latest_backup_mtime(directory: Path) -> float | None:
    if not directory.is_dir():
        return None
    mtimes = [
        path.stat().st_mtime
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".zip"
        and (path.name.startswith(ZIP_PREFIX) or path.name.startswith(PRE_RESTORE_PREFIX))
    ]
    return max(mtimes) if mtimes else None


def create_backup(
    *,
    settings: Settings | None = None,
    root: Path | None = None,
    destination_dir: Path | None = None,
    keep: int | None = None,
    prefix: str = ZIP_PREFIX,
) -> Path:
    current = settings or get_settings()
    keep = DEFAULT_KEEP if keep is None else keep
    if settings is not None and keep == DEFAULT_KEEP:
        keep = max(1, int(current.db_backup_keep_count))
    base = root or BASE_DIR
    folder = destination_dir or backups_dir(base)
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    archive = folder / f"{prefix}{stamp}.zip"
    if archive.exists():
        archive = folder / f"{prefix}{stamp}_{int(time.time())}.zip"

    sources = collect_backup_sources(current, base)
    work = folder / f".staging_{archive.stem}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    written: list[str] = []
    try:
        for source in sources:
            relative = _relative_to_root(source, base)
            target = work / relative
            if _is_sqlite_file(source):
                _copy_sqlite_snapshot(source, target)
            else:
                _copy_plain(source, target)
            written.append(relative)

        (work / "README.txt").write_text(README_TEXT, encoding="utf-8")
        manifest = {
            "created_at": datetime.now().astimezone().isoformat(),
            "files": written,
            "keep": keep,
        }
        (work / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in work.rglob("*"):
                if path.is_file():
                    bundle.write(path, path.relative_to(work).as_posix())
    finally:
        shutil.rmtree(work, ignore_errors=True)

    prune_old_backups(folder, keep=keep)
    return archive


def maybe_backup_on_startup(
    settings: Settings | None = None,
    *,
    root: Path | None = None,
    max_age_seconds: float = STARTUP_MAX_AGE_SECONDS,
) -> Path | None:
    """Best-effort: write a backup when the newest zip is older than 24h."""
    if os.environ.get("FLOW_SKIP_STARTUP_BACKUP", "").strip().lower() in {"1", "true", "yes"}:
        return None
    try:
        current = settings or get_settings()
        base = root or BASE_DIR
        folder = backups_dir(base)
        latest = latest_backup_mtime(folder)
        if latest is not None and (time.time() - latest) < max_age_seconds:
            return None
        return create_backup(settings=current, root=base)
    except Exception:
        logger.exception("Startup env/DB backup failed; continuing startup")
        return None


_RESTORABLE_DB_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


def _restorable(relative: str) -> bool:
    """Only what create_backup writes: .env and SQLite files at the top or under data/.

    A zip from elsewhere must not overwrite code (app/...) or anything else in the folder.
    """
    if relative == ".env":
        return True
    parts = relative.split("/")
    if Path(relative).suffix.lower() not in _RESTORABLE_DB_SUFFIXES:
        return False
    return len(parts) == 1 or (len(parts) == 2 and parts[0] == "data")


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.is_file():
            sidecar.unlink()


def restore_backup(
    archive: Path,
    *,
    settings: Settings | None = None,
    root: Path | None = None,
    safety_backup: bool = True,
) -> Path | None:
    """Overwrite current .env/DBs from a backup zip. Returns the safety zip path."""
    if not archive.is_file():
        raise FileNotFoundError(f"백업 파일이 없습니다: {archive}")

    current = settings or get_settings()
    base = root or BASE_DIR
    safety: Path | None = None
    if safety_backup:
        safety = create_backup(
            settings=current,
            root=base,
            prefix=PRE_RESTORE_PREFIX,
        )

    extract = base / "backups" / f".restore_{int(time.time())}"
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extract)
        skip = {"readme.txt", "manifest.json"}
        for path in sorted(extract.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(extract).as_posix()
            if relative.lower() in skip:
                continue
            if not _restorable(relative):
                logger.warning("Backup restore skipped an unexpected file: %s", relative)
                continue
            dest = base / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            if dest.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                _remove_sqlite_sidecars(dest)
    finally:
        shutil.rmtree(extract, ignore_errors=True)
    return safety
