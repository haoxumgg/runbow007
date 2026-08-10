from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def cleanup_old_downloads(root: Path, *, retain_days: int, now: datetime) -> int:
    """Delete regular download files older than the configured retention window."""
    if retain_days < 1:
        raise ValueError("retain_days 必须大于 0")
    root = root.resolve()
    if not root.exists():
        return 0

    cutoff = now - timedelta(days=retain_days)
    removed = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
        if modified >= cutoff:
            continue
        path.unlink()
        removed += 1

    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed
