from __future__ import annotations

from datetime import datetime

import pytest

from runbow007.models import Order


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
