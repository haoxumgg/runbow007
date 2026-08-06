from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import FeishuConfig
from .models import ReminderCandidate


class FeishuError(RuntimeError):
    """Raised when Feishu rejects a message."""


@dataclass(frozen=True, slots=True)
class FeishuMessage:
    title: str
    content: list[list[dict[str, Any]]]


class MessageFormatter:
    TITLES = {
        "R1": "WMS过账时效预警",
        "R2": "今日签收提醒",
        "R3": "合同签署状态异常提醒",
        "R4": "延迟无原因提醒",
    }

    def __init__(self, *, mention_user_id: str, mention_name: str) -> None:
        self.mention_user_id = mention_user_id
        self.mention_name = mention_name

    def format(self, rule_code: str, candidates: list[ReminderCandidate]) -> FeishuMessage:
        if not candidates:
            raise ValueError("没有可格式化的提醒")
        lines: list[list[dict[str, Any]]] = [self._mention_line()]
        if rule_code == "R1":
            lines.extend(self._rule_1(candidates))
        elif rule_code == "R2":
            lines.extend(self._rule_2(candidates))
        elif rule_code == "R3":
            lines.extend(self._rule_3(candidates))
        elif rule_code == "R4":
            lines.extend(self._rule_4(candidates))
        else:
            raise ValueError(f"未知规则: {rule_code}")
        return FeishuMessage(self.TITLES[rule_code], lines)

    def _mention_line(self) -> list[dict[str, Any]]:
        if self.mention_user_id:
            return [
                {"tag": "at", "user_id": self.mention_user_id},
                {"tag": "text", "text": " 请关注以下订单："},
            ]
        return [{"tag": "text", "text": "请关注以下订单："}]

    @staticmethod
    def _text_line(text: str) -> list[dict[str, Any]]:
        return [{"tag": "text", "text": text}]

    def _rule_1(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        lines = [self._text_line(f"共 {len(candidates)} 单，WMS过账距离离厂不足配置阈值。")]
        lines.extend(
            self._text_line(
                f"- {item.order.order_no}｜箱数 {item.order.box_count}｜{item.reason}"
            )
            for item in candidates
        )
        return lines

    def _rule_2(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        in_transit = [item for item in candidates if item.order.transport_status != "已签收"]
        signed = [item for item in candidates if item.order.transport_status == "已签收"]
        lines = [self._text_line(f"今日预计到达共 {len(candidates)} 单。")]
        if in_transit:
            lines.append(self._text_line(f"【运输在途】{len(in_transit)} 单，重点关注："))
            lines.extend(
                self._text_line(f"- {item.order.order_no}｜箱数 {item.order.box_count}")
                for item in in_transit
            )
        lines.append(self._text_line(f"【已签收】{len(signed)} 单。"))
        return lines

    def _rule_3(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        unsigned = [item for item in candidates if item.scenario == "customer_unsigned"]
        pending = [item for item in candidates if item.scenario == "operation_pending"]
        lines: list[list[dict[str, Any]]] = []
        if unsigned:
            lines.append(self._text_line(f"【客户未电子签】{len(unsigned)} 单："))
            lines.extend(
                self._text_line(f"- {item.order.order_no}｜箱数 {item.order.box_count}")
                for item in unsigned
            )
        if pending:
            lines.append(self._text_line(f"【运营未操作签收】{len(pending)} 单："))
            lines.extend(self._text_line(f"- {item.order.order_no}") for item in pending)
            lines.append(self._text_line("请将运输状态更新为“已签收”。"))
        return lines

    def _rule_4(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        lines = [self._text_line(f"共 {len(candidates)} 单延迟但未填写原因：")]
        lines.extend(self._text_line(f"- {item.order.order_no}") for item in candidates)
        return lines


class FeishuClient:
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
    MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(
        self,
        config: FeishuConfig,
        *,
        app_secret: str,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.app_secret = app_secret
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires_at = 0.0

    def send(self, message: FeishuMessage) -> str:
        payload = {
            "receive_id": self.config.chat_id,
            "msg_type": "post",
            "content": json.dumps(
                {"zh_cn": {"title": message.title, "content": message.content}},
                ensure_ascii=False,
            ),
        }
        response_data: dict[str, Any] | None = None
        for attempt, delay in enumerate((0, 1, 3), start=1):
            if delay:
                time.sleep(delay)
            token = self._access_token()
            response = self.session.post(
                self.MESSAGE_URL,
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=self.config.request_timeout_seconds,
            )
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"code": response.status_code, "msg": response.text[:300]}
            if response.ok and response_data.get("code") == 0:
                return str(response_data.get("data", {}).get("message_id", ""))
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                break
        raise FeishuError(
            f"飞书发送失败: {response_data.get('code')} {response_data.get('msg')}"
            if response_data
            else "飞书发送失败"
        )

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = self.session.post(
            self.TOKEN_URL,
            json={"app_id": self.config.app_id, "app_secret": self.app_secret},
            timeout=self.config.request_timeout_seconds,
        )
        data = response.json()
        if not response.ok or data.get("code") != 0:
            raise FeishuError(f"获取飞书访问凭证失败: {data.get('code')} {data.get('msg')}")
        self._token = str(data["tenant_access_token"])
        expires = int(data.get("expire", 7200))
        self._token_expires_at = time.monotonic() + max(60, expires - 300)
        return self._token
