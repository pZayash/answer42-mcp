from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
import shlex
import subprocess
import re
import hashlib
import importlib.resources
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass, field
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from . import os_support
from .protocol import MANAGER_PROTOCOL_VERSION
from . import platform as _platform
from . import runtime
from . import skill_installer
from . import release_helper
from .bridge import OneCBridgeServer
from .credentials import get_store as _credentials_store
from .rag import RagService
from .window_control import active_1c_window_geometry as _active_windows_1c_window_geometry
from .window_control import maximize_test_client_window as _maximize_external_window
from .recorder import RecordingSession

mcp = FastMCP("Answer42")
_RAG = RagService()
_RAG_ENABLED = os.getenv("ONEC_MCP_DISABLE_RAG", "").strip().lower() not in {"1", "true", "yes", "on"}
_CREDENTIAL_STORE_ENABLED = os.getenv("ONEC_MCP_DISABLE_CREDENTIAL_STORE", "").strip().lower() not in {"1", "true", "yes", "on"}
_DEV_TOOLS_ENABLED = os.getenv("ONEC_MCP_DEV_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_SESSION = "default"
STATE_FILE = os_support.runtime_path("ONEC_MCP_SESSION_STATE", "sessions.json")
AUTORELOAD_PID_FILE = os_support.runtime_path("ONEC_MCP_AUTORELOAD_PID_FILE", "answer42-autoreload.pid")
AUTORELOAD_LOG_FILE = os_support.runtime_path("ONEC_MCP_AUTORELOAD_LOG_FILE", "answer42-autoreload.log")
RAG_BINDINGS_FILE = os_support.runtime_path("ONEC_MCP_RAG_BINDINGS", "rag-bindings.json")

_PORT_ALLOC_LOCK = os_support.runtime_path("ONEC_MCP_PORT_ALLOC_LOCK", "port-allocation.lock")
_SHARED_FILE_SERVERS_FILE = os_support.runtime_path("ONEC_MCP_SHARED_FILE_SERVERS", "shared-file-servers.json")
_SHARED_FILE_SERVERS_LOCK = os_support.runtime_path("ONEC_MCP_SHARED_FILE_SERVERS_LOCK", "shared-file-servers.lock")
_METADATA_OBJECTS_CACHE: dict[str, dict[str, Any]] = {}


@contextmanager
def _port_allocation_lock():
    with os_support.file_lock(_PORT_ALLOC_LOCK):
        yield


@contextmanager
def _shared_file_servers_lock():
    with os_support.file_lock(_SHARED_FILE_SERVERS_LOCK):
        yield


_PORT_BAND_SIZE = 20
_PORT_SCAN_WIDTH = 10


@dataclass(slots=True)
class SharedFileServer:
    """Shared ibsrv instance for one file infobase.

    Several /TESTCLIENT windows can connect to the same autonomous server.
    The server is stopped when the last owning session releases it.
    """

    key: str
    db_path: Path
    data_dir: Path
    log_dir: Path
    address: str
    port: int
    direct_regport: int
    name: str
    platform_dir: Path
    pid: int
    process: subprocess.Popen | None = None
    owners: set[str] = field(default_factory=set)

    @property
    def base_url(self) -> str:
        return f"http://{self.address}:{self.port}"


_SHARED_FILE_SERVERS: dict[str, SharedFileServer] = {}


def _load_shared_file_registry() -> dict[str, Any]:
    if not _SHARED_FILE_SERVERS_FILE.exists():
        return {}
    try:
        return json.loads(_SHARED_FILE_SERVERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_shared_file_registry(registry: dict[str, Any]) -> None:
    _SHARED_FILE_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SHARED_FILE_SERVERS_FILE.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def _shared_file_key(kind: str, platform_dir: Path, db_path: Path) -> str:
    return f"{kind}:{platform_dir.resolve()}:{db_path.resolve()}"


def _shared_file_server_alive(shared: SharedFileServer) -> bool:
    if shared.process is not None and shared.process.poll() is not None:
        return False
    if not _pid_is_running(shared.pid):
        return False
    return runtime.is_port_listening(shared.address, shared.port)


def _shared_file_entry_alive(entry: dict[str, Any]) -> bool:
    return _pid_is_running(int(entry.get("pid") or 0)) and runtime.is_port_listening(
        str(entry.get("address") or "127.0.0.1"),
        int(entry.get("port") or 0),
    )


def _shared_file_from_entry(key: str, entry: dict[str, Any]) -> SharedFileServer:
    return SharedFileServer(
        key=key,
        db_path=Path(str(entry["db_path"])),
        data_dir=Path(str(entry["data_dir"])),
        log_dir=Path(str(entry["log_dir"])),
        address=str(entry.get("address") or "127.0.0.1"),
        port=int(entry["port"]),
        direct_regport=int(entry.get("direct_regport") or 0),
        name=str(entry.get("name") or "mcpfileclient"),
        platform_dir=Path(str(entry["platform_dir"])),
        pid=int(entry["pid"]),
        process=None,
        owners=set(map(str, entry.get("owners") or [])),
    )


def _shared_file_to_entry(shared: SharedFileServer) -> dict[str, Any]:
    return {
        "db_path": str(shared.db_path),
        "data_dir": str(shared.data_dir),
        "log_dir": str(shared.log_dir),
        "address": shared.address,
        "port": shared.port,
        "direct_regport": shared.direct_regport,
        "name": shared.name,
        "platform_dir": str(shared.platform_dir),
        "pid": shared.pid,
        "owners": sorted(shared.owners),
    }


def _kill_pid_group(pid: int, label: str, timeout: float = 5.0) -> None:
    """Terminate a process tree by pid when we do not own a Popen handle."""
    if pid <= 0 or not _pid_is_running(pid):
        return
    if os_support.IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=max(1, int(timeout) + 5),
            )
            logging.info("Stopped %s (pid=%s) via taskkill", label, pid)
        except Exception as exc:
            logging.warning("Failed to stop %s (pid=%s) via taskkill: %s", label, pid, exc)
        return

    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            logging.info("Stopped %s (pid=%s) via SIGTERM", label, pid)
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            return
    logging.warning("Killed %s (pid=%s) via SIGKILL", label, pid)


def _shared_file_paths(kind: str, digest: str) -> tuple[Path, Path, Path]:
    root = Path(os.getenv("ONEC_MCP_SHARED_FILE_SERVERS_DIR", str(os_support.default_data_dir() / "shared-file-servers")))
    base = root / kind / digest
    return base / "ib", base / "ibsrv-data", base / "logs"


def _acquire_shared_file_server(
    *,
    owner_session_id: str,
    kind: str,
    platform_dir: Path,
    ibcmd: str,
    ibsrv: str,
    db_path: Path,
    cf_path: str = "",
    create_if_missing: bool = False,
    port_seed: str = "",
    server_name: str = "mcpfileclient",
) -> SharedFileServer:
    """Start or reuse one ibsrv for a file infobase and add an owner ref.

    The registry is persisted under /tmp so parallel MCP server processes can
    share a single autonomous server for the same file infobase.
    """
    resolved_db_path = db_path.resolve()
    key = _shared_file_key(kind, platform_dir, resolved_db_path)
    with _shared_file_servers_lock():
        registry = _load_shared_file_registry()
        entry = registry.get(key)
        if isinstance(entry, dict) and _shared_file_entry_alive(entry):
            owners = set(map(str, entry.get("owners") or []))
            owners.add(owner_session_id)
            entry["owners"] = sorted(owners)
            registry[key] = entry
            _save_shared_file_registry(registry)
            shared = _shared_file_from_entry(key, entry)
            _SHARED_FILE_SERVERS[key] = shared
            return shared
        if entry:
            _kill_pid_group(int(entry.get("pid") or 0), f"shared-ibsrv-{kind}")
            registry.pop(key, None)
            _save_shared_file_registry(registry)

        if create_if_missing:
            cf = Path(cf_path) if cf_path else None
            if cf and not cf.exists():
                raise FileNotFoundError(f"Shared file infobase CF not found at {cf}.")
            if cf and resolved_db_path.exists() and not _cf_marker_matches(resolved_db_path, cf):
                logging.info("Shared file infobase %s has stale Answer42 CF marker; recreating", resolved_db_path)
                shutil.rmtree(resolved_db_path, ignore_errors=True)
            # Shared file infobases are intentionally persistent across several
            # MCP server processes.  If a previous process already created the
            # database but died before writing the registry entry, do not wipe it
            # here: just publish it through a fresh ibsrv unless its CF marker is stale.
            if not resolved_db_path.exists() or not any(resolved_db_path.iterdir()):
                runtime.create_file_infobase(ibcmd, resolved_db_path)
                if cf:
                    runtime.load_configuration(ibcmd, resolved_db_path, str(cf))
                    _write_cf_marker(resolved_db_path, cf)
        elif not resolved_db_path.exists():
            raise FileNotFoundError(f"File infobase directory not found: {resolved_db_path}")

        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        _, data_dir, log_dir = _shared_file_paths(kind, digest)
        port_offset = _stable_session_port_offset(port_seed or key)
        port = _find_session_port(runtime.DEFAULT_IBSRV_HTTP_PORT, port_offset, sub_offset=10)
        direct_regport = _find_session_port(runtime.DEFAULT_IBSRV_DIRECT_PORT, port_offset, sub_offset=10)
        proc = runtime.start_standalone_server(
            ibsrv=ibsrv,
            address="127.0.0.1",
            port=port,
            name=server_name,
            data_dir=data_dir,
            log_dir=log_dir,
            db_path=resolved_db_path,
            direct_regport=direct_regport,
            direct_range=_direct_range_from_regport(direct_regport),
        )
        shared = SharedFileServer(
            key=key,
            db_path=resolved_db_path,
            data_dir=data_dir,
            log_dir=log_dir,
            address="127.0.0.1",
            port=port,
            direct_regport=direct_regport,
            name=server_name,
            platform_dir=platform_dir,
            pid=proc.pid,
            process=proc,
            owners={owner_session_id},
        )
        registry[key] = _shared_file_to_entry(shared)
        _save_shared_file_registry(registry)
        _SHARED_FILE_SERVERS[key] = shared
        return shared


def _release_shared_file_server(key: str, owner_session_id: str) -> bool:
    """Release one owner ref. Return True when the shared server was stopped."""
    if not key:
        return False
    with _shared_file_servers_lock():
        registry = _load_shared_file_registry()
        entry = registry.get(key)
        if not isinstance(entry, dict):
            _SHARED_FILE_SERVERS.pop(key, None)
            return False
        owners = set(map(str, entry.get("owners") or []))
        owners.discard(owner_session_id)
        if owners and _shared_file_entry_alive(entry):
            entry["owners"] = sorted(owners)
            registry[key] = entry
            _save_shared_file_registry(registry)
            local = _SHARED_FILE_SERVERS.get(key)
            if local:
                local.owners = owners
            return False
        _kill_pid_group(int(entry.get("pid") or 0), "shared-ibsrv-file")
        registry.pop(key, None)
        _save_shared_file_registry(registry)
        _SHARED_FILE_SERVERS.pop(key, None)
        return True


def _stable_session_port_offset(session_id: str) -> int:
    digest = hashlib.sha1(session_id.encode("utf-8")).digest()
    return (int.from_bytes(digest[:2], "big") % 100) * _PORT_BAND_SIZE


def _find_session_port(base: int, offset: int, *, sub_offset: int = 0) -> int:
    start = base + offset + sub_offset
    return runtime.find_free_port(start, start + _PORT_SCAN_WIDTH)


def _direct_range_from_regport(direct_regport: int, width: int = 8) -> tuple[int, int]:
    """Return a small direct port range unique to this standalone server."""
    start = direct_regport + 20
    return start, start + width - 1


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
        self.client_ibsrv_port = 0  # to be set in start_session for the built-in demo client
        self.client_runtime: runtime.ManagerRuntime | None = None
        self.shared_client_key: str = ""  # shared ibsrv key for built-in demo client
        self.file_client_ibsrv_port = 0  # to be set when a file infobase target is served through ibsrv
        self.file_client_runtime: runtime.ManagerRuntime | None = None
        self.shared_file_key: str = ""  # shared ibsrv key for connection_mode=file
        self.xvfb_process: subprocess.Popen | None = None
        self.client_display: str = ""
        self.client_xvfb_process: subprocess.Popen | None = None
        self.runtime: runtime.ManagerRuntime | None = None
        self.test_client_process: subprocess.Popen | None = None
        self.bridge = OneCBridgeServer(
            host="127.0.0.1",
            port=ws_port,
            request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "60")),
        )
        self.recorder = RecordingSession()
        self.rag_snapshot: str = ""
        self.rag_enabled: bool = True
        self.idle_timeout_seconds: int = 60 * 60
        self.last_activity_monotonic: float = time.monotonic()

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "display": self.display,
            "client_display": self.client_display,
            "platform_dir": str(self.platform_dir),
            "ws_port": self.ws_port,
            "ibsrv_port": self.ibsrv_port,
            "testclient_port": self.testclient_port,
            "data_dir": str(self.data_dir),
            "base_url": self.base_url,
            "username": self.username,
            # Password is intentionally not persisted.
            "client_ibsrv_port": self.client_ibsrv_port,
            "shared_client_key": self.shared_client_key,
            "file_client_ibsrv_port": self.file_client_ibsrv_port,
            "shared_file_key": self.shared_file_key,
            "rag_snapshot": self.rag_snapshot,
            "rag_enabled": self.rag_enabled,
            "idle_timeout_seconds": self.idle_timeout_seconds,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SessionState":
        sess = cls(
            session_id=str(data["session_id"]),
            display=str(data["display"]),
            platform_dir=Path(str(data["platform_dir"])),
            ws_port=int(data["ws_port"]),
            ibsrv_port=int(data["ibsrv_port"]),
            testclient_port=int(data["testclient_port"]),
            data_dir=Path(str(data["data_dir"])),
            base_url=str(data.get("base_url", "")),
            username=str(data.get("username", "")),
            password="",
        )
        sess.client_ibsrv_port = int(data.get("client_ibsrv_port") or 0)
        sess.shared_client_key = str(data.get("shared_client_key") or "")
        sess.file_client_ibsrv_port = int(data.get("file_client_ibsrv_port") or 0)
        sess.shared_file_key = str(data.get("shared_file_key") or "")
        sess.client_display = str(data.get("client_display", ""))
        sess.rag_enabled = bool(data.get("rag_enabled", True))
        sess.idle_timeout_seconds = int(data.get("idle_timeout_seconds") or 60 * 60)
        return sess

    @property
    def onec_thin(self) -> str:
        return str(self.platform_dir / os_support.executable_name("1cv8c"))

    @property
    def ibcmd(self) -> str:
        return str(self.platform_dir / os_support.executable_name("ibcmd"))

    @property
    def ibsrv(self) -> str:
        return str(self.platform_dir / os_support.executable_name("ibsrv"))

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

    @property
    def file_client_server_data_dir(self) -> Path:
        return self.data_dir / "ibsrv-data-file-client"

    @property
    def file_client_log_dir(self) -> Path:
        return self.data_dir / "logs-file-client"


