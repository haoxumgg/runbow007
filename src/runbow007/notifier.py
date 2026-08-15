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
        lines.extend(self._rule_lines(rule_code, candidates))
        return FeishuMessage(self.TITLES[rule_code], lines)

    def format_combined(
        self,
        rule_codes: tuple[str, ...],
        candidates: list[ReminderCandidate],
        *,
        current_candidates: list[ReminderCandidate] | None = None,
    ) -> FeishuMessage:
        """Put every selected rule into one post and preserve deduplication semantics."""
        unknown = set(rule_codes) - set(self.TITLES)
        if unknown:
            raise ValueError(f"未知规则: {', '.join(sorted(unknown))}")

        groups: dict[str, list[ReminderCandidate]] = {code: [] for code in rule_codes}
        for candidate in candidates:
            if candidate.rule_code in groups:
                groups[candidate.rule_code].append(candidate)
        current_groups: dict[str, list[ReminderCandidate]] = {
            code: [] for code in rule_codes
        }
        for candidate in current_candidates if current_candidates is not None else candidates:
            if candidate.rule_code in current_groups:
                current_groups[candidate.rule_code].append(candidate)

        lines: list[list[dict[str, Any]]] = [self._mention_line()]
        for rule_code in rule_codes:
            lines.append(self._text_line(f"【{rule_code}｜{self.TITLES[rule_code]}】"))
            group = groups[rule_code]
            current_group = current_groups[rule_code]
            if group:
                previously_sent = len(current_group) - len(group)
                if previously_sent > 0:
                    lines.append(
                        self._text_line(
                            f"当前符合条件共 {len(current_group)} 个订单；"
                            f"以下为本轮新增或到期重提醒的 {len(group)} 个订单，"
                            f"另有 {previously_sent} 个此前已提醒。"
                        )
                    )
                lines.extend(self._rule_lines(rule_code, group))
            elif current_group:
                lines.append(
                    self._text_line(
                        f"当前仍有 {len(current_group)} 个符合条件订单；"
                        "本轮无新增提醒（此前已提醒）。"
                    )
                )
            else:
                lines.append(self._text_line("无符合条件订单。"))

        title_prefix = (
            "R1–R4"
            if rule_codes == ("R1", "R2", "R3", "R4")
            else "、".join(rule_codes)
        )
        return FeishuMessage(f"{title_prefix}订单提醒汇总", lines)

    def _rule_lines(
        self, rule_code: str, candidates: list[ReminderCandidate]
    ) -> list[list[dict[str, Any]]]:
        if rule_code == "R1":
            return self._rule_1(candidates)
        if rule_code == "R2":
            return self._rule_2(candidates)
        if rule_code == "R3":
            return self._rule_3(candidates)
        if rule_code == "R4":
            return self._rule_4(candidates)
        raise ValueError(f"未知规则: {rule_code}")

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
        lines = [
            self._text_line(
                f"共 {len(candidates)} 单，离厂时间为空且WMS过账已超过配置阈值。"
            )
        ]
        lines.extend(
            self._text_line(
                f"- {item.order.order_no}｜箱数 {item.order.box_count}｜{item.reason}"
            )
            for item in candidates
        )
        return lines

    def _rule_2(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        total_boxes = sum(item.order.box_count for item in candidates)
        lines = [
            self._text_line(
                f"总共 {len(candidates)} 个订单，总共 {total_boxes} 箱。"
            )
        ]
        lines.extend(
            self._text_line(f"- {item.order.order_no}｜箱数 {item.order.box_count}")
            for item in candidates
        )
        return lines

    def _rule_3(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        unsigned = [item for item in candidates if item.scenario == "customer_unsigned"]
        pending = [item for item in candidates if item.scenario == "operation_pending"]
        lines: list[list[dict[str, Any]]] = []
        if unsigned:
            total_boxes = sum(item.order.box_count for item in unsigned)
            lines.append(self._text_line("【客户未电子签】"))
            lines.append(
                self._text_line(
                    f"总共 {len(unsigned)} 个订单，总共 {total_boxes} 箱。"
                )
            )
            lines.extend(
                self._text_line(f"- {item.order.order_no}｜箱数 {item.order.box_count}")
                for item in unsigned
            )
        if pending:
            lines.append(self._text_line("【运营未操作签收】"))
            lines.append(self._text_line(f"提醒内容：共 {len(pending)} 个订单。"))
            lines.extend(self._text_line(f"- {item.order.order_no}") for item in pending)
            lines.append(
                self._text_line(
                    "请运营人员将状态更新为「已签收」，合同状态为「已完成」。"
                )
            )
        return lines

    def _rule_4(self, candidates: list[ReminderCandidate]) -> list[list[dict[str, Any]]]:
        lines = [
            self._text_line(f"综合统计：共 {len(candidates)} 个订单。"),
            self._text_line("明细："),
        ]
        lines.extend(self._text_line(f"- {item.order.order_no}") for item in candidates)
        lines.append(
            self._text_line(
                "请督促相关人员及时填写延误原因，确保延误订单有完整的归因记录。"
            )
        )
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
