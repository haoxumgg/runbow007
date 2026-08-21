from pathlib import Path

import pytest

from runbow007.config import AppConfig, ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_server_identity_values_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("RUNBOW007_TMS_USERNAME", "server-user")
    monkeypatch.setenv("RUNBOW007_TMS_DOWNLOAD_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("RUNBOW007_FEISHU_APP_ID", "server-app")
    monkeypatch.setenv("RUNBOW007_FEISHU_CHAT_ID", "server-chat")

    config = AppConfig.load(ROOT / "config.example.yaml")

    assert config.tms.username == "server-user"
    assert config.tms.download_timeout_seconds == 1200
    assert config.feishu.app_id == "server-app"
    assert config.feishu.chat_id == "server-chat"


def test_blank_environment_values_do_not_replace_yaml(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
tms:
  username: yaml-user
feishu:
  app_id: yaml-app
  chat_id: yaml-chat
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNBOW007_TMS_USERNAME", "   ")
    monkeypatch.setenv("RUNBOW007_FEISHU_APP_ID", "")
    monkeypatch.setenv("RUNBOW007_FEISHU_CHAT_ID", "   ")

    config = AppConfig.load(config_file)

    assert config.tms.username == "yaml-user"
    assert config.feishu.app_id == "yaml-app"
    assert config.feishu.chat_id == "yaml-chat"


def test_upload_page_ships_with_the_documented_default_account():
    config = AppConfig.load(ROOT / "config.example.yaml")

    assert (config.web.username, config.web.password) == ("admin", "admin123456")
    assert config.web.port == 8080
    assert config.web.default_rules == ("R1", "R3", "R4")


def test_upload_page_credentials_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("RUNBOW007_WEB_USERNAME", "ops")
    monkeypatch.setenv("RUNBOW007_WEB_PASSWORD", "另一个口令")
    monkeypatch.setenv("RUNBOW007_WEB_PORT", "9090")

    config = AppConfig.load(ROOT / "config.example.yaml")

    assert (config.web.username, config.web.password) == ("ops", "另一个口令")
    assert config.web.port == 9090


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("  port: 0", "web.port"),
        ("  session_timeout_minutes: 1", "web.session_timeout_minutes"),
        ("  max_upload_mb: 0", "web.max_upload_mb"),
        ('  username: ""', "web.username"),
        ('  password: ""\n  password_hash: ""', "web.password"),
        ("  default_rules: [R9]", "未知规则"),
    ],
)
def test_upload_page_settings_are_validated(tmp_path, section, message):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"web:\n{section}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        AppConfig.load(config_file)
