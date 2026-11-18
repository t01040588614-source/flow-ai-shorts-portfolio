"""Shared ownership checks for shorts jobs and timeline renders."""

from __future__ import annotations

import json
import logging

from fastapi import Depends, HTTPException, Request

from app.auth.auth_schema import AuthUserSchema
from app.auth.dependencies import get_current_user_optional
from app.auth.role_dependencies import require_authenticated_user
from app.config import Settings, get_settings
from app.logging_config import log_event
from app.models.schemas import ShortsJobResponse, TimelineRenderResponse
from app.security.path_validation import UUID_PATTERN, safe_path_under, validate_job_id, validate_render_id
from app.services.job_store import job_store
from app.services.local_job_asset import first_party_job_id
from app.services.render_store import render_store

_JOB_NOT_FOUND = "작업을 찾을 수 없습니다."
_RENDER_NOT_FOUND = "렌더 작업을 찾을 수 없습니다."
_MEDIA_NOT_FOUND = "미디어를 찾을 수 없습니다."


def assert_job_owner(
    job: ShortsJobResponse,
    actor: AuthUserSchema,
    settings: Settings,
) -> None:
    """Raise 404 when the actor may not access this job (hides cross-user existence)."""
    if not settings.jwt_auth_active:
        return
    if actor.role == "admin":
        return
    if job.owner_id is None or job.owner_id != actor.user_id:
        log_event(
            "ownership_denied",
            "Shorts job ownership check failed",
            level=logging.WARNING,
            resource_type="shorts_job",
            resource_id=job.job_id,
            actor_user_id=actor.user_id,
            owner_id=job.owner_id,
            http_status=404,
        )
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)


def assert_render_owner(
    render: TimelineRenderResponse,
    actor: AuthUserSchema,
    settings: Settings,
) -> None:
    """Raise 404 when the actor may not access this render."""
    if not settings.jwt_auth_active:
        return
    if actor.role == "admin":
        return
    if render.owner_id is None or render.owner_id != actor.user_id:
        log_event(
            "ownership_denied",
            "Render job ownership check failed",
            level=logging.WARNING,
            resource_type="render",
            resource_id=render.render_id,
            actor_user_id=actor.user_id,
            owner_id=render.owner_id,
            http_status=404,
        )
        raise HTTPException(status_code=404, detail=_RENDER_NOT_FOUND)


async def require_owned_job(
    job_id: str,
    settings: Settings = Depends(get_settings),
    current_user: AuthUserSchema = Depends(require_authenticated_user),
) -> ShortsJobResponse:
    validate_job_id(job_id)
    job = await job_store.get(job_id)
    if job is None:
        await job_store.recover_from_disk(settings)
        job = await job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    assert_job_owner(job, current_user, settings)
    return job


async def require_owned_render(
    render_id: str,
    settings: Settings = Depends(get_settings),
    current_user: AuthUserSchema = Depends(require_authenticated_user),
) -> TimelineRenderResponse:
    validate_render_id(render_id)
    job = await render_store.get(render_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_RENDER_NOT_FOUND)
    assert_render_owner(job, current_user, settings)
    return job


async def _find_job(job_id: str, settings: Settings) -> ShortsJobResponse | None:
    if not UUID_PATTERN.fullmatch(job_id):
        return None
    job = await job_store.get(job_id)
    if job is None and (settings.output_dir / job_id / "job.json").is_file():
        await job_store.recover_from_disk(settings)
        job = await job_store.get(job_id)
    return job


def _editor_media_owner(media_id: str, settings: Settings) -> str | None:
    meta_path = safe_path_under(settings.output_dir, "editor", media_id, "meta.json")
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    owner_id = meta.get("owner_id") if isinstance(meta, dict) else None
    return owner_id if isinstance(owner_id, str) and owner_id else None


def _bypasses_ownership(actor: AuthUserSchema, settings: Settings) -> bool:
    return not settings.jwt_auth_active or actor.role == "admin"


async def assert_project_access(project_id: str | None, actor: AuthUserSchema, settings: Settings) -> None:
    """A project_id that names a shorts job may only be used by that job's owner."""
    if not project_id or _bypasses_ownership(actor, settings):
        return
    job = await _find_job(project_id, settings)
    if job is not None:
        assert_job_owner(job, actor, settings)


