from datetime import datetime

from runbow007.downloader import TmsDownloader


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
