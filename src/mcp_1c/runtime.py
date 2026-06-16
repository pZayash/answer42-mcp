"""1C test infrastructure launchers for the MCP bridge.

This module owns:
  * file infobase creation for the test-manager via `ibcmd infobase create`
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
DEFAULT_IBCMD = f"{DEFAULT_PLATFORM_DIR}/ibcmd"
DEFAULT_TEST_CLIENT_PORT = 1538


def _default_inbox_dir() -> Path:
    return Path(os.getenv("ONEC_MCP_DATA_DIR", "/tmp/onec-mcp"))


@dataclass(slots=True)
class InfobaseSpec:
    """File infobase used by the test-manager (thick client, /TESTMANAGER)."""

    db_path: Path


@dataclass(slots=True)
class TestManagerSpec:
    """Test-manager (thick client) connection to the infobase."""

    onec_path: str = DEFAULT_THICK_CLIENT
    processor: str = "build/MCPTestManager.epf"


@dataclass(slots=True)
class ManagerRuntime:
    """Tracks the live processes owned by the MCP server."""

    infobase: InfobaseSpec
    manager: TestManagerSpec
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
    onec_path: str,
    db_path: Path,
    timeout: float = 60.0,
) -> None:
    """Create a fresh file infobase via `1cv8 CREATEINFOBASE`.

    Wipes any existing directory at db_path first. We use `1cv8` instead of
    `ibcmd infobase create` because the latter produces an empty DB without
    a configuration tree, which makes /TESTMANAGER fail to load the EPF
    (the thick client requires a configured IB to enter /TESTMANAGER).
    """
    if db_path.exists():
        shutil.rmtree(db_path, ignore_errors=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        onec_path,
        "CREATEINFOBASE",
        f"File={db_path}",
    ]
    LOGGER.info("Creating file infobase at %s via %s", db_path, onec_path)
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"1cv8 CREATEINFOBASE failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    LOGGER.info("File infobase created: %s", db_path)


def build_test_manager_argv(
    onec_path: str,
    db_path: str,
    processor: str,
    websocket_url: str,
) -> list[str]:
    """Compose argv for 1cv8 ENTERPRISE /F <ib> /TESTMANAGER /Execute <epf> /C <ws>.

    Important: `/Execute <path>` must be passed as two argv elements (a
    space-separated pair, not `/Execute=path`). 1C 8.5.1.1150 treats the
    `=` form as the *value* of the parameter (literally `=path`),
    which results in a missing-file error at startup.
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
