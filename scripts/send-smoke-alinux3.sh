#!/usr/bin/env bash
set -euo pipefail

rule_code="${1:-}"
order_count="${2:-}"
if [[ "$rule_code" != "R1" && "$rule_code" != "R2" && "$rule_code" != "R3" && "$rule_code" != "R4" && "$rule_code" != "R1,R2,R3,R4" ]]; then
  echo "用法: $0 R1|R2|R3|R4|R1,R2,R3,R4 3|5|all" >&2
  exit 2
fi
if [[ "$order_count" != "3" && "$order_count" != "5" && "$order_count" != "all" ]]; then
  echo "用法: $0 R1|R2|R3|R4|R1,R2,R3,R4 3|5|all" >&2
  exit 2
fi

runtime_file="${RUNBOW007_RUNTIME_FILE:-/etc/runbow007/runtime.env}"
if [[ -r "$runtime_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$runtime_file"
  set +a
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
echo "使用最新测试文件: $latest_file"
command_args=(
  --config /app/config.yaml process-file "$container_file"
  --rules "$rule_code" --send --force-send
)
if [[ "$order_count" != "all" ]]; then
  command_args+=(--max-send-orders "$order_count")
fi

cd "$project_root"
exec /usr/bin/docker compose --project-directory "$project_root" \
  run --rm -T app "${command_args[@]}"
