from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from runbow007.credentials import CredentialError
from runbow007.downloader import (
    DownloadResult,
    TmsAuthenticationError,
    TmsDownloader,
    TmsDownloadError,
    TmsExportTaskNotFound,
    _ExportState,
)


class Probeable:
    """Fake locator whose visibility is probed through ``wait_for``.

    Playwright ignores ``is_visible(timeout=...)``, so the downloader probes with
    ``wait_for(state="visible", timeout=...)`` and treats a timeout as "not visible".
    """

    def __init__(self, visible: bool) -> None:
        self.visible = visible
        self.probes: list[int] = []

    def wait_for(self, *, state, timeout):
        assert state == "visible"
        self.probes.append(timeout)
        if not self.visible:
            raise TimeoutError("locator not visible")


def test_parse_download_center_row():
    text = """
    maintainCompanyOrderPage
    成功
    2026-08-06 10:03
    2026-08-06 10:03
    1211
    2665
    """

    started_at, record_count = TmsDownloader._parse_download_row(text)

    assert started_at == datetime(2026, 8, 6, 10, 3)
    assert record_count == 1211


def test_parse_incomplete_download_center_row():
    started_at, record_count = TmsDownloader._parse_download_row(
        "maintainCompanyOrderPage 处理中"
    )

    assert started_at is None
    assert record_count is None


def test_downloader_uses_configured_timezone():
    config = SimpleNamespace(runtime=SimpleNamespace(timezone="Asia/Shanghai"))

    now = TmsDownloader(config)._local_now()

    assert now.tzinfo is None
    expected = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    assert abs((expected - now).total_seconds()) < 5


def test_download_retries_then_succeeds(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)
    expected = DownloadResult(Path("orders.xls"), 10, "current_month")
    outcomes = [RuntimeError("temporary"), RuntimeError("temporary"), expected]
    sleeps = []

    attempts = []

    def attempt(dataset, *, state):
        attempts.append((state, state.expected_total, state.task_created))
        if len(attempts) == 1:
            state.expected_total = 4177
            state.task_created = True
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(downloader, "_download_once", attempt)
    monkeypatch.setattr("runbow007.downloader.time.sleep", sleeps.append)

    assert downloader.download() == expected
    assert sleeps == [60, 180]
    assert attempts[0][0] is attempts[1][0] is attempts[2][0]
    assert attempts[1][1:] == attempts[2][1:] == (4177, True)


def test_download_reexports_when_created_task_never_appears(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)
    expected = DownloadResult(Path("orders.xls"), 4220, "current_month")
    attempts = []
    sleeps = []

    def attempt(dataset, *, state):
        attempts.append((state.expected_total, state.task_created))
        if len(attempts) == 1:
            state.expected_total = 4220
            state.task_created = True
            raise TmsExportTaskNotFound("no matching task")
        return expected

    monkeypatch.setattr(downloader, "_download_once", attempt)
    monkeypatch.setattr("runbow007.downloader.time.sleep", sleeps.append)

    assert downloader.download() == expected
    assert attempts == [(None, False), (4220, False)]
    assert sleeps == [60]


def test_download_gives_up_when_run_budget_is_spent(app_config, monkeypatch):
    """单轮不能把整个小时耗光，否则下一个整点会被文件锁静默吃掉。"""
    downloader = TmsDownloader(app_config)
    attempts = []
    sleeps = []

    def attempt(dataset, *, state):
        attempts.append(state.task_created)
        raise RuntimeError("temporary")

    # 第 1 次失败后预算只剩 10 秒，不足以再等 60 秒退避。
    timeline = iter((0, TmsDownloader._RUN_BUDGET_SECONDS - 10))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))
    monkeypatch.setattr("runbow007.downloader.time.sleep", sleeps.append)
    monkeypatch.setattr(downloader, "_download_once", attempt)

    with pytest.raises(TmsDownloadError, match="连续 1 次失败: temporary"):
        downloader.download()

    assert len(attempts) == 1
    assert sleeps == []


