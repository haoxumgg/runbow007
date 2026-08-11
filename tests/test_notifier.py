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
    order = make_order()
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
        [make_order()], now=datetime(2026, 8, 6), rule_codes=["R1"]
    )[0]
    message = MessageFormatter(mention_user_id="", mention_name="许昊").format(
        "R1", [candidate]
    )

    assert message.content[0] == [{"tag": "text", "text": "请关注以下订单："}]


def test_formats_all_remaining_rule_messages(make_order):
    formatter = MessageFormatter(mention_user_id="", mention_name="许昊")
    in_transit = make_order(order_no="R2A", expected_arrival_at=datetime(2026, 8, 6))
    signed = make_order(
        order_no="R2B",
        expected_arrival_at=datetime(2026, 8, 6),
        transport_status="已签收",
    )
    r2 = RuleEngine(RulesConfig()).evaluate(
        [in_transit, signed], now=datetime(2026, 8, 6), rule_codes=["R2"]
    )
    assert "【运输在途】1 单" in formatter.format("R2", r2).content[2][0]["text"]

    unsigned = ReminderCandidate("a", "R3", "customer_unsigned", "x", signed)
    pending = ReminderCandidate("b", "R3", "operation_pending", "x", in_transit)
    r3_message = formatter.format("R3", [unsigned, pending])
    assert any("客户未电子签" in line[0]["text"] for line in r3_message.content)
    assert any("运输状态更新" in line[0]["text"] for line in r3_message.content)

    delayed = ReminderCandidate("c", "R4", "delay_reason_missing", "x", in_transit)
    assert "R2A" in formatter.format("R4", [delayed]).content[-1][0]["text"]

    with pytest.raises(ValueError, match="没有可格式化"):
        formatter.format("R1", [])
    with pytest.raises(ValueError, match="未知规则"):
        formatter.format("RX", [delayed])


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
