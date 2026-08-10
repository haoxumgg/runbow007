#!/usr/bin/env bash
set -euo pipefail

commit_sha="${1:-}"
run_smoke_test="${2:-true}"
enable_timers="${3:-false}"

if [[ ! "$commit_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "无效的 Git commit SHA。" >&2
  exit 2
fi
for value in "$run_smoke_test" "$enable_timers"; do
  if [[ "$value" != "true" && "$value" != "false" ]]; then
    echo "布尔参数只能是 true 或 false。" >&2
    exit 2
  fi
done

if [[ "$(id -u)" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "部署用户不是 root，且服务器没有 sudo。" >&2
    exit 1
  fi
  exec sudo -n bash "$0" "$@"
fi

project_root=/opt/runbow007
archive="/tmp/runbow007-${commit_sha}.tar.gz"
secrets_file="/tmp/runbow007-${commit_sha}.env"
script_file="/tmp/runbow007-deploy-${commit_sha}.sh"

cleanup() {
  rm -f -- "$archive" "$secrets_file" "$script_file"
}
trap cleanup EXIT

for path in "$archive" "$secrets_file"; do
  if [[ ! -f "$path" ]]; then
    echo "缺少部署文件: $path" >&2
    exit 1
  fi
done

tar -tzf "$archive" >/dev/null
if tar -tzf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo "部署压缩包包含不安全路径。" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "服务器尚未安装 Docker；请先安装 Docker CE 和 Compose 插件。" >&2
  exit 1
fi
docker compose version >/dev/null

install -d -m 0755 "$project_root"
tar -xzf "$archive" -C "$project_root" --no-same-owner --no-same-permissions
install -d -m 0750 /etc/runbow007
install -m 0600 "$secrets_file" /etc/runbow007/secrets.env

cd "$project_root"
chmod +x scripts/deploy-alinux3.sh scripts/deploy-from-actions.sh scripts/run-alinux3.sh
./scripts/deploy-alinux3.sh

if [[ "$run_smoke_test" == "true" ]]; then
  echo "执行一次真实下载演练；RUNBOW007_ENABLE_SENDING 保持 false。"
  ./scripts/run-alinux3.sh hourly
fi

if [[ "$enable_timers" == "true" ]]; then
  systemctl enable --now runbow007-hourly.timer runbow007-arrival.timer
  systemctl --no-pager --full status runbow007-hourly.timer runbow007-arrival.timer
else
  echo "定时器保持关闭；验收后可重新运行工作流并启用。"
fi