class _MenuPage:
    """右上角全局菜单里的「下载中心」，按选择器/文本两种方式提供。"""

    def __init__(self, *, by_selector=None, by_text=None):
        self.by_selector = by_selector
        self.by_text = by_text
        self.gotos = []

    @staticmethod
    def _collection(item):
        items = [item] if item is not None else []
        return SimpleNamespace(
            count=lambda: len(items), nth=lambda index: items[index]
        )

    def locator(self, selector):
        assert "thorn6-icon-xiazai" in selector
        return self._collection(self.by_selector)

    def get_by_text(self, text, *, exact):
        assert (text, exact) == ("下载中心", True)
        return self._collection(self.by_text)

    def goto(self, url, *, wait_until):
        self.gotos.append(url)

    def wait_for_timeout(self, milliseconds):
        pass


def test_download_center_prefers_the_icon_selector_over_text(app_config, monkeypatch):
    """真实 DOM：<li class="menu-item"><i class="thorn6-icon-xiazai"></i> 下载中心 </li>

    图标类名唯一且稳定；文本节点前后带大量空白，文本匹配今天在 10:05 和 14:05
    第 1 次都没找到它，各白烧掉一整次尝试。
    """
    clicks = []

    class Menu(Probeable):
        def evaluate(self, expression):
            clicks.append(expression)

    page = _MenuPage(by_selector=Menu(True))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: 0)

    TmsDownloader(app_config)._open_download_center(page)

    assert clicks == ["element => element.click()"]
    assert page.gotos == []


def test_download_center_reloads_home_when_the_menu_is_missing(app_config, monkeypatch):
    """找不到入口就重载首页重来。

    导出任务此时已在服务端建好，页面状态不再重要；14:05 第 2 次正是靠重开浏览器
    后 10 秒就找到了，重载比放弃整次尝试便宜得多。
    """
    clicks = []

    class Menu(Probeable):
        def evaluate(self, expression):
            clicks.append(expression)

    appears = Menu(True)
    page = _MenuPage()
    timeline = iter((0, TmsDownloader._ELEMENT_WAIT_SECONDS + 1, 0, 0))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))

    def reload_makes_menu_appear(url, *, wait_until):
        page.gotos.append(url)
        page.by_selector = appears

    page.goto = reload_makes_menu_appear

    TmsDownloader(app_config)._open_download_center(page)

    assert page.gotos == [app_config.tms.url]
    assert clicks == ["element => element.click()"]


def test_download_center_gives_up_after_one_reload(app_config, monkeypatch):
    page = _MenuPage()
    wait = TmsDownloader._ELEMENT_WAIT_SECONDS + 1
    timeline = iter((0, wait, 0, wait))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))

    with pytest.raises(TmsDownloadError, match="页面未找到可见元素: 下载中心"):
        TmsDownloader(app_config)._open_download_center(page)

    assert page.gotos == [app_config.tms.url]


def test_download_center_window_is_clamped_to_remaining_budget(app_config, monkeypatch):
    class EmptyRows:
        def filter(self, *, has_text):
            return self

        def count(self):
            return 0

    class Refresh:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

    class FakePage:
        def locator(self, selector):
            return EmptyRows() if selector == "tr" else Refresh()

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 2_000

    # 预算只剩 60 秒时，等待窗口从默认的 5 分钟收缩到 60 秒。
    timeline = iter((0, 0, 0, 61))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))
    downloader = TmsDownloader(app_config)
    monkeypatch.setattr(downloader, "_open_download_center", lambda page: None)

    with pytest.raises(TmsExportTaskNotFound):
        downloader._download_from_center(
            FakePage(), datetime(2026, 8, 14, 14, 51), 4220, budget_seconds=60
        )


@pytest.mark.parametrize(
    ("record_count", "expected"),
    [(4672, True), (4677, True), (4662, True), (4683, False), (12563, False)],
)
def test_export_task_total_allows_bounded_drift(app_config, record_count, expected):
    """订单数一小时内会自然增减，严格相等会让本轮任务永远匹配不上。"""
    app_config.tms.total_tolerance = 10

    assert TmsDownloader(app_config)._total_matches(record_count, 4672) is expected


