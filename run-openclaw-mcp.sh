#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Each OpenClaw MCP runtime process needs its own Answer42 session scope.
# A fixed scope makes concurrent agent sessions fail with: "scope ... is locked by another live process".
raw_scope="${OPENCLAW_SESSION_ID:-${OPENCLAW_SESSION_KEY:-${OPENCLAW_AGENT_ID:-answer42}}}-${$}"
scope="$(printf '%s' "$raw_scope" | tr -c 'A-Za-z0-9_.-' '-' | cut -c1-120)"
exec "$ROOT/.venv/bin/answer42" --session-scope "$scope" "$@"
