from __future__ import annotations

import http.client
import io
import re
import threading
import time
import urllib.parse

import portalocker
import pytest

from runbow007 import web as web_module
from runbow007.web import (
    LoginThrottle,
    SessionStore,
    WebApp,
    boundary_of,
    hash_password,
    parse_multipart,
    verify_password,
)

BOUNDARY = "----runbow007test"


class Client:
    """把 WSGI 环境包起来，让测试读起来像浏览器操作。"""

    def __init__(self, app: WebApp) -> None:
        self.app = app
        self.cookie = ""

    def request(self, method, path, *, body=b"", content_type=None, remote_addr="10.0.0.1"):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "REMOTE_ADDR": remote_addr,
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
        }
        if content_type:
            environ["CONTENT_TYPE"] = content_type
        if self.cookie:
            environ["HTTP_COOKIE"] = self.cookie
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        captured["body"] = b"".join(self.app(environ, start_response)).decode("utf-8")
        captured["header_map"] = {key: value for key, value in captured["headers"]}
        set_cookie = captured["header_map"].get("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        return captured

    def post_form(self, path, fields, **kwargs):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        return self.request(
            path=path,
            method="POST",
            body=body,
            content_type="application/x-www-form-urlencoded",
            **kwargs,
        )

    def login(self, username="admin", password="admin123456", **kwargs):
        return self.post_form(
            "/login", {"username": username, "password": password}, **kwargs
        )

    def csrf_token(self):
        page = self.request("GET", "/upload")["body"]
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def upload(self, *, filename, payload, rules=("R4",), csrf=None, extra=()):
        parts = [_part("csrf_token", self.csrf_token() if csrf is None else csrf)]
        parts.extend(_part("rules", code) for code in rules)
        parts.extend(_part(name, value) for name, value in extra)
        if filename is not None:
            parts.append(_part("file", payload, filename=filename))
        body = b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()
        return self.request(
            "POST",
            "/upload",
            body=body,
            content_type=f"multipart/form-data; boundary={BOUNDARY}",
        )


def _part(name: str, value, *, filename: str | None = None) -> bytes:
    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"'
    if filename is not None:
        head += f'; filename="{filename}"\r\nContent-Type: application/octet-stream'
    head += "\r\n\r\n"
    payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return head.encode("utf-8") + payload + b"\r\n"


@pytest.fixture
def client(app_config):
    app_config.ensure_directories()
    return Client(WebApp(app_config))


@pytest.fixture
def workbook_bytes(tmp_path, make_order, write_orders_xlsx):
    source = write_orders_xlsx(
        tmp_path / "tms-export.xlsx",
        [
            make_order(order_no=f"D00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 4)
        ],
    )
    return source.read_bytes()


def _banner(page: str) -> str:
    match = re.search(r'class="banner [a-z]+">([^<]*)', page)
    return match.group(1).strip() if match else ""


def test_password_hash_round_trip():
    encoded = hash_password("admin123456", iterations=1000)

    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert verify_password("admin123456", encoded)
    assert not verify_password("admin1234567", encoded)
    assert not verify_password("admin123456", "not-a-hash")
    assert not verify_password("admin123456", "md5$1$aa$bb")


def test_upload_requires_login(client):
    response = client.request("GET", "/upload")

    assert response["status"].startswith("303")
    assert response["header_map"]["Location"] == "/login"


def test_login_rejects_wrong_password_and_accepts_the_default_account(client):
    failed = client.login(password="wrong")

    assert failed["status"].startswith("200")
    assert "账号或密码不正确" in failed["body"]
    assert "Set-Cookie" not in failed["header_map"]

    accepted = client.login()

    assert accepted["status"].startswith("303")
    assert accepted["header_map"]["Location"] == "/upload"
    cookie = accepted["header_map"]["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "上传 TMS 导出的 Excel" in client.request("GET", "/upload")["body"]


def test_login_accepts_a_non_ascii_password(app_config):
    """口令里有中文时定长比较不能炸成 500。"""
    app_config.web.password = "口令口令口令"
    client = Client(WebApp(app_config))

    assert "账号或密码不正确" in client.login(password="admin123456")["body"]
    assert client.login(password="口令口令口令")["status"].startswith("303")


def test_oversized_login_form_is_rejected_instead_of_crashing(client):
    response = client.post_form(
        "/login", {"username": "admin", "password": "admin123456", "junk": "x" * 9000}
    )

    assert response["status"].startswith("200")
    assert "账号或密码不正确" in response["body"]


def test_login_hash_takes_precedence_over_plain_password(app_config):
    app_config.web.password_hash = hash_password("另一个口令", iterations=1000)
    client = Client(WebApp(app_config))

    assert "账号或密码不正确" in client.login(password="admin123456")["body"]
    assert client.login(password="另一个口令")["status"].startswith("303")


def test_login_is_throttled_after_repeated_failures(client):
    for _ in range(5):
        client.login(password="wrong")

    blocked = client.login()

    assert "登录失败次数过多" in blocked["body"]
    assert client.login(remote_addr="10.0.0.2")["status"].startswith("303")


def test_logout_clears_the_session(client):
    client.login()
    token = client.csrf_token()

    response = client.post_form("/logout", {"csrf_token": token})

    assert response["header_map"]["Location"] == "/login"
    assert client.request("GET", "/upload")["header_map"]["Location"] == "/login"


def test_upload_dry_run_parses_without_sending(client, workbook_bytes):
    client.login()

    response = client.upload(
        filename="hdrunbow-export.xlsx",
        payload=workbook_bytes,
        extra=(("dry_run", "1"), ("ui_total", "3")),
    )

    assert response["status"].startswith("200")
    assert _banner(response["body"]) == "演练完成，未发送飞书。"
    assert "<td>3</td>" in response["body"]
    assert "R4 3" in response["body"]


def test_upload_sends_one_feishu_message_with_the_same_rules(
    client, app_config, workbook_bytes, monkeypatch
):
    sent = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            assert config.chat_id == "test-chat"

        def send(self, message):
            sent.append(message)
            return "om_manual"

    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    client.login()

    response = client.upload(filename="导出 hdrunbow.xls x.xlsx", payload=workbook_bytes)

    assert len(sent) == 1
    assert sent[0].title == "R4订单提醒汇总"
    assert "已推送飞书，本次提醒 3 项。" in response["body"]

    repeated = client.upload(filename="hdrunbow-export.xlsx", payload=workbook_bytes)

    # 去重与自动任务完全一致：同一批订单当天不会被重复推送。
    assert len(sent) == 1
    assert "没有需要新提醒的订单" in repeated["body"]


def test_upload_rejects_a_stale_csrf_token(client, workbook_bytes):
    client.login()

    response = client.upload(
        filename="export.xlsx", payload=workbook_bytes, csrf="forged-token"
    )

    assert response["status"].startswith("400")
    assert _banner(response["body"]) == "会话已过期，请重新提交。"


@pytest.mark.parametrize(
    ("filename", "payload", "rules", "extra", "message"),
    [
        ("export.csv", b"a,b", ("R4",), (), "只支持 TMS 导出的 .xls 或 .xlsx 文件。"),
        (None, b"", ("R4",), (), "请选择要上传的 Excel 文件。"),
        ("export.xlsx", b"", ("R4",), (), "上传的文件是空的。"),
        ("export.xlsx", b"x", (), (), "请至少勾选一条规则。"),
        ("export.xlsx", b"x", ("R4",), (("ui_total", "abc"),), "页面总条数必须是数字。"),
        ("export.xlsx", b"x", ("R4",), (("ui_total", "0"),), "页面总条数必须大于 0。"),
    ],
)
def test_upload_validates_the_form(client, filename, payload, rules, extra, message):
    client.login()

    response = client.upload(
        filename=filename, payload=payload, rules=rules, extra=extra
    )

    assert response["status"].startswith("400")
    assert _banner(response["body"]) == message


def test_upload_reports_a_broken_workbook_instead_of_a_blank_page(client):
    client.login()

    response = client.upload(filename="broken.xlsx", payload=b"definitely-not-a-workbook")

    assert response["status"].startswith("400")
    assert _banner(response["body"]).startswith("处理失败：")
    # 临时文件不能留在磁盘上。
    uploads = client.app.config.runtime.data_dir / "uploads"
    assert not list(uploads.iterdir())


def test_upload_refuses_a_body_over_the_size_limit(client, app_config):
    app_config.web.max_upload_mb = 1
    client.app.max_upload_bytes = 1024
    client.login()

    response = client.upload(filename="export.xlsx", payload=b"x" * 4096)

    assert "上传内容超过 1 MB 上限" in response["body"]


def test_upload_reports_a_busy_job_lock_instead_of_queueing(
    client, workbook_bytes, monkeypatch
):
    """上传和定时任务抢同一把锁，撞上时要给一句人话，而不是 500。"""

    def busy(*args, **kwargs):
        raise portalocker.AlreadyLocked("busy")

    monkeypatch.setattr(portalocker, "Lock", busy)
    client.login()

    response = client.upload(filename="export.xlsx", payload=workbook_bytes)

    assert _banner(response["body"]) == "已有任务正在运行，请稍后再试。"


def test_serve_answers_health_checks_on_a_real_socket(app_config, monkeypatch):
    app_config.web.host = "127.0.0.1"
    app_config.web.port = 0
    servers: list = []
    real_make_server = web_module.make_server

    def spy(host, port, app, **kwargs):
        server = real_make_server(host, port, app, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(web_module, "make_server", spy)
    thread = threading.Thread(target=web_module.serve, args=(app_config,), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not servers and time.time() < deadline:
            time.sleep(0.02)
        assert servers, "服务器没有在 10 秒内启动"
        connection = http.client.HTTPConnection("127.0.0.1", servers[0].server_port, timeout=5)
        connection.request("GET", "/healthz")
        response = connection.getresponse()

        assert (response.status, response.read()) == (200, b"ok")
        connection.close()
    finally:
        for server in servers:
            server.shutdown()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_unknown_paths_and_methods_are_rejected(client):
    assert client.request("GET", "/nope")["status"].startswith("404")
    assert client.request("PUT", "/login")["status"].startswith("405")
    assert client.request("GET", "/logout")["status"].startswith("405")
    assert client.request("GET", "/healthz")["body"] == "ok"
    assert client.request("GET", "/")["header_map"]["Location"] == "/upload"

    client.login()
    assert client.request("PUT", "/upload")["status"].startswith("405")
    # 已登录时再打开登录页直接回上传页。
    assert client.request("GET", "/login")["header_map"]["Location"] == "/upload"


def test_responses_carry_the_hardening_headers(client):
    headers = client.request("GET", "/login")["header_map"]

    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert "form-action 'self'" in headers["Content-Security-Policy"]


def test_sessions_expire_and_can_be_dropped():
    clock = iter([0.0, 0.0, 0.0, 61.0])
    store = SessionStore(60, clock=lambda: next(clock))
    session = store.create("admin")

    assert store.get(session.token) is session
    assert store.get(session.token) is None
    assert store.get(None) is None
    store.drop(None)


def test_login_throttle_forgets_old_failures():
    now = 0.0
    throttle = LoginThrottle(max_failures=2, block_seconds=100, clock=lambda: now)

    throttle.record_failure("ip")
    throttle.record_failure("ip")
    assert throttle.blocked_seconds("ip") > 0

    now = 200.0
    assert throttle.blocked_seconds("ip") == 0
    throttle.record_failure("ip")
    assert throttle.blocked_seconds("ip") == 0
    throttle.reset("ip")
    assert throttle.blocked_seconds("ip") == 0


def test_multipart_parser_handles_filenames_and_ignores_junk():
    body = (
        _part("rules", "R1")
        + _part("file", b"\r\nbinary\r\n", filename="报表 2026.xlsx")
        + b"--" + BOUNDARY.encode() + b"\r\nno-disposition\r\n\r\nignored\r\n"
        + f"--{BOUNDARY}--\r\n".encode()
    )

    parts = parse_multipart(body, BOUNDARY.encode())

    assert [(part.name, part.filename) for part in parts] == [
        ("rules", None),
        ("file", "报表 2026.xlsx"),
    ]
    assert parts[0].text == "R1"
    assert parts[1].value == b"\r\nbinary\r\n"


def test_boundary_is_read_from_the_content_type():
    assert boundary_of('multipart/form-data; boundary="abc"') == b"abc"
    assert boundary_of("multipart/form-data") == b""
    with pytest.raises(ValueError, match="分隔符"):
        parse_multipart(b"", b"")