def test_export_task_total_still_requires_a_count(app_config):
    downloader = TmsDownloader(app_config)

    # 页面总数已知但任务还没出条数（处理中）：不能当成本轮任务。
    assert downloader._total_matches(None, 4672) is False
    assert downloader._total_matches(None, None) is True


def test_export_task_total_skips_filtering_when_page_total_unknown(app_config):
    """TMS 慢时 _read_total 会返回 0，expected_total 保持 None。

    这种情况下必须放行、只靠时间窗判断归属；一旦改成拒绝，本轮任务永远匹配不上，
    5 分钟后重新点导出，最终整轮失败。
    """
    downloader = TmsDownloader(app_config)

    assert downloader._total_matches(4672, None) is True
    assert downloader._total_matches(0, None) is True


@pytest.mark.parametrize("error", [CredentialError("missing"), TmsAuthenticationError("bad")])
def test_download_does_not_retry_authentication_errors(app_config, monkeypatch, error):
    downloader = TmsDownloader(app_config)
    calls = 0

    def fail(dataset, *, state):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(downloader, "_download_once", fail)
    with pytest.raises(type(error)):
        downloader.download()
    assert calls == 1


def test_download_normalizes_three_browser_failures(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)
    monkeypatch.setattr(
        downloader,
        "_download_once",
        lambda dataset, *, state: (_ for _ in ()).throw(OSError("browser")),
    )
    monkeypatch.setattr("runbow007.downloader.time.sleep", lambda seconds: None)

    with pytest.raises(TmsDownloadError, match="连续 3 次失败: browser"):
        downloader.download("open_carryover")


def test_download_once_drives_browser_and_saves_file(app_config, monkeypatch):
    export_clicks = []

    class FakePage:
        def set_default_timeout(self, timeout):
            assert timeout == app_config.tms.navigation_timeout_seconds * 1000

        def goto(self, url, *, wait_until):
            assert (url, wait_until) == (app_config.tms.url, "domcontentloaded")

    class FakeDownload:
        suggested_filename = "orders.xlsx"

        def save_as(self, target):
            Path(target).write_bytes(b"valid workbook bytes")

    page = FakePage()

    class FakeContext:
        closed = False

        def new_page(self):
            return page

        def close(self):
            self.closed = True

    context = FakeContext()

    class FakeBrowser:
        closed = False

        def new_context(self, **kwargs):
            assert kwargs == {"accept_downloads": True}
            return context

        def close(self):
            self.closed = True

    browser = FakeBrowser()

    class FakeChromium:
        def launch_persistent_context(self, profile, **kwargs):
            raise AssertionError("默认必须是全新浏览器，不能复用持久化 profile")

        def launch(self, **kwargs):
            assert kwargs == {"headless": True}
            return browser

    fake_playwright = SimpleNamespace(chromium=FakeChromium())

    class FakeManager:
        def __enter__(self):
            return fake_playwright

        def __exit__(self, *args):
            return False

    downloader = TmsDownloader(app_config)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeManager())
    monkeypatch.setattr("runbow007.downloader.get_tms_password", lambda username: "password")
    monkeypatch.setattr(downloader, "_login_if_needed", lambda page, password: None)
    monkeypatch.setattr(downloader, "_open_order_page", lambda page: None)
    monkeypatch.setattr(downloader, "_apply_filters", lambda page, dataset: None)
    monkeypatch.setattr(downloader, "_read_total", lambda page: 42)
    monkeypatch.setattr(
        downloader,
        "_visible_locator_or_button",
        lambda page, selector, pattern: SimpleNamespace(
            wait_for=lambda **kwargs: export_clicks.append(("wait_for", kwargs)),
            is_enabled=lambda **kwargs: export_clicks.append(("is_enabled", kwargs))
            or True,
            evaluate=lambda expression: export_clicks.append(("evaluate", expression)),
        ),
    )
    monkeypatch.setattr(downloader, "_confirm_export", lambda page: None)
    monkeypatch.setattr(
        downloader,
        "_download_from_center",
        lambda page, export_started, ui_total, *, budget_seconds=None, step=None: (
            FakeDownload()
        ),
    )

    state = _ExportState(not_before=datetime(2026, 8, 14, 10, 8))
    result = downloader._download_once("current_month", state=state)

    assert result.ui_total == 42
    assert result.dataset == "current_month"
    assert result.path.suffix == ".xlsx"
    assert result.path.read_bytes() == b"valid workbook bytes"
    assert context.closed is True
    assert state.expected_total == 42
    assert state.task_created is True
    # 点击导出前重新锚定下载中心的时间窗，避免把点击之前的任务当成本轮产物。
    assert state.not_before > datetime(2026, 8, 14, 10, 8)
    # 调用方已确认可见性，_dom_click 不再重复 wait_for。
    assert export_clicks == [
        ("is_enabled", {"timeout": 5_000}),
        ("evaluate", "element => element.click()"),
    ]


