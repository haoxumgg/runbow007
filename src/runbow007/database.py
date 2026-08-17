from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from .models import Order, ReminderCandidate

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    row_count INTEGER,
    candidate_count INTEGER,
    sent_count INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_no TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    source_file TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_events (
    event_key TEXT PRIMARY KEY,
    rule_code TEXT NOT NULL,
    order_no TEXT NOT NULL,
    scenario TEXT NOT NULL,
    state TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_sent_at TEXT,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_reminder_events_rule_state
ON reminder_events(rule_code, state);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_key) REFERENCES reminder_events(event_key),
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
"""


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def begin_run(
        self, run_id: str, source_file: Path, file_sha256: str, started_at: datetime
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(run_id, source_file, file_sha256, started_at, status)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (run_id, str(source_file), file_sha256, started_at.isoformat()),
            )

    def upsert_orders(
        self, orders: Iterable[Order], *, source_file: Path, seen_at: datetime
    ) -> None:
        rows = [
            (
                order.order_no,
                json.dumps(order.to_json_dict(), ensure_ascii=False, separators=(",", ":")),
                str(source_file),
                seen_at.isoformat(),
            )
            for order in orders
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO orders(order_no, payload_json, source_file, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(order_no) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    source_file = excluded.source_file,
                    last_seen_at = excluded.last_seen_at
                """,
                rows,
            )

    def sync_candidates(
        self,
        candidates: Iterable[ReminderCandidate],
        *,
        selected_rules: Iterable[str],
        observed_order_nos: Iterable[str],
        seen_at: datetime,
        reopen_grace_hours: int = 0,
    ) -> None:
        items = list(candidates)
        selected = {code.upper() for code in selected_rules}
        observed = sorted(set(observed_order_nos))
        timestamp = seen_at.isoformat()
        # 命中集合每小时都在抖动（同一订单可能这轮消失、下轮又出现）。只有真正消停
        # 了 reopen_grace_hours 之后再复发，才算新问题、才清空发送记录；否则保留
        # last_sent_at，让每日重复提醒的规则继续生效，避免抖一次就重复推送一次。
        reopen_cutoff = (seen_at - timedelta(hours=reopen_grace_hours)).isoformat()
        with self.connect() as connection:
            for candidate in items:
                connection.execute(
                    """
                    INSERT INTO reminder_events(
                        event_key, rule_code, order_no, scenario, state,
                        first_seen_at, last_seen_at, resolved_at
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, NULL)
                    ON CONFLICT(event_key) DO UPDATE SET
                        state = 'open',
                        last_seen_at = excluded.last_seen_at,
                        last_sent_at = CASE
                            WHEN reminder_events.state = 'resolved'
                                 AND reminder_events.resolved_at IS NOT NULL
                                 AND reminder_events.resolved_at <= ?
                            THEN NULL
                            ELSE reminder_events.last_sent_at
                        END,
                        resolved_at = NULL
                    """,
                    (
                        candidate.event_key,
                        candidate.rule_code,
                        candidate.order.order_no,
                        candidate.scenario,
                        timestamp,
                        timestamp,
                        reopen_cutoff,
                    ),
                )

            if not observed:
                return
            observed_placeholders = ",".join("?" for _ in observed)
            for rule_code in selected:
                active = [item.event_key for item in items if item.rule_code == rule_code]
                if active:
                    placeholders = ",".join("?" for _ in active)
                    connection.execute(
                        f"""
                        UPDATE reminder_events
                        SET state = 'resolved', resolved_at = ?
                        WHERE rule_code = ? AND state = 'open'
                          AND order_no IN ({observed_placeholders})
                          AND event_key NOT IN ({placeholders})
                        """,
                        (timestamp, rule_code, *observed, *active),
                    )
                else:
                    connection.execute(
                        f"""
                        UPDATE reminder_events
                        SET state = 'resolved', resolved_at = ?
                        WHERE rule_code = ? AND state = 'open'
                          AND order_no IN ({observed_placeholders})
                        """,
                        (timestamp, rule_code, *observed),
                    )

    def should_send(
        self, candidate: ReminderCandidate, *, now: datetime, repeat_hour: int
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT last_sent_at FROM reminder_events WHERE event_key = ?",
                (candidate.event_key,),
            ).fetchone()
        if row is None or not row["last_sent_at"]:
            return True
        if candidate.rule_code not in {"R3", "R4"}:
            return False
        last_sent = datetime.fromisoformat(row["last_sent_at"])
        return last_sent.date() < now.date() and now.hour >= repeat_hour

    def mark_sent(
        self,
        candidates: Iterable[ReminderCandidate],
        *,
        run_id: str,
        message_id: str,
        sent_at: datetime,
    ) -> None:
        items = list(candidates)
        with self.connect() as connection:
            for candidate in items:
                connection.execute(
                    "UPDATE reminder_events SET last_sent_at = ? WHERE event_key = ?",
                    (sent_at.isoformat(), candidate.event_key),
                )
                connection.execute(
                    """
                    INSERT INTO deliveries(
                        event_key, run_id, status, message_id, created_at
                    ) VALUES (?, ?, 'sent', ?, ?)
                    """,
                    (candidate.event_key, run_id, message_id, sent_at.isoformat()),
                )

    def mark_failed(
        self,
        candidates: Iterable[ReminderCandidate],
        *,
        run_id: str,
        error: str,
        failed_at: datetime,
    ) -> None:
        with self.connect() as connection:
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO deliveries(event_key, run_id, status, error, created_at)
                    VALUES (?, ?, 'failed', ?, ?)
                    """,
                    (candidate.event_key, run_id, error, failed_at.isoformat()),
                )

    def latest_successful_row_count(self) -> int | None:
        """Row count of the most recent successful run, for anomaly detection."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT row_count FROM runs
                WHERE status = 'success' AND row_count IS NOT NULL AND row_count > 0
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
        return int(row["row_count"]) if row else None

    def complete_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        row_count: int | None = None,
        candidate_count: int | None = None,
        sent_count: int | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE runs SET
                    finished_at = ?, status = ?, row_count = ?,
                    candidate_count = ?, sent_count = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    finished_at.isoformat(),
                    status,
                    row_count,
                    candidate_count,
                    sent_count,
                    error,
                    run_id,
                ),
            )
