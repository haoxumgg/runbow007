from datetime import datetime

from runbow007.config import RulesConfig
from runbow007.notifier import MessageFormatter
from runbow007.rules import RuleEngine


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
