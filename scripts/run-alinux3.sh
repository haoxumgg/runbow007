#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"
runtime_file="${RUNBOW007_RUNTIME_FILE:-/etc/runbow007/runtime.env}"
if [[ -r "$runtime_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$runtime_file"
  set +a
fi
project_root="${RUNBOW007_ROOT:-/opt/runbow007}"

case "$mode" in
  hourly)
    command_args=(--config /app/config.yaml run --rules R1,R3,R4)
    ;;
  arrival)
    command_args=(--config /app/config.yaml run --rules R2)
    ;;
  *)
    echo "用法: $0 hourly|arrival" >&2
    exit 2
    ;;
esac

case "${RUNBOW007_ENABLE_SENDING:-false}" in
  1|true|TRUE|yes|YES)
    command_args+=(--send)
    ;;
esac

cd "$project_root"
exec /usr/bin/docker compose --project-directory "$project_root" \
  run --rm -T app "${command_args[@]}"
