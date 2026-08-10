import pytest
from keyring.errors import NoKeyringError

from runbow007 import credentials


def test_environment_secrets_take_priority(monkeypatch):
    monkeypatch.setenv(credentials.TMS_PASSWORD_ENV, "tms-from-env")
    monkeypatch.setenv(credentials.FEISHU_SECRET_ENV, "feishu-from-env")

    def fail_if_called(*_args):
        raise AssertionError("环境变量存在时不应访问系统凭据库")

    monkeypatch.setattr(credentials.keyring, "get_password", fail_if_called)

    assert credentials.get_tms_password("user") == "tms-from-env"
    assert credentials.get_feishu_secret("app") == "feishu-from-env"


def test_missing_linux_keyring_has_actionable_error(monkeypatch):
    monkeypatch.delenv(credentials.TMS_PASSWORD_ENV, raising=False)

    def no_backend(*_args):
        raise NoKeyringError("no backend")

    monkeypatch.setattr(credentials.keyring, "get_password", no_backend)

    with pytest.raises(credentials.CredentialError, match=credentials.TMS_PASSWORD_ENV):
        credentials.get_tms_password("user")


def test_set_secret_explains_linux_environment_fallback(monkeypatch):
    def no_backend(*_args):
        raise NoKeyringError("no backend")

    monkeypatch.setattr(credentials.keyring, "set_password", no_backend)

    with pytest.raises(credentials.CredentialError, match=credentials.FEISHU_SECRET_ENV):
        credentials.set_feishu_secret("app", "secret")
