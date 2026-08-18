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
    _detect_suffix,
    _export_id,
    _ExportState,
)

XLSX = b"PK\x03\x04" + b"0" * 64
XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 64
HTML = b"<!DOCTYPE html><html><body>\xe4\xbc\x9a\xe8\xaf\x9d\xe5\xb7\xb2\xe8\xbf\x87\xe6\x9c\x9f"


# --------------------------------------------------------------------- fakes


class _Element:
    """A stand-in for one DOM node behind a Playwright locator."""

    def __init__(self, *, visible=True, text="", href=None, on_click=None):
        self.visible = visible
        self.text = text
        self.href = href
        self.on_click = on_click
        self.clicks = 0
        self.filled = None
        self.probes: list[int] = []

    def wait_for(self, *, state, timeout):
        assert state == "visible"
        self.probes.append(timeout)
        if not self.visible:
            raise TimeoutError("locator not visible")

    def click(self, **kwargs):
        self._fire()

    def evaluate(self, expression):
        assert expression == "element => element.click()"
        self._fire()

    def _fire(self):
        if not self.visible:
            raise TimeoutError("locator not visible")
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()

    def fill(self, value):
        self.filled = value

    def inner_text(self, **kwargs):
        return self.text


class _Missing:
    """What a locator resolves to when the selector matches nothing."""

    def wait_for(self, *, state, timeout):
        raise TimeoutError("no such element")

    def click(self, **kwargs):
        raise TimeoutError("no such element")

    def evaluate(self, expression):
        raise TimeoutError("no such element")

    def inner_text(self, **kwargs):
        raise TimeoutError("no such element")

    def fill(self, value):
        raise TimeoutError("no such element")


class _Locator:
    """Lazily resolved, like the real thing.

    Playwright 的 locator 每次访问都重新匹配 DOM，代码里也是这么用的（先拿到
    locator，再在循环里反复探测）。假对象必须同样惰性，否则遮罩之类"会变的东西"
    在测试里永远停在第一次求值的状态。
    """

    def __init__(self, source):
        self._source = source

    @property
    def _elements(self):
        return list(self._source() if callable(self._source) else self._source)

    @property
    def first(self):
        found = self._elements
        return found[0] if found else _Missing()

    @property
    def last(self):
        found = self._elements
        return found[-1] if found else _Missing()

    def count(self):
        return len(self._elements)

    def filter(self, *, has_text):
        return _Locator([item for item in self._elements if has_text in item.text])

    def evaluate_all(self, expression):
        if "getAttribute('href')" in expression:
            return [item.href for item in self._elements]
        assert "innerText" in expression
        return [item.text.strip() for item in self._elements]


class _Page:
    """Selector-keyed fake page. Values may be a list or a zero-arg callable."""

    def __init__(self, elements=None):
        self.elements = dict(elements or {})
        self.gotos: list[str] = []
        self.waits: list[int] = []
        self.default_timeout = None

    def locator(self, selector):
        return _Locator(self.elements.get(selector, []))

    def goto(self, url, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.gotos.append(url)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def wait_for_function(self, expression, *, timeout):
        return None

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def screenshot(self, *, path, timeout):
        Path(path).write_bytes(b"png")

    def content(self):
        return "<html></html>"


class _Response:
    def __init__(self, body=XLSX, *, ok=True, status=200):
        self._body = body
        self.ok = ok
        self.status = status

    def body(self):
        return self._body


class _Context:
    def __init__(self, response=None):
        self.closed = False
        self.requested: list[str] = []
        self._response = response or _Response()
        self.request = SimpleNamespace(get=self._get)

    def _get(self, url, **kwargs):
        self.requested.append(url)
        return self._response

    def close(self):
        self.closed = True


# ----------------------------------------------------------------- pure bits


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/tms/exportFileDowload?id=34921", 34921),
        ("exportFileDowload?id=7&name=orders.xlsx", 7),
        ("https://otb.lining.com/tms/exportFileDowload?name=x&id=12", 12),
        ("/tms/exportFileDowload", None),
        ("/tms/exportFileDowload?id=abc", None),
        (None, None),
    ],
)
def test_export_id_reads_the_task_id_from_the_link(href, expected):
    assert _export_id(href) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (XLSX, ".xlsx"),
        (XLS, ".xls"),
        (HTML, None),
        (b"", None),
        (b"%PDF-1.7", None),
    ],
)
def test_detect_suffix_identifies_the_payload_by_magic_bytes(body, expected):
    assert _detect_suffix(body) == expected


