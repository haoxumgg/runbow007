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


class TmsExportTaskNotFound(TmsDownloadError):
    """Raised when an export click never produces a matching download-center task."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    ui_total: int | None
    dataset: str


@dataclass(slots=True)
class _ExportState:
    not_before: datetime
    expected_total: int | None = None
    task_created: bool = False
    budget_deadline: float | None = None


class TmsDownloader:
    # Playwright ignores the timeout passed to Locator.is_visible()/is_hidden() and
    # falls back to the page default (navigation_timeout_seconds). On a busy TMS page a
    # single probe then blocks for the full default and blows past the polling deadline
    # that was supposed to contain it. Locator.wait_for() honours its timeout, so every
    # visibility probe goes through _visible_now() below.
    #
    # 单次探测必须远小于循环时限，循环时限才有意义。_PROBE_TIMEOUT_MS 用于一次性的
    # 可见性判断（确认窗口、下载链接、登录表单）。
    _PROBE_TIMEOUT_MS = 1_000
    # 查找元素分两阶段，见 _find_visible。8/17 白天的失败说明"每个候选各等 1 秒、
    # 轮着来"在 TMS 慢的时候反而不如旧代码"死等第一个候选 45 秒"：页面卡住时每个
    # 探测都会耗满超时，一轮扫下来就把时限用光，实际只扫了一两轮。
    _QUICK_PROBE_MS = 250
    _PATIENT_PROBE_MS = 15_000
    _ELEMENT_WAIT_SECONDS = 60
    _RETRY_DELAYS = (0, 60, 180)
    # The hourly timer fires every 60 minutes and a second run is refused by the file
    # lock, so a single run must never spend a whole hour retrying: it would silently
    # eat the next slot. Give up on further attempts once the budget is spent.
    _RUN_BUDGET_SECONDS = 50 * 60

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def download(self, dataset: str = "current_month") -> DownloadResult:
        last_error: Exception | None = None
        attempts_made = 0
        state = _ExportState(
            not_before=self._local_now(),
            budget_deadline=time.monotonic() + self._RUN_BUDGET_SECONDS,
        )
        for attempt, delay in enumerate(self._RETRY_DELAYS, start=1):
            if delay:
                remaining = self._budget_remaining(state)
                if remaining is not None and remaining <= delay:
                    logger.error(
                        "本轮下载预算仅剩 %.0f 秒，放弃第 %s 次重试，避免占用下一个整点",
                        max(remaining, 0.0),
                        attempt,
                    )
                    break
                logger.warning("第 %s 次下载失败，%s 秒后重试", attempt - 1, delay)
                time.sleep(delay)
            attempts_made = attempt
            try:
                return self._download_once(dataset, state=state)
            except (CredentialError, TmsAuthenticationError):
                raise
            except Exception as exc:  # browser errors are normalized at the boundary
                if isinstance(exc, TmsExportTaskNotFound):
                    # The confirmation toast is not reliable. If the download center also
                    # has no matching task after a bounded wait, the click did not create
                    # an observable export and the next attempt must click Export again.
                    state.task_created = False
                    logger.warning("下载中心未发现本轮任务，下次重试将重新执行导出")
                last_error = exc
                logger.exception("TMS 下载第 %s 次失败", attempt)
        raise TmsDownloadError(
            f"TMS 下载连续 {attempts_made} 次失败: {last_error}"
        ) from last_error

    @staticmethod
    def _budget_remaining(state: _ExportState) -> float | None:
        if state.budget_deadline is None:
            return None
        return state.budget_deadline - time.monotonic()

    def _download_once(
        self, dataset: str, *, state: _ExportState | None = None
    ) -> DownloadResult:
        try:
            from playwright.sync_api import Page, sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise TmsDownloadError("请先安装 Playwright") from exc

        self.config.ensure_directories()
        state = state or _ExportState(not_before=self._local_now())
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
            if not state.task_created:
                logger.info("TMS 登录状态确认完成，打开集团订单管理")
                self._open_order_page(page)
                logger.info("集团订单管理已打开，应用数据筛选")
                self._apply_filters(page, dataset)
                ui_total = self._read_total(page)
                if ui_total is not None and ui_total > 0:
                    if state.expected_total is None:
                        state.expected_total = ui_total
                    elif state.expected_total != ui_total:
                        logger.warning(
                            "TMS 页面订单总数由 %s 变为 %s，继续使用首次有效总数",
                            state.expected_total,
                            ui_total,
                        )
                logger.info(
                    "TMS 筛选完成，页面订单总数=%s，本轮匹配总数=%s",
                    ui_total,
                    state.expected_total,
                )

                button = self._visible_locator_or_button(
                    page, self.config.tms.selectors.download_button, r"下载|批量导出"
                )
                # Anchor the download-center window on the click itself. Anchoring it on
                # the start of the run left minutes of slack in which somebody else's
                # export could be mistaken for ours.
                state.not_before = self._local_now()
                self._dom_click(button, "导出")
                logger.info("已点击导出，等待确认窗口")
                self._confirm_export(page)
                state.task_created = True
                logger.info("导出任务已创建，进入下载中心核验")
            else:
                logger.info(
                    "前次已创建导出任务，直接在下载中心复用；匹配总数=%s",
                    state.expected_total,
                )

            download = self._download_from_center(
                page,
                state.not_before,
                state.expected_total,
                budget_seconds=self._budget_remaining(state),
            )
            suggested = Path(download.suggested_filename)
            suffix = suggested.suffix.lower() if suggested.suffix else ".xls"
            target = run_dir / f"{dataset}-{uuid.uuid4().hex[:12]}{suffix}"
            download.save_as(target)
            logger.info("TMS Excel 已保存: %s", target.name)
            context.close()

        if not target.exists() or target.stat().st_size == 0:
            raise TmsDownloadError("浏览器报告下载完成，但文件为空")
        return DownloadResult(target, state.expected_total, dataset)

    def _login_if_needed(self, page: object, password: str) -> None:
        selectors = self.config.tms.selectors
        username = page.locator(selectors.username).first
        # TMS 是 SPA，DOMContentLoaded 后登录表单或首页菜单仍会延迟挂载。
        visible = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._visible_now(username):
                visible = True
                break
            home = page.get_by_text("下载中心", exact=True)
            if self._any_visible(home):
                return
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
        if self._visible_now(username, timeout_ms=5_000):
            raise TmsAuthenticationError("TMS 登录后仍停留在登录页，请检查账号或密码")

    @classmethod
    def _open_order_page(cls, page: object) -> None:
        advanced = page.locator("#searchItem").first
        if advanced.count() and cls._visible_now(advanced):
            return
        cls._click_visible_text(page, "订单管理")
        page.wait_for_timeout(500)
        cls._click_visible_text(page, "集团订单管理")
        advanced.wait_for(state="visible", timeout=30_000)

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
        deadline = time.monotonic() + self._ELEMENT_WAIT_SECONDS
        clicked = False
        while time.monotonic() < deadline:
            for index in range(confirms.count()):
                confirm = confirms.nth(index)
                if self._visible_now(confirm):
                    self._dom_click(confirm, "确定")
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
            if self._any_visible(success):
                return
            page.wait_for_timeout(250)
        # 成功提示是短暂 toast，页面响应慢时可能在定位前已消失。下载中心的
        # 最新任务时间、状态和条数才是最终确认依据，因此这里继续轮询下载中心。
        logger.info("未捕获到导出成功提示，继续在下载中心核验任务")

    def _download_from_center(
        self,
        page: object,
        export_started: datetime,
        expected_total: int | None,
        *,
        budget_seconds: float | None = None,
    ) -> object:
        self._click_visible_text(page, "下载中心", force=True)
        page.wait_for_timeout(2_000)
        wait_seconds = self.config.tms.download_timeout_seconds
        if budget_seconds is not None:
            wait_seconds = min(wait_seconds, max(budget_seconds, 60.0))
        deadline = time.monotonic() + wait_seconds
        appear_minutes = self.config.tms.export_task_appear_minutes
        task_appearance_deadline = min(
            deadline, time.monotonic() + appear_minutes * 60
        )
        earliest = export_started - timedelta(minutes=1)
        last_snapshot: tuple[object, ...] | None = None
        matching_task_seen = False

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
                if not self._total_matches(record_count, expected_total):
                    continue
                matching_task_seen = True
                if "失败" in text:
                    raise TmsDownloadError(f"TMS 下载中心任务失败: {text[:200]}")
                if "成功" not in text:
                    continue
                link = row.locator("a:has(img[src*='excel'])").first
                if not link.count() or not self._visible_now(link):
                    continue
                remaining = max(1, int(deadline - time.monotonic())) * 1_000
                with page.expect_download(timeout=remaining) as info:
                    link.click()
                return info.value

            self._click_first_visible_dom(page.locator("#refreshItem"))
            page.wait_for_timeout(2_000)
            if (
                not matching_task_seen
                and time.monotonic() >= task_appearance_deadline
            ):
                raise TmsExportTaskNotFound(
                    f"点击导出后 {appear_minutes} 分钟内，下载中心未出现本轮匹配任务"
                )

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

    def _total_matches(self, record_count: int | None, expected_total: int | None) -> bool:
        """Accept the export task whose row count is close enough to the page total.

        订单数在"读取页面总数"到"TMS 真正生成导出"之间会继续变化（实测一小时内
        4672→4677），此前的严格相等比较会让本轮任务永远匹配不上，5 分钟后重新点击
        导出，最终整轮失败。容差用于吸收这种漂移，同时仍然排除条数量级不同的
        无关任务。
        """
        if expected_total is None:
            # 页面总数没读出来（TMS 慢时 _read_total 会返回 0）就不做条数过滤，
            # 只靠时间窗判断归属。这里一旦改成拒绝，本轮任务永远匹配不上。
            return True
        if record_count is None:
            return False
        drift = abs(record_count - expected_total)
        if drift > self.config.tms.total_tolerance:
            return False
        if drift:
            logger.info(
                "下载中心任务条数 %s 与页面总数 %s 相差 %s，在容差 %s 内，按本轮任务处理",
                record_count,
                expected_total,
                drift,
                self.config.tms.total_tolerance,
            )
        return True

    @classmethod
    def _visible_now(cls, locator: object, *, timeout_ms: int | None = None) -> bool:
        """Bounded visibility probe.

        ``Locator.is_visible(timeout=...)`` silently ignores its timeout and uses the
        page default instead, so probing with it turned every polling loop in this
        module into a single 45 秒 block that raised a raw ``TimeoutError`` past the
        deadline it was supposed to respect. ``wait_for`` honours the timeout it is
        given; a probe that does not resolve in time simply means "not visible yet".
        """
        try:
            locator.wait_for(
                state="visible", timeout=timeout_ms or cls._PROBE_TIMEOUT_MS
            )
        except Exception:
            return False
        return True

    @classmethod
    def _find_visible(
        cls, page: object, collections: list, deadline: float
    ) -> object | None:
        """Two-phase element hunt for a SPA that renders duplicate, slow toolbars.

        阶段一用很短的探测快速扫一遍所有候选，挑出"已经可见"的那个——TMS 会把同一
        个工具栏渲染多份，只有一份可见，这一步就是为了跳过隐藏的副本。

        阶段二在一整轮都没扫到时，对第一个候选做一次长等待。TMS 慢的时候元素只是
        还没渲染出来，继续用短探测轮询纯属空转；把耐心押在单个候选上，正是旧代码
        （45 秒默认超时）在这种场景下反而能成功的原因。
        """
        while time.monotonic() < deadline:
            for matches in collections:
                for index in range(matches.count()):
                    candidate = matches.nth(index)
                    if cls._visible_now(candidate, timeout_ms=cls._QUICK_PROBE_MS):
                        return candidate
            if time.monotonic() >= deadline:
                break
            patient = next(
                (matches.nth(0) for matches in collections if matches.count()), None
            )
            if patient is not None and cls._visible_now(
                patient, timeout_ms=cls._PATIENT_PROBE_MS
            ):
                return patient
            page.wait_for_timeout(250)
        return None

    @classmethod
    def _any_visible(cls, matches: object) -> bool:
        try:
            total = matches.count()
        except Exception:
            return False
        return any(cls._visible_now(matches.nth(index)) for index in range(total))

    @staticmethod
    def _locator_or_button(page: object, selector: str, name_pattern: str) -> object:
        if selector:
            return page.locator(selector).first
        return page.get_by_role("button", name=re.compile(name_pattern)).last

    @classmethod
    def _visible_locator_or_button(
        cls, page: object, selector: str, name_pattern: str
    ) -> object:
        """Return the visible button when the SPA keeps hidden duplicate toolbars."""
        collections = []
        if selector:
            collections.append(page.locator(selector))
        collections.append(
            page.get_by_role("button", name=re.compile(name_pattern))
        )
        deadline = time.monotonic() + cls._ELEMENT_WAIT_SECONDS
        button = cls._find_visible(page, collections, deadline)
        if button is None:
            raise TmsDownloadError("页面未找到可见的导出按钮")
        return button

    @classmethod
    def _click_first_visible_dom(cls, matches: object) -> bool:
        """Click the visible copy when the SPA renders duplicate toolbar controls."""
        for index in range(matches.count()):
            candidate = matches.nth(index)
            if cls._visible_now(candidate):
                candidate.evaluate("element => element.click()")
                return True
        return False

    @staticmethod
    def _dom_click(locator: object, name: str) -> None:
        # 调用方已经用 _visible_now 确认过可见性，这里不再 wait_for。Element UI 的
        # 工具栏会反复重渲染，重复等待时 Playwright 会报"resolved to visible"却仍然
        # 超时（2026-08-16 21:10 那轮就挂在这里），等于凭空多一个失败点。
        if not locator.is_enabled(timeout=5_000):
            raise TmsDownloadError(f"页面按钮不可用: {name}")
        # 李宁 TMS 是 SPA，Playwright 的鼠标点击偶发卡在 actionability 或
        # 导航等待。元素已确认可见且可用后触发 DOM click，再由后续业务状态
        # 判断点击是否真正生效。
        locator.evaluate("element => element.click()")

    @classmethod
    def _click_visible_text(cls, page: object, text: str, *, force: bool = False) -> None:
        deadline = time.monotonic() + cls._ELEMENT_WAIT_SECONDS
        candidate = cls._find_visible(
            page, [page.get_by_text(text, exact=True)], deadline
        )
        if candidate is None:
            raise TmsDownloadError(f"页面未找到可见元素: {text}")
        if force:
            # TMS 的成功提示层可能长期覆盖菜单。这里直接触发已确认可见菜单元素的
            # DOM click，避免覆盖层截获鼠标事件。
            candidate.evaluate("element => element.click()")
        else:
            candidate.click()
