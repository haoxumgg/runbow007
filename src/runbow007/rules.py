from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .config import RulesConfig
from .models import Order, ReminderCandidate


class RuleEngine:
    def __init__(self, config: RulesConfig) -> None:
        self.config = config

    def evaluate(
        self,
        orders: Iterable[Order],
        *,
        now: datetime,
        rule_codes: Iterable[str] | None = None,
    ) -> list[ReminderCandidate]:
        selected = {code.upper() for code in (rule_codes or self.config.enabled)}
        selected &= set(self.config.enabled)
        candidates: list[ReminderCandidate] = []
        for order in orders:
            if "R1" in selected:
                candidate = self._rule_1(order, now)
                if candidate:
                    candidates.append(candidate)
            if "R2" in selected:
                candidate = self._rule_2(order, now)
                if candidate:
                    candidates.append(candidate)
            if "R3" in selected:
                candidate = self._rule_3(order)
                if candidate:
                    candidates.append(candidate)
            if "R4" in selected:
                candidate = self._rule_4(order)
                if candidate:
                    candidates.append(candidate)
        return candidates

    def _rule_1(self, order: Order, now: datetime) -> ReminderCandidate | None:
        if order.departed_at is not None or order.wms_posted_at is None:
            return None
        local_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
        elapsed_minutes = (local_now - order.wms_posted_at).total_seconds() / 60
        if elapsed_minutes <= self.config.wms_lead_minutes:
            return None
        key = "|".join(("R1", order.order_no, order.wms_posted_at.isoformat()))
        return ReminderCandidate(
            key,
            "R1",
            "departure_missing_overdue",
            f"WMS过账已 {elapsed_minutes:.1f} 分钟，仍无离厂时间",
            order,
        )

    @staticmethod
    def _rule_2(order: Order, now: datetime) -> ReminderCandidate | None:
        if (
            order.actual_arrival_at is None
            or order.actual_arrival_at.date() != now.date()
            or order.transport_status != "运输在途（已离厂）"
        ):
            return None
        return ReminderCandidate(
            f"R2|{order.order_no}|{now.date().isoformat()}",
            "R2",
            "arrival_today",
            "实际到达日期为今天但运输状态仍为在途",
            order,
        )

    @staticmethod
    def _rule_3(order: Order) -> ReminderCandidate | None:
        if (
            order.actual_arrival_at is None
            or order.signed_at is None
            or order.actual_arrival_at != order.signed_at
        ):
            return None
        if order.transport_status == "已签收" and order.contract_status == "签署中":
            return ReminderCandidate(
                f"R3|unsigned|{order.order_no}",
                "R3",
                "customer_unsigned",
                "实际到达时间与签收时间一致，订单已签收但合同仍在签署中",
                order,
            )
        if (
            order.transport_status == "运输在途"
            and order.contract_status == "已完成"
        ):
            return ReminderCandidate(
                f"R3|operation_pending|{order.order_no}",
                "R3",
                "operation_pending",
                "实际到达时间与签收时间一致，合同已完成但运输状态仍为在途",
                order,
            )
        return None

    @staticmethod
    def _rule_4(order: Order) -> ReminderCandidate | None:
        if not order.is_delayed or (order.delay_reason and order.delay_reason.strip()):
            return None
        return ReminderCandidate(
            f"R4|{order.order_no}",
            "R4",
            "delay_reason_missing",
            "订单已延迟但未填写延迟原因",
            order,
        )
