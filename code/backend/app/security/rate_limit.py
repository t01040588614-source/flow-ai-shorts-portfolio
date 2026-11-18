"""IP-based rate limiting and concurrent heavy-work guards."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import defaultdict, deque
from functools import lru_cache

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.config import Settings, get_settings
from app.logging_config import log_event

logger = logging.getLogger(__name__)

HEAVY_PATHS = {
    "/api/images/generate",
    "/api/shorts/generate",
    "/api/timeline/render",
    # Calls the LLM on every request.
    "/api/style/preview",
}
# Browser reports anyone can send without signing in; capped per IP so they cannot fill the disk.
CLIENT_REPORT_HOURLY_LIMITS = {"errors": 60, "stats": 600}

HEAVY_WORK_LIMIT_DETAIL = (
    "지금 처리 중인 작업이 많습니다. 잠시 후 다시 시도해주세요."
)
RATE_LIMIT_BURST_DETAIL = "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."
IMAGE_PHOTO_LIMIT_DETAIL = (
    "실사 이미지 생성 한도를 넘었습니다. 한 시간에 {limit}회까지 가능합니다. 잠시 후 다시 시도해주세요."
)
REGISTER_LIMIT_DETAIL = (
    "회원가입 한도를 넘었습니다. 한 시간에 {limit}회까지 가능합니다. 잠시 후 다시 시도해주세요."
)
PEXELS_SEARCH_LIMIT_DETAIL = (
    "외부 소재 검색 한도를 넘었습니다. 한 시간에 {limit}회까지 가능합니다. 잠시 후 다시 시도해주세요."
)
VERIFY_EMAIL_LIMIT_DETAIL = (
    "인증 메일 재발송 한도를 넘었습니다. 한 시간에 {limit}회까지 가능합니다. 잠시 후 다시 시도해주세요."
)
CAPTION_TRANSLATE_LIMIT_DETAIL = "자막 번역은 1시간에 {limit}번까지 할 수 있습니다. 잠시 후 다시 시도해 주세요."
PASSWORD_RESET_LIMIT_DETAIL = (
    "비밀번호 재설정 요청 한도를 넘었습니다. 한 시간에 {limit}회까지 가능합니다. 잠시 후 다시 시도해주세요."
)


def heavy_work_retry_after_seconds(settings: Settings) -> int:
    return max(1, settings.security_heavy_work_retry_after_seconds)


def rate_limit_retry_after_seconds(settings: Settings) -> int:
    configured = settings.security_rate_limit_retry_after_seconds
    if configured > 0:
        return configured
    rps = max(0.1, settings.security_rate_limit_rps)
    burst = max(1, settings.security_rate_limit_burst)
    return max(1, round(burst / rps))


class RateLimiter:
    def __init__(self, requests_per_second: float, burst: int) -> None:
        self.requests_per_second = max(0.1, requests_per_second)
        self.burst = max(1, burst)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self.burst / self.requests_per_second
        async with self._lock:
            bucket = self._events[key]
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= self.burst:
                return False
            bucket.append(now)
            return True


class HeavyWorkGuard:
    """Limits concurrent expensive operations per client IP."""

    def __init__(self, max_concurrent: int) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, *, endpoint: str | None = None) -> None:
        async with self._lock:
            if self._counts[key] >= self.max_concurrent:
                settings = get_settings()
                retry_after = heavy_work_retry_after_seconds(settings)
                log_event(
                    "heavy_work_guard_rejected",
                    "Heavy work guard rejected concurrent request",
                    level=logging.WARNING,
                    client_ip=key,
                    endpoint=endpoint,
                    occupied_slots=self._counts[key],
                    max_concurrent=self.max_concurrent,
                    retry_after_seconds=retry_after,
                )
                raise HTTPException(
                    status_code=429,
                    detail=HEAVY_WORK_LIMIT_DETAIL,
                    headers={"Retry-After": str(retry_after)},
                )
            self._counts[key] += 1

    async def release(self, key: str) -> None:
        async with self._lock:
            current = self._counts.get(key, 0)
            if current <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = current - 1


_rate_limiter: RateLimiter | None = None
_heavy_guard: HeavyWorkGuard | None = None
_photo_hourly_quota: HourlyQuota | None = None
_register_hourly_quota: HourlyQuota | None = None
_password_reset_hourly_quota: HourlyQuota | None = None
_pexels_search_hourly_quota: HourlyQuota | None = None
_verify_email_hourly_quota: HourlyQuota | None = None
_caption_translate_hourly_quota: HourlyQuota | None = None
_client_report_quotas: dict[str, HourlyQuota] = {}


class HourlyQuota:
    """Sliding 1-hour cap, separate from concurrent HeavyWorkGuard."""

    def __init__(
        self,
        max_per_hour: int,
        *,
        limit_detail: str = IMAGE_PHOTO_LIMIT_DETAIL,
        rejected_event: str = "image_photo_quota_rejected",
        reserved_event: str = "image_photo_quota_reserved",
        rejected_log: str = "Photo image hourly quota rejected",
        reserved_log: str = "Photo image hourly quota reserved before provider call",
        log_reserved: bool = True,
    ) -> None:
        self.max_per_hour = max(1, max_per_hour)
        self.limit_detail = limit_detail
        self.rejected_event = rejected_event
        self.reserved_event = reserved_event
        self.rejected_log = rejected_log
        self.reserved_log = reserved_log
        self.log_reserved = log_reserved
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def consume(self, key: str, *, endpoint: str | None = None) -> None:
        now = time.monotonic()
        window = 3600.0
        async with self._lock:
            bucket = self._events[key]
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= self.max_per_hour:
                retry_after = max(1, round(window - (now - bucket[0])))
                log_event(
                    self.rejected_event,
                    self.rejected_log,
                    level=logging.WARNING,
                    quota_key=key,
                    endpoint=endpoint,
                    used=len(bucket),
                    limit=self.max_per_hour,
                    retry_after_seconds=retry_after,
                )
                raise HTTPException(
                    status_code=429,
                    detail=self.limit_detail.format(limit=self.max_per_hour),
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)
            if not self.log_reserved:
                return
            log_event(
                self.reserved_event,
                self.reserved_log,
                quota_key=key,
                endpoint=endpoint,
                used=len(bucket),
                limit=self.max_per_hour,
            )


def get_photo_hourly_quota(settings: Settings) -> HourlyQuota:
    global _photo_hourly_quota
    if _photo_hourly_quota is None:
        _photo_hourly_quota = HourlyQuota(settings.image_photo_max_per_hour)
    return _photo_hourly_quota


def get_register_hourly_quota(settings: Settings) -> HourlyQuota:
    global _register_hourly_quota
    if _register_hourly_quota is None:
        _register_hourly_quota = HourlyQuota(
            settings.auth_register_max_per_hour,
            limit_detail=REGISTER_LIMIT_DETAIL,
            rejected_event="register_quota_rejected",
            reserved_event="register_quota_reserved",
            rejected_log="Register hourly quota rejected",
            reserved_log="Register hourly quota reserved",
        )
    return _register_hourly_quota


def get_pexels_search_hourly_quota(settings: Settings) -> HourlyQuota:
    global _pexels_search_hourly_quota
    if _pexels_search_hourly_quota is None:
        _pexels_search_hourly_quota = HourlyQuota(
            settings.pexels_search_max_per_hour,
            limit_detail=PEXELS_SEARCH_LIMIT_DETAIL,
            rejected_event="pexels_search_quota_rejected",
            reserved_event="pexels_search_quota_reserved",
            rejected_log="Pexels search hourly quota rejected",
            reserved_log="Pexels search hourly quota reserved",
        )
    return _pexels_search_hourly_quota


def get_verify_email_hourly_quota(settings: Settings) -> HourlyQuota:
    global _verify_email_hourly_quota
    if _verify_email_hourly_quota is None:
        _verify_email_hourly_quota = HourlyQuota(
            settings.auth_verify_email_max_per_hour,
            limit_detail=VERIFY_EMAIL_LIMIT_DETAIL,
            rejected_event="verify_email_quota_rejected",
            reserved_event="verify_email_quota_reserved",
            rejected_log="Verify email hourly quota rejected",
            reserved_log="Verify email hourly quota reserved",
        )
    return _verify_email_hourly_quota


def get_password_reset_hourly_quota(settings: Settings) -> HourlyQuota:
    global _password_reset_hourly_quota
    if _password_reset_hourly_quota is None:
        _password_reset_hourly_quota = HourlyQuota(
            settings.auth_password_reset_max_per_hour,
            limit_detail=PASSWORD_RESET_LIMIT_DETAIL,
            rejected_event="password_reset_quota_rejected",
            reserved_event="password_reset_quota_reserved",
            rejected_log="Password reset hourly quota rejected",
            reserved_log="Password reset hourly quota reserved",
        )
    return _password_reset_hourly_quota


def get_rate_limiter(settings: Settings) -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(
            settings.security_rate_limit_rps,
            settings.security_rate_limit_burst,
        )
    return _rate_limiter


def get_heavy_work_guard(settings: Settings) -> HeavyWorkGuard:
    global _heavy_guard
    if _heavy_guard is None:
        _heavy_guard = HeavyWorkGuard(settings.security_max_concurrent_heavy)
    return _heavy_guard


def get_caption_translate_hourly_quota(settings: Settings) -> HourlyQuota:
    global _caption_translate_hourly_quota
    if _caption_translate_hourly_quota is None:
        _caption_translate_hourly_quota = HourlyQuota(
            settings.caption_translate_max_per_hour,
            limit_detail=CAPTION_TRANSLATE_LIMIT_DETAIL,
            rejected_event="caption_translate_quota_rejected",
            reserved_event="caption_translate_quota_reserved",
            rejected_log="Caption translate hourly quota rejected",
            reserved_log="Caption translate hourly quota reserved",
        )
    return _caption_translate_hourly_quota


def get_client_report_quota(kind: str) -> HourlyQuota:
    """Per-IP hourly cap for unauthenticated browser reports ("errors" or "stats")."""
    quota = _client_report_quotas.get(kind)
    if quota is None:
        quota = HourlyQuota(
            CLIENT_REPORT_HOURLY_LIMITS[kind],
            limit_detail=RATE_LIMIT_BURST_DETAIL,
            rejected_event="client_report_quota_rejected",
            rejected_log=f"Client {kind} report hourly quota rejected",
            log_reserved=False,
        )
        _client_report_quotas[kind] = quota
    return quota


def reset_rate_limit_state_for_tests() -> None:
    global _rate_limiter, _heavy_guard, _photo_hourly_quota, _register_hourly_quota
    global _password_reset_hourly_quota, _verify_email_hourly_quota, _pexels_search_hourly_quota
    global _caption_translate_hourly_quota
    _caption_translate_hourly_quota = None
    _client_report_quotas.clear()
    _rate_limiter = None
    _heavy_guard = None
    _photo_hourly_quota = None
    _register_hourly_quota = None
    _password_reset_hourly_quota = None
    _verify_email_hourly_quota = None
    _pexels_search_hourly_quota = None


@lru_cache(maxsize=8)
def _trusted_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXY_IPS entry: %s", item)
    return tuple(networks)


def _is_trusted(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return any(ip.version == net.version and ip in net for net in networks)


def client_ip(request: Request) -> str:
    """The real client address.

    X-Forwarded-For is only believed when the connection itself comes from a trusted
    proxy (TRUSTED_PROXY_IPS). Its entries are then read right to left, skipping our
    own proxies; the first address that is not one of them is the client. A value a
    client typed into the header sits further left and is never reached.
    """
    peer = request.client.host if request.client and request.client.host else "unknown"
    networks = _trusted_networks(get_settings().trusted_proxy_ips)
    if not _is_trusted(peer, networks):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, networks):
            return hop
    # Every hop is one of our proxies (or there is no header): the closest one is all we know.
    return hops[0] if hops else peer


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        if not settings.security_rate_limit_active:
            return await call_next(request)

        path = request.url.path.rstrip("/") or "/"
        if request.method == "POST" and path in HEAVY_PATHS:
            limiter = get_rate_limiter(settings)
            allowed = await limiter.allow(f"{client_ip(request)}:{path}")
            if not allowed:
                retry_after = rate_limit_retry_after_seconds(settings)
                client = client_ip(request)
                log_event(
                    "rate_limit_rejected",
                    "Rate limit rejected burst request",
                    level=logging.WARNING,
                    client_ip=client,
                    endpoint=path,
                    retry_after_seconds=retry_after,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": RATE_LIMIT_BURST_DETAIL},
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)