def test_download_once_reuses_created_export_task(app_config, monkeypatch):
    class FakePage:
        def set_default_timeout(self, timeout):
            assert timeout == app_config.tms.navigation_timeout_seconds * 1000

        def goto(self, url, *, wait_until):
            assert (url, wait_until) == (app_config.tms.url, "domcontentloaded")

    class FakeDownload:
        suggested_filename = "orders.xls"

        def save_as(self, target):
            Path(target).write_bytes(b"reused workbook")

    page = FakePage()
    context = SimpleNamespace(new_page=lambda: page, close=lambda: None)
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)
    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=lambda **kwargs: browser)
    )

    class FakeManager:
        def __enter__(self):
            return fake_playwright

        def __exit__(self, *args):
            return False

    def unexpected(*args, **kwargs):
        raise AssertionError("existing export task must be reused")

    downloader = TmsDownloader(app_config)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeManager())
    monkeypatch.setattr("runbow007.downloader.get_tms_password", lambda username: "password")
    monkeypatch.setattr(downloader, "_login_if_needed", lambda page, password: None)
    monkeypatch.setattr(downloader, "_open_order_page", unexpected)
    monkeypatch.setattr(downloader, "_apply_filters", unexpected)
    monkeypatch.setattr(downloader, "_read_total", unexpected)
    monkeypatch.setattr(downloader, "_locator_or_button", unexpected)
    monkeypatch.setattr(downloader, "_confirm_export", unexpected)

    center_calls = []

    def download_from_center(
        page, export_started, expected_total, *, budget_seconds=None, step=None
    ):
        center_calls.append((page, export_started, expected_total))
        return FakeDownload()

    monkeypatch.setattr(downloader, "_download_from_center", download_from_center)
    state = _ExportState(
        not_before=datetime(2026, 8, 14, 10, 8),
        expected_total=4177,
        task_created=True,
    )

    result = downloader._download_once("current_month", state=state)

    assert result.ui_total == 4177
    assert result.path.read_bytes() == b"reused workbook"
    assert center_calls == [(page, state.not_before, 4177)]


class _GridCell(Probeable):
    def __init__(self, text, *, visible=True):
        super().__init__(visible)
        self.text = text

    def inner_text(self, **kwargs):
        return self.text


class _GridPage:
    """筛选后分页组件先渲染出来显示 0，过一会儿才回填真实条数。

    每次 locator() 返回一批候选，模拟 TMS 标签页式 SPA 里同时存在的多个分页控件。
    """

    def __init__(self, batches):
        self.batches = [
            [c if isinstance(c, _GridCell) else _GridCell(c) for c in batch]
            if isinstance(batch, list)
            else [_GridCell(batch)]
            for batch in batches
        ]
        self.waits = 0

    def locator(self, selector):
        cells = self.batches.pop(0) if self.batches else []
        return SimpleNamespace(
            count=lambda: len(cells), nth=lambda index: cells[index]
        )

    def wait_for_timeout(self, milliseconds):
        self.waits += 1


