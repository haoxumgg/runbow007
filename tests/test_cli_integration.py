from __future__ import annotations

from contextlib import nullcontext

from runbow007 import cli
from runbow007.downloader import DownloadResult


def _disable_file_lock(monkeypatch):
    monkeypatch.setattr(cli.portalocker, "Lock", lambda *args, **kwargs: nullcontext())


def test_check_config_initializes_database(app_config, monkeypatch, capsys):
    monkeypatch.setattr(cli.AppConfig, "load", lambda path: app_config)

    assert cli.main(["--config", "ignored.yaml", "check-config"]) == 0

    assert app_config.runtime.database_path.exists()
    assert "配置有效" in capsys.readouterr().out


def test_process_file_command_runs_full_dry_run(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch, capsys
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [make_order(order_no="CLI001", is_delayed=True, delay_reason=None)],
    )
    monkeypatch.setattr(cli.AppConfig, "load", lambda path: app_config)
    _disable_file_lock(monkeypatch)

    status = cli.main(
        [
            "--config",
            "ignored.yaml",
            "process-file",
            str(source),
            "--rules",
            " r4, ",
            "--ui-total",
            "1",
        ]
    )

    assert status == 0
    output = capsys.readouterr().out
    assert "rows=1" in output
    assert "candidates=1" in output
    assert "sent=0" in output


def test_run_command_uses_download_result(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "downloaded.xlsx",
        [make_order(order_no="CLI002", is_delayed=True, delay_reason=None)],
    )

    class FakeDownloader:
        def __init__(self, config):
            assert config is app_config

        def download(self, dataset):
            assert dataset == "open_carryover"
            return DownloadResult(source, 1, dataset)

    monkeypatch.setattr(cli.AppConfig, "load", lambda path: app_config)
    monkeypatch.setattr(cli, "TmsDownloader", FakeDownloader)
    _disable_file_lock(monkeypatch)

    assert (
        cli.main(
            [
                "--config",
                "ignored.yaml",
                "run",
                "--dataset",
                "open_carryover",
                "--rules",
                "R4",
            ]
        )
        == 0
    )


def test_cli_rejects_send_limit_above_safety_cap(capsys):
    try:
        cli.build_parser().parse_args(
            ["process-file", "orders.xlsx", "--send", "--max-send-orders", "6"]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unsafe limit must be rejected")
    assert "1–5" in capsys.readouterr().err


def test_cli_returns_distinct_codes_for_failure_and_lock(app_config, monkeypatch, capsys):
    monkeypatch.setattr(cli.AppConfig, "load", lambda path: app_config)
    _disable_file_lock(monkeypatch)
    assert (
        cli.main(
            [
                "--config",
                "ignored.yaml",
                "process-file",
                "missing.xlsx",
                "--rules",
                "RX",
            ]
        )
        == 2
    )
    assert "未知规则" in capsys.readouterr().err

    def locked(*args, **kwargs):
        raise cli.portalocker.AlreadyLocked("busy")

    monkeypatch.setattr(cli.portalocker, "Lock", locked)
    assert cli.main(["--config", "ignored.yaml", "process-file", "x.xlsx"]) == 3
    assert "已有任务运行" in capsys.readouterr().err


def test_credentials_commands_use_system_store(app_config, monkeypatch, capsys):
    saved = []
    monkeypatch.setattr(cli.AppConfig, "load", lambda path: app_config)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "entered-secret")
    monkeypatch.setattr(
        cli, "set_tms_password", lambda username, password: saved.append((username, password))
    )
    monkeypatch.setattr(
        cli, "set_feishu_secret", lambda app_id, secret: saved.append((app_id, secret))
    )

    assert cli.main(["--config", "x", "credentials", "set-tms"]) == 0
    assert cli.main(["--config", "x", "credentials", "set-feishu"]) == 0
    assert saved == [("test-user", "entered-secret"), ("test-app", "entered-secret")]
    assert "已保存" in capsys.readouterr().out


def test_cleanup_downloads_tolerates_os_error(app_config, monkeypatch):
    monkeypatch.setattr(cli, "cleanup_old_downloads", lambda *args, **kwargs: 2)
    cli._cleanup_downloads(app_config)

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(cli, "cleanup_old_downloads", fail)
    cli._cleanup_downloads(app_config)