def test_downloader_uses_configured_timezone():
    config = SimpleNamespace(runtime=SimpleNamespace(timezone="Asia/Shanghai"))

    now = TmsDownloader(config)._local_now()

    assert now.tzinfo is None
    expected = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    assert abs((expected - now).total_seconds()) < 5


# --------------------------------------------------------------------- retry


def test_download_retries_then_succeeds(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)
    expected = DownloadResult(Path("orders.xls"), 10, "current_month")
    outcomes = [RuntimeError("temporary"), RuntimeError("temporary"), expected]
    attempts = []

    def attempt(dataset, *, state):
        attempts.append((state, state.expected_total, state.task_created, state.baseline_id))
        if len(attempts) == 1:
            state.expected_total = 4177
            state.task_created = True
            state.baseline_id = 34920
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []
    monkeypatch.setattr(downloader, "_download_once", attempt)
    monkeypatch.setattr("runbow007.downloader.time.sleep", sleeps.append)

    assert downloader.download() == expected
    assert sleeps == [60, 180]
    # 同一个 state 贯穿三次尝试：基线只拍一次，条数和"已建任务"都跟着走。
    assert attempts[0][0] is attempts[1][0] is attempts[2][0]
    assert attempts[1][1:] == attempts[2][1:] == (4177, True, 34920)


def test_download_reexports_when_no_new_task_ever_appears(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)
    expected = DownloadResult(Path("orders.xls"), 4220, "current_month")
    attempts = []
    sleeps = []

    def attempt(dataset, *, state):
        attempts.append((state.task_created, state.baseline_id))
        if len(attempts) == 1:
            state.expected_total = 4220
            state.task_created = True
            state.baseline_id = 34920
            raise TmsExportTaskNotFound("no new export")
        return expected

    monkeypatch.setattr(downloader, "_download_once", attempt)
    monkeypatch.setattr("runbow007.downloader.time.sleep", sleeps.append)

    assert downloader.download() == expected
    # task_created 被重置成 False，下一次重新点导出；基线保持不变，
    # 万一上一次其实建成了，新的一轮照样能认出它。
    assert attempts == [(False, None), (False, 34920)]
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


@pytest.mark.parametrize(
    "error", [CredentialError("no password"), TmsAuthenticationError("bad password")]
)
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


def test_download_reports_the_step_it_died_on(app_config, monkeypatch):
    downloader = TmsDownloader(app_config)

    def fail(dataset, *, state):
        state.last_step = "等待下载中心生成文件"
        raise RuntimeError("browser")

    monkeypatch.setattr(downloader, "_download_once", fail)
    monkeypatch.setattr("runbow007.downloader.time.sleep", lambda seconds: None)

    with pytest.raises(
        TmsDownloadError, match="连续 3 次失败，最后卡在「等待下载中心生成文件」: browser"
    ):
        downloader.download("open_carryover")


# ------------------------------------------------------------------ watchdog


def test_attempt_watchdog_interrupts_a_hung_attempt(app_config, monkeypatch):
    """浏览器卡死时，Playwright 超时、循环 deadline、单轮预算全部失效。

    SIGALRM 由内核投递，不依赖浏览器是死是活。
    """
    import signal as signal_module

    if not hasattr(signal_module, "SIGALRM"):
        pytest.skip("平台没有 SIGALRM")

    app_config.tms.attempt_timeout_seconds = 60
    downloader = TmsDownloader(app_config)
    alarms = []
    monkeypatch.setattr(signal_module, "alarm", lambda seconds: alarms.append(seconds))

    with downloader._attempt_watchdog():
        pass

    # 进入时按配置武装，退出时解除。
    assert alarms == [60, 0]