def test_waits_for_grid_to_report_rows_before_exporting(app_config, monkeypatch):
    """读到 0 说明表格还没出数据，必须等到真实条数再点导出。"""
    page = _GridPage(["共 0 条", "共 0 条", "共 4,750 条"])
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: 0)

    total = TmsDownloader(app_config)._wait_for_orders_loaded(page)

    assert total == 4750
    assert page.waits == 2


def test_waits_out_the_loading_mask(app_config, monkeypatch):
    """TMS 点查询后会盖一层「拼命加载中」；它消失才代表表格渲染完。

    networkidle 在 TMS 上几乎必然超时（后台请求就没停过），之前只能死等 2 秒。
    """
    states = [True, True, False]

    class Mask(Probeable):
        def __init__(self):
            super().__init__(True)

        def wait_for(self, *, state, timeout):
            assert state == "visible"
            if not states.pop(0):
                raise TimeoutError("mask gone")

    mask = Mask()

    class FakePage:
        def __init__(self):
            self.waits = 0

        def locator(self, selector):
            assert selector == ".el-loading-mask"
            return SimpleNamespace(
                first=mask, count=lambda: 1, nth=lambda index: mask
            )

        def wait_for_timeout(self, milliseconds):
            self.waits += 1

    page = FakePage()
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: 0)

    TmsDownloader(app_config)._wait_for_grid_loading(page)

    # 遮罩出现 → 还在 → 消失后返回，中间退避一次。
    assert page.waits == 1


def test_refuses_to_export_against_an_empty_grid(app_config, monkeypatch):
    """等不到数据就快速失败。

    带着 0 往下走会在空表格上点导出，TMS 不建任务，之后在下载中心白等 8 分钟
    ——2026-08-17 15:05 那轮就是这样烧掉 9 分钟的。
    """
    page = _GridPage(["共 0 条"] * 3)
    wait = app_config.tms.grid_load_timeout_seconds
    timeline = iter((0, 0, wait + 1))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))

    with pytest.raises(TmsDownloadError, match="订单总数仍为 0"):
        TmsDownloader(app_config)._wait_for_orders_loaded(page)


def test_missing_pagination_element_does_not_block_for_the_page_default(app_config):
    """分页组件没挂载时 inner_text 不能硬等 45 秒——那会搞挂整次尝试。"""

    class Missing:
        first = None

        def __init__(self):
            self.first = self

        def inner_text(self, **kwargs):
            assert kwargs == {"timeout": TmsDownloader._PROBE_TIMEOUT_MS}
            raise TimeoutError("locator not attached")

    page = SimpleNamespace(locator=lambda selector: Missing())

    assert TmsDownloader(app_config)._read_total(page) is None


def test_read_total_and_locator_fallback(app_config):
    class FakePage:
        def locator(self, selector):
            cells = [_GridCell("共 1,211 条")]
            return SimpleNamespace(
                count=lambda: len(cells), nth=lambda index: cells[index]
            )

        def get_by_role(self, role, *, name):
            assert role == "button"
            return SimpleNamespace(last="fallback-button")

    downloader = TmsDownloader(app_config)
    assert downloader._read_total(FakePage()) == 1211
    app_config.tms.selectors.total_count = ""
    assert downloader._read_total(FakePage()) is None
    assert downloader._locator_or_button(FakePage(), "", r"下载") == "fallback-button"


def test_read_total_ignores_the_hidden_tab_pagination(app_config):
    """TMS 是标签页式 SPA，button.pagination-total 会同时匹配到多个。

    2026-08-17 实测：集团订单管理「共 4753 条」和下载中心「共 34920 条」同时在
    DOM 里。硬取 .first 等于赌 DOM 顺序，赌输了就一直读另一个标签页的数字。
    """
    hidden = _GridCell("共 34920 条", visible=False)
    active = _GridCell("共 4,753 条")

    class FakePage:
        def locator(self, selector):
            cells = [hidden, active]
            return SimpleNamespace(
                count=lambda: len(cells), nth=lambda index: cells[index]
            )

    assert TmsDownloader(app_config)._read_total(FakePage()) == 4753


