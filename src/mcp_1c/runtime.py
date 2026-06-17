"""1C test infrastructure launchers for the MCP bridge.

This module owns:
  * file infobase creation via `ibcmd infobase create`
  * .cf configuration loading (`ibcmd config load`)
  * the autonomous 1C server (`ibsrv`) HTTP-gate lifecycle
  * thin-client (`1cv8c /WS ws://host:port`) launch in /TESTMANAGER mode
  * test-manager (thick client, `/TESTMANAGER`) launch and log wiring

The test client itself is launched from inside the 1C manager processor
(BSL `КомандаСистемы`/`НачатьЗапускПриложения`) so this module does not
deal with 1cv8c /TESTCLIENT.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_ONEC_VERSION = "8.5.1.1150"
DEFAULT_PLATFORM_DIR = f"/opt/1cv8/x86_64/{DEFAULT_ONEC_VERSION}"
DEFAULT_THICK_CLIENT = f"{DEFAULT_PLATFORM_DIR}/1cv8"
DEFAULT_THIN_CLIENT = f"{DEFAULT_PLATFORM_DIR}/1cv8c"
DEFAULT_IBCMD = f"{DEFAULT_PLATFORM_DIR}/ibcmd"
DEFAULT_IBSRV = f"{DEFAULT_PLATFORM_DIR}/ibsrv"
DEFAULT_TEST_CLIENT_PORT = 1538
DEFAULT_IBSRV_HTTP_PORT = 8314
DEFAULT_IB_PATH = "/tmp/onec-mcp/ib"
DEFAULT_IBSRV_DATA = "/tmp/onec-mcp/ibsrv-data"
DEFAULT_CF_BUILT = "build/MCPTestManager.cf"


def _default_inbox_dir() -> Path:
    return Path(os.getenv("ONEC_MCP_DATA_DIR", "/tmp/onec-mcp"))


@dataclass(slots=True)
class InfobaseSpec:
    """File infobase used by the test-manager and the ibsrv HTTP-gate."""

    db_path: Path


@dataclass(slots=True)
class StandaloneServerSpec:
    """Standalone 1C server (ibsrv) HTTP-gate connection parameters.

    ibsrv publishes a single TCP port that the thin client reaches via
    `/WS"http://host:port"` (note: the *http://* form, not `ws://`). The
    URL must NOT carry a trailing slash; the HTTP-base path is configured
    inside ibsrv itself.

    The standalone server requires that the infobase it serves has a
    stable name (`--name=`) — the thin client must refer to the same
    name when connecting.
    """

    address: str = "127.0.0.1"
    port: int = DEFAULT_IBSRV_HTTP_PORT
    name: str = "mcp"
    data_dir: Path = field(default_factory=lambda: Path(DEFAULT_IBSRV_DATA))


@dataclass(slots=True)
class TestManagerSpec:
    """Test-manager (thin client) connection to the standalone server."""

    onec_path: str = DEFAULT_THIN_CLIENT
    processor: str = "build/MCPTestManager.epf"


@dataclass(slots=True)
class ManagerRuntime:
    """Tracks the live processes owned by the MCP server."""

    infobase: InfobaseSpec
    server: StandaloneServerSpec
    manager: TestManagerSpec
    server_process: subprocess.Popen | None = None
    manager_process: subprocess.Popen | None = None
    log_dir: Path = field(default_factory=lambda: _default_inbox_dir() / "logs")
    extra: dict[str, Any] = field(default_factory=dict)


def is_port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def wait_for_port(host: str, port: int, timeout: float = 30.0, poll: float = 0.5) -> bool:
    """Block until host:port accepts TCP connections or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_listening(host, port):
            return True
        time.sleep(poll)
    return False


def kill_process(proc: subprocess.Popen | None, label: str) -> None:
    """Terminate a subprocess, escalating to SIGKILL if needed."""
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        LOGGER.info("Stopped %s (pid=%s) via SIGTERM", label, proc.pid)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
        LOGGER.warning("Killed %s (pid=%s) via SIGKILL", label, proc.pid)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        LOGGER.error("Failed to stop %s (pid=%s)", label, proc.pid)


def create_file_infobase(
    ibcmd: str,
    db_path: Path,
    timeout: float = 60.0,
) -> None:
    """Create a fresh file infobase via `ibcmd infobase create --create-database`.

    Wipes any existing directory at db_path first. The DB is created
    without a configuration — a .cf is loaded into it next via
    `load_configuration`.
    """
    if db_path.exists():
        shutil.rmtree(db_path, ignore_errors=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        ibcmd,
        "infobase",
        "create",
        "--db-path=" + str(db_path),
        "--create-database",
    ]
    LOGGER.info("Creating file infobase at %s via %s", db_path, ibcmd)
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ibcmd infobase create failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    LOGGER.info("File infobase created: %s", db_path)


def load_configuration(
    ibcmd: str,
    db_path: Path,
    cf_path: str | Path,
    timeout: float = 120.0,
) -> None:
    """Load a `.cf` configuration into an existing file infobase.

    The default flow uses the built-in `MCPTestManager.cf` which
    embeds the MCP processor in the configuration and registers a
    `ПриНачалеРаботыСистемы` handler that opens it. A legacy path
    loads a bare `.cf` template (e.g. v8runner's empty 1Cv8.cf) and
    relies on an externally-loaded EPF instead.
    """
    if not Path(cf_path).exists():
        raise FileNotFoundError(f"Configuration file not found: {cf_path}")
    argv = [
        ibcmd,
        "config",
        "load",
        f"--db-path={db_path}",
        str(cf_path),
    ]
    LOGGER.info("Loading configuration %s into %s", cf_path, db_path)
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ibcmd config load failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    LOGGER.info("Configuration loaded from %s into %s", cf_path, db_path)
    apply_configuration(ibcmd=ibcmd, db_path=db_path, timeout=timeout)


def apply_configuration(ibcmd: str, db_path: Path, timeout: float = 120.0) -> None:
    """Apply the loaded configuration to the infobase database configuration."""
    argv = [
        ibcmd,
        "config",
        "apply",
        "--db-path=" + str(db_path),
        "--force",
        "--dynamic=force",
        "--session-terminate=force",
    ]
    LOGGER.info("Applying configuration to %s", db_path)
    completed = subprocess.run(argv, input="3\n", capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ibcmd config apply failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    LOGGER.info("Configuration applied to %s", db_path)


def ensure_standalone_config(
    data_dir: Path,
    db_path: Path,
    address: str,
    port: int,
    name: str,
) -> Path:
    """Write a minimal standalone-server YAML config for the MCP infobase."""
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "standalone.yaml"
    config_path.write_text(
        "server:\n"
        f"  address: {address}\n"
        f"  port: {port}\n"
        "database:\n"
        f"  path: {db_path}\n"
        "infobase:\n"
        f"  name: {name}\n"
        "http:\n"
        "  base: /\n",
        encoding="utf-8",
    )
    return config_path


def start_standalone_server(
    ibsrv: str,
    address: str,
    port: int,
    name: str,
    data_dir: Path,
    log_dir: Path,
    db_path: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.Popen:
    """Start the autonomous 1C server (ibsrv) and wait for the HTTP-gate.

    `ibsrv` is started with NO command-line overrides; it picks up its
    data directory, HTTP port, and DB path entirely from defaults (it
    uses the per-user `~/.1cv8/1C/1cv8/standalone-server/` tree). This
    matches the documented "default invocation" form and is the only
    way ibsrv reliably opens the HTTP socket on this build.

    `--name` is required: the thin client refers to the infobase by
    this name when connecting via `/WS"http://host:port"`.
    """
    if not Path(ibsrv).exists():
        raise FileNotFoundError(f"ibsrv not found: {ibsrv}")
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ibsrv.log"
    log_handle = open(log_path, "ab", buffering=0)
    LOGGER.info("Starting ibsrv (data=%s, name=%s, address=%s, port=%s)", data_dir, name, address, port)
    config_path = ensure_standalone_config(
        data_dir=data_dir,
        db_path=db_path or Path(DEFAULT_IB_PATH),
        address=address,
        port=port,
        name=name,
    )
    proc = subprocess.Popen(
        [
            ibsrv,
            f"--data={data_dir}",
            f"--config={config_path}",
            f"--name={name}",
        ],
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    if not wait_for_port(address, port, timeout=timeout):
        kill_process(proc, "ibsrv")
        raise RuntimeError(
            f"ibsrv did not start listening on {address}:{port} within {timeout}s. "
            f"See log: {log_path}"
        )
    LOGGER.info("ibsrv is listening on %s:%s (pid=%s, name=%s)", address, port, proc.pid, name)
    return proc


def export_eventlog(
    ibcmd: str,
    eventlog_dir: Path,
    out_path: Path,
    from_time: str | None = None,
    to_time: str | None = None,
    fmt: str = "json",
    timeout: float = 30.0,
) -> Path:
    """Export the 1C autonomous-server event log to a file.

    The `ibcmd eventlog export` command works directly with the
    event-log directory and does not connect to the infobase. This is
    the preferred diagnostic path for manager/client startup errors in
    the autonomous-server scenario.
    """
    if not Path(ibcmd).exists():
        raise FileNotFoundError(f"ibcmd not found: {ibcmd}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        ibcmd,
        "eventlog",
        "export",
        f"--format={fmt}",
        f"--out={out_path}",
    ]
    if from_time:
        argv.append(f"--from={from_time}")
    if to_time:
        argv.append(f"--to={to_time}")
    argv.append(str(eventlog_dir))
    LOGGER.info("Exporting 1C event log: %s", " ".join(argv))
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ibcmd eventlog export failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    return out_path


def build_thin_client_ws_url(address: str, port: int) -> str:
    """Build the `/WS` argument value for `1cv8c` against the ibsrv HTTP-gate.

    The format is `/WS"http://host:port"` (no trailing slash). The
    infobase name is selected by `--name=<mcp>` on the ibsrv side; the
    thin client does not need to specify it here.
    """
    return f'"http://{address}:{port}"'


def build_thin_test_manager_argv(
    onec_path: str,
    ws_url: str,
    websocket_url: str,
) -> list[str]:
    """Compose argv for 1cv8c /WS <ws-url> /TESTMANAGER /C <ws-bridge>.

    The MCP test-manager infobase is intentionally built without any
    user accounts (see the configuration's application module), so
    `/N`/`/P` are not passed: the thin client starts directly in the
    test-manager form. The MCP processor is opened automatically by
    the application module's `ПриНачалеРаботыСистемы` handler.

    `/Execute` is intentionally not used here — the processor lives
    inside the configuration and is started by the application module
    rather than by the client command line.
    """
    return [
        onec_path,
        "ENTERPRISE",
        f"/WS{ws_url}",
        "/TESTMANAGER",
    ]


def start_thin_test_manager(
    onec_path: str,
    ws_url: str,
    websocket_url: str,
    log_dir: Path,
) -> subprocess.Popen:
    """Start the 1C test-manager (thin client, /WS ws://ibsv) and return Popen."""
    if not Path(onec_path).exists():
        raise FileNotFoundError(f"1cv8c (thin) not found: {onec_path}")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "test_manager.log"
    log_handle = open(log_path, "ab", buffering=0)
    argv = build_thin_test_manager_argv(
        onec_path, ws_url, websocket_url
    )
    LOGGER.info("Starting thin test manager: %s", " ".join(argv))
    proc = subprocess.Popen(
        argv,
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
    )
    LOGGER.info("Test manager launched (pid=%s); log=%s", proc.pid, log_path)
    return proc


def build_test_manager_argv(
    onec_path: str,
    db_path: str,
    processor: str,
    websocket_url: str,
) -> list[str]:
    """Compose argv for 1cv8 ENTERPRISE /F <ib> /TESTMANAGER /Execute <epf> /C <ws>.

    Legacy /F-based path used while the ibsrv HTTP-gate integration
    is being stabilised. /Execute is split into two argv elements
    (see build_thin_test_manager_argv for the rationale).
    """
    return [
        onec_path,
        "ENTERPRISE",
        "/F",
        db_path,
        "/TESTMANAGER",
        "/Execute",
        processor,
        "/C",
        websocket_url,
        "/DisableStartupDialogs",
    ]


def start_test_manager(
    onec_path: str,
    db_path: str,
    processor: str,
    websocket_url: str,
    log_dir: Path,
) -> subprocess.Popen:
    """Start the 1C test-manager (thick client) and return the Popen.

    The thick client runs in /TESTMANAGER mode, opens the EPF in its own
    form module, and the EPF opens the WebSocket bridge. The MCP caller
    is responsible for waiting on the WS bridge to know the manager is up.
    """
    if not Path(onec_path).exists():
        raise FileNotFoundError(f"1cv8 (thick) not found: {onec_path}")
    if not Path(processor).exists():
        raise FileNotFoundError(f"Test manager EPF not found: {processor}")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "test_manager.log"
    log_handle = open(log_path, "ab", buffering=0)
    argv = build_test_manager_argv(onec_path, db_path, processor, websocket_url)
    LOGGER.info("Starting test manager: %s", " ".join(argv))
    proc = subprocess.Popen(
        argv,
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
    )
    LOGGER.info("Test manager launched (pid=%s); log=%s", proc.pid, log_path)
    return proc
