from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
from datetime import datetime
import shlex
import subprocess
import re
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from . import platform as _platform
from . import runtime
from .bridge import OneCBridgeServer
from .rag import RagService
from .window_control import maximize_test_client_window as _maximize_external_window
from .recorder import RecordingSession

mcp = FastMCP("testclient-bridge")
_RAG = RagService()
DEFAULT_SESSION = "default"


class SessionState:
    """Per-session container for all live 1C/MCP bridge state."""

    def __init__(
        self,
        session_id: str,
        display: str,
        platform_dir: Path,
        ws_port: int,
        ibsrv_port: int,
        testclient_port: int,
        data_dir: Path,
        base_url: str = "",
        username: str = "",
        password: str = "",
    ) -> None:
        self.session_id = session_id
        self.display = display
        self.platform_dir = platform_dir
        self.ws_port = ws_port
        self.ibsrv_port = ibsrv_port
        self.testclient_port = testclient_port
        self.data_dir = data_dir
        self.base_url = base_url
        self.username = username
        self.password = password
        self.client_ibsrv_port = 0  # to be set in start_session
        self.client_runtime: runtime.ManagerRuntime | None = None
        self.xvfb_process: subprocess.Popen | None = None
        self.runtime: runtime.ManagerRuntime | None = None
        self.bridge = OneCBridgeServer(
            host="127.0.0.1",
            port=ws_port,
            request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "60")),
        )
        self.recorder = RecordingSession()
        self.xvfb_process: subprocess.Popen | None = None
        self.runtime: runtime.ManagerRuntime | None = None
        self.bridge = OneCBridgeServer(
            host="127.0.0.1",
            port=ws_port,
            request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "60")),
        )
        self.recorder = RecordingSession()
        self.xvfb_process: subprocess.Popen | None = None
        self.runtime: runtime.ManagerRuntime | None = None
        self.bridge = OneCBridgeServer(
            host="127.0.0.1",
            port=ws_port,
            request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "60")),
        )
        self.recorder = RecordingSession()

    @property
    def onec_thin(self) -> str:
        return str(self.platform_dir / "1cv8c")

    @property
    def ibcmd(self) -> str:
        return str(self.platform_dir / "ibcmd")

    @property
    def ibsrv(self) -> str:
        return str(self.platform_dir / "ibsrv")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ib"

    @property
    def server_data_dir(self) -> Path:
        return self.data_dir / "ibsrv-data"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def client_db_path(self) -> Path:
        return self.data_dir / "ib-client"

    @property
    def client_server_data_dir(self) -> Path:
        return self.data_dir / "ibsrv-data-client"

    @property
    def client_log_dir(self) -> Path:
        return self.data_dir / "logs-client"


_SESSIONS: dict[str, SessionState] = {}


def _session(session_id: str = DEFAULT_SESSION) -> SessionState:
    try:
        return _SESSIONS[session_id]
    except KeyError:
        raise ValueError(f"Session {session_id!r} not found. Call start_session first.")


def _new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:12]


def _record_tool(
    session_id: str,
    action: str,
    params: dict[str, Any],
    result: Any,
) -> Any:
    sess = _session(session_id)
    try:
        sess.recorder.capture(
            lambda path, window, display: _screenshot_for_recorder(path, window, display, session_id),
            action, params, result,
        )
    except Exception as exc:
        logging.debug("recording capture failed for %s/%s: %s", session_id, action, exc)
    return result


async def _bridge_call_recorded(
    session_id: str,
    method: str,
    params: dict[str, Any],
) -> Any:
    sess = _session(session_id)
    result = await sess.bridge.call(method, params)
    return _record_tool(session_id, method, params, result)


def _screenshot_for_recorder(
    path: str,
    window: bool,
    display: str,
    session_id: str = DEFAULT_SESSION,
) -> dict[str, Any]:
    return _do_screenshot(path, window, display, session_id)


def _resolve_runtime(
    platform_dir: Path,
    db_path: Path,
    ibsrv_address: str,
    ibsrv_port: int,
    ibsrv_name: str,
    server_data_dir: Path,
) -> runtime.ManagerRuntime:
    return runtime.ManagerRuntime(
        infobase=runtime.InfobaseSpec(db_path=db_path),
        server=runtime.StandaloneServerSpec(
            address=ibsrv_address,
            port=ibsrv_port,
            name=ibsrv_name,
            data_dir=server_data_dir,
        ),
        manager=runtime.TestManagerSpec(
            onec_path=str(platform_dir / "1cv8c"),
        ),
    )


# ---------------------------------------------------------------------------
# 1C configuration RAG / metadata index tools
# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
# 1C configuration RAG / metadata index tools
# ---------------------------------------------------------------------------


