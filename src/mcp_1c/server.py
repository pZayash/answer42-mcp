from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
from datetime import datetime
import shlex
import subprocess
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from . import runtime
from .bridge import OneCBridgeServer

DEFAULT_TEST_BASE = "<TARGET_INFOBASE_URL>"
DEFAULT_TEST_USER = ""
DEFAULT_TEST_PASSWORD = ""

mcp = FastMCP("1c-test-manager")
bridge = OneCBridgeServer(
    host=os.getenv("ONEC_MCP_WS_HOST", "127.0.0.1"),
    port=int(os.getenv("ONEC_MCP_WS_PORT", "8765")),
    request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "60")),
)

# Single-instance runtime state for the manager process.
_RUNTIME: runtime.ManagerRuntime | None = None


def _default_inbox_dir() -> Path:
    return Path(os.environ.get("ONEC_MCP_DATA_DIR", "/tmp/onec-mcp"))


def _resolve_paths(
    platform_dir: str,
    db_path: str,
    ibsrv_address: str,
    ibsrv_port: int,
    ibsrv_name: str,
    processor: str,
) -> runtime.ManagerRuntime:
    return runtime.ManagerRuntime(
        infobase=runtime.InfobaseSpec(db_path=Path(db_path)),
        server=runtime.StandaloneServerSpec(
            address=ibsrv_address,
            port=ibsrv_port,
            name=ibsrv_name,
            data_dir=Path(os.environ.get("ONEC_MCP_SERVER_DATA_DIR", runtime.DEFAULT_IBSRV_DATA)),
        ),
        manager=runtime.TestManagerSpec(
            onec_path=os.path.join(platform_dir, "1cv8c"),
            processor=processor,
        ),
    )


@mcp.tool()
def bridge_status() -> dict[str, Any]:
    """Return WebSocket bridge status and URL for the 1C manager processor."""
    return bridge.status()


@mcp.tool()
def runtime_status() -> dict[str, Any]:
    """Report the live state of the autonomous server + test-manager processes."""
    if _RUNTIME is None:
        return {"running": False}
    return {
        "running": True,
        "infobase": str(_RUNTIME.infobase.db_path),
        "server": {
            "pid": _RUNTIME.server_process.pid if _RUNTIME.server_process else None,
            "alive": _RUNTIME.server_process.poll() is None if _RUNTIME.server_process else False,
            "address": _RUNTIME.server.address,
            "port": _RUNTIME.server.port,
            "listening": runtime.is_port_listening(
                _RUNTIME.server.address, _RUNTIME.server.port
            ),
        },
        "manager": {
            "pid": _RUNTIME.manager_process.pid if _RUNTIME.manager_process else None,
            "alive": _RUNTIME.manager_process.poll() is None if _RUNTIME.manager_process else False,
        },
        "log_dir": str(_RUNTIME.log_dir),
    }


