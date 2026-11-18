"""Authentication service — Sprint 7 Phase 1."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import uuid4

from app.auth.auth_schema import (
    AuthUserSchema,
    LoginResponseSchema,
    LogoutResponseSchema,
    MeResponseSchema,
    PasswordResetConfirmResponseSchema,
    PasswordResetRequestResponseSchema,
    RefreshResponseSchema,
    SessionSchema,
    TokenPairSchema,
    UserAccountSchema,
    VerifyEmailConfirmResponseSchema,
    VerifyEmailRequestResponseSchema,
)
from app.auth.auth_store import AuthStore, get_auth_store, hash_password, password_needs_rehash, verify_password
from app.auth.jwt_service import JwtService, JwtValidationError
from app.auth.session_service import SessionService
from app.config import Settings, get_settings
from app.security.auth_attempts import unlock_email
from app.services.email_sender import EmailSender, get_email_sender

logger = logging.getLogger(__name__)
DEV_JWT_SECRET = "dev-jwt-secret-change-me"
MIN_PASSWORD_LENGTH = 8
# A rotated refresh token used again within this many seconds is two tabs refreshing at once
# (just refused); after that it is treated as stolen and its sign-in ends.
REFRESH_REUSE_GRACE_SECONDS = 30
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailAlreadyExistsError(ValueError):
    """Raised when register() is called with an email already in user_accounts."""


# Dummy credentials for the unknown-email login path (see AuthService.login).
_TIMING_SALT, _TIMING_HASH = hash_password("timing-equalizer")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(email))


def _issued_before(payload: dict[str, Any], stamps: Iterable[str | None]) -> bool:
    """True when the token's iat is before the latest of these ISO times.

    Compared as timestamps, not strings. An unreadable time or iat counts as revoked
    (fail closed): a broken row must not let every token through.
    """
    cutoffs: list[float] = []
    for stamp in stamps:
        if not stamp:
            continue
        try:
            cutoffs.append(datetime.fromisoformat(stamp).timestamp())
        except ValueError:
            return True
    if not cutoffs:
        return False
    try:
        issued_at = float(payload.get("iat") or 0)
    except (TypeError, ValueError):
        return True
    return issued_at < max(cutoffs)


def _session_id(payload: dict[str, Any]) -> str | None:
    """The sign-in (device) a token belongs to; None for tokens issued before sids existed."""
    sid = payload.get("sid")
    return sid if isinstance(sid, str) and sid else None


def _seconds_since(stamp: object) -> float:
    """Age of an ISO time in seconds; an unreadable or missing time counts as long ago."""
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds()


class AuthService:
    def __init__(
        self,
        *,
        store: AuthStore,
        jwt_service: JwtService,
        session_service: SessionService,
        email_sender: EmailSender | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._jwt = jwt_service
        self._sessions = session_service
        self._email = email_sender or get_email_sender()
        self._settings = settings or get_settings()

    def register(
        self,
        email: str,
        password: str,
        display_name: str = "",
    ) -> LoginResponseSchema:
        """Create a role=user account and issue tokens (same shape as login)."""
        normalized = _normalize_email(email)
        if not _is_valid_email(normalized):
            raise ValueError("이메일 형식이 올바르지 않습니다.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")

        if self._store.get_user_by_email(normalized) is not None:
            raise EmailAlreadyExistsError("이미 가입된 이메일입니다.")

        salt, password_hash = hash_password(password)
        now = datetime.now(UTC).isoformat()
        resolved_name = display_name.strip() or normalized.split("@", 1)[0]
        account = UserAccountSchema(
            user_id=f"usr_{uuid4().hex[:12]}",
            email=normalized,
            display_name=resolved_name,
            role="user",
            password_salt=salt,
            password_hash=password_hash,
            created_at=now,
            updated_at=now,
            email_verified=False,
        )
        try:
            created = self._store.create_user(account)
        except sqlite3.IntegrityError as exc:
            raise EmailAlreadyExistsError("이미 가입된 이메일입니다.") from exc

        user = self._store.to_auth_user(created)
        tokens, session = self._start_session(user)
        self._store.log_activity(
            user_id=user.user_id,
            action="register",
            metadata={"session_id": session.session_id},
        )
        dev_verify_url, verification_message = self._send_verification_email(created)
        return LoginResponseSchema(
            user=user,
            tokens=tokens,
            verification_message=verification_message,
            dev_verify_url=dev_verify_url,
        )

    def login(self, email: str, password: str) -> LoginResponseSchema:
        # Same form as register() and the lock counter, so " Me@x.com " finds the account.
        account = self._store.get_user_by_email(_normalize_email(email))
        if account is None:
            # Same password-hash work as a real account, so response time does not reveal it.
            verify_password(password, _TIMING_SALT, _TIMING_HASH)
            raise ValueError("이메일 또는 비밀번호가 맞지 않아요")
        if not verify_password(password, account.password_salt, account.password_hash):
            raise ValueError("이메일 또는 비밀번호가 맞지 않아요")
        if password_needs_rehash(account.password_salt):
            # Only a login sees the plain password, so older accounts move to the new count here.
            salt, password_hash = hash_password(password)
            self._store.upgrade_password_hash(account.user_id, salt=salt, password_hash=password_hash)

        user = self._store.to_auth_user(account)
        tokens, session = self._start_session(user)
        self._store.log_activity(user_id=user.user_id, action="login", metadata={"session_id": session.session_id})
        return LoginResponseSchema(user=user, tokens=tokens)

    def refresh(self, refresh_token: str) -> RefreshResponseSchema:
        payload = self._jwt.decode_token(refresh_token, expected_type="refresh")
        user = self._jwt.user_from_payload(payload)
        account = self._store.get_user_by_id(user.user_id)
        if account is None:
            raise ValueError("User not found")

        token_hash = self._hash_token(refresh_token)
        stored = self._store.get_refresh_token(token_hash)
        # The logout check also covers a row a racing refresh stored after logout revoked the rest.
        if stored is None or str(stored["user_id"]) != user.user_id or self._refresh_token_revoked(payload):
            raise ValueError("Refresh token revoked")
        if not self._store.claim_refresh_token(token_hash):
            self._end_session_on_reuse(user.user_id, payload, token_hash)
            raise ValueError("Refresh token revoked")

        # Tokens from before per-device sign-ins carry no sid: they get a session now.
        session_id = _session_id(payload) or self._sessions.create_session(user.user_id).session_id
        tokens = self._issue_tokens(self._store.to_auth_user(account), session_id=session_id)
        # A logout or password change that committed while this ran must win: look again before
        # handing out tokens. The old token's iat against the password/role time also covers a
        # token without sid, whose brand-new session that change's revoke-all may have missed.
        if self._refresh_token_revoked(payload) or self._ended_during_refresh(user.user_id, session_id, payload):
            self._store.end_session(user.user_id, session_id)
            raise ValueError("Refresh token revoked")
        self._store.extend_session(session_id, self._refresh_expiry_iso())
        return RefreshResponseSchema(tokens=tokens)

    def _ended_during_refresh(self, user_id: str, session_id: str, payload: dict[str, Any]) -> bool:
        state = self._store.get_revocation_state(user_id, session_id)
        if state is None or state.session_revoked:
            return True
        # Every password or role change also revokes all refresh tokens, so a token issued
        # before one cannot be live: seeing it here means that change landed mid-refresh.
        return _issued_before(payload, [state.credentials_updated_at])

    def _end_session_on_reuse(self, user_id: str, payload: dict[str, Any], token_hash: str) -> None:
        """An already-rotated refresh token came back. Seconds after its rotation that is two tabs
        racing; later it means a copy is in someone else's hands, so that sign-in ends."""
        session_id = _session_id(payload)
        stored = self._store.get_refresh_token(token_hash)
        if not session_id or stored is None:
            return
        if _seconds_since(stored.get("revoked_at")) < REFRESH_REUSE_GRACE_SECONDS:
            return
        state = self._store.get_revocation_state(user_id, session_id)
        if state is None or state.session_revoked:
            return
        self._store.end_session(user_id, session_id)
        self._store.log_activity(user_id=user_id, action="refresh_token_reuse", metadata={"session_id": session_id})
        logger.warning("Rotated refresh token used again; ended that sign-in", extra={"user_id": user_id})

    def logout(
        self,
        user_id: str,
        refresh_token: str | None = None,
        *,
        session_id: str | None = None,
    ) -> LogoutResponseSchema:
        """This device only. Other devices stay signed in (logout_all ends them)."""
        if refresh_token:
            self._store.revoke_refresh_token(self._hash_token(refresh_token))
        if session_id:
            self._store.end_session(user_id, session_id)
        else:
            # A token from before per-device sign-ins says nothing about which device: end them
            # all, as logout did then, rather than leave this one signed in.
            self._store.revoke_all_refresh_tokens(user_id)
            self._store.revoke_access_tokens(user_id)
        self._store.log_activity(user_id=user_id, action="logout", metadata={"session_id": session_id})
        return LogoutResponseSchema()

    def logout_with_tokens(self, access_token: str | None, refresh_token: str | None) -> LogoutResponseSchema:
        """Logout from whatever this device still holds. An expired access token is fine: the
        refresh token says whose sign-in it is. With nothing live there is nothing to end."""
        access = self._decode_quietly(access_token, "access")
        refresh = self._decode_quietly(refresh_token, "refresh")
        if access is not None and not self.access_token_revoked(access):
            owner = access
        elif refresh is not None and refresh_token and self._refresh_token_can_log_out(refresh_token, refresh):
            owner = refresh
        else:
            return LogoutResponseSchema()
        user_id, session_id = str(owner.get("sub") or ""), _session_id(owner)
        # A refresh token from another sign-in (or account) is not this logout's to revoke.
        same_sign_in = refresh is not None and str(refresh.get("sub") or "") == user_id and _session_id(refresh) == session_id
        return self.logout(user_id, refresh_token if same_sign_in else None, session_id=session_id)

    def _decode_quietly(self, token: str | None, token_type: str) -> dict[str, Any] | None:
        if not token:
            return None
        try:
            return self._jwt.decode_token(token, expected_type=token_type)
        except (JwtValidationError, ValueError):
            return None

    def _refresh_token_can_log_out(self, token: str, payload: dict[str, Any]) -> bool:
        stored = self._store.get_refresh_token(self._hash_token(token))
        if stored is None or str(stored["user_id"]) != str(payload.get("sub") or ""):
            return False
        if self._refresh_token_revoked(payload):
            return False  # that sign-in already ended
        if _session_id(payload):
            # Even if another tab or a copy already rotated it: whoever holds this token can end
            # its sign-in (a replay would do the same), and the user asked to be signed out.
            return True
        # Without a sid the logout ends every device, so only a live token may ask for that.
        return not bool(stored["revoked"])

    def logout_all(self, user_id: str) -> LogoutResponseSchema:
        self._store.revoke_all_refresh_tokens(user_id)
        self._store.revoke_all_sessions(user_id)
        self._store.revoke_access_tokens(user_id)
        self._store.log_activity(user_id=user_id, action="logout_all")
        return LogoutResponseSchema(message="All sessions revoked")

    def list_sessions(self, user_id: str) -> list:
        from app.dashboard.dashboard_schema import SessionInfoSchema

        return [
            SessionInfoSchema(
                session_id=item.session_id,
                user_id=item.user_id,
                created_at=item.created_at,
                expires_at=item.expires_at,
                revoked=item.revoked,
            )
            for item in self._store.list_sessions(user_id)
        ]

    def request_password_reset(self, email: str) -> PasswordResetRequestResponseSchema:
        """Always return the same message. Only existing accounts get a token."""
        message = self._idle_mail_message("이메일로 안내를 보냈습니다.")
        normalized = _normalize_email(email)
        if not _is_valid_email(normalized):
            return PasswordResetRequestResponseSchema(message=message)

        account = self._store.get_user_by_email(normalized)
        if account is None or not account.email_verified:
            return PasswordResetRequestResponseSchema(message=message)

        raw_token = secrets.token_urlsafe(32)
        minutes = max(1, self._settings.auth_password_reset_expire_minutes)
        expires_at = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
        self._store.save_password_reset_token(
            user_id=account.user_id,
            token_hash=self._hash_token(raw_token),
            expires_at=expires_at,
        )
        reset_url = self._reset_url(raw_token)
        if self._mail_configured():
            try:
                self._email.send_password_reset(to_email=account.email, reset_url=reset_url)
            except Exception:
                logger.exception("Password reset email send failed")
        else:
            # The link is a working login for that account: never write it to production logs.
            logger.info("Password reset link for %s: %s", account.email, self._loggable_link(reset_url))
        self._store.log_activity(user_id=account.user_id, action="password_reset_request")
        dev_url = None if self._settings.is_production else reset_url
        return PasswordResetRequestResponseSchema(message=message, dev_reset_url=dev_url)

    def _loggable_link(self, url: str) -> str:
        return "(hidden in production)" if self._settings.is_production else url

    def confirm_password_reset(
        self,
        token: str,
        new_password: str,
    ) -> PasswordResetConfirmResponseSchema:
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
        stored = self._store.get_password_reset_token(self._hash_token(token.strip()))
        if stored is None or bool(stored["used"]) or self._iso_expired(str(stored["expires_at"])):
            raise ValueError("재설정 링크가 만료되었거나 이미 사용되었습니다.")

        user_id = str(stored["user_id"])
        salt, password_hash = hash_password(new_password)
        self._store.update_password(user_id, salt=salt, password_hash=password_hash)
        self._store.mark_password_reset_used(str(stored["token_hash"]))
        self._store.revoke_all_refresh_tokens(user_id)
        self._store.revoke_all_sessions(user_id)
        self._store.log_activity(user_id=user_id, action="password_reset_confirm")
        account = self._store.get_user_by_id(user_id)
        if account is not None:
            # The owner proved control of the mailbox: lift any login lock on this email.
            unlock_email(self._settings, account.email)
        return PasswordResetConfirmResponseSchema()

    def change_password(self, user_id: str, current_password: str, new_password: str) -> LoginResponseSchema:
        """Signed-in password change. Every other session is signed out; this one gets fresh tokens."""
        account = self._store.get_user_by_id(user_id)
        if account is None:
            raise LookupError("User not found")
        if not verify_password(current_password, account.password_salt, account.password_hash):
            raise ValueError("현재 비밀번호가 맞지 않습니다.")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"새 비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
        if new_password == current_password:
            raise ValueError("새 비밀번호가 지금 비밀번호와 같습니다.")
        salt, password_hash = hash_password(new_password)
        self._store.update_password(user_id, salt=salt, password_hash=password_hash)
        self._store.revoke_all_refresh_tokens(user_id)
        self._store.revoke_all_sessions(user_id)
        self._store.log_activity(user_id=user_id, action="password_change")
        user = self._store.to_auth_user(account)
        tokens, _session = self._start_session(user)
        return LoginResponseSchema(user=user, tokens=tokens)

    def verify_account_password(self, user_id: str, password: str) -> UserAccountSchema:
        """Re-check the password before a destructive action such as account deletion."""
        account = self._store.get_user_by_id(user_id)
        if account is None:
            raise LookupError("User not found")
        if not verify_password(password, account.password_salt, account.password_hash):
            raise ValueError("비밀번호가 맞지 않습니다.")
        return account

    def delete_account(self, user_id: str) -> None:
        if not self._store.delete_user(user_id):
            raise LookupError("User not found")

    def request_verify_email(self, user_id: str) -> VerifyEmailRequestResponseSchema:
        account = self._store.get_user_by_id(user_id)
        if account is None:
            raise ValueError("User not found")
        if account.email_verified:
            return VerifyEmailRequestResponseSchema(
                message="이미 인증된 계정입니다.",
                already_verified=True,
            )
        dev_verify_url, message = self._send_verification_email(account)
        self._store.log_activity(user_id=user_id, action="verify_email_request")
        return VerifyEmailRequestResponseSchema(message=message, dev_verify_url=dev_verify_url)

    def confirm_verify_email(self, token: str) -> VerifyEmailConfirmResponseSchema:
        stored = self._store.get_email_verification_token(self._hash_token(token.strip()))
        if stored is None or bool(stored["used"]) or self._iso_expired(str(stored["expires_at"])):
            raise ValueError("인증 링크가 만료되었거나 이미 사용되었습니다.")

        user_id = str(stored["user_id"])
        self._store.mark_email_verified(user_id)
        self._store.mark_email_verification_used(str(stored["token_hash"]))
        self._store.log_activity(user_id=user_id, action="verify_email_confirm")
        return VerifyEmailConfirmResponseSchema()

    def access_token_revoked(self, payload: dict[str, Any]) -> bool:
        """True when the account is gone, this sign-in logged out, or the account logged out
        everywhere or changed password/role after this token was issued."""
        state = self._store.get_revocation_state(str(payload.get("sub") or ""), _session_id(payload))
        if state is None:
            # No such account (or deleted by an admin): its unexpired tokens must stop working.
            return True
        if state.session_revoked:
            return True
        return _issued_before(payload, [state.credentials_updated_at, state.tokens_revoked_at])

    def _refresh_token_revoked(self, payload: dict[str, Any]) -> bool:
        """Only a logout ends refresh tokens by time or session. A role change keeps them (the next
        refresh carries the new role) and a password change already revokes them row by row."""
        state = self._store.get_revocation_state(str(payload.get("sub") or ""), _session_id(payload))
        if state is None or state.session_revoked:
            return True
        return _issued_before(payload, [state.tokens_revoked_at])

    def _start_session(self, user: AuthUserSchema) -> tuple[TokenPairSchema, SessionSchema]:
        """A new sign-in on one device: its own session, and tokens that carry its id."""
        session = self._sessions.create_session(user.user_id)
        return self._issue_tokens(user, session_id=session.session_id), session

    def _refresh_expiry_iso(self) -> str:
        return self._sessions.refresh_token_expiry_iso(self._jwt.refresh_expire_days)

    def _reset_url(self, raw_token: str) -> str:
        origins = self._settings.cors_origin_list
        base = origins[0] if origins else "http://127.0.0.1:5150"
        # After '#': browsers never send it to the server, so it stays out of Caddy/nginx access logs.
        return f"{base.rstrip('/')}/reset-password#token={raw_token}"

    def _verify_url(self, raw_token: str) -> str:
        origins = self._settings.cors_origin_list
        base = origins[0] if origins else "http://127.0.0.1:5150"
        return f"{base.rstrip('/')}/verify-email#token={raw_token}"

    def _mail_configured(self) -> bool:
        mode = self._settings.email_sender.strip().lower()
        if mode == "log":
            return False
        return bool(self._settings.smtp_host.strip())

    def _idle_mail_message(self, sent_message: str) -> str:
        """Same text whether or not the address exists, so resets do not reveal accounts."""
        if self._mail_configured():
            return sent_message
        if self._settings.is_production:
            return "메일 발송 설정이 안 되어 있습니다. 관리자에게 문의해 주세요."
        return "개발 환경이라 메일은 보내지 않았습니다. 계정이 있으면 아래 링크를 사용하세요."

    def _send_verification_email(self, account: UserAccountSchema) -> tuple[str | None, str]:
        raw_token = secrets.token_urlsafe(32)
        hours = max(1, self._settings.auth_verify_email_expire_hours)
        expires_at = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
        self._store.save_email_verification_token(
            user_id=account.user_id,
            token_hash=self._hash_token(raw_token),
            expires_at=expires_at,
        )
        verify_url = self._verify_url(raw_token)
        if not self._mail_configured():
            logger.info("Email verification link for %s: %s", account.email, self._loggable_link(verify_url))
            if self._settings.is_production:
                return None, "메일 발송 설정이 안 되어 있습니다. 관리자에게 문의해 주세요."
            return verify_url, "개발 환경이라 메일은 보내지 않았습니다. 아래 링크로 인증하세요."
        try:
            self._email.send_email_verification(to_email=account.email, verify_url=verify_url)
        except Exception as exc:
            logger.exception("Email verification send failed")
            reason = exc.__class__.__name__
            if self._settings.is_production:
                return None, f"메일 발송에 실패했습니다 ({reason}). 잠시 후 다시 시도해 주세요."
            return verify_url, f"메일 발송에 실패했습니다 ({reason}). 아래 개발용 링크를 사용하세요."
        return None, "인증 메일을 보냈습니다. 받은 편지함을 확인해 주세요."

    @staticmethod
    def _iso_expired(raw: str) -> bool:
        try:
            expires = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)

    def me(self, user_id: str) -> MeResponseSchema:
        account = self._store.get_user_by_id(user_id)
        if account is None:
            raise ValueError("User not found")
        return MeResponseSchema(user=self._store.to_auth_user(account))

    def _issue_tokens(self, user: AuthUserSchema, *, session_id: str | None = None) -> TokenPairSchema:
        access_token, expires_in = self._jwt.create_access_token(user, session_id=session_id)
        refresh_token, _ = self._jwt.create_refresh_token(user, session_id=session_id)
        self._store.save_refresh_token(
            user_id=user.user_id,
            token_hash=self._hash_token(refresh_token),
            expires_at=self._refresh_expiry_iso(),
            session_id=session_id,
        )
        return TokenPairSchema(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_jwt_service(settings: Settings) -> JwtService:
    secret = settings.jwt_secret.strip() or settings.security_api_token.strip() or DEV_JWT_SECRET
    if not settings.jwt_secret.strip() and not settings.security_api_token.strip():
        logger.warning("JWT_SECRET 미설정 — 개발용 기본값 사용 중")
    return JwtService(
        secret=secret,
        access_expire_minutes=settings.jwt_access_expire_minutes,
        refresh_expire_days=settings.jwt_refresh_expire_days,
    )


@lru_cache
def get_auth_service(db_path: str) -> AuthService:
    settings = get_settings()
    store = get_auth_store(db_path)
    jwt_service = build_jwt_service(settings)
    # A sign-in lasts as long as its refresh token (each refresh extends it), not just a day.
    session_hours = max(settings.jwt_session_ttl_hours, settings.jwt_refresh_expire_days * 24)
    session_service = SessionService(store, session_ttl_hours=session_hours)
    return AuthService(
        store=store,
        jwt_service=jwt_service,
        session_service=session_service,
        email_sender=get_email_sender(),
        settings=settings,
    )


def reset_auth_service_cache() -> None:
    get_auth_service.cache_clear()
    from app.auth.auth_store import reset_auth_store_cache
    from app.services.email_sender import reset_email_sender_cache_for_tests

    reset_auth_store_cache()
    reset_email_sender_cache_for_tests()
