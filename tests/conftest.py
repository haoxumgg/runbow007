from __future__ import annotations

from datetime import datetime

import pytest
from openpyxl import Workbook

from runbow007.config import (
    AppConfig,
    FeishuConfig,
    RulesConfig,
    RuntimeConfig,
    TmsConfig,
)
from runbow007.models import Order

HEADERS = [
    "所属组织",
    "承运商名称",
    "离厂时间(承运商提货时间)",
    "WMS过账时间",
    "预计到达时间",
    "订单号",
    "状态",
    "合同状态",
    "总箱数",
    "实际到达时间",
    "签收时间",
    "是否延迟",
    "延迟原因",
    "承运商时效",
    "电子签签署时间",
    "明细单总数",
]


@pytest.fixture
def make_order():
    def factory(**overrides):
        values = {
            "order_no": "C001",
            "organization": "华东中心仓-上海嘉定",
            "carrier": "华东虹迪",
            "departed_at": datetime(2026, 8, 5, 10, 0),
            "wms_posted_at": datetime(2026, 8, 5, 9, 15),
            "expected_arrival_at": datetime(2026, 8, 6, 18, 0),
            "transport_status": "运输在途（已离厂）",
            "contract_status": "签署中",
            "box_count": 10,
            "actual_arrival_at": None,
            "signed_at": None,
            "is_delayed": False,
            "delay_reason": None,
            "carrier_sla_hours": 24.0,
            "electronic_signed_at": None,
            "detail_count": 1,
            "source_row": 2,
        }
        values.update(overrides)
        return Order(**values)

    return factory


@pytest.fixture
def app_config(tmp_path):
    return AppConfig(
        source_path=tmp_path / "config.yaml",
        runtime=RuntimeConfig(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            logs_dir=tmp_path / "logs",
            browser_profile_dir=tmp_path / "browser-profile",
            database_path=tmp_path / "data" / "runbow007.db",
            lock_path=tmp_path / "data" / "runbow007.lock",
        ),
        tms=TmsConfig(username="test-user"),
        feishu=FeishuConfig(app_id="test-app", chat_id="test-chat"),
        rules=RulesConfig(),
    )


@pytest.fixture
def write_orders_xlsx():
    def writer(path, orders):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "maintainCompanyOrderPage"
        sheet.append(HEADERS)
        for order in orders:
            sheet.append(
                [
                    order.organization,
                    order.carrier,
                    order.departed_at,
                    order.wms_posted_at,
                    order.expected_arrival_at,
                    order.order_no,
                    order.transport_status,
                    order.contract_status,
                    order.box_count,
                    order.actual_arrival_at,
                    order.signed_at,
                    "是" if order.is_delayed else "否",
                    order.delay_reason,
                    order.carrier_sla_hours,
                    order.electronic_signed_at,
                    order.detail_count,
                ]
            )
        workbook.save(path)
        return path

    return writer
