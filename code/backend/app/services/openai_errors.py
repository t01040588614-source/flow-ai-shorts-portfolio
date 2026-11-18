"""Classify OpenAI failures, user-facing copy, and operator alerts."""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from typing import Any

import httpx
import openai
from fastapi import HTTPException

from app.config import Settings
from app.services import alerts

logger = logging.getLogger(__name__)

INSUFFICIENT_QUOTA_USER_MESSAGE = (
    "AI 서비스 잔액이 부족해 지금은 만들 수 없어요. 잠시 후 다시 시도해 주세요."
)
RATE_LIMIT_USER_MESSAGE = "요청이 많습니다. 잠시 후 다시 시도해 주세요."
WHISPER_FAILED_MESSAGE = "음성 인식 서버 요청에 실패했습니다. 잠시 후 다시 시도해 주세요."
MISSING_KEY_USER_MESSAGE = "AI 키가 설정되지 않아 지금은 이 작업을 할 수 없어요. 관리자에게 알려 주세요."
# SDK wording when no api_key reaches the client (v2: "api_key client option", v3: "Missing credentials").
_MISSING_KEY_MARKERS = ("missing credentials", "api_key client option must be set")


class OpenAiFailureKind(str, Enum):
    INSUFFICIENT_QUOTA = "insufficient_quota"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    MISSING_KEY = "missing_key"
    OTHER = "other"


class InsufficientQuotaError(RuntimeError):
    """OpenAI billing/quota exhausted — never retry."""

    def __init__(self, cause: Exception | None = None) -> None:
        super().__init__(INSUFFICIENT_QUOTA_USER_MESSAGE)
        self.cause = cause


def _error_payload(exc: Exception) -> dict[str, Any] | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        return err if isinstance(err, dict) else body
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            data = response.json()
        except Exception:
            text = getattr(response, "text", "") or ""
            try:
                data = json.loads(text)
            except Exception:
                return None
        if isinstance(data, dict):
            err = data.get("error")
            return err if isinstance(err, dict) else data
    return None


def _code_from_payload(payload: dict[str, Any] | None, message: str) -> str | None:
    if payload:
        code = payload.get("code") or payload.get("type")
        if isinstance(code, str) and code.strip():
            return code.strip()
    lowered = message.lower()
    if "insufficient_quota" in lowered or "exceeded your current quota" in lowered:
        return "insufficient_quota"
    if "rate_limit" in lowered or "rate limit" in lowered:
        return "rate_limit"
    return None


def classify_openai_error(exc: Exception) -> OpenAiFailureKind:
    if isinstance(exc, InsufficientQuotaError):
        return OpenAiFailureKind.INSUFFICIENT_QUOTA

    if isinstance(exc, (openai.APITimeoutError, asyncio.TimeoutError, TimeoutError)):
        return OpenAiFailureKind.TIMEOUT

    if isinstance(exc, openai.RateLimitError):
        code = _code_from_payload(_error_payload(exc), str(exc))
        if code == "insufficient_quota":
            return OpenAiFailureKind.INSUFFICIENT_QUOTA
        return OpenAiFailureKind.RATE_LIMIT

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        code = _code_from_payload(_error_payload(exc), exc.response.text)
        if code == "insufficient_quota":
            return OpenAiFailureKind.INSUFFICIENT_QUOTA
        return OpenAiFailureKind.RATE_LIMIT

    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 429:
            code = _code_from_payload(_error_payload(exc), str(exc))
            if code == "insufficient_quota":
                return OpenAiFailureKind.INSUFFICIENT_QUOTA
            return OpenAiFailureKind.RATE_LIMIT
        if exc.status_code in {408, 504}:
            return OpenAiFailureKind.TIMEOUT

    message = str(exc).lower()
    if (
        isinstance(exc, openai.OpenAIError)
        and not isinstance(exc, openai.APIError)
        and any(marker in message for marker in _MISSING_KEY_MARKERS)
    ):
        return OpenAiFailureKind.MISSING_KEY
    if "insufficient_quota" in message or "exceeded your current quota" in message:
        return OpenAiFailureKind.INSUFFICIENT_QUOTA
    if "rate limit" in message or "too many requests" in message:
        return OpenAiFailureKind.RATE_LIMIT
    if "timed out" in message or "timeout" in message:
        return OpenAiFailureKind.TIMEOUT
    return OpenAiFailureKind.OTHER


def is_insufficient_quota(exc: Exception) -> bool:
    return classify_openai_error(exc) == OpenAiFailureKind.INSUFFICIENT_QUOTA


def rate_limit_sleep_seconds(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("retry-after") or response.headers.get("Retry-After")
        if header:
            try:
                return max(1.0, float(header))
            except ValueError:
                pass
    return min(60.0, 2.0 * (2**attempt))


def notify_insufficient_quota(settings: Settings, *, context: str = "OpenAI") -> None:
    alerts.notify(
        "openai_insufficient_quota",
        f"{context}: API 잔액이 부족합니다(insufficient_quota). 충전 후 AI 작업을 다시 켜 주세요.",
        settings,
    )


def job_error_message(exc: Exception) -> str:
    kind = classify_openai_error(exc)
    if kind == OpenAiFailureKind.INSUFFICIENT_QUOTA:
        return INSUFFICIENT_QUOTA_USER_MESSAGE
    if kind == OpenAiFailureKind.MISSING_KEY:
        return MISSING_KEY_USER_MESSAGE
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    text = str(exc).strip()
    return text[:300] if text else "작업에 실패했습니다."


def http_exception_from_openai(
    exc: Exception,
    *,
    generic_detail: str = "AI 서버 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.",
    notify_context: str | None = "OpenAI",
    settings: Settings | None = None,
) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    kind = classify_openai_error(exc)
    if kind == OpenAiFailureKind.INSUFFICIENT_QUOTA:
        if notify_context:
            from app.config import get_settings

            notify_insufficient_quota(settings or get_settings(), context=notify_context)
        return HTTPException(status_code=503, detail=INSUFFICIENT_QUOTA_USER_MESSAGE)
    if kind == OpenAiFailureKind.RATE_LIMIT:
        return HTTPException(status_code=429, detail=RATE_LIMIT_USER_MESSAGE)
    if kind == OpenAiFailureKind.MISSING_KEY:
        return HTTPException(status_code=503, detail=MISSING_KEY_USER_MESSAGE)
    if kind == OpenAiFailureKind.TIMEOUT:
        return HTTPException(status_code=504, detail=generic_detail)
    return HTTPException(status_code=502, detail=generic_detail)


async def call_openai_with_handling(
    settings: Settings,
    fn,
    *,
    generic_detail: str = "AI 서버 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.",
    notify_context: str | None = "OpenAI",
):
    """Run one OpenAI coroutine; map quota/rate-limit to HTTPException."""
    try:
        return await fn()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("OpenAI call failed")
        raise http_exception_from_openai(
            exc,
            generic_detail=generic_detail,
            notify_context=notify_context,
            settings=settings,
        ) from exc
