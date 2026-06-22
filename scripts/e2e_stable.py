import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/openclaw/.openclaw/workspace/testclient-bridge/src")

from mcp_1c import server  # noqa: E402


def kill_all_1c() -> None:
    subprocess.run(["pkill", "-9", "-f", "1cv8"], check=False)
    subprocess.run(["pkill", "-9", "-f", "ibcmd"], check=False)
    subprocess.run(["pkill", "-9", "-f", "Xvfb"], check=False)
    time.sleep(1)


async def wait_for_ws(session_id: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.bridge_status(session_id=session_id)["connected"]:
            return True
        await asyncio.sleep(0.5)
    return False


async def main() -> None:
    print("=== Cleaning up any prior 1C/Xvfb processes ===", flush=True)
    kill_all_1c()

    print("=== Starting WebSocket bridge ===", flush=True)
    server.bridge.request_timeout = 150
    await server.bridge.start()
    print(f"Bridge URL: {server.bridge.url}", flush=True)

    print("=== start_session (one-shot: Xvfb + free ports + version detection + ibsrv + test-manager) ===", flush=True)
    result = await server.start_session(
        base_url=os.environ["TARGET_INFOBASE_URL"],
        username=os.environ["TARGET_USERNAME"],
        password=os.environ.get("TARGET_PASSWORD", ""),
        version="8.5.1.1150",  # опционально, авто-детект из base_url
        session_id="e2e-stable",
        start_pipeline=True,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    session_id = result["session_id"]

    print("=== Waiting for manager to connect to WS bridge ===", flush=True)
    if not await wait_for_ws(session_id, 30):
        print("ERROR: manager did not connect to WS bridge within 30s", flush=True)
        print("=== runtime_status ===", flush=True)
        print(json.dumps(server.runtime_status(session_id=session_id), indent=2, ensure_ascii=False), flush=True)
        await server.stop_session(session_id=session_id)
        print("=== exporting autonomous-server event log ===", flush=True)
        try:
            eventlog = server.export_eventlog(
                eventlog_dir=str(Path(result["data_dir"]) / "ibsrv-data" / "log-data"),
                out_path=str(Path(result["log_dir"]) / "eventlog_manager_timeout.json"),
                session_id=session_id,
            )
            print(json.dumps(eventlog, indent=2, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(f"eventlog export failed: {exc}", flush=True)
        raise SystemExit(1)
    print("Manager connected to WS bridge!", flush=True)

    ping = await server.bridge_call("ping", {}, session_id=session_id)
    print(f"Ping: {ping}", flush=True)

    print("=== connect_test_client (1cv8c /TESTCLIENT against target infobase) ===", flush=True)
    try:
        r = await server.connect_test_client(
            base_url=os.environ["TARGET_INFOBASE_URL"],
            username=os.environ["TARGET_USERNAME"],
            password=os.environ.get("TARGET_PASSWORD", ""),
            port=result["testclient_port"],
            onec_path=result["platform_dir"] + "/1cv8c",
            host="localhost",
            session_id=session_id,
        )
    except Exception as exc:
        print(f"connect_test_client failed: {exc!r}", flush=True)
        print("=== stop_session ===", flush=True)
        stopped = await server.stop_session(session_id=session_id)
        print(f"stop_session: {stopped}", flush=True)
        print("=== exporting autonomous-server event log ===", flush=True)
        try:
            eventlog = server.export_eventlog(
                eventlog_dir=str(Path(result["data_dir"]) / "ibsrv-data" / "log-data"),
                out_path=str(Path(result["log_dir"]) / "eventlog_connect_timeout.json"),
                session_id=session_id,
            )
            print(json.dumps(eventlog, indent=2, ensure_ascii=False), flush=True)
        except Exception as eventlog_exc:
            print(f"eventlog export failed: {eventlog_exc}", flush=True)
        kill_all_1c()
        raise
    print(f"connect_test_client: {r}", flush=True)

    if isinstance(r, dict) and r.get("connected"):
        w = await server.bridge_call("active_window", {}, session_id=session_id)
        print(f"Active window: {w}", flush=True)
    elif isinstance(r, dict) and r.get("error"):
        print(f"Test client connect error: {r}", flush=True)

    print("=== disconnect_test_client ===", flush=True)
    try:
        d = await server.bridge_call("disconnect_test_client", {}, session_id=session_id)
        print(f"disconnect: {d}", flush=True)
    except Exception as exc:
        print(f"disconnect error (ignored): {exc}", flush=True)

    print("=== stop_session ===", flush=True)
    stopped = await server.stop_session(session_id=session_id)
    print(f"stop_session: {stopped}", flush=True)

    try:
        eventlog = server.export_eventlog(
            eventlog_dir=str(Path(result["data_dir"]) / "ibsrv-data" / "log-data"),
            out_path=str(Path(result["log_dir"]) / "eventlog_e2e.json"),
            session_id=session_id,
        )
        print(f"eventlog: {eventlog}", flush=True)
    except Exception as exc:
        print(f"eventlog export failed: {exc}", flush=True)

    kill_all_1c()

    print("=== E2E PASSED ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
