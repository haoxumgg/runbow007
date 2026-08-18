from __future__ import annotations

import logging
import re
import signal
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
    """Raised when an export click never produces a usable download-center task."""


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


@dataclass(frozen=True, slots=True)
class _DownloadTask:
    """One row of the download centre, already merged across fixed-column copies."""

    started_at: datetime | None
    record_count: int | None
    succeeded: bool
    failed: bool
    href: str | None
    text: str


class _StepTracker:
    """Name the phase we are in so a failure says *where* it went wrong."""

    __slots__ = ("name", "started")

    def __init__(self) -> None:
        self.name = "启动浏览器"
        self.started = time.monotonic()

    def enter(self, name: str) -> None:
        logger.info("→ %s", name)
        self.name = name
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


# 下载中心的表格被 Element UI 的固定列拆成多份 <table>：任务名可能在左边那份、
# 下载图标在右边那份，同一条记录未必落在同一个 <tr> 里。一次 evaluate 把所有可见
# 表格按行序号合并，既避开了这个坑，也避免几十次 locator 往返各自超时。
_SCRAPE_DOWNLOAD_ROWS = """
(linkSelector) => {
  const merged = new Map();
  for (const table of document.querySelectorAll('table')) {
    const box = table.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    table.querySelectorAll('tbody tr').forEach((row, index) => {
      let record = merged.get(index);
      if (!record) {
        record = { index: index, text: '', href: null };
        merged.set(index, record);
      }
      const text = (row.innerText || '').replace(/\\s+/g, ' ').trim();
      if (text) record.text = record.text ? record.text + ' ' + text : text;
      const link = row.querySelector(linkSelector);
      if (link && !record.href) record.href = link.getAttribute('href');
    });
  }
  return Array.from(merged.values());
}
"""

_ROW_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


