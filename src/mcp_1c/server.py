from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
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
    processor: str,
) -> runtime.ManagerRuntime:
    return runtime.ManagerRuntime(
        infobase=runtime.InfobaseSpec(db_path=Path(db_path)),
        manager=runtime.TestManagerSpec(
            onec_path=os.path.join(platform_dir, "1cv8"),
            processor=processor,
        ),
    )


@mcp.tool()
def bridge_status() -> dict[str, Any]:
    """Return WebSocket bridge status and URL for the 1C manager processor."""
    return bridge.status()


@mcp.tool()
def runtime_status() -> dict[str, Any]:
    """Report the live state of the test-manager process."""
    if _RUNTIME is None:
        return {"running": False}
    return {
        "running": True,
        "infobase": str(_RUNTIME.infobase.db_path),
        "manager": {
            "pid": _RUNTIME.manager_process.pid if _RUNTIME.manager_process else None,
            "alive": _RUNTIME.manager_process.poll() is None if _RUNTIME.manager_process else False,
        },
        "log_dir": str(_RUNTIME.log_dir),
    }


@mcp.tool()
def launch_manager(
    platform_dir: Annotated[str, "1C platform directory (contains 1cv8 + ibcmd)"] = "/opt/1cv8/x86_64/8.5.1.1150",
    db_path: Annotated[str, "File infobase path for the test-manager"] = "/tmp/onec-mcp/ib",
    processor: Annotated[str, "Path to MCPTestManager.epf"] = "build/MCPTestManager.epf",
    create_infobase: Annotated[bool, "(Re)create the file infobase before launching"] = True,
    extra_args: Annotated[str, "Extra raw 1C command-line arguments"] = "",
) -> dict[str, Any]:
    """Launch the 1C test-manager (thick client, /TESTMANAGER mode).

    The test-manager is a thick client (`1cv8`) opened against a local
    file infobase. The infobase is created via `ibcmd infobase create`,
    then the manager is started in /TESTMANAGER mode with the EPF executed
    automatically. The EPF opens the WebSocket bridge and waits for
    commands from this MCP server.
    """
    global _RUNTIME

    if _RUNTIME is not None:
        stop_manager()

    runtime_state = _resolve_paths(platform_dir, db_path, processor)
    log_dir = runtime_state.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    onec_thick = os.path.join(platform_dir, "1cv8")
    if create_infobase:
        runtime.create_file_infobase(onec_thick, runtime_state.infobase.db_path)

    argv = runtime.build_test_manager_argv(
        onec_path=runtime_state.manager.onec_path,
        db_path=str(runtime_state.infobase.db_path),
        processor=runtime_state.manager.processor,
        websocket_url=bridge.url,
    )
    if extra_args:
        argv.extend(shlex.split(extra_args))

    manager_proc = runtime.start_test_manager(
        onec_path=runtime_state.manager.onec_path,
        db_path=str(runtime_state.infobase.db_path),
        processor=runtime_state.manager.processor,
        websocket_url=bridge.url,
        log_dir=log_dir,
    )
    runtime_state.manager_process = manager_proc
    _RUNTIME = runtime_state

    return {
        "pid": manager_proc.pid,
        "argv": argv,
        "websocket_url": bridge.url,
        "log_dir": str(log_dir),
    }


@mcp.tool()
def stop_manager() -> dict[str, Any]:
    """Tear down the manager started by launch_manager."""
    global _RUNTIME
    if _RUNTIME is None:
        return {"stopped": False, "reason": "no runtime"}
    rt = _RUNTIME
    _RUNTIME = None
    runtime.kill_process(rt.manager_process, "test-manager")
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
