from __future__ import annotations

import openai
import pytest

from app.services.openai_errors import (
    MISSING_KEY_USER_MESSAGE,
    OpenAiFailureKind,
    classify_openai_error,
    http_exception_from_openai,
    job_error_message,
)


def _missing_key_error() -> Exception:
    try:
        openai.AsyncOpenAI(api_key="")
    except openai.OpenAIError as exc:
        return exc
    pytest.skip("this SDK version accepts an empty key")


def test_missing_key_is_shown_in_korean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    exc = _missing_key_error()
    assert classify_openai_error(exc) == OpenAiFailureKind.MISSING_KEY
    assert job_error_message(exc) == MISSING_KEY_USER_MESSAGE
    http = http_exception_from_openai(exc, notify_context=None)
    assert http.status_code == 503
    assert http.detail == MISSING_KEY_USER_MESSAGE


def test_other_sdk_errors_are_not_called_missing_key() -> None:
    assert classify_openai_error(openai.OpenAIError("something else")) == OpenAiFailureKind.OTHER
