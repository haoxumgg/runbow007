#!/usr/bin/env bash
set -euo pipefail

order_count="${1:-}"
if [[ "$order_count" != "3" && "$order_count" != "5" ]]; then
  echo "用法: $0 3|5" >&2
  exit 2
fi

project_root="${RUNBOW007_ROOT:-/opt/runbow007}"
latest_file="$(find "$project_root/downloads" -type f \
  \( -name '*.xls' -o -name '*.xlsx' \) -printf '%T@\t%p\n' \
  | sort -nr | sed -n '1{s/^[^\t]*\t//;p;}')"
if [[ -z "$latest_file" ]]; then
  echo "没有找到下载演练生成的 Excel 文件。" >&2
  exit 1
fi

relative_file="${latest_file#"$project_root/downloads/"}"
if [[ "$relative_file" == "$latest_file" || "$relative_file" == ../* ]]; then
  echo "测试文件不在受控下载目录中。" >&2
  exit 1
fi
container_file="/app/downloads/$relative_file"

cd "$project_root"
exec /usr/bin/docker compose --project-directory "$project_root" \
  run --rm -T app --config /app/config.yaml process-file "$container_file" \
  --rules R3 --send --max-send-orders "$order_count"