class TmsDownloader:
    """按《在TMS系统上下载数据》文档的四个步骤驱动李宁 TMS。

    步骤一 登录；步骤二 订单管理 → 集团订单管理；步骤三 高级查找 → 选预设
    「AI导出数据（勿动）」→ 查询 → 导出 → 确定；步骤四 下载中心 → 找到本轮那一行
    → 点下载图标。每一步都只操作「可见」的那个元素：TMS 是标签页式 SPA，打开过的
    页面全部留在 DOM 里，隐藏副本和真正在用的元素长得一模一样。
    """

    # Playwright 会忽略 Locator.is_visible(timeout=...) 里的超时、退回页面默认值，
    # 于是一次探测就能把整个轮询时限吃光。所有可见性判断都走 wait_for()。
    _PROBE_TIMEOUT_MS = 1_000
    _QUICK_PROBE_MS = 250
    _PATIENT_PROBE_MS = 15_000
    _CLICK_TIMEOUT_MS = 5_000
    _ELEMENT_WAIT_SECONDS = 60
    _RETRY_DELAYS = (0, 60, 180)
    # 整点定时器每 60 分钟触发一次，第二次运行会被文件锁拒绝，所以单轮绝不能
    # 把一小时耗在重试上，否则等于悄悄吃掉下一个整点。
    _RUN_BUDGET_SECONDS = 50 * 60

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # 重试 / 预算 / 看门狗：这几层不属于「操作步骤」，是兜住浏览器异常的安全网
    # ------------------------------------------------------------------

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
                with self._attempt_watchdog():
                    return self._download_once(dataset, state=state)
            except (CredentialError, TmsAuthenticationError):
                raise
            except Exception as exc:  # browser errors are normalized at the boundary
                if isinstance(exc, TmsExportTaskNotFound):
                    # 导出点击没有变成一条可用的任务，下一次必须重新走完步骤三。
                    state.task_created = False
                    logger.warning("下载中心没有本轮可用任务，下次重试将重新执行导出")
                last_error = exc
                logger.exception("TMS 下载第 %s 次失败", attempt)
        where = f"，最后卡在「{state.last_step}」" if state.last_step else ""
        raise TmsDownloadError(
            f"TMS 下载连续 {attempts_made} 次失败{where}: {last_error}"
        ) from last_error

    @contextmanager
    def _attempt_watchdog(self) -> Iterator[None]:
        """Hard wall-clock ceiling for one attempt, independent of the browser.

        Playwright 的超时靠驱动回消息才会触发，循环里的 deadline 靠调用能返回才会
        被检查——浏览器渲染进程一卡死这两层全部失效。SIGALRM 由内核投递，是唯一
        真正兜得住的一层。Windows 没有 SIGALRM，那里退化成无保护。
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

    def _new_browser(self, playwright: object) -> tuple[object, object]:
        """Start from a clean browser unless the profile is explicitly opted into.

        持久化 profile 会把上一轮的标签页、缓存和 DOM 状态全带到下一轮，而 TMS 的
        视图状态还是账号级共享的，于是每轮的起点都不确定。每轮全新浏览器 + 重新
        登录，用几秒登录时间换一个确定的起点。
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
        """Dump a screenshot and the DOM so the next failure needs no guessing.

        截图先做，因为它能带 timeout。截图失败说明浏览器已经不响应了，这时候绝不能
        再去调 page.content()——那个 API 不接受 timeout，页面卡死时会无限期挂着。
        """
        if page is None:
            return
        stamp = self._local_now().strftime("%H%M%S")
        safe = re.sub(r"[^\w]+", "-", step).strip("-")
        base = run_dir / f"failure-{stamp}-{safe}"
        shot = base.with_suffix(".png")
        try:
            page.screenshot(path=str(shot), timeout=15_000)
            logger.error("失败现场已保存: %s", shot.name)
        except Exception:
            logger.warning("浏览器已无响应，跳过失败现场采集", exc_info=True)
            return
        dom = base.with_suffix(".html")
        try:
            dom.write_text(page.content(), encoding="utf-8")
            logger.error("失败现场已保存: %s", dom.name)
        except Exception:  # pragma: no cover - diagnostics must never mask the cause
            logger.warning("保存页面 HTML 时出错", exc_info=True)

    # ------------------------------------------------------------------
    # 一次完整的四步操作
    # ------------------------------------------------------------------

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

                step.enter("步骤一 登录")
                page.goto(self.config.tms.url, wait_until="domcontentloaded")
                self._login(page, password)

                if state.task_created:
                    logger.info(
                        "上一次尝试已经建好导出任务，直接去下载中心取件；页面总数=%s",
                        state.expected_total,
                    )
                else:
                    step.enter("步骤二 打开集团订单管理")
                    self._open_order_page(page)

                    step.enter("步骤三 高级查找并应用预设")
                    self._apply_preset(page, dataset)

                    step.enter("步骤三 等待表格加载并读取总数")
                    ui_total = self._wait_for_grid(page)
                    if ui_total and state.expected_total is None:
                        state.expected_total = ui_total

                    step.enter("步骤三 点击导出并确认")
                    # 归属判断的时间窗必须锚在「点导出」这一刻，锚在整轮开始会留出
                    # 几分钟空档，别人的导出会被误认成我们的。
                    state.not_before = self._local_now()
                    self._export(page)
                    state.task_created = True

                step.enter("步骤四 进入下载中心")
                self._open_download_center(page)

                step.enter("步骤四 等待本轮导出任务完成")
                download = self._wait_for_export_file(page, state, step)

                step.enter("步骤四 保存 Excel")
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

    # ------------------------------------------------------------------
    # 步骤一：登录
    # ------------------------------------------------------------------

    def _login(self, page: object, password: str) -> None:
        """输入用户名、密码，点击登录。

        TMS 是 SPA，DOMContentLoaded 之后登录表单还要再挂载一会儿；持久化 profile
        模式下也可能直接跳过登录页。所以同时等「用户名输入框」和「已登录的标志」，
        谁先出现听谁的。
        """
        selectors = self.config.tms.selectors
        username = page.locator(selectors.username)
        signed_in = page.locator(selectors.download_center_menu)

        deadline = time.monotonic() + self.config.tms.navigation_timeout_seconds
        form = None
        while time.monotonic() < deadline:
            form = self._first_visible(username, timeout_ms=self._QUICK_PROBE_MS)
            if form is not None:
                break
            if self._first_visible(signed_in, timeout_ms=self._QUICK_PROBE_MS):
                logger.info("会话仍然有效，跳过登录")
                return
            page.wait_for_timeout(250)
        if form is None:
            raise TmsDownloadError("登录页未加载出用户名输入框")

        form.fill(self.config.tms.username)
        self._wait_visible(page, selectors.password, "密码输入框", seconds=15).fill(
            password
        )
        self._click(
            self._wait_visible(page, selectors.login_button, "登录按钮", seconds=15),
            "登录",
        )

        deadline = time.monotonic() + self.config.tms.navigation_timeout_seconds
        errors = page.locator(".el-message--error, .el-message--warning")
        while time.monotonic() < deadline:
            if self._first_visible(signed_in, timeout_ms=self._QUICK_PROBE_MS):
                logger.info("TMS 登录成功")
                return
            failed = self._first_visible(errors, timeout_ms=self._QUICK_PROBE_MS)
            if failed is not None:
                raise TmsAuthenticationError(
                    f"TMS 拒绝登录: {self._safe_text(failed)[:120]}"
                )
            page.wait_for_timeout(250)
        raise TmsAuthenticationError("TMS 登录后未进入主页面，请检查账号或密码")

    # ------------------------------------------------------------------
    # 步骤二：订单管理 → 集团订单管理
    # ------------------------------------------------------------------

    def _open_order_page(self, page: object) -> None:
        """点击「订单管理」，再点击展开后的「集团订单管理」。

        左侧菜单里父级和子级都叫「订单管理」，所以父级只在 .el-submenu__title 里按
        整词匹配，子级只在 li.el-menu-item 里按整词匹配。菜单已经展开时不要再点父级
        ——那一下会把它收起来。
        """
        selectors = self.config.tms.selectors
        entry = page.locator(selectors.order_page_menu).get_by_text(
            "集团订单管理", exact=True
        )
        if self._first_visible(entry, timeout_ms=self._QUICK_PROBE_MS) is None:
            parent = self._wait_visible_any(
                page,
                [page.locator(selectors.order_menu).get_by_text("订单管理", exact=True)],
                "订单管理",
                seconds=30,
            )
            self._click(parent, "订单管理")
            page.wait_for_timeout(500)
        self._click(
            self._wait_visible_any(page, [entry], "集团订单管理", seconds=30),
            "集团订单管理",
        )
        self._wait_visible(page, selectors.advanced_search_button, "高级查找按钮")

    # ------------------------------------------------------------------
    # 步骤三：高级查找 → 预设 → 查询 → 导出 → 确定
    # ------------------------------------------------------------------

    def _apply_preset(self, page: object, dataset: str) -> None:
        selectors = self.config.tms.selectors
        self._click(
            self._wait_visible(page, selectors.advanced_search_button, "高级查找"),
            "高级查找",
        )
        preset = (
            self.config.tms.current_month_preset
            if dataset == "current_month"
            else self.config.tms.open_carryover_preset
        )
        if preset:
            # 预设是账号级共享且粘性的：默认选中的永远是「上一次用过的那个」，别人
            # 在浏览器里切过，我们下一轮就会继承。所以每轮都必须显式选回来。
            self._open_preset_list(page)
            self._choose_preset(page, preset)
            self._confirm_preset_applied(page, preset)
        else:
            logger.warning("未配置预设名称，直接使用页面当前的查询条件")
        # 只点「查询」，绝不点「保存」：预设里的日期条件由人工维护，程序改了会影响
        # 所有共用这个视图的人。
        self._click(
            self._wait_visible_any(
                page,
                [
                    page.locator(selectors.query_button).filter(has_text="查询"),
                    page.get_by_role("button", name="查询"),
                ],
                "查询",
            ),
            "查询",
        )

    def _open_preset_list(self, page: object) -> None:
        """点开预设下拉。触发器只按结构定位，绝不按它显示的文字。

        触发器显示的是「上一次用过的预设」——谁在这个账号上切过就变成谁的那个，
        所以它的文本是个变量，拿它做定位依据必然出错。这里只认两样结构特征：
        el-popover 的 reference 类名，以及里面那个 .show-search-list 箭头图标。

        下拉已经开着时不要再点触发器——那一下会把它关掉。
        """
        selectors = self.config.tms.selectors
        items = page.locator(selectors.preset_item)
        if self._first_visible(items, timeout_ms=self._QUICK_PROBE_MS) is not None:
            return
        trigger = self._wait_visible(page, selectors.preset_trigger, "预设模板选择")
        self._click(trigger, "预设模板选择")
        self._wait_visible_any(page, [items], "预设下拉列表", seconds=15)

    def _choose_preset(self, page: object, preset: str) -> None:
        """在下拉框里选中指定预设，按整词匹配。

        只在可见的 .search-item 里逐项比对：列表里有「正向」和「上海正向」这种互为
        子串的名字，子串匹配会选错；Element UI 的 popover 又可能在 DOM 里留下隐藏
        副本，读到隐藏那份就会点了个点不着的元素。
        """
        wanted = self._normalize(preset)
        items = page.locator(self.config.tms.selectors.preset_item)
        deadline = time.monotonic() + self._ELEMENT_WAIT_SECONDS
        labels: list[str] = []
        while time.monotonic() < deadline:
            labels = []
            try:
                total = items.count()
            except Exception:
                total = 0
            for index in range(total):
                item = items.nth(index)
                if not self._visible_now(item, timeout_ms=self._QUICK_PROBE_MS):
                    continue
                text = self._safe_text(item)
                if not text:
                    continue
                labels.append(text)
                if self._normalize(text) == wanted:
                    self._click(item, f"预设「{preset}」")
                    return
            page.wait_for_timeout(250)
        available = "、".join(labels[:30]) if labels else "（没读到任何选项）"
        raise TmsDownloadError(f"高级查找里没有预设「{preset}」，当前可选: {available}")

    def _confirm_preset_applied(self, page: object, preset: str) -> None:
        """选完回读触发器，确认它确实变成了目标预设。

        这里读文字是「事后核对」，不是「按文字定位」——正因为触发器显示的就是当前
        生效的那个预设，它是唯一能证明这一下真的选中了的信号。

        必须有这道检查：2026-08-17 17:54 那轮就是没选中却一路跑到底，带着继承来的
        12644 行视图（正常 4750）导出、算规则、发飞书，全程没有任何一处报错。
        """
        wanted = self._normalize(preset)
        trigger = page.locator(self.config.tms.selectors.preset_trigger)
        deadline = time.monotonic() + 10
        current = ""
        while time.monotonic() < deadline:
            visible = self._first_visible(trigger, timeout_ms=self._QUICK_PROBE_MS)
            if visible is not None:
                current = self._safe_text(visible)
                if self._normalize(current) == wanted:
                    logger.info("预设已切换到「%s」", preset)
                    return
            page.wait_for_timeout(250)
        if not current:
            # 读不到就别凭空造一个失败点，后面还有行数合理性检查兜着。
            logger.warning("读不到预设标题，跳过核对，继续查询")
            return
        raise TmsDownloadError(
            f"预设没有切换成功：标题仍然是「{current}」，期望「{preset}」。"
            "继续下去会导出别人留下的视图，本次中止。"
        )

    def _wait_for_grid(self, page: object) -> int | None:
        """等「拼命加载中」遮罩散掉，并尽力读出左下角的「共 N 条」。

        总数只是给 Excel 行数校验用的参考值，读不到不影响导出，记一条日志继续；
        但如果确确实实读到了 0，那就是在空表格上点导出——TMS 不会建任何任务，等于
        白烧掉一整次尝试，这种情况直接失败更快。
        """
        deadline = time.monotonic() + self.config.tms.grid_load_timeout_seconds
        self._wait_out_loading_mask(page, deadline)
        if not self.config.tms.selectors.total_count:
            return None
        total: int | None = None
        while time.monotonic() < deadline:
            total = self._read_total(page)
            if total:
                logger.info("集团订单管理页面总数: 共 %s 条", total)
                return total
            page.wait_for_timeout(500)
        if total == 0:
            raise TmsDownloadError(
                f"筛选后 {self.config.tms.grid_load_timeout_seconds} 秒内页面仍显示"
                "共 0 条，本次不执行导出"
            )
        logger.warning("未能读到页面总数，跳过总数校验继续导出")
        return None

    def _wait_out_loading_mask(self, page: object, deadline: float) -> None:
        """Wait out the "拼命加载中" mask that TMS shows while the grid loads.

        networkidle 在 TMS 上几乎必然超时（后台请求就没停过）。Element UI 的加载
        遮罩才是准确信号：它出现代表查询发出去了，它消失代表表格渲染完了。
        """
        mask = page.locator(".el-loading-mask")
        if not self._visible_now(mask.first, timeout_ms=5_000):
            logger.info("未捕获到加载遮罩，直接按超时等待表格")
            return
        while time.monotonic() < deadline:
            if not self._any_visible(mask):
                return
            page.wait_for_timeout(500)
        logger.warning("加载遮罩未在限定时间内消失，继续尝试读取表格")

    def _read_total(self, page: object) -> int | None:
        """Read the order count from the *visible* pagination control.

        TMS 是标签页式 SPA，打开过的页面全都留在 DOM 里，非活动的只是隐藏。
        button.pagination-total 会同时匹配到集团订单管理的「共 4753 条」和下载中心的
        「共 34920 条」，硬取 .first 等于赌 DOM 顺序。只认可见的那个。
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
            match = re.search(r"([\d,]+)", self._safe_text(candidate))
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    def _export(self, page: object) -> None:
        selectors = self.config.tms.selectors
        self._click(
            self._wait_visible(page, selectors.export_button, "导出"),
            "导出",
        )
        logger.info("已点击导出，等待确认窗口")
        self._click(
            self._wait_visible_any(
                page, [page.get_by_role("button", name="确定")], "确定"
            ),
            "确定",
        )
        toast = page.get_by_text("下载任务添加成功", exact=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._any_visible(toast):
                logger.info("导出任务已创建")
                return
            page.wait_for_timeout(250)
        # 成功提示是短暂 toast，页面慢时可能在定位前就消失了。下载中心才是最终依据。
        logger.info("未捕获到导出成功提示，改由下载中心核验")

    # ------------------------------------------------------------------
    # 步骤四：下载中心 → 找到本轮那一行 → 点下载图标
    # ------------------------------------------------------------------

    def _open_download_center(self, page: object) -> None:
        """点右上角的「下载中心」，等它的表格真的出来。

        入口的文本节点前后带大量空白，get_by_text 经常匹配不上；图标类名
        thorn6-icon-xiazai 唯一且稳定，所以选择器优先、文本兜底。
        """
        selectors = self.config.tms.selectors
        rows = page.locator("tbody tr")
        for attempt in range(1, 3):
            menu = self._wait_visible_any(
                page,
                [
                    page.locator(selectors.download_center_menu),
                    page.get_by_text("下载中心", exact=True),
                ],
                "下载中心",
                seconds=self._ELEMENT_WAIT_SECONDS,
            )
            self._click(menu, "下载中心")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._first_visible(rows, timeout_ms=self._QUICK_PROBE_MS):
                    return
                page.wait_for_timeout(500)
            logger.warning("第 %s 次进入下载中心后表格没出来，重试", attempt)
        raise TmsDownloadError("进入下载中心后始终没有加载出任务列表")

    def _wait_for_export_file(
        self, page: object, state: _ExportState, step: _StepTracker
    ) -> object:
        """轮询下载中心，锁定本轮那一行并点下载图标。

        同名任务页面上会有很多（别人也在导），归属判断按文档的做法：功能名匹配 +
        开始时间不早于我们点「导出」的时刻 + 状态成功，取其中最新的一条。行里的
        时间只精确到分钟，所以时间窗往前放宽一分钟。
        """
        tms = self.config.tms
        wait_seconds = float(tms.download_timeout_seconds)
        budget = self._budget_remaining(state)
        if budget is not None:
            wait_seconds = min(wait_seconds, max(budget, 60.0))
        deadline = time.monotonic() + wait_seconds
        appear_deadline = min(deadline, time.monotonic() + tms.export_task_appear_minutes * 60)
        earliest = state.not_before - timedelta(minutes=1)
        logger.info(
            "在下载中心等待「%s」且开始时间不早于 %s 的成功任务",
            tms.export_task_keyword,
            earliest,
        )

        task_seen = False
        missing_link_logged = False
        last_snapshot: tuple[object, ...] | None = None
        while True:
            mine = [
                task
                for task in self._collect_tasks(page)
                if task.started_at is not None and task.started_at >= earliest
            ]
            mine.sort(key=lambda task: task.started_at, reverse=True)
            if mine:
                task_seen = True
                newest = mine[0]
                snapshot = (
                    newest.started_at,
                    newest.record_count,
                    newest.succeeded,
                    newest.failed,
                )
                if snapshot != last_snapshot:
                    logger.info(
                        "下载中心本轮最新任务: 开始时间=%s 记录数=%s 成功=%s 失败=%s"
                        "（页面总数=%s）",
                        newest.started_at,
                        newest.record_count,
                        newest.succeeded,
                        newest.failed,
                        state.expected_total,
                    )
                    last_snapshot = snapshot
                ready = [task for task in mine if task.succeeded and task.href]
                if ready:
                    step.enter("步骤四 下载 Excel")
                    return self._click_download_link(page, ready[0], deadline)
                if any(task.failed for task in mine) and not any(
                    task.succeeded for task in mine
                ):
                    raise TmsExportTaskNotFound(
                        f"下载中心本轮任务状态为失败: {mine[0].text[:200]}"
                    )
                if not missing_link_logged and any(
                    task.succeeded and not task.href for task in mine
                ):
                    logger.warning("任务已成功但还没渲染出下载图标，继续刷新")
                    missing_link_logged = True

            now = time.monotonic()
            if not task_seen and now >= appear_deadline:
                raise TmsExportTaskNotFound(
                    f"点击导出后 {tms.export_task_appear_minutes} 分钟内，"
                    "下载中心没有出现本轮任务"
                )
            if now >= deadline:
                break
            self._refresh_download_center(page)
            page.wait_for_timeout(3_000)
        raise TmsDownloadError("下载中心任务在超时时间内未完成")

    def _collect_tasks(self, page: object) -> list[_DownloadTask]:
        selectors = self.config.tms.selectors
        try:
            rows = page.evaluate(_SCRAPE_DOWNLOAD_ROWS, selectors.download_link)
        except Exception:
            logger.warning("读取下载中心表格失败，稍后重试", exc_info=True)
            return []
        keyword = self.config.tms.export_task_keyword
        tasks: list[_DownloadTask] = []
        for row in rows or []:
            text = (row or {}).get("text") or ""
            if keyword not in text:
                continue
            started_at, record_count = self._parse_download_row(text)
            tasks.append(
                _DownloadTask(
                    started_at=started_at,
                    record_count=record_count,
                    succeeded="成功" in text,
                    failed="失败" in text,
                    href=row.get("href"),
                    text=text,
                )
            )
        return tasks

    def _click_download_link(
        self, page: object, task: _DownloadTask, deadline: float
    ) -> object:
        selector = (
            f"{self.config.tms.selectors.download_link}"
            f'[href="{self._escape_attribute(task.href or "")}"]'
        )
        matches = page.locator(selector)
        link = self._first_visible(matches, timeout_ms=self._QUICK_PROBE_MS)
        if link is None:
            link = matches.first
        remaining_ms = max(5, int(deadline - time.monotonic())) * 1_000
        logger.info(
            "开始下载本轮任务: 开始时间=%s 记录数=%s", task.started_at, task.record_count
        )
        with page.expect_download(timeout=remaining_ms) as info:
            self._click(link, "下载图标")
        return info.value

    def _refresh_download_center(self, page: object) -> None:
        selector = self.config.tms.selectors.download_center_refresh
        if not selector:
            return
        button = self._first_visible(
            page.locator(selector), timeout_ms=self._QUICK_PROBE_MS
        )
        if button is None:
            return
        try:
            self._click(button, "刷新")
        except Exception:  # pragma: no cover - refreshing is best effort
            logger.debug("刷新下载中心失败", exc_info=True)

    @staticmethod
    def _parse_download_row(text: str) -> tuple[datetime | None, int | None]:
        """列顺序是 功能 / 状态 / 开始时间 / 结束时间 / 导出记录数 / 文件大小。"""
        flat = re.sub(r"\s+", " ", text).strip()
        stamps = _ROW_TIMESTAMP.findall(flat)
        if not stamps:
            return None, None
        started_at = datetime.strptime(stamps[0], "%Y-%m-%d %H:%M")
        tail = flat[flat.rindex(stamps[-1]) + len(stamps[-1]) :]
        numbers = re.findall(r"\d+", tail)
        return started_at, int(numbers[0]) if numbers else None

    # ------------------------------------------------------------------
    # 通用元素操作
    # ------------------------------------------------------------------

    def _local_now(self) -> datetime:
        # TMS 下载中心显示的是配置时区的无时区时间，统一成同口径后再比较。
        return datetime.now(ZoneInfo(self.config.runtime.timezone)).replace(tzinfo=None)

    @classmethod
    def _visible_now(cls, locator: object, *, timeout_ms: int | None = None) -> bool:
        """Bounded visibility probe.

        ``Locator.is_visible(timeout=...)`` 会忽略传入的超时、退回页面默认值，于是
        一次探测就能把整个轮询时限吃光。``wait_for`` 会遵守超时，探测不到就当作
        「还不可见」。
        """
        try:
            locator.wait_for(
                state="visible", timeout=timeout_ms or cls._PROBE_TIMEOUT_MS
            )
        except Exception:
            return False
        return True

    @classmethod
    def _first_visible(cls, matches: object, *, timeout_ms: int | None = None) -> object | None:
        """Return the visible copy when the SPA keeps hidden duplicates around."""
        try:
            total = matches.count()
        except Exception:
            return None
        for index in range(total):
            candidate = matches.nth(index)
            if cls._visible_now(candidate, timeout_ms=timeout_ms):
                return candidate
        return None

    @classmethod
    def _any_visible(cls, matches: object) -> bool:
        return cls._first_visible(matches) is not None

    def _wait_visible(
        self, page: object, selector: str, name: str, *, seconds: float | None = None
    ) -> object:
        return self._wait_visible_any(page, [page.locator(selector)], name, seconds=seconds)

    @classmethod
    def _wait_visible_any(
        cls, page: object, collections: list, name: str, *, seconds: float | None = None
    ) -> object:
        """Two-phase element hunt for a SPA that renders duplicate, slow toolbars.

        阶段一用很短的探测快速扫一遍所有候选，挑出已经可见的那个——TMS 会把同一个
        工具栏渲染多份，只有一份可见。阶段二在一整轮都没扫到时，对第一个候选做一次
        长等待：元素只是还没渲染出来时，继续用短探测轮询纯属空转。
        """
        deadline = time.monotonic() + (seconds or cls._ELEMENT_WAIT_SECONDS)
        while True:
            for matches in collections:
                found = cls._first_visible(matches, timeout_ms=cls._QUICK_PROBE_MS)
                if found is not None:
                    return found
            remaining_ms = int((deadline - time.monotonic()) * 1_000)
            if remaining_ms <= 0:
                break
            patient = next(
                (matches.nth(0) for matches in collections if matches.count()), None
            )
            if patient is not None and cls._visible_now(
                patient, timeout_ms=min(cls._PATIENT_PROBE_MS, remaining_ms)
            ):
                return patient
            page.wait_for_timeout(250)
        raise TmsDownloadError(f"页面未找到可见元素: {name}")

    @classmethod
    def _click(cls, locator: object, name: str) -> None:
        """Real click first, DOM click as the fallback.

        真实点击能触发挂在任何一层上的处理器；但 TMS 的成功提示层会盖住菜单、
        Element UI 的工具栏又会反复重渲染，这时 Playwright 会一直重试到超时。
        所以失败后退回 DOM click——元素已经确认可见，覆盖层拦不住它。
        """
        try:
            locator.scroll_into_view_if_needed(timeout=cls._CLICK_TIMEOUT_MS)
        except Exception:
            logger.debug("「%s」无法滚动到可视区域，直接尝试点击", name)
        try:
            locator.click(timeout=cls._CLICK_TIMEOUT_MS)
            return
        except Exception:
            logger.info("「%s」常规点击未成功，改用 DOM click", name)
        try:
            locator.evaluate("element => element.click()")
        except Exception as exc:
            raise TmsDownloadError(f"点击「{name}」失败: {exc}") from exc

    @classmethod
    def _safe_text(cls, locator: object) -> str:
        """Read text without letting a not-yet-mounted node block for the page default."""
        try:
            return (locator.inner_text(timeout=cls._PROBE_TIMEOUT_MS) or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text)

    @staticmethod
    def _escape_attribute(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
