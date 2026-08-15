import json
from datetime import datetime

import pytest

from runbow007.config import FeishuConfig, RulesConfig
from runbow007.models import ReminderCandidate
from runbow007.notifier import FeishuClient, FeishuError, FeishuMessage, MessageFormatter
from runbow007.rules import RuleEngine


class FakeResponse:
    def __init__(self, status_code, data=None, *, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.data = data
        self.text = text

    def json(self):
        if isinstance(self.data, ValueError):
            raise self.data
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_message_contains_real_mention_token(make_order):
    order = make_order(departed_at=None)
    candidate = RuleEngine(RulesConfig()).evaluate(
        [order], now=datetime(2026, 8, 6), rule_codes=["R1"]
    )[0]
    message = MessageFormatter(
        mention_user_id="ou_xuhao", mention_name="许昊"
    ).format("R1", [candidate])
    assert message.content[0][0] == {"tag": "at", "user_id": "ou_xuhao"}
    assert order.order_no in message.content[-1][0]["text"]


def test_message_without_user_id_has_no_mention(make_order):
    candidate = RuleEngine(RulesConfig()).evaluate(
        [make_order(departed_at=None)], now=datetime(2026, 8, 6), rule_codes=["R1"]
    )[0]
    message = MessageFormatter(mention_user_id="", mention_name="许昊").format(
        "R1", [candidate]
    )

    assert message.content[0] == [{"tag": "text", "text": "请关注以下订单："}]
    assert "离厂时间为空" in message.content[1][0]["text"]


def test_formats_all_remaining_rule_messages(make_order):
    formatter = MessageFormatter(mention_user_id="", mention_name="许昊")
    in_transit = make_order(
        order_no="R2A",
        actual_arrival_at=datetime(2026, 8, 6),
        box_count=10,
    )
    second_in_transit = make_order(
        order_no="R2B",
        actual_arrival_at=datetime(2026, 8, 6, 18, 0),
        box_count=5,
    )
    r2 = RuleEngine(RulesConfig()).evaluate(
        [in_transit, second_in_transit],
        now=datetime(2026, 8, 6),
        rule_codes=["R2"],
    )
    r2_message = formatter.format("R2", r2)
    assert r2_message.content[1][0]["text"] == "总共 2 个订单，总共 15 箱。"
    assert r2_message.content[2][0]["text"] == "- R2A｜箱数 10"
    assert r2_message.content[3][0]["text"] == "- R2B｜箱数 5"

    signed = make_order(order_no="R3A", transport_status="已签收", box_count=12)
    transit = make_order(order_no="R3B", transport_status="运输在途")
    unsigned = ReminderCandidate("a", "R3", "customer_unsigned", "x", signed)
    pending = ReminderCandidate("b", "R3", "operation_pending", "x", transit)
    r3_message = formatter.format("R3", [unsigned, pending])
    r3_lines = [line[0]["text"] for line in r3_message.content]
    assert "总共 1 个订单，总共 12 箱。" in r3_lines
    assert "- R3A｜箱数 12" in r3_lines
    assert "提醒内容：共 1 个订单。" in r3_lines
    assert "- R3B" in r3_lines
    assert "请运营人员将状态更新为「已签收」，合同状态为「已完成」。" in r3_lines

    delayed = ReminderCandidate("c", "R4", "delay_reason_missing", "x", in_transit)
    r4_lines = [line[0]["text"] for line in formatter.format("R4", [delayed]).content]
    assert "综合统计：共 1 个订单。" in r4_lines
    assert "- R2A" in r4_lines
    assert "请督促相关人员及时填写延误原因，确保延误订单有完整的归因记录。" in r4_lines

    with pytest.raises(ValueError, match="没有可格式化"):
        formatter.format("R1", [])
    with pytest.raises(ValueError, match="未知规则"):
        formatter.format("RX", [delayed])


def test_formats_all_rules_in_one_message_with_empty_sections(make_order):
    formatter = MessageFormatter(mention_user_id="", mention_name="许昊")
    r2_order = make_order(order_no="COMBINED-R2", box_count=8)
    r4_order = make_order(order_no="COMBINED-R4")
    candidates = [
        ReminderCandidate("r2", "R2", "arrived_today", "x", r2_order),
        ReminderCandidate("r4", "R4", "delay_reason_missing", "x", r4_order),
    ]

    message = formatter.format_combined(("R1", "R2", "R3", "R4"), candidates)
    lines = [line[0]["text"] for line in message.content]

    assert message.title == "R1–R4订单提醒汇总"
    assert lines.count("无符合条件订单。") == 2
    assert "【R1｜WMS过账时效预警】" in lines
    assert "【R2｜今日签收提醒】" in lines
    assert "【R3｜合同签署状态异常提醒】" in lines
    assert "【R4｜延迟无原因提醒】" in lines
    assert "总共 1 个订单，总共 8 箱。" in lines
    assert "综合统计：共 1 个订单。" in lines

    with pytest.raises(ValueError, match="没有可格式化"):
        formatter.format_combined(("R1",), [])
    with pytest.raises(ValueError, match="未知规则: RX"):
        formatter.format_combined(("RX",), candidates)


def test_combined_message_does_not_call_suppressed_candidates_resolved(make_order):
    formatter = MessageFormatter(mention_user_id="", mention_name="许昊")
    new_r2 = ReminderCandidate(
        "new-r2", "R2", "arrival_today", "x", make_order(order_no="NEW-R2")
    )
    existing_r4 = ReminderCandidate(
        "old-r4",
        "R4",
        "delay_reason_missing",
        "x",
        make_order(order_no="OLD-R4"),
    )

    message = formatter.format_combined(
        ("R2", "R4"),
        [new_r2],
        current_candidates=[new_r2, existing_r4],
    )
    lines = [line[0]["text"] for line in message.content]

    assert "当前仍有 1 个符合条件订单；本轮无新增提醒（此前已提醒）。" in lines
    assert lines.count("无符合条件订单。") == 0


def test_feishu_client_sends_payload_and_reuses_token():
    session = FakeSession(
        [
            FakeResponse(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}),
            FakeResponse(200, {"code": 0, "data": {"message_id": "om_1"}}),
            FakeResponse(200, {"code": 0, "data": {"message_id": "om_2"}}),
        ]
    )
    client = FeishuClient(
        FeishuConfig(app_id="app", chat_id="chat"), app_secret="secret", session=session
    )
    message = FeishuMessage("测试", [[{"tag": "text", "text": "hello"}]])

    assert client.send(message) == "om_1"
    assert client.send(message) == "om_2"
    assert len(session.calls) == 3
    message_call = session.calls[1][1]
    assert message_call["params"] == {"receive_id_type": "chat_id"}
    assert message_call["headers"]["Authorization"] == "Bearer token"
    payload = message_call["json"]
    assert payload["receive_id"] == "chat"
    assert json.loads(payload["content"])["zh_cn"]["title"] == "测试"


