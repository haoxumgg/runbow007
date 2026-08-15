from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Order:
    order_no: str
    organization: str
    carrier: str
    departed_at: datetime | None
    wms_posted_at: datetime | None
    expected_arrival_at: datetime | None
    transport_status: str
    contract_status: str
    box_count: int
    actual_arrival_at: datetime | None
    signed_at: datetime | None
    is_delayed: bool
    delay_reason: str | None
    carrier_sla_hours: float | None
    electronic_signed_at: datetime | None
    detail_count: int | None
    source_row: int

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in payload.items():
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class ParsedWorkbook:
    path: Path
    sheet_name: str
    headers: tuple[str, ...]
    orders: tuple[Order, ...]

    @property
    def row_count(self) -> int:
        return len(self.orders)

    @property
    def unique_order_count(self) -> int:
        return len({order.order_no for order in self.orders})


@dataclass(frozen=True, slots=True)
class ReminderCandidate:
    event_key: str
    rule_code: str
    scenario: str
    reason: str
    order: Order


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    source_file: Path
    row_count: int
    candidate_count: int
    sent_count: int
    dry_run: bool