def test_attempt_watchdog_is_disabled_by_zero(app_config, monkeypatch):
    import signal as signal_module

    app_config.tms.attempt_timeout_seconds = 0
    alarms = []
    # Windows 没有 signal.alarm，raising=False 让这条断言在两个平台上都成立：
    # 关闭时代码提前返回，本来就不会碰它。
    monkeypatch.setattr(
        signal_module, "alarm", lambda seconds: alarms.append(seconds), raising=False
    )

    with TmsDownloader(app_config)._attempt_watchdog():
        pass

    assert alarms == []


# ---------------------------------------------------------------- diagnostics


def test_failure_capture_skips_the_dom_when_the_page_is_unresponsive(
    app_config, tmp_path
):
    """截图失败说明浏览器已经不响应，绝不能再调 page.content()——它没有 timeout。"""
    content_calls = []

    class DeadPage:
        def screenshot(self, *, path, timeout):
            assert timeout == 15_000
            raise TimeoutError("page frozen")

        def content(self):
            content_calls.append(1)
            raise AssertionError("浏览器无响应时不该再取 HTML，那会无限期挂住")

    TmsDownloader(app_config)._capture_failure(DeadPage(), "等待订单表格出数据", tmp_path)

    assert content_calls == []
    assert not list(tmp_path.iterdir())


def test_failure_capture_saves_both_when_the_page_responds(app_config, tmp_path):
    TmsDownloader(app_config)._capture_failure(_Page(), "点击导出并确认", tmp_path)

    names = sorted(item.name for item in tmp_path.iterdir())
    assert [name.split("-", 2)[2] for name in names] == [
        "点击导出并确认.html",
        "点击导出并确认.png",
    ]


# ---------------------------------------------------------------------- login


def test_login_fills_credentials_and_submits(app_config):
    selectors = app_config.tms.selectors
    username = _Element()
    password = _Element()
    # 登录成功后 SPA 离开登录路由，表单随之消失。
    button = _Element(on_click=lambda: setattr(username, "visible", False))
    page = _Page(
        {
            selectors.username: [username],
            selectors.password: [password],
            selectors.login_button: [button],
        }
    )

    TmsDownloader(app_config)._login_if_needed(page, "secret")

    assert username.filled == "test-user"
    assert password.filled == "secret"
    assert button.clicks == 1


def test_login_is_skipped_when_the_form_never_appears(app_config):
    """已登录的会话不会渲染登录表单，这不是错误。"""
    page = _Page({app_config.tms.selectors.username: []})

    TmsDownloader(app_config)._login_if_needed(page, "secret")


def test_login_rejects_bad_credentials(app_config):
    selectors = app_config.tms.selectors
    # 登录框点完还在，说明账号密码没过。
    page = _Page(
        {
            selectors.username: [_Element()],
            selectors.password: [_Element()],
            selectors.login_button: [_Element()],
        }
    )

    with pytest.raises(TmsAuthenticationError, match="仍停留在登录页"):
        TmsDownloader(app_config)._login_if_needed(page, "wrong")


# ----------------------------------------------------------------- order grid


def test_read_total_uses_the_visible_pagination(app_config):
    """选择器自带 :visible，隐藏标签页的分页数字不会混进来。"""
    page = _Page({app_config.tms.selectors.total_count: [_Element(text="共 4,753 条")]})

    assert TmsDownloader(app_config)._read_total(page) == 4753


def test_read_total_returns_none_when_pagination_is_absent(app_config):
    assert TmsDownloader(app_config)._read_total(_Page()) is None


def test_waits_out_the_loading_mask_before_reading_the_total(app_config, monkeypatch):
    """遮罩还在就不读数——不然会读到"分页已渲染、数据还没回来"的那个 0。"""
    reads = []
    monkeypatch.setattr(
        TmsDownloader, "_read_total", lambda self, page: reads.append(1) or 4753
    )
    mask = _Element(visible=True)
    probes = []

    def masks():
        probes.append(1)
        if len(probes) > 3:
            mask.visible = False
        return [mask]

    page = _Page({".el-loading-mask": masks})

    assert TmsDownloader(app_config)._wait_for_orders(page) == 4753
    # 前三轮遮罩可见，一次都没去读分页。
    assert len(reads) == 1


