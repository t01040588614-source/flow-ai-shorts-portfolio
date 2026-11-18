from pathlib import Path

from app.config import Settings
from app.launch_check import BEFORE_LAUNCH, REQUIRED, collect_findings, main, read_env_file, render

FULL_ROOT = {
    "JWT_SECRET": "s" * 48,
    "DOMAIN": "flow.example.com",
    "VITE_BIZ_NAME": "플로우",
    "VITE_BIZ_OWNER": "홍길동",
    "VITE_BIZ_REG_NO": "123-45-67890",
    "VITE_BIZ_MAIL_ORDER_NO": "2026-서울-0001",
    "VITE_BIZ_ADDRESS": "서울",
    "VITE_BIZ_PHONE": "02-000-0000",
    "VITE_BIZ_EMAIL": "help@example.com",
    "VITE_PRIVACY_OFFICER_NAME": "홍길동",
    "VITE_PRIVACY_OFFICER_CONTACT": "privacy@example.com",
    "VITE_POLICY_EFFECTIVE_DATE": "2026-12-01",
    "VITE_SITE_URL": "https://flow.example.com",
}


def _full_settings(**overrides) -> Settings:
    values = {
        "admin_email": "admin@example.com",
        "admin_password": "a-long-admin-pass",
        "smtp_host": "smtp.example.com",
        "smtp_username": "mailer",
        "smtp_password": "pw",
        "openai_api_key": "sk-test",
        "toss_client_key": "live_ck_x",
        "toss_secret_key": "live_sk_x",
        "telegram_bot_token": "t",
        "telegram_alert_chat_id": "1",
        "agent_labs_enabled": False,
        **overrides,
    }
    return Settings(**values)


def _missing(findings, level):
    return {item.name for item in findings if item.level == level and not item.ok}


def test_everything_filled_passes() -> None:
    findings = collect_findings(_full_settings(), FULL_ROOT)
    assert _missing(findings, REQUIRED) == set()
    assert _missing(findings, BEFORE_LAUNCH) == set()
    assert "모두 채워졌어요" in render(findings)


def test_empty_setup_lists_what_blocks_launch() -> None:
    findings = collect_findings(Settings(admin_email="", admin_password="", smtp_host=""), {})
    blocking = _missing(findings, REQUIRED)
    assert {"JWT_SECRET", "DOMAIN", "ADMIN_EMAIL / ADMIN_PASSWORD", "VITE_POLICY_EFFECTIVE_DATE"} <= blocking
    assert any(name.startswith("SMTP_HOST") for name in blocking)


def test_short_secret_localhost_domain_and_test_toss_key_are_flagged() -> None:
    root = {**FULL_ROOT, "JWT_SECRET": "short", "DOMAIN": "https://flow.example.com"}
    findings = collect_findings(_full_settings(toss_secret_key="test_sk_x"), root)
    assert {"JWT_SECRET", "DOMAIN"} <= _missing(findings, REQUIRED)
    toss = next(item for item in findings if item.name.startswith("TOSS"))
    assert not toss.ok and "test_" in toss.note


def test_missing_business_fields_are_named() -> None:
    root = {key: value for key, value in FULL_ROOT.items() if key != "VITE_BIZ_REG_NO"}
    findings = collect_findings(_full_settings(), root)
    biz = next(item for item in findings if item.name.startswith("VITE_BIZ"))
    assert not biz.ok and "사업자등록번호" in biz.note


def test_report_never_prints_secret_values() -> None:
    settings = _full_settings(smtp_password="super-secret-mail-pw", openai_api_key="sk-very-secret")
    text = render(collect_findings(settings, {**FULL_ROOT, "JWT_SECRET": "j" * 40 + "Zq9Marker"}))
    for secret in ("super-secret-mail-pw", "sk-very-secret", "Zq9Marker", "live_sk_x"):
        assert secret not in text


def test_read_env_file_strips_quotes_and_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nDOMAIN=\"flow.example.com\"\r\nEMPTY=\nVITE_BIZ_NAME='플로우'\n", encoding="utf-8")
    values = read_env_file(env)
    assert values == {"DOMAIN": "flow.example.com", "EMPTY": "", "VITE_BIZ_NAME": "플로우"}
    assert read_env_file(tmp_path / "missing.env") == {}


def test_exit_code_is_one_when_something_required_is_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ADMIN_EMAIL", "")
    assert main(["--root-env", str(tmp_path / "none.env")]) == 1
    assert "필수" in capsys.readouterr().out


def test_jwt_secret_comes_from_the_top_level_env_when_given() -> None:
    settings = _full_settings(jwt_secret="b" * 48)
    root = {key: value for key, value in FULL_ROOT.items() if key != "JWT_SECRET"}
    assert "JWT_SECRET" in _missing(collect_findings(settings, root), REQUIRED)
    assert "JWT_SECRET" not in _missing(collect_findings(settings, {}), REQUIRED)
