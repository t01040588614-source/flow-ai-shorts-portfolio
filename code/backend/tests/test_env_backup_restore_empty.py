from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

from app.config import Settings
from app.services.env_backup import create_backup, restore_backup


def _write_sqlite(path: Path, note: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ping (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO ping(note) VALUES (?)", (note,))
        conn.commit()


def test_restore_into_empty_folder_then_db_readable(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / ".env").write_text("JWT_SECRET=restore-test\n", encoding="utf-8")
    _write_sqlite(source_root / "data" / "ai_memory.db", "alive")
    archive = create_backup(settings=Settings(ai_db_path=source_root / "data" / "ai_memory.db"), root=source_root)

    target = tmp_path / "empty"
    target.mkdir()
    settings = Settings(
        ai_db_path=target / "data" / "ai_memory.db",
        render_db_path=target / "data" / "renders.db",
    )
    restore_backup(archive, settings=settings, root=target, safety_backup=False)
    db = target / "data" / "ai_memory.db"
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT note FROM ping ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and row[0] == "alive"


def test_restore_only_writes_env_and_databases(tmp_path: Path) -> None:
    archive = tmp_path / "backup_crafted.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(".env", "JWT_SECRET=from-backup\n")
        bundle.writestr("data/ai_memory.db", b"SQLite format 3\x00")
        bundle.writestr("app/main.py", "print('replaced')\n")
        bundle.writestr("data/nested/x.db", b"x")
        bundle.writestr("scripts/run.sh", "echo hi\n")
        bundle.writestr("README.txt", "readme")
    target = tmp_path / "target"
    (target / "app").mkdir(parents=True)
    (target / "app" / "main.py").write_text("original\n", encoding="utf-8")
    settings = Settings(ai_db_path=target / "data" / "ai_memory.db", render_db_path=target / "data" / "renders.db")

    restore_backup(archive, settings=settings, root=target, safety_backup=False)

    assert (target / ".env").read_text(encoding="utf-8") == "JWT_SECRET=from-backup\n"
    assert (target / "data" / "ai_memory.db").is_file()
    assert (target / "app" / "main.py").read_text(encoding="utf-8") == "original\n"
    assert not (target / "data" / "nested").exists()
    assert not (target / "scripts").exists()
    assert not (target / "README.txt").exists()
