from __future__ import annotations

import logging
import re
import signal
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from .config import AppConfig
from .credentials import CredentialError, get_tms_password

logger = logging.getLogger(__name__)


class TmsDownloadError(RuntimeError):
    """Raised when browser automation cannot produce a download."""


class TmsAuthenticationError(TmsDownloadError):
    """Raised for credential problems that should not be retried."""


class TmsExportTaskNotFound(TmsDownloadError):
    """Raised when an export click never produces a new download-center task."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    ui_total: int | None
    dataset: str


@dataclass(slots=True)
class _ExportState:
    """What survives across retries within one run."""

    # 点导出前下载中心已有的最大任务 id。本轮任务就是"id 比它大"的那个。
    baseline_id: int | None = None
    expected_total: int | None = None
    task_created: bool = False
    budget_deadline: float | None = None
    last_step: str | None = None


class _StepTracker:
    """Name the phase we are in so a failure says *where* it went wrong."""

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


def _export_id(href: str | None) -> int | None:
    """Read the export task id out of a download-center link."""
    if not href:
        return None
    values = parse_qs(urlparse(href).query).get("id") or []
    try:
        return int(values[0])
    except (IndexError, ValueError):
        return None


def _detect_suffix(body: bytes) -> str | None:
    """Identify the payload by magic bytes rather than trusting the filename.

    TMS 在会话过期或任务出错时会返回一个 HTML 页面，HTTP 状态码仍是 200。按文件名
    后缀存成 .xls 再交给 xlrd，报出来的错跟真实原因毫无关系。这里在落盘前就认出来。
    """
    if body[:2] == b"PK":
        return ".xlsx"
    if body[:4] == b"\xd0\xcf\x11\xe0":
        return ".xls"
    return None


class TmsDownloader:
    # Playwright ignores the timeout passed to Locator.is_visible() and falls back to
    # the page default, which turns every bounded probe into a full-length block.
    # Locator.wait_for() honours its timeout, so visibility goes through _visible().
    _PROBE_MS = 1_000
    _ELEMENT_WAIT_MS = 30_000
    _RETRY_DELAYS = (0, 60, 180)
    # The hourly timer fires every 60 minutes and a second run is refused by the file
    # lock, so one run must never spend a whole hour retrying: it would eat the next slot.
    _RUN_BUDGET_SECONDS = 50 * 60

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ retry

    def download(self, dataset: str = "current_month") -> DownloadResult:
        last_error: Exception | None = None
        attempts_made = 0
        state = _ExportState(budget_deadline=time.monotonic() + self._RUN_BUDGET_SECONDS)
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
                with self._attempt_watchdog():
                    return self._download_once(dataset, state=state)
            except (CredentialError, TmsAuthenticationError):
                raise
            except Exception as exc:  # browser errors are normalized at the boundary
                if isinstance(exc, TmsExportTaskNotFound):
                    # 下载中心始终没出现比基线更新的任务，说明那次点击没生效，
                    # 下一次必须重新走一遍筛选和导出。
                    state.task_created = False
                    logger.warning("下载中心未出现本轮任务，下次重试将重新执行导出")
                last_error = exc
                logger.exception("TMS 下载第 %s 次失败", attempt)
        where = f"，最后卡在「{state.last_step}」" if state.last_step else ""
        raise TmsDownloadError(
            f"TMS 下载连续 {attempts_made} 次失败{where}: {last_error}"
        ) from last_error

    @contextmanager
    def _attempt_watchdog(self) -> Iterator[None]:
        """Hard wall-clock ceiling for one attempt, independent of the browser.

        所有其它超时都是"建议性"的：Playwright 的 timeout 靠驱动回消息才能触发，
        循环 deadline 靠调用能返回才会被检查。浏览器渲染进程一卡死，这些全部失效。
        SIGALRM 由内核投递，是唯一真正兜得住的一层；Windows 没有它，那里退化成无保护。
        """
        seconds = self.config.tms.attempt_timeout_seconds
        if seconds <= 0 or not hasattr(signal, "SIGALRM"):
            yield
            return

        def _fire(signum: int, frame: object) -> None:
            raise TmsDownloadError(f"单次尝试超过 {seconds} 秒硬上限，已强制中断")

        previous = signal.signal(signal.SIGALRM, _fire)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    @staticmethod
    def _budget_remaining(state: _ExportState) -> float | None:
        if state.budget_deadline is None:
            return None
        return state.budget_deadline - time.monotonic()

    # ------------------------------------------------------------- one attempt

    def _download_once(
        self, dataset: str, *, state: _ExportState | None = None
    ) -> DownloadResult:
        try:
            from playwright.sync_api import Page, sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise TmsDownloadError("请先安装 Playwright") from exc

        self.config.ensure_directories()
        state = state or _ExportState()
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

                step.enter("登录")
                self._login_if_needed(page, password)

                if state.baseline_id is None:
                    # 必须在点导出之前拍这一张快照，之后"id 更大"才等于"本轮新建的"。
                    step.enter("记录下载中心已有任务")
                    self._open_download_center(page)
                    links = self._export_links(page)
                    state.baseline_id = max(links, default=0)
                    logger.info(
                        "下载中心现有 %s 个导出文件，最大任务 id=%s",
                        len(links),
                        state.baseline_id,
                    )

                if state.task_created:
                    logger.info("前次已创建导出任务，直接回下载中心取文件")
                else:
                    step.enter("打开集团订单管理")
                    self._open_order_page(page)

                    step.enter("应用筛选并查询")
                    self._apply_filters(page, dataset)

                    step.enter("等待订单表格出数据")
                    ui_total = self._wait_for_orders(page)
                    if ui_total and state.expected_total is None:
                        state.expected_total = ui_total
                    logger.info(
                        "筛选完成，页面订单总数=%s，本轮匹配总数=%s",
                        ui_total,
                        state.expected_total,
                    )

                    step.enter("点击导出并确认")
                    self._click_export(page)
                    state.task_created = True
                    logger.info("导出任务已创建，进入下载中心等待生成")

                step.enter("等待下载中心生成文件")
                href = self._await_export(page, state)

                step.enter("下载并保存 Excel")
                stem = f"{dataset}-{uuid.uuid4().hex[:12]}"
                target = self._save_export(context, href, stem, run_dir)
                logger.info("TMS Excel 已保存: %s（%s 字节）", target.name, target.stat().st_size)
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

        return DownloadResult(target, state.expected_total, dataset)

    # ----------------------------------------------------------------- browser

    def _new_browser(self, playwright: object) -> tuple[object, object]:
        """Start from a clean browser unless the profile is explicitly opted into.

        持久化 profile 会把上一轮的标签页、缓存和 DOM 状态全带到下一轮。TMS 是标签页式
        SPA、视图状态又按账号共享，于是每轮的页面结构都不确定。默认每轮全新浏览器 +
        重新登录，用几秒登录时间换一个确定的起点。

        固定 1600×900 视口：导出按钮在分页栏右下角，窗口窄了会被折叠进溢出菜单。
        """
        options = {"viewport": {"width": 1600, "height": 900}, "locale": "zh-CN"}
        headless = self.config.tms.headless
        if self.config.tms.persistent_profile:
            context = playwright.chromium.launch_persistent_context(
                str(self.config.runtime.browser_profile_dir), headless=headless, **options
            )
            return None, context
        browser = playwright.chromium.launch(headless=headless)
        return browser, browser.new_context(**options)

    def _capture_failure(self, page: object, step: str, run_dir: Path) -> None:
        """Dump a screenshot and the DOM so the next failure needs no guessing.

        截图先做，因为它能带 timeout。截图失败说明浏览器已经不响应了，这时候绝不能再去
        调 page.content()——那个 API 不接受 timeout，页面卡死时会无限期挂着。
        """
        if page is None:
            return
        stamp = self._local_now().strftime("%H%M%S")
        safe = re.sub(r"[^\w]+", "-", step).strip("-")
        base = run_dir / f"failure-{stamp}-{safe}"
        try:
            page.screenshot(path=str(base.with_suffix(".png")), timeout=15_000)
            logger.error("失败现场已保存: %s", base.with_suffix(".png").name)
        except Exception:
            logger.warning("浏览器已无响应，跳过失败现场采集", exc_info=True)
            return
        try:
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            logger.error("失败现场已保存: %s", base.with_suffix(".html").name)
        except Exception:  # pragma: no cover - diagnostics must never mask the cause
            logger.warning("保存页面 HTML 时出错", exc_info=True)

    # ------------------------------------------------------------------- steps

    def _login_if_needed(self, page: object, password: str) -> None:
        selectors = self.config.tms.selectors
        username = page.locator(selectors.username).first
        # TMS 是 SPA，DOMContentLoaded 之后登录表单还要再挂一会儿。等不到就说明
        # 这个会话已经登录过了（persistent_profile 模式），直接往下走。
        if not self._visible(username, 15_000):
            return
        username.fill(self.config.tms.username)
        page.locator(selectors.password).first.fill(password)
        page.locator(selectors.login_button).first.click()
        try:
            page.wait_for_function(
                "() => !location.hash.includes('/login')", timeout=30_000
            )
        except Exception:
            pass
        if self._visible(username, 5_000):
            raise TmsAuthenticationError("TMS 登录后仍停留在登录页，请检查账号或密码")
        logger.info("TMS 登录成功")

    def _open_order_page(self, page: object) -> None:
        selectors = self.config.tms.selectors
        advanced = page.locator(selectors.advanced_filter_button).first
        if self._visible(advanced, 250):
            return
        page.locator(selectors.order_menu).first.click()
        page.wait_for_timeout(500)
        page.locator(selectors.group_order_menu).first.click()
        if not self._visible(advanced, self._ELEMENT_WAIT_MS):
            raise TmsDownloadError("集团订单管理未加载出高级筛选按钮")

    def _apply_filters(self, page: object, dataset: str) -> None:
        selectors = self.config.tms.selectors
        page.locator(selectors.advanced_filter_button).first.click()
        preset = (
            self.config.tms.current_month_preset
            if dataset == "current_month"
            else self.config.tms.open_carryover_preset
        )
        if preset:
            # 视图状态是账号级共享且粘性的——默认视图就是"上一次操作的视图"，别人
            # 切过视图下一轮就会继承。每轮都必须显式选回预设。
            page.locator(selectors.preset_name).first.click()
            option = page.locator(selectors.preset_option).filter(has_text=preset).first
            if not self._visible(option, self._ELEMENT_WAIT_MS):
                raise TmsDownloadError(f"高级筛选里没有找到预设视图: {preset}")
            option.click()
            page.wait_for_timeout(500)
        # 日期不再填：预设自身已经带了日期范围，重复填写只是多一个会出错的操作。
        page.locator(selectors.query_button).first.click()

    def _wait_for_orders(self, page: object) -> int | None:
        """Wait out the loading mask and refuse to export against an empty grid.

        带着 0 条继续走就是在空表格上点导出：TMS 不会创建任何任务，然后在下载中心
        白等到超时。这里等不到真实条数就快速失败，把时间留给下一次重试。
        """
        selector = self.config.tms.selectors.total_count
        timeout = self.config.tms.grid_load_timeout_seconds
        deadline = time.monotonic() + timeout
        mask = page.locator(".el-loading-mask")
        page.wait_for_timeout(1_000)
        while time.monotonic() < deadline:
            if not self._visible(mask.first, 250):
                if not selector:
                    return None
                total = self._read_total(page)
                if total:
                    return total
            page.wait_for_timeout(500)
        raise TmsDownloadError(
            f"筛选后 {timeout} 秒内表格仍未加载出订单，本轮不执行导出"
        )

    def _read_total(self, page: object) -> int | None:
        """Read the order count from the *visible* pagination control.

        TMS 是标签页式 SPA，打开过的页面全留在 DOM 里，非活动的只是隐藏。实测
        button.pagination-total 会同时匹配集团订单管理的「共 4753 条」和下载中心的
        「共 34920 条」，所以选择器里必须带 :visible。
        """
        matches = page.locator(self.config.tms.selectors.total_count)
        try:
            if not matches.count():
                return None
            text = matches.first.inner_text(timeout=self._PROBE_MS)
        except Exception:
            return None
        found = re.search(r"([\d,]+)", text)
        return int(found.group(1).replace(",", "")) if found else None

    def _click_export(self, page: object) -> None:
        selectors = self.config.tms.selectors
        button = page.locator(selectors.download_button).first
        if not self._visible(button, self._ELEMENT_WAIT_MS):
            raise TmsDownloadError("页面未找到可见的导出按钮")
        # Element UI 的成功提示层会盖住工具栏，鼠标点击可能被覆盖层截获；元素已确认
        # 可见后直接触发 DOM click。
        button.evaluate("element => element.click()")
        page.wait_for_timeout(1_000)
        confirm = page.locator(selectors.confirm_button).last
        if not self._visible(confirm, self._ELEMENT_WAIT_MS):
            raise TmsDownloadError("点击导出后未出现确认窗口")
        confirm.evaluate("element => element.click()")

    def _open_download_center(self, page: object) -> None:
        """Reach the download centre by its icon class, not by matching link text.

        真实 DOM 里这个入口的文本节点前后带大量空白，exact 文本匹配失败过好几次；
        图标类名唯一且稳定。再找不到就重新加载首页重来一次——导出任务此时已经在服务端
        建好了，页面状态不再重要，换一份干净的 DOM 比放弃整次尝试便宜得多。
        """
        selector = self.config.tms.selectors.download_center_menu
        for attempt in range(2):
            menu = page.locator(selector).first
            if self._visible(menu, 15_000):
                menu.evaluate("element => element.click()")
                page.wait_for_timeout(2_000)
                return
            if attempt == 0:
                logger.warning("未找到下载中心入口，重新加载首页后重试")
                page.goto(self.config.tms.url, wait_until="domcontentloaded")
                page.wait_for_timeout(2_000)
        raise TmsDownloadError("页面未找到下载中心入口")

    def _export_links(self, page: object) -> dict[int, str]:
        """Map every download-center export file to its task id.

        直接读 <a href> 而不是去匹配表格行：Element UI 的固定列会把 tbody 复制成多份，
        含任务名的行和含下载图标的行经常不是同一个 tr，按行匹配会莫名其妙地找不到链接。
        链接只在任务生成完毕后才渲染，所以"链接存在"本身就等于"任务已完成"。
        """
        try:
            hrefs = page.locator(self.config.tms.selectors.export_link).evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('href'))"
            )
        except Exception:
            return {}
        links: dict[int, str] = {}
        for href in hrefs:
            export_id = _export_id(href)
            if export_id is not None:
                links[export_id] = href
        return links

    def _await_export(self, page: object, state: _ExportState) -> str:
        """Poll the download centre until a task newer than the baseline shows up.

        只有一个超时。等不到就抛 TmsExportTaskNotFound，下一次重试重新走一遍导出——
        这里分不出"点击没生效"和"任务生成得特别慢"（两种情况看到的都是"没有新链接"），
        而重新导出对两种情况都是对的：多出来的那个任务只是下载中心里多一行，
        基线比较照样取最新的那个。
        """
        self._open_download_center(page)
        baseline = state.baseline_id or 0
        wait_seconds = float(self.config.tms.download_timeout_seconds)
        budget = self._budget_remaining(state)
        if budget is not None:
            wait_seconds = min(wait_seconds, max(budget, 60.0))
        deadline = time.monotonic() + wait_seconds
        refresh = self.config.tms.selectors.refresh_button
        seen = -1

        while True:
            links = self._export_links(page)
            fresh = [export_id for export_id in links if export_id > baseline]
            if fresh:
                newest = max(fresh)
                logger.info("下载中心出现本轮导出文件 id=%s（基线 %s）", newest, baseline)
                return links[newest]
            newest_seen = max(links, default=0)
            if newest_seen != seen:
                logger.info(
                    "下载中心暂无新文件：现有 %s 个，最大 id=%s，等待 id>%s",
                    len(links),
                    newest_seen,
                    baseline,
                )
                seen = newest_seen
            if time.monotonic() >= deadline:
                raise TmsExportTaskNotFound(
                    f"点击导出后 {wait_seconds:.0f} 秒内，下载中心未出现新的导出文件"
                )
            # 刷新按钮偶尔不在（表格重渲染中），点不到就等下一轮，不是错误。
            try:
                page.locator(refresh).first.click(timeout=self._PROBE_MS)
            except Exception:
                logger.debug("下载中心刷新按钮暂不可点", exc_info=True)
            page.wait_for_timeout(5_000)

    def _save_export(
        self, context: object, href: str, stem: str, run_dir: Path
    ) -> Path:
        """Fetch the export through the browser session and verify what came back.

        用会话直接 GET 链接，而不是点链接等浏览器下载事件：少一个"点击没被识别成下载"
        的失败点，拿到的又是完整字节流，能在落盘前先验格式。
        """
        parsed = urlparse(self.config.tms.url)
        url = urljoin(f"{parsed.scheme}://{parsed.netloc}", href)
        logger.info("下载导出文件: %s", url)
        response = context.request.get(
            url, timeout=self.config.tms.download_timeout_seconds * 1000
        )
        if not response.ok:
            raise TmsDownloadError(f"下载导出文件失败: HTTP {response.status}")
        body = response.body()
        suffix = _detect_suffix(body)
        if suffix is None:
            evidence = run_dir / f"{stem}-bad-body.bin"
            evidence.write_bytes(body[:1_000_000])
            raise TmsDownloadError(
                f"下载内容不是 Excel（{len(body)} 字节，开头 {body[:8]!r}），"
                f"已保存现场 {evidence.name}"
            )
        target = run_dir / f"{stem}{suffix}"
        target.write_bytes(body)
        return target

    # ----------------------------------------------------------------- helpers

    def _local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.runtime.timezone)).replace(tzinfo=None)

    @classmethod
    def _visible(cls, locator: object, timeout_ms: int | None = None) -> bool:
        """Bounded visibility probe.

        ``Locator.is_visible(timeout=...)`` silently ignores its timeout and uses the
        page default instead, turning every polling loop into one long block that then
        raises past the deadline it was supposed to respect. ``wait_for`` honours the
        timeout it is given; not resolving in time simply means "not visible yet".
        """
        try:
            locator.wait_for(state="visible", timeout=timeout_ms or cls._PROBE_MS)
        except Exception:
            return False
        return True
