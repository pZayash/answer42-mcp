#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PLATFORM_DIR:-}" ]]; then
  PLATFORM_DIR="$(find /opt/1cv8/x86_64 -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
    | sort -V \
    | awk -F. '($1>8)||($1==8&&$2>3)||($1==8&&$2==3&&$3>=27){v=$0} END{if(v) print "/opt/1cv8/x86_64/" v}')"
  if [[ -z "$PLATFORM_DIR" ]]; then
    echo "No installed 1C platform >= 8.3.27 found under /opt/1cv8/x86_64" >&2
    exit 127
  fi
fi
DB_PATH="${DB_PATH:-/tmp/onec-mcp/ib-build-cf}"
SRC_DIR="${1:-src/cf}"
OUT_CF="${2:-build/MCPTestManager.cf}"
LOG="${BUILD_CF_LOG:-build/build_cf.log}"
DISPLAY="${DISPLAY:-:99}"
export DISPLAY

rm -rf "$DB_PATH"
mkdir -p "$(dirname "$OUT_CF")" "$(dirname "$LOG")"

"$PLATFORM_DIR/1cv8" CREATEINFOBASE "File=$DB_PATH" /DisableStartupDialogs >/dev/null
"$PLATFORM_DIR/1cv8" DESIGNER /F "$DB_PATH" \
  /LoadConfigFromFiles "$SRC_DIR" \
  /Out "$LOG" \
  /DisableStartupDialogs
"$PLATFORM_DIR/1cv8" DESIGNER /F "$DB_PATH" \
  /UpdateDBCfg \
  /Out "$LOG" \
  /DisableStartupDialogs
"$PLATFORM_DIR/1cv8" DESIGNER /F "$DB_PATH" \
  /DumpCfg "$OUT_CF" \
  /Out "$LOG" \
  /DisableStartupDialogs

echo "$OUT_CF"