_SESSIONS: dict[str, SessionState] = {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _start_autoreload_watcher(dev: str = "", dev_restart_timeout: float = 30.0) -> None:
    """Start a detached dev autoreload watcher once per host.

    The watcher must outlive the current stdio MCP process: `openclaw mcp reload`
    disposes that process, and the next tool call starts a fresh one.  A pid file
    prevents every MCP start from creating another watcher.
    """

    if dev != "openclaw":
        logging.info("Answer42 autoreload watcher disabled: pass --dev openclaw to enable it")
        return

    if os.getenv("ONEC_MCP_AUTORELOAD", "1").lower() in {"0", "false", "no", "off"}:
        logging.info("Answer42 autoreload watcher disabled by ONEC_MCP_AUTORELOAD")
        return

    repo_root = Path(__file__).resolve().parents[2]
    watcher = repo_root / "scripts" / "openclaw_mcp_autoreload.py"
    if not watcher.exists():
        logging.warning("Autoreload watcher script not found: %s", watcher)
        return

    try:
        if AUTORELOAD_PID_FILE.exists():
            try:
                pid = int(AUTORELOAD_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if _pid_is_running(pid):
                logging.info("Autoreload watcher already running, pid=%s", pid)
                return
            AUTORELOAD_PID_FILE.unlink(missing_ok=True)

        AUTORELOAD_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTORELOAD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUTORELOAD_LOG_FILE.open("ab") as log:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(watcher),
                    "--root",
                    str(repo_root),
                    "--pid-file",
                    str(AUTORELOAD_PID_FILE),
                    "--dev-restart-timeout",
                    str(dev_restart_timeout),
                ],
                cwd=str(repo_root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **os_support.subprocess_creation_kwargs(),
            )
        AUTORELOAD_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        logging.info("Started autoreload watcher pid=%s log=%s", proc.pid, AUTORELOAD_LOG_FILE)
    except Exception as exc:
        logging.warning("Could not start autoreload watcher: %s", exc)


def _persist_sessions() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({sid: sess.to_json() for sid, sess in _SESSIONS.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _source_newer_than(target: Path, src_dir: Path) -> bool:
    if not target.exists():
        return True
    if not src_dir.exists():
        return False
    target_mtime = target.stat().st_mtime
    for path in src_dir.rglob("*"):
        if path.is_file() and path.stat().st_mtime > target_mtime:
            return True
    return False


def _packaged_cf_name(src_dir: str) -> str:
    normalized = Path(src_dir).as_posix().rstrip("/")
    if normalized.endswith("src/client_cf") or normalized == "src/client_cf":
        return "MCPTestClient.cf"
    return "MCPTestManager.cf"


def _file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _packaged_cf_path(src_dir: str) -> Path | None:
    cf_name = _packaged_cf_name(src_dir)
    try:
        resource = importlib.resources.files("mcp_1c.assets").joinpath(cf_name)
        if not resource.is_file():
            return None
        with importlib.resources.as_file(resource) as path:
            return Path(path)
    except Exception as exc:
        logging.debug("Packaged CF artifact %s is not available: %s", cf_name, exc)
        return None


def _copy_packaged_cf(target: Path, src_dir: str, *, force: bool = False) -> bool:
    cf_name = _packaged_cf_name(src_dir)
    source = _packaged_cf_path(src_dir)
    if source is None:
        return False
    try:
        if target.exists() and not force and _file_sha256(target) == _file_sha256(source):
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        logging.info("Copied packaged CF artifact %s to %s", cf_name, target)
        return True
    except Exception as exc:
        logging.debug("Could not copy packaged CF artifact %s: %s", cf_name, exc)
        return False


def _ensure_cf_built(out_cf: str, *, src_dir: str, platform_dir: Path) -> str:
    """Build a .cf artifact when XML sources are available; otherwise copy packaged CF."""
    target = Path(out_cf)
    source = Path(src_dir)
    script = Path(__file__).resolve().parents[2] / "scripts" / "build_cf.py"
    if not source.exists() or not script.exists():
        packaged = _packaged_cf_path(src_dir)
        if packaged is not None:
            force = not target.exists() or _file_sha256(target) != _file_sha256(packaged)
            if _copy_packaged_cf(target, src_dir, force=force):
                return str(target)
        if not source.exists():
            raise FileNotFoundError(f"CF source directory not found and no packaged CF is available: {source}")
        raise FileNotFoundError(f"CF build script not found and no packaged CF is available: {script}")
    if not _source_newer_than(target, source):
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ONEC_PLATFORM_DIR"] = str(platform_dir)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    logging.info("Building CF artifact %s from %s", target, source)
    completed = subprocess.run(
        [sys.executable, str(script), str(source), str(target)],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Failed to build CF artifact {target} from {source} "
            f"(rc={completed.returncode}):\n{completed.stdout}"
        )
    return str(target)


def _cf_marker_path(db_path: Path) -> Path:
    return db_path.parent / f".{db_path.name}.answer42-cf.json"


def _cf_marker_data(cf_path: Path | str) -> dict[str, str]:
    path = Path(cf_path)
    return {"answer42_version": __version__, "cf_sha256": _file_sha256(path), "cf_path": str(path)}


def _write_cf_marker(db_path: Path, cf_path: Path | str) -> None:
    marker = _cf_marker_path(db_path)
    marker.write_text(json.dumps(_cf_marker_data(cf_path), ensure_ascii=False, indent=2), encoding="utf-8")


def _cf_marker_matches(db_path: Path, cf_path: Path | str) -> bool:
    marker = _cf_marker_path(db_path)
    if not db_path.exists() or not marker.exists() or not Path(cf_path).exists():
        return False
    try:
        stored = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    expected = _cf_marker_data(cf_path)
    return stored.get("answer42_version") == expected["answer42_version"] and stored.get("cf_sha256") == expected["cf_sha256"]


def _normalize_base_url(url: str) -> str:
    return _platform.normalize_infobase_url(str(url or "").strip())


def _load_rag_bindings() -> dict[str, str]:
    if not RAG_BINDINGS_FILE.exists():
        return {}
    try:
        raw = json.loads(RAG_BINDINGS_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in dict(raw).items() if k and v}
    except Exception as exc:
        logging.warning("Could not read RAG bindings %s: %s", RAG_BINDINGS_FILE, exc)
        return {}


def _save_rag_binding(base_url: str, snapshot: str) -> None:
    key = _normalize_base_url(base_url)
    if not key or not snapshot:
        return
    data = _load_rag_bindings()
    data[key] = snapshot
    RAG_BINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RAG_BINDINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_for_base_url(base_url: str) -> str:
    return _load_rag_bindings().get(_normalize_base_url(base_url), "")


def _normalize_connection_mode(mode: str, value: str, base_url: str = "") -> str:
    requested = (mode or "auto").strip().lower()
    if requested in {"http", "url"}:
        return "ws"
    if requested in {"direct-file", "direct_file", "file-direct", "file_direct"}:
        return "file-direct"
    if requested in {"ws", "file", "server"}:
        return requested
    if requested not in {"", "auto"}:
        raise ValueError(f"Unsupported connection_mode: {mode!r}. Expected auto, ws, file, file-direct or server.")
    effective = (value or base_url or "").strip()
    lowered = effective.lower()
    if lowered.startswith(("http://", "https://")):
        return "ws"
    if effective and (Path(effective).is_absolute() or Path(effective, "1Cv8.1CD").exists() or Path(effective, "1cv8.1cd").exists()):
        return "file"
    if effective:
        return "server"
    return "ws"


def _touch_session(sess: SessionState) -> None:
    sess.last_activity_monotonic = time.monotonic()


async def _idle_watch_session(session_id: str) -> None:
    while True:
        await asyncio.sleep(30)
        sess = _SESSIONS.get(session_id)
        if sess is None:
            return
        timeout = max(1, int(sess.idle_timeout_seconds or 60 * 60))
        if time.monotonic() - sess.last_activity_monotonic >= timeout:
            logging.info("Session %s idle for %ss; stopping", session_id, timeout)
            try:
                await _stop_session_impl(session_id, clean_data=False)
            except Exception as exc:
                logging.warning("idle stop_session failed for %s: %s", session_id, exc)
            return


def _load_persisted_sessions() -> dict[str, SessionState]:
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("Could not read session state %s: %s", STATE_FILE, exc)
        return {}
    result: dict[str, SessionState] = {}
    for sid, data in raw.items():
        try:
            result[str(sid)] = SessionState.from_json(data)
        except Exception as exc:
            logging.warning("Could not restore session %s: %s", sid, exc)
    return result


def _session_process_substrings(sess: SessionState) -> list[str]:
    return [
        str(sess.data_dir),
        str(sess.server_data_dir),
        str(sess.client_server_data_dir),
        str(sess.file_client_server_data_dir),
        sess.bridge.url,
        f"-TPort{sess.testclient_port}",
        f"-TPort {sess.testclient_port}",
        f"TPort{sess.testclient_port}",
    ]


def _cleanup_stale_session_processes(sess: SessionState, reason: str) -> None:
    logging.info("Cleaning stale 1C processes for session %s: %s", sess.session_id, reason)
    os_support.kill_processes_by_command_substrings(
        _session_process_substrings(sess),
        f"Answer42 session {sess.session_id}",
        logging.getLogger(__name__),
    )


async def _cleanup_failed_start_session(sess: SessionState, reason: str) -> None:
    logging.info("Cleaning failed start_session %s: %s", sess.session_id, reason)
    try:
        await sess.bridge.call("disconnect_test_client", {})
    except Exception:
        pass
    runtime.kill_process(sess.test_client_process, "test-client")
    sess.test_client_process = None
    for rt, manager_label, server_label in (
        (sess.runtime, "test-manager", "ibsrv"),
        (sess.client_runtime, "test-manager-client", "ibsrv-client"),
        (sess.file_client_runtime, "test-manager-file-client", "ibsrv-file-client"),
    ):
        if rt is not None:
            runtime.kill_process(rt.manager_process, manager_label)
            runtime.kill_process(rt.server_process, server_label)
    try:
        await sess.bridge.stop()
    except Exception:
        pass
    runtime.stop_xvfb(sess.client_xvfb_process)
    runtime.stop_xvfb(sess.xvfb_process)
    _cleanup_stale_session_processes(sess, reason)
    if sess.data_dir.exists():
        shutil.rmtree(sess.data_dir, ignore_errors=True)
    _SESSIONS.pop(sess.session_id, None)
    _persist_sessions()


async def _ensure_restored_session(session_id: str = DEFAULT_SESSION) -> SessionState:
    if session_id in _SESSIONS:
        return _SESSIONS[session_id]
    persisted = _load_persisted_sessions()
    sess = persisted.get(session_id)
    if sess is None:
        raise ValueError(f"Session {session_id!r} not found. Call start_session first.")
    cf_path = _ensure_cf_built(runtime.DEFAULT_CF_BUILT, src_dir="src/cf", platform_dir=sess.platform_dir)
    if not _cf_marker_matches(sess.db_path, cf_path):
        _cleanup_stale_session_processes(sess, "stale manager CF marker")
        shutil.rmtree(sess.data_dir, ignore_errors=True)
        persisted.pop(session_id, None)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({sid: s.to_json() for sid, s in persisted.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError(
            f"Session {session_id!r} was created with an older Answer42 manager CF. "
            "Its temporary manager infobase was removed; call start_session again."
        )
    await sess.bridge.start()
    _SESSIONS[session_id] = sess
    logging.info("Restored MCP session %s from %s; bridge=%s", session_id, STATE_FILE, sess.bridge.url)
    return sess


async def _wait_for_bridge_connected(sess: SessionState, timeout: float = 45.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = sess.bridge.status()
        if status.get("connected"):
            return status
        await asyncio.sleep(0.5)
    status = sess.bridge.status()
    raise RuntimeError(
        f"1C bridge did not connect to {sess.bridge.url} within {timeout:g}s; "
        f"status={status}. Check test-manager log: {sess.log_dir / 'test_manager.log'}"
    )


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
    _touch_session(sess)
    text_only = action.startswith("rag_")
    try:
        sess.recorder.capture(
            lambda path, window, display: _screenshot_for_recorder(path, window, display, session_id),
            action, params, result, screenshot=not text_only,
        )
    except Exception as exc:
        logging.debug("recording capture failed for %s/%s: %s", session_id, action, exc)
    return result


def _rag_disabled_result(session_rag_enabled: bool = True) -> dict[str, Any]:
    reason = "rag_disabled_for_session" if _RAG_ENABLED and not session_rag_enabled else "rag_disabled"
    message = (
        "Built-in Answer42 RAG is disabled for this session. Start session with rag_enabled=true to use RAG tools."
        if reason == "rag_disabled_for_session"
        else "Built-in Answer42 RAG is disabled for this MCP server. Start without --disable-rag / ONEC_MCP_DISABLE_RAG to use RAG tools."
    )
    return {
        "available": False,
        "rag_enabled": False,
        "server_rag_enabled": _RAG_ENABLED,
        "session_rag_enabled": session_rag_enabled,
        "reason": reason,
        "message": message,
    }


def _rag_diagnostic(session_id: str, snapshot: str | None) -> dict[str, Any]:
    sess = _SESSIONS.get(session_id) or _load_persisted_sessions().get(session_id)
    session_snapshot = sess.rag_snapshot if sess is not None else ""
    session_rag_enabled = sess.rag_enabled if sess is not None else True
    configured_snapshot = bool(snapshot or session_snapshot)
    effective_snapshot = snapshot or session_snapshot or None
    diagnostics: list[dict[str, Any]] = []
    if not _RAG_ENABLED or not session_rag_enabled:
        diagnostics.append(_rag_disabled_result(session_rag_enabled))
    elif not configured_snapshot:
        diagnostics.append(
            {
                "code": "snapshot_not_specified",
                "message": "RAG snapshot is not specified for this session. Pass snapshot/rag_snapshot or create/bind one with rag_snapshot_create.",
            }
        )
    elif not _RAG.snapshot_has_index(effective_snapshot):
        diagnostics.append(
            {
                "code": "snapshot_has_no_indexed_data",
                "snapshot": effective_snapshot,
                "message": "RAG has no indexed metadata for this snapshot. Register sources with rag_source_add/rag_sources_add_from_directory and run rag_index_build.",
            }
        )
    return {
        "rag_enabled": _RAG_ENABLED,
        "snapshot": effective_snapshot,
        "session_snapshot": session_snapshot,
        "diagnostics": diagnostics,
    }


def _rag_require_enabled(session_id: str, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
    sess = _session(session_id)
    if _RAG_ENABLED and sess.rag_enabled:
        return None
    return _record_tool(session_id, action, params, _rag_disabled_result(sess.rag_enabled))


def _register_startup_rag_source_dirs(paths: list[str], prefix: str, recursive: bool, max_depth: int) -> None:
    env_paths = os.getenv("ONEC_MCP_RAG_SOURCE_DIRS", "")
    all_paths = list(paths)
    if env_paths:
        all_paths.extend(part.strip() for part in re.split(r"[:;]", env_paths) if part.strip())
    for path in all_paths:
        try:
            result = _RAG.add_sources_from_directory(path, prefix=prefix, recursive=recursive, max_depth=max_depth, watch=True)
            logging.info("RAG source dir registered: %s (%s sources)", path, result.get("registered_count"))
        except Exception as exc:
            logging.warning("Could not register RAG source dir %s: %s", path, exc)


def _norm_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _walk_ui_tree(node: Any):
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children") or []:
        yield from _walk_ui_tree(child)


def _metadata_name_from_navigation_url(url: str) -> str:
    """Best-effort conversion from e1cib/list/... to a metadata full name."""
    value = str(url or "")
    for prefix in ("e1cib/list/", "e1cib/data/", "e1cib/command/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.split("?", 1)[0]
    ru_to_en = {
        "Справочник.": "Catalog.",
        "Документ.": "Document.",
        "РегистрСведений.": "InformationRegister.",
        "РегистрНакопления.": "AccumulationRegister.",
        "РегистрБухгалтерии.": "AccountingRegister.",
        "РегистрРасчета.": "CalculationRegister.",
        "ПланВидовРасчета.": "ChartOfCalculationTypes.",
        "ПланСчетов.": "ChartOfAccounts.",
        "ПланВидовХарактеристик.": "ChartOfCharacteristicTypes.",
        "ПланОбмена.": "ExchangePlan.",
        "БизнесПроцесс.": "BusinessProcess.",
        "Задача.": "Task.",
        "Отчет.": "Report.",
        "Обработка.": "DataProcessor.",
        "Перечисление.": "Enum.",
        "Константа.": "Constant.",
        "ЖурналДокументов.": "DocumentJournal.",
        "Последовательность.": "Sequence.",
        "КритерийОтбора.": "FilterCriterion.",
        "Нумератор.": "NumberingPrefix.",
        "Подсистема.": "Subsystem.",
        "ОбщаяКоманда.": "CommonCommand.",
        "ГруппаКоманд.": "CommandGroup.",
        "ХранилищеНастроек.": "SettingsStorage.",
        "Роль.": "Role.",
        "РегламентноеЗадание.": "ScheduledJob.",
        "ОпределяемыйТип.": "DefinedType.",
        "ОбщийМодуль.": "CommonModule.",
        "ОбщаяФорма.": "CommonForm.",
        "ОбщийМакет.": "CommonTemplate.",
        "ОбщаяКартинка.": "CommonPicture.",
        "ПараметрСеанса.": "SessionParameter.",
        "ПодпискаНаСобытие.": "EventSubscription.",
        "ФункциональнаяОпция.": "FunctionalOption.",
        "ПараметрФункциональныхОпций.": "FunctionalOptionParameter.",
        "Интерфейс.": "Interface.",
        "Стиль.": "Style.",
        "Язык.": "Language.",
        "ПакетXDTO.": "XDTOPackage.",
        "WebСервис.": "WebService.",
        "WSСсылка.": "WSReference.",
        "HTTPСервис.": "HTTPService.",
        "СервисИнтеграции.": "IntegrationService.",
        "ВнешнийИсточникДанных.": "ExternalDataSource.",
        "Бот.": "Bot.",
        "WebSocketКлиент.": "WebSocketClient.",
        "ЦветПалитры.": "PaletteColor.",
        "ЭлементСтиля.": "StyleElement.",
        "ОбщийРеквизит.": "CommonAttribute.",
    }
    for ru, en in ru_to_en.items():
        if value.startswith(ru):
            return en + value[len(ru):]
    return value


def _metadata_name_to_rag_name(value: str) -> str:
    """Convert runtime/e1cib English metadata names to Russian names used by RAG."""
    text = str(value or "")
    en_to_ru = {
        "Catalog.": "Справочник.",
        "Document.": "Документ.",
        "InformationRegister.": "РегистрСведений.",
        "AccumulationRegister.": "РегистрНакопления.",
        "AccountingRegister.": "РегистрБухгалтерии.",
        "CalculationRegister.": "РегистрРасчета.",
        "ChartOfCalculationTypes.": "ПланВидовРасчета.",
        "ChartOfAccounts.": "ПланСчетов.",
        "ChartOfCharacteristicTypes.": "ПланВидовХарактеристик.",
        "ExchangePlan.": "ПланОбмена.",
        "BusinessProcess.": "БизнесПроцесс.",
        "Task.": "Задача.",
        "Report.": "Отчет.",
        "DataProcessor.": "Обработка.",
        "Enum.": "Перечисление.",
        "Constant.": "Константа.",
        "DocumentJournal.": "ЖурналДокументов.",
        "Sequence.": "Последовательность.",
        "FilterCriterion.": "КритерийОтбора.",
        "NumberingPrefix.": "Нумератор.",
        "Subsystem.": "Подсистема.",
        "CommonCommand.": "ОбщаяКоманда.",
        "CommandGroup.": "ГруппаКоманд.",
        "SettingsStorage.": "ХранилищеНастроек.",
        "Role.": "Роль.",
        "ScheduledJob.": "РегламентноеЗадание.",
        "DefinedType.": "ОпределяемыйТип.",
        "CommonModule.": "ОбщийМодуль.",
        "CommonForm.": "ОбщаяФорма.",
        "CommonTemplate.": "ОбщийМакет.",
        "CommonPicture.": "ОбщаяКартинка.",
        "SessionParameter.": "ПараметрСеанса.",
        "EventSubscription.": "ПодпискаНаСобытие.",
        "FunctionalOption.": "ФункциональнаяОпция.",
        "FunctionalOptionParameter.": "ПараметрФункциональныхОпций.",
        "Interface.": "Интерфейс.",
        "Style.": "Стиль.",
        "Language.": "Язык.",
        "XDTOPackage.": "ПакетXDTO.",
        "WebService.": "WebСервис.",
        "WSReference.": "WSСсылка.",
        "HTTPService.": "HTTPСервис.",
        "IntegrationService.": "СервисИнтеграции.",
        "ExternalDataSource.": "ВнешнийИсточникДанных.",
        "Bot.": "Бот.",
        "WebSocketClient.": "WebSocketКлиент.",
        "PaletteColor.": "ЦветПалитры.",
        "StyleElement.": "ЭлементСтиля.",
        "CommonAttribute.": "ОбщийРеквизит.",
    }
    for en, ru in en_to_ru.items():
        if text.startswith(en):
            return ru + text[len(en):]
    return text


def _rag_form_element_index(snapshot: str | int | None = None) -> dict[str, list[dict[str, Any]]]:
    try:
        items = _RAG.lookup_form_element_types(snapshot=snapshot).get("items", [])
    except Exception as exc:
        logging.debug("RAG form element lookup failed: %s", exc)
        return {}
    index: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for key in (item.get("name"), item.get("title"), item.get("data_path"), item.get("command_name")):
            nk = _norm_key(key)
            if nk:
                index.setdefault(nk, []).append(item)
    return index


def _pick_rag_element(ui_node: dict[str, Any], candidates: list[dict[str, Any]], active_object: str = "") -> dict[str, Any] | None:
    if not candidates:
        return None
    active_norm = _norm_key(active_object)
    form_name = _norm_key(ui_node.get("form_name"))

    def score(item: dict[str, Any]) -> int:
        value = 0
        if active_norm and active_norm in _norm_key(item.get("object_full_name")):
            value += 20
        if form_name and form_name == _norm_key(item.get("form_name")):
            value += 5
        if _norm_key(ui_node.get("name")) == _norm_key(item.get("name")):
            value += 3
        if _norm_key(ui_node.get("title")) == _norm_key(item.get("title")):
            value += 2
        return value

    return sorted(candidates, key=score, reverse=True)[0]


def _enrich_ui_tree_with_rag_types(tree: Any, snapshot: str | int | None = None) -> Any:
    if not isinstance(tree, dict):
        return tree
    index = _rag_form_element_index(snapshot=snapshot)
    if not index:
        return tree
    active_object = _metadata_name_from_navigation_url(str(tree.get("navigation_url") or ""))
    for node in _walk_ui_tree(tree):
        keys = [_norm_key(node.get("name")), _norm_key(node.get("title")), _norm_key(node.get("data_path"))]
        candidates: list[dict[str, Any]] = []
        seen: set[int] = set()
        for key in keys:
            for item in index.get(key, []):
                ident = id(item)
                if ident not in seen:
                    candidates.append(item)
                    seen.add(ident)
        item = _pick_rag_element(node, candidates, active_object)
        if item is None:
            continue
        value_type = item.get("attribute_type") or item.get("dcs_field_type") or item.get("element_type")
        node["rag_type"] = value_type
        node["rag"] = {
            "object": item.get("object_full_name"),
            "form": item.get("form_name"),
            "element_type": item.get("element_type"),
            "data_path": item.get("data_path"),
            "attribute_type": item.get("attribute_type"),
            "attribute_role": item.get("attribute_role"),
            "dcs_field_type": item.get("dcs_field_type"),
            "source_path": item.get("source_path"),
        }
    return tree


def _standard_attributes_for_kind(kind: str) -> list[dict[str, str]]:
    """Return standard (auto-generated) attributes for a metadata object kind.

    1C automatically generates standard attributes for every object type.
    These are always available in dynamic lists and auto-generated forms.
    """
    kind = str(kind or "").lower()
    common = [
        {"name": "Ссылка", "title": "Ссылка", "type": "Ref", "role": "standard"},
        {"name": "ПометкаУдаления", "title": "ПометкаУдаления", "type": "Boolean", "role": "standard"},
    ]
    if kind in ("справочник", "catalog"):
        return common + [
            {"name": "Код", "title": "Код", "type": "String|Number", "role": "standard"},
            {"name": "Наименование", "title": "Наименование", "type": "String", "role": "standard"},
            {"name": "Предопределенный", "title": "Предопределенный", "type": "Boolean", "role": "standard"},
            {"name": "ИмяПредопределенныхДанных", "title": "ИмяПредопределенныхДанных", "type": "String", "role": "standard"},
        ]
    if kind in ("документ", "document"):
        return common + [
            {"name": "Номер", "title": "Номер", "type": "String|Number", "role": "standard"},
            {"name": "Дата", "title": "Дата", "type": "Date", "role": "standard"},
            {"name": "Проведен", "title": "Проведен", "type": "Boolean", "role": "standard"},
        ]
    if kind in ("регистрсведений", "informationregister"):
        return common + [
            {"name": "Период", "title": "Период", "type": "Date", "role": "standard"},
        ]
    if kind in ("регистрнакопления", "accumulationregister"):
        return common + [
            {"name": "Период", "title": "Период", "type": "Date", "role": "standard"},
        ]
    return common


def _rag_fields_for_object(object_full_name: str, snapshot: str | int | None = None) -> list[dict[str, Any]]:
    if not object_full_name:
        return []
    rag_name = _metadata_name_to_rag_name(object_full_name)
    try:
        matches = _RAG.lookup_object(object=rag_name, snapshot=snapshot).get("matches", [])
    except Exception as exc:
        logging.debug("RAG object lookup failed for %s (%s): %s", object_full_name, rag_name, exc)
        return []
    fields: list[dict[str, Any]] = []
    for match in matches:
        kind = match.get("kind", "")
        for std in _standard_attributes_for_kind(kind):
            fields.append({
                "name": std["name"],
                "title": std["title"],
                "type": std["type"],
                "role": std["role"],
                "source": "rag.standard_attribute",
                "object": match.get("full_name"),
                "filter_supported": True,
                "order_supported": True,
            })
        for attr in match.get("attributes", []) or []:
            fields.append({
                "name": attr.get("name"),
                "title": attr.get("synonym") or attr.get("name"),
                "type": attr.get("type_name"),
                "role": attr.get("role"),
                "source": "rag.attribute",
                "object": match.get("full_name"),
                "filter_supported": True,
                "order_supported": True,
            })
        for dcf in match.get("data_composition_fields", []) or []:
            fields.append({
                "name": dcf.get("field") or dcf.get("data_path"),
                "title": dcf.get("title") or dcf.get("field") or dcf.get("data_path"),
                "type": dcf.get("field_type"),
                "data_path": dcf.get("data_path"),
                "expression": dcf.get("expression"),
                "source": "rag.dcs_field",
                "object": match.get("full_name"),
                "filter_supported": True,
                "order_supported": True,
            })
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for item in fields:
        key = (_norm_key(item.get("name")), _norm_key(item.get("title")))
        dedup.setdefault(key, item)
    return list(dedup.values())


async def _bridge_call_recorded(
    session_id: str,
    method: str,
    params: dict[str, Any],
) -> Any:
    sess = await _ensure_restored_session(session_id)
    await _wait_for_bridge_connected(sess, timeout=60.0)
    _touch_session(sess)
    result = await sess.bridge.call(method, params)
    return _record_tool(session_id, method, params, result)


def _screenshot_for_recorder(
    path: str,
    window: bool,
    display: str,
    session_id: str = DEFAULT_SESSION,
) -> dict[str, Any]:
    return screenshot(path=path, window=window, display=display, session_id=session_id)


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
            onec_path=str(platform_dir / os_support.executable_name("1cv8c")),
        ),
    )


# ---------------------------------------------------------------------------
# 1C configuration RAG / metadata index tools
# ---------------------------------------------------------------------------
# 1C configuration RAG / metadata index tools
# ---------------------------------------------------------------------------


@mcp.tool()
def rag_source_add(
    name: Annotated[str, "Logical source name, e.g. Configuration or Extension1"],
    path: Annotated[str, "Path to 1C source directory"],
    kind: Annotated[str, "base | extension | configuration | standalone"] = "standalone",
    format: Annotated[str, "auto | edt | designer_xml"] = "auto",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Register/update a 1C source tree; format is detected automatically when format=auto."""
    disabled = _rag_require_enabled(session_id, "rag_source_add", {"name": name, "path": path, "kind": kind, "format": format})
    if disabled is not None:
        return disabled
    detected = _RAG.detect_source(path) if format == "auto" else {"format": format}
    effective_format = str(detected.get("format") or format)
    result = _RAG.add_source(name=name, path=path, kind=kind, format=effective_format)
    if isinstance(result, dict):
        result["detected"] = detected
    return _record_tool(session_id, "rag_source_add", {"name": name, "path": path, "kind": kind, "format": format}, result)


@mcp.tool()
def rag_sources_add_from_directory(
    path: Annotated[str, "Directory where Answer42 should discover 1C configurations/extensions"],
    prefix: Annotated[str, "Optional prefix for generated source names"] = "",
    recursive: Annotated[bool, "Search child directories"] = True,
    max_depth: Annotated[int, "Maximum recursive discovery depth"] = 4,
    watch: Annotated[bool, "Remember this directory and auto-discover new sources on future RAG listings"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Discover/register 1C configuration and extension source directories under a root directory."""
    params = {"path": path, "prefix": prefix, "recursive": recursive, "max_depth": max_depth, "watch": watch}
    disabled = _rag_require_enabled(session_id, "rag_sources_add_from_directory", params)
    if disabled is not None:
        return disabled
    return _record_tool(session_id, "rag_sources_add_from_directory", params, _RAG.add_sources_from_directory(path, prefix=prefix, recursive=recursive, max_depth=max_depth, watch=watch))


@mcp.tool()
def rag_source_roots_list(*,
    sync: Annotated[bool, "Discover new sources in watched roots before listing"] = True,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """List RAG source discovery roots and optionally sync newly added configurations/extensions."""
    params = {"sync": sync}
    disabled = _rag_require_enabled(session_id, "rag_source_roots_list", params)
    if disabled is not None:
        return disabled
    result = {"watch_roots": _RAG.list_watch_roots()}
    if sync:
        result["sync"] = _RAG.sync_watch_roots()
        result["watch_roots"] = _RAG.list_watch_roots()
    return _record_tool(session_id, "rag_source_roots_list", params, result)


@mcp.tool()
def rag_available_configurations(
    sync: Annotated[bool, "Discover new sources in watched roots before listing"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """List RAG-available configurations, extensions, snapshots and discovery roots."""
    params = {"sync": sync}
    disabled = _rag_require_enabled(session_id, "rag_available_configurations", params)
    if disabled is not None:
        return disabled
    result = _RAG.available_configurations(sync=sync)
    sess = _session(session_id)
    result["session_snapshot"] = sess.rag_snapshot
    result["diagnostics"] = _rag_diagnostic(session_id, sess.rag_snapshot).get("diagnostics", [])
    return _record_tool(session_id, "rag_available_configurations", params, result)


@mcp.tool()
async def rag_session_autoconfigure(
    snapshot_name: Annotated[str, "Optional snapshot name; defaults to runtime configuration plus matched extensions"] = "",
    build_index: Annotated[bool, "Build/rebuild RAG index for the created snapshot"] = True,
    sync_sources: Annotated[bool, "Discover new sources from watched roots before matching"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Auto-configure the session RAG snapshot from the live 1C configuration and installed extensions.

    The tool reads the live main configuration/version from `ВерсииПодсистем`, reads
    installed extensions from `ВерсииРасширений`, matches them against registered
    RAG sources by metadata name and version, falls back to the nearest available
    version with warnings, creates a snapshot, attaches it to the session, and
    optionally builds its index.
    """
    params = {"snapshot_name": snapshot_name, "build_index": build_index, "sync_sources": sync_sources}
    disabled = _rag_require_enabled(session_id, "rag_session_autoconfigure", params)
    if disabled is not None:
        return disabled

    available = _RAG.available_configurations(sync=sync_sources)
    sources = [*available.get("configurations", []), *available.get("extensions", []), *available.get("other_sources", [])]
    base_sources = [src for src in sources if str(src.get("kind") or "") in {"base", "configuration", "standalone"}]
    extension_sources = [src for src in sources if str(src.get("kind") or "") in {"extension", "standalone"}]

    config = await configuration_name_version_from_subsystems(session_id=session_id)
    extensions_info = await installed_extensions_from_catalog(session_id=session_id)
    config_name = str(config.get("configuration_name") or "")
    config_version = str(config.get("configuration_version") or "")
    installed_extensions = extensions_info.get("extensions", []) if isinstance(extensions_info, dict) else []

    warnings: list[str] = []
    base_match = _best_rag_source_match(config_name, config_version, base_sources, None)
    if not base_match.get("matched"):
        raise ValueError(f"RAG source for runtime configuration {config_name!r} {config_version!r} not found")
    if base_match.get("warning"):
        warnings.append(str(base_match["warning"]))
    base_candidate = base_match["candidate"]
    base_source_name = str(base_candidate["source"].get("name") or "")

    extension_source_names: list[str] = []
    extension_matches: list[dict[str, Any]] = []
    used_extension_sources: set[str] = set()
    for extension in installed_extensions:
        ext_name = str(extension.get("name") or "")
        ext_version = str(extension.get("version") or "")
        match = _best_rag_source_match(ext_name, ext_version, extension_sources, None)
        public_match = _rag_match_public(match) | {"runtime_name": ext_name, "runtime_version": ext_version}
        extension_matches.append(public_match)
        if not match.get("matched"):
            warnings.append(f"RAG source for extension {ext_name!r} {ext_version!r} not found; extension was not added to snapshot.")
            continue
        if match.get("warning"):
            warnings.append(str(match["warning"]))
        source_name = str(match["candidate"]["source"].get("name") or "")
        if source_name and source_name not in used_extension_sources:
            extension_source_names.append(source_name)
            used_extension_sources.add(source_name)

    effective_snapshot_name = snapshot_name or "+".join([_safe_snapshot_part(config_name), *(_safe_snapshot_part(ext.get("name", "")) for ext in installed_extensions)])
    if len(effective_snapshot_name) > 180:
        digest = hashlib.sha1(effective_snapshot_name.encode("utf-8")).hexdigest()[:10]
        effective_snapshot_name = f"{_safe_snapshot_part(config_name)}+extensions-{digest}"

    snapshot = _RAG.create_snapshot(name=effective_snapshot_name, base=base_source_name, extensions=extension_source_names)
    sess = _session(session_id)
    sess.rag_snapshot = str(snapshot.get("name") or effective_snapshot_name)
    if sess.base_url:
        _save_rag_binding(sess.base_url, sess.rag_snapshot)
    _persist_sessions()

    index_result = _RAG.build_index(snapshot=sess.rag_snapshot) if build_index else None
    result = {
        "snapshot": sess.rag_snapshot,
        "snapshot_result": snapshot,
        "index_result": index_result,
        "warnings": warnings,
        "runtime_configuration": {"name": config_name, "version": config_version, "match": _rag_match_public(base_match)},
        "runtime_extensions": installed_extensions,
        "extension_matches": extension_matches,
        "available_sources_count": len(sources),
        "session_snapshot": sess.rag_snapshot,
    }
    return _record_tool(session_id, "rag_session_autoconfigure", params, result)


@mcp.tool()
def rag_sources_list(
    sync: Annotated[bool, "Discover new sources in watched roots before listing"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> list[dict[str, Any]]:
    """List registered 1C source trees."""
    disabled = _rag_require_enabled(session_id, "rag_sources_list", {"sync": sync})
    if disabled is not None:
        return disabled
    if sync:
        _RAG.sync_watch_roots()
    return _record_tool(session_id, "rag_sources_list", {"sync": sync}, _RAG.list_sources())


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
    params = {"name": name, "base": base, "extensions": extensions, "sources": sources}
    disabled = _rag_require_enabled(session_id, "rag_snapshot_create", params)
    if disabled is not None:
        return disabled
    result = _RAG.create_snapshot(name=name, base=base, extensions=extensions, sources=sources)
    sess = _session(session_id)
    sess.rag_snapshot = str(result.get("name") or name)
    if sess.base_url:
        _save_rag_binding(sess.base_url, sess.rag_snapshot)
    _persist_sessions()
    return result


@mcp.tool()
def rag_snapshots_list(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> list[dict[str, Any]]:
    """List effective RAG snapshots and their layers."""
    disabled = _rag_require_enabled(session_id, "rag_snapshots_list", {})
    if disabled is not None:
        return disabled
    return _record_tool(session_id, "rag_snapshots_list", {}, _RAG.list_snapshots())


@mcp.tool()
def rag_index_build(
    snapshot: Annotated[str | None, "Snapshot name/id to index"] = None,
    source: Annotated[str | None, "Single source name to index"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Build/rebuild the SQLite/FTS RAG index for a source or snapshot."""
    disabled = _rag_require_enabled(session_id, "rag_index_build", {"snapshot": snapshot, "source": source})
    if disabled is not None:
        return disabled
    sess = _session(session_id)
    effective_snapshot = snapshot or sess.rag_snapshot or None
    if effective_snapshot:
        sess.rag_snapshot = str(effective_snapshot)
        if sess.base_url:
            _save_rag_binding(sess.base_url, sess.rag_snapshot)
        _persist_sessions()
    return _record_tool(session_id, "rag_index_build", {"snapshot": effective_snapshot, "source": source}, _RAG.build_index(snapshot=effective_snapshot, source=source))


@mcp.tool()
def rag_lookup_object(
    object: Annotated[str, "1C object full name or metadata name"],
    snapshot: Annotated[str | None, "Optional snapshot name/id"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Look up an indexed 1C metadata object, including navigation, fields and forms."""
    disabled = _rag_require_enabled(session_id, "rag_lookup_object", {"object": object, "snapshot": snapshot})
    if disabled is not None:
        return disabled
    sess = _session(session_id)
    effective_snapshot = snapshot or sess.rag_snapshot or None
    result = _RAG.lookup_object(object=object, snapshot=effective_snapshot)
    result["rag_diagnostic"] = _rag_diagnostic(session_id, snapshot)
    return _record_tool(session_id, "rag_lookup_object", {"object": object, "snapshot": effective_snapshot}, result)


@mcp.tool()
def rag_query(
    query: Annotated[str, "Natural-language or metadata search query"],
    snapshot: Annotated[str | None, "Optional snapshot name/id"] = None,
    limit: Annotated[int, "Max result count"] = 10,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Search business terms and full-text RAG chunks in the local SQLite index."""
    disabled = _rag_require_enabled(session_id, "rag_query", {"query": query, "snapshot": snapshot, "limit": limit})
    if disabled is not None:
        return disabled
    sess = _session(session_id)
    effective_snapshot = snapshot or sess.rag_snapshot or None
    result = _RAG.query(text=query, snapshot=effective_snapshot, limit=limit)
    result["snapshot"] = effective_snapshot
    result["rag_diagnostic"] = _rag_diagnostic(session_id, snapshot)
    return _record_tool(session_id, "rag_query", {"query": query, "snapshot": effective_snapshot, "limit": limit}, result)


@mcp.tool()
def sessions_list() -> dict[str, Any]:
    """List active/restorable Answer42 sessions and their connection state."""
    persisted = _load_persisted_sessions()
    session_ids = sorted(set(persisted) | set(_SESSIONS))
    items: list[dict[str, Any]] = []
    for sid in session_ids:
        sess = _SESSIONS.get(sid) or persisted.get(sid)
        if sess is None:
            continue
        rt = sess.runtime
        client_rt = sess.client_runtime
        file_client_rt = sess.file_client_runtime
        bridge_status = sess.bridge.status()
        items.append({
            "session_id": sid,
            "running": sid in _SESSIONS,
            "base_url": sess.base_url,
            "version": _platform.platform_version_from_dir(sess.platform_dir),
            "display": sess.display,
            "client_display": sess.client_display,
            "ws_port": sess.ws_port,
            "bridge_connected": bool(bridge_status.get("connected")),
            "manager_alive": bool(rt and rt.manager_process and rt.manager_process.poll() is None),
            "ibsrv_alive": bool(rt and rt.server_process and rt.server_process.poll() is None),
            "testclient_port": sess.testclient_port,
            "client_ibsrv_port": sess.client_ibsrv_port,
            "client_ibsrv_alive": bool(client_rt and (
                (client_rt.server_process and client_rt.server_process.poll() is None)
                or (not client_rt.server_process and runtime.is_port_listening(client_rt.server.address, client_rt.server.port))
            )),
            "file_client_ibsrv_port": sess.file_client_ibsrv_port,
            "file_client_ibsrv_alive": bool(file_client_rt and (
                (file_client_rt.server_process and file_client_rt.server_process.poll() is None)
                or (not file_client_rt.server_process and runtime.is_port_listening(file_client_rt.server.address, file_client_rt.server.port))
            )),
            "rag_snapshot": sess.rag_snapshot,
            "rag_enabled": _RAG_ENABLED and sess.rag_enabled,
            "idle_timeout_seconds": sess.idle_timeout_seconds,
        })
    return {"count": len(items), "sessions": items}


@mcp.tool()
def session_status(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Return bridge, runtime, test-client and RAG state for a session."""
    sess = _SESSIONS.get(session_id) or _load_persisted_sessions().get(session_id)
    if sess is not None:
        _touch_session(sess)
    if sess is None:
        return {"running": False, "session_id": session_id, "reason": "session not found"}
    bridge = sess.bridge.status()
    rt = sess.runtime
    client_rt = sess.client_runtime
    file_client_rt = sess.file_client_runtime
    client_server_alive = False
    if client_rt:
        if client_rt.server_process:
            client_server_alive = client_rt.server_process.poll() is None
        else:
            client_server_alive = runtime.is_port_listening(client_rt.server.address, client_rt.server.port)
    file_client_server_alive = False
    if file_client_rt:
        if file_client_rt.server_process:
            file_client_server_alive = file_client_rt.server_process.poll() is None
        else:
            file_client_server_alive = runtime.is_port_listening(file_client_rt.server.address, file_client_rt.server.port)
    return {
        "running": True,
        "session_id": session_id,
        "base_url": sess.base_url,
        "version": _platform.platform_version_from_dir(sess.platform_dir),
        "display": sess.display,
        "client_display": sess.client_display,
        "bridge": bridge,
        "manager": None if rt is None else {
            "pid": rt.manager_process.pid if rt.manager_process else None,
            "alive": rt.manager_process.poll() is None if rt.manager_process else False,
            "server_pid": rt.server_process.pid if rt.server_process else None,
            "server_alive": rt.server_process.poll() is None if rt.server_process else False,
            "port": rt.server.port,
            "log_dir": str(rt.log_dir),
        },
        "test_client": {
            "port": sess.testclient_port,
            "base_url": sess.base_url,
            "client_ibsrv_port": sess.client_ibsrv_port,
            "server_alive": client_server_alive,
            "shared_client_key": sess.shared_client_key,
            "file_client_ibsrv_port": sess.file_client_ibsrv_port,
            "file_client_server_alive": file_client_server_alive,
            "shared_file_key": sess.shared_file_key,
        },
        "rag_snapshot": sess.rag_snapshot,
        "rag_enabled": _RAG_ENABLED and sess.rag_enabled,
        "server_rag_enabled": _RAG_ENABLED,
        "rag_diagnostic": _rag_diagnostic(session_id, sess.rag_snapshot),
        "idle_timeout_seconds": sess.idle_timeout_seconds,
    }


@mcp.tool()
async def start_session(
    base_url: Annotated[str, "Target 1C infobase URL; used for version detection and test-client connection"] = "",
    username: Annotated[str, "1C username"] = "",
    password: Annotated[str, "1C password"] = "",
    version: Annotated[str, "1C platform version override (auto-detected from base_url if empty)"] = "",
    session_id: Annotated[str, "Desired session id; auto-generated when empty"] = "",
    idle_timeout_minutes: Annotated[int, "Minutes of inactivity before automatic stop_session; 0 disables"] = 60,
    extra_args: Annotated[str, "Extra raw 1C command-line arguments for the test client"] = "",
    execute: Annotated[str, "External data processor/report path for the test-client /Execute command-line parameter"] = "",
    command_parameter: Annotated[str, "Additional test-client /C value; when extra_args already contains /C, this value is appended with ';'"] = "",
    rag_enabled: Annotated[bool, "Enable built-in RAG for this session"] = True,
) -> dict[str, Any]:
    """One-shot session factory: detect version, start infrastructure and connect the 1C test client."""
    credential_ref = _resolve_credential_reference(base_url)
    input_looks_like_url = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", str(base_url or "")))
    if credential_ref is not None:
        # For saved titles/aliases, replace title -> real URL.  For URL/wildcard matches,
        # keep the original requested URL; using the wildcard pattern itself as /WS value
        # would make the 1C client fail with invalid connection parameters.
        if credential_ref.get("url") and not input_looks_like_url:
            base_url = str(credential_ref["url"])
        if not username:
            username = str(credential_ref.get("username", ""))
        if not password:
            password = str(credential_ref.get("password", ""))
    base_url = _platform.normalize_infobase_url(base_url)
    connection_mode = "auto"
    connection_value = ""
    initial_connection_mode = _normalize_connection_mode(connection_mode, base_url, base_url)
    if base_url and initial_connection_mode != "ws":
        # Non-http(s) base_url is treated as an auto-detected test-client target:
        # a file infobase path (/F or ibsrv) or a server/infobase string (/S).
        connection_value = base_url
        base_url = ""
    resolved_version = version or None
    if not resolved_version and base_url and initial_connection_mode == "ws":
        resolved_version = _platform.detect_version_from_url(base_url)
    if resolved_version:
        _platform.require_websocket_support(resolved_version)

    platform_dir = _platform.find_platform_dir(resolved_version)
    actual_version = _platform.platform_version_from_dir(platform_dir)
    _platform.require_websocket_support(actual_version)
    logging.info("start_session: version=%s platform_dir=%s", actual_version, platform_dir)

    sid = session_id or DEFAULT_SESSION
    if sid in _SESSIONS:
        raise ValueError(f"Session {sid!r} already exists.")

    data_dir = os_support.default_data_dir() / "sessions" / sid
    with _port_allocation_lock():
        port_offset = _stable_session_port_offset(sid)
        display_offset = port_offset // _PORT_BAND_SIZE
        display = "" if os_support.IS_WINDOWS else runtime.find_free_display(runtime.DEFAULT_DISPLAY_START + display_offset)
        ws_port = _find_session_port(runtime.DEFAULT_WS_PORT, port_offset, sub_offset=0)
        ibsrv_port = _find_session_port(runtime.DEFAULT_IBSRV_HTTP_PORT, port_offset, sub_offset=0)
        ibsrv_direct_regport = _find_session_port(runtime.DEFAULT_IBSRV_DIRECT_PORT, port_offset, sub_offset=0)
        testclient_port = _find_session_port(runtime.DEFAULT_TEST_CLIENT_PORT, port_offset, sub_offset=0)

        sess = SessionState(
            session_id=sid, display=display, platform_dir=platform_dir,
            ws_port=ws_port, ibsrv_port=ibsrv_port, testclient_port=testclient_port,
            data_dir=data_dir, base_url=base_url, username=username, password=password
        )
        sess.rag_enabled = bool(rag_enabled)
        sess.idle_timeout_seconds = max(0, int(idle_timeout_minutes)) * 60
        _cleanup_stale_session_processes(sess, "before start_session")
        if base_url and _RAG_ENABLED and sess.rag_enabled:
            bound_snapshot = _snapshot_for_base_url(base_url)
            if bound_snapshot:
                sess.rag_snapshot = bound_snapshot
        if display:
            sess.xvfb_process = runtime.start_xvfb(display)
        # Separate display for the test client so screenshots capture only the client window
        client_display = "" if os_support.IS_WINDOWS else runtime.find_free_display(runtime.DEFAULT_DISPLAY_START + display_offset + 1)
        sess.client_display = client_display
        if client_display:
            sess.client_xvfb_process = runtime.start_xvfb(client_display)
        await sess.bridge.start()
    _SESSIONS[sid] = sess
    _persist_sessions()
    logging.info("start_session: bridge listening on ws://127.0.0.1:%s", ws_port)

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

    try:
        with _port_allocation_lock():
            runtime.create_file_infobase(sess.ibcmd, sess.db_path)
            cf_path = _ensure_cf_built(
                runtime.DEFAULT_CF_BUILT,
                src_dir="src/cf",
                platform_dir=platform_dir,
            )
            runtime.load_configuration(sess.ibcmd, sess.db_path, cf_path)
            _write_cf_marker(sess.db_path, cf_path)
    except Exception:
        await _cleanup_failed_start_session(sess, "infobase preparation failure")
        raise

    ibsrv_available = Path(sess.ibsrv).exists()
    sess.runtime = runtime_state
    try:
        if ibsrv_available:
            server_proc = runtime.start_standalone_server(
                ibsrv=sess.ibsrv, address=ibsrv_address, port=ibsrv_port,
                name=ibsrv_name, data_dir=sess.server_data_dir,
                log_dir=log_dir, db_path=sess.db_path,
                direct_regport=ibsrv_direct_regport,
                direct_range=_direct_range_from_regport(ibsrv_direct_regport),
            )
            runtime_state.server_process = server_proc
            ws_url = runtime.build_thin_client_ws_url(ibsrv_address, ibsrv_port)
            manager_proc = runtime.start_thin_test_manager(
                onec_path=sess.onec_thin, ws_url=ws_url,
                websocket_url=sess.bridge.url, log_dir=log_dir, display=display,
            )
        else:
            logging.info("ibsrv not found; starting test-manager in file-direct mode")
            sess.ibsrv_port = 0
            ws_url = ""
            manager_proc = runtime.start_thin_test_manager_file_direct(
                onec_path=sess.onec_thin,
                db_path=sess.db_path,
                websocket_url=sess.bridge.url,
                log_dir=log_dir,
                display=display,
            )
        runtime_state.manager_process = manager_proc
        await _wait_for_bridge_connected(sess, timeout=60.0)
    except Exception as exc:
        diagnostic_error = _manager_failure_diagnostics(sess, exc)
        await _cleanup_failed_start_session(sess, "manager startup failure")
        raise diagnostic_error from exc
    _persist_sessions()

    pipeline_info: dict[str, Any] = {"manager_pid": manager_proc.pid, "ws_url": ws_url, "log_dir": str(log_dir)}

    try:
        # --- Built-in demo client ibsrv (second autonomous server for /TESTCLIENT) ---
        # It is only needed for self-contained E2E sessions.  When the caller
        # passed a target URL/file/server infobase, connect the test client to
        # that target instead of creating an extra demo infobase.
        if not (base_url or connection_value) and ibsrv_available:
            shared_client_id = os.getenv("ONEC_MCP_SHARED_CLIENT_BASE_ID") or "default"
            shared_client_root = Path(os.getenv("ONEC_MCP_SHARED_CLIENT_BASE_DIR", str(os_support.default_data_dir() / "shared-client-bases")))
            shared_client_db = shared_client_root / shared_client_id / "ib"
            client_cf_path = _ensure_cf_built(
                runtime.DEFAULT_CLIENT_CF_BUILT,
                src_dir="src/client_cf",
                platform_dir=platform_dir,
            )
            shared_client = _acquire_shared_file_server(
                owner_session_id=sid,
                kind="demo-client",
                platform_dir=platform_dir,
                ibcmd=sess.ibcmd,
                ibsrv=sess.ibsrv,
                db_path=shared_client_db,
                cf_path=client_cf_path,
                create_if_missing=True,
                port_seed=f"demo-client:{shared_client_id}",
                server_name="mcpclient",
            )
            sess.client_ibsrv_port = shared_client.port
            sess.shared_client_key = shared_client.key
            sess.client_runtime = runtime.ManagerRuntime(
                infobase=runtime.InfobaseSpec(db_path=shared_client.db_path),
                server=runtime.StandaloneServerSpec(
                    address=shared_client.address,
                    port=shared_client.port,
                    name=shared_client.name,
                    data_dir=shared_client.data_dir,
                ),
                manager=runtime.TestManagerSpec(onec_path=sess.onec_thin),
                server_process=shared_client.process,
                log_dir=shared_client.log_dir,
                extra={"shared_key": shared_client.key, "shared_refcount": len(shared_client.owners)},
            )

            client_ws_url = runtime.build_thin_client_ws_url(shared_client.address, shared_client.port)
            pipeline_info.update({
                "client_ibsrv_port": shared_client.port,
                "client_ws_url": client_ws_url,
                "client_base_url": shared_client.base_url,
                "client_shared": True,
                "client_shared_refcount": len(shared_client.owners),
            })
        elif not (base_url or connection_value):
            logging.info("ibsrv not found; preparing demo test-client in file-direct mode")
            runtime.create_file_infobase(sess.ibcmd, sess.client_db_path)
            client_cf_path = _ensure_cf_built(
                runtime.DEFAULT_CLIENT_CF_BUILT,
                src_dir="src/client_cf",
                platform_dir=platform_dir,
            )
            runtime.load_configuration(sess.ibcmd, sess.client_db_path, client_cf_path)
            _write_cf_marker(sess.client_db_path, client_cf_path)
            sess.client_runtime = runtime.ManagerRuntime(
                infobase=runtime.InfobaseSpec(db_path=sess.client_db_path),
                server=runtime.StandaloneServerSpec(address="", port=0, name="file-direct", data_dir=sess.client_server_data_dir),
                manager=runtime.TestManagerSpec(onec_path=sess.onec_thin),
                server_process=None,
                log_dir=sess.client_log_dir,
                extra={"target_connection_mode": "file-direct"},
            )
            pipeline_info.update({
                "client_connection_mode": "file-direct",
                "client_connection_value": str(sess.client_db_path),
                "client_base_url": str(sess.client_db_path),
                "client_shared": False,
            })
    except Exception:
        await _cleanup_failed_start_session(sess, "demo test-client preparation failure")
        raise

    connect_result: Any = None
    connect_base_url = base_url
    connect_connection_value = connection_value
    connect_connection_mode = connection_mode
    if not connect_base_url and pipeline_info.get("client_base_url"):
        connect_base_url = str(pipeline_info["client_base_url"])
    if not connect_connection_value and pipeline_info.get("client_connection_value"):
        connect_connection_value = str(pipeline_info["client_connection_value"])
        connect_connection_mode = str(pipeline_info.get("client_connection_mode") or "file-direct")
    if connect_base_url or connect_connection_value:
        try:
            connect_result = await _connect_test_client_impl(
                sess, base_url=connect_base_url, username=username, password=password,
                port=testclient_port, onec_path=sess.onec_thin, host="localhost",
                extra_args=extra_args, execute=execute, command_parameter=command_parameter,
                connection_mode=connect_connection_mode, connection_value=connect_connection_value,
            )
        except Exception as exc:
            enriched = await _enrich_auth_failure_for_web_base(exc, connect_base_url)
            target_mode = _normalize_connection_mode(
                connect_connection_mode, connect_connection_value or connect_base_url, connect_base_url,
            )
            diagnostic_error = _client_failure_diagnostics(sess, enriched, target_mode)
            await _cleanup_failed_start_session(sess, "test-client startup failure")
            raise diagnostic_error from exc
        _auto_maximize_test_client_result(connect_result, sess.client_display or sess.display)

    if sess.idle_timeout_seconds > 0:
        asyncio.create_task(_idle_watch_session(sid))

    _persist_sessions()
    return {
        "session_id": sid, "version": actual_version,
        "platform_dir": str(platform_dir), "display": display,
        "client_display": sess.client_display,
        "ws_port": ws_port, "ibsrv_port": ibsrv_port,
        "testclient_port": testclient_port, "data_dir": str(data_dir),
        "bridge_url": sess.bridge.url, "rag_snapshot": sess.rag_snapshot,
        "rag_enabled": _RAG_ENABLED and sess.rag_enabled,
        "server_rag_enabled": _RAG_ENABLED,
        "idle_timeout_seconds": sess.idle_timeout_seconds,
        "test_client": connect_result, **pipeline_info,
    }


async def _stop_session_impl(session_id: str = DEFAULT_SESSION, clean_data: bool = False) -> dict[str, Any]:
    try:
        sess = await _ensure_restored_session(session_id)
        _SESSIONS.pop(session_id, None)
    except ValueError:
        return {"stopped": False, "reason": f"session {session_id!r} not found"}
    stopped: list[str] = []
    try:
        await sess.bridge.call("disconnect_test_client", {})
        stopped.append("test-client")
    except Exception:
        pass
    if sess.test_client_process is not None:
        runtime.kill_process(sess.test_client_process, "test-client")
        stopped.append("test-client-process")
    if sess.runtime is not None:
        runtime.kill_process(sess.runtime.manager_process, "test-manager")
        runtime.kill_process(sess.runtime.server_process, "ibsrv")
        stopped.append("pipeline")
    if sess.client_runtime is not None:
        if sess.shared_client_key:
            if _release_shared_file_server(sess.shared_client_key, session_id):
                stopped.append("shared-client-ibsrv")
        else:
            runtime.kill_process(sess.client_runtime.server_process, "ibsrv-client")
            stopped.append("client-ibsrv")
    if sess.file_client_runtime is not None:
        if sess.shared_file_key:
            if _release_shared_file_server(sess.shared_file_key, session_id):
                stopped.append("shared-file-ibsrv")
        else:
            runtime.kill_process(sess.file_client_runtime.server_process, "ibsrv-file-client")
            stopped.append("file-client-ibsrv")
    try:
        await sess.bridge.stop()
        stopped.append("bridge")
    except Exception as exc:
        logging.warning("stop_session: bridge.stop failed: %s", exc)
    runtime.stop_xvfb(sess.client_xvfb_process)
    stopped.append("client-xvfb")
    runtime.stop_xvfb(sess.xvfb_process)
    stopped.append("xvfb")
    _cleanup_stale_session_processes(sess, "stop_session")
    stopped.append("stale-processes")
    if clean_data and sess.data_dir.exists():
        shutil.rmtree(sess.data_dir, ignore_errors=True)
        stopped.append("data_dir")
    _persist_sessions()
    return {"stopped": True, "session_id": session_id, "components": stopped}


@mcp.tool()
async def stop_session(
    session_id: Annotated[str, "Session to tear down"] = DEFAULT_SESSION,
    clean_data: Annotated[bool, "Remove the session data directory"] = False,
) -> dict[str, Any]:
    """Tear down a session: disconnect test-client, stop test-manager, ibsrv, Xvfb and bridge."""
    return await _stop_session_impl(session_id=session_id, clean_data=clean_data)


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

    ibcmd = str(pd / os_support.executable_name("ibcmd"))
    ibsrv = str(pd / os_support.executable_name("ibsrv"))
    onec_thin = str(pd / os_support.executable_name("1cv8c"))

    if create_infobase:
        runtime.create_file_infobase(ibcmd, db)
    if load_configuration and cf_path:
        resolved_cf_path = cf_path
        if cf_path == runtime.DEFAULT_CF_BUILT:
            resolved_cf_path = _ensure_cf_built(cf_path, src_dir="src/cf", platform_dir=pd)
        runtime.load_configuration(ibcmd, db, resolved_cf_path)
    if start_standalone:
        with _port_allocation_lock():
            port_offset = _stable_session_port_offset(f"launch:{session_id}:{port}")
            direct_regport = _find_session_port(runtime.DEFAULT_IBSRV_DIRECT_PORT, port_offset, sub_offset=0)
        server_proc = runtime.start_standalone_server(
            ibsrv=ibsrv, address=ibsrv_address, port=port,
            name=ibsrv_name, data_dir=sdd, log_dir=log_dir, db_path=db,
            direct_regport=direct_regport,
            direct_range=_direct_range_from_regport(direct_regport),
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
    """Export the 1C event log."""
    sess = _session(session_id)
    ibcmd = str(Path(platform_dir) / "ibcmd") if platform_dir else sess.ibcmd
    if sess.runtime is not None and not eventlog_dir:
        eventlog_path = sess.runtime.server.data_dir / "log-data"
        log_dir = sess.runtime.log_dir
    else:
        eventlog_path = Path(eventlog_dir or str(Path.home() / ".1cv8/1C/1cv8/standalone-server/log-data"))
        log_dir = sess.log_dir
    if not out_path:
        out = log_dir / f"eventlog_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
    else:
        out = Path(out_path)
    exported = runtime.export_eventlog(ibcmd, eventlog_path, out, from_time or None, to_time or None, fmt)
    return {"path": str(exported), "format": fmt, "eventlog_dir": str(eventlog_path)}


def _credential_store_disabled_error() -> RuntimeError:
    return RuntimeError("Credential store is disabled by --disable-credential-store or ONEC_MCP_DISABLE_CREDENTIAL_STORE.")


def _lookup_credentials(base_url: str) -> dict[str, str] | None:
    if not _CREDENTIAL_STORE_ENABLED:
        return None
    return _credentials_store().lookup(base_url)


def _resolve_credential_reference(value: str) -> dict[str, str] | None:
    if not _CREDENTIAL_STORE_ENABLED or not value:
        return None
    return _credentials_store().resolve(value)


@mcp.tool()
def credentials_check(base_url: str) -> dict[str, object]:
    """Check whether credentials exist for a base URL or saved title without returning secrets."""
    if not _CREDENTIAL_STORE_ENABLED:
        return {"base_url": base_url, "found": False, "disabled": True}
    credentials = _credentials_store().resolve(base_url)
    if credentials is None:
        return {"base_url": base_url, "found": False}
    result: dict[str, object] = {"base_url": base_url, "found": True, "url": credentials.get("url", "")}
    if credentials.get("title"):
        result["title"] = credentials["title"]
    return result


@mcp.tool()
def credentials_save(
    url: Annotated[str, "Base URL pattern (exact or wildcard like https://*.example.com/*)"],
    username: Annotated[str, "1C username"],
    password: Annotated[str, "1C password"],
    title: Annotated[str, "Optional saved base title/alias, e.g. bp27932"] = "",
    aliases: Annotated[str, "Optional comma-separated aliases for the same entry"] = "",
) -> dict[str, object]:
    """Save or update credentials for a URL pattern in the local credentials store.

    The credentials file is never committed to git.  Use this tool to persist
    login/password pairs so that ``start_session`` can pick them up automatically
    without exposing secrets in MCP tool-call arguments.
    """
    if not _CREDENTIAL_STORE_ENABLED:
        raise _credential_store_disabled_error()
    alias_list = [part.strip() for part in aliases.split(",") if part.strip()]
    updated = _credentials_store().upsert(url, username, password, title=title, aliases=alias_list)
    result: dict[str, object] = {"url": url, "saved": True, "updated": updated}
    if title:
        result["title"] = title
    if alias_list:
        result["aliases"] = alias_list
    return result


@mcp.tool()
def credentials_remove(
    url: Annotated[str, "Exact URL pattern to remove from the credentials store"],
) -> dict[str, object]:
    """Remove a credential entry from the local store by exact URL match."""
    if not _CREDENTIAL_STORE_ENABLED:
        raise _credential_store_disabled_error()
    removed = _credentials_store().remove(url)
    return {"url": url, "removed": removed}


@mcp.tool()
def credentials_list() -> list[dict[str, object]]:
    """List configured credential URL patterns/titles (no usernames or passwords)."""
    if not _CREDENTIAL_STORE_ENABLED:
        return []
    return _credentials_store().list_entries()


@mcp.tool()
def credentials_titles() -> list[dict[str, object]]:
    """List saved credential titles/aliases that can be used as start_session base_url."""
    if not _CREDENTIAL_STORE_ENABLED:
        return []
    return _credentials_store().titles()


_PLACEHOLDER_PASSWORD_RE = re.compile(r"^\*{3,}$")
_AUTH_FAILURE_MARKERS = (
    "пользователь иб не идентифицирован",
    "пользователь не идентифицирован",
    "ошибка аутентификации",
    "ошибка авторизации",
    "неверный пароль",
    "неправильный пароль",
    "authentication failed",
    "authorization failed",
    "invalid user",
    "invalid password",
)
_EVENTLOG_ERROR_MARKERS = (
    "ошиб",
    "error",
    "exception",
    "исключен",
    "failed",
    "fail",
)


def _is_placeholder_password(password: str) -> bool:
    """Return True when *password* looks like a redacted placeholder (e.g. ``***``)."""
    return bool(password) and bool(_PLACEHOLDER_PASSWORD_RE.match(password))


def _is_web_base_url(base_url: str) -> bool:
    scheme = urlsplit(base_url).scheme.lower()
    return scheme in {"http", "https"}


def _users_url_for_base_url(base_url: str) -> str:
    parts = urlsplit(_platform.normalize_infobase_url(base_url).rstrip("/"))
    path = parts.path.rstrip("/") + "/ru_RU/e1cib/users"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _parse_users_plain_text(text: str) -> list[str]:
    users: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        user = raw_line.strip().lstrip("\ufeff")
        if not user or user in seen:
            continue
        seen.add(user)
        users.append(user)
    return users


def _fetch_available_users_sync(base_url: str, timeout: float = 10.0) -> dict[str, Any]:
    users_url = _users_url_for_base_url(base_url)
    request = urllib.request.Request(users_url, headers={"User-Agent": "Answer42/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace")
        users = _parse_users_plain_text(text)
        return {"url": users_url, "users": users, "count": len(users)}
    except Exception as exc:
        return {"url": users_url, "users": [], "count": 0, "error": str(exc)}


async def _fetch_available_users(base_url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_available_users_sync, base_url)


def _looks_like_auth_failure(error_text: str) -> bool:
    normalized = error_text.lower()
    return any(marker in normalized for marker in _AUTH_FAILURE_MARKERS)


def _append_available_users_to_auth_error(error_text: str, users_info: dict[str, Any]) -> str:
    users = users_info.get("users") or []
    if users:
        users_text = "\n".join(f"- {user}" for user in users)
        return (
            f"{error_text}\n\n"
            f"Доступные пользователи из {users_info.get('url')}:\n{users_text}"
        )
    if users_info.get("error"):
        return (
            f"{error_text}\n\n"
            f"Не удалось получить список доступных пользователей из {users_info.get('url')}: {users_info.get('error')}"
        )
    return f"{error_text}\n\nСписок доступных пользователей из {users_info.get('url')} пуст."


async def _enrich_auth_failure_for_web_base(error: Exception, base_url: str) -> Exception:
    error_text = str(error)
    if not base_url or not _is_web_base_url(base_url) or not _looks_like_auth_failure(error_text):
        return error
    users_info = await _fetch_available_users(base_url)
    enriched_text = _append_available_users_to_auth_error(error_text, users_info)
    return type(error)(enriched_text)


def _decode_log_bytes(data: bytes) -> str:
    """Decode 1C text logs written by different OS/platform versions.

    Linux logs are usually UTF-8, while Windows 1C /Out logs may be
    UTF-16LE/UTF-16BE or an ANSI codepage.  Decoding Windows UTF-16LE as
    UTF-8 with replacement hides Russian startup errors behind NUL bytes,
    so auth failures such as ``Пользователь ИБ не идентифицирован`` are not
    detected and the user only sees a generic startup timeout.
    """
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        for encoding in ("utf-16", "utf-8-sig"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
    sample = data[:4096]
    if sample:
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        # Heuristic for UTF-16 logs without BOM.  Russian/ASCII text in
        # UTF-16LE has most NULs in odd bytes; UTF-16BE has them in even bytes.
        threshold = max(8, len(sample) // 8)
        if odd_nuls > threshold and odd_nuls > even_nuls * 2:
            try:
                return data.decode("utf-16-le")
            except UnicodeDecodeError:
                pass
        if even_nuls > threshold and even_nuls > odd_nuls * 2:
            try:
                return data.decode("utf-16-be")
            except UnicodeDecodeError:
                pass
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def _tail_text_file(path: Path | str, *, max_lines: int = 80, max_chars: int = 20000) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return ""
    try:
        text_data = _decode_log_bytes(file_path.read_bytes())
        lines = text_data.splitlines()
    except Exception as exc:
        return f"<не удалось прочитать {file_path}: {exc}>"
    text = "\n".join(lines[-max_lines:])
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def _append_file_tail(message: str, title: str, path: Path | str, *, max_lines: int = 80) -> str:
    tail = _tail_text_file(path, max_lines=max_lines)
    if not tail:
        return f"{message}\n\n{title}: {path}\n<лог пуст или не найден>"
    return f"{message}\n\n{title}: {path}\n{tail}"


def _extract_error_lines(text: str, *, max_lines: int = 80) -> str:
    lines = text.splitlines()
    error_lines = [line for line in lines if any(marker in line.lower() for marker in _EVENTLOG_ERROR_MARKERS)]
    selected = error_lines[-max_lines:] if error_lines else lines[-max_lines:]
    return "\n".join(selected)


def _eventlog_errors_for_runtime(
    sess: SessionState,
    rt: runtime.ManagerRuntime | None,
    label: str,
    *,
    max_lines: int = 80,
) -> dict[str, str]:
    if rt is None or rt.server_process is None:
        return {}
    eventlog_dir = rt.server.data_dir / "log-data"
    out_path = rt.log_dir / f"eventlog_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        exported = runtime.export_eventlog(sess.ibcmd, eventlog_dir, out_path, None, None, "json")
        text = _tail_text_file(exported, max_lines=500, max_chars=120000)
        return {"path": str(exported), "tail": _extract_error_lines(text, max_lines=max_lines)}
    except Exception as exc:
        return {"path": str(out_path), "error": str(exc)}


def _append_eventlog_diagnostics(message: str, diagnostics: dict[str, str], title: str) -> str:
    if not diagnostics:
        return message
    if diagnostics.get("tail"):
        return f"{message}\n\n{title}: {diagnostics.get('path')}\n{diagnostics.get('tail')}"
    return f"{message}\n\n{title}: {diagnostics.get('path')}\n<не удалось выгрузить/прочитать ЖР: {diagnostics.get('error', 'нет данных')}>"


def _manager_failure_diagnostics(sess: SessionState, error: Exception) -> RuntimeError:
    message = str(error)
    message = _append_file_tail(message, "Лог stdout/stderr менеджера тестирования", sess.log_dir / "test_manager.log")
    message = _append_file_tail(message, "Лог 1С /Out менеджера тестирования", sess.log_dir / "test_manager_1c_out.log")
    diagnostics = _eventlog_errors_for_runtime(sess, sess.runtime, "manager")
    message = _append_eventlog_diagnostics(message, diagnostics, "Последние ошибки ЖР автономного сервера менеджера")
    return RuntimeError(message)


def _client_runtime_for_eventlog(sess: SessionState, target_connection_mode: str) -> runtime.ManagerRuntime | None:
    if target_connection_mode == "file":
        return sess.file_client_runtime
    if target_connection_mode == "ws" and sess.client_runtime is not None:
        # Built-in demo client is served through a local autonomous server.
        return sess.client_runtime
    return None


def _client_failure_diagnostics(sess: SessionState, error: Exception, target_connection_mode: str) -> RuntimeError:
    message = str(error)
    message = _append_file_tail(message, "Лог stdout/stderr клиента тестирования", sess.client_log_dir / "test_client_stdout.log")
    message = _append_file_tail(message, "Лог 1С /Out клиента тестирования", sess.client_log_dir / "test_client_1c_out.log")
    rt = _client_runtime_for_eventlog(sess, target_connection_mode)
    diagnostics = _eventlog_errors_for_runtime(sess, rt, "client")
    message = _append_eventlog_diagnostics(message, diagnostics, "Последние ошибки ЖР автономного сервера клиента тестирования")
    return RuntimeError(message)


def _test_client_startup_error_from_logs(sess: SessionState) -> str:
    """Return a meaningful early startup error from test-client logs, if any."""
    fragments: list[str] = []
    for path in (sess.client_log_dir / "test_client_1c_out.log", sess.client_log_dir / "test_client_stdout.log"):
        tail = _tail_text_file(path, max_lines=120, max_chars=30000)
        if tail:
            fragments.append(tail)
    if not fragments:
        return ""

    text = "\n".join(fragments)
    auth_lines = [line for line in text.splitlines() if _looks_like_auth_failure(line)]
    if auth_lines:
        return "\n".join(auth_lines[-20:])

    meaningful = _extract_error_lines(text, max_lines=20)
    if meaningful and any(marker in meaningful.lower() for marker in _EVENTLOG_ERROR_MARKERS):
        return meaningful
    return ""


async def _wait_for_test_client_process_exit(process: subprocess.Popen[Any]) -> int | None:
    while process.poll() is None:
        await asyncio.sleep(0.2)
    return process.returncode


async def _wait_for_test_client_ready(sess: SessionState, host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    proc = sess.test_client_process
    while time.time() < deadline:
        startup_error = _test_client_startup_error_from_logs(sess)
        if startup_error:
            message = f"Test client startup error before opening test port {host}:{port}:\n{startup_error}"
            message = _append_file_tail(message, "Лог stdout/stderr клиента тестирования", sess.client_log_dir / "test_client_stdout.log")
            message = _append_file_tail(message, "Лог 1С /Out клиента тестирования", sess.client_log_dir / "test_client_1c_out.log")
            raise RuntimeError(message)
        if proc is not None and proc.poll() is not None:
            message = f"Test client process exited before opening test port {host}:{port}; exit_code={proc.returncode}"
            message = _append_file_tail(message, "Лог stdout/stderr клиента тестирования", sess.client_log_dir / "test_client_stdout.log")
            message = _append_file_tail(message, "Лог 1С /Out клиента тестирования", sess.client_log_dir / "test_client_1c_out.log")
            raise RuntimeError(message)
        if await asyncio.to_thread(runtime.wait_for_port, host, port, 0.2, 0.05):
            return
        await asyncio.sleep(0.4)
    message = f"Test client did not open test port {host}:{port} within {timeout:.0f}s"
    message = _append_file_tail(message, "Лог stdout/stderr клиента тестирования", sess.client_log_dir / "test_client_stdout.log")
    message = _append_file_tail(message, "Лог 1С /Out клиента тестирования", sess.client_log_dir / "test_client_1c_out.log")
    if proc is not None:
        message += f"\nprocess_pid={proc.pid}; process_exit_code={proc.poll()}"
    raise RuntimeError(message)


async def _verify_manager_protocol(sess: SessionState) -> dict[str, Any]:
    info = await sess.bridge.call("manager_info", {})
    if not isinstance(info, dict):
        raise RuntimeError(
            "MCPTestManager.cf is too old or incompatible: manager_info returned an unexpected response. "
            f"Expected protocol {MANAGER_PROTOCOL_VERSION}. Rebuild/reload MCPTestManager.cf."
        )
    actual = str(info.get("manager_protocol_version") or "")
    if actual != MANAGER_PROTOCOL_VERSION:
        raise RuntimeError(
            "MCPTestManager.cf protocol mismatch: "
            f"Python expects {MANAGER_PROTOCOL_VERSION}, but 1C manager reports {actual or '<missing>'}. "
            "Rebuild MCPTestManager.cf from current sources and restart/reload Answer42 MCP."
        )
    return info


async def _connect_test_client_impl(
    sess: SessionState,
    *,
    base_url: str = "",
    username: str = "",
    password: str = "",
    port: int = 0,
    onec_path: str = "",
    host: str = "localhost",
    extra_args: str = "",
    execute: str = "",
    command_parameter: str = "",
    connection_mode: str = "auto",
    connection_value: str = "",
) -> Any:
    """Launch 1cv8c /TESTCLIENT from Python and attach the test manager to it."""
    await _wait_for_bridge_connected(sess, timeout=60.0)
    manager_info = await _verify_manager_protocol(sess)
    r_base = _platform.normalize_infobase_url(base_url or sess.base_url)
    r_value = connection_value or r_base
    mode = _normalize_connection_mode(connection_mode, r_value, r_base)
    r_user = username or sess.username
    r_pass = password if password else sess.password
    r_port = port if port else sess.testclient_port
    r_onec = onec_path or sess.onec_thin

    if mode == "ws":
        if not r_value and sess.client_ibsrv_port:
            r_value = f"http://127.0.0.1:{sess.client_ibsrv_port}"
            r_user = r_user or ""
            r_pass = r_pass or ""
        if not r_value:
            raise ValueError("base_url or connection_value is required")
        r_base = r_value
        if not r_user and not r_pass:
            credentials = _lookup_credentials(r_base)
            if credentials:
                r_user = credentials["username"]
                r_pass = credentials["password"]
    elif mode == "file-direct":
        if not r_value:
            raise ValueError("connection_value or base_url must contain a file infobase directory for connection_mode='file-direct'")
        db_path = Path(r_value).expanduser()
        if not db_path.exists():
            raise FileNotFoundError(f"File infobase directory not found: {db_path}")
        r_base = str(db_path)
        r_value = str(db_path)
        target_connection_mode = "file-direct"
    elif mode == "file":
        if not r_value:
            raise ValueError("connection_value or base_url must contain a file infobase directory for connection_mode='file'")
        db_path = Path(r_value).expanduser()
        if not db_path.exists():
            raise FileNotFoundError(f"File infobase directory not found: {db_path}")
        if sess.shared_file_key:
            _release_shared_file_server(sess.shared_file_key, sess.session_id)
            sess.shared_file_key = ""
        elif sess.file_client_runtime is not None:
            runtime.kill_process(sess.file_client_runtime.server_process, "ibsrv-file-client")
            sess.file_client_runtime = None
        shared_file = _acquire_shared_file_server(
            owner_session_id=sess.session_id,
            kind="file",
            platform_dir=sess.platform_dir,
            ibcmd=sess.ibcmd,
            ibsrv=sess.ibsrv,
            db_path=db_path,
            create_if_missing=False,
            port_seed=f"file:{db_path.resolve()}",
            server_name="mcpfileclient",
        )
        sess.file_client_ibsrv_port = shared_file.port
        sess.shared_file_key = shared_file.key
        sess.file_client_runtime = runtime.ManagerRuntime(
            infobase=runtime.InfobaseSpec(db_path=shared_file.db_path),
            server=runtime.StandaloneServerSpec(
                address=shared_file.address,
                port=shared_file.port,
                name=shared_file.name,
                data_dir=shared_file.data_dir,
            ),
            manager=runtime.TestManagerSpec(onec_path=sess.onec_thin),
            server_process=shared_file.process,
            log_dir=shared_file.log_dir,
            extra={"target_connection_mode": "file", "shared_key": shared_file.key, "shared_refcount": len(shared_file.owners)},
        )
        r_base = str(db_path)
        r_value = shared_file.base_url
        mode = "ws"
        target_connection_mode = "file"
    elif mode == "server":
        if not r_value:
            raise ValueError("connection_value or base_url must contain server/infobase for connection_mode='server'")
        r_base = r_value
        target_connection_mode = "server"
    else:
        raise ValueError(f"Unsupported connection mode after normalization: {mode!r}")
    if "target_connection_mode" not in locals():
        target_connection_mode = mode

    if not r_user and not r_pass and r_base:
        credentials = _lookup_credentials(r_base)
        if credentials:
            r_user = credentials["username"]
            r_pass = credentials["password"]

    if _is_placeholder_password(r_pass):
        raise ValueError(
            "Передан пароль-заглушка из звёздочек вместо реального пароля. "
            "Укажите настоящий пароль или сохраните корректные креды через credentials_save()."
        )

    sess.base_url = r_base
    sess.username = r_user
    sess.password = r_pass
    if sess.rag_snapshot and r_base:
        _save_rag_binding(r_base, sess.rag_snapshot)
    else:
        bound_snapshot = _snapshot_for_base_url(r_base) if r_base else ""
        if bound_snapshot:
            sess.rag_snapshot = bound_snapshot
    runtime.kill_process(sess.test_client_process, "test-client")
    sess.client_log_dir.mkdir(parents=True, exist_ok=True)
    client_out_log = sess.client_log_dir / "test_client_1c_out.log"
    client_stdout_log = "test_client_stdout.log"
    argv = runtime.build_test_client_argv(
        r_onec,
        connection_mode=mode,
        connection_value=r_value,
        port=r_port,
        username=r_user,
        password=r_pass,
        extra_args=extra_args,
        execute=execute,
        command_parameter=command_parameter,
        out_log=client_out_log,
    )
    sess.test_client_process = runtime.start_test_client(
        argv,
        log_dir=sess.client_log_dir,
        display=sess.client_display or sess.display,
        log_name=client_stdout_log,
    )
    params = {"base_url": r_base, "username": r_user, "password": r_pass,
              "port": r_port, "host": host, "client_pid": sess.test_client_process.pid,
              "client_log": str(client_out_log), "client_stdout_log": str(sess.client_log_dir / client_stdout_log),
              "connection_mode": mode, "connection_value": r_value,
              "target_connection_mode": target_connection_mode,
              "expected_manager_protocol_version": MANAGER_PROTOCOL_VERSION}
    _touch_session(sess)
    monitor_task: asyncio.Task[int | None] | None = None
    try:
        wait_timeout = float(os.getenv("ONEC_MCP_TEST_CLIENT_READY_TIMEOUT", "55"))
        await _wait_for_test_client_ready(sess, host, r_port, timeout=wait_timeout)
        if sess.test_client_process is not None:
            monitor_task = asyncio.create_task(_wait_for_test_client_process_exit(sess.test_client_process))
        attach_task = asyncio.create_task(sess.bridge.call("attach_test_client", params))
        wait_tasks: set[asyncio.Task[Any]] = {attach_task}
        if monitor_task is not None:
            wait_tasks.add(monitor_task)
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if monitor_task is not None and monitor_task in done and not attach_task.done():
            exit_code = monitor_task.result()
            attach_task.cancel()
            with suppress(asyncio.CancelledError):
                await attach_task
            message = f"Test client process exited during attach_test_client; exit_code={exit_code}"
            message = _append_file_tail(
                message,
                "Лог stdout/stderr клиента тестирования",
                sess.client_log_dir / "test_client_stdout.log",
            )
            message = _append_file_tail(
                message,
                "Лог 1С /Out клиента тестирования",
                sess.client_log_dir / "test_client_1c_out.log",
            )
            raise RuntimeError(message)
        for pending_task in pending:
            pending_task.cancel()
        result = await attach_task
    except Exception as exc:
        runtime.kill_process(sess.test_client_process, "test-client")
        sess.test_client_process = None
        attach_error = _test_client_startup_error_from_logs(sess)
        if attach_error:
            message = f"Test client attach error:\n{attach_error}"
            message = _append_file_tail(
                message,
                "Лог stdout/stderr клиента тестирования",
                sess.client_log_dir / "test_client_stdout.log",
            )
            message = _append_file_tail(
                message,
                "Лог 1С /Out клиента тестирования",
                sess.client_log_dir / "test_client_1c_out.log",
            )
            raise RuntimeError(message) from exc
        raise
    finally:
        if monitor_task is not None and not monitor_task.done():
            monitor_task.cancel()
    return _record_tool(sess.session_id, "connect_test_client", params, result)


def _auto_maximize_on_error(display: str) -> None:
    try:
        _maximize_external_window(title_contains="Клиент тестирования (1С:Предприятие)", display=display, width=None, height=None)
    except Exception as exc:
        logging.debug("auto maximize after failed connect_test_client failed: %s", exc)


def _auto_maximize_test_client_result(result: Any, display: str) -> None:
    try:
        mr = _maximize_external_window(title_contains="Клиент тестирования (1С:Предприятие)", display=display, width=None, height=None)
        if not mr.get("ok"):
            mr = _maximize_external_window(title_contains="", display=display, width=None, height=None)
        if isinstance(result, dict):
            result["auto_maximize"] = mr
    except Exception as exc:
        logging.debug("auto maximize after connect_test_client failed: %s", exc)
        if isinstance(result, dict):
            result["auto_maximize_error"] = str(exc)


@mcp.tool()
async def active_window(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return active 1C test client window state."""
    return await _bridge_call_recorded(session_id, "active_window", {})


@mcp.tool()
async def windows_list(*,
    max_depth: Annotated[int, "Serialization depth for each window"] = 2,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return all windows of the tested 1C client application."""
    return await _bridge_call_recorded(session_id, "windows_list", {"max_depth": max_depth})


@mcp.tool()
async def activate_window(
    index: Annotated[int, "Window index from windows_list; -1 means search by name/title"] = -1,
    title: Annotated[str, "Case-insensitive title substring"] = "",
    name: Annotated[str, "Exact window name when available"] = "",
    max_depth: Annotated[int, "Serialization depth for returned window"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Activate/switch to a tested 1C client window by index, title or name."""
    return await _bridge_call_recorded(
        session_id,
        "activate_window",
        {"index": index, "title": title, "name": name, "max_depth": max_depth},
    )


@mcp.tool()
async def goto_start_page(*,
    max_depth: Annotated[int, "Serialization depth for returned window"] = 2,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Navigate the main test-client window to the start page."""
    return await _bridge_call_recorded(
        session_id,
        "goto_start_page",
        {"max_depth": max_depth},
    )


@mcp.tool()
async def goto_previous_window(*,
    max_depth: Annotated[int, "Serialization depth for returned window"] = 2,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Navigate the main test-client window to the previous open window."""
    return await _bridge_call_recorded(
        session_id,
        "goto_previous_window",
        {"max_depth": max_depth},
    )


@mcp.tool()
async def goto_next_window(*,
    max_depth: Annotated[int, "Serialization depth for returned window"] = 2,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Navigate the main test-client window to the next open window."""
    return await _bridge_call_recorded(
        session_id,
        "goto_next_window",
        {"max_depth": max_depth},
    )



@mcp.tool()
async def wait_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object title substring"] = "",
    type: Annotated[str, "1C test object type"] = "",
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    max_depth: Annotated[int, "Serialization depth for returned object"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until an object appears in the tested client UI tree."""
    return await _bridge_call_recorded(session_id, "wait_object", {"name": name, "title": title, "type": type, "timeout": timeout, "max_depth": max_depth})


@mcp.tool()
async def wait_form(
    form_name: Annotated[str, "1C metadata form name, e.g. Catalog.X.Form.ItemForm"] = "",
    title: Annotated[str, "Form title substring"] = "",
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    max_depth: Annotated[int, "Serialization depth for returned form"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until a form appears."""
    return await _bridge_call_recorded(session_id, "wait_form", {"form_name": form_name, "title": title, "timeout": timeout, "max_depth": max_depth})


@mcp.tool()
async def wait_window(
    title: Annotated[str, "Window title substring"] = "",
    name: Annotated[str, "Window exact name when available"] = "",
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    max_depth: Annotated[int, "Serialization depth for returned window"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until a client window appears."""
    return await _bridge_call_recorded(session_id, "wait_window", {"title": title, "name": name, "timeout": timeout, "max_depth": max_depth})


@mcp.tool()
async def user_messages(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return current user messages shown in the active test-client window."""
    return await _bridge_call_recorded(session_id, "user_messages", {})


@mcp.tool()
async def close_user_messages_panel(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Close the user messages panel in the active test-client window."""
    return await _bridge_call_recorded(session_id, "close_user_messages_panel", {})


@mcp.tool()
async def current_error_info(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return current 1C script error information displayed in the tested client, if any."""
    return await _bridge_call_recorded(session_id, "current_error_info", {})


@mcp.tool()
async def set_file_dialog_result(
    paths: Annotated[str, "JSON array of file paths, or a single file path string"],
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Preload the result for the next 1C file selection dialog."""
    return await _bridge_call_recorded(session_id, "set_file_dialog_result", {"paths": paths})


@mcp.tool()
async def clear_file_dialog_result(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Clear queued file selection dialog results."""
    return await _bridge_call_recorded(session_id, "clear_file_dialog_result", {})


@mcp.tool()
async def field_info(
    field_name: Annotated[str, "Field name, or title when search_by_title=true"],
    search_by_title: Annotated[bool, "Search by visible title instead of technical name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return diagnostics for a form field: visibility, availability, readonly, kind, title."""
    return await _bridge_call_recorded(session_id, "field_info", {"field_name": field_name, "search_by_title": search_by_title})


@mcp.tool()
async def field_value_text(
    field_name: Annotated[str, "Field name, or title when search_by_title=true"],
    search_by_title: Annotated[bool, "Search by visible title instead of technical name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return field data presentation text via ПолучитьПредставлениеДанных()."""
    return await _bridge_call_recorded(session_id, "field_value_text", {"field_name": field_name, "search_by_title": search_by_title})


@mcp.tool()
async def field_tooltip(
    field_name: Annotated[str, "Field name, or title when search_by_title=true"],
    search_by_title: Annotated[bool, "Search by visible title instead of technical name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return field tooltip text."""
    return await _bridge_call_recorded(session_id, "field_tooltip", {"field_name": field_name, "search_by_title": search_by_title})


@mcp.tool()
async def field_dropdown_items(
    field_name: Annotated[str, "Field name, or title when search_by_title=true"],
    search_by_title: Annotated[bool, "Search by visible title instead of technical name"] = False,
    timeout: Annotated[int, "Dropdown formation timeout in seconds"] = 10,
    max_depth: Annotated[int, "Maximum depth used to serialize dropdown child objects"] = 3,
    search_depth: Annotated[int, "Maximum depth used to find the field; 0 means auto-expand search depth"] = 0,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open a form field dropdown and return its visible choice items when exposed by the 1C test-client API."""
    return await _bridge_call_recorded(
        session_id,
        "field_dropdown_items",
        {
            "field_name": field_name,
            "search_by_title": search_by_title,
            "timeout": timeout,
            "max_depth": max_depth,
            "search_depth": search_depth,
        },
    )


@mcp.tool()
async def form_next_element(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move focus to the next form element and return the current element."""
    return await _bridge_call_recorded(session_id, "form_next_element", {})


@mcp.tool()
async def form_previous_element(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move focus to the previous form element and return the current element."""
    return await _bridge_call_recorded(session_id, "form_previous_element", {})


@mcp.tool()
async def form_state(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return active form availability/modified/readonly state."""
    return await _bridge_call_recorded(session_id, "form_state", {})


@mcp.tool()
async def form_default_button(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return the active form default button, when available."""
    return await _bridge_call_recorded(session_id, "form_default_button", {})


@mcp.tool()
async def wait_form_closed(
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until the active form closes."""
    return await _bridge_call_recorded(session_id, "wait_form_closed", {"timeout": timeout})


@mcp.tool()
async def object_command_panel(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object title substring"] = "",
    type: Annotated[str, "1C test object type"] = "",
    max_depth: Annotated[int, "Serialization depth for returned panel"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return command panel for a found UI object, when supported."""
    return await _bridge_call_recorded(session_id, "object_command_panel", {"name": name, "title": title, "type": type, "max_depth": max_depth})


@mcp.tool()
async def object_context_menu(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object title substring"] = "",
    type: Annotated[str, "1C test object type"] = "",
    max_depth: Annotated[int, "Serialization depth for returned menu"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return context menu for a found UI object, when supported."""
    return await _bridge_call_recorded(session_id, "object_context_menu", {"name": name, "title": title, "type": type, "max_depth": max_depth})


@mcp.tool()
async def window_command_interface(
    max_depth: Annotated[int, "Serialization depth for returned command interface"] = 2,
    window_title: Annotated[str, "Optional window title substring to activate before reading command interface"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return active window command interface, when supported."""
    return await _bridge_call_recorded(session_id, "window_command_interface", {"max_depth": max_depth, "window_title": window_title})


@mcp.tool()
async def command_interface_sections(
    window_title: Annotated[str, "Optional window title substring to activate before reading command interface"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return top-level sections of the active window command interface."""
    return await _bridge_call_recorded(session_id, "command_interface_sections", {"window_title": window_title})


@mcp.tool()
async def command_interface_commands(
    section: Annotated[str, "Optional top-level section title substring, e.g. 'Главное' or 'Администрирование'"] = "",
    max_depth: Annotated[int, "Serialization depth for returned command tree"] = 4,
    window_title: Annotated[str, "Optional window title substring to activate before reading command interface"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return command-interface command tree and flattened command list, optionally filtered by section."""
    return await _bridge_call_recorded(
        session_id,
        "command_interface_commands",
        {"section": section, "max_depth": max_depth, "window_title": window_title},
    )


@mcp.tool()
async def command_interface_search(
    query: Annotated[str, "Search text to match against command title, path and navigation URL"],
    section: Annotated[str, "Optional top-level section title substring to activate before searching"] = "",
    max_results: Annotated[int, "Maximum number of matches to return; 0 means no limit"] = 20,
    window_title: Annotated[str, "Optional window title substring to activate before reading command interface"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Search live command-interface commands by title, path or navigation URL."""
    return await _bridge_call_recorded(
        session_id,
        "command_interface_search",
        {"query": query, "section": section, "max_results": max_results, "window_title": window_title},
    )


@mcp.tool()
async def group_expand(name: Annotated[str, "Group technical name"] = "", title: Annotated[str, "Group title substring"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Expand a collapsible form group."""
    return await _bridge_call_recorded(session_id, "group_expand", {"name": name, "title": title})


@mcp.tool()
async def group_collapse(name: Annotated[str, "Group technical name"] = "", title: Annotated[str, "Group title substring"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Collapse a collapsible form group."""
    return await _bridge_call_recorded(session_id, "group_collapse", {"name": name, "title": title})


@mcp.tool()
async def group_current_page(name: Annotated[str, "Group technical name"] = "", title: Annotated[str, "Group title substring"] = "", max_depth: Annotated[int, "Serialization depth for returned page"] = 2, *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Return current page of a page-group form element."""
    return await _bridge_call_recorded(session_id, "group_current_page", {"name": name, "title": title, "max_depth": max_depth})


@mcp.tool()
async def group_activate_page(name: Annotated[str, "Page-group technical name"] = "", title: Annotated[str, "Page-group title substring"] = "", page_name: Annotated[str, "Target page technical name"] = "", page_title: Annotated[str, "Target page title"] = "", max_depth: Annotated[int, "Search depth"] = 8, *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Activate/switch to another page in a page-group form element."""
    return await _bridge_call_recorded(session_id, "group_activate_page", {"name": name, "title": title, "page_name": page_name, "page_title": page_title, "max_depth": max_depth})


@mcp.tool()
async def group_state(name: Annotated[str, "Group technical name"] = "", title: Annotated[str, "Group title substring"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Return form group expanded/visible/available state."""
    return await _bridge_call_recorded(session_id, "group_state", {"name": name, "title": title})


@mcp.tool()
async def decoration_links(name: Annotated[str, "Decoration technical name"] = "", title: Annotated[str, "Decoration title substring"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Return hyperlink presentations from a formatted-string decoration."""
    return await _bridge_call_recorded(session_id, "decoration_links", {"name": name, "title": title})


@mcp.tool()
async def decoration_click_link(name: Annotated[str, "Decoration technical name"] = "", title: Annotated[str, "Decoration title substring"] = "", presentation: Annotated[str, "Hyperlink presentation to click"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Click a hyperlink inside a formatted-string decoration."""
    return await _bridge_call_recorded(session_id, "decoration_click_link", {"name": name, "title": title, "presentation": presentation})


@mcp.tool()
async def decoration_click(name: Annotated[str, "Decoration technical name"] = "", title: Annotated[str, "Decoration title substring"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Click a form decoration."""
    return await _bridge_call_recorded(session_id, "decoration_click", {"name": name, "title": title})


@mcp.tool()
async def ui_tree(max_depth: Annotated[int, "Maximum UI tree depth"] = 4,
    include_rag_types: Annotated[bool, "Enrich UI elements with best-effort types from the local RAG index"] = True,
    include_hidden: Annotated[bool, "Include invisible form elements; visible/available flags are always returned"] = False,
    rag_snapshot: Annotated[str, "Optional RAG snapshot name/id used for type enrichment"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return a serialized tree of the active window/forms/elements.

    When RAG data is available, each matching element gets `rag_type` and `rag`
    fields with metadata-derived type/source information.  Runtime 1C UI data is
    returned unchanged when the RAG index has no match. Invisible elements are
    omitted by default; pass include_hidden=true to inspect the full 1C form tree.
    """
    tree = await _bridge_call_recorded(session_id, "ui_tree", {"max_depth": max_depth, "include_hidden": include_hidden})
    if include_rag_types:
        sess = _session(session_id)
        if _RAG_ENABLED and sess.rag_enabled:
            tree = _enrich_ui_tree_with_rag_types(tree, snapshot=rag_snapshot or sess.rag_snapshot or None)
            if isinstance(tree, dict):
                tree["rag_diagnostic"] = _rag_diagnostic(session_id, rag_snapshot)
        elif isinstance(tree, dict):
            tree["rag_diagnostic"] = _rag_diagnostic(session_id, rag_snapshot)
    return tree


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
async def open_navigation_link(navigation_link: Annotated[str, "Explicit 1C navigation link, e.g. e1cib/list/..."],
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open an explicit 1C navigation link in the active test-client window.

    This tool intentionally rejects arbitrary form command names such as
    `ФормаНайти` or `СледующаяСтрока`. Use first-class MCP tools for form
    actions, table navigation, search/settings, and dynamic-list operations.
    """
    if not navigation_link.startswith("e1cib/"):
        raise ValueError(f"open_navigation_link accepts only explicit e1cib/... navigation links; rejected: {navigation_link}")
    return await _bridge_call_recorded(session_id, "open_navigation_link", {"navigation_link": navigation_link})


@mcp.tool()
async def current_element(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return the current active form element."""
    return await _bridge_call_recorded(session_id, "current_element", {})


@mcp.tool()
async def field_dropdown_state(
    field_name: Annotated[str, "Field technical name or title"],
    search_by_title: Annotated[bool, "Search field by visible title"] = False,
    timeout: Annotated[int, "Wait timeout for dropdown formation"] = 60,
    max_depth: Annotated[int, "Maximum search depth"] = 32,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return whether a field dropdown is open and its visible choice items."""
    return await _bridge_call_recorded(
        session_id,
        "field_dropdown_state",
        {"field_name": field_name, "search_by_title": search_by_title, "timeout": timeout, "max_depth": max_depth},
    )


@mcp.tool()
async def field_dropdown_close(
    field_name: Annotated[str, "Field technical name or title"],
    search_by_title: Annotated[bool, "Search field by visible title"] = False,
    max_depth: Annotated[int, "Maximum search depth"] = 32,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Close an open dropdown for a form field, if it is open."""
    return await _bridge_call_recorded(
        session_id,
        "field_dropdown_close",
        {"field_name": field_name, "search_by_title": search_by_title, "max_depth": max_depth},
    )


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
    include_text: Annotated[bool, "Include tabular document cells"] = False,
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    row_start: Annotated[int, "First row to read when include_text=true (1-based)"] = 1,
    row_end: Annotated[int, "Last row to read when include_text=true; 0 = row_start + max_rows - 1"] = 0,
    column_start: Annotated[int, "First column to read when include_text=true (1-based)"] = 1,
    column_end: Annotated[int, "Last column to read when include_text=true; 0 = column_start + max_columns - 1"] = 0,
    max_rows: Annotated[int, "Maximum rows to read when row_end=0; 0 = all rows"] = 50,
    max_columns: Annotated[int, "Maximum columns to read when column_end=0; 0 = all columns"] = 20,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """List tabular document fields with dimensions; optionally include a cells range.

    include_text is intended for small/partial ranges. For large tabular
    documents, save/export the report to a file and analyze that file instead.
    """
    return await _bridge_call_recorded(session_id, "tabular_documents", {"include_text": include_text, "max_depth": max_depth, "row_start": row_start, "row_end": row_end, "column_start": column_start, "column_end": column_end, "max_rows": max_rows, "max_columns": max_columns})


@mcp.tool()
async def tabular_document_text(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    row_start: Annotated[int, "First row to read (1-based)"] = 1,
    row_end: Annotated[int, "Last row to read; 0 = row_start + max_rows - 1"] = 0,
    column_start: Annotated[int, "First column to read (1-based)"] = 1,
    column_end: Annotated[int, "Last column to read; 0 = column_start + max_columns - 1"] = 0,
    max_rows: Annotated[int, "Maximum rows to read when row_end=0; 0 = all rows"] = 50,
    max_columns: Annotated[int, "Maximum columns to read when column_end=0; 0 = all columns"] = 20,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return dimensions and a cells range of a tabular document field in the active window.

    Use this for small tables or partial reads (headers, totals, spot checks). For
    large reports/tabular documents, prefer tabular_document_save/report export
    and analyze the saved PDF/XLSX/TXT/HTML file instead of reading UI cells one
    by one.
    """
    return await _bridge_call_recorded(session_id, "tabular_document_text", {"name": name, "title": title, "max_depth": max_depth, "row_start": row_start, "row_end": row_end, "column_start": column_start, "column_end": column_end, "max_rows": max_rows, "max_columns": max_columns})


@mcp.tool()
async def tabular_document_save(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    path: Annotated[str, "Output file path; empty = auto-generated temp path"] = "",
    format: Annotated[str, "Output format: pdf | xlsx | xls | html | txt"] = "pdf",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Save a tabular document field to a file in the selected format."""
    result = await _bridge_call_recorded(session_id, "tabular_document_save", {"name": name, "title": title, "path": path, "format": format, "max_depth": max_depth})
    return _select_saved_tabular_document_file(result, format)


@mcp.tool()
async def tabular_document_state_display(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return the state display of a tabular document field, when exposed by 1C."""
    return await _bridge_call_recorded(session_id, "tabular_document_state_display", {"name": name, "title": title, "max_depth": max_depth})


def _select_saved_tabular_document_file(result: Any, format: str) -> Any:
    if not isinstance(result, dict):
        return result
    files = result.get("files")
    if not isinstance(files, list) or not files:
        if result.get("found", True) is not False:
            result["saved"] = False
            result.setdefault("error", f"No files were produced for requested tabular document format: {format.lower()}")
        return result
    selected = _find_saved_tabular_document_file(files, format)
    if selected is None:
        result["selected"] = None
        result["saved"] = False
        result["error"] = f"No produced file matches requested tabular document format: {format.lower()}"
        return result
    selected_path = selected.get("path")
    requested_path = result.get("path")
    if isinstance(selected_path, str) and isinstance(requested_path, str) and requested_path and selected_path != requested_path:
        with suppress(Exception):
            shutil.copyfile(selected_path, requested_path)
            selected = dict(selected)
            selected["copied_to"] = requested_path
            result["path"] = requested_path
    result["selected"] = selected
    result["saved"] = True
    return result


def _find_saved_tabular_document_file(files: list[Any], format: str) -> dict[str, Any] | None:
    wanted = format.lower()
    for item in files:
        if not isinstance(item, dict):
            continue
        file_path = item.get("path")
        if not isinstance(file_path, str):
            continue
        p = Path(file_path)
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
            header = data[:8]
        except OSError:
            continue
        if wanted == "pdf" and header.startswith(b"%PDF"):
            return item
        if wanted == "xlsx" and _zip_contains(p, "xl/workbook.xml"):
            return item
        if wanted == "docx" and _zip_contains(p, "word/document.xml"):
            return item
        if wanted == "xls" and header.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            return item
        if wanted in {"html", "htm"} and (data.startswith(b"<") or data.startswith(b"\xef\xbb\xbf<")):
            return item
        if wanted == "txt" and _looks_like_plain_text(data):
            return item
    return None


def _zip_contains(path: Path, member: str) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            return member in archive.namelist()
    except Exception:
        return False


def _looks_like_plain_text(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:4096]
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:]
    stripped = sample.lstrip().lower()
    if stripped.startswith((b"<", b"<!doctype", b"<html")):
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False



@mcp.tool()
async def toggle_field(
    field_name: Annotated[str, "Field technical name"],
    value: Annotated[bool, "Desired checkbox value"] = False,
    max_depth: Annotated[int, "Max UI tree depth for nested fields"] = 20,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a checkbox-like field using the 1C test client API."""
    return await _bridge_call_recorded(session_id, "toggle_field", {"field_name": field_name, "value": value, "max_depth": max_depth})


@mcp.tool()
async def set_field_value(
    field_name: Annotated[str, "Field technical name or title"],
    value: Annotated[str, "Value to set"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    max_depth: Annotated[int, "Max UI tree depth for nested fields"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a value in a form field by name or title."""
    return await _bridge_call_recorded(session_id, "set_field_value", {"field_name": field_name, "value": value, "search_by_title": search_by_title, "max_depth": max_depth})


@mcp.tool()
async def activate_field(
    field_name: Annotated[str, "Field technical name or title"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    max_depth: Annotated[int, "Max UI tree depth for nested fields"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Activate/focus a form field by name or title."""
    return await _bridge_call_recorded(session_id, "activate_field", {"field_name": field_name, "search_by_title": search_by_title, "max_depth": max_depth})


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


def _table_row_pairs(row: Any) -> dict[str, str]:
    pairs = row.get("pairs", []) if isinstance(row, dict) else []
    result: dict[str, str] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        key = str(pair.get("key", ""))
        value = str(pair.get("value", ""))
        result[key] = value
        normalized_key = re.sub(r"\\s+", " ", key).strip()
        if normalized_key and normalized_key != key:
            result.setdefault(normalized_key, value)
    return result


def _row_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in row:
            return row[key]
    wanted = {re.sub(r"\\s+", "", key).casefold() for key in keys}
    for key, value in row.items():
        if re.sub(r"\\s+", "", key).casefold() in wanted:
            return value
    return ""


def _is_truthy_1c(value: str) -> bool:
    return value.strip().casefold() in {"да", "истина", "true", "1", "yes", "✓"}


def _is_not_deleted_catalog_row(row: dict[str, str]) -> bool:
    delete_mark = _row_value(row, "Пометка удаления", "ПометкаУдаления", "Пометка\nудаления")
    return not _is_truthy_1c(delete_mark)


def _parse_1c_date_text(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.min


def _parse_int_text(value: str) -> int:
    match = re.search(r"-?\d+", value.replace("\xa0", " "))
    return int(match.group(0)) if match else 0


def _extension_catalog_row_sort_key(row: dict[str, str]) -> tuple[datetime, int]:
    return (
        _parse_1c_date_text(_row_value(row, "Дата последнего использования", "ДатаПоследнегоИспользования", "Дата последнего\nиспользования")),
        _parse_int_text(_row_value(row, "Порядковый номер", "Код", "Порядковый\nномер")),
    )


def _parse_extension_metadata_description(description: str) -> list[dict[str, str]]:
    extensions: list[dict[str, str]] = []
    pattern = re.compile(r"^(?P<name>.+?)\s*\((?P<version>[^)]*)\)(?:\s+(?P<hash>\S+))?(?:\s+БезопасныйРежим:(?P<safe_mode>\S+))?")
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("EF_") or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            extensions.append({"name": line, "version": "", "hash": "", "safe_mode": ""})
            continue
        extensions.append({
            "name": match.group("name").strip(),
            "version": (match.group("version") or "").strip(),
            "hash": match.group("hash") or "",
            "safe_mode": match.group("safe_mode") or "",
        })
    return extensions


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", value or "")]
    return tuple(numbers) if numbers else (0,)


def _version_distance(left: str, right: str) -> tuple[int, int]:
    left_parts = _version_tuple(left)
    right_parts = _version_tuple(right)
    width = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (width - len(left_parts))
    padded_right = right_parts + (0,) * (width - len(right_parts))
    return (sum(abs(a - b) for a, b in zip(padded_left, padded_right)), abs(len(left_parts) - len(right_parts)))


def _read_text_head(path: Path, max_chars: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as file:
            return file.read(max_chars)
    except OSError:
        return ""


def _tag_value(text: str, tag: str) -> str:
    match = re.search(rf"<[^:>/]*:?{re.escape(tag)}(?:\s[^>]*)?>(.*?)</[^:>/]*:?{re.escape(tag)}>", text, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _source_metadata(path: str) -> dict[str, str]:
    root = Path(path)
    candidates = [root / "Configuration" / "Configuration.mdo", root / "Configuration.xml"]
    text = ""
    for candidate in candidates:
        if candidate.exists():
            text = _read_text_head(candidate)
            break
    return {
        "name": _tag_value(text, "Name") or _tag_value(text, "name") or root.name,
        "synonym": _tag_value(text, "Synonym") or _tag_value(text, "synonym"),
        "version": _tag_value(text, "Version") or _tag_value(text, "version"),
    }


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", (value or "").casefold())


def _name_similarity(left: str, right: str) -> float:
    left_norm = _normalize_match_text(left)
    right_norm = _normalize_match_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))
    left_tokens = set(re.findall(r"[0-9a-zа-яё]+", (left or "").casefold()))
    right_tokens = set(re.findall(r"[0-9a-zа-яё]+", (right or "").casefold()))
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) if left_tokens and right_tokens else 0.0


def _source_display_name(source: dict[str, Any], metadata: dict[str, str]) -> str:
    return metadata.get("name") or source.get("name") or Path(str(source.get("path") or "")).name


def _safe_snapshot_part(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_.-]+", "_", value or "")
    return normalized.strip("_") or "Runtime"


def _rag_match_public(match: dict[str, Any]) -> dict[str, Any]:
    candidate = match.get("candidate") or match.get("best_candidate") or {}
    source = candidate.get("source") or {}
    metadata = candidate.get("metadata") or {}
    return {
        "matched": bool(match.get("matched")),
        "reason": match.get("reason", ""),
        "warning": match.get("warning", ""),
        "source_name": source.get("name", ""),
        "source_path": source.get("path", ""),
        "source_kind": source.get("kind", ""),
        "source_version": candidate.get("source_version", "") or metadata.get("version", ""),
        "source_metadata_name": metadata.get("name", ""),
        "source_metadata_synonym": metadata.get("synonym", ""),
        "similarity": candidate.get("similarity", 0),
        "exact_version": bool(candidate.get("exact_version")),
    }


def _best_rag_source_match(target_name: str, target_version: str, sources: list[dict[str, Any]], expected_kind: str | None = None) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for source in sources:
        if expected_kind and str(source.get("kind") or "") not in {expected_kind, "configuration" if expected_kind == "base" else expected_kind}:
            continue
        metadata = _source_metadata(str(source.get("path") or ""))
        source_names = [str(source.get("name") or ""), Path(str(source.get("path") or "")).name, metadata.get("name", ""), metadata.get("synonym", "")]
        similarity = max((_name_similarity(target_name, name) for name in source_names), default=0.0)
        source_version = metadata.get("version", "")
        exact_version = bool(target_version) and source_version == target_version
        distance = _version_distance(target_version, source_version) if target_version and source_version else (999_999, 999_999)
        candidates.append({
            "source": source,
            "metadata": metadata,
            "similarity": similarity,
            "source_version": source_version,
            "exact_version": exact_version,
            "version_distance": distance,
        })
    if not candidates:
        return {"matched": False, "reason": "no_sources"}
    exact_name = [item for item in candidates if item["similarity"] >= 0.99]
    pool = exact_name or [item for item in candidates if item["similarity"] >= 0.35]
    if not pool:
        best = max(candidates, key=lambda item: item["similarity"])
        return {"matched": False, "reason": "no_name_match", "best_candidate": best}
    exact_versions = [item for item in pool if item["exact_version"]]
    if exact_versions:
        best = max(exact_versions, key=lambda item: (item["similarity"], str(item["source"].get("name") or "")))
        return {"matched": True, "warning": "", "candidate": best}
    best = min(pool, key=lambda item: (item["version_distance"], -item["similarity"], str(item["source"].get("name") or "")))
    warning = ""
    if target_version:
        warning = f"Exact version {target_version} for {target_name} not found; selected nearest version {best['source_version'] or 'unknown'} from source {best['source'].get('name')}."
    else:
        warning = f"Runtime version for {target_name} is empty; selected source {best['source'].get('name')} without exact version check."
    return {"matched": True, "warning": warning, "candidate": best}


def _metadata_cache_key(session_id: str) -> str:
    session = _SESSIONS.get(session_id)
    if session and session.base_url:
        return f"{session_id}:{session.base_url}"
    return session_id


def _metadata_object_from_row(row: dict[str, str]) -> dict[str, str]:
    presentation = _row_value(row, "Ссылка", "Наименование")
    full_name = _row_value(row, "Полное имя", "ПолноеИмя", "СписокПолноеИмя")
    synonym = _row_value(row, "Синоним")
    if not full_name:
        full_name = _metadata_full_name_from_presentation(presentation)
    metadata_type = _metadata_type_from_full_name(full_name) or _metadata_type_from_presentation(presentation)
    name = _metadata_name_from_full_name(full_name)
    if not synonym:
        synonym = _metadata_synonym_from_presentation(presentation)
    is_group = not full_name and bool(presentation) and not presentation.startswith("?") and not re.search(r"\([^)]+\)$", presentation)
    return {
        "presentation": presentation,
        "full_name": full_name,
        "type": metadata_type,
        "name": name,
        "synonym": synonym,
        "is_group": "Да" if is_group else "Нет",
    }


def _metadata_full_name_from_presentation(presentation: str) -> str:
    text = presentation.strip()
    if text.startswith("?"):
        return text[1:].strip()
    match = re.match(r"^(.+?)\s*\(([^()]+)\)$", text)
    if match:
        synonym = match.group(1).strip()
        if "." in synonym:
            return synonym
        return ""
    return text if "." in text else ""


def _metadata_type_from_full_name(full_name: str) -> str:
    return full_name.split(".", 1)[0] if "." in full_name else ""


def _metadata_name_from_full_name(full_name: str) -> str:
    return full_name.rsplit(".", 1)[-1] if "." in full_name else ""


def _metadata_type_from_presentation(presentation: str) -> str:
    match = re.search(r"\(([^()]+)\)$", presentation.strip())
    return match.group(1).strip() if match else ""


def _metadata_synonym_from_presentation(presentation: str) -> str:
    text = presentation.strip()
    match = re.match(r"^(.+?)\s*\(([^()]+)\)$", text)
    return match.group(1).strip() if match else ""


def _match_metadata_object(item: dict[str, str], query: str) -> bool:
    if not query:
        return True
    needle = query.casefold()
    return any(needle in str(item.get(field, "")).casefold() for field in ("presentation", "full_name", "type", "name", "synonym"))


async def _load_metadata_objects_from_catalog(session_id: str, limit: int) -> dict[str, Any]:
    navigation_link = "e1cib/list/Справочник.ИдентификаторыОбъектовМетаданных"
    await _bridge_call_recorded(session_id, "open_navigation_link", {"navigation_link": navigation_link})
    await _bridge_call_recorded(session_id, "click_button", {"name": "ФормаСписок", "search_by_title": False})
    rows_result = await _bridge_call_recorded(session_id, "table_rows", {"name": "Список", "title": "", "limit": limit, "max_depth": 8})
    rows = rows_result.get("rows", []) if isinstance(rows_result, dict) else []
    parsed_rows = [_table_row_pairs(row) for row in rows]
    actual_rows = [row for row in parsed_rows if _is_not_deleted_catalog_row(row)]
    objects = [_metadata_object_from_row(row) for row in actual_rows]
    groups = [item for item in objects if item["is_group"] == "Да"]
    metadata_items = [item for item in objects if item["is_group"] != "Да"]
    type_counts: dict[str, int] = {}
    for item in metadata_items:
        metadata_type = item["type"] or "<unknown>"
        type_counts[metadata_type] = type_counts.get(metadata_type, 0) + 1
    return {
        "objects": objects,
        "groups": groups,
        "type_counts": dict(sorted(type_counts.items())),
        "rows_count": len(parsed_rows),
        "non_deleted_rows_count": len(actual_rows),
        "objects_count": len(objects),
        "groups_count": len(groups),
        "metadata_objects_count": len(metadata_items),
        "navigation_link": navigation_link,
        "source": "Справочник.ИдентификаторыОбъектовМетаданных",
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
        "limit": limit,
    }


@mcp.tool()
async def metadata_objects_from_catalog(
    query: Annotated[str, "Substring to search in presentation, full name, type, name or synonym"] = "",
    limit: Annotated[int, "Maximum rows to read from the catalog when cache is refreshed"] = 5000,
    max_results: Annotated[int, "Maximum matched objects to return"] = 200,
    refresh_cache: Annotated[bool, "Force rereading the hierarchical catalog instead of using the session cache"] = False,
    include_groups: Annotated[bool, "Include hierarchy group rows in results"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Return/search metadata objects from the hierarchical catalog `Справочник.ИдентификаторыОбъектовМетаданных`.

    The first call reads the catalog in flat list mode, skips rows marked for
    deletion when that field is available in the form, normalizes objects to
    `presentation/full_name/type/name/synonym`, and stores them in an in-memory
    per-session cache. Later calls use the cache unless `refresh_cache=true`.
    """
    cache_key = _metadata_cache_key(session_id)
    cache_hit = cache_key in _METADATA_OBJECTS_CACHE and not refresh_cache
    if cache_hit:
        catalog_data = _METADATA_OBJECTS_CACHE[cache_key]
    else:
        catalog_data = await _load_metadata_objects_from_catalog(session_id, limit)
        _METADATA_OBJECTS_CACHE[cache_key] = catalog_data

    all_objects = catalog_data["objects"]
    searchable = all_objects if include_groups else [item for item in all_objects if item["is_group"] != "Да"]
    matches = [item for item in searchable if _match_metadata_object(item, query)]
    return {
        "found": bool(matches),
        "cache_hit": cache_hit,
        "query": query,
        "objects": matches[:max_results],
        "matched_count": len(matches),
        "returned_count": min(len(matches), max_results),
        "truncated": len(matches) > max_results,
        "type_counts": catalog_data["type_counts"],
        "rows_count": catalog_data["rows_count"],
        "non_deleted_rows_count": catalog_data["non_deleted_rows_count"],
        "objects_count": catalog_data["objects_count"],
        "groups_count": catalog_data["groups_count"],
        "metadata_objects_count": catalog_data["metadata_objects_count"],
        "navigation_link": catalog_data["navigation_link"],
        "source": catalog_data["source"],
        "loaded_at": catalog_data["loaded_at"],
    }


@mcp.tool()
async def metadata_objects_catalog_cache_clear(
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Clear cached metadata objects for the current session."""
    cache_key = _metadata_cache_key(session_id)
    existed = cache_key in _METADATA_OBJECTS_CACHE
    _METADATA_OBJECTS_CACHE.pop(cache_key, None)
    return {"cleared": existed, "cache_key": cache_key}


@mcp.tool()
async def configuration_name_version_from_subsystems(
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Open the 'Subsystem versions' information register and return the main configuration name/version.

    The tool reads `РегистрСведений.ВерсииПодсистем` via the list output
    command so that columns available to the dynamic list, including
    `Это основная конфигурация`, are visible even when they are not displayed
    in the default form table.
    """
    navigation_link = "e1cib/list/РегистрСведений.ВерсииПодсистем"
    await _bridge_call_recorded(session_id, "open_navigation_link", {"navigation_link": navigation_link})

    output_result = await _bridge_call_recorded(session_id, "dynamic_list_output", {
        "button": "",
        "wait_seconds": 3,
        "max_depth": 8,
    })
    tabular_docs = output_result.get("tabular_documents", {}) if isinstance(output_result, dict) else {}
    items = tabular_docs.get("items", []) if isinstance(tabular_docs, dict) else []

    main: dict[str, str] = {}
    cells: list[list[str]] = []
    if items:
        doc_name = items[0].get("name", "")
        text_result = await _bridge_call_recorded(session_id, "tabular_document_text", {
            "name": doc_name,
            "max_rows": 50,
            "max_columns": 20,
        })
        cells = text_result.get("cells", []) if isinstance(text_result, dict) else []
        main = _find_main_configuration_in_tabular_cells(cells)

    return {
        "found": bool(main),
        "configuration_name": main.get("configuration_name", ""),
        "configuration_version": main.get("configuration_version", ""),
        "main_configuration_row": main,
        "main_configuration_rows_count": 1 if main else 0,
        "rows_count": max(0, len(cells) - 1),
        "navigation_link": navigation_link,
        "source": "РегистрСведений.ВерсииПодсистем",
    }


def _find_main_configuration_in_tabular_cells(cells: list[list[str]]) -> dict[str, str]:
    """Scan tabular document cells for the main configuration row."""
    if not cells or len(cells) < 2:
        return {}

    header_row_idx = -1
    name_col = -1
    version_col = -1
    main_cfg_col = -1
    for i, row in enumerate(cells):
        local_name_col = local_version_col = local_main_cfg_col = -1
        for j, cell in enumerate(row):
            cell_norm = str(cell).strip()
            if cell_norm == "Имя подсистемы":
                local_name_col = j
            elif cell_norm == "Версия":
                local_version_col = j
            elif cell_norm == "Это основная конфигурация":
                local_main_cfg_col = j
        if local_name_col >= 0 and local_version_col >= 0 and local_main_cfg_col >= 0:
            header_row_idx = i
            name_col = local_name_col
            version_col = local_version_col
            main_cfg_col = local_main_cfg_col
            break

    if header_row_idx < 0:
        return {}

    for row in cells[header_row_idx + 1:]:
        if len(row) <= max(name_col, version_col, main_cfg_col):
            continue
        if _is_truthy_1c(str(row[main_cfg_col])):
            return {
                "configuration_name": str(row[name_col]).strip(),
                "configuration_version": str(row[version_col]).strip(),
                "Это основная конфигурация": str(row[main_cfg_col]).strip(),
            }

    return {}


@mcp.tool()
async def installed_extensions_from_catalog(
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Open `Справочник.ВерсииРасширений` and return installed extensions from the latest non-deleted metadata snapshot.

    The tool reads catalog rows, ignores rows marked for deletion, takes the latest
    row by last-use date/code and parses `Описание метаданных`. Metadata lines
    starting with `EF_` and the main configuration line starting with `#` are not
    returned as extensions.
    """
    navigation_link = "e1cib/list/Справочник.ВерсииРасширений"
    await _bridge_call_recorded(session_id, "open_navigation_link", {"navigation_link": navigation_link})
    rows_result = await _bridge_call_recorded(session_id, "table_rows", {"name": "Список", "title": "", "limit": 500, "max_depth": 8})
    rows = rows_result.get("rows", []) if isinstance(rows_result, dict) else []

    parsed_rows = [_table_row_pairs(row) for row in rows]
    actual_rows = [row for row in parsed_rows if _is_not_deleted_catalog_row(row)]
    selected_row = max(actual_rows, key=_extension_catalog_row_sort_key) if actual_rows else {}
    description = _row_value(selected_row, "Описание метаданных", "ОписаниеМетаданных")
    extensions = _parse_extension_metadata_description(description)

    return {
        "found": bool(selected_row),
        "extensions": extensions,
        "extensions_count": len(extensions),
        "selected_row": selected_row,
        "selected_row_code": _row_value(selected_row, "Порядковый номер", "Код", "Порядковый\nномер"),
        "selected_row_last_use_date": _row_value(selected_row, "Дата последнего использования", "ДатаПоследнегоИспользования", "Дата последнего\nиспользования"),
        "rows_count": len(parsed_rows),
        "non_deleted_rows_count": len(actual_rows),
        "navigation_link": navigation_link,
        "source": "Справочник.ВерсииРасширений",
    }


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
    max_depth: Annotated[int, "Maximum UI search depth"] = 20,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a field in the currently edited row of a form table.

    Finds the editor field for the column (e.g. ТоварыНоменклатура), activates it,
    delegates to the general set_field_value logic, ends row editing, and verifies
    the value in the current table row."""
    return await _bridge_call_recorded(session_id, "table_set_field", {"table": table, "field": field, "value": value, "search_by_title": search_by_title, "max_depth": max_depth})


@mcp.tool()
async def table_set_row_values(
    table: Annotated[str, "Table technical name"],
    values: Annotated[str, "JSON object mapping column names/titles to values"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set multiple fields in the current row of a form table and verify each value."""
    return await _bridge_call_recorded(session_id, "table_set_row_values", {"table": table, "values": values, "search_by_title": search_by_title})


@mcp.tool()
async def set_form_fields(
    values: Annotated[str, "JSON object mapping field names/titles to values"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    max_depth: Annotated[int, "Maximum UI search depth"] = 20,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set multiple ordinary form fields using the same logic as set_field_value."""
    return await _bridge_call_recorded(session_id, "set_form_fields", {"values": values, "search_by_title": search_by_title, "max_depth": max_depth})


@mcp.tool()
async def table_choose_field_from_list(
    table: Annotated[str, "Table technical name"],
    field: Annotated[str, "Column field technical name or title"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open the selection form for a reference field in a form table row."""
    return await _bridge_call_recorded(session_id, "table_choose_field_from_list", {"table": table, "field": field, "search_by_title": search_by_title})


@mcp.tool()
async def table_edit_info(
    table: Annotated[str, "Table technical name"] = "ДополнительнаяИнформация",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return diagnostics for currently edited table row and its child fields."""
    return await _bridge_call_recorded(session_id, "table_edit_info", {"table": table})


@mcp.tool()
async def table_select_all(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Select all rows in a form table."""
    return await _bridge_call_recorded(session_id, "table_select_all", {"name": name, "title": title})


@mcp.tool()
async def table_clear_selection(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Clear all row selection in a form table."""
    return await _bridge_call_recorded(session_id, "table_clear_selection", {"name": name, "title": title})


@mcp.tool()
async def table_delete_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Delete the current row in a form table."""
    return await _bridge_call_recorded(session_id, "table_delete_row", {"name": name, "title": title})


@mcp.tool()
async def table_copy_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Copy the current row in a form table."""
    return await _bridge_call_recorded(session_id, "table_copy_row", {"name": name, "title": title})


@mcp.tool()
async def table_toggle_delete_mark(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Toggle the delete mark on the current row."""
    return await _bridge_call_recorded(session_id, "table_toggle_delete_mark", {"name": name, "title": title})


@mcp.tool()
async def table_begin_edit(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Begin editing the current row in a form table."""
    return await _bridge_call_recorded(session_id, "table_begin_edit", {"name": name, "title": title})


@mcp.tool()
async def table_end_edit(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """End editing the current row in a form table."""
    return await _bridge_call_recorded(session_id, "table_end_edit", {"name": name, "title": title})


@mcp.tool()
async def table_editing_state(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table title substring"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return whether a form table is currently in edit mode."""
    return await _bridge_call_recorded(session_id, "table_editing_state", {"name": name, "title": title})


@mcp.tool()
async def dynamic_list_available_fields(
    object: Annotated[str, "1C metadata object full name, e.g. Catalog.ПС_Справочник1; empty = derive from active window"] = "",
    rag_snapshot: Annotated[str, "Optional RAG snapshot name/id"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return RAG-known fields available for dynamic-list filtering and ordering."""
    sess = _session(session_id)
    disabled = _rag_require_enabled(session_id, "dynamic_list_available_fields", {"object": object, "rag_snapshot": rag_snapshot})
    if disabled is not None:
        return disabled
    if not object:
        try:
            win = await _bridge_call_recorded(session_id, "active_window", {})
            object = _metadata_name_from_navigation_url(str(win.get("navigation_url") or ""))
        except Exception:
            object = ""
    effective_snapshot = rag_snapshot or sess.rag_snapshot or None
    fields = _rag_fields_for_object(object, snapshot=effective_snapshot)
    return {"object": object, "rag_object": _metadata_name_to_rag_name(object), "snapshot": effective_snapshot, "count": len(fields), "fields": fields, "rag_diagnostic": _rag_diagnostic(session_id, rag_snapshot)}


@mcp.tool()
async def dynamic_list_apply_settings(
    filters: Annotated[str, "JSON array of filter objects [{field, value, comparison?}] or empty string"] = "",
    orders: Annotated[str, "JSON array of order objects [{field, direction?}] or empty string"] = "",
    clear_first: Annotated[bool, "Reset settings to defaults before applying new ones"] = False,
    name: Annotated[str, "Dynamic list/table technical name"] = "Список",
    title: Annotated[str, "Dynamic list/table visible title"] = "",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Apply dynamic-list filters/orders and optionally clear settings first.

    `orders` are applied with the stable test-client API `УстановитьПорядок`.
    `filters` are applied through the standard "Настроить список" dialog using
    best-effort field/value filling; if a configuration renders the settings
    form differently, the returned step contains the exact diagnostic/error.
    """
    steps: list[dict[str, Any]] = []
    if clear_first:
        cleared = await _bridge_call_recorded(session_id, "dynamic_list_clear_settings", {"button": ""})
        steps.append({"action": "clear", "result": cleared})
    filter_list = json.loads(filters) if filters else []
    order_list = json.loads(orders) if orders else []
    if filter_list:
        opened = await _bridge_call_recorded(session_id, "dynamic_list_open_settings", {"button": ""})
        steps.append({"action": "open_settings", "result": opened})
        for spec in filter_list:
            applied = await _bridge_call_recorded(session_id, "dynamic_list_apply_filter", {
                "field": spec.get("field", ""),
                "value": spec.get("value", ""),
                "comparison": spec.get("comparison", "Equal"),
                "name": name,
                "title": title,
                "max_depth": max_depth,
            })
            steps.append({"action": "filter", "spec": spec, "result": applied})
        closed = await _bridge_call_recorded(session_id, "close_form", {})
        steps.append({"action": "close_settings", "result": closed})
    for spec in order_list:
        field = spec.get("field", "")
        ordered = await _bridge_call_recorded(session_id, "dynamic_list_set_order", {
            "name": name, "title": title, "column_title": field, "max_depth": max_depth,
        })
        steps.append({"action": "order", "spec": spec, "result": ordered})
    return {"applied": True, "filters": len(filter_list), "orders": len(order_list), "steps": steps}


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


def _x11_screen_size(display: str | None = None) -> tuple[int, int] | None:
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    try:
        out = subprocess.run(
            ["xdpyinfo"],
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if "dimensions:" not in line:
            continue
        try:
            dimensions = line.split("dimensions:", 1)[1].strip().split()[0]
            width, height = dimensions.split("x", 1)
            return int(width), int(height)
        except Exception:
            return None
    return None


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

    screen_size = _x11_screen_size(display)
    screen_width = screen_size[0] if screen_size else None
    screen_height = screen_size[1] if screen_size else None
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
        priority = 0
        if title and title not in {"1cv8c", "1cv8", "Конфигурация"}:
            priority += 100
        if any(word in title for word in (
            "1С:Предприятие",
            "Бухгалтерия",
            "Предприятие",
            "Управление",
            "Зарплата",
            "ERP",
            "Комплексная",
            "Розница",
        )):
            priority += 50
        if any(word in title for word in ("создание", "Инструменты", "Шаблон", "Сессия", "DEV-AREA")):
            priority += 25
        priority += min(width * height // 10000, 50)

        # ``xwininfo -root -tree`` reports child coordinates relative to their
        # parent.  For many 1C/X11 windows this means the client area appears as
        # e.g. ``1280x1024+0+0`` even when the decorated top-level window is
        # slightly larger or shifted.  mss needs root-relative, in-bounds
        # monitor coordinates, so normalize the candidate to the virtual screen
        # instead of discarding negative/off-screen decorations.
        capture_left = max(0, left)
        capture_top = max(0, top)
        capture_width = width - max(0, -left)
        capture_height = height - max(0, -top)
        if screen_width is not None:
            capture_width = min(capture_width, max(0, screen_width - capture_left))
        if screen_height is not None:
            capture_height = min(capture_height, max(0, screen_height - capture_top))
        if capture_width < 300 or capture_height < 150:
            continue
        candidates.append((priority, {
            "left": capture_left,
            "top": capture_top,
            "width": capture_width,
            "height": capture_height,
            "title": title,
            "xwininfo_left": left,
            "xwininfo_top": top,
            "xwininfo_width": width,
            "xwininfo_height": height,
        }))
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
    event = _session(session_id).recorder.capture(
        lambda path, window, display: _screenshot_for_recorder(path, window, display, session_id),
        action,
        {"note": note},
        {"note": note},
    )
    return event or {"active": False}


@mcp.tool()
def recording_stop(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Stop recording and build PDF evidence from captured slides."""
    return _session(session_id).recorder.stop()


async def _dev_eval(
    code: Annotated[str, "BSL expression or 'Возврат <expression>' to evaluate in the Answer42 test manager form"],
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Development-only: execute a BSL expression in MCPTestManager and return JSON-serialized result."""
    return await _bridge_call_recorded(session_id, "dev_eval", {"code": code})


@mcp.tool()
def screenshot(
    path: Annotated[str, "Output image path"] = "build/screenshots/screenshot.png",
    window: Annotated[bool, "Capture the best visible 1C window instead of the whole virtual display"] = True,
    display: Annotated[str, "X11 display to inspect/capture. Defaults to the session's display."] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Capture a screenshot from the MCP host. Defaults to the active visible 1C window.

    Screenshots are for user evidence and UI diagnostics only. Do not use them
    to analyze report/table/tabular-document/list data; use structured tools
    such as tabular_document_text, tabular_documents, table_rows,
    dynamic_list_output, or report export/save tools instead.
    """
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sess = _session(session_id)
    resolved_display = display or sess.client_display or sess.display

    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore

        if os_support.IS_WINDOWS:
            geometry = _active_windows_1c_window_geometry() if window else None
            with mss.mss() as sct:
                monitor = geometry or sct.monitors[0]
                image = sct.grab(monitor)
                mss.tools.to_png(image.rgb, image.size, output=str(output_path))
        else:
            if not resolved_display:
                raise RuntimeError("Cannot capture screenshot: no display set for session and no display argument provided")

            previous_display = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = resolved_display
            try:
                geometry = _active_x11_window_geometry(resolved_display) if window else None
                if window and geometry is None:
                    raise RuntimeError(
                        "Cannot capture 1C window screenshot: window geometry was not detected. "
                        "Use window=false to capture the whole virtual display, or maximize/resize the "
                        "test client window and retry."
                    )
                with mss.mss() as sct:
                    monitor = geometry or sct.monitors[0]
                    image = sct.grab(monitor)
                    mss.tools.to_png(image.rgb, image.size, output=str(output_path))
            finally:
                if previous_display is None:
                    os.environ.pop("DISPLAY", None)
                else:
                    os.environ["DISPLAY"] = previous_display
    except ImportError as exc:
        raise RuntimeError(
            "Screenshot support requires the optional dependency 'mss'. "
            "Install it with: pip install 'answer42[screenshot]' "
            "or, for pipx installs: pipx inject answer42 mss."
        ) from exc

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
            if sess.client_runtime is not None:
                if sess.shared_client_key:
                    _release_shared_file_server(sess.shared_client_key, sid)
                else:
                    runtime.kill_process(sess.client_runtime.server_process, "ibsrv-client")
            if sess.file_client_runtime is not None:
                if sess.shared_file_key:
                    _release_shared_file_server(sess.shared_file_key, sid)
                else:
                    runtime.kill_process(sess.file_client_runtime.server_process, "ibsrv-file-client")
            try:
                await sess.bridge.stop()
            except Exception:
                pass
            runtime.stop_xvfb(sess.client_xvfb_process)
            runtime.stop_xvfb(sess.xvfb_process)
        _SESSIONS.clear()


def _register_dev_tools(enabled: bool) -> None:
    try:
        mcp.remove_tool("dev_eval")
    except Exception:
        pass
    if enabled:
        mcp.add_tool(
            _dev_eval,
            name="dev_eval",
            description="Development-only: execute a BSL expression in MCPTestManager. Enable with --dev-tools or ONEC_MCP_DEV_TOOLS=1.",
        )


def main(argv: list[str] | None = None) -> None:
    global _RAG_ENABLED, _CREDENTIAL_STORE_ENABLED, _DEV_TOOLS_ENABLED

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"install-skills", "skills-install"}:
        raise SystemExit(skill_installer.main(argv[1:]))
    if argv and argv[0] in {"release", "release-pypi"}:
        raise SystemExit(release_helper.main(argv[1:]))

    parser = argparse.ArgumentParser(description="MCP server for 1C:Enterprise test manager")
    parser.add_argument("--version", action="version", version=f"answer42 {__version__}")
    parser.add_argument("--log-level", default=os.getenv("ONEC_MCP_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--disable-rag",
        action="store_true",
        default=os.getenv("ONEC_MCP_DISABLE_RAG", "").strip().lower() in {"1", "true", "yes", "on"},
        help="disable built-in Answer42 RAG tools and RAG enrichment",
    )
    parser.add_argument(
        "--disable-credential-store",
        action="store_true",
        default=os.getenv("ONEC_MCP_DISABLE_CREDENTIAL_STORE", "").strip().lower() in {"1", "true", "yes", "on"},
        help="disable loading, saving and automatic lookup of credentials from the local credential store",
    )
    parser.add_argument(
        "--rag-source-dir",
        action="append",
        default=None,
        help="directory to scan/watch for 1C configuration or extension sources; can be passed multiple times",
    )
    parser.add_argument(
        "--rag-source-prefix",
        default=os.getenv("ONEC_MCP_RAG_SOURCE_PREFIX", ""),
        help="prefix for source names discovered from --rag-source-dir / ONEC_MCP_RAG_SOURCE_DIRS",
    )
    parser.add_argument(
        "--rag-source-max-depth",
        type=int,
        default=int(os.getenv("ONEC_MCP_RAG_SOURCE_MAX_DEPTH", "4")),
        help="maximum recursive depth for RAG source directory discovery",
    )
    parser.add_argument(
        "--rag-source-no-recursive",
        action="store_true",
        default=os.getenv("ONEC_MCP_RAG_SOURCE_RECURSIVE", "1").strip().lower() in {"0", "false", "no", "off"},
        help="do not recurse while discovering RAG source directories",
    )
    parser.add_argument(
        "--dev-tools",
        action="store_true",
        default=os.getenv("ONEC_MCP_DEV_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"},
        help="register dangerous development-only MCP tools, including dev_eval for arbitrary BSL expression evaluation",
    )
    parser.add_argument(
        "--dev",
        default=os.getenv("ONEC_MCP_DEV", ""),
        choices=("", "openclaw"),
        help="enable development integrations; currently only 'openclaw' starts the MCP autoreload watchdog",
    )
    parser.add_argument(
        "--dev-restart-timeout",
        type=float,
        default=float(os.getenv("ONEC_MCP_DEV_RESTART_TIMEOUT", "30")),
        help="seconds of no file changes before the OpenClaw MCP autoreload watchdog triggers reload",
    )
    args = parser.parse_args(argv)

    _RAG_ENABLED = not args.disable_rag
    _CREDENTIAL_STORE_ENABLED = not args.disable_credential_store
    _DEV_TOOLS_ENABLED = bool(args.dev_tools)
    _register_dev_tools(_DEV_TOOLS_ENABLED)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if _RAG_ENABLED:
        _register_startup_rag_source_dirs(args.rag_source_dir or [], args.rag_source_prefix, not args.rag_source_no_recursive, args.rag_source_max_depth)
    else:
        logging.info("Built-in Answer42 RAG is disabled")
    _start_autoreload_watcher(args.dev, args.dev_restart_timeout)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
