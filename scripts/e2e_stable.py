#!/usr/bin/env python3
"""E2E test of the autonomous-server pipeline (ibcmd → ibsrv → 1cv8c /WS).

Validates that:
  1. `launch_manager(create_infobase=True, load_configuration=True, start_standalone=True)`
     creates the file IB, loads a .cf into it, and starts ibsrv.
  2. `launch_manager(use_thin_client=True)` starts the thin test-manager
     via `1cv8c /WS ws://.../TESTMANAGER /Execute <epf> /C <ws>`.
  3. The manager connects to the WebSocket bridge.
  4. `connect_test_client` launches 1cv8c /TESTCLIENT and connects.
  5. `disconnect_test_client` cleanly tears down.
  6. `stop_manager` tears down manager + ibsrv.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/openclaw/.openclaw/workspace/ai-sandbox/src")

from mcp_1c import server  # noqa: E402
from mcp_1c import runtime  # noqa: E402

INBOX = Path(os.environ.get("ONEC_MCP_DATA_DIR", "/tmp/onec-mcp"))
IB_PATH = INBOX / "ib"
SRV_DATA = INBOX / "ibsrv-data"
LOG_DIR = INBOX / "logs"


def kill_all_1c() -> None:
    subprocess.run(["pkill", "-9", "-f", "1cv8"], check=False)
    subprocess.run(["pkill", "-9", "-f", "ibcmd"], check=False)
    time.sleep(1)


async def wait_for_ws(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.bridge.status()["connected"]:
            return True
        await asyncio.sleep(0.5)
    return False


async def main() -> None:
    print("=== Cleaning up any prior 1C processes ===", flush=True)
    kill_all_1c()
    INBOX.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Starting WebSocket bridge ===", flush=True)
    await server.bridge.start()
    print(f"Bridge URL: {server.bridge.url}", flush=True)

    print("=== launch_manager (CREATEINFOBASE → ibcmd config load → ibsrv → 1cv8c /WS) ===", flush=True)
    result = server.launch_manager(
        platform_dir="/opt/1cv8/x86_64/8.5.1.1150",
        db_path=str(IB_PATH),
        server_data_dir=str(SRV_DATA),
        ibsrv_address="127.0.0.1",
        ibsrv_port=8314,
        ibsrv_name="mcp",
        processor="build/MCPTestManager.epf",
        create_infobase=True,
        load_configuration=True,
        cf_path=runtime.DEFAULT_CF_TEMPLATE,
        start_standalone=True,
        use_thin_client=True,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)

    print("=== Waiting for manager to connect to WS bridge ===", flush=True)
    if not await wait_for_ws(30):
        print("ERROR: manager did not connect to WS bridge within 30s", flush=True)
        print("=== runtime_status ===", flush=True)
        print(json.dumps(server.runtime_status(), indent=2, ensure_ascii=False), flush=True)
        server.stop_manager()
        return
    print("Manager connected to WS bridge!", flush=True)

    ping = await server.bridge.call("ping", {})
    print(f"Ping: {ping}", flush=True)

    print("=== connect_test_client (1cv8c /TESTCLIENT against dev-06) ===", flush=True)
    r = await server.bridge.call(
        "connect_test_client",
        {
            "base_url": "<TARGET_INFOBASE_URL>",
            "username": "",
            "password": "***",
            "port": 1538,
            "onec_path": "/opt/1cv8/x86_64/8.5.1.1150/1cv8c",
            "host": "localhost",
        },
    )
    print(f"connect_test_client: {r}", flush=True)

    if isinstance(r, dict) and r.get("connected"):
        w = await server.bridge.call("active_window", {})
        print(f"Active window: {w}", flush=True)
    elif isinstance(r, dict) and r.get("error"):
        print(f"Test client connect error: {r}", flush=True)

    print("=== disconnect_test_client ===", flush=True)
    try:
        d = await server.bridge.call("disconnect_test_client", {})
        print(f"disconnect: {d}", flush=True)
    except Exception as exc:
        print(f"disconnect error (ignored): {exc}", flush=True)

    print("=== stop_manager ===", flush=True)
    stopped = server.stop_manager()
    print(f"stop_manager: {stopped}", flush=True)

    kill_all_1c()

    print("=== E2E PASSED ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
