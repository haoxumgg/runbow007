from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
        for attempt, delay in enumerate((0, 60, 180), start=1):
            if delay:
                logger.warning("第 %s 次下载失败，%s 秒后重试", attempt - 1, delay)
                time.sleep(delay)
            try:
                return self._download_once(dataset)
            except (CredentialError, TmsAuthenticationError):
                raise
            except Exception as exc:  # browser errors are normalized at the boundary
                last_error = exc
                logger.exception("TMS 下载第 %s 次失败", attempt)
        raise TmsDownloadError(f"TMS 下载连续三次失败: {last_error}") from last_error

    def _download_once(self, dataset: str) -> DownloadResult:
        try:
            from playwright.sync_api import Page, sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise TmsDownloadError("请先安装 Playwright") from exc

        self.config.ensure_directories()
        password = get_tms_password(self.config.tms.username)
        run_dir = self.config.runtime.downloads_dir / datetime.now().strftime("%Y%m%d")
        run_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.config.runtime.browser_profile_dir),
                headless=self.config.tms.headless,
                accept_downloads=True,
            )
            page: Page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.config.tms.navigation_timeout_seconds * 1000)
            page.goto(self.config.tms.url, wait_until="domcontentloaded")
            self._login_if_needed(page, password)
            self._open_order_page(page)
            self._apply_filters(page, dataset)
            ui_total = self._read_total(page)

            button = self._locator_or_button(
                page, self.config.tms.selectors.download_button, r"下载|批量导出"
            )
            download_timeout = self.config.tms.download_timeout_seconds * 1000
            with page.expect_download(timeout=download_timeout) as info:
                button.click()
            download = info.value
            suggested = Path(download.suggested_filename)
            suffix = suggested.suffix.lower() if suggested.suffix else ".xls"
            target = run_dir / f"{dataset}-{uuid.uuid4().hex[:12]}{suffix}"
            download.save_as(target)
            context.close()

        if not target.exists() or target.stat().st_size == 0:
            raise TmsDownloadError("浏览器报告下载完成，但文件为空")
        return DownloadResult(target, ui_total, dataset)

    def _login_if_needed(self, page: object, password: str) -> None:
        selectors = self.config.tms.selectors
        username = page.locator(selectors.username).first
        try:
            visible = username.is_visible(timeout=3_000)
        except Exception:
            visible = False
        if not visible:
            return
        username.fill(self.config.tms.username)
        page.locator(selectors.password).first.fill(password)
        button = self._locator_or_button(page, selectors.login_button, r"登录|登 录")
        button.click()
        page.wait_for_load_state("domcontentloaded")
        try:
            still_visible = username.is_visible(timeout=5_000)
        except Exception:
            still_visible = False
        if still_visible:
            raise TmsAuthenticationError("TMS 登录后仍停留在登录页，请检查账号或密码")

    @staticmethod
    def _open_order_page(page: object) -> None:
        page.get_by_text("订单管理", exact=True).first.click()
        page.get_by_text("集团订单管理", exact=True).first.click()

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
            if selectors.preset_name:
                page.locator(selectors.preset_name).first.click()
            else:
                page.get_by_text(preset, exact=True).first.click()
        if dataset == "current_month" and selectors.date_from_input:
            month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d 00:00")
            page.locator(selectors.date_from_input).first.fill(month_start)
        query = self._locator_or_button(page, selectors.query_button, r"查询")
        query.click()
        page.wait_for_load_state("networkidle")

    def _read_total(self, page: object) -> int | None:
        selector = self.config.tms.selectors.total_count
        if not selector:
            return None
        text = page.locator(selector).first.inner_text()
        match = re.search(r"([\d,]+)", text)
        return int(match.group(1).replace(",", "")) if match else None

    @staticmethod
    def _locator_or_button(page: object, selector: str, name_pattern: str) -> object:
        if selector:
            return page.locator(selector).first
        return page.get_by_role("button", name=re.compile(name_pattern)).last
