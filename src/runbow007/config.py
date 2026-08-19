from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(slots=True)
class RuntimeConfig:
    timezone: str = "Asia/Shanghai"
    data_dir: Path = Path("data")
    downloads_dir: Path = Path("downloads")
    logs_dir: Path = Path("logs")
    browser_profile_dir: Path = Path("browser-profile")
    database_path: Path = Path("data/runbow007.db")
    lock_path: Path = Path("data/runbow007.lock")
    retain_days: int = 30


@dataclass(slots=True)
class TmsSelectors:
    """CSS selectors for the TMS SPA.

    TMS 是标签页式 SPA：打开过的页面全部留在 DOM 里，非活动的只是隐藏。所以凡是
    会重复渲染的控件（工具栏、分页、菜单项），选择器里都必须带 :visible——由选择器
    引擎一次过滤掉隐藏副本，比在 Python 里逐个探测可见性简单也快得多。
    """

    username: str = (
        "input[name='username'], input[placeholder*='账号'], "
        "input[placeholder*='用户名']"
    )
    password: str = "input[type='password']"
    login_button: str = "button.submit-btn"
    order_menu: str = '.el-submenu__title:has-text("订单管理"):visible'
    group_order_menu: str = 'li.el-menu-item:has-text("集团订单管理"):visible'
    advanced_filter_button: str = "#searchItem:visible"
    # 只认对话框里的那一个。加不限定对话框的兜底分支会引入歧义：locator("A, B").first
    # 取的是 DOM 顺序而不是分支顺序，从下载中心切回来时会选中错误的元素。
    preset_name: str = ".el-dialog:visible .page-header-title"
    preset_option: str = ".search-list .search-item:visible"
    query_button: str = ".el-dialog:visible button.el-button--primary"
    total_count: str = "button.pagination-total:visible"
    download_button: str = (
        "button.round-btn:has(.thorn6-icon-daoru):visible, "
        "button:has(.thorn6-icon-daochu):visible, "
        "button:has(.thorn6-icon-daoru):visible"
    )
    confirm_button: str = 'button:has-text("确定"):visible, button:has-text("确 定"):visible'
    download_center_menu: str = "ul.right_menu li.menu-item:has(.thorn6-icon-xiazai)"
    # 下载中心里的导出文件链接。href 上的 id 自增，是判断"这个任务是不是本轮新建的"
    # 唯一可靠依据。注意 Dowload 是 TMS 自己的拼写。
    export_link: str = "a[href*='exportFileDowload']"
    # 下载中心的任务行。链接和功能列、开始时间必须取自同一个 tr。
    download_row: str = "table.el-table__body tr.el-table__row"
    refresh_button: str = "#refreshItem:visible"


@dataclass(slots=True)
class TmsConfig:
    url: str = "https://otb.lining.com/#/"
    username: str = ""
    headless: bool = True
    persistent_profile: bool = False
    navigation_timeout_seconds: int = 45
    download_timeout_seconds: int = 600
    grid_load_timeout_seconds: int = 180
    attempt_timeout_seconds: int = 900
    total_tolerance: int = 10
    # 下载中心是全公司共享队列，别人的导出会插在我们前后。认领本轮任务除了"id 比
    # 基线新"，还要求功能列是集团订单管理的导出、且开始时间落在点"确定"的前后容差内。
    export_task_name: str = "maintainCompanyOrderPage"
    export_match_window_minutes: int = 5
    current_month_preset: str = "AI导出数据（勿动）"
    open_carryover_preset: str = ""
    selectors: TmsSelectors = field(default_factory=TmsSelectors)


@dataclass(slots=True)
class FeishuConfig:
    app_id: str = ""
    chat_id: str = "oc_f79000009c4f09cbdf78b55fd35ae04a"
    mention_user_id: str = ""
    mention_name: str = "许昊"
    request_timeout_seconds: int = 20


@dataclass(slots=True)
class RulesConfig:
    enabled: tuple[str, ...] = ("R1", "R2", "R3", "R4")
    wms_lead_minutes: int = 90
    unresolved_repeat_hour: int = 9
    reopen_grace_hours: int = 12
    min_row_ratio: float = 0.5
    max_row_ratio: float = 1.5