@mcp.tool()
def rag_source_detect(path: Annotated[str, "Path to 1C source directory"],
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Detect 1C source format: EDT-style or Configurator XML dump."""
    return _record_tool(session_id, "rag_source_detect", {"path": path}, _RAG.detect_source(path))


@mcp.tool()
def rag_source_add(
    name: Annotated[str, "Logical source name, e.g. MS or Platform42"],
    path: Annotated[str, "Path to 1C source directory"],
    kind: Annotated[str, "base | extension | configuration | standalone"] = "standalone",
    format: Annotated[str, "auto | edt | designer_xml"] = "auto",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Register or update a 1C source tree for RAG indexing."""
    return _record_tool(session_id, "rag_source_add", {"name": name, "path": path, "kind": kind, "format": format}, _RAG.add_source(name=name, path=path, kind=kind, format=format))


@mcp.tool()
def rag_sources_list(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> list[dict[str, Any]]:
    """List registered 1C source trees."""
    return _RAG.list_sources()


@mcp.tool()
def rag_snapshot_create(
    name: Annotated[str, "Snapshot name, e.g. MS+Platform42"],
    base: Annotated[str | None, "Base source name"] = None,
    extensions: Annotated[list[str] | None, "Extension source names in layer order"] = None,
    sources: Annotated[list[str] | None, "Standalone ordered source names"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Create/update an effective configuration snapshot from base + extensions."""
    return _RAG.create_snapshot(name=name, base=base, extensions=extensions, sources=sources)


@mcp.tool()
def rag_snapshots_list(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> list[dict[str, Any]]:
    """List effective RAG snapshots and their layers."""
    return _RAG.list_snapshots()


@mcp.tool()
def rag_index_build(
    snapshot: Annotated[str | None, "Snapshot name/id to index"] = None,
    source: Annotated[str | None, "Single source name to index"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Build/rebuild the SQLite/FTS RAG index for a source or snapshot."""
    return _RAG.build_index(snapshot=snapshot, source=source)


@mcp.tool()
def rag_lookup_object(
    object: Annotated[str, "1C object full name or metadata name"],
    snapshot: Annotated[str | None, "Optional snapshot name/id"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Look up an indexed 1C metadata object, including navigation, fields and forms."""
    return _record_tool(session_id, "rag_lookup_object", {"object": object, "snapshot": snapshot}, _RAG.lookup_object(object=object, snapshot=snapshot))


@mcp.tool()
def rag_query(
    query: Annotated[str, "Natural-language or metadata search query"],
    snapshot: Annotated[str | None, "Optional snapshot name/id"] = None,
    limit: Annotated[int, "Max result count"] = 10,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Search business terms and full-text RAG chunks in the local SQLite index."""
    return _RAG.query(text=query, snapshot=snapshot, limit=limit)


@mcp.tool()
def bridge_status(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Return WebSocket bridge status and URL for the 1C manager processor."""
    sess = _session(session_id)
    return sess.bridge.status()


@mcp.tool()
def runtime_status(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Report the live state of the autonomous server + test-manager processes."""
    sess = _session(session_id)
    rt = sess.runtime
    if rt is None:
        return {"running": False, "session_id": session_id}
    return {
        "running": True,
        "session_id": session_id,
        "infobase": str(rt.infobase.db_path),
        "server": {
            "pid": rt.server_process.pid if rt.server_process else None,
            "alive": rt.server_process.poll() is None if rt.server_process else False,
            "address": rt.server.address,
            "port": rt.server.port,
            "listening": runtime.is_port_listening(rt.server.address, rt.server.port),
        },
        "manager": {
            "pid": rt.manager_process.pid if rt.manager_process else None,
            "alive": rt.manager_process.poll() is None if rt.manager_process else False,
        },
        "log_dir": str(rt.log_dir),
    }



@mcp.tool()
async def start_session(
    base_url: Annotated[str, "1C infobase URL (for version detection and later connect_test_client)"] = "",
    username: Annotated[str, "1C username for later connect_test_client"] = "",
    password: Annotated[str, "1C password for later connect_test_client"] = "",
    version: Annotated[str, "1C platform version override (auto-detected from base_url if empty)"] = "",
    session_id: Annotated[str, "Desired session id; auto-generated when empty"] = "",
    start_pipeline: Annotated[bool, "Also create infobase, start ibsrv and test-manager"] = True,
) -> dict[str, Any]:
    """One-shot session factory: auto-picks display, ports, platform version,
    starts Xvfb, and optionally launches the full 1C pipeline."""
    resolved_version = version or None
    if not resolved_version and base_url:
        resolved_version = _platform.detect_version_from_url(base_url)
    if resolved_version:
        _platform.require_websocket_support(resolved_version)

    platform_dir = _platform.find_platform_dir(resolved_version)
    actual_version = platform_dir.name
    logging.info("start_session: version=%s platform_dir=%s", actual_version, platform_dir)

    display = runtime.find_free_display()
    ws_port = runtime.find_free_port(runtime.DEFAULT_WS_PORT)
    ibsrv_port = runtime.find_free_port(runtime.DEFAULT_IBSRV_HTTP_PORT)
    testclient_port = runtime.find_free_port(runtime.DEFAULT_TEST_CLIENT_PORT)

    sid = session_id or _new_session_id()
    if sid in _SESSIONS:
        raise ValueError(f"Session {sid!r} already exists.")

    data_dir = Path(f"/tmp/onec-mcp/sessions/{sid}")
    sess = SessionState(
        session_id=sid, display=display, platform_dir=platform_dir,
        ws_port=ws_port, ibsrv_port=ibsrv_port, testclient_port=testclient_port,
        data_dir=data_dir, base_url=base_url, username=username, password=password
    )
    sess.xvfb_process = runtime.start_xvfb(display)
    await sess.bridge.start()
    logging.info("start_session: bridge listening on ws://127.0.0.1:%s", ws_port)

    pipeline_info: dict[str, Any] = {}
    if start_pipeline:
        log_dir = sess.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        ibsrv_address = "127.0.0.1"
        ibsrv_name = "mcp"

        runtime_state = _resolve_runtime(
            platform_dir=platform_dir, db_path=sess.db_path,
            ibsrv_address=ibsrv_address, ibsrv_port=ibsrv_port,
            ibsrv_name=ibsrv_name, server_data_dir=sess.server_data_dir,
        )
        runtime_state.log_dir = log_dir

        runtime.create_file_infobase(sess.ibcmd, sess.db_path)
        cf_path = runtime.DEFAULT_CF_BUILT
        if not Path(cf_path).exists():
            raise FileNotFoundError(f"MCPTestManager.cf not found at {cf_path}.")
        runtime.load_configuration(sess.ibcmd, sess.db_path, cf_path)

        server_proc = runtime.start_standalone_server(
            ibsrv=sess.ibsrv, address=ibsrv_address, port=ibsrv_port,
            name=ibsrv_name, data_dir=sess.server_data_dir,
            log_dir=log_dir, db_path=sess.db_path,
        )
        runtime_state.server_process = server_proc

        ws_url = runtime.build_thin_client_ws_url(ibsrv_address, ibsrv_port)
        manager_proc = runtime.start_thin_test_manager(
            onec_path=sess.onec_thin, ws_url=ws_url,
            websocket_url=sess.bridge.url, log_dir=log_dir, display=display,
        )
        runtime_state.manager_process = manager_proc
        sess.runtime = runtime_state

        # --- Client ibsrv (second autonomous server for /TESTCLIENT) ---
        client_ibsrv_port = runtime.find_free_port(runtime.DEFAULT_IBSRV_HTTP_PORT + 100)
        client_log_dir = sess.client_log_dir
        client_log_dir.mkdir(parents=True, exist_ok=True)
        client_ibsrv_name = "mcpclient"

        runtime.create_file_infobase(sess.ibcmd, sess.client_db_path)
        runtime.load_configuration(sess.ibcmd, sess.client_db_path, cf_path)

        client_server_proc = runtime.start_standalone_server(
            ibsrv=sess.ibsrv, address=ibsrv_address, port=client_ibsrv_port,
            name=client_ibsrv_name, data_dir=sess.client_server_data_dir,
            log_dir=client_log_dir, db_path=sess.client_db_path,
        )
        sess.client_ibsrv_port = client_ibsrv_port
        sess.client_runtime = runtime.ManagerRuntime(
            infobase=runtime.InfobaseSpec(db_path=sess.client_db_path),
            server=runtime.StandaloneServerSpec(
                address=ibsrv_address, port=client_ibsrv_port,
                name=client_ibsrv_name, data_dir=sess.client_server_data_dir,
            ),
            manager=runtime.TestManagerSpec(onec_path=sess.onec_thin),
            server_process=client_server_proc,
            log_dir=client_log_dir,
        )

        client_ws_url = runtime.build_thin_client_ws_url(ibsrv_address, client_ibsrv_port)
        pipeline_info = {
            "manager_pid": manager_proc.pid, "ws_url": ws_url, "log_dir": str(log_dir),
            "client_ibsrv_port": client_ibsrv_port, "client_ws_url": client_ws_url,
            "client_base_url": f"http://{ibsrv_address}:{client_ibsrv_port}",
        }

    _SESSIONS[sid] = sess
    return {
        "session_id": sid, "version": actual_version,
        "platform_dir": str(platform_dir), "display": display,
        "ws_port": ws_port, "ibsrv_port": ibsrv_port,
        "testclient_port": testclient_port, "data_dir": str(data_dir),
        "bridge_url": sess.bridge.url, **pipeline_info,
    }


@mcp.tool()
async def stop_session(
    session_id: Annotated[str, "Session to tear down"] = DEFAULT_SESSION,
    clean_data: Annotated[bool, "Remove the session data directory"] = False,
) -> dict[str, Any]:
    """Tear down a session: stop test-manager, ibsrv, Xvfb, bridge."""
    try:
        sess = _SESSIONS.pop(session_id)
    except KeyError:
        return {"stopped": False, "reason": f"session {session_id!r} not found"}
    stopped: list[str] = []
    if sess.runtime is not None:
        runtime.kill_process(sess.runtime.manager_process, "test-manager")
        runtime.kill_process(sess.runtime.server_process, "ibsrv")
        stopped.append("pipeline")
    if sess.client_runtime is not None:
        runtime.kill_process(sess.client_runtime.server_process, "ibsrv-client")
        stopped.append("client-ibsrv")
    try:
        await sess.bridge.stop()
        stopped.append("bridge")
    except Exception as exc:
        logging.warning("stop_session: bridge.stop failed: %s", exc)
    runtime.stop_xvfb(sess.xvfb_process)
    stopped.append("xvfb")
    if clean_data and sess.data_dir.exists():
        shutil.rmtree(sess.data_dir, ignore_errors=True)
        stopped.append("data_dir")
    return {"stopped": True, "session_id": session_id, "components": stopped}


@mcp.tool()
def launch_manager(
    platform_dir: Annotated[str, "1C platform directory override"] = "",
    db_path: Annotated[str, "File infobase path override"] = "",
    server_data_dir: Annotated[str, "Standalone server data dir override"] = "",
    ibsrv_address: Annotated[str, "Standalone server listen address"] = "127.0.0.1",
    ibsrv_port: Annotated[int, "Standalone server HTTP port (0 = use session's)"] = 0,
    ibsrv_name: Annotated[str, "Standalone server infobase name"] = "mcp",
    create_infobase: Annotated[bool, "(Re)create the file infobase"] = True,
    load_configuration: Annotated[bool, "Load .cf into the infobase"] = True,
    cf_path: Annotated[str, "Path to .cf file"] = runtime.DEFAULT_CF_BUILT,
    start_standalone: Annotated[bool, "Start ibsrv"] = True,
    use_thin_client: Annotated[bool, "Use 1cv8c /WS"] = True,
    extra_args: Annotated[str, "Extra raw 1C command-line arguments"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Launch the 1C test infrastructure inside an existing session."""
    sess = _session(session_id)
    pd = Path(platform_dir) if platform_dir else sess.platform_dir
    db = Path(db_path) if db_path else sess.db_path
    sdd = Path(server_data_dir) if server_data_dir else sess.server_data_dir
    port = ibsrv_port if ibsrv_port else sess.ibsrv_port

    if sess.runtime is not None:
        runtime.kill_process(sess.runtime.manager_process, "test-manager")
        runtime.kill_process(sess.runtime.server_process, "ibsrv")
        sess.runtime = None

    runtime_state = _resolve_runtime(pd, db, ibsrv_address, port, ibsrv_name, sdd)
    log_dir = sess.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_state.log_dir = log_dir

    ibcmd = str(pd / "ibcmd")
    ibsrv = str(pd / "ibsrv")
    onec_thin = str(pd / "1cv8c")

    if create_infobase:
        runtime.create_file_infobase(ibcmd, db)
    if load_configuration and cf_path:
        runtime.load_configuration(ibcmd, db, cf_path)
    if start_standalone:
        server_proc = runtime.start_standalone_server(
            ibsrv=ibsrv, address=ibsrv_address, port=port,
            name=ibsrv_name, data_dir=sdd, log_dir=log_dir, db_path=db,
        )
        runtime_state.server_process = server_proc

    if use_thin_client:
        ws_url = runtime.build_thin_client_ws_url(ibsrv_address, port)
        argv = runtime.build_thin_test_manager_argv(onec_thin, ws_url, sess.bridge.url)
        manager_proc = runtime.start_thin_test_manager(
            onec_path=onec_thin, ws_url=ws_url,
            websocket_url=sess.bridge.url, log_dir=log_dir, display=sess.display,
        )
    else:
        raise ValueError("Thick-client fallback removed; use thin /WS /TESTMANAGER")

    if extra_args:
        argv.extend(shlex.split(extra_args))
    runtime_state.manager_process = manager_proc
    sess.runtime = runtime_state
    return {
        "pid": manager_proc.pid, "argv": argv,
        "websocket_url": sess.bridge.url, "ws_url": ws_url, "log_dir": str(log_dir),
    }


@mcp.tool()
def export_eventlog(
    platform_dir: Annotated[str, "1C platform dir override"] = "",
    eventlog_dir: Annotated[str, "Event log directory"] = "",
    out_path: Annotated[str, "Output file path"] = "",
    from_time: Annotated[str, "Start timestamp"] = "",
    to_time: Annotated[str, "End timestamp"] = "",
    fmt: Annotated[str, "Export format: json or xml"] = "json",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Export the 1C event log via ibcmd eventlog export."""
    sess = _session(session_id)
    ibcmd = str(Path(platform_dir) / "ibcmd") if platform_dir else sess.ibcmd
    if sess.runtime is not None and not eventlog_dir:
        eventlog_path = sess.runtime.server.data_dir / "log-data"
        log_dir = sess.runtime.log_dir
    else:
        eventlog_path = Path(eventlog_dir or "/home/openclaw/.1cv8/1C/1cv8/standalone-server/log-data")
        log_dir = sess.log_dir
    if not out_path:
        out = log_dir / f"eventlog_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
    else:
        out = Path(out_path)
    exported = runtime.export_eventlog(ibcmd, eventlog_path, out, from_time or None, to_time or None, fmt)
    return {"path": str(exported), "format": fmt, "eventlog_dir": str(eventlog_path)}


@mcp.tool()
def stop_manager(
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Tear down the manager + autonomous server for a session."""
    sess = _session(session_id)
    if sess.runtime is None:
        return {"stopped": False, "reason": "no runtime"}
    rt = sess.runtime
    sess.runtime = None
    runtime.kill_process(rt.manager_process, "test-manager")
    runtime.kill_process(rt.server_process, "ibsrv")
    # Also stop client ibsrv if running
    if sess.client_runtime is not None:
        client_rt = sess.client_runtime
        sess.client_runtime = None
        runtime.kill_process(client_rt.server_process, "ibsrv-client")
    return {"stopped": True}


@mcp.tool()
async def connect_test_client(
    base_url: Annotated[str, "1C infobase URL"] = "",
    username: Annotated[str, "1C username"] = "",
    password: Annotated[str, "1C password"] = "",
    port: Annotated[int, "Test client TCP port (0 = use session's)"] = 0,
    onec_path: Annotated[str, "1cv8c path override"] = "",
    host: Annotated[str, "Host where the test client runs"] = "localhost",
    extra_args: Annotated[str, "Extra raw 1C command-line arguments"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Launch 1cv8c /TESTCLIENT inside the manager processor and connect."""
    sess = _session(session_id)
    r_base = base_url or sess.base_url
    r_user = username or sess.username
    r_pass = password if password else sess.password
    r_port = port if port else sess.testclient_port
    r_onec = onec_path or sess.onec_thin
    # If no base_url provided, use the local client ibsrv (second autonomous server)
    if not r_base and sess.client_ibsrv_port:
        r_base = f"http://127.0.0.1:{sess.client_ibsrv_port}"
        r_user = r_user or "Admin"
        r_pass = r_pass or ""
    if not r_base or not r_user:
        raise ValueError("base_url and username are required")
    params = {"base_url": r_base, "username": r_user, "password": r_pass,
              "port": r_port, "onec_path": r_onec, "host": host, "extra_args": extra_args}
    result: Any
    try:
        result = await sess.bridge.call("connect_test_client", params)
    except Exception as exc:
        _auto_maximize_on_error(sess.display)
        raise exc
    _auto_maximize_test_client_result(result, sess.display)
    return _record_tool(session_id, "connect_test_client", params, result)


def _auto_maximize_on_error(display: str) -> None:
    try:
        __maximize_external_window(title_contains="", display=display, width=None, height=None)
    except Exception as exc:
        logging.debug("auto maximize after failed connect_test_client failed: %s", exc)


def _auto_maximize_test_client_result(result: Any, display: str) -> None:
    try:
        mr = __maximize_external_window(title_contains="", display=display, width=None, height=None)
        if isinstance(result, dict):
            result["auto_maximize"] = mr
    except Exception as exc:
        logging.debug("auto maximize after connect_test_client failed: %s", exc)
        if isinstance(result, dict):
            result["auto_maximize_error"] = str(exc)


@mcp.tool()
async def disconnect_test_client(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Disconnect the active 1C test client."""
    return await _bridge_call_recorded(session_id, "disconnect_test_client", {})


@mcp.tool()
async def active_window(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return active 1C test client window state."""
    return await _bridge_call_recorded(session_id, "active_window", {})


@mcp.tool()
async def maximize_test_client_window(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Activate and maximize the active 1C test client window via the 1C test-client API."""
    return await _bridge_call_recorded(session_id, "maximize_test_client_window", {})


@mcp.tool()
def maximize_test_client_external_window(
    title_contains: Annotated[str, "Window title substring; empty means choose likely 1C test-client window"] = "",
    display: Annotated[str, "X11 display override (empty = use session's)"] = "",
    width: Annotated[int, "Target width; 0 means display width"] = 0,
    height: Annotated[int, "Target height; 0 means display height"] = 0,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Maximize/resize the 1C test-client OS window: wmctrl -> xdotool -> Xlib fallback."""
    sess = _session(session_id)
    return __maximize_external_window(
        title_contains=title_contains,
        display=display or sess.display,
        width=width or None,
        height=height or None,
    )


@mcp.tool()
async def ui_tree(max_depth: Annotated[int, "Maximum UI tree depth"] = 4,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return a serialized tree of the active window/forms/elements."""
    return await _bridge_call_recorded(session_id, "ui_tree", {"max_depth": max_depth})


@mcp.tool()
async def command_bar(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return command interface/panel information for the active window/form when available."""
    return await _bridge_call_recorded(session_id, "command_bar", {})


@mcp.tool()
async def click_button(
    name: Annotated[str, "Button name or title text"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find a button in the active UI tree and press it."""
    return await _bridge_call_recorded(session_id, "click_button", {"name": name, "search_by_title": search_by_title})


@mcp.tool()
async def find_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object visible title/caption"] = "",
    type: Annotated[str, "1C test object type, e.g. ТестируемаяФорма"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find an object in the active UI hierarchy by name, title, or test object type."""
    return await _bridge_call_recorded(session_id, "find_object", {"name": name, "title": title, "type": type, "max_depth": max_depth})


@mcp.tool()
async def get_object(
    name: Annotated[str, "Object name pattern"] = "",
    title: Annotated[str, "Object title pattern"] = "",
    type: Annotated[str, "1C tested object type name, optional"] = "",
    timeout: Annotated[int, "Search timeout in seconds"] = 0,
    max_depth: Annotated[int, "Children depth to serialize"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Call TestedApplication.GetObject with optional wildcard name/title."""
    return await _bridge_call_recorded(session_id, "get_object", {"name": name, "title": title, "type": type, "timeout": timeout, "max_depth": max_depth})


@mcp.tool()
async def activate_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object visible title/caption"] = "",
    type: Annotated[str, "1C test object type"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find and activate a UI object in the active test client window."""
    return await _bridge_call_recorded(session_id, "activate_object", {"name": name, "title": title, "type": type, "max_depth": max_depth})


@mcp.tool()
async def execute_window_command(command: Annotated[str, "Explicit 1C navigation link, e.g. e1cib/list/..."],
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Execute an explicit 1C navigation link in the active test-client window.

    This tool intentionally rejects arbitrary form command names such as
    `ФормаНайти` or `СледующаяСтрока`. Use first-class MCP tools for form
    actions, table navigation, search/settings, and dynamic-list operations.
    """
    if not command.startswith("e1cib/"):
        raise ValueError(f"execute_window_command accepts only explicit e1cib/... navigation links; rejected: {command}")
    return await _bridge_call_recorded(session_id, "execute_window_command", {"command": command})


@mcp.tool()
async def current_element(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return the current active form element."""
    return await _bridge_call_recorded(session_id, "current_element", {})


@mcp.tool()
async def table_current_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return current row of a tested table as key/value text mapping."""
    return await _bridge_call_recorded(session_id, "table_current_row", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def table_selected_rows(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return selected rows of a tested table as key/value text mappings."""
    return await _bridge_call_recorded(session_id, "table_selected_rows", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def table_cell_text(
    cell: Annotated[str, "Column name or index; empty returns current cell"] = "",
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return cell text from a tested table via GetCellText()."""
    return await _bridge_call_recorded(session_id, "table_cell_text", {"name": name, "title": title, "cell": cell, "max_depth": max_depth})


@mcp.tool()
async def tabular_documents(
    include_text: Annotated[bool, "Include tabular document text"] = False,
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """List tabular document fields in the active window; optionally include their text."""
    return await _bridge_call_recorded(session_id, "tabular_documents", {"include_text": include_text, "max_depth": max_depth})


@mcp.tool()
async def tabular_document_text(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return text and dimensions of a tabular document field in the active window."""
    return await _bridge_call_recorded(session_id, "tabular_document_text", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def toggle_field(
    field_name: Annotated[str, "Field technical name"],
    value: Annotated[bool, "Desired checkbox value"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a checkbox-like field using the 1C test client API."""
    return await _bridge_call_recorded(session_id, "toggle_field", {"field_name": field_name, "value": value})


@mcp.tool()
async def set_field_value(
    field_name: Annotated[str, "Field technical name or title"],
    value: Annotated[str, "Value to set"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a value in a form field by name or title."""
    return await _bridge_call_recorded(session_id, "set_field_value", {"field_name": field_name, "value": value, "search_by_title": search_by_title})


@mcp.tool()
async def activate_field(
    field_name: Annotated[str, "Field technical name or title"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Activate/focus a form field by name or title."""
    return await _bridge_call_recorded(session_id, "activate_field", {"field_name": field_name, "search_by_title": search_by_title})


@mcp.tool()
async def choose_field_from_list(
    field_name: Annotated[str, "Reference field technical name or title"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open the standard selection form for a reference field."""
    return await _bridge_call_recorded(session_id, "choose_field_from_list", {"field_name": field_name, "search_by_title": search_by_title})


@mcp.tool()
async def choose_field_first_row(
    field_name: Annotated[str, "Reference field technical name or title"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open reference selection form and press its standard Choose button."""
    return await _bridge_call_recorded(session_id, "choose_field_first_row", {"field_name": field_name, "search_by_title": search_by_title})


@mcp.tool()
async def choose_current_row(
    name: Annotated[str, "Table technical name"] = "Список",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Choose the current row in the active selection/list form."""
    return await _bridge_call_recorded(session_id, "choose_current_row", {"name": name})


@mcp.tool()
async def table_rows(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    limit: Annotated[int, "Maximum rows to return"] = 50,
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return rows of a tested table as key/value mappings."""
    return await _bridge_call_recorded(session_id, "table_rows", {"name": name, "title": title, "limit": limit, "max_depth": max_depth})


@mcp.tool()
async def table_find_row(
    text: Annotated[str, "Substring to search in serialized row values"],
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    limit: Annotated[int, "Maximum rows to scan"] = 200,
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find a tested table row containing text."""
    return await _bridge_call_recorded(session_id, "table_find_row", {"name": name, "title": title, "text": text, "limit": limit, "max_depth": max_depth})


@mcp.tool()
async def table_goto_row(
    text: Annotated[str, "Substring to search in serialized row values"],
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    limit: Annotated[int, "Maximum rows to scan"] = 200,
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move cursor to the first tested table row containing text."""
    return await _bridge_call_recorded(session_id, "table_goto_row", {"name": name, "title": title, "text": text, "limit": limit, "max_depth": max_depth})


@mcp.tool()
async def table_move_row(
    direction: Annotated[str, "first | last | next | previous | expand | collapse | level_down | level_up"] = "next",
    steps: Annotated[int, "Number of row moves"] = 1,
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move current row using official test-client table navigation methods."""
    return await _bridge_call_recorded(session_id, "table_move_row",
        {"name": name, "title": title, "direction": direction, "steps": steps, "max_depth": max_depth},
    )


@mcp.tool()
async def table_add_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    max_depth: Annotated[int, "Maximum UI search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Add a row to the active tested table via the 1C test-client API (Insert / plus)."""
    return await _bridge_call_recorded(session_id, "table_add_row", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def table_set_field(
    table: Annotated[str, "Table technical name"],
    field: Annotated[str, "Column field technical name or title"],
    value: Annotated[str, "Value to set"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a field in the currently edited row of a form table."""
    return await _bridge_call_recorded(session_id, "table_set_field", {"table": table, "field": field, "value": value, "search_by_title": search_by_title})


@mcp.tool()
async def table_edit_info(
    table: Annotated[str, "Table technical name"] = "ДополнительнаяИнформация",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return diagnostics for currently edited table row and its child fields."""
    return await _bridge_call_recorded(session_id, "table_edit_info", {"table": table})


@mcp.tool()
async def dynamic_list_set_order(
    column_title: Annotated[str, "Visible column title to sort by using 1C УстановитьПорядок(<ЗаголовокКолонки>)"],
    name: Annotated[str, "Dynamic list/table technical name"] = "Список",
    title: Annotated[str, "Dynamic list/table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Sort a dynamic list by visible column title via the 1C test-client API."""
    return await _bridge_call_recorded(session_id, "dynamic_list_set_order", {"name": name, "title": title, "column_title": column_title, "max_depth": max_depth})


@mcp.tool()
async def dynamic_list_output(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries common Вывести список buttons"] = "",
    wait_seconds: Annotated[int, "Seconds to wait for the tabular document form"] = 2,
    max_depth: Annotated[int, "Maximum UI search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Press dynamic list 'Вывести список' button and return detected tabular document(s), including text when possible."""
    return await _bridge_call_recorded(session_id, "dynamic_list_output", {"button": button, "wait_seconds": wait_seconds, "max_depth": max_depth})


@mcp.tool()
async def dynamic_list_open_settings(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries common Настроить список buttons"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open dynamic-list settings (filter/order/conditional formatting/grouping) via 'Настроить список'."""
    return await _bridge_call_recorded(session_id, "dynamic_list_open_settings", {"button": button})


@mcp.tool()
async def dynamic_list_clear_settings(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries common reset/clear settings buttons"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Quickly reset/clear dynamic-list filter/order/conditional formatting/grouping settings."""
    return await _bridge_call_recorded(session_id, "dynamic_list_clear_settings", {"button": button})


@mcp.tool()
async def dynamic_list_open_form_settings(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries Изменить форму buttons"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open form customization via 'Изменить форму'."""
    return await _bridge_call_recorded(session_id, "dynamic_list_open_form_settings", {"button": button})


@mcp.tool()
async def create_new_item(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Create a new item in the active form (e.g. new catalog/dictionary entry)."""
    return await _bridge_call_recorded(session_id, "create_new_item", {})


@mcp.tool()
async def save_form(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Save/write the active form."""
    return await _bridge_call_recorded(session_id, "save_form", {})


@mcp.tool()
async def close_form(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Close the active form."""
    return await _bridge_call_recorded(session_id, "close_form", {})


def _active_x11_window_geometry(display: str | None = None) -> dict[str, int] | None:
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    try:
        tree = subprocess.run(
            ["xwininfo", "-root", "-tree"],
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    candidates: list[tuple[int, dict[str, int]]] = []
    for line in tree.splitlines():
        if '("1cv8c" "1cv8c")' not in line and '("1cv8" "1cv8")' not in line:
            continue
        match = re.search(r'0x[0-9a-f]+ "([^"]*)".*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
        if not match:
            continue
        title = match.group(1)
        width, height, left, top = map(int, match.groups()[1:])
        if width < 300 or height < 150:
            continue
        if left < 0 or top < 0:
            continue
        priority = 0
        if title and title not in {"1cv8c", "1cv8", "Конфигурация"}:
            priority += 100
        if "1С:Предприятие" in title:
            priority += 50
        if any(word in title for word in ("создание", "Инструменты", "Шаблон", "Сессия", "DEV-AREA")):
            priority += 25
        priority += min(width * height // 10000, 50)
        candidates.append((priority, {"left": left, "top": top, "width": width, "height": height, "title": title}))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


@mcp.tool()
def recording_start(
    output_dir: Annotated[str, "Directory for frames/slides/media"] = "build/recordings/session",
    display: Annotated[str, "X11 display to capture"] = "",
    window: Annotated[bool, "Capture best 1C window instead of whole display"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Start automatic evidence recording: each recorded tool call gets screenshot + annotated slide."""
    return _session(session_id).recorder.start(output_dir=output_dir, display=display, window=window)


@mcp.tool()
def recording_capture(
    action: Annotated[str, "Manual step/action label"],
    note: Annotated[str, "Text to put into the slide"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Manually capture a screenshot/slide in the active recording session."""
    event = _session(session_id).recorder.capture(_screenshot_for_recorder, action, {"note": note}, {"note": note})
    return event or {"active": False}


@mcp.tool()
def recording_stop(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Stop recording and build PDF/GIF/MP4 from captured slides."""
    return _session(session_id).recorder.stop()


@mcp.tool()
def screenshot(
    path: Annotated[str, "Output image path"] = "build/screenshots/screenshot.png",
    window: Annotated[bool, "Capture the best visible 1C window instead of the whole virtual display"] = True,
    display: Annotated[str, "X11 display override (empty = use session's)"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Capture a screenshot from the MCP host."""
    return _do_screenshot(path, window, display, session_id)


def _do_screenshot(path, window, display, session_id=DEFAULT_SESSION):
    sess = _session(session_id)
    resolved = display or sess.display
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import mss
        import mss.tools
        if resolved:
            os.environ["DISPLAY"] = resolved
        geometry = _active_x11_window_geometry(resolved) if window else None
        with mss.mss() as sct:
            monitor = geometry or sct.monitors[0]
            image = sct.grab(monitor)
            mss.tools.to_png(image.rgb, image.size, output=str(output_path))
    except ImportError as exc:
        raise RuntimeError("Install screenshot extra: pip install '.[screenshot]' or mss") from exc
    data = output_path.read_bytes()
    result = {"path": str(output_path), "bytes": len(data), "base64": base64.b64encode(data).decode("ascii")}
    if window:
        result["window_geometry"] = geometry
    return result


def _active_x11_window_geometry(display: str | None = None) -> dict[str, int] | None:
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    try:
        tree = subprocess.run(
            ["xwininfo", "-root", "-tree"],
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    candidates: list[tuple[int, dict[str, int]]] = []
    for line in tree.splitlines():
        if '("1cv8c" "1cv8c")' not in line and '("1cv8" "1cv8")' not in line:
            continue
        match = re.search(r'0x[0-9a-f]+ "([^"]*)".*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
        if not match:
            continue
        title = match.group(1)
        width, height, left, top = map(int, match.groups()[1:])
        if width < 300 or height < 150:
            continue
        if left < 0 or top < 0:
            continue
        priority = 0
        if title and title not in {"1cv8c", "1cv8", "Конфигурация"}:
            priority += 100
        if "1С:Предприятие" in title:
            priority += 50
        if any(word in title for word in ("создание", "Инструменты", "Шаблон", "Сессия", "DEV-AREA")):
            priority += 25
        priority += min(width * height // 10000, 50)
        candidates.append((priority, {"left": left, "top": top, "width": width, "height": height, "title": title}))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


@mcp.tool()
def recording_start(
    output_dir: Annotated[str, "Directory for frames/slides/media"] = "build/recordings/session",
    display: Annotated[str, "X11 display to capture"] = "",
    window: Annotated[bool, "Capture best 1C window instead of whole display"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Start automatic evidence recording: each recorded tool call gets screenshot + annotated slide."""
    return _session(session_id).recorder.start(output_dir=output_dir, display=display, window=window)


@mcp.tool()
def recording_capture(
    action: Annotated[str, "Manual step/action label"],
    note: Annotated[str, "Text to put into the slide"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Manually capture a screenshot/slide in the active recording session."""
    event = _session(session_id).recorder.capture(_screenshot_for_recorder, action, {"note": note}, {"note": note})
    return event or {"active": False}


@mcp.tool()
def recording_stop(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Stop recording and build PDF/GIF/MP4 from captured slides."""
    return _session(session_id).recorder.stop()


@mcp.tool()
def screenshot(
    path: Annotated[str, "Output image path"] = "build/screenshots/screenshot.png",
    window: Annotated[bool, "Capture the best visible 1C window instead of the whole virtual display"] = True,
    display: Annotated[str, "X11 display to inspect/capture"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Capture a screenshot from the MCP host. Defaults to the active visible 1C window."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore

        if display:
            os.environ["DISPLAY"] = display
        geometry = _active_x11_window_geometry(display or None) if window else None
        with mss.mss() as sct:
            monitor = geometry or sct.monitors[0]
            image = sct.grab(monitor)
            mss.tools.to_png(image.rgb, image.size, output=str(output_path))
    except ImportError as exc:
        raise RuntimeError("Install screenshot extra: pip install '.[screenshot]' or mss") from exc

    data = output_path.read_bytes()
    result = {"path": str(output_path), "bytes": len(data), "base64": base64.b64encode(data).decode("ascii")}
    if window:
        result["window_geometry"] = geometry
    return result


async def _run() -> None:
    try:
        await mcp.run_stdio_async()
    finally:
        for sid, sess in list(_SESSIONS.items()):
            if sess.runtime is not None:
                runtime.kill_process(sess.runtime.manager_process, "test-manager")
                runtime.kill_process(sess.runtime.server_process, "ibsrv")
            try:
                await sess.bridge.stop()
            except Exception:
                pass
            runtime.stop_xvfb(sess.xvfb_process)
        _SESSIONS.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for 1C:Enterprise test manager")
    parser.add_argument("--log-level", default=os.getenv("ONEC_MCP_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    asyncio.run(_run())


if __name__ == "__main__":
    main()