def test_refuses_to_export_against_an_empty_grid(app_config, monkeypatch):
    """空表格上点导出，TMS 不会建任何任务，只会在下载中心白等到超时。"""
    app_config.tms.grid_load_timeout_seconds = 10
    monkeypatch.setattr(TmsDownloader, "_read_total", lambda self, page: 0)
    page = _Page({".el-loading-mask": []})
    clock = iter([0.0] + [float(step) for step in range(1, 200)])
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(clock))

    with pytest.raises(TmsDownloadError, match="10 秒内表格仍未加载出订单"):
        TmsDownloader(app_config)._wait_for_orders(page)


# --------------------------------------------------------------------- export


def test_click_export_confirms_the_dialog(app_config):
    selectors = app_config.tms.selectors
    export = _Element()
    confirm = _Element(text="确定")
    page = _Page(
        {selectors.download_button: [export], selectors.confirm_button: [confirm]}
    )

    TmsDownloader(app_config)._click_export(page)

    assert (export.clicks, confirm.clicks) == (1, 1)


def test_click_export_fails_loudly_without_an_export_button(app_config):
    page = _Page({app_config.tms.selectors.download_button: []})

    with pytest.raises(TmsDownloadError, match="未找到可见的导出按钮"):
        TmsDownloader(app_config)._click_export(page)


def test_click_export_requires_the_confirmation_dialog(app_config):
    selectors = app_config.tms.selectors
    page = _Page(
        {selectors.download_button: [_Element()], selectors.confirm_button: []}
    )

    with pytest.raises(TmsDownloadError, match="未出现确认窗口"):
        TmsDownloader(app_config)._click_export(page)


def test_apply_filters_selects_the_preset_view(app_config):
    """视图状态按账号共享且粘性，每轮都必须显式选回预设。"""
    selectors = app_config.tms.selectors
    trigger = _Element()
    wanted = _Element(text="AI导出数据（勿动）")
    other = _Element(text="上周发运")
    query = _Element()
    page = _Page(
        {
            selectors.advanced_filter_button: [_Element()],
            selectors.preset_name: [trigger],
            selectors.preset_option: [other, wanted],
            selectors.query_button: [query],
        }
    )

    TmsDownloader(app_config)._apply_filters(page, "current_month")

    assert (trigger.clicks, wanted.clicks, other.clicks, query.clicks) == (1, 1, 0, 1)


def test_apply_filters_names_the_presets_it_could_see(app_config):
    """失败信息要能区分"对话框没展开"和"预设改名了"，否则下次还得猜。"""
    selectors = app_config.tms.selectors
    page = _Page(
        {
            selectors.advanced_filter_button: [_Element()],
            selectors.preset_name: [_Element()],
            selectors.preset_option: [_Element(text="上周发运"), _Element(text="本月全量")],
            selectors.query_button: [_Element()],
        }
    )

    with pytest.raises(TmsDownloadError) as excinfo:
        TmsDownloader(app_config)._apply_filters(page, "current_month")

    message = str(excinfo.value)
    assert "没有找到预设视图: AI导出数据（勿动）" in message
    assert "上周发运" in message and "本月全量" in message


def test_apply_filters_fails_when_the_dialog_never_opens(app_config):
    """点了高级筛选但对话框没开：此前会去点到 DOM 里另一个同类元素，白等 30 秒。"""
    selectors = app_config.tms.selectors
    trigger = _Element(visible=False)
    option = _Element(text="AI导出数据（勿动）")
    page = _Page(
        {
            selectors.advanced_filter_button: [_Element()],
            selectors.preset_name: [trigger],
            selectors.preset_option: [option],
            selectors.query_button: [_Element()],
        }
    )

    with pytest.raises(TmsDownloadError, match="未打开筛选对话框"):
        TmsDownloader(app_config)._apply_filters(page, "current_month")

    assert option.clicks == 0


