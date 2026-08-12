from __future__ import annotations

import sqlite3

import pytest

from runbow007.pipeline import Pipeline


def _rows(database_path, query):
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(query).fetchall()


def test_pipeline_dry_run_archives_and_persists_real_workbook(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [make_order(order_no="D001", is_delayed=True, delay_reason=None)],
    )

    result = Pipeline(app_config).process_file(source, rule_codes=["R4"])

    assert result.row_count == 1
    assert result.candidate_count == 1
    assert result.sent_count == 0
    assert result.dry_run is True
    assert result.source_file.parent.parent == app_config.runtime.downloads_dir
    assert result.source_file.read_bytes() == source.read_bytes()
    run = _rows(app_config.runtime.database_path, "SELECT * FROM runs")[0]
    assert (run["status"], run["row_count"], run["candidate_count"]) == ("success", 1, 1)
    assert _rows(app_config.runtime.database_path, "SELECT state FROM reminder_events")[0][
        "state"
    ] == "open"
    assert not _rows(app_config.runtime.database_path, "SELECT * FROM deliveries")


def test_pipeline_sends_in_batches_and_deduplicates_same_day(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(order_no=f"D00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 4)
        ],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            assert config.chat_id == "test-chat"
            assert app_secret == "secret"

        def send(self, message):
            sent_messages.append(message)
            return f"om_{len(sent_messages)}"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    first = pipeline.process_file(source, rule_codes=["R4"], send=True)
    second = pipeline.process_file(source, rule_codes=["R4"], send=True)

    assert first.sent_count == 3
    assert second.sent_count == 0
    assert [len(message.content) for message in sent_messages] == [4, 3]
    deliveries = _rows(
        app_config.runtime.database_path,
        "SELECT status, message_id FROM deliveries ORDER BY id",
    )
    assert len(deliveries) == 3
    assert {row["status"] for row in deliveries} == {"sent"}
    assert [row["message_id"] for row in deliveries] == ["om_1", "om_1", "om_2"]


def test_pipeline_limited_send_is_stable_and_never_moves_to_later_orders(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(order_no=f"L00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 5)
        ],
    )

    class FakeClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            return "om_limited"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    first = pipeline.process_file(
        source, rule_codes=["R4"], send=True, max_send_orders=3
    )
    second = pipeline.process_file(
        source, rule_codes=["R4"], send=True, max_send_orders=3
    )

    assert first.sent_count == 3
    assert second.sent_count == 0
    deliveries = _rows(
        app_config.runtime.database_path,
        """
        SELECT DISTINCT reminder_events.order_no
        FROM deliveries
        JOIN reminder_events USING(event_key)
        WHERE deliveries.status = 'sent'
        ORDER BY reminder_events.order_no
        """,
    )
    assert [row["order_no"] for row in deliveries] == ["L001", "L002", "L003"]


@pytest.mark.parametrize("limit", [0, 6])
def test_pipeline_rejects_unsafe_send_limits(app_config, limit):
    with pytest.raises(ValueError, match="1–5"):
        Pipeline(app_config).process_file(
            "missing.xlsx", rule_codes=["R4"], send=True, max_send_orders=limit
        )


def test_pipeline_rejects_send_limit_in_dry_run(app_config):
    with pytest.raises(ValueError, match="真实发送"):
        Pipeline(app_config).process_file(
            "missing.xlsx", rule_codes=["R4"], send=False, max_send_orders=3
        )


def test_pipeline_records_failed_delivery_and_failed_run(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(order_no=f"F00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 4)
        ],
    )

    class FailingClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            raise RuntimeError("simulated Feishu outage")

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FailingClient)

    with pytest.raises(RuntimeError, match="simulated Feishu outage"):
        Pipeline(app_config).process_file(source, rule_codes=["R4"], send=True)

    failed_run = _rows(app_config.runtime.database_path, "SELECT * FROM runs")[0]
    assert failed_run["status"] == "failed"
    assert "simulated Feishu outage" in failed_run["error"]
    deliveries = _rows(app_config.runtime.database_path, "SELECT * FROM deliveries")
    assert len(deliveries) == 2
    assert {row["status"] for row in deliveries} == {"failed"}


def test_pipeline_rejects_unknown_or_disabled_rules(app_config):
    pipeline = Pipeline(app_config)
    with pytest.raises(ValueError, match="未知规则: RX"):
        pipeline.process_file("missing.xlsx", rule_codes=["RX"])

    app_config.rules.enabled = ("R1",)
    with pytest.raises(ValueError, match="请求的规则均未启用"):
        pipeline.process_file("missing.xlsx", rule_codes=["R4"])