@mcp.tool()
def launch_manager(
    platform_dir: Annotated[str, "1C platform directory (contains 1cv8/1cv8c/ibcmd/ibsrv)"] = "/opt/1cv8/x86_64/8.5.1.1150",
    db_path: Annotated[str, "File infobase path"] = "/tmp/onec-mcp/ib",
    server_data_dir: Annotated[str, "Standalone server data directory"] = "/tmp/onec-mcp/ibsrv-data",
    ibsrv_address: Annotated[str, "Standalone server listen address"] = "127.0.0.1",
    ibsrv_port: Annotated[int, "Standalone server HTTP port"] = 8314,
    ibsrv_name: Annotated[str, "Standalone server infobase name (--name)"] = "mcp",
    processor: Annotated[str, "Path to MCPTestManager.epf"] = "build/MCPTestManager.epf",
    create_infobase: Annotated[bool, "(Re)create the file infobase before launching"] = True,
    load_configuration: Annotated[bool, "Load a .cf template into the infobase after creation"] = True,
    cf_path: Annotated[str, "Path to .cf file used to populate the configuration"] = runtime.DEFAULT_CF_BUILT,
    start_standalone: Annotated[bool, "Start the autonomous 1C server (ibsrv)"] = True,
    use_thin_client: Annotated[bool, "Use 1cv8c /WS ws://... instead of 1cv8 /F <ib>"] = True,
    extra_args: Annotated[str, "Extra raw 1C command-line arguments"] = "",
) -> dict[str, Any]:
    """Launch the 1C test infrastructure: ibcmd → ibsrv → 1cv8c /WS /TESTMANAGER.

    The default pipeline is:

    1. `1cv8 CREATEINFOBASE File=<db>` creates the file IB on disk.
    2. `ibcmd config load --db-path=<db> <cf>` populates the configuration
       tree (mandatory: a fresh file DB has no configuration, so the
       thin client refuses to log in otherwise).
    3. `ibsrv` is launched in its default form and listens on the
       configured HTTP port.
    4. `1cv8c ENTERPRISE /WS"http://host:port" /TESTMANAGER` starts the
       test manager. The embedded configuration opens MCPTestManager.

    Set `use_thin_client=False` to fall back to the thick-client
    `/F <ib>` form while the HTTP-gate integration is being stabilised.
    """
    global _RUNTIME

    if _RUNTIME is not None:
        stop_manager()

    runtime_state = _resolve_paths(
        platform_dir=platform_dir,
        db_path=db_path,
        ibsrv_address=ibsrv_address,
        ibsrv_port=ibsrv_port,
        ibsrv_name=ibsrv_name,
        processor=processor,
    )
    runtime_state.server.data_dir = Path(server_data_dir)
    log_dir = runtime_state.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    onec_thick = os.path.join(platform_dir, "1cv8")
    onec_thin = os.path.join(platform_dir, "1cv8c")
    ibcmd = os.path.join(platform_dir, "ibcmd")
    ibsrv = os.path.join(platform_dir, "ibsrv")

    if create_infobase:
        runtime.create_file_infobase(ibcmd, runtime_state.infobase.db_path)

    if load_configuration and cf_path:
        runtime.load_configuration(ibcmd, runtime_state.infobase.db_path, cf_path)

    if start_standalone:
        server_proc = runtime.start_standalone_server(
            ibsrv=ibsrv,
            address=ibsrv_address,
            port=ibsrv_port,
            name=runtime_state.server.name,
            data_dir=Path(server_data_dir),
            log_dir=log_dir,
            db_path=runtime_state.infobase.db_path,
        )
        runtime_state.server_process = server_proc

    if use_thin_client:
        ws_url = runtime.build_thin_client_ws_url(ibsrv_address, ibsrv_port)
        argv = runtime.build_thin_test_manager_argv(
            onec_path=onec_thin,
            ws_url=ws_url,
            websocket_url=bridge.url,
        )
        manager_proc = runtime.start_thin_test_manager(
            onec_path=onec_thin,
            ws_url=ws_url,
            websocket_url=bridge.url,
            log_dir=log_dir,
        )
    else:
        argv = runtime.build_test_manager_argv(
            onec_path=onec_thick,
            db_path=str(runtime_state.infobase.db_path),
            processor=processor,
            websocket_url=bridge.url,
        )
        manager_proc = runtime.start_test_manager(
            onec_path=onec_thick,
            db_path=str(runtime_state.infobase.db_path),
            processor=processor,
            websocket_url=bridge.url,
            log_dir=log_dir,
        )

    if extra_args:
        argv.extend(shlex.split(extra_args))

    runtime_state.manager_process = manager_proc
    _RUNTIME = runtime_state

    return {
        "pid": manager_proc.pid,
        "argv": argv,
        "websocket_url": bridge.url,
        "ws_url": ws_url if use_thin_client else None,
        "log_dir": str(log_dir),
    }


