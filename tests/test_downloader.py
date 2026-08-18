"""按《在TMS系统上下载数据》的四个步骤驱动 TMS，这里逐步验证。

页面用一个很小的假 Playwright 实现：locator 按选择器登记元素，元素同时充当
locator（wait_for/click/evaluate/fill），因为下载器全程只用这几个能力。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
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
    _StepTracker,
)


class FakeElement:
    """既是元素也是单元素集合；可见性通过 wait_for 探测，和真实 locator 一致。"""

    def __init__(self, text="", *, visible=True, clickable=True, on_click=None):
        self.text = text
        self.visible = visible
        self.clickable = clickable
        self.on_click = on_click
        self.clicks: list[str] = []
        self.probes: list[int] = []
        self.filled: str | None = None
        self.scrolls = 0

    # --- locator 协议 ---
    def wait_for(self, *, state, timeout):
        assert state == "visible"
        self.probes.append(timeout)
        if not self.visible:
            raise TimeoutError("locator not visible")

    def scroll_into_view_if_needed(self, *, timeout=None):
        self.scrolls += 1

    def click(self, *, timeout=None):
        if not self.clickable:
            raise TimeoutError("click intercepted by an overlay")
        self._fire("real")

    def evaluate(self, expression, *args):
        assert expression == "element => element.click()"
        self._fire("dom")

    def inner_text(self, **kwargs):
        return self.text

    def fill(self, value):
        self.filled = value

    def _fire(self, kind):
        self.clicks.append(kind)
        if self.on_click is not None:
            self.on_click()

    # --- 集合协议 ---
    def count(self):
        return 1

    def nth(self, index):
        assert index == 0
        return self

    @property
    def first(self):
        return self


class FakeCollection:
    def __init__(self, elements):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    @property
    def first(self):
        return self.elements[0] if self.elements else FakeElement(visible=False)

    def filter(self, *, has_text):
        return FakeCollection([item for item in self.elements if has_text in item.text])

    def get_by_text(self, value, *, exact):
        if exact:
            return FakeCollection([i for i in self.elements if i.text.strip() == value])
        return FakeCollection([item for item in self.elements if value in item.text])


class FakePage:
    def __init__(
        self, *, css=None, role=None, text=None, rows=None, download=None, responses=None
    ):
        self.responses = responses or []
        self.listeners = []
        self.removed = []
        self.css = css or {}
        self.role = role or {}
        self.text = text or {}
        self.rows = rows
        self.download = download
        self.gotos: list[str] = []
        self.waits: list[int] = []
        self.download_timeouts: list[int] = []
        self.default_timeout = None
        self.scrapes = 0

    def locator(self, selector):
        return FakeCollection(_as_list(self.css.get(selector)))

    def get_by_role(self, role, *, name):
        assert role == "button"
        return FakeCollection(_as_list(self.role.get(name)))

    def get_by_text(self, value, *, exact=True):
        return FakeCollection(_as_list(self.text.get(value)))

    def on(self, event, handler):
        assert event == "response"
        self.listeners.append(handler)
        for response in self.responses:
            handler(response)

    def remove_listener(self, event, handler):
        self.removed.append(event)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def goto(self, url, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.gotos.append(url)

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def evaluate(self, expression, argument=None):
        self.scrapes += 1
        return self.rows(argument) if callable(self.rows) else (self.rows or [])

    @contextmanager
    def expect_download(self, *, timeout):
        self.download_timeouts.append(timeout)
        holder = SimpleNamespace(value=None)
        yield holder
        holder.value = self.download

    def screenshot(self, *, path, timeout):
        Path(path).write_bytes(b"png")

    def content(self):
        return "<html></html>"


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Clock:
    """每次读秒都往前走一点，让所有轮询循环都能自己收敛。"""

    def __init__(self, step=0.25):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


@pytest.fixture
def clock(monkeypatch):
    ticker = Clock()
    monkeypatch.setattr("runbow007.downloader.time.monotonic", ticker)
    return ticker


def _row(started, *, status="成功", records=4910, href="a.exportFileDowload?id=1"):
    text = f"maintainCompanyOrderPage {status} {started} {started} {records} 11032"
    return {"index": 0, "text": text, "href": href}


# ---------------------------------------------------------------------------
# 下载中心行解析
# ---------------------------------------------------------------------------


def test_parse_download_center_row():
    text = "maintainCompanyOrderPage 成功 2026-08-18 13:41 2026-08-18 13:41 4910 11032"

    started_at, record_count = TmsDownloader._parse_download_row(text)

    assert started_at == datetime(2026, 8, 18, 13, 41)
    assert record_count == 4910


def test_parse_download_center_row_survives_raw_cell_whitespace():
    """innerText 里换行、制表符混杂，归一化后仍要认得出列的顺序。"""
    text = "\n maintainCompanyOrderPage\t成功\n2026-08-06 10:03\n2026-08-06 10:03\n1211\n2665\n"

    assert TmsDownloader._parse_download_row(text) == (datetime(2026, 8, 6, 10, 3), 1211)


def test_parse_running_row_has_no_record_count():
    """任务还在跑时只有开始时间，记录数一栏是空的。"""
    started_at, record_count = TmsDownloader._parse_download_row(
        "maintainCompanyOrderPage 处理中 2026-08-18 13:41"
    )

    assert (started_at, record_count) == (datetime(2026, 8, 18, 13, 41), None)


def test_parse_incomplete_download_center_row():
    assert TmsDownloader._parse_download_row("maintainCompanyOrderPage 处理中") == (
        None,
        None,
    )


def test_downloader_uses_configured_timezone():
    config = SimpleNamespace(runtime=SimpleNamespace(timezone="Asia/Shanghai"))

    now = TmsDownloader(config)._local_now()

    assert now.tzinfo is None
    expected = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    assert abs((expected - now).total_seconds()) < 5


# ---------------------------------------------------------------------------
# 重试、单轮预算、看门狗：兜住浏览器异常的安全网
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize("error", [CredentialError("missing"), TmsAuthenticationError("bad")])
def test_download_does_not_retry_authentication_errors(app_config, monkeypatch, error):
    downloader = TmsDownloader(app_config)
    attempts = []

    def fail(dataset, *, state):
        attempts.append(dataset)
        raise error

    monkeypatch.setattr(downloader, "_download_once", fail)

    with pytest.raises(type(error)):
        downloader.download()
    assert attempts == ["current_month"]


def test_download_failure_names_the_step_it_died_on(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)

    def fail(dataset, *, state):
        state.last_step = "步骤三 点击导出并确认"
        raise RuntimeError("boom")

    monkeypatch.setattr(downloader, "_download_once", fail)
    monkeypatch.setattr("runbow007.downloader.time.sleep", lambda seconds: None)

    with pytest.raises(TmsDownloadError, match="最后卡在「步骤三 点击导出并确认」"):
        downloader.download()


def test_attempt_watchdog_interrupts_a_hung_attempt(app_config, monkeypatch):
    """浏览器卡死时，Playwright 超时、循环 deadline、单轮预算全部失效。"""
    import signal as signal_module

    if not hasattr(signal_module, "SIGALRM"):
        pytest.skip("平台没有 SIGALRM")

    app_config.tms.attempt_timeout_seconds = 60
    downloader = TmsDownloader(app_config)
    alarms = []
    monkeypatch.setattr(signal_module, "alarm", lambda seconds: alarms.append(seconds))

    with downloader._attempt_watchdog():
        pass

    assert alarms == [60, 0]


def test_attempt_watchdog_is_disabled_by_zero(app_config, monkeypatch):
    import signal as signal_module

    app_config.tms.attempt_timeout_seconds = 0
    alarms = []
    monkeypatch.setattr(
        signal_module, "alarm", lambda seconds: alarms.append(seconds), raising=False
    )

    with TmsDownloader(app_config)._attempt_watchdog():
        pass

    assert alarms == []


def test_failure_capture_skips_the_dom_when_the_page_is_unresponsive(app_config, tmp_path):
    """截图失败说明浏览器已经不响应，绝不能再调 page.content()——它没有 timeout。"""
    content_calls = []

    class DeadPage:
        def screenshot(self, *, path, timeout):
            assert timeout == 15_000
            raise TimeoutError("page frozen")

        def content(self):
            content_calls.append(1)
            raise AssertionError("浏览器无响应时不该再取 HTML，那会无限期挂住")

    TmsDownloader(app_config)._capture_failure(
        DeadPage(), "步骤三 等待表格加载并读取总数", tmp_path
    )

    assert content_calls == []
    assert not list(tmp_path.iterdir())


def test_failure_capture_saves_both_when_the_page_responds(app_config, tmp_path):
    TmsDownloader(app_config)._capture_failure(FakePage(), "步骤三 点击导出并确认", tmp_path)

    names = sorted(item.name for item in tmp_path.iterdir())
    assert [name.split("-", 2)[2] for name in names] == [
        "步骤三-点击导出并确认.html",
        "步骤三-点击导出并确认.png",
    ]


# ---------------------------------------------------------------------------
# 通用点击 / 可见性
# ---------------------------------------------------------------------------


def test_click_prefers_a_real_click():
    element = FakeElement("导出")

    TmsDownloader._click(element, "导出")

    assert element.clicks == ["real"]
    assert element.scrolls == 1


def test_click_falls_back_to_dom_click_when_an_overlay_intercepts():
    """导出成功提示层会盖住菜单，Playwright 会一直重试到超时。"""
    element = FakeElement("下载中心", clickable=False)

    TmsDownloader._click(element, "下载中心")

    assert element.clicks == ["dom"]


def test_click_reports_the_element_name_when_both_ways_fail():
    class Broken(FakeElement):
        def evaluate(self, expression, *args):
            raise RuntimeError("detached from DOM")

    with pytest.raises(TmsDownloadError, match="点击「查询」失败"):
        TmsDownloader._click(Broken("查询", clickable=False), "查询")


def test_first_visible_skips_hidden_duplicates():
    """TMS 把同一个工具栏渲染多份，只有一份可见。"""
    hidden = FakeElement("导出", visible=False)
    shown = FakeElement("导出")

    assert TmsDownloader._first_visible(FakeCollection([hidden, shown])) is shown


def test_wait_visible_reports_the_element_it_was_looking_for(clock):
    page = FakePage(css={"#quickSearch": FakeElement("高级查找", visible=False)})

    with pytest.raises(TmsDownloadError, match="页面未找到可见元素: 高级查找"):
        page_wait = TmsDownloader(SimpleNamespace())._wait_visible
        page_wait(page, "#quickSearch", "高级查找", seconds=1)


# ---------------------------------------------------------------------------
# 步骤一 登录
# ---------------------------------------------------------------------------


def _login_page(app_config, *, username_visible=True, signed_in_after_click=True, errors=None):
    selectors = app_config.tms.selectors
    signed_in = FakeElement("下载中心", visible=not username_visible)
    username = FakeElement(visible=username_visible)

    def sign_in():
        if signed_in_after_click:
            signed_in.visible = True

    return signed_in, username, FakePage(
        css={
            selectors.username: username,
            selectors.password: FakeElement(),
            selectors.login_button: FakeElement("登录", on_click=sign_in),
            selectors.download_center_menu: signed_in,
            ".el-message--error, .el-message--warning": errors or [],
        }
    )


def test_login_fills_the_form_and_waits_for_the_home_page(app_config, clock):
    selectors = app_config.tms.selectors
    _, username, page = _login_page(app_config)

    TmsDownloader(app_config)._login(page, "s3cret")

    assert username.filled == "test-user"
    assert page.css[selectors.password].filled == "s3cret"
    assert page.css[selectors.login_button].clicks == ["real"]


def test_login_is_skipped_when_the_session_is_still_valid(app_config, clock):
    """持久化 profile 模式下会直接落在首页，这时候没有表单可填。"""
    selectors = app_config.tms.selectors
    _, _, page = _login_page(app_config, username_visible=False)

    TmsDownloader(app_config)._login(page, "s3cret")

    assert page.css[selectors.login_button].clicks == []


def test_login_reports_a_rejected_password_without_retrying(app_config, clock):
    _, _, page = _login_page(
        app_config,
        signed_in_after_click=False,
        errors=[FakeElement("用户名或密码错误")],
    )

    with pytest.raises(TmsAuthenticationError, match="用户名或密码错误"):
        TmsDownloader(app_config)._login(page, "wrong")


def test_login_fails_when_the_form_never_renders(app_config, clock):
    app_config.tms.navigation_timeout_seconds = 1
    page = FakePage(css={})

    with pytest.raises(TmsDownloadError, match="登录页未加载出用户名输入框"):
        TmsDownloader(app_config)._login(page, "s3cret")


# ---------------------------------------------------------------------------
# 步骤二 订单管理 → 集团订单管理
# ---------------------------------------------------------------------------


def test_open_order_page_expands_the_menu_then_opens_the_group_page(app_config, clock):
    selectors = app_config.tms.selectors
    group = FakeElement("集团订单管理", visible=False)
    parent = FakeElement("订单管理", on_click=lambda: setattr(group, "visible", True))
    page = FakePage(
        css={
            selectors.order_menu: [parent],
            selectors.order_page_menu: [group],
            selectors.advanced_search_button: FakeElement("高级查找"),
        }
    )

    TmsDownloader(app_config)._open_order_page(page)

    assert parent.clicks == ["real"]
    assert group.clicks == ["real"]


def test_open_order_page_does_not_collapse_an_already_open_menu(app_config, clock):
    """菜单已经展开时再点父级会把它收起来，子项就消失了。"""
    selectors = app_config.tms.selectors
    parent = FakeElement("订单管理")
    group = FakeElement("集团订单管理")
    page = FakePage(
        css={
            selectors.order_menu: [parent],
            selectors.order_page_menu: [group],
            selectors.advanced_search_button: FakeElement("高级查找"),
        }
    )

    TmsDownloader(app_config)._open_order_page(page)

    assert parent.clicks == []
    assert group.clicks == ["real"]


def test_open_order_page_ignores_the_parent_menu_of_the_same_name(app_config, clock):
    """父级和子级都叫「订单管理」，子项必须只在 li.el-menu-item 里找。"""
    selectors = app_config.tms.selectors
    sibling = FakeElement("订单管理")
    group = FakeElement("集团订单管理")
    page = FakePage(
        css={
            selectors.order_menu: [FakeElement("订单管理")],
            selectors.order_page_menu: [sibling, FakeElement("订单影像管理"), group],
            selectors.advanced_search_button: FakeElement("高级查找"),
        }
    )

    TmsDownloader(app_config)._open_order_page(page)

    assert sibling.clicks == []
    assert group.clicks == ["real"]


# ---------------------------------------------------------------------------
# 步骤三 高级查找 → 预设 → 查询
# ---------------------------------------------------------------------------


def _preset_widget(presets, *, showing, list_open=False):
    """真实控件的行为：触发器显示「上一次用过的预设」，点它才展开列表，
    点中某一项之后触发器的文字才会变成那一项。"""
    trigger = FakeElement(showing)
    items = [FakeElement(name, visible=list_open) for name in presets]

    def open_list():
        for item in items:
            item.visible = True

    trigger.on_click = open_list
    for item in items:
        item.on_click = lambda chosen=item: setattr(trigger, "text", chosen.text)
    return trigger, items


def _filter_page(
    app_config,
    presets=("电子签（11）", "AI导出数据（勿动）", "上海正向", "正向"),
    *,
    showing="浙江离场",
    list_open=False,
):
    selectors = app_config.tms.selectors
    trigger, items = _preset_widget(presets, showing=showing, list_open=list_open)
    return FakePage(
        css={
            selectors.advanced_search_button: FakeElement("高级查找"),
            selectors.preset_trigger: trigger,
            selectors.preset_item: items,
            selectors.query_button: [FakeElement("查询"), FakeElement("保存")],
        },
        role={"查询": []},
    )


def test_apply_preset_walks_the_documented_buttons(app_config, clock):
    selectors = app_config.tms.selectors
    page = _filter_page(app_config)

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert page.css[selectors.advanced_search_button].clicks == ["real"]
    assert page.css[selectors.preset_trigger].clicks == ["real"]
    chosen = [item for item in page.css[selectors.preset_item] if item.clicks]
    assert [item.text for item in chosen] == ["AI导出数据（勿动）"]


def test_apply_preset_never_clicks_save(app_config, clock):
    """预设里的日期条件由人工维护，程序点「保存」会改掉所有人共用的视图。"""
    selectors = app_config.tms.selectors
    page = _filter_page(app_config)

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    query, save = page.css[selectors.query_button]
    assert query.clicks == ["real"]
    assert save.clicks == []


def test_preset_choice_is_an_exact_match(app_config, clock):
    """列表里「正向」和「上海正向」互为子串，子串匹配会选错。"""
    app_config.tms.current_month_preset = "正向"
    selectors = app_config.tms.selectors
    page = _filter_page(app_config)

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    chosen = [item.text for item in page.css[selectors.preset_item] if item.clicks]
    assert chosen == ["正向"]


def test_preset_choice_tolerates_whitespace_in_the_label(app_config, clock):
    selectors = app_config.tms.selectors
    page = _filter_page(app_config, presets=("电子签（11）", "\n  AI导出数据（勿动）\n "))

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert page.css[selectors.preset_item][1].clicks == ["real"]


def test_preset_trigger_is_located_without_reading_its_text(app_config, clock):
    """触发器显示的是「上一次用过的预设」，谁切过就变成谁的，绝不能拿它定位。"""
    selectors = app_config.tms.selectors
    page = _filter_page(app_config, showing="随便哪个别人选过的视图")

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert page.css[selectors.preset_trigger].clicks == ["real"]
    chosen = [item.text for item in page.css[selectors.preset_item] if item.clicks]
    assert chosen == ["AI导出数据（勿动）"]


def test_preset_list_already_open_is_not_clicked_shut(app_config, clock):
    """下拉已经开着时再点触发器，会把它关掉——旧代码就是这样一个都没选中。"""
    selectors = app_config.tms.selectors
    page = _filter_page(app_config, list_open=True)

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert page.css[selectors.preset_trigger].clicks == []
    chosen = [item.text for item in page.css[selectors.preset_item] if item.clicks]
    assert chosen == ["AI导出数据（勿动）"]


def test_hidden_popover_copies_are_skipped(app_config, clock):
    """Element UI 会在 DOM 里留下隐藏的 popover 副本，点它等于什么都没点。"""
    selectors = app_config.tms.selectors
    page = _filter_page(app_config)
    ghost = FakeElement("AI导出数据（勿动）", visible=False)
    # 换一个新列表，展开下拉时不会顺带把这份隐藏副本也点亮。
    page.css[selectors.preset_item] = [ghost, *page.css[selectors.preset_item]]

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert ghost.clicks == []
    assert page.css[selectors.preset_item][2].clicks == ["real"]


def test_preset_that_did_not_take_effect_aborts_the_round(app_config, clock):
    """2026-08-17 那轮就是没选中却一路跑到底，带着 12644 行的错误视图发了飞书。"""
    selectors = app_config.tms.selectors
    page = _filter_page(app_config, showing="浙江离场")
    for item in page.css[selectors.preset_item]:
        item.on_click = None  # 点了没反应，标题还是别人的那个

    with pytest.raises(TmsDownloadError, match="标题仍然是「浙江离场」"):
        TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert page.css[selectors.query_button][0].clicks == []


def test_unreadable_preset_title_does_not_invent_a_failure(app_config, clock):
    selectors = app_config.tms.selectors
    page = _filter_page(app_config)
    trigger = page.css[selectors.preset_trigger]
    original = trigger.on_click
    trigger.on_click = lambda: (original(), setattr(trigger, "visible", False))

    TmsDownloader(app_config)._apply_preset(page, "current_month")

    assert page.css[selectors.query_button][0].clicks == ["real"]


def test_missing_preset_lists_what_the_dropdown_actually_offers(app_config, clock):
    app_config.tms.current_month_preset = "AI导出数据（勿动）"
    page = _filter_page(app_config, presets=("电子签（11）", "调拨"))

    with pytest.raises(TmsDownloadError, match="当前可选: 电子签（11）、调拨"):
        TmsDownloader(app_config)._apply_preset(page, "current_month")


# ---------------------------------------------------------------------------
# 步骤三 等待表格 / 读取总数
# ---------------------------------------------------------------------------


def _grid_page(app_config, totals, *, mask_visible=False):
    selectors = app_config.tms.selectors
    counter = iter(totals)

    class Pagination(FakeElement):
        def inner_text(self, **kwargs):
            try:
                value = next(counter)
            except StopIteration:
                value = totals[-1]
            return "" if value is None else f"共 {value} 条"

    return FakePage(
        css={
            ".el-loading-mask": FakeElement(visible=mask_visible),
            selectors.total_count: Pagination(),
        }
    )


def test_grid_wait_returns_the_visible_page_total(app_config, clock):
    page = _grid_page(app_config, [0, 0, 4910])

    assert TmsDownloader(app_config)._wait_for_grid(page) == 4910


def test_grid_wait_continues_when_the_total_cannot_be_read(app_config, clock):
    """总数只是给行数校验用的参考值，读不到不该拖垮整轮。"""
    app_config.tms.grid_load_timeout_seconds = 10
    page = _grid_page(app_config, [None])

    assert TmsDownloader(app_config)._wait_for_grid(page) is None


def test_grid_wait_refuses_to_export_an_empty_grid(app_config, clock):
    """在空表格上点导出，TMS 不会建任何任务，等于白烧一整次尝试。"""
    app_config.tms.grid_load_timeout_seconds = 10
    page = _grid_page(app_config, [0])

    with pytest.raises(TmsDownloadError, match="页面仍显示共 0 条"):
        TmsDownloader(app_config)._wait_for_grid(page)


def test_read_total_ignores_the_hidden_tab_pagination(app_config):
    """集团订单管理的「共 4753 条」和下载中心的「共 34920 条」选择器一模一样。"""
    hidden = FakeElement("共 34920 条", visible=False)
    shown = FakeElement("共 4753 条")
    page = FakePage(css={app_config.tms.selectors.total_count: [hidden, shown]})

    assert TmsDownloader(app_config)._read_total(page) == 4753


def test_read_total_is_skipped_when_the_selector_is_blank(app_config):
    app_config.tms.selectors.total_count = ""

    assert TmsDownloader(app_config)._read_total(FakePage()) is None


def test_loading_mask_delays_the_total_read(app_config, clock):
    """遮罩没散就去读分页，读到的是上一次查询留下的数字。"""
    selectors = app_config.tms.selectors
    mask = FakeElement(visible=True)
    reads = []

    class Pagination(FakeElement):
        def inner_text(self, **kwargs):
            reads.append(mask.visible)
            return "共 4910 条"

    page = FakePage(css={".el-loading-mask": mask, selectors.total_count: Pagination()})
    original = page.wait_for_timeout

    def hide_mask_after_a_moment(milliseconds):
        original(milliseconds)
        if len(page.waits) >= 3:
            mask.visible = False

    page.wait_for_timeout = hide_mask_after_a_moment

    assert TmsDownloader(app_config)._wait_for_grid(page) == 4910
    assert reads and not any(reads)


# ---------------------------------------------------------------------------
# 步骤三 导出 → 确定
# ---------------------------------------------------------------------------


def test_export_clicks_the_button_then_the_confirmation(app_config, clock):
    selectors = app_config.tms.selectors
    page = FakePage(
        css={selectors.export_button: FakeElement("导出")},
        role={"确定": FakeElement("确定")},
        text={"下载任务添加成功": FakeElement("下载任务添加成功")},
    )

    TmsDownloader(app_config)._export(page, _ExportState(not_before=EXPORT_CLICKED_AT))

    assert page.css[selectors.export_button].clicks == ["real"]
    assert page.role["确定"].clicks == ["real"]


def test_export_accepts_a_toast_that_already_disappeared(app_config, clock):
    """成功提示是短暂 toast，页面慢时定位不到；下载中心才是最终依据。"""
    page = FakePage(
        css={app_config.tms.selectors.export_button: FakeElement("导出")},
        role={"确定": FakeElement("确定")},
        text={"下载任务添加成功": FakeElement(visible=False)},
    )

    TmsDownloader(app_config)._export(page, _ExportState(not_before=EXPORT_CLICKED_AT))

    assert page.role["确定"].clicks == ["real"]


def test_export_requires_the_confirmation_dialog(app_config, clock):
    page = FakePage(
        css={app_config.tms.selectors.export_button: FakeElement("导出")},
        role={"确定": FakeElement("确定", visible=False)},
    )

    with pytest.raises(TmsDownloadError, match="页面未找到可见元素: 确定"):
        TmsDownloader(app_config)._export(page, _ExportState(not_before=EXPORT_CLICKED_AT))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_export_remembers_the_task_id_returned_by_tms(app_config, clock):
    """建任务的接口如果把任务号回给我们，本轮该下哪一个就是确定的。"""
    state = _ExportState(not_before=EXPORT_CLICKED_AT)
    page = FakePage(
        css={app_config.tms.selectors.export_button: FakeElement("导出")},
        role={"确定": FakeElement("确定")},
        text={"下载任务添加成功": FakeElement("下载任务添加成功")},
        responses=[
            FakeResponse({"success": True, "data": {"id": 814690, "userId": 42}}),
            FakeResponse(ValueError("不是 JSON")),
        ],
    )

    TmsDownloader(app_config)._export(page, state)

    assert state.export_task_ids == (814690, 42)
    assert page.removed == ["response"]


def test_export_without_a_task_id_falls_back_quietly(app_config, clock):
    state = _ExportState(not_before=EXPORT_CLICKED_AT)
    page = FakePage(
        css={app_config.tms.selectors.export_button: FakeElement("导出")},
        role={"确定": FakeElement("确定")},
        text={"下载任务添加成功": FakeElement("下载任务添加成功")},
        responses=[FakeResponse({"code": 0, "msg": "下载任务添加成功"})],
    )

    TmsDownloader(app_config)._export(page, state)

    assert state.export_task_ids == ()


@pytest.mark.parametrize(
    "href,expected",
    [
        ("*.exportFileDowload?id=814690&authorization=undefined", 814690),
        ("*.exportFileDowload?authorization=undefined&id=99", 99),
        ("*.exportFileDowload", None),
    ],
)
def test_task_id_is_read_out_of_the_download_href(href, expected):
    assert TmsDownloader._href_task_id(href) == expected


# ---------------------------------------------------------------------------
# 步骤四 下载中心
# ---------------------------------------------------------------------------


def test_download_centre_prefers_the_icon_selector_over_text(app_config, clock):
    selectors = app_config.tms.selectors
    by_icon = FakeElement("下载中心")
    by_text = FakeElement("下载中心")
    page = FakePage(
        css={
            selectors.download_center_menu: by_icon,
            "tbody tr": FakeElement("maintainCompanyOrderPage"),
        },
        text={"下载中心": by_text},
    )

    TmsDownloader(app_config)._open_download_center(page)

    assert by_icon.clicks == ["real"]
    assert by_text.clicks == []


def test_download_centre_falls_back_to_the_menu_text(app_config, clock):
    by_text = FakeElement("下载中心")
    page = FakePage(
        css={"tbody tr": FakeElement("maintainCompanyOrderPage")},
        text={"下载中心": by_text},
    )

    TmsDownloader(app_config)._open_download_center(page)

    assert by_text.clicks == ["real"]


def test_download_centre_retries_when_the_table_never_renders(app_config, clock):
    menu = FakeElement("下载中心")
    page = FakePage(css={app_config.tms.selectors.download_center_menu: menu})

    with pytest.raises(TmsDownloadError, match="没有加载出任务列表"):
        TmsDownloader(app_config)._open_download_center(page)

    assert menu.clicks == ["real", "real"]


def test_collect_tasks_merges_rows_split_by_fixed_columns(app_config):
    """Element UI 的固定列会把一行拆进两张表：任务名在左、下载图标在右。"""
    rows = [
        {
            "index": 0,
            "text": "maintainCompanyOrderPage 成功 2026-08-18 13:41 2026-08-18 13:41 4910 11032",
            "href": "a.exportFileDowload?id=814653",
        },
        {"index": 1, "text": "在途&签收异常填报 成功 2026-08-18 09:16", "href": "x"},
    ]
    page = FakePage(rows=rows)

    tasks = TmsDownloader(app_config)._collect_tasks(page)

    assert len(tasks) == 1
    assert tasks[0].started_at == datetime(2026, 8, 18, 13, 41)
    assert tasks[0].record_count == 4910
    assert tasks[0].succeeded is True
    assert tasks[0].href == "a.exportFileDowload?id=814653"


def test_collect_tasks_survives_a_failed_scrape(app_config):
    class BrokenPage(FakePage):
        def evaluate(self, expression, argument=None):
            raise RuntimeError("execution context destroyed")

    assert TmsDownloader(app_config)._collect_tasks(BrokenPage()) == []


def _center_page(app_config, rows, *, download=None):
    selectors = app_config.tms.selectors
    css = {selectors.download_center_refresh: FakeElement("刷新")}
    for row in rows:
        if row.get("href"):
            css[f'{selectors.download_link}[href="{row["href"]}"]'] = FakeElement()
    return FakePage(css=css, rows=lambda selector: rows, download=download)


#: 点「导出」的时刻。下载中心的归属判断只看它，所以这里用固定时间。
EXPORT_CLICKED_AT = datetime(2026, 8, 18, 13, 40, 30)


def _state(app_config):
    return _ExportState(not_before=EXPORT_CLICKED_AT, budget_deadline=None), EXPORT_CLICKED_AT


def _stamp(moment, *, minutes=0):
    return (moment - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def test_known_task_id_beats_taking_the_newest(app_config, clock):
    """拿到任务号就不用猜了：哪怕别人的导出更新，也认我们自己那一号。"""
    state, _ = _state(app_config)
    state.export_task_ids = (814653,)
    rows = [
        _row("2026-08-18 13:41", href="a.exportFileDowload?id=814690"),  # 别人的，更新
        _row("2026-08-18 13:40", href="a.exportFileDowload?id=814653"),  # 我们的
    ]
    page = _center_page(app_config, rows, download=SimpleNamespace(name="excel"))

    TmsDownloader(app_config)._wait_for_export_file(page, state, _StepTracker())

    link = "a[href*='exportFileDowload'][href=\"a.exportFileDowload?id=%s\"]"
    assert page.css[link % "814653"].clicks == ["real"]
    assert page.css[link % "814690"].clicks == []


def test_unmatched_task_id_falls_back_to_the_newest(app_config, clock):
    """抓到的号在页面上对不上时，退回原来的判断，不能因此卡死。"""
    state, _ = _state(app_config)
    state.export_task_ids = (999999,)
    rows = [_row("2026-08-18 13:41", href="a.exportFileDowload?id=814690")]
    page = _center_page(app_config, rows, download=SimpleNamespace(name="excel"))

    TmsDownloader(app_config)._wait_for_export_file(page, state, _StepTracker())

    link = 'a[href*=\'exportFileDowload\'][href="a.exportFileDowload?id=814690"]'
    assert page.css[link].clicks == ["real"]


def test_export_file_takes_the_newest_successful_task_in_the_window(app_config, clock):
    """同名任务页面上会有很多，按时间窗 + 成功 + 最新锁定本轮那一行。

    两条都在时间窗内（行里的时间只精确到分钟，所以窗口往前放宽了一分钟），
    只有「取最新」这一条规则能把它们分开。
    """
    state, _ = _state(app_config)  # 点导出的时刻是 13:40:30，时间窗从 13:39:30 起算
    rows = [
        _row("2026-08-18 13:41", records=4910, href="a.exportFileDowload?id=NEW"),
        _row("2026-08-18 13:40", records=4825, href="a.exportFileDowload?id=EARLIER"),
        _row("2026-08-18 09:07", records=4825, href="a.exportFileDowload?id=OLD"),
    ]
    page = _center_page(app_config, rows, download=SimpleNamespace(name="excel"))

    download = TmsDownloader(app_config)._wait_for_export_file(page, state, _StepTracker())

    assert download.name == "excel"
    link = "a[href*='exportFileDowload'][href=\"a.exportFileDowload?id=%s\"]"
    assert page.css[link % "NEW"].clicks == ["real"]
    assert page.css[link % "EARLIER"].clicks == []
    assert page.css[link % "OLD"].clicks == []


def test_export_file_ignores_tasks_started_before_the_export_click(app_config, clock):
    """别人几小时前导出的同名任务不能被当成本轮产物。"""
    app_config.tms.export_task_appear_minutes = 1
    state, _ = _state(app_config)
    page = _center_page(app_config, [_row("2026-01-01 08:00")])

    with pytest.raises(TmsExportTaskNotFound, match="没有出现本轮任务"):
        TmsDownloader(app_config)._wait_for_export_file(page, state, _StepTracker())


def test_export_file_keeps_waiting_while_the_task_is_running(app_config, clock):
    state, now = _state(app_config)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    running = _row(stamp, status="处理中", records=None, href=None)
    running["text"] = f"maintainCompanyOrderPage 处理中 {stamp}"
    finished = _row(stamp, href="a.exportFileDowload?id=DONE")
    rows = [running]

    def scrape(selector):
        # 刷新几次之后任务才跑完。
        return rows if page.scrapes < 3 else [finished]

    page = _center_page(app_config, [finished])
    page.rows = scrape

    download = TmsDownloader(app_config)._wait_for_export_file(
        page, state, _StepTracker()
    )

    assert download is page.download
    assert page.css[app_config.tms.selectors.download_center_refresh].clicks


def test_export_file_reexports_when_the_task_failed(app_config, clock):
    """任务状态是失败就别再等了，快速重来一遍比空转 8 分钟划算。

    失败的行照样带着下载图标，所以「成功」这个条件必须真的参与判断。
    """
    state, now = _state(app_config)
    failed = _row(_stamp(now), status="失败", href="a.exportFileDowload?id=BAD")
    page = _center_page(app_config, [failed])

    with pytest.raises(TmsExportTaskNotFound, match="状态为失败"):
        TmsDownloader(app_config)._wait_for_export_file(page, state, _StepTracker())

    link = 'a[href*=\'exportFileDowload\'][href="a.exportFileDowload?id=BAD"]'
    assert page.css[link].clicks == []


def test_export_file_window_is_clamped_to_the_remaining_budget(app_config, clock):
    """整点调度只有一小时，下载中心不能等到把下一轮也吃掉。"""
    app_config.tms.download_timeout_seconds = 600
    app_config.tms.export_task_appear_minutes = 30
    state, now = _state(app_config)
    state.budget_deadline = clock.now + 120
    page = _center_page(app_config, [_row("2026-01-01 08:00")])

    with pytest.raises(TmsDownloadError):
        TmsDownloader(app_config)._wait_for_export_file(page, state, _StepTracker())

    assert clock.now <= 200


# ---------------------------------------------------------------------------
# 四步串起来
# ---------------------------------------------------------------------------


def _full_page(app_config):
    selectors = app_config.tms.selectors
    href = "a.exportFileDowload?id=814653"
    downloader = TmsDownloader(app_config)

    def rows(selector):
        # 抓取时才生成时间戳：它必然不早于点「导出」的时刻，不会踩到分钟翻转。
        return [_row(downloader._local_now().strftime("%Y-%m-%d %H:%M"), href=href)]

    signed_in = FakeElement("下载中心", visible=False)
    username = FakeElement()
    group = FakeElement("集团订单管理", visible=False)
    # 触发器上留着别人上一次用的预设，本轮必须显式切回来。
    preset_trigger, preset_items = _preset_widget(
        ("电子签（11）", "AI导出数据（勿动）"), showing="浙江离场"
    )

    page = FakePage(
        css={
            selectors.username: username,
            selectors.password: FakeElement(),
            selectors.login_button: FakeElement(
                "登录", on_click=lambda: setattr(signed_in, "visible", True)
            ),
            selectors.download_center_menu: signed_in,
            ".el-message--error, .el-message--warning": [],
            selectors.order_menu: FakeElement(
                "订单管理", on_click=lambda: setattr(group, "visible", True)
            ),
            selectors.order_page_menu: group,
            selectors.advanced_search_button: FakeElement("高级查找"),
            selectors.preset_trigger: preset_trigger,
            selectors.preset_item: preset_items,
            selectors.query_button: [FakeElement("查询"), FakeElement("保存")],
            ".el-loading-mask": FakeElement(visible=False),
            selectors.total_count: FakeElement("共 4910 条"),
            selectors.export_button: FakeElement("导出"),
            "tbody tr": FakeElement("maintainCompanyOrderPage"),
            selectors.download_center_refresh: FakeElement("刷新"),
            f'{selectors.download_link}[href="{href}"]': FakeElement(),
        },
        role={"确定": FakeElement("确定"), "查询": []},
        text={
            "下载任务添加成功": FakeElement("下载任务添加成功"),
            "下载中心": FakeElement("下载中心"),
        },
        rows=rows,
    )
    return page


def _fake_playwright(page, monkeypatch):
    context = SimpleNamespace(new_page=lambda: page, close=lambda: None)
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)

    class Chromium:
        def launch_persistent_context(self, profile, **kwargs):
            raise AssertionError("默认必须是全新浏览器，不能复用持久化 profile")

        def launch(self, **kwargs):
            assert kwargs == {"headless": True}
            return browser

    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=Chromium())

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: Manager())
    monkeypatch.setattr("runbow007.downloader.get_tms_password", lambda username: "pw")


def test_download_once_walks_all_four_documented_steps(app_config, clock, monkeypatch):
    selectors = app_config.tms.selectors
    page = _full_page(app_config)
    page.download = SimpleNamespace(
        suggested_filename="orders.xlsx",
        save_as=lambda target: Path(target).write_bytes(b"valid workbook bytes"),
    )
    _fake_playwright(page, monkeypatch)

    state = _ExportState(not_before=datetime(2026, 8, 14, 10, 8))
    result = TmsDownloader(app_config)._download_once("current_month", state=state)

    assert page.gotos == [app_config.tms.url]
    assert page.default_timeout == app_config.tms.navigation_timeout_seconds * 1000
    assert page.css[selectors.username].filled == "test-user"
    assert page.css[selectors.order_page_menu].clicks == ["real"]
    assert page.css[selectors.advanced_search_button].clicks == ["real"]
    assert page.css[selectors.preset_item][1].clicks == ["real"]
    assert page.css[selectors.preset_trigger].text == "AI导出数据（勿动）"
    assert page.css[selectors.query_button][0].clicks == ["real"]
    assert page.css[selectors.query_button][1].clicks == []
    assert page.css[selectors.export_button].clicks == ["real"]
    assert page.role["确定"].clicks == ["real"]
    assert result.ui_total == 4910
    assert result.dataset == "current_month"
    assert result.path.suffix == ".xlsx"
    assert result.path.read_bytes() == b"valid workbook bytes"
    assert state.task_created is True
    # 归属时间窗锚在「点导出」那一刻，而不是整轮开始。
    assert state.not_before > datetime(2026, 8, 14, 10, 8)


def test_download_once_reuses_an_export_task_created_by_a_previous_attempt(
    app_config, clock, monkeypatch
):
    selectors = app_config.tms.selectors
    page = _full_page(app_config)
    page.download = SimpleNamespace(
        suggested_filename="orders.xls",
        save_as=lambda target: Path(target).write_bytes(b"reused workbook"),
    )
    _fake_playwright(page, monkeypatch)

    state = _ExportState(
        not_before=TmsDownloader(app_config)._local_now(),
        expected_total=4177,
        task_created=True,
    )
    result = TmsDownloader(app_config)._download_once("current_month", state=state)

    assert result.ui_total == 4177
    assert result.path.read_bytes() == b"reused workbook"
    # 步骤二、三整段跳过，不会重复建一个导出任务。
    assert page.css[selectors.advanced_search_button].clicks == []
    assert page.css[selectors.export_button].clicks == []


def test_download_once_rejects_an_empty_file(app_config, clock, monkeypatch):
    page = _full_page(app_config)
    page.download = SimpleNamespace(
        suggested_filename="orders.xls",
        save_as=lambda target: Path(target).write_bytes(b""),
    )
    _fake_playwright(page, monkeypatch)

    with pytest.raises(TmsDownloadError, match="文件为空"):
        TmsDownloader(app_config)._download_once(
            "current_month", state=_ExportState(not_before=datetime(2026, 8, 14, 10, 8))
        )
