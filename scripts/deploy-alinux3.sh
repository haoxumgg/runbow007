#!/usr/bin/env bash
set -euo pipefail

enable_timers=false
if [[ "${1:-}" == "--enable-timers" ]]; then
  enable_timers=true
elif [[ -n "${1:-}" ]]; then
  echo "用法: sudo $0 [--enable-timers]" >&2
  exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 sudo 执行部署脚本" >&2
  exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$project_root" != "/opt/runbow007" ]]; then
  echo "请将仓库部署到 /opt/runbow007，当前路径: $project_root" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未安装 Docker，请先按阿里云 Alibaba Cloud Linux 3 文档安装 Docker CE。" >&2
  exit 1
fi
docker compose version >/dev/null

install -d -m 0750 /etc/runbow007
if [[ ! -f /etc/runbow007/secrets.env ]]; then
  install -m 0600 deploy/secrets.env.example /etc/runbow007/secrets.env
fi
if [[ ! -f /etc/runbow007/runtime.env ]]; then
  install -m 0640 deploy/runtime.env.example /etc/runbow007/runtime.env
fi
if [[ ! -f config.yaml ]]; then
  install -m 0644 config.example.yaml config.yaml
fi

install -d -m 0750 data downloads logs browser-profile
chown -R 10001:10001 data downloads logs browser-profile
chmod +x \
  scripts/run-alinux3.sh \
  scripts/notify-failure-alinux3.sh \
  scripts/deploy-alinux3.sh \
  scripts/web-alinux3.sh \
  scripts/timers-alinux3.sh

export RUNBOW007_SECRETS_FILE=/etc/runbow007/secrets.env
docker compose --project-directory "$project_root" build app

# buildx 的构建缓存从不自动回收：镜像本身只有约 1.9GB，但每次构建都会把
# Playwright 的 chromium(184MB)、headless shell(115MB)、ffmpeg 和 apt 依赖
# 再塞一份进缓存。2026-08-17 实测 51 条缓存记录占了 27.3GB，39GB 的盘用到
# 80%，照那个速度几天就会写满。保留 5GB 热缓存让下次构建仍能复用，其余回收。
# --keep-storage 在新版 Docker 里已改名为 --reserved-space，逐个降级尝试；
# 清理失败绝不能让整个部署挂掉，所以最后兜一个 true。
docker builder prune --force --keep-storage 5GB \
  || docker builder prune --force --reserved-space 5GB \
  || docker builder prune --force \
  || true

install -m 0644 deploy/systemd/runbow007-hourly.service /etc/systemd/system/
install -m 0644 deploy/systemd/runbow007-hourly.timer /etc/systemd/system/
install -m 0644 deploy/systemd/runbow007-arrival.service /etc/systemd/system/
install -m 0644 deploy/systemd/runbow007-arrival.timer /etc/systemd/system/
install -m 0644 deploy/systemd/runbow007-failure@.service /etc/systemd/system/
install -m 0644 deploy/systemd/runbow007-web.service /etc/systemd/system/
systemctl daemon-reload

# 人工上传兜底页面是常驻服务，任何时候都要在。
systemctl enable runbow007-web.service
./scripts/web-alinux3.sh start
systemctl --no-pager --full status runbow007-web.service || true

if $enable_timers; then
  systemctl enable --now runbow007-hourly.timer runbow007-arrival.timer
  echo "自动下载定时器已启用。"
else
  # 只是"不启用"不够：上一次部署开着的定时器会原样留下来。TMS 导出经常取不到
  # 数据，默认必须是关的，由 scripts/timers-alinux3.sh 手动控制。
  systemctl disable --now runbow007-hourly.timer runbow007-arrival.timer 2>/dev/null || true
  echo "自动下载定时器保持关闭；需要时执行 sudo scripts/timers-alinux3.sh on"
fi

echo "人工上传页面: http://<服务器地址>:${RUNBOW007_WEB_PORT:-8080}/"
