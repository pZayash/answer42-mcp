#!/usr/bin/env bash
set -euo pipefail

V8_PATH="${V8_PATH:-/opt/1cv8/x86_64/8.5.1.1150/1cv8}"
DISPLAY="${DISPLAY:-:99}"
export DISPLAY

python3 /home/openclaw/.openclaw/workspace/cc-1c-skills/.claude/skills/epf-build/scripts/epf-build.py \
  -V8Path "$V8_PATH" \
  -SourceFile src/1c/MCPTestManager.xml \
  -OutputFile build/MCPTestManager.epf