def test_visible_export_button_skips_hidden_duplicate(app_config, monkeypatch):
    class Matches:
        def __init__(self, candidates):
            self.candidates = candidates

        def count(self):
            return len(self.candidates)

        def nth(self, index):
            return self.candidates[index]

    hidden = Probeable(False)
    visible = Probeable(True)

    class FakePage:
        def locator(self, selector):
            assert "thorn6-icon-daochu" in selector
            return Matches([hidden, visible])

        def get_by_role(self, role, *, name):
            return Matches([])

        def wait_for_timeout(self, milliseconds):
            raise AssertionError("visible configured match should return immediately")

    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: 0)

    result = TmsDownloader(app_config)._visible_locator_or_button(
        FakePage(), app_config.tms.selectors.download_button, r"下载|批量导出"
    )

    assert result is visible
    # 阶段一是快扫：探测必须有界，且短到能在时限内扫完所有候选。
    assert hidden.probes == [TmsDownloader._QUICK_PROBE_MS]


def test_export_button_waits_patiently_when_nothing_is_visible_yet(
    app_config, monkeypatch
):
    """快扫一轮全不可见时，要对首个候选长等一次，而不是继续短探测空转。

    TMS 慢的时候元素只是还没渲染出来。8/17 白天 08:05/10:05 两轮就是死在"每个候选
    各等 250ms、轮着来"——页面卡住时一轮就把时限耗光，实际只扫了一两轮。
    """

    class Slow:
        """第一次快扫时不可见，只有拿到长超时才会"渲染出来"。"""

        def __init__(self):
            self.probes = []

        def wait_for(self, *, state, timeout):
            assert state == "visible"
            self.probes.append(timeout)
            if timeout < TmsDownloader._PATIENT_PROBE_MS:
                raise TimeoutError("still rendering")

    slow = Slow()

    class Matches:
        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return slow

    class FakePage:
        def locator(self, selector):
            return Matches()

        def get_by_role(self, role, *, name):
            return Matches()

        def wait_for_timeout(self, milliseconds):
            raise AssertionError("耐心等待命中后不应再退避")

    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: 0)

    result = TmsDownloader(app_config)._visible_locator_or_button(
        FakePage(), app_config.tms.selectors.download_button, r"下载|批量导出"
    )

    assert result is slow
    # 两个 collection 各快扫一次，然后对首个候选长等一次。
    assert slow.probes == [
        TmsDownloader._QUICK_PROBE_MS,
        TmsDownloader._QUICK_PROBE_MS,
        TmsDownloader._PATIENT_PROBE_MS,
    ]


def test_download_center_refresh_clicks_visible_duplicate():
    evaluated = []

    class Candidate(Probeable):
        def evaluate(self, expression):
            evaluated.append(expression)

    candidates = [Candidate(False), Candidate(True)]

    class Matches:
        def count(self):
            return len(candidates)

        def nth(self, index):
            return candidates[index]

    assert TmsDownloader._click_first_visible_dom(Matches()) is True
    assert evaluated == ["element => element.click()"]


def test_download_center_ignores_unrelated_task_counts(app_config, monkeypatch):
    class Link(Probeable):
        first = None

        def __init__(self):
            super().__init__(True)
            self.first = self
            self.clicked = False

        def count(self):
            return 1

        def click(self):
            self.clicked = True

    class Row:
        def __init__(self, text, link=None):
            self.text = text
            self.link = link

        def inner_text(self):
            return self.text

        def locator(self, selector):
            assert selector == "a:has(img[src*='excel'])"
            assert self.link is not None
            return self.link

    link = Link()
    rows = [
        Row("maintainCompanyOrderPage\n失败\n2026-08-14 10:07\n200\n481"),
        Row(
            "maintainCompanyOrderPage\n成功\n2026-08-14 10:08\n4177\n9320",
            link,
        ),
    ]

    class Rows:
        def filter(self, *, has_text):
            assert has_text == "maintainCompanyOrderPage"
            return self

        def count(self):
            return len(rows)

        def nth(self, index):
            return rows[index]

    expected_download = object()

    class DownloadInfo:
        value = expected_download

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakePage:
        def locator(self, selector):
            assert selector == "tr"
            return Rows()

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 2_000

        def expect_download(self, *, timeout):
            assert timeout > 0
            return DownloadInfo()

    downloader = TmsDownloader(app_config)
    monkeypatch.setattr(downloader, "_open_download_center", lambda page: None)
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: 0)

    result = downloader._download_from_center(
        FakePage(), datetime(2026, 8, 14, 10, 8), 4177
    )

    assert result is expected_download
    assert link.clicked is True


