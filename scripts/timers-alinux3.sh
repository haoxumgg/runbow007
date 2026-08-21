#!/usr/bin/env bash
set -euo pipefail

# 自动去 TMS 取数下载的定时任务开关。李宁 TMS 的导出经常取不到数据，所以这两个
# 定时器默认是关的，改用人工上传页面兜底；确认自动下载恢复稳定后再手动打开。

action="${1:-status}"
timers=(runbow007-hourly.timer runbow007-arrival.timer)

case "$action" in
  on)
    systemctl enable --now "${timers[@]}"
    echo "自动下载定时器已开启。"
    ;;
  off)
    systemctl disable --now "${timers[@]}"
    echo "自动下载定时器已关闭；请改用人工上传页面。"
    ;;
  status)
    systemctl is-enabled "${timers[@]}" || true
    systemctl list-timers 'runbow007-*' --all --no-pager
    ;;
  *)
    echo "用法: sudo $0 on|off|status" >&2
    exit 2
    ;;
esac
