from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import portalocker

from .config import AppConfig
from .credentials import set_feishu_secret, set_tms_password
from .downloader import TmsDownloader
from .pipeline import Pipeline
from .retention import cleanup_old_downloads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="007 李宁 TMS 提醒自动化")
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check-config", help="检查配置并初始化本地目录/数据库")

    process = subparsers.add_parser("process-file", help="处理已下载的 Excel")
    process.add_argument("file", help=".xls/.xlsx 文件")
    process.add_argument("--rules", default="R1,R2,R3,R4", help="逗号分隔的规则")
    process.add_argument("--ui-total", type=int, help="TMS 页面显示的总数")
    process.add_argument("--send", action="store_true", help="真实发送飞书")
    process.add_argument(
        "--force-send",
        action="store_true",
        help="人工验收：忽略历史发送去重记录并重新发送当前命中项",
    )
    process.add_argument(
        "--max-send-orders",
        type=_limited_send_count,
        help="仅真实发送时生效；固定限制为最多 1–5 个唯一订单",
    )

    run = subparsers.add_parser("run", help="自动登录 TMS、下载并处理")
    run.add_argument(
        "--dataset", choices=("current_month", "open_carryover"), default="current_month"
    )
    run.add_argument("--rules", default="R1,R3,R4", help="逗号分隔的规则")
    run.add_argument("--send", action="store_true", help="真实发送飞书")
    run.add_argument(
        "--max-send-orders",
        type=_limited_send_count,
        help="仅真实发送时生效；固定限制为最多 1–5 个唯一订单",
    )

    credentials = subparsers.add_parser("credentials", help="保存凭据到系统凭据库")
    credentials_sub = credentials.add_subparsers(dest="credential_command", required=True)
    credentials_sub.add_parser("set-tms", help="保存 TMS 密码")
    credentials_sub.add_parser("set-feishu", help="保存飞书 App Secret")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = AppConfig.load(args.config)
        config.ensure_directories()
        _configure_logging(config.runtime.logs_dir, config.runtime.retain_days)

        if args.command == "credentials":
            return _credentials(args, config)
        if args.command == "check-config":
            Pipeline(config)
            print(f"配置有效；数据库: {config.runtime.database_path}")
            return 0

        with portalocker.Lock(config.runtime.lock_path, timeout=1):
            pipeline = Pipeline(config)
            if args.command == "process-file":
                result = pipeline.process_file(
                    args.file,
                    rule_codes=_rules(args.rules),
                    expected_ui_total=args.ui_total,
                    send=args.send,
                    max_send_orders=args.max_send_orders,
                    force_send=args.force_send,
                )
            else:
                download = TmsDownloader(config).download(args.dataset)
                result = pipeline.process_file(
                    download.path,
                    rule_codes=_rules(args.rules),
                    expected_ui_total=download.ui_total,
                    send=args.send,
                    max_send_orders=args.max_send_orders,
                )
            _cleanup_downloads(config)
        print(
            f"运行完成: rows={result.row_count}, candidates={result.candidate_count}, "
            f"sent={result.sent_count}, run_id={result.run_id}"
        )
        return 0
    except portalocker.AlreadyLocked:
        print("已有任务运行，本次跳过", file=sys.stderr)
        return 3
    except Exception as exc:
        logging.getLogger(__name__).exception("运行失败")
        print(f"运行失败: {exc}", file=sys.stderr)
        return 2


def _credentials(args: argparse.Namespace, config: AppConfig) -> int:
    if args.credential_command == "set-tms":
        if not config.tms.username:
            raise ValueError("请先在 config.yaml 设置 tms.username")
        password = getpass.getpass("李宁 TMS 密码: ")
        set_tms_password(config.tms.username, password)
        print("TMS 密码已保存到系统凭据库")
        return 0
    if not config.feishu.app_id:
        raise ValueError("请先在 config.yaml 设置 feishu.app_id")
    secret = getpass.getpass("飞书 App Secret: ")
    set_feishu_secret(config.feishu.app_id, secret)
    print("飞书 App Secret 已保存到系统凭据库")
    return 0


def _rules(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


def _limited_send_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 5:
        raise argparse.ArgumentTypeError("小批量发送只能限制为 1–5 个唯一订单")
    return count


def _configure_logging(log_dir: Path, retain_days: int) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        log_dir / "runbow007.log",
        when="midnight",
        backupCount=retain_days,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            file_handler,
        ],
    )


def _cleanup_downloads(config: AppConfig) -> None:
    try:
        removed = cleanup_old_downloads(
            config.runtime.downloads_dir,
            retain_days=config.runtime.retain_days,
            now=datetime.now(ZoneInfo(config.runtime.timezone)),
        )
    except OSError:
        logging.getLogger(__name__).warning("清理过期下载文件失败", exc_info=True)
        return
    if removed:
        logging.getLogger(__name__).info("已清理 %s 个过期下载文件", removed)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
