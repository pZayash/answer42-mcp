#!/usr/bin/env bash
set -euo pipefail

PLATFORM_DIR="${PLATFORM_DIR:-/opt/1cv8/x86_64/8.5.1.1150}"
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