# ------------------------------------------------------------ download centre


def test_download_center_opens_via_the_icon_selector(app_config):
    menu = _Element()
    page = _Page({app_config.tms.selectors.download_center_menu: [menu]})

    TmsDownloader(app_config)._open_download_center(page)

    assert menu.clicks == 1
    assert page.gotos == []


def test_download_center_reloads_home_when_the_menu_is_missing(app_config):
    menu = _Element(visible=False)
    page = _Page({app_config.tms.selectors.download_center_menu: [menu]})

    def reappear(url, *, wait_until):
        page.gotos.append(url)
        menu.visible = True

    page.goto = reappear

    TmsDownloader(app_config)._open_download_center(page)

    assert page.gotos == [app_config.tms.url]
    assert menu.clicks == 1


def test_download_center_gives_up_after_one_reload(app_config):
    page = _Page({app_config.tms.selectors.download_center_menu: []})

    with pytest.raises(TmsDownloadError, match="未找到下载中心入口"):
        TmsDownloader(app_config)._open_download_center(page)

    assert page.gotos == [app_config.tms.url]


def test_export_links_map_task_ids_to_hrefs(app_config):
    page = _Page(
        {
            app_config.tms.selectors.export_link: [
                _Element(href="/tms/exportFileDowload?id=34920"),
                _Element(href="/tms/exportFileDowload?id=34921"),
                _Element(href="/tms/exportFileDowload"),
            ]
        }
    )

    assert TmsDownloader(app_config)._export_links(page) == {
        34920: "/tms/exportFileDowload?id=34920",
        34921: "/tms/exportFileDowload?id=34921",
    }


def test_await_export_returns_the_task_created_after_the_baseline(app_config, monkeypatch):
    """比基线更新的那个才是本轮的产物；老任务永远不会被误领。"""
    selectors = app_config.tms.selectors
    rounds = [
        [_Element(href="/e?id=34920")],
        [_Element(href="/e?id=34920")],
        [_Element(href="/e?id=34920"), _Element(href="/e?id=34922")],
    ]
    page = _Page(
        {
            selectors.download_center_menu: [_Element()],
            selectors.refresh_button: [_Element()],
            selectors.export_link: lambda: rounds.pop(0) if len(rounds) > 1 else rounds[0],
        }
    )
    state = _ExportState(baseline_id=34920)

    href = TmsDownloader(app_config)._await_export(page, state)

    assert href == "/e?id=34922"


def test_await_export_times_out_into_a_reexport(app_config, monkeypatch):
    selectors = app_config.tms.selectors
    page = _Page(
        {
            selectors.download_center_menu: [_Element()],
            selectors.refresh_button: [_Element()],
            selectors.export_link: [_Element(href="/e?id=34920")],
        }
    )
    app_config.tms.download_timeout_seconds = 60
    clock = iter([0.0] + [float(step) * 30 for step in range(1, 50)])
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(clock))

    with pytest.raises(TmsExportTaskNotFound, match="未出现新的导出文件"):
        TmsDownloader(app_config)._await_export(page, _ExportState(baseline_id=34920))


def test_await_export_window_is_clamped_to_the_remaining_budget(app_config, monkeypatch):
    """剩余预算比配置的等待窗口短时，用预算，别占掉下一个整点。"""
    selectors = app_config.tms.selectors
    page = _Page(
        {
            selectors.download_center_menu: [_Element()],
            selectors.refresh_button: [_Element()],
            selectors.export_link: [],
        }
    )
    app_config.tms.download_timeout_seconds = 600
    ticks = iter([0.0, 0.0, 0.0, 200.0])
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(ticks))
    state = _ExportState(baseline_id=0, budget_deadline=120.0)

    with pytest.raises(TmsExportTaskNotFound, match="120 秒内"):
        TmsDownloader(app_config)._await_export(page, state)


# ---------------------------------------------------------------- saving file


