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

    with pytest.raises(TmsDownloadError, match="连续三次失败: browser"):
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
        pages = [page]

        def close(self):
            self.closed = True

    context = FakeContext()

    class FakeChromium:
        def launch_persistent_context(self, profile, **kwargs):
            assert profile == str(app_config.runtime.browser_profile_dir)
            assert kwargs == {"headless": True, "accept_downloads": True}
            return context

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
        "_locator_or_button",
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
        lambda page, export_started, ui_total: FakeDownload(),
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
    assert export_clicks == [
        ("wait_for", {"state": "visible"}),
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
    context = SimpleNamespace(pages=[page], close=lambda: None)
    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=lambda profile, **kwargs: context
        )
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

    def download_from_center(page, export_started, expected_total):
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


def test_read_total_and_locator_fallback(app_config):
    class FakeLocator:
        first = None

        def __init__(self, text=""):
            self.first = self
            self.text = text

        def inner_text(self):
            return self.text

    class FakePage:
        def locator(self, selector):
            return FakeLocator("共 1,211 条")

        def get_by_role(self, role, *, name):
            assert role == "button"
            return SimpleNamespace(last="fallback-button")

    downloader = TmsDownloader(app_config)
    assert downloader._read_total(FakePage()) == 1211
    app_config.tms.selectors.total_count = ""
    assert downloader._read_total(FakePage()) is None
    assert downloader._locator_or_button(FakePage(), "", r"下载") == "fallback-button"


def test_download_center_ignores_unrelated_task_counts(app_config, monkeypatch):
    class Link:
        first = None

        def __init__(self):
            self.first = self
            self.clicked = False

        def count(self):
            return 1

        def is_visible(self):
            return True

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
    monkeypatch.setattr(downloader, "_click_visible_text", lambda *args, **kwargs: None)
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

    timeline = iter((0, 0, 0, 301))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))
    downloader = TmsDownloader(app_config)
    monkeypatch.setattr(downloader, "_click_visible_text", lambda *args, **kwargs: None)

    with pytest.raises(TmsExportTaskNotFound, match="5 分钟内"):
        downloader._download_from_center(
            FakePage(), datetime(2026, 8, 14, 14, 51), 4220
        )


def test_confirm_export_accepts_disappeared_success_toast(app_config, monkeypatch):
    class Item:
        def __init__(self, *, visible=False):
            self.visible = visible
            self.clicked = False

        def is_visible(self, *, timeout):
            assert timeout == 500
            return self.visible

        def click(self, **kwargs):
            raise AssertionError("confirmation must use DOM click")

        def wait_for(self, **kwargs):
            assert kwargs == {"state": "visible"}

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

    timeline = iter((0, 0, 16))
    monkeypatch.setattr("runbow007.downloader.time.monotonic", lambda: next(timeline))

    with pytest.raises(TmsDownloadError, match="未出现确认窗口"):
        TmsDownloader(app_config)._confirm_export(FakePage())


def test_dom_click_rejects_disabled_button():
    class Disabled:
        def wait_for(self, **kwargs):
            assert kwargs == {"state": "visible"}

        def is_enabled(self, **kwargs):
            assert kwargs == {"timeout": 5_000}
            return False

        def evaluate(self, expression):
            raise AssertionError("disabled button must not be clicked")

    with pytest.raises(TmsDownloadError, match="页面按钮不可用: 导出"):
        TmsDownloader._dom_click(Disabled(), "导出")


def test_force_click_uses_dom_click_when_toast_covers_menu(app_config):
    evaluated = []

    class Candidate:
        def is_visible(self):
            return True

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
