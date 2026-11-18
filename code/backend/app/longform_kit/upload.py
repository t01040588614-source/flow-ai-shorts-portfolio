from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.config import Settings
from app.longform_kit.store import load_json, save_json, upload_dir, uploads_root
from app.services.disk_guard import assert_enough_disk
from app.services.media_probe import probe_video_file


def create_upload_session(
    settings: Settings,
    *,
    upload_id: str,
    filename: str,
    total_size: int,
    total_chunks: int,
    user_id: str | None = None,
) -> dict[str, Any]:
    if total_size <= 0 or total_chunks <= 0:
        raise HTTPException(status_code=400, detail="total_size와 total_chunks는 1 이상이어야 합니다.")
    if total_size > settings.longform_max_upload_bytes:
        limit_gb = settings.longform_max_upload_bytes / (1024**3)
        raise HTTPException(status_code=413, detail=f"파일 용량이 너무 큽니다. 업로드 한도는 약 {limit_gb:.1f}GB 입니다.")
    assert_enough_disk(uploads_root(settings), total_size)
    try:
        free_gb = shutil.disk_usage(uploads_root(settings)).free / (1024**3)
    except OSError:
        free_gb = None
    if free_gb is not None and free_gb < settings.longform_min_free_disk_gb:
        raise HTTPException(
            status_code=507,
            detail=(
                f"디스크 여유가 {settings.longform_min_free_disk_gb:.0f}GB 미만이라 "
                "긴 영상 업로드를 시작할 수 없습니다. 공간을 확보한 뒤 다시 시도해 주세요."
            ),
        )
    folder = upload_dir(settings, upload_id)
    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "total_size": total_size,
        "total_chunks": total_chunks,
        "received_chunks": [],
        "bytes_received": 0,
        "status": "uploading",
        "user_id": user_id,
    }
    save_json(folder / "session.json", meta)
    return meta


def load_upload_session(settings: Settings, upload_id: str) -> dict[str, Any]:
    meta = load_json(upload_dir(settings, upload_id) / "session.json")
    if meta is None:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")
    return meta


def assert_session_owner(meta: dict[str, Any], user_id: str | None, *, jwt_active: bool) -> None:
    if not jwt_active or not user_id:
        return
    owner = meta.get("user_id")
    if owner and owner != user_id:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")


def write_chunk(settings: Settings, upload_id: str, chunk_index: int, data: bytes) -> dict[str, Any]:
    meta = load_upload_session(settings, upload_id)
    if meta.get("status") == "complete":
        raise HTTPException(status_code=409, detail="이미 완료된 업로드입니다.")
    if chunk_index < 0 or chunk_index >= int(meta["total_chunks"]):
        raise HTTPException(status_code=400, detail="chunk_index가 범위를 벗어났습니다.")
    max_chunk = settings.longform_upload_chunk_bytes
    if len(data) > max_chunk + 1024:
        raise HTTPException(status_code=413, detail=f"청크 크기는 {max_chunk // (1024 * 1024)}MB 이하여야 합니다.")
    folder = upload_dir(settings, upload_id)
    assert_enough_disk(folder, len(data))
    part = folder / f"chunk_{chunk_index:06d}.part"
    received = set(meta.get("received_chunks") or [])
    if chunk_index in received and part.is_file() and part.stat().st_size == len(data):
        return meta
    part.write_bytes(data)
    received.add(chunk_index)
    meta["received_chunks"] = sorted(received)
    meta["bytes_received"] = sum(
        (folder / f"chunk_{i:06d}.part").stat().st_size
        for i in received
        if (folder / f"chunk_{i:06d}.part").is_file()
    )
    save_json(folder / "session.json", meta)
    return meta


def delete_upload_session(settings: Settings, upload_id: str) -> None:
    folder = upload_dir(settings, upload_id)
    shutil.rmtree(folder, ignore_errors=True)


def finalize_upload(settings: Settings, upload_id: str) -> tuple[Path, dict[str, Any]]:
    meta = load_upload_session(settings, upload_id)
    total_chunks = int(meta["total_chunks"])
    received = set(meta.get("received_chunks") or [])
    if len(received) != total_chunks or received != set(range(total_chunks)):
        missing = sorted(set(range(total_chunks)) - received)
        raise HTTPException(status_code=400, detail=f"아직 받지 못한 청크가 있습니다: {missing[:10]}")
    folder = upload_dir(settings, upload_id)
    target = folder / "source.mp4"
    with target.open("wb") as out:
        for index in range(total_chunks):
            part = folder / f"chunk_{index:06d}.part"
            if not part.is_file():
                raise HTTPException(status_code=400, detail=f"청크 {index} 파일이 없습니다.")
            with part.open("rb") as handle:
                shutil.copyfileobj(handle, out)
    size = target.stat().st_size
    if size != int(meta["total_size"]):
        raise HTTPException(
            status_code=422,
            detail=f"병합된 파일 크기({size})가 선언한 total_size({meta['total_size']})와 다릅니다.",
        )
    probe = probe_video_file(target)
    if probe is None:
        raise HTTPException(status_code=422, detail="영상 메타데이터를 읽지 못했습니다. 파일이 손상됐을 수 있습니다.")
    if probe.duration_seconds > settings.longform_max_duration_seconds:
        limit_h = settings.longform_max_duration_seconds / 3600
        raise HTTPException(status_code=413, detail=f"영상 길이 한도는 {limit_h:.1f}시간입니다.")
    if not probe.has_audio:
        raise HTTPException(status_code=422, detail="음성 트랙이 없습니다. 말이 포함된 영상을 올려 주세요.")
    meta["status"] = "complete"
    meta["probe"] = {
        "duration_seconds": round(probe.duration_seconds, 3),
        "width": probe.width,
        "height": probe.height,
        "has_audio": probe.has_audio,
    }
    meta["source_path"] = str(target)
    save_json(folder / "session.json", meta)
    return target, meta