def test_download_center_stops_early_when_new_export_task_never_appears(
    app_config, monkeypatch
):
    class EmptyRows:
        def filter(self, *, has_text):
            return self

        def count(self):
            return 0

    class Refresh:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class FakePage:
        def locator(self, selector):
            return EmptyRows() if selector == "tr" else Refresh()

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 2_000

    app_config.tms.export_task_appear_minutes = 8
    timeline = iter((0, 0, 0, 8 * 60 + 1))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))
    downloader = TmsDownloader(app_config)
    monkeypatch.setattr(downloader, "_open_download_center", lambda page: None)

    with pytest.raises(TmsExportTaskNotFound, match="8 分钟内"):
        downloader._download_from_center(
            FakePage(), datetime(2026, 8, 14, 14, 51), 4220
        )


def test_confirm_export_accepts_disappeared_success_toast(app_config, monkeypatch):
    class Item(Probeable):
        def __init__(self, *, visible=False):
            super().__init__(visible)
            self.clicked = False

        def click(self, **kwargs):
            raise AssertionError("confirmation must use DOM click")

        def is_enabled(self, **kwargs):
            assert kwargs == {"timeout": 5_000}
            return True

        def evaluate(self, expression):
            assert expression == "element => element.click()"
            self.clicked = True

    class Collection:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    confirm = Item(visible=True)
    success = Item(visible=False)

    class FakePage:
        def get_by_role(self, role, *, name):
            return Collection([confirm])

        def get_by_text(self, text, *, exact):
            return Collection([success])

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 250

    timeline = iter((0, 0, 0, 11))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))

    TmsDownloader(app_config)._confirm_export(FakePage())

    assert confirm.clicked is True


def test_confirm_export_still_requires_confirmation_dialog(app_config, monkeypatch):
    class EmptyCollection:
        def count(self):
            return 0

    class FakePage:
        def get_by_role(self, role, *, name):
            return EmptyCollection()

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 250

    timeline = iter((0, 0, TmsDownloader._ELEMENT_WAIT_SECONDS + 1))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))

    with pytest.raises(TmsDownloadError, match="未出现确认窗口"):
        TmsDownloader(app_config)._confirm_export(FakePage())


def test_dom_click_rejects_disabled_button():
    class Disabled:
        def wait_for(self, **kwargs):
            raise AssertionError("已确认可见的元素不应再次 wait_for")

        def is_enabled(self, **kwargs):
            assert kwargs == {"timeout": 5_000}
            return False

        def evaluate(self, expression):
            raise AssertionError("disabled button must not be clicked")

    with pytest.raises(TmsDownloadError, match="页面按钮不可用: 导出"):
        TmsDownloader._dom_click(Disabled(), "导出")


def test_force_click_uses_dom_click_when_toast_covers_menu(app_config):
    evaluated = []

    class Candidate(Probeable):
        def __init__(self):
            super().__init__(True)

        def evaluate(self, expression):
            evaluated.append(expression)

        def click(self):
            raise AssertionError("covered menu must not use a pointer click")

    class Matches:
        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return Candidate()

    class FakePage:
        def get_by_text(self, text, *, exact):
            assert (text, exact) == ("下载中心", True)
            return Matches()

    TmsDownloader(app_config)._click_visible_text(
        FakePage(), "下载中心", force=True
    )

    assert evaluated == ["element => element.click()"]
