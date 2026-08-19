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
  scripts/deploy-alinux3.sh

export RUNBOW007_SECRETS_FILE=/etc/runbow007/secrets.env

# 构建时把 apt 指向阿里云镜像。这台服务器到境外源的吞吐普遍很低，2026-08-19
# 一次构建光 apt 就耗掉 21 分钟仍未装完，浏览器下载还没轮到就触发了构建上限。
# mirrors.cloud.aliyuncs.com 是本项目已经在用的源（见 deploy-from-actions.sh
# 安装 Docker CE 的部分），这里不引入新的来源。置空可退回 Debian 官方源。
export RUNBOW007_APT_MIRROR="${RUNBOW007_APT_MIRROR-mirrors.cloud.aliyuncs.com}"

# 构建必须有上限。缓存命中时这一步是秒级，需要重新下载 Playwright 浏览器时才会
# 变长——而这台服务器到 cdn.playwright.dev 的吞吐随时段剧烈波动：2026-08-18
# 15:53 全部下完只用了 165 秒，同日 18:00 起实测只有约 0.1 MB/s，300MB 根本不可能
# 在部署窗口内下完。没有上限的话，这种时候构建会一直磨到 GitHub 那边 75 分钟的
# 作业上限才被打断，当天为此白烧了五次部署，而且日志要等作业结束才能下载，
# 期间完全看不出发生了什么。
# 25 分钟远高于健康构建的耗时，触发它就说明网络已经不具备完成下载的条件，
# 此时立刻失败并说清楚原因，比磨满 75 分钟有用得多。
build_timeout_seconds="${RUNBOW007_BUILD_TIMEOUT_SECONDS:-1500}"
# 用 `|| status=$?` 而不是 `if ! cmd`：后者在 then 分支里读到的 $? 是取反之后的
# 结果，拿不到 timeout 用来表示"超时"的 124。
build_status=0
timeout "$build_timeout_seconds" \
  docker compose --project-directory "$project_root" build app || build_status=$?
if [[ "$build_status" -ne 0 ]]; then
  if [[ "$build_status" -eq 124 ]]; then
    echo "镜像构建超过 ${build_timeout_seconds} 秒仍未完成。" >&2
    echo "常见原因是需要重新下载 Playwright 浏览器，而当前到 CDN 的带宽过低；" >&2
    echo "可稍后重试，或改用镜像源/预构建镜像。" >&2
  fi
  exit "$build_status"
fi

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
systemctl daemon-reload

if $enable_timers; then
  systemctl enable --now runbow007-hourly.timer runbow007-arrival.timer
  echo "定时器已启用。"
else
  echo "镜像和 systemd 文件已安装，但定时器尚未启动。"
  echo "请填写 config.yaml 和 /etc/runbow007/secrets.env，完成演练后再执行："
  echo "sudo scripts/deploy-alinux3.sh --enable-timers"
fi
