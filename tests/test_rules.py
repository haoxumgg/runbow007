from datetime import datetime
from zoneinfo import ZoneInfo

from runbow007.config import RulesConfig
from runbow007.rules import RuleEngine


def test_r1_detects_missing_departure_after_wms_timeout(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(
        departed_at=None,
        wms_posted_at=datetime(2026, 8, 5, 9, 0),
    )
    candidates = engine.evaluate(
        [order], now=datetime(2026, 8, 5, 10, 31), rule_codes=["R1"]
    )
    assert len(candidates) == 1
    assert candidates[0].scenario == "departure_missing_overdue"
    assert candidates[0].reason == "WMS过账已 91.0 分钟，仍无离厂时间"
    assert candidates[0].event_key == "R1|C001|2026-08-05T09:00:00"


def test_r1_uses_strict_timeout_and_requires_missing_departure(make_order):
    engine = RuleEngine(RulesConfig(wms_lead_minutes=90))
    boundary = make_order(
        order_no="C001", departed_at=None, wms_posted_at=datetime(2026, 8, 5, 9, 0)
    )
    departed = make_order(
        order_no="C002", departed_at=datetime(2026, 8, 5, 10, 0)
    )
    missing_wms = make_order(order_no="C003", departed_at=None, wms_posted_at=None)
    future_wms = make_order(
        order_no="C004", departed_at=None, wms_posted_at=datetime(2026, 8, 5, 11, 0)
    )
    assert not engine.evaluate(
        [boundary, departed, missing_wms, future_wms],
        now=datetime(2026, 8, 5, 10, 30),
        rule_codes=["R1"],
    )


def test_r1_compares_aware_local_now_with_naive_tms_timestamp(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(
        departed_at=None,
        wms_posted_at=datetime(2026, 8, 5, 9, 0),
    )
    candidates = engine.evaluate(
        [order],
        now=datetime(2026, 8, 5, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai")),
        rule_codes=["R1"],
    )
    assert len(candidates) == 1


def test_r2_uses_tms_expected_arrival(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(expected_arrival_at=datetime(2026, 8, 6, 22, 0), carrier_sla_hours=96)
    candidates = engine.evaluate([order], now=datetime(2026, 8, 6, 13, 30), rule_codes=["R2"])
    assert len(candidates) == 1


def test_r3_detects_both_scenarios(make_order):
    engine = RuleEngine(RulesConfig())
    unsigned = make_order(
        transport_status="已签收", contract_status="签署中", signed_at=datetime(2026, 8, 5)
    )
    pending = make_order(
        order_no="C002", transport_status="运输在途（已离厂）", contract_status="已完成"
    )
    candidates = engine.evaluate(
        [unsigned, pending], now=datetime(2026, 8, 6), rule_codes=["R3"]
    )
    assert {item.scenario for item in candidates} == {"customer_unsigned", "operation_pending"}


def test_r4_treats_whitespace_as_missing(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(is_delayed=True, delay_reason="   ")
    candidates = engine.evaluate([order], now=datetime(2026, 8, 6), rule_codes=["R4"])
    assert len(candidates) == 1