async def assert_media_access(media_id: str | None, actor: AuthUserSchema, settings: Settings) -> None:
    """media_id may be a shorts job (its output or uploaded assets) or an editor upload."""
    if not media_id or _bypasses_ownership(actor, settings):
        return
    job = await _find_job(media_id, settings)
    if job is not None:
        assert_job_owner(job, actor, settings)
        return
    if not UUID_PATTERN.fullmatch(media_id):
        return
    owner_id = _editor_media_owner(media_id, settings)
    if owner_id is not None and owner_id != actor.user_id:
        log_event(
            "ownership_denied",
            "Editor media ownership check failed",
            level=logging.WARNING,
            resource_type="editor_media",
            resource_id=media_id,
            actor_user_id=actor.user_id,
            owner_id=owner_id,
            http_status=404,
        )
        raise HTTPException(status_code=404, detail=_MEDIA_NOT_FOUND)


async def assert_assets_access(assets: list[dict], actor: AuthUserSchema, settings: Settings) -> None:
    """Timeline assets may read only the actor's own jobs through first-party /api URLs.

    Staging reads those files straight from storage, so without this check a render could
    copy another user's scenes or video into the caller's output.
    """
    if _bypasses_ownership(actor, settings):
        return
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        for field in ("url", "path"):
            job_id = first_party_job_id(str(asset.get(field) or ""))
            if job_id:
                await assert_media_access(job_id, actor, settings)


def filter_renders_for_user(
    items: list[TimelineRenderResponse],
    actor: AuthUserSchema,
    settings: Settings,
) -> list[TimelineRenderResponse]:
    if not settings.jwt_auth_active or actor.role == "admin":
        return items
    return [item for item in items if item.owner_id == actor.user_id]


def filter_jobs_for_user(
    items: list[ShortsJobResponse],
    actor: AuthUserSchema,
    settings: Settings,
) -> list[ShortsJobResponse]:
    if not settings.jwt_auth_active or actor.role == "admin":
        return items
    return [item for item in items if item.owner_id == actor.user_id]


# --- router-level guard for project-scoped AI routes (history, chain, quality, agent) ---
#
# Those routes take a project id in the query or JSON body. This reuses
# assert_project_access / assert_media_access so every route follows the same rules.

_ID_KEYS = ("project_id", "projectId", "job_id", "jobId", "media_id", "mediaId")


def _collect_ids(value: object, depth: int = 0) -> set[str]:
    found: set[str] = set()
    if depth > 3 or not isinstance(value, dict):
        return found
    for key, item in value.items():
        if key in _ID_KEYS and isinstance(item, str) and item.strip():
            found.add(item.strip())
        elif isinstance(item, dict):
            found |= _collect_ids(item, depth + 1)
    return found


def user_project_prefix(user_id: str) -> str:
    return f"user:{user_id}:"


async def personal_project_key(project_id: str, actor: AuthUserSchema | None, settings: Settings) -> str:
    """The key chat, edit-history and learning records are stored under.

    A shorts job's id is used as is (the records go with the project). Any other id —
    the home chat's shared "proj_dashboard_demo", an unsaved editor project — is kept
    per signed-in user, so one user's records never mix with another's and are deleted
    with the account (personal_data.delete_user_records).
    """
    if not project_id or actor is None or not actor.user_id:
        return project_id
    prefix = user_project_prefix(actor.user_id)
    if project_id.startswith(prefix) or await _find_job(project_id, settings) is not None:
        return project_id
    if UUID_PATTERN.fullmatch(project_id) and (settings.output_dir / project_id).is_dir():
        return project_id  # a project folder (deleted with the project)
    return prefix + project_id


async def require_project_access(
    request: Request,
    settings: Settings = Depends(get_settings),
    current_user: AuthUserSchema | None = Depends(get_current_user_optional),
) -> None:
    """Router dependency: 404 when a referenced job/media belongs to someone else."""
    if not settings.jwt_auth_active:
        return
    ids = {value for key, value in request.query_params.items() if key in _ID_KEYS}
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            ids |= _collect_ids(await request.json())
        except ValueError:
            pass
    for candidate in ids:
        if current_user is None:
            if await _find_job(candidate, settings) is not None or (
                UUID_PATTERN.fullmatch(candidate) and _editor_media_owner(candidate, settings)
            ):
                raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
            continue
        await assert_media_access(candidate, current_user, settings)
