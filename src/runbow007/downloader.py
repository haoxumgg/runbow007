from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .credentials import CredentialError, get_tms_password

logger = logging.getLogger(__name__)


class TmsDownloadError(RuntimeError):
    """Raised when browser automation cannot produce a download."""


class TmsAuthenticationError(TmsDownloadError):
    """Raised for credential problems that should not be retried."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    ui_total: int | None
    dataset: str


class TmsDownloader:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def download(self, dataset: str = "current_month") -> DownloadResult:
        last_error: Exception | None = None
        export_not_before = self._local_now()
        for attempt, delay in enumerate((0, 60, 180), start=1):
            if delay:
                logger.warning("第 %s 次下载失败，%s 秒后重试", attempt - 1, delay)
                time.sleep(delay)
            try:
                return self._download_once(dataset, export_not_before=export_not_before)
            except (CredentialError, TmsAuthenticationError):
                raise
            except Exception as exc:  # browser errors are normalized at the boundary
                last_error = exc
                logger.exception("TMS 下载第 %s 次失败", attempt)
        raise TmsDownloadError(f"TMS 下载连续三次失败: {last_error}") from last_error

    def _download_once(
        self, dataset: str, *, export_not_before: datetime | None = None
    ) -> DownloadResult:
        try:
            from playwright.sync_api import Page, sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise TmsDownloadError("请先安装 Playwright") from exc

        self.config.ensure_directories()
        password = get_tms_password(self.config.tms.username)
        run_dir = self.config.runtime.downloads_dir / self._local_now().strftime("%Y%m%d")
        run_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            logger.info("启动 TMS 浏览器")
            context = playwright.chromium.launch_persistent_context(
                str(self.config.runtime.browser_profile_dir),
                headless=self.config.tms.headless,
                accept_downloads=True,
            )
            page: Page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.config.tms.navigation_timeout_seconds * 1000)
            page.goto(self.config.tms.url, wait_until="domcontentloaded")
            logger.info("TMS 首页已加载，检查登录状态")
            self._login_if_needed(page, password)
            logger.info("TMS 登录状态确认完成，打开集团订单管理")
            self._open_order_page(page)
            logger.info("集团订单管理已打开，应用数据筛选")
            self._apply_filters(page, dataset)
            ui_total = self._read_total(page)
            logger.info("TMS 筛选完成，页面订单总数=%s", ui_total)

            button = self._locator_or_button(
                page, self.config.tms.selectors.download_button, r"下载|批量导出"
            )
            export_started = export_not_before or self._local_now()
            button.click()
            logger.info("已点击导出，等待确认窗口")
            self._confirm_export(page)
            logger.info("导出任务已创建，进入下载中心核验")
            download = self._download_from_center(page, export_started, ui_total)
            suggested = Path(download.suggested_filename)
            suffix = suggested.suffix.lower() if suggested.suffix else ".xls"
            target = run_dir / f"{dataset}-{uuid.uuid4().hex[:12]}{suffix}"
            download.save_as(target)
            logger.info("TMS Excel 已保存: %s", target.name)
            context.close()

        if not target.exists() or target.stat().st_size == 0:
            raise TmsDownloadError("浏览器报告下载完成，但文件为空")
        return DownloadResult(target, ui_total, dataset)

    def _login_if_needed(self, page: object, password: str) -> None:
        selectors = self.config.tms.selectors
        username = page.locator(selectors.username).first
        # TMS 是 SPA，DOMContentLoaded 后登录表单或首页菜单仍会延迟挂载。
        visible = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if username.is_visible():
                    visible = True
                    break
                home = page.get_by_text("下载中心", exact=True)
                if any(home.nth(index).is_visible() for index in range(home.count())):
                    return
            except Exception:
                pass
            page.wait_for_timeout(250)
        if not visible:
            return
        username.fill(self.config.tms.username)
        page.locator(selectors.password).first.fill(password)
        button = self._locator_or_button(page, selectors.login_button, r"登录|登 录")
        button.click()
        try:
            page.wait_for_function(
                "() => !location.hash.includes('/login')", timeout=15_000
            )
        except Exception:
            pass
        try:
            still_visible = username.is_visible(timeout=5_000)
        except Exception:
            still_visible = False
        if still_visible:
            raise TmsAuthenticationError("TMS 登录后仍停留在登录页，请检查账号或密码")

    @classmethod
    def _open_order_page(cls, page: object) -> None:
        advanced = page.locator("#searchItem").first
        if advanced.count() and advanced.is_visible():
            return
        cls._click_visible_text(page, "订单管理")
        page.wait_for_timeout(500)
        cls._click_visible_text(page, "集团订单管理")
        advanced.wait_for(state="visible")

    def _apply_filters(self, page: object, dataset: str) -> None:
        selectors = self.config.tms.selectors
        advanced = self._locator_or_button(page, selectors.advanced_filter_button, r"高级筛选")
        advanced.click()
        preset = (
            self.config.tms.current_month_preset
            if dataset == "current_month"
            else self.config.tms.open_carryover_preset
        )
        if preset:
            preset_trigger = selectors.preset_name or ".el-dialog:visible .page-header-title"
            page.locator(preset_trigger).first.click()
            self._click_visible_text(page, preset)
        if dataset == "current_month" and selectors.date_from_input:
            month_start = self._local_now().replace(day=1).strftime("%Y-%m-%d 00:00")
            page.locator(selectors.date_from_input).first.fill(month_start)
        query = self._locator_or_button(page, selectors.query_button, r"查询")
        query.click()
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            logger.info("TMS 页面持续有后台请求，跳过 networkidle 等待")
        page.wait_for_timeout(2_000)

    def _read_total(self, page: object) -> int | None:
        selector = self.config.tms.selectors.total_count
        if not selector:
            return None
        text = page.locator(selector).first.inner_text()
        match = re.search(r"([\d,]+)", text)
        return int(match.group(1).replace(",", "")) if match else None

    def _local_now(self) -> datetime:
        # TMS 下载中心显示的是配置时区的无时区时间，统一成同口径后再比较。
        return datetime.now(ZoneInfo(self.config.runtime.timezone)).replace(tzinfo=None)

    def _confirm_export(self, page: object) -> None:
        confirms = page.get_by_role("button", name="确定")
        deadline = time.monotonic() + 15
        clicked = False
        while time.monotonic() < deadline:
            for index in range(confirms.count()):
                confirm = confirms.nth(index)
                if confirm.is_visible(timeout=500):
                    confirm.click()
                    clicked = True
                    break
            if clicked:
                break
            page.wait_for_timeout(250)
        if not clicked:
            raise TmsDownloadError("点击导出后未出现确认窗口")

        success = page.get_by_text("下载任务添加成功", exact=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(
                success.nth(index).is_visible(timeout=500)
                for index in range(success.count())
            ):
                return
            page.wait_for_timeout(250)
        # 成功提示是短暂 toast，页面响应慢时可能在定位前已消失。下载中心的
        # 最新任务时间、状态和条数才是最终确认依据，因此这里继续轮询下载中心。
        logger.info("未捕获到导出成功提示，继续在下载中心核验任务")

    def _download_from_center(
        self, page: object, export_started: datetime, expected_total: int | None
    ) -> object:
        self._click_visible_text(page, "下载中心", force=True)
        page.wait_for_timeout(2_000)
        deadline = time.monotonic() + self.config.tms.download_timeout_seconds
        earliest = export_started - timedelta(minutes=1)
        last_snapshot: tuple[object, ...] | None = None

        while time.monotonic() < deadline:
            # Element UI 会为固定列复制 tbody；只遍历包含任务名的完整任务行。
            rows = page.locator("tr").filter(has_text="maintainCompanyOrderPage")
            for index in range(min(rows.count(), 20)):
                row = rows.nth(index)
                text = row.inner_text()
                if "maintainCompanyOrderPage" not in text:
                    continue
                started_at, record_count = self._parse_download_row(text)
                if index == 0:
                    snapshot = (started_at, record_count, "成功" in text, expected_total)
                    if snapshot != last_snapshot:
                        logger.info(
                            "下载中心最新任务: started_at=%s records=%s success=%s "
                            "expected=%s earliest=%s",
                            started_at,
                            record_count,
                            "成功" in text,
                            expected_total,
                            earliest,
                        )
                        last_snapshot = snapshot
                if started_at is None or started_at < earliest:
                    continue
                if "失败" in text:
                    raise TmsDownloadError(f"TMS 下载中心任务失败: {text[:200]}")
                if "成功" not in text:
                    continue
                if expected_total is not None and record_count != expected_total:
                    continue
                link = row.locator("a:has(img[src*='excel'])").first
                if not link.count() or not link.is_visible():
                    continue
                remaining = max(1, int(deadline - time.monotonic())) * 1_000
                with page.expect_download(timeout=remaining) as info:
                    link.click()
                return info.value

            refresh = page.locator("#refreshItem").first
            if refresh.count() and refresh.is_visible():
                refresh.click(force=True)
            page.wait_for_timeout(2_000)

        raise TmsDownloadError("TMS 下载中心任务在超时时间内未完成")

    @staticmethod
    def _parse_download_row(text: str) -> tuple[datetime | None, int | None]:
        timestamps = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", text)
        started_at = (
            datetime.strptime(timestamps[0], "%Y-%m-%d %H:%M") if timestamps else None
        )
        tokens = [item.strip() for item in re.split(r"[\r\n\t]+", text) if item.strip()]
        numeric = [int(item) for item in tokens if item.isdigit()]
        record_count = numeric[-2] if len(numeric) >= 2 else None
        return started_at, record_count

    @staticmethod
    def _locator_or_button(page: object, selector: str, name_pattern: str) -> object:
        if selector:
            return page.locator(selector).first
        return page.get_by_role("button", name=re.compile(name_pattern)).last

    @staticmethod
    def _click_visible_text(page: object, text: str, *, force: bool = False) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            matches = page.get_by_text(text, exact=True)
            for index in range(matches.count()):
                candidate = matches.nth(index)
                if candidate.is_visible():
                    if force:
                        # TMS 的成功提示层可能长期覆盖菜单。这里直接触发已确认
                        # 可见菜单元素的 DOM click，避免覆盖层截获鼠标事件。
                        candidate.evaluate("element => element.click()")
                    else:
                        candidate.click()
                    return
            page.wait_for_timeout(250)
        raise TmsDownloadError(f"页面未找到可见元素: {text}")
