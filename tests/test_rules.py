from datetime import datetime

from runbow007.config import RulesConfig
from runbow007.rules import RuleEngine


def test_r1_uses_departure_minus_wms(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order()
    candidates = engine.evaluate([order], now=datetime(2026, 8, 6), rule_codes=["R1"])
    assert len(candidates) == 1
    assert candidates[0].reason.endswith("45.0 分钟")


def test_r1_rejects_wms_after_departure_and_90_minute_boundary(make_order):
    engine = RuleEngine(RulesConfig(wms_lead_minutes=90))
    after = make_order(wms_posted_at=datetime(2026, 8, 5, 10, 1))
    boundary = make_order(order_no="C002", wms_posted_at=datetime(2026, 8, 5, 8, 30))
    assert not engine.evaluate([after, boundary], now=datetime(2026, 8, 6), rule_codes=["R1"])


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