@dataclass(slots=True)
class AppConfig:
    source_path: Path
    runtime: RuntimeConfig
    tms: TmsConfig
    feishu: FeishuConfig
    rules: RulesConfig

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        source_path = Path(path).resolve()
        if not source_path.exists():
            raise ConfigError(f"配置文件不存在: {source_path}")
        raw = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError("配置文件顶层必须是 YAML 对象")

        base = source_path.parent
        runtime_raw = raw.get("runtime", {})
        tms_raw = raw.get("tms", {})
        feishu_raw = raw.get("feishu", {})
        rules_raw = raw.get("rules", {})
        selectors_raw = tms_raw.get("selectors", {})

        runtime = RuntimeConfig(
            timezone=str(runtime_raw.get("timezone", "Asia/Shanghai")),
            data_dir=_path(base, runtime_raw.get("data_dir", "data")),
            downloads_dir=_path(base, runtime_raw.get("downloads_dir", "downloads")),
            logs_dir=_path(base, runtime_raw.get("logs_dir", "logs")),
            browser_profile_dir=_path(
                base, runtime_raw.get("browser_profile_dir", "browser-profile")
            ),
            database_path=_path(base, runtime_raw.get("database_path", "data/runbow007.db")),
            lock_path=_path(base, runtime_raw.get("lock_path", "data/runbow007.lock")),
            retain_days=int(runtime_raw.get("retain_days", 30)),
        )
        selectors = TmsSelectors(**_known_values(TmsSelectors, selectors_raw))
        tms_values = _known_values(TmsConfig, tms_raw, exclude={"selectors"})
        tms = TmsConfig(**tms_values, selectors=selectors)
        feishu = FeishuConfig(**_known_values(FeishuConfig, feishu_raw))
        tms.username = _environment_value("RUNBOW007_TMS_USERNAME", tms.username)
        tms.download_timeout_seconds = _environment_int(
            "RUNBOW007_TMS_DOWNLOAD_TIMEOUT_SECONDS",
            tms.download_timeout_seconds,
        )
        feishu.app_id = _environment_value("RUNBOW007_FEISHU_APP_ID", feishu.app_id)
        feishu.chat_id = _environment_value("RUNBOW007_FEISHU_CHAT_ID", feishu.chat_id)
        enabled = tuple(
            str(item).upper() for item in rules_raw.get("enabled", RulesConfig().enabled)
        )
        rules_values = _known_values(RulesConfig, rules_raw, exclude={"enabled"})
        rules = RulesConfig(**rules_values, enabled=enabled)

        config = cls(source_path, runtime, tms, feishu, rules)
        config.validate()
        return config

    def validate(self, *, sending: bool = False) -> None:
        unknown_rules = set(self.rules.enabled) - {"R1", "R2", "R3", "R4"}
        if unknown_rules:
            raise ConfigError(f"未知规则: {', '.join(sorted(unknown_rules))}")
        if not 1 <= self.rules.wms_lead_minutes <= 24 * 60:
            raise ConfigError("rules.wms_lead_minutes 必须在 1 到 1440 之间")
        if not 0 <= self.rules.unresolved_repeat_hour <= 23:
            raise ConfigError("rules.unresolved_repeat_hour 必须在 0 到 23 之间")
        if not 0 <= self.rules.reopen_grace_hours <= 24 * 30:
            raise ConfigError("rules.reopen_grace_hours 必须在 0 到 720 之间")
        if not 0 <= self.rules.min_row_ratio < 1:
            raise ConfigError("rules.min_row_ratio 必须在 0 到 1 之间（0 表示关闭）")
        if self.rules.max_row_ratio and self.rules.max_row_ratio <= 1:
            raise ConfigError("rules.max_row_ratio 必须大于 1（0 表示关闭）")
        if not 1 <= self.runtime.retain_days <= 3650:
            raise ConfigError("runtime.retain_days 必须在 1 到 3650 之间")
        if not self.tms.url.startswith("https://"):
            raise ConfigError("tms.url 必须使用 HTTPS")
        if not 60 <= self.tms.download_timeout_seconds <= 1800:
            raise ConfigError("tms.download_timeout_seconds 必须在 60 到 1800 之间")
        if not 0 <= self.tms.total_tolerance <= 1000:
            raise ConfigError("tms.total_tolerance 必须在 0 到 1000 之间")
        if not 1 <= self.tms.export_match_window_minutes <= 60:
            raise ConfigError("tms.export_match_window_minutes 必须在 1 到 60 之间")
        if not 10 <= self.tms.grid_load_timeout_seconds <= 900:
            raise ConfigError("tms.grid_load_timeout_seconds 必须在 10 到 900 之间")
        if self.tms.attempt_timeout_seconds and not (
            60 <= self.tms.attempt_timeout_seconds <= 3600
        ):
            raise ConfigError(
                "tms.attempt_timeout_seconds 必须在 60 到 3600 之间（0 表示关闭）"
            )
        if sending:
            missing = [
                name
                for name, value in (
                    ("feishu.app_id", self.feishu.app_id),
                    ("feishu.chat_id", self.feishu.chat_id),
                )
                if not value
            ]
            if missing:
                raise ConfigError("真实发送前必须配置: " + ", ".join(missing))

    def ensure_directories(self) -> None:
        for path in (
            self.runtime.data_dir,
            self.runtime.downloads_dir,
            self.runtime.logs_dir,
            self.runtime.browser_profile_dir,
            self.runtime.database_path.parent,
            self.runtime.lock_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _environment_value(name: str, fallback: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else fallback


def _environment_int(name: str, fallback: int) -> int:
    value = os.getenv(name)
    return int(value.strip()) if value and value.strip() else fallback


def _known_values(
    data_class: type[Any], values: dict[str, Any], *, exclude: set[str] | None = None
) -> dict[str, Any]:
    exclude = exclude or set()
    allowed = set(data_class.__dataclass_fields__) - exclude
    return {key: value for key, value in values.items() if key in allowed}

