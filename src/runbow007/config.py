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
    username: str = (
        "input[name='username'], input[placeholder*='账号'], "
        "input[placeholder*='用户名']"
    )
    password: str = "input[type='password']"
    login_button: str = ".submit-btn"
    advanced_filter_button: str = "#searchItem"
    preset_name: str = ".el-dialog:visible .page-header-title"
    date_from_input: str = ".el-dialog:visible .el-date-editor input"
    query_button: str = ".el-dialog:visible button.el-button--primary"
    total_count: str = "button.pagination-total"
    download_button: str = (
        "button:has(.thorn6-icon-daochu), button:has(.thorn6-icon-daoru)"
    )
    download_center_menu: str = "ul.right_menu li.menu-item:has(.thorn6-icon-xiazai)"


@dataclass(slots=True)
class TmsConfig:
    url: str = "https://otb.lining.com/#/"
    username: str = ""
    headless: bool = True
    persistent_profile: bool = False
    navigation_timeout_seconds: int = 45
    download_timeout_seconds: int = 600
    grid_load_timeout_seconds: int = 180
    export_task_appear_minutes: int = 8
    total_tolerance: int = 10
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
        if not 1 <= self.tms.export_task_appear_minutes <= 30:
            raise ConfigError("tms.export_task_appear_minutes 必须在 1 到 30 之间")
        if not 10 <= self.tms.grid_load_timeout_seconds <= 900:
            raise ConfigError("tms.grid_load_timeout_seconds 必须在 10 到 900 之间")
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

