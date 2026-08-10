import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from runbow007.retention import cleanup_old_downloads


def test_cleanup_removes_only_expired_downloads(tmp_path):
    now = datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    old_dir = tmp_path / "20260701"
    new_dir = tmp_path / "20260810"
    old_dir.mkdir()
    new_dir.mkdir()
    old_file = old_dir / "old.xls"
    new_file = new_dir / "new.xls"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    old_timestamp = (now - timedelta(days=31)).timestamp()
    new_timestamp = (now - timedelta(days=1)).timestamp()
    os.utime(old_file, (old_timestamp, old_timestamp))
    os.utime(new_file, (new_timestamp, new_timestamp))

    removed = cleanup_old_downloads(tmp_path, retain_days=30, now=now)

    assert removed == 1
    assert not old_file.exists()
    assert not old_dir.exists()
    assert new_file.exists()


def test_cleanup_rejects_invalid_retention(tmp_path):
    now = datetime(2026, 8, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    try:
        cleanup_old_downloads(tmp_path, retain_days=0, now=now)
    except ValueError as exc:
        assert "retain_days" in str(exc)
    else:
        raise AssertionError("应拒绝非正数保留天数")
