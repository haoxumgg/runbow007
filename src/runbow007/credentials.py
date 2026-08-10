from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError

TMS_SERVICE = "runbow007:tms"
FEISHU_SERVICE = "runbow007:feishu"
TMS_PASSWORD_ENV = "RUNBOW007_TMS_PASSWORD"
FEISHU_SECRET_ENV = "RUNBOW007_FEISHU_APP_SECRET"


class CredentialError(RuntimeError):
    """Raised when a required secret is unavailable."""


def get_tms_password(username: str) -> str:
    if not username:
        raise CredentialError("请先在 config.yaml 配置 tms.username")
    password = _environment_secret(TMS_PASSWORD_ENV)
    if password:
        return password
    password = _keyring_get(TMS_SERVICE, username)
    if not password:
        raise CredentialError(
            f"未找到 TMS 密码，请设置环境变量 {TMS_PASSWORD_ENV}，"
            "或运行 credentials set-tms"
        )
    return password


def set_tms_password(username: str, password: str) -> None:
    _keyring_set(TMS_SERVICE, username, password, TMS_PASSWORD_ENV)


def get_feishu_secret(app_id: str) -> str:
    if not app_id:
        raise CredentialError("请先在 config.yaml 配置 feishu.app_id")
    secret = _environment_secret(FEISHU_SECRET_ENV)
    if secret:
        return secret
    secret = _keyring_get(FEISHU_SERVICE, app_id)
    if not secret:
        raise CredentialError(
            f"未找到飞书 App Secret，请设置环境变量 {FEISHU_SECRET_ENV}，"
            "或运行 credentials set-feishu"
        )
    return secret


def set_feishu_secret(app_id: str, secret: str) -> None:
    _keyring_set(FEISHU_SERVICE, app_id, secret, FEISHU_SECRET_ENV)


def _environment_secret(name: str) -> str | None:
    value = os.getenv(name)
    return value if value and value.strip() else None


def _keyring_get(service: str, account: str) -> str | None:
    try:
        return keyring.get_password(service, account)
    except KeyringError:
        return None


def _keyring_set(service: str, account: str, secret: str, env_name: str) -> None:
    try:
        keyring.set_password(service, account, secret)
    except KeyringError as exc:
        raise CredentialError(
            f"当前系统没有可用的凭据库；Linux 服务器请设置环境变量 {env_name}"
        ) from exc
