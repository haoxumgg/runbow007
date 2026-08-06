from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .credentials import get_feishu_secret
from .database import SQLiteStore
from .excel import read_orders
from .models import ReminderCandidate, RunResult
from .notifier import FeishuClient, MessageFormatter
from .rules import RuleEngine

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.config.ensure_directories()
        self.store = SQLiteStore(config.runtime.database_path)
        self.rules = RuleEngine(config.rules)

    def process_file(
        self,
        source_file: str | Path,
        *,
        rule_codes: Iterable[str] | None = None,
        expected_ui_total: int | None = None,
        send: bool | None = None,
    ) -> RunResult:
        now = datetime.now(ZoneInfo(self.config.runtime.timezone))
        requested = tuple(code.upper() for code in (rule_codes or self.config.rules.enabled))
        unknown = set(requested) - {"R1", "R2", "R3", "R4"}
        if unknown:
            raise ValueError("未知规则: " + ", ".join(sorted(unknown)))
        selected = tuple(code for code in requested if code in self.config.rules.enabled)
        if not selected:
            raise ValueError("请求的规则均未启用")
        should_send = (not self.config.runtime.dry_run) if send is None else send
        run_id = uuid.uuid4().hex
        archived = self._archive_source(Path(source_file), run_id)
        digest = _sha256(archived)
        self.store.begin_run(run_id, archived, digest, now)

        try:
            parsed = read_orders(archived, expected_ui_total=expected_ui_total)
            self.store.upsert_orders(parsed.orders, source_file=archived, seen_at=now)
            candidates = self.rules.evaluate(parsed.orders, now=now, rule_codes=selected)
            self.store.sync_candidates(
                candidates,
                selected_rules=selected,
                observed_order_nos=(order.order_no for order in parsed.orders),
                seen_at=now,
            )
            sendable = [
                item
                for item in candidates
                if self.store.should_send(
                    item,
                    now=now,
                    repeat_hour=self.config.rules.unresolved_repeat_hour,
                )
            ]

            sent_count = 0
            if should_send and sendable:
                self.config.validate(sending=True)
                sent_count = self._send_groups(sendable, run_id, now)
            else:
                self._log_preview(sendable)

            self.store.complete_run(
                run_id,
                finished_at=datetime.now(ZoneInfo(self.config.runtime.timezone)),
                status="success",
                row_count=parsed.row_count,
                candidate_count=len(candidates),
                sent_count=sent_count,
            )
            return RunResult(
                run_id,
                archived,
                parsed.row_count,
                len(candidates),
                sent_count,
                not should_send,
            )
        except Exception as exc:
            self.store.complete_run(
                run_id,
                finished_at=datetime.now(ZoneInfo(self.config.runtime.timezone)),
                status="failed",
                error=str(exc),
            )
            raise

    def _send_groups(
        self, candidates: list[ReminderCandidate], run_id: str, now: datetime
    ) -> int:
        app_secret = get_feishu_secret(self.config.feishu.app_id)
        client = FeishuClient(self.config.feishu, app_secret=app_secret)
        formatter = MessageFormatter(
            mention_user_id=self.config.feishu.mention_user_id,
            mention_name=self.config.feishu.mention_name,
        )
        groups: dict[str, list[ReminderCandidate]] = defaultdict(list)
        for candidate in candidates:
            groups[candidate.rule_code].append(candidate)

        sent_count = 0
        for rule_code in sorted(groups):
            group = groups[rule_code]
            batch_size = self.config.feishu.max_orders_per_message
            for start in range(0, len(group), batch_size):
                batch = group[start : start + batch_size]
                try:
                    message_id = client.send(formatter.format(rule_code, batch))
                    self.store.mark_sent(
                        batch, run_id=run_id, message_id=message_id, sent_at=now
                    )
                    sent_count += len(batch)
                except Exception as exc:
                    self.store.mark_failed(batch, run_id=run_id, error=str(exc), failed_at=now)
                    raise
        return sent_count

    def _archive_source(self, source: Path, run_id: str) -> Path:
        source = source.resolve()
        if not source.exists():
            return source
        target_dir = self.config.runtime.downloads_dir / datetime.now().strftime("%Y%m%d")
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            source.relative_to(self.config.runtime.downloads_dir)
            return source
        except ValueError:
            target = target_dir / f"manual-{run_id[:12]}{source.suffix.lower()}"
            shutil.copy2(source, target)
            return target

    @staticmethod
    def _log_preview(candidates: list[ReminderCandidate]) -> None:
        counts: dict[str, int] = defaultdict(int)
        for candidate in candidates:
            counts[candidate.rule_code] += 1
        if counts:
            logger.info(
                "演练模式，未发送飞书。待发送: %s",
                ", ".join(f"{code}={count}" for code, count in sorted(counts.items())),
            )
        else:
            logger.info("没有新的待发送提醒")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
