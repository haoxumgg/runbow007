#!/usr/bin/env bash
set -euo pipefail

lines="${1:-400}"
if [[ ! "$lines" =~ ^[0-9]{1,5}$ ]] || (( lines < 1 || lines > 20000 )); then
  echo "日志行数只能是 1 到 20000。" >&2
  exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "采集用户不是 root，且服务器没有 sudo。" >&2
    exit 1
  fi
  exec sudo -n bash "$0" "$@"
fi

project_root="${RUNBOW007_ROOT:-/opt/runbow007}"

section() {
  printf '\n===== %s =====\n' "$1"
}

section "服务器时间"
date -Is
uptime

section "定时器"
systemctl list-timers 'runbow007-*' --all --no-pager || true

section "服务状态"
systemctl --no-pager --full status \
  runbow007-hourly.service runbow007-arrival.service || true

section "最近 $lines 行 journal（hourly / arrival / 失败告警）"
journalctl --no-pager -n "$lines" \
  -u runbow007-hourly.service \
  -u runbow007-arrival.service \
  -u 'runbow007-failure@*.service' || true

section "失败次数统计（最近 7 天）"
journalctl --no-pager --since '7 days ago' \
  -u runbow007-hourly.service -u runbow007-arrival.service \
  | grep -Ec 'Failed with result|Main process exited, code=exited, status=[1-9]' || true

section "应用日志目录"
ls -lt "$project_root/logs" 2>/dev/null | head -n 20 || true

latest_log="$(ls -1t "$project_root"/logs/*.log 2>/dev/null | head -n 1 || true)"
if [[ -n "$latest_log" ]]; then
  section "最新应用日志 $latest_log 的最后 $lines 行"
  tail -n "$lines" "$latest_log"

  section "最新应用日志中的 WARNING / ERROR / Traceback"
  grep -nE 'WARNING|ERROR|CRITICAL|Traceback|Error|Timeout' "$latest_log" \
    | tail -n "$lines" || true
else
  section "应用日志"
  echo "未找到 $project_root/logs/*.log"
fi

section "下载目录最近文件"
ls -lt "$project_root/downloads" 2>/dev/null | head -n 10 || true
find "$project_root/downloads" -type f -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' \
  2>/dev/null | sort -r | head -n 10 || true

section "运行开关"
sed -E 's/(SECRET|PASSWORD|TOKEN)[A-Z_]*=.*/\1...=***/' \
  /etc/runbow007/runtime.env 2>/dev/null || echo "未找到 runtime.env"

section "磁盘与 Docker"
df -h "$project_root" || true
docker ps -a --filter 'name=runbow007' \
  --format 'table {{.Names}}\t{{.Status}}\t{{.CreatedAt}}' 2>/dev/null | head -n 15 || true
docker images runbow007 --format 'table {{.Repository}}\t{{.Tag}}\t{{.CreatedAt}}' \
  2>/dev/null || true