@pytest.mark.parametrize(("body", "suffix"), [(XLSX, ".xlsx"), (XLS, ".xls")])
def test_save_export_names_the_file_after_its_real_format(
    app_config, tmp_path, body, suffix
):
    context = _Context(_Response(body))

    target = TmsDownloader(app_config)._save_export(
        context, "/tms/exportFileDowload?id=7", "current_month-abc", tmp_path
    )

    assert target.name == f"current_month-abc{suffix}"
    assert target.read_bytes() == body
    assert context.requested == ["https://otb.lining.com/tms/exportFileDowload?id=7"]


def test_save_export_rejects_an_html_error_page(app_config, tmp_path):
    """TMS 会话过期时返回 HTML 但状态码仍是 200，按后缀存下去只会在 xlrd 里炸。"""
    context = _Context(_Response(HTML))

    with pytest.raises(TmsDownloadError, match="下载内容不是 Excel"):
        TmsDownloader(app_config)._save_export(
            context, "/tms/exportFileDowload?id=7", "current_month-abc", tmp_path
        )

    evidence = tmp_path / "current_month-abc-bad-body.bin"
    assert evidence.read_bytes() == HTML


def test_save_export_reports_http_failures(app_config, tmp_path):
    context = _Context(_Response(b"", ok=False, status=502))

    with pytest.raises(TmsDownloadError, match="HTTP 502"):
        TmsDownloader(app_config)._save_export(
            context, "/tms/exportFileDowload?id=7", "stem", tmp_path
        )


# ------------------------------------------------------------------ full run


def _fake_playwright(monkeypatch, context, *, expect_persistent=False):
    class FakeBrowser:
        closed = False

        def new_context(self, **kwargs):
            assert kwargs == {
                "viewport": {"width": 1600, "height": 900},
                "locale": "zh-CN",
            }
            return context

        def close(self):
            self.closed = True

    browser = FakeBrowser()

    class FakeChromium:
        def launch_persistent_context(self, profile, **kwargs):
            if not expect_persistent:
                raise AssertionError("默认必须是全新浏览器，不能复用持久化 profile")
            return context

        def launch(self, **kwargs):
            assert kwargs == {"headless": True}
            return browser

    class FakeManager:
        def __enter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeManager())
    monkeypatch.setattr(
        "runbow007.downloader.get_tms_password", lambda username: "password"
    )
    return browser


def test_download_once_snapshots_the_baseline_before_exporting(app_config, monkeypatch):
    order = []
    page = _Page()
    context = _Context()
    context.new_page = lambda: page
    browser = _fake_playwright(monkeypatch, context)
    downloader = TmsDownloader(app_config)

    monkeypatch.setattr(downloader, "_login_if_needed", lambda page, password: None)
    monkeypatch.setattr(
        downloader, "_open_download_center", lambda page: order.append("open-center")
    )
    monkeypatch.setattr(
        downloader,
        "_export_links",
        lambda page: order.append("snapshot") or {34919: "/e?id=34919", 34920: "/e?id=34920"},
    )
    monkeypatch.setattr(
        downloader, "_open_order_page", lambda page: order.append("order-page")
    )
    monkeypatch.setattr(
        downloader, "_apply_filters", lambda page, dataset: order.append("filter")
    )
    monkeypatch.setattr(downloader, "_wait_for_orders", lambda page: 4753)
    monkeypatch.setattr(
        downloader, "_click_export", lambda page: order.append("export")
    )
    monkeypatch.setattr(
        downloader,
        "_await_export",
        lambda page, state: order.append("await") or "/e?id=34921",
    )

    state = _ExportState()
    result = downloader._download_once("current_month", state=state)

    # 快照必须在点导出之前，之后"id 更大"才等于"本轮新建的"。
    assert order == ["open-center", "snapshot", "order-page", "filter", "export", "await"]
    # 逛完下载中心后 DOM 里堆着多份菜单副本，菜单导航会在上面失败；重新加载首页
    # 换回确定的起点。首次 goto 之外，快照后必须再来一次。
    assert page.gotos == [app_config.tms.url, app_config.tms.url]
    assert state.baseline_id == 34920
    assert state.expected_total == 4753
    assert state.task_created is True
    assert result.ui_total == 4753
    assert result.dataset == "current_month"
    assert result.path.suffix == ".xlsx"
    assert result.path.read_bytes() == XLSX
    assert page.default_timeout == app_config.tms.navigation_timeout_seconds * 1000
    assert context.closed is True
    assert browser.closed is True


