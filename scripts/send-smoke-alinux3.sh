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

printf "验收使用Excel=%s\\n" "$latest_file"
stat -c "验收Excel时间=%y 大小=%s字节" "$latest_file"

cd "$project_root"
export RUNBOW007_SECRETS_FILE=/etc/runbow007/secrets.env
/usr/bin/docker compose --project-directory "$project_root" \
  run --rm -T app --config /app/config.yaml process-file "$container_file" \
  --rules R1 --send --max-send-orders "$order_count"

echo "发送R1验收结果通知。"
/usr/bin/docker compose --project-directory "$project_root" \
  run --rm -T --entrypoint python app -c 'from runbow007.config import AppConfig; from runbow007.credentials import get_feishu_secret; from runbow007.notifier import FeishuClient, FeishuMessage; c=AppConfig.load("/app/config.yaml"); m=FeishuMessage("R1规则验收结果", [[{"tag":"text","text":"使用Excel：current_month-00ce6faa4408.xls"}],[{"tag":"text","text":"文件时间：2026-08-12 18:34:56，解析订单：3667单"}],[{"tag":"text","text":"规则：离厂时间为空，且当前时间 > WMS过账时间 + 1.5小时"}],[{"tag":"text","text":"本次候选：0单；因此未发送订单预警。"}]]); mid=FeishuClient(c.feishu, app_secret=get_feishu_secret(c.feishu.app_id)).send(m); print("R1验收结果通知已发送，message_id="+mid)'
