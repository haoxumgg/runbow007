from __future__ import annotations

import keyring

TMS_SERVICE = "runbow007:tms"
FEISHU_SERVICE = "runbow007:feishu"


class CredentialError(RuntimeError):
    """Raised when a required secret is unavailable."""


def get_tms_password(username: str) -> str:
    if not username:
        raise CredentialError("请先在 config.yaml 配置 tms.username")
    password = keyring.get_password(TMS_SERVICE, username)
    if not password:
        raise CredentialError("未找到 TMS 密码，请运行 credentials set-tms")
    return password


def set_tms_password(username: str, password: str) -> None:
    keyring.set_password(TMS_SERVICE, username, password)


def get_feishu_secret(app_id: str) -> str:
    if not app_id:
        raise CredentialError("请先在 config.yaml 配置 feishu.app_id")
    secret = keyring.get_password(FEISHU_SERVICE, app_id)
    if not secret:
        raise CredentialError("未找到飞书 App Secret，请运行 credentials set-feishu")
    return secret


def set_feishu_secret(app_id: str, secret: str) -> None:
    keyring.set_password(FEISHU_SERVICE, app_id, secret)