def test_download_once_reuses_a_created_export_task(app_config, monkeypatch):
    page = _Page()
    context = _Context(_Response(XLS))
    context.new_page = lambda: page
    _fake_playwright(monkeypatch, context)
    downloader = TmsDownloader(app_config)

    def unexpected(*args, **kwargs):
        raise AssertionError("已创建的导出任务不该被重复触发")

    monkeypatch.setattr(downloader, "_login_if_needed", lambda page, password: None)
    monkeypatch.setattr(downloader, "_open_order_page", unexpected)
    monkeypatch.setattr(downloader, "_apply_filters", unexpected)
    monkeypatch.setattr(downloader, "_click_export", unexpected)
    monkeypatch.setattr(downloader, "_export_links", unexpected)
    monkeypatch.setattr(downloader, "_await_export", lambda page, state: "/e?id=34921")

    state = _ExportState(baseline_id=34920, expected_total=4753, task_created=True)
    result = downloader._download_once("current_month", state=state)

    assert result.ui_total == 4753
    assert result.path.suffix == ".xls"


def test_download_once_records_the_failing_step(app_config, monkeypatch):
    page = _Page()
    context = _Context()
    context.new_page = lambda: page
    _fake_playwright(monkeypatch, context)
    downloader = TmsDownloader(app_config)

    monkeypatch.setattr(downloader, "_login_if_needed", lambda page, password: None)
    monkeypatch.setattr(downloader, "_open_download_center", lambda page: None)
    monkeypatch.setattr(downloader, "_export_links", lambda page: {})
    monkeypatch.setattr(downloader, "_open_order_page", lambda page: None)
    monkeypatch.setattr(downloader, "_apply_filters", lambda page, dataset: None)

    def empty_grid(page):
        raise TmsDownloadError("表格未加载出订单")

    monkeypatch.setattr(downloader, "_wait_for_orders", empty_grid)

    state = _ExportState()
    with pytest.raises(TmsDownloadError, match="表格未加载出订单"):
        downloader._download_once("current_month", state=state)

    assert state.last_step == "等待订单表格出数据"
    assert context.closed is True
    # 失败现场落在当天的下载目录里。
    day = app_config.runtime.downloads_dir / datetime.now(
        ZoneInfo(app_config.runtime.timezone)
    ).strftime("%Y%m%d")
    assert [item.suffix for item in sorted(day.iterdir())] == [".html", ".png"]


def test_open_order_page_navigates_the_menu(app_config):
    selectors = app_config.tms.selectors
    advanced = _Element(visible=False)
    order_menu = _Element()
    group_menu = _Element(on_click=lambda: setattr(advanced, "visible", True))
    page = _Page(
        {
            selectors.advanced_filter_button: [advanced],
            selectors.order_menu: [order_menu],
            selectors.group_order_menu: [group_menu],
        }
    )

    TmsDownloader(app_config)._open_order_page(page)

    assert (order_menu.clicks, group_menu.clicks) == (1, 1)


def test_open_order_page_is_a_no_op_when_already_there(app_config):
    selectors = app_config.tms.selectors
    order_menu = _Element()
    page = _Page(
        {
            selectors.advanced_filter_button: [_Element()],
            selectors.order_menu: [order_menu],
        }
    )

    TmsDownloader(app_config)._open_order_page(page)

    assert order_menu.clicks == 0


def test_open_order_page_fails_when_the_grid_never_mounts(app_config):
    selectors = app_config.tms.selectors
    page = _Page(
        {
            selectors.advanced_filter_button: [_Element(visible=False)],
            selectors.order_menu: [_Element()],
            selectors.group_order_menu: [_Element()],
        }
    )

    with pytest.raises(TmsDownloadError, match="未加载出高级筛选按钮"):
        TmsDownloader(app_config)._open_order_page(page)
