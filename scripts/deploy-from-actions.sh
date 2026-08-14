#!/usr/bin/env bash
set -euo pipefail

commit_sha="${1:-}"
run_smoke_test="${2:-true}"
enable_timers="${3:-false}"
feishu_test_orders="${4:-0}"
feishu_test_rule="${5:-R3}"

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
if [[ "$feishu_test_orders" != "0" && "$feishu_test_orders" != "3" && "$feishu_test_orders" != "5" && "$feishu_test_orders" != "all" ]]; then
  echo "飞书测试订单数只能是 0、3、5 或 all。" >&2
  exit 2
fi
if [[ "$feishu_test_rule" != "R1" && "$feishu_test_rule" != "R2" && "$feishu_test_rule" != "R3" && "$feishu_test_rule" != "R4" ]]; then
  echo "飞书测试规则只能是 R1、R2、R3 或 R4。" >&2
  exit 2
fi
if [[ "$feishu_test_orders" != "0" && "$run_smoke_test" != "true" ]]; then
  echo "真实飞书小批量测试前必须先完成本轮下载演练。" >&2
  exit 2
fi

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
  if [[ ! -r /etc/os-release ]]; then
    echo "服务器未安装 Docker，且无法识别操作系统。" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" == "alinux" && "${VERSION_ID%%.*}" == "3" ]]; then
    echo "检测到 Alibaba Cloud Linux 3，按阿里云官方方式安装 Docker CE 和 Compose。"
    dnf -y install wget
    wget -qO /etc/yum.repos.d/docker-ce.repo \
      http://mirrors.cloud.aliyuncs.com/docker-ce/linux/centos/docker-ce.repo
    sed -i \
      's|https://mirrors.aliyun.com|http://mirrors.cloud.aliyuncs.com|g' \
      /etc/yum.repos.d/docker-ce.repo
    dnf -y install dnf-plugin-releasever-adapter --repo alinux3-plus
    dnf -y install \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  elif [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]]; then
    echo "检测到 Ubuntu 24.04，按 Docker 官方 APT 软件源安装 Docker CE 和 Compose。"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    architecture="$(dpkg --print-architecture)"
    codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    if [[ -z "$codename" ]]; then
      echo "无法识别 Ubuntu 发行代号。" >&2
      exit 1
    fi
    printf '%s\n' \
      'Types: deb' \
      'URIs: https://download.docker.com/linux/ubuntu' \
      "Suites: $codename" \
      'Components: stable' \
      "Architectures: $architecture" \
      'Signed-By: /etc/apt/keyrings/docker.asc' \
      > /etc/apt/sources.list.d/docker.sources
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    echo "仅支持在 Alibaba Cloud Linux 3 或 Ubuntu 24.04 上自动安装 Docker，当前系统: ${PRETTY_NAME:-未知}" >&2
    exit 1
  fi
fi
systemctl enable --now docker
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

if [[ "$feishu_test_orders" != "0" ]]; then
  echo "执行 ${feishu_test_rule} 真实群消息人工验收，订单范围为 ${feishu_test_orders}。"
  bash ./scripts/send-smoke-alinux3.sh "$feishu_test_rule" "$feishu_test_orders"
fi

if [[ "$enable_timers" == "true" ]]; then
  systemctl enable --now runbow007-hourly.timer runbow007-arrival.timer
  systemctl --no-pager --full status runbow007-hourly.timer runbow007-arrival.timer
else
  echo "定时器保持关闭；验收后可重新运行工作流并启用。"
fi

