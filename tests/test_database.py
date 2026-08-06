from datetime import datetime, timedelta

from runbow007.database import SQLiteStore
from runbow007.models import ReminderCandidate


def test_event_dedup_and_daily_repeat(tmp_path, make_order):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    now = datetime(2026, 8, 5, 10, 0)
    candidate = ReminderCandidate("R4|C001", "R4", "missing", "missing", make_order())
    store.begin_run("run1", tmp_path / "a.xls", "abc", now)
    store.sync_candidates(
        [candidate], selected_rules=["R4"], observed_order_nos=["C001"], seen_at=now
    )
    assert store.should_send(candidate, now=now, repeat_hour=9)

    store.mark_sent([candidate], run_id="run1", message_id="om_1", sent_at=now)
    assert not store.should_send(candidate, now=now + timedelta(hours=1), repeat_hour=9)
    assert not store.should_send(candidate, now=now + timedelta(hours=22), repeat_hour=9)
    assert store.should_send(candidate, now=now + timedelta(days=1), repeat_hour=9)


def test_resolves_missing_candidate(tmp_path, make_order):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    now = datetime(2026, 8, 5, 10, 0)
    candidate = ReminderCandidate("R4|C001", "R4", "missing", "missing", make_order())
    store.sync_candidates(
        [candidate], selected_rules=["R4"], observed_order_nos=["C001"], seen_at=now
    )
    store.sync_candidates(
        [],
        selected_rules=["R4"],
        observed_order_nos=["C001"],
        seen_at=now + timedelta(hours=1),
    )
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM reminder_events WHERE event_key = ?", (candidate.event_key,)
        ).fetchone()
    assert row["state"] == "resolved"


def test_absent_order_is_not_resolved_and_reopen_sends_immediately(tmp_path, make_order):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    now = datetime(2026, 8, 5, 10, 0)
    candidate = ReminderCandidate("R4|C001", "R4", "missing", "missing", make_order())
    store.begin_run("run1", tmp_path / "a.xls", "abc", now)
    store.sync_candidates(
        [candidate], selected_rules=["R4"], observed_order_nos=["C001"], seen_at=now
    )
    store.mark_sent([candidate], run_id="run1", message_id="om_1", sent_at=now)

    store.sync_candidates(
        [], selected_rules=["R4"], observed_order_nos=["C999"], seen_at=now + timedelta(hours=1)
    )
    assert not store.should_send(candidate, now=now + timedelta(hours=2), repeat_hour=9)

    store.sync_candidates(
        [],
        selected_rules=["R4"],
        observed_order_nos=["C001"],
        seen_at=now + timedelta(hours=3),
    )
    store.sync_candidates(
        [candidate],
        selected_rules=["R4"],
        observed_order_nos=["C001"],
        seen_at=now + timedelta(hours=4),
    )
    assert store.should_send(candidate, now=now + timedelta(hours=4), repeat_hour=9)
