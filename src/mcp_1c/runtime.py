"""1C test infrastructure launchers for the MCP bridge.

This module owns:
  * file infobase creation via `ibcmd infobase create`
  * .cf configuration loading (`ibcmd config load`)
  * the autonomous 1C server (`ibsrv`) HTTP-gate lifecycle
  * thin-client (`1cv8c /WS ws://host:port`) launch in /TESTMANAGER mode
  * test-manager (thin client, `/TESTMANAGER`) launch and log wiring

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

DEFAULT_ONEC_VERSION = "8.3.27.1606"
DEFAULT_WS_PORT = 8765
DEFAULT_IBSRV_HTTP_PORT = 8314
DEFAULT_TEST_CLIENT_PORT = 1538
DEFAULT_IBSRV_DIRECT_PORT = 1541
DEFAULT_DISPLAY_START = 99
DEFAULT_DISPLAY_RES = "1280x1024x24"
DEFAULT_PLATFORM_DIR = f"/opt/1cv8/x86_64/{DEFAULT_ONEC_VERSION}"
DEFAULT_THICK_CLIENT = f"{DEFAULT_PLATFORM_DIR}/1cv8"
DEFAULT_THIN_CLIENT = f"{DEFAULT_PLATFORM_DIR}/1cv8c"
DEFAULT_IBCMD = f"{DEFAULT_PLATFORM_DIR}/ibcmd"
DEFAULT_IBSRV = f"{DEFAULT_PLATFORM_DIR}/ibsrv"
DEFAULT_IB_PATH = "/tmp/onec-mcp/ib"
DEFAULT_IBSRV_DATA = "/tmp/onec-mcp/ibsrv-data"
DEFAULT_CF_BUILT = "build/MCPTestManager.cf"
DEFAULT_CLIENT_CF_BUILT = "build/MCPTestClient.cf"


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


def find_free_display(start: int = DEFAULT_DISPLAY_START) -> str:
    """Find the first free Xvfb display starting from :99."""
    for n in range(start, 200):
        path = f"/tmp/.X11-unix/X{n}"
        if not os.path.exists(path):
            return f":{n}"
    raise RuntimeError("No free X display found")


def find_free_port(start: int = DEFAULT_WS_PORT, end: int | None = None) -> int:
    """Find a free TCP port in the range start..start+500 (or start..end)."""
    end = end or start + 500
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}..{end}")


def start_xvfb(display: str, resolution: str = DEFAULT_DISPLAY_RES) -> subprocess.Popen:
    """Start Xvfb for the given display and return the Popen handle."""
    num = display.lstrip(":")
    xvfb_bin = shutil.which("Xvfb") or "/usr/bin/Xvfb"
    if not Path(xvfb_bin).exists():
        raise FileNotFoundError("Xvfb not found; install it to run headless sessions")
    proc = subprocess.Popen(
        [xvfb_bin, display, "-screen", "0", resolution, "-ac", "+extension", "GLX", "+render", "-noreset"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Give Xvfb a moment; caller should poll DISPLAY if needed.
    time.sleep(0.5)
    LOGGER.info("Xvfb started on display %s (pid=%s)", display, proc.pid)
    return proc


def stop_xvfb(proc: subprocess.Popen | None) -> None:
    """Stop the Xvfb process started by start_xvfb."""
    kill_process(proc, "Xvfb")


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
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        LOGGER.info("Stopped %s (pid=%s) via SIGTERM", label, proc.pid)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
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

    The default flow uses `MCPTestManager.cf` which embeds the MCP
    processor in the configuration.
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
    direct_regport: int | None = None,
    direct_range: tuple[int, int] | None = None,
) -> subprocess.Popen:
    """Start the autonomous 1C server (ibsrv) and wait for the HTTP-gate.

    `direct_regport` overrides the direct-connect gateway port (default
    1541 for current platform builds).  When running two ibsrv instances on the same
    host, each must use a different direct-regport.

    `direct_range` must also be unique for concurrently running autonomous
    servers.  The platform default 1560:1591 is shared, and otherwise several
    ibsrv processes can fail with "Ошибка открытия порта '156x' шлюза
    Конфигуратора" while the HTTP port itself looks free.
    """
    if not Path(ibsrv).exists():
        raise FileNotFoundError(f"ibsrv not found: {ibsrv}")
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ibsrv.log"
    log_handle = open(log_path, "ab", buffering=0)
    LOGGER.info("Starting ibsrv (data=%s, name=%s, address=%s, port=%s, direct_regport=%s)", data_dir, name, address, port, direct_regport or "default")
    config_path = ensure_standalone_config(
        data_dir=data_dir,
        db_path=db_path or Path(DEFAULT_IB_PATH),
        address=address,
        port=port,
        name=name,
    )
    argv = [
        ibsrv,
        f"--data={data_dir}",
        f"--config={config_path}",
        f"--name={name}",
    ]
    if direct_regport is not None:
        argv.append(f"--direct-regport={direct_regport}")
    if direct_range is not None:
        argv.append(f"--direct-range={direct_range[0]}:{direct_range[1]}")
    proc = subprocess.Popen(
        argv,
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
        "/DisableStartupDialogs",
        f"/WS{ws_url}",
        "/Lru",
        "/VLru_RU",
        "/TESTMANAGER",
        "/C",
        websocket_url,
    ]


def start_thin_test_manager(
    onec_path: str,
    ws_url: str,
    websocket_url: str,
    log_dir: Path,
    display: str = ":99",
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
        env={**os.environ, "DISPLAY": display},
    )
    LOGGER.info("Test manager launched (pid=%s); log=%s", proc.pid, log_path)
    return proc
