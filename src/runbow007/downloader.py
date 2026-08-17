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
    last_step: str | None = None


class _StepTracker:
    """Name the phase we are in so a failure says *where* it went wrong.

    之前失败只能看 traceback 猜环节，而且看不出"在这一步卡了多久"。这个记录当前
    环节名和进入时间，异常时一起打出来，飞书告警里也带上。
    """

    __slots__ = ("name", "started")

    def __init__(self) -> None:
        self.name = "启动浏览器"
        self.started = time.monotonic()

    def enter(self, name: str) -> None:
        self.name = name
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


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
        where = f"，最后卡在「{state.last_step}」" if state.last_step else ""
        raise TmsDownloadError(
            f"TMS 下载连续 {attempts_made} 次失败{where}: {last_error}"
        ) from last_error

    def _new_browser(self, playwright: object) -> tuple[object, object]:
        """Start from a clean browser unless the profile is explicitly opted into.

        持久化 profile 会把上一轮的标签页、缓存和 DOM 状态全带到下一轮。TMS 又是
        标签页式 SPA、视图状态还按账号共享，于是每轮启动时"DOM 里有几个标签页、
        停在哪个视图、哪些元素可见"完全不确定，所有选择器都在这片流沙上撞运气。
        凌晨 01–07 七轮全成、白天一半在挂，差别就在于白天有人在动这个账号。

        改成每轮全新浏览器 + 重新登录，用几秒登录时间换一个确定的起点。
        """
        headless = self.config.tms.headless
        if self.config.tms.persistent_profile:
            context = playwright.chromium.launch_persistent_context(
                str(self.config.runtime.browser_profile_dir),
                headless=headless,
                accept_downloads=True,
            )
            return None, context
        browser = playwright.chromium.launch(headless=headless)
        return browser, browser.new_context(accept_downloads=True)

    def _capture_failure(self, page: object, step: str, run_dir: Path) -> None:
        """Dump a screenshot and the DOM so the next failure needs no guessing."""
        if page is None:
            return
        stamp = self._local_now().strftime("%H%M%S")
        safe = re.sub(r"[^\w]+", "-", step).strip("-")
        base = run_dir / f"failure-{stamp}-{safe}"
        for suffix, dump in (
            (".png", lambda path: page.screenshot(path=str(path), timeout=15_000)),
            (".html", lambda path: path.write_text(page.content(), encoding="utf-8")),
        ):
            target = base.with_suffix(suffix)
            try:
                dump(target)
                logger.error("失败现场已保存: %s", target.name)
            except Exception:  # pragma: no cover - diagnostics must never mask the cause
                logger.warning("保存 %s 失败现场时出错", suffix, exc_info=True)

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

        step = _StepTracker()
        page: Page | None = None
        with sync_playwright() as playwright:
            browser = context = None
            try:
                logger.info("启动 TMS 浏览器")
                browser, context = self._new_browser(playwright)
                page = context.new_page()
                page.set_default_timeout(
                    self.config.tms.navigation_timeout_seconds * 1000
                )

                step.enter("打开 TMS 首页")
                page.goto(self.config.tms.url, wait_until="domcontentloaded")
                logger.info("TMS 首页已加载，检查登录状态")

                step.enter("登录")
                self._login_if_needed(page, password)
                if not state.task_created:
                    step.enter("打开集团订单管理")
                    logger.info("TMS 登录状态确认完成，打开集团订单管理")
                    self._open_order_page(page)

                    step.enter("应用筛选并查询")
                    logger.info("集团订单管理已打开，应用数据筛选")
                    self._apply_filters(page, dataset)

                    step.enter("等待订单表格出数据")
                    ui_total = self._wait_for_orders_loaded(page)
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

                    step.enter("查找导出按钮")
                    button = self._visible_locator_or_button(
                        page, self.config.tms.selectors.download_button, r"下载|批量导出"
                    )

                    step.enter("点击导出并确认")
                    # Anchor the download-center window on the click itself. Anchoring
                    # it on the start of the run left minutes of slack in which somebody
                    # else's export could be mistaken for ours.
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
                    step=step,
                )

                step.enter("保存 Excel")
                suggested = Path(download.suggested_filename)
                suffix = suggested.suffix.lower() if suggested.suffix else ".xls"
                target = run_dir / f"{dataset}-{uuid.uuid4().hex[:12]}{suffix}"
                download.save_as(target)
                logger.info("TMS Excel 已保存: %s", target.name)
            except Exception:
                state.last_step = step.name
                logger.error(
                    "失败环节: %s（该环节已耗时 %.0f 秒）", step.name, step.elapsed
                )
                self._capture_failure(page, step.name, run_dir)
                raise
            finally:
                for closeable in (context, browser):
                    if closeable is not None:
                        try:
                            closeable.close()
                        except Exception:  # pragma: no cover - best effort teardown
                            logger.debug("关闭浏览器时出错", exc_info=True)

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
            # 视图状态是账号级共享且粘性的——默认视图就是"上一次操作的视图"，别人
            # （或人工在浏览器里）切过视图，下一轮就会继承那个。所以每轮都必须显式
            # 选回预设，否则可能拿着别的视图的筛选范围导出，条数校验还查不出来。
            preset_trigger = selectors.preset_name or ".el-dialog:visible .page-header-title"
            page.locator(preset_trigger).first.click()
            self._click_visible_text(page, preset)
        # 日期不再填：预设自身已经带了日期范围，重复填写只是多一个会出错的操作。
        query = self._locator_or_button(page, selectors.query_button, r"查询")
        query.click()
        self._wait_for_grid_loading(page)

    def _wait_for_grid_loading(self, page: object) -> None:
        """Wait out the "拼命加载中" mask that TMS shows while the grid loads.

        networkidle 在 TMS 上几乎必然超时（后台请求就没停过），之前只能退回"死等
        2 秒"，数据量大时远远不够。Element UI 的加载遮罩才是准确信号：它出现代表
        查询发出去了，它消失代表表格渲染完了。
        """
        mask = page.locator(".el-loading-mask")
        # 点查询到遮罩挂上有延迟；没等到也不算错，可能查询快到遮罩一闪而过。
        appeared = self._visible_now(mask.first, timeout_ms=5_000)
        if not appeared:
            logger.info("未捕获到加载遮罩，直接按超时等待表格")
        deadline = time.monotonic() + self.config.tms.grid_load_timeout_seconds
        while time.monotonic() < deadline:
            if not self._any_visible(mask):
                return
            page.wait_for_timeout(500)
        logger.warning(
            "加载遮罩 %s 秒内未消失，继续尝试读取表格",
            self.config.tms.grid_load_timeout_seconds,
        )

    def _read_total(self, page: object) -> int | None:
        """Read the order count from the *visible* pagination control.

        TMS 是标签页式 SPA，打开过的页面全都留在 DOM 里，非活动的只是隐藏。实测
        （2026-08-17 在真实页面上跑 querySelectorAll）button.pagination-total 会同时
        匹配到两个：集团订单管理的「共 4753 条」和下载中心的「共 34920 条」。

        原来硬取 .first 等于赌 DOM 顺序——赌输了就一直在读另一个标签页的分页，而且
        那个数字永远不会变成我们要的值，等多久都没用。改成只认可见的那个。
        """
        selector = self.config.tms.selectors.total_count
        if not selector:
            return None
        matches = page.locator(selector)
        try:
            count = matches.count()
        except Exception:
            return None
        for index in range(count):
            candidate = matches.nth(index)
            if not self._visible_now(candidate, timeout_ms=self._QUICK_PROBE_MS):
                continue
            try:
                text = candidate.inner_text(timeout=self._PROBE_TIMEOUT_MS)
            except Exception:
                # 分页组件还没挂载。不能按页面默认的 45 秒硬等——那会直接搞挂
                # 一整次尝试（2026-08-16 23:59 那轮就是 inner_text 超时抛的）。
                continue
            match = re.search(r"([\d,]+)", text)
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    def _wait_for_orders_loaded(self, page: object) -> int | None:
        """Wait until the filtered grid actually reports rows before exporting.

        _apply_filters 只固定等 2 秒，而 networkidle 在 TMS 上几乎必然超时跳过，
        所以经常在"分页组件已渲染、数据还没回来"的瞬间读到 0。带着 0 继续往下走
        就是在空表格上点导出：TMS 没有数据可导，不会创建任何任务，然后在下载中心
        白等 8 分钟才发现（2026-08-17 15:05 那轮为此烧掉 9 分钟）。

        这里改成轮询等真实条数，等不到就快速失败——把时间留给下一次重试，而不是
        喂给一个注定无效的导出。
        """
        if not self.config.tms.selectors.total_count:
            return None
        wait_seconds = self.config.tms.grid_load_timeout_seconds
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            total = self._read_total(page)
            if total:
                return total
            page.wait_for_timeout(500)
        raise TmsDownloadError(
            f"筛选后 {wait_seconds} 秒内订单总数仍为 0，表格未加载完成，本次不执行导出"
        )

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

    def _open_download_center(self, page: object) -> None:
        """Reach the download centre without relying on a text match.

        右上角这个入口在真实页面里长这样（2026-08-17 从 DOM 抓的）：

            <ul class="right_menu">
              <li class="menu-item"><i class="thorn6-icon thorn6-icon-xiazai"></i>
                下载中心
              </li>

        图标类名唯一且稳定，跟导出按钮用的 thorn6-icon-daoru 是同一套模式；而
        get_by_text("下载中心", exact=True) 匹配的文本节点前后带大量空白，今天
        10:05 和 14:05 第 1 次都没找到它，各白烧掉一整次尝试。

        再找不到就重新加载首页重来一次：导出任务此时已经在服务端建好了，页面状态
        不再重要，换一份干净的 DOM 比放弃整次尝试便宜得多——14:05 第 2 次正是靠
        重开浏览器后 10 秒就找到了。
        """
        selector = self.config.tms.selectors.download_center_menu
        for attempt in range(2):
            collections = []
            if selector:
                collections.append(page.locator(selector))
            collections.append(page.get_by_text("下载中心", exact=True))
            deadline = time.monotonic() + self._ELEMENT_WAIT_SECONDS
            menu = self._find_visible(page, collections, deadline)
            if menu is not None:
                # 导出成功提示层可能盖住菜单，用 DOM click 避免覆盖层截获鼠标事件。
                menu.evaluate("element => element.click()")
                return
            if attempt:
                break
            logger.warning("未找到下载中心入口，重新加载首页后重试")
            page.goto(self.config.tms.url, wait_until="domcontentloaded")
            page.wait_for_timeout(2_000)
        raise TmsDownloadError("页面未找到可见元素: 下载中心")

    def _download_from_center(
        self,
        page: object,
        export_started: datetime,
        expected_total: int | None,
        *,
        budget_seconds: float | None = None,
        step: _StepTracker | None = None,
    ) -> object:
        if step is not None:
            step.enter("进入下载中心")
        self._open_download_center(page)
        page.wait_for_timeout(2_000)
        if step is not None:
            step.enter("等待下载中心出现本轮任务")
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
        link_missing_logged = False

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
                    # 之前这里是静默 continue，于是"任务明明成功却下不下来"完全
                    # 看不见：2026-08-17 17:59 匹配到了 success=True 的任务，却
                    # 一直空转到 18:08 超时。Element UI 的固定列会把 tbody 复制
                    # 成多份，含任务名的行和含下载图标的行可能根本不是同一个 tr。
                    if not link_missing_logged:
                        logger.warning(
                            "已匹配到成功的任务但找不到可见的下载链接，继续刷新重试"
                        )
                        link_missing_logged = True
                    continue
                if step is not None:
                    step.enter("下载 Excel 文件")
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
