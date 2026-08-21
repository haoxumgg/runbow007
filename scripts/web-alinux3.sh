#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
runtime_file="${RUNBOW007_RUNTIME_FILE:-/etc/runbow007/runtime.env}"
if [[ -r "$runtime_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$runtime_file"
  set +a
fi
project_root="${RUNBOW007_ROOT:-/opt/runbow007}"
export RUNBOW007_SECRETS_FILE="${RUNBOW007_SECRETS_FILE:-/etc/runbow007/secrets.env}"
export RUNBOW007_WEB_PORT="${RUNBOW007_WEB_PORT:-8080}"

compose() {
  /usr/bin/docker compose --project-directory "$project_root" "$@"
}

cd "$project_root"
case "$action" in
  start)
    compose up -d web
    ;;
  stop)
    compose rm --stop --force web
    ;;
  restart)
    compose up -d --force-recreate web
    ;;
  status)
    compose ps web
    ;;
  logs)
    compose logs --tail "${2:-200}" web
    ;;
  *)
    echo "用法: $0 start|stop|restart|status|logs [行数]" >&2
    exit 2
    ;;
esac