def test_feishu_client_retries_transient_failures(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}),
            FakeResponse(500, {"code": 500, "msg": "busy"}),
            FakeResponse(200, {"code": 0, "data": {"message_id": "om_retry"}}),
        ]
    )
    sleeps = []
    monkeypatch.setattr("runbow007.notifier.time.sleep", sleeps.append)
    client = FeishuClient(
        FeishuConfig(app_id="app", chat_id="chat"), app_secret="secret", session=session
    )

    assert client.send(FeishuMessage("x", [])) == "om_retry"
    assert sleeps == [1]


def test_feishu_client_reports_token_and_message_errors():
    auth_session = FakeSession([FakeResponse(401, {"code": 999, "msg": "bad app"})])
    auth_client = FeishuClient(
        FeishuConfig(app_id="app", chat_id="chat"),
        app_secret="secret",
        session=auth_session,
    )
    with pytest.raises(FeishuError, match="获取飞书访问凭证失败"):
        auth_client.send(FeishuMessage("x", []))

    message_session = FakeSession(
        [
            FakeResponse(200, {"code": 0, "tenant_access_token": "token"}),
            FakeResponse(400, ValueError("invalid json"), text="upstream html"),
        ]
    )
    message_client = FeishuClient(
        FeishuConfig(app_id="app", chat_id="chat"),
        app_secret="secret",
        session=message_session,
    )
    with pytest.raises(FeishuError, match="飞书发送失败: 400 upstream html"):
        message_client.send(FeishuMessage("x", []))
