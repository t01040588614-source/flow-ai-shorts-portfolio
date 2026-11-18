"""출시 전 설정 점검: 빠진 운영 설정값을 한글로 알려 준다. 값 자체는 출력하지 않는다.

서버에서:  bash scripts/launch_check.sh
이 PC에서: backend\\.venv\\Scripts\\python -m app.launch_check --root-env .env

backend/.env(서버 설정)는 Settings로, 저장소 맨 위 .env(DOMAIN·JWT_SECRET·화면 빌드 값)는
--root-env 파일로 읽는다. 필수 항목이 하나라도 빠지면 종료 코드 1.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

REQUIRED = "필수"
BEFORE_LAUNCH = "출시 전"
OPTIONAL = "선택"

MIN_JWT_SECRET_CHARS = 32
MIN_ADMIN_PASSWORD_CHARS = 12

BUSINESS_FIELDS = {
    "VITE_BIZ_NAME": "상호",
    "VITE_BIZ_OWNER": "대표자",
    "VITE_BIZ_REG_NO": "사업자등록번호",
    "VITE_BIZ_MAIL_ORDER_NO": "통신판매업 신고번호",
    "VITE_BIZ_ADDRESS": "주소",
    "VITE_BIZ_PHONE": "전화번호",
    "VITE_BIZ_EMAIL": "이메일",
}
PRIVACY_FIELDS = {
    "VITE_PRIVACY_OFFICER_NAME": "개인정보 보호책임자 이름",
    "VITE_PRIVACY_OFFICER_CONTACT": "개인정보 보호책임자 연락처",
}


@dataclass(frozen=True)
class Finding:
    level: str  # REQUIRED, BEFORE_LAUNCH, OPTIONAL
    name: str
    ok: bool
    note: str


def read_env_file(path: Path | None) -> dict[str, str]:
    """KEY=VALUE lines (quotes and a trailing CR removed). Missing file → empty."""
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _filled(value: str) -> bool:
    return bool(value.strip())


def _server_findings(settings: Settings, root: Mapping[str, str]) -> list[Finding]:
    # docker compose takes JWT_SECRET from the top-level .env and it overrides backend/.env,
    # so with that file given only its value counts.
    jwt = root.get("JWT_SECRET", "") if root else settings.jwt_secret
    domain = root.get("DOMAIN", "")
    findings = [
        Finding(
            REQUIRED,
            "JWT_SECRET",
            len(jwt.strip()) >= MIN_JWT_SECRET_CHARS,
            f"로그인 토큰 서명 키. 맨 위 .env에 {MIN_JWT_SECRET_CHARS}자 이상 무작위 값 (없으면 운영 서버가 뜨지 않음)",
        ),
        Finding(
            REQUIRED,
            "DOMAIN",
            _filled(domain) and "://" not in domain and domain not in {"localhost", "127.0.0.1"},
            "맨 위 .env에 https 없이 도메인만 (예: flow.example.com). 없으면 HTTPS가 붙지 않음",
        ),
        Finding(
            REQUIRED,
            "ADMIN_EMAIL / ADMIN_PASSWORD",
            _filled(settings.admin_email) and len(settings.admin_password) >= MIN_ADMIN_PASSWORD_CHARS,
            f"첫 관리자 계정 (비밀번호 {MIN_ADMIN_PASSWORD_CHARS}자 이상). 운영에서 비어 있으면 관리자가 없음",
        ),
        Finding(
            REQUIRED,
            "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD",
            all(_filled(v) for v in (settings.smtp_host, settings.smtp_username, settings.smtp_password)),
            "가입 인증·비밀번호 재설정 메일. 없으면 사용자가 인증도, 비밀번호 찾기도 못 함",
        ),
        Finding(
            REQUIRED,
            "AGENT_LABS_ENABLED",
            not settings.agent_labs_enabled,
            "개발용 실험 기능은 운영에서 꺼야 함",
        ),
        Finding(
            REQUIRED,
            "SECURITY_RATE_LIMIT_ENABLED",
            settings.security_rate_limit_enabled.strip().lower() not in {"false", "0", "no", "off"},
            "요청 횟수 제한. 운영에서 끄면 가입·로그인 대량 시도를 못 막음",
        ),
        Finding(
            BEFORE_LAUNCH,
            "OPENAI_API_KEY",
            _filled(settings.openai_api_key),
            "대본·장면 그림·자동 자막. 없으면 규칙 기반 데모 대본과 카드 그림으로만 만들어짐",
        ),
        _toss_finding(settings),
        Finding(
            BEFORE_LAUNCH,
            "TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID",
            _filled(settings.telegram_bot_token) and _filled(settings.telegram_alert_chat_id),
            "장애 알림(서버 시작·실패 급증·디스크·배포 실패). 없으면 서버 로그에만 남음",
        ),
        Finding(
            OPTIONAL,
            "STORAGE_BACKEND",
            True,
            f"지금 '{settings.storage_backend}'. local이면 결과 영상이 도커 볼륨에 있어 백업을 따로 정해야 함",
        ),
    ]
    return findings


def _toss_finding(settings: Settings) -> Finding:
    client, secret = settings.toss_client_key.strip(), settings.toss_secret_key.strip()
    if not client or not secret:
        return Finding(
            BEFORE_LAUNCH,
            "TOSS_CLIENT_KEY / TOSS_SECRET_KEY",
            False,
            "결제. 없으면 구매 버튼이 꺼지고 '관리자 문의'만 보임",
        )
    if client.startswith("test_") or secret.startswith("test_"):
        return Finding(
            BEFORE_LAUNCH,
            "TOSS_CLIENT_KEY / TOSS_SECRET_KEY",
            False,
            "시험 키(test_)가 들어 있음. 실제로 돈을 받으려면 가맹 계약 뒤 live_ 키로 교체",
        )
    return Finding(BEFORE_LAUNCH, "TOSS_CLIENT_KEY / TOSS_SECRET_KEY", True, "결제 실제 키")


def _site_findings(root: Mapping[str, str]) -> list[Finding]:
    missing_biz = [label for key, label in BUSINESS_FIELDS.items() if not _filled(root.get(key, ""))]
    missing_privacy = [label for key, label in PRIVACY_FIELDS.items() if not _filled(root.get(key, ""))]
    return [
        Finding(
            REQUIRED,
            "VITE_BIZ_* (사업자 표시)",
            not missing_biz,
            "전자상거래법 사업자 표시" + (f" — 빠짐: {', '.join(missing_biz)}" if missing_biz else ""),
        ),
        Finding(
            REQUIRED,
            "VITE_PRIVACY_OFFICER_*",
            not missing_privacy,
            "개인정보처리방침의 보호책임자" + (f" — 빠짐: {', '.join(missing_privacy)}" if missing_privacy else ""),
        ),
        Finding(
            REQUIRED,
            "VITE_POLICY_EFFECTIVE_DATE",
            _filled(root.get("VITE_POLICY_EFFECTIVE_DATE", "")),
            "약관·개인정보처리방침 시행일 (예: 2026-12-01)",
        ),
        Finding(
            BEFORE_LAUNCH,
            "VITE_SITE_URL",
            root.get("VITE_SITE_URL", "").startswith("https://"),
            "공유 카드 그림 주소 (예: https://flow.example.com). 없으면 카카오톡 공유에 그림이 안 보일 수 있음",
        ),
    ]


def collect_findings(settings: Settings, root: Mapping[str, str]) -> list[Finding]:
    return [*_server_findings(settings, root), *_site_findings(root)]


def render(findings: list[Finding]) -> str:
    lines = ["출시 전 설정 점검 (값은 보여 주지 않음)", ""]
    for level in (REQUIRED, BEFORE_LAUNCH, OPTIONAL):
        group = [item for item in findings if item.level == level]
        if not group:
            continue
        lines.append(f"[{level}]")
        for item in group:
            mark = "  확인" if item.ok else "  빠짐"
            if level == OPTIONAL:
                mark = "  참고"
            lines.append(f"{mark}  {item.name}: {item.note}")
        lines.append("")
    blocking = [item for item in findings if item.level == REQUIRED and not item.ok]
    soon = [item for item in findings if item.level == BEFORE_LAUNCH and not item.ok]
    if blocking:
        lines.append(f"필수 {len(blocking)}개가 빠져 있어요. 채운 뒤 다시 실행하세요.")
    elif soon:
        lines.append(f"필수는 모두 채워졌어요. 출시 전 항목 {len(soon)}개가 남았어요.")
    else:
        lines.append("모두 채워졌어요.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="출시 전 설정 점검")
    parser.add_argument("--root-env", type=Path, default=None, help="저장소 맨 위 .env 경로")
    args = parser.parse_args(argv)
    root = read_env_file(args.root_env)
    findings = collect_findings(Settings(), root)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    print(render(findings))
    return 1 if any(item.level == REQUIRED and not item.ok for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