@mcp.tool()
def export_eventlog(
    platform_dir: Annotated[str, "1C platform directory (contains ibcmd)"] = "/opt/1cv8/x86_64/8.5.1.1150",
    eventlog_dir: Annotated[str, "Path to autonomous-server event log directory"] = "",
    out_path: Annotated[str, "Output JSON/XML file path"] = "",
    from_time: Annotated[str, "Start timestamp YYYY-MM-DDThh:mm:ss[.mmmmmm]"] = "",
    to_time: Annotated[str, "End timestamp YYYY-MM-DDThh:mm:ss[.mmmmmm]"] = "",
    fmt: Annotated[str, "Export format: json or xml"] = "json",
) -> dict[str, Any]:
    """Export the 1C event log via autonomous-server `ibcmd eventlog export`."""
    ibcmd = os.path.join(platform_dir, "ibcmd")
    if _RUNTIME is not None and not eventlog_dir:
        eventlog_path = _RUNTIME.server.data_dir / "log-data"
        log_dir = _RUNTIME.log_dir
    else:
        eventlog_path = Path(eventlog_dir or "/home/openclaw/.1cv8/1C/1cv8/standalone-server/log-data")
        log_dir = Path("/tmp/onec-mcp/logs")
    if not out_path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = log_dir / f"eventlog_{stamp}.{fmt}"
    else:
        out = Path(out_path)
    exported = runtime.export_eventlog(
        ibcmd=ibcmd,
        eventlog_dir=eventlog_path,
        out_path=out,
        from_time=from_time or None,
        to_time=to_time or None,
        fmt=fmt,
    )
    return {"path": str(exported), "format": fmt, "eventlog_dir": str(eventlog_path)}


@mcp.tool()
def stop_manager() -> dict[str, Any]:
    """Tear down the manager + autonomous server started by launch_manager."""
    global _RUNTIME
    if _RUNTIME is None:
        return {"stopped": False, "reason": "no runtime"}
    rt = _RUNTIME
    _RUNTIME = None
    runtime.kill_process(rt.manager_process, "test-manager")
    runtime.kill_process(rt.server_process, "ibsrv")
    return {"stopped": True}


@mcp.tool()
async def connect_test_client(
    base_url: Annotated[str, "1C infobase URL to open as test client"] = DEFAULT_TEST_BASE,
    username: Annotated[str, "1C username"] = DEFAULT_TEST_USER,
    password: Annotated[str, "1C password"] = DEFAULT_TEST_PASSWORD,
    port: Annotated[int, "Test client TCP port"] = runtime.DEFAULT_TEST_CLIENT_PORT,
    onec_path: Annotated[str, "Path to 1cv8c executable as seen by 1C manager host"] = "/opt/1cv8/x86_64/8.5.1.1150/1cv8c",
    host: Annotated[str, "Host where the test client runs"] = "localhost",
) -> Any:
    """Launch 1cv8c /TESTCLIENT inside the manager processor and connect.

    The actual subprocess is started by the 1C manager via `КомандаСистемы`
    to keep the child process lifetime tied to the manager. This call
    simply forwards the parameters to the manager.
    """
    return await bridge.call(
        "connect_test_client",
        {
            "base_url": base_url,
            "username": username,
            "password": password,
            "port": port,
            "onec_path": onec_path,
            "host": host,
        },
    )


@mcp.tool()
async def disconnect_test_client() -> Any:
    """Disconnect the active 1C test client."""
    return await bridge.call("disconnect_test_client", {})


@mcp.tool()
async def active_window() -> Any:
    """Return active 1C test client window state."""
    return await bridge.call("active_window", {})


@mcp.tool()
async def ui_tree(max_depth: Annotated[int, "Maximum UI tree depth"] = 4) -> Any:
    """Return a serialized tree of the active window/forms/elements."""
    return await bridge.call("ui_tree", {"max_depth": max_depth})


@mcp.tool()
async def command_bar() -> Any:
    """Return command interface/panel information for the active window/form when available."""
    return await bridge.call("command_bar", {})


