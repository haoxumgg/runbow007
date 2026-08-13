from datetime import datetime

import pytest
from openpyxl import Workbook

from runbow007.excel import WorkbookValidationError, read_orders

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


def _write_sample(path, *, duplicate=False, blank_departure=False):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "maintainCompanyOrderPage"
    sheet.append(HEADERS)
    row = [
        "华东中心仓-上海嘉定",
        "华东虹迪",
        None if blank_departure else datetime(2026, 8, 5, 10, 0),
        datetime(2026, 8, 5, 9, 15),
        datetime(2026, 8, 6, 18, 0),
        "C001",
        "运输在途（已离厂）",
        "签署中",
        10,
        None,
        None,
        "否",
        None,
        24,
        None,
        1,
    ]
    sheet.append(row)
    if duplicate:
        sheet.append(row)
    workbook.save(path)


def test_reads_xlsx_and_maps_actual_headers(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path)
    parsed = read_orders(path, expected_ui_total=1)
    assert parsed.sheet_name == "maintainCompanyOrderPage"
    assert parsed.unique_order_count == 1
    assert parsed.orders[0].carrier_sla_hours == 24
    assert parsed.orders[0].is_delayed is False


def test_allows_blank_departure_time_for_r1(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, blank_departure=True)

    parsed = read_orders(path, expected_ui_total=1)

    assert parsed.orders[0].departed_at is None


def test_rejects_ui_total_mismatch(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path)
    with pytest.raises(WorkbookValidationError, match="页面显示 2 条"):
        read_orders(path, expected_ui_total=2)


def test_rejects_duplicate_order_numbers(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, duplicate=True)
    with pytest.raises(WorkbookValidationError, match="订单号重复"):
        read_orders(path)
