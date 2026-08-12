from pathlib import Path

from runbow007.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]


def test_server_identity_values_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("RUNBOW007_TMS_USERNAME", "server-user")
    monkeypatch.setenv("RUNBOW007_TMS_DOWNLOAD_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("RUNBOW007_FEISHU_APP_ID", "server-app")
    monkeypatch.setenv("RUNBOW007_FEISHU_CHAT_ID", "server-chat")

    config = AppConfig.load(ROOT / "config.example.yaml")

    assert config.tms.username == "server-user"
    assert config.tms.download_timeout_seconds == 600
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