@mcp.tool()
async def click_button(
    name: Annotated[str, "Button name or title text"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
) -> Any:
    """Find a button in the active UI tree and press it."""
    return await bridge.call("click_button", {"name": name, "search_by_title": search_by_title})


@mcp.tool()
async def find_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object visible title/caption"] = "",
    type: Annotated[str, "1C test object type, e.g. ТестируемаяФорма"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
) -> Any:
    """Find an object in the active UI hierarchy by name, title, or test object type."""
    return await bridge.call("find_object", {"name": name, "title": title, "type": type, "max_depth": max_depth})


@mcp.tool()
async def activate_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object visible title/caption"] = "",
    type: Annotated[str, "1C test object type"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
) -> Any:
    """Find and activate a UI object in the active test client window."""
    return await bridge.call("activate_object", {"name": name, "title": title, "type": type, "max_depth": max_depth})


@mcp.tool()
async def execute_window_command(command: Annotated[str, "Command name to execute in the active window"]) -> Any:
    """Execute a command in the active 1C test client window."""
    return await bridge.call("execute_window_command", {"command": command})


@mcp.tool()
async def current_element() -> Any:
    """Return the current active form element."""
    return await bridge.call("current_element", {})


@mcp.tool()
async def table_current_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
) -> Any:
    """Return current row of a tested table as key/value text mapping."""
    return await bridge.call("table_current_row", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def table_selected_rows(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
) -> Any:
    """Return selected rows of a tested table as key/value text mappings."""
    return await bridge.call("table_selected_rows", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def tabular_documents(
    include_text: Annotated[bool, "Include tabular document text"] = False,
    max_depth: Annotated[int, "Maximum search depth"] = 8,
) -> Any:
    """List tabular document fields in the active window; optionally include their text."""
    return await bridge.call("tabular_documents", {"include_text": include_text, "max_depth": max_depth})


@mcp.tool()
async def tabular_document_text(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
) -> Any:
    """Return text and dimensions of a tabular document field in the active window."""
    return await bridge.call("tabular_document_text", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def set_field_value(
    field_name: Annotated[str, "Field technical name or title"],
    value: Annotated[str, "Value to set"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
) -> Any:
    """Set a value in a form field by name or title."""
    return await bridge.call("set_field_value", {"field_name": field_name, "value": value, "search_by_title": search_by_title})


@mcp.tool()
async def create_new_item() -> Any:
    """Create a new item in the active form (e.g. new catalog/dictionary entry)."""
    return await bridge.call("create_new_item", {})


@mcp.tool()
async def save_form() -> Any:
    """Save/write the active form."""
    return await bridge.call("save_form", {})


@mcp.tool()
async def close_form() -> Any:
    """Close the active form."""
    return await bridge.call("close_form", {})


@mcp.tool()
def screenshot(path: Annotated[str, "Output image path"] = "build/screenshots/screenshot.png") -> dict[str, Any]:
    """Capture a desktop screenshot from the MCP host."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore

        with mss.mss() as sct:
            monitor = sct.monitors[0]
            image = sct.grab(monitor)
            mss.tools.to_png(image.rgb, image.size, output=str(output_path))
    except ImportError as exc:
        raise RuntimeError("Install screenshot extra: pip install '.[screenshot]' or mss") from exc

    data = output_path.read_bytes()
    return {"path": str(output_path), "bytes": len(data), "base64": base64.b64encode(data).decode("ascii")}


async def _run() -> None:
    await bridge.start()
    try:
        await mcp.run_stdio_async()
    finally:
        if _RUNTIME is not None:
            runtime.kill_process(_RUNTIME.manager_process, "test-manager")
            runtime.kill_process(_RUNTIME.server_process, "ibsrv")
        await bridge.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for 1C:Enterprise test manager")
    parser.add_argument("--ws-host", default=os.getenv("ONEC_MCP_WS_HOST", "127.0.0.1"))
    parser.add_argument("--ws-port", type=int, default=int(os.getenv("ONEC_MCP_WS_PORT", "8765")))
    parser.add_argument("--log-level", default=os.getenv("ONEC_MCP_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    bridge.host = args.ws_host
    bridge.port = args.ws_port
    asyncio.run(_run())


if __name__ == "__main__":
    main()
