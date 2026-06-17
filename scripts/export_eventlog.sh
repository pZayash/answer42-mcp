#!/usr/bin/env bash
set -euo pipefail

PLATFORM_DIR="${PLATFORM_DIR:-/opt/1cv8/x86_64/8.5.1.1150}"
EVENTLOG_DIR="${1:-${ONEC_MCP_EVENTLOG_DIR:-/tmp/onec-mcp/ibsrv-data/log-data}}"
OUT="${2:-build/eventlog.json}"
FORMAT="${FORMAT:-json}"
FROM="${FROM:-}"
TO="${TO:-}"

mkdir -p "$(dirname "$OUT")"
args=("$PLATFORM_DIR/ibcmd" eventlog export "--format=$FORMAT" "--out=$OUT")
if [[ -n "$FROM" ]]; then
  args+=("--from=$FROM")
fi
if [[ -n "$TO" ]]; then
  args+=("--to=$TO")
fi
args+=("$EVENTLOG_DIR")

"${args[@]}"
echo "$OUT"
