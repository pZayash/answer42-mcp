#!/usr/bin/env python3
"""Autonomous E2E test for testclient-bridge.

Starts two local autonomous 1C servers:
  * manager ibsrv: hosts MCPTestManager and the WebSocket bridge client
  * client ibsrv: hosts the tested 1C application for /TESTCLIENT

No external stand is used.
"""

import asyncio
import subprocess
import sys
import time

sys.path.insert(0, "/home/openclaw/.openclaw/workspace/testclient-bridge/src")

from mcp_1c import server  # noqa: E402


def kill_all_1c() -> None:
    subprocess.run(["pkill", "-9", "-f", "1cv8"], check=False)
    subprocess.run(["pkill", "-9", "-f", "ibcmd"], check=False)
    subprocess.run(["pkill", "-9", "-f", "ibsrv"], check=False)
    subprocess.run(["pkill", "-9", "-f", "Xvfb"], check=False)
    time.sleep(1)


async def wait_for_bridge(session_id: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.bridge_status(session_id=session_id)["connected"]:
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Manager did not connect to bridge within {timeout}s")


async def main() -> None:
    print("=== Autonomous testclient-bridge E2E (two ibsrv) ===", flush=True)
    kill_all_1c()

    result = await server.start_session(
        version="8.5.1.1150",
        session_id="e2e-full",
        start_pipeline=True,
    )
    sid = result["session_id"]

    assert result.get("client_ibsrv_port"), result
    assert result["ibsrv_port"] != result["client_ibsrv_port"], result
    print(f"Manager ibsrv={result['ibsrv_port']}; client ibsrv={result['client_ibsrv_port']}; display={result['display']}", flush=True)

    assert server.runtime_status(session_id=sid)["running"] is True
    await wait_for_bridge(sid)
    print("Manager bridge connected", flush=True)

    connected = await server.connect_test_client(
        base_url=result["client_base_url"],
        username="",
        password="",
        port=result["testclient_port"],
        onec_path=result["platform_dir"] + "/1cv8c",
        session_id=sid,
    )
    assert connected["connected"] is True, connected
    print("Test client connected", flush=True)

    active = await server.active_window(session_id=sid)
    assert active, active

    tree = await server.ui_tree(session_id=sid, max_depth=2)
    assert tree, tree

    command_bar = await server.command_bar(session_id=sid)
    assert isinstance(command_bar, dict), command_bar

    current = await server.current_element(session_id=sid)
    assert isinstance(current, dict), current

    # Open a real list form from the autonomous test configuration.
    nav = await server.execute_window_command(command="e1cib/list/Справочник.Пользователи", session_id=sid)
    assert nav["executed"] is True, nav

    rows = await server.table_rows(name="Список", session_id=sid, limit=5)
    assert rows["found"] is True, rows
    assert "rows" in rows, rows

    current_row = await server.table_current_row(name="Список", session_id=sid)
    assert current_row["found"] is True, current_row

    selected_rows = await server.table_selected_rows(name="Список", session_id=sid)
    assert selected_rows["found"] is True, selected_rows

    moved = await server.table_move_row(name="Список", direction="next", session_id=sid)
    assert moved["found"] is True, moved

    screenshot = server.screenshot(path=f"{result['data_dir']}/test.png", session_id=sid)
    assert screenshot["bytes"] > 0, screenshot
    print(f"Screenshot: {screenshot['path']} ({screenshot['bytes']} bytes)", flush=True)

    closed = await server.close_form(session_id=sid)
    assert closed["closed"] is True, closed

    await server.disconnect_test_client(session_id=sid)
    await server.stop_session(session_id=sid, clean_data=True)

    print("=== AUTONOMOUS E2E PASSED ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
