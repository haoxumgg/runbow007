#!/usr/bin/env bash
set -euo pipefail

failed_unit="${1:-}"
if [[ ! "$failed_unit" =~ ^runbow007-(hourly|arrival)\.service$ ]]; then
  echo "不支持的失败单元: $failed_unit" >&2
  exit 2
fi

runtime_file="${RUNBOW007_RUNTIME_FILE:-/etc/runbow007/runtime.env}"
if [[ -r "$runtime_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$runtime_file"
  set +a
fi

case "${RUNBOW007_ENABLE_SENDING:-false}" in
  1|true|TRUE|yes|YES)
    ;;
  *)
    echo "群消息发送未启用，仅记录失败日志: $failed_unit"
    exit 0
    ;;
esac

project_root="${RUNBOW007_ROOT:-/opt/runbow007}"
details="$(
  /usr/bin/systemctl show "$failed_unit" --no-pager \
    --property=Result,ExecMainCode,ExecMainStatus,ActiveEnterTimestamp \
    | /usr/bin/tr '\n' ' '
)"
export RUNBOW007_SECRETS_FILE="${RUNBOW007_SECRETS_FILE:-/etc/runbow007/secrets.env}"

cd "$project_root"
exec /usr/bin/docker compose --project-directory "$project_root" \
  run --rm -T app \
  --config /app/config.yaml notify-failure \
  --unit "$failed_unit" --details "$details"
