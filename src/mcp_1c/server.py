from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.resources
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from mcp.server import MCPServer
from mcp.server.lowlevel import NotificationOptions
from mcp.server.mcpserver.resources import FileResource
from mcp.server.subscriptions import ToolsListChanged

from . import __version__, os_support, release_helper, runtime, skill_installer
from . import platform as _platform
from .auth import build_auth_settings, build_token_verifier, load_static_token_config
from .auth import current_account_id as _current_account_id
from .bridge import BridgeError, OneCBridgeServer
from .credentials import get_store as _credentials_store
from .protocol import MANAGER_PROTOCOL_VERSION
from .rag import RagService
from .rag.dump import dump_all_extensions_to_xml, dump_cf_to_xml, dump_infobase_to_xml
from .recorder import RecordingSession
from .window_control import active_1c_window_geometry as _active_external_1c_window_geometry
from .window_control import maximize_test_client_window as _maximize_external_window

mcp = MCPServer("Answer42")
_RAG = RagService()
_RAG_ENABLED = os.getenv("ONEC_MCP_DISABLE_RAG", "").strip().lower() not in {"1", "true", "yes", "on"}
_CREDENTIAL_STORE_ENABLED = os.getenv("ONEC_MCP_DISABLE_CREDENTIAL_STORE", "").strip().lower() not in {"1", "true", "yes", "on"}
_DEV_TOOLS_ENABLED = os.getenv("ONEC_MCP_DEV_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_SESSION = os.getenv("ONEC_MCP_DEFAULT_SESSION", f"default-{os.getpid()}")
# Ordered, empirically preferred 1C file-dialog filter indexes for
# tabular-document saves.  The Python side validates the produced file
# signature after every single-index save attempt; these values only choose the
# first likely attempts, then the remaining indexes are tried without duplicates.
TABULAR_DOCUMENT_SAVE_PREFERRED_FILTER_INDEXES: dict[str, list[int]] = {
    "xlsx": [1],  # Verified on live BP Universal report.
    "pdf": [2],  # Verified on live dynamic-list output.
    "xls": [3],
    "html": [4],
    "htm": [4],
    "txt": [5],
    "mxl": [1],
}
SESSION_SCOPE_ID = os.getenv("ONEC_MCP_SESSION_SCOPE_ID") or os.getenv("ONEC_MCP_SESSION_SCOPE") or ""
STATE_FILE = os_support.runtime_path("ONEC_MCP_SESSION_STATE", "sessions.json")
SCOPE_DIR: Path | None = None
SCOPE_LOCK_FILE: Path | None = None
_SCOPE_LOCK_HANDLE: Any | None = None
GLOBAL_REGISTRY_FILE = os_support.runtime_path("ONEC_MCP_GLOBAL_SESSION_REGISTRY", "global-sessions.json")
GLOBAL_REGISTRY_LOCK = os_support.runtime_path("ONEC_MCP_GLOBAL_SESSION_REGISTRY_LOCK", "global-sessions.lock")
GLOBAL_ORPHAN_TTL_SECONDS = int(os.getenv("ONEC_MCP_ORPHAN_TTL_SECONDS", "1800"))
AUTORELOAD_PID_FILE = os_support.runtime_path("ONEC_MCP_AUTORELOAD_PID_FILE", "answer42-autoreload.pid")
AUTORELOAD_LOG_FILE = os_support.runtime_path("ONEC_MCP_AUTORELOAD_LOG_FILE", "answer42-autoreload.log")
RAG_BINDINGS_FILE = os_support.runtime_path("ONEC_MCP_RAG_BINDINGS", "rag-bindings.json")
UI_TREE_SNAPSHOTS_DIR = os_support.runtime_path("ONEC_MCP_UI_TREE_SNAPSHOTS_DIR", "ui-tree-snapshots")
UI_TREE_RESOURCE_AUTO_THRESHOLD_TOKENS = int(os.getenv("ONEC_MCP_UI_TREE_RESOURCE_AUTO_THRESHOLD_TOKENS", "50000"))
UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES = int(os.getenv("ONEC_MCP_UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES", "0"))  # deprecated fallback; 0 disables
NOTIFY_TOOLS_CHANGED_ON_STARTUP = os.getenv("ONEC_MCP_NOTIFY_TOOLS_CHANGED_ON_STARTUP", "1").strip().lower() not in {"0", "false", "no", "off"}
TOOLS_CHANGED_STARTUP_DELAYS = tuple(
    float(part)
    for part in re.split(r"[,;\s]+", os.getenv("ONEC_MCP_TOOLS_CHANGED_STARTUP_DELAYS", "1,3,10,30").strip())
    if part.strip()
)
_TOOLS_LIST_CHANGED_CAPABILITY_ENABLED = False


def _enable_tools_list_changed_capability() -> None:
    """Advertise tools/list_changed for handshake-era clients.

    Modern 2026-07-28 clients derive list-changed support from
    `subscriptions/listen`.  Older clients still look at the legacy
    `tools.listChanged` capability bit in `initialize`, and the MCP SDK only
    sets that bit when `NotificationOptions(tools_changed=True)` is supplied
    to `create_initialization_options()`.  Answer42 normally delegates that
    call to the SDK internals, so wrap the low-level method once and preserve
    explicit caller-provided options.
    """
    global _TOOLS_LIST_CHANGED_CAPABILITY_ENABLED
    if _TOOLS_LIST_CHANGED_CAPABILITY_ENABLED:
        return
    lowlevel = mcp._lowlevel_server
    original = lowlevel.create_initialization_options

    def create_initialization_options_with_tools_changed(
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> Any:
        if notification_options is None:
            notification_options = NotificationOptions(tools_changed=True)
        return original(notification_options, experimental_capabilities, extensions)

    lowlevel.create_initialization_options = create_initialization_options_with_tools_changed  # type: ignore[method-assign]
    _TOOLS_LIST_CHANGED_CAPABILITY_ENABLED = True

_PORT_ALLOC_LOCK = os_support.runtime_path("ONEC_MCP_PORT_ALLOC_LOCK", "port-allocation.lock")
_SHARED_FILE_SERVERS_FILE = os_support.runtime_path("ONEC_MCP_SHARED_FILE_SERVERS", "shared-file-servers.json")
_SHARED_FILE_SERVERS_LOCK = os_support.runtime_path("ONEC_MCP_SHARED_FILE_SERVERS_LOCK", "shared-file-servers.lock")
_METADATA_OBJECTS_CACHE: dict[str, dict[str, Any]] = {}

TOOL_PROFILE_GROUPS: dict[str, set[str]] = {
    "sessions": {
        "sessions_list", "session_status", "start_session", "stop_session", "export_eventlog",
        "credentials_check", "credentials_save", "credentials_remove", "credentials_list", "credentials_titles",
    },
    "windows": {"active_window", "windows_list", "activate_window", "goto_start_page", "goto_previous_window", "goto_next_window"},
    "forms": {
        "wait_object", "wait_form", "wait_window", "user_messages", "close_user_messages_panel", "current_error_info",
        "client_busy_status", "wait_client_idle",
        "set_file_dialog_result", "clear_file_dialog_result", "field_info", "field_value_text", "field_tooltip",
        "field_dropdown_items", "form_next_element", "form_previous_element", "form_state", "form_default_button",
        "wait_form_closed", "object_command_panel", "object_context_menu", "window_command_interface",
        "command_interface_sections", "command_interface_commands", "command_interface_search", "group_expand", "group_collapse",
        "group_current_page", "group_activate_page", "group_state", "decoration_links", "decoration_click_link", "decoration_click",
        "ui_tree", "ui_tree_resource_search", "ui_tree_resource_get_node", "ui_tree_resource_children", "ui_tree_diff",
        "ui_tree_resource_action",
        "command_bar", "click_button", "find_object", "search_objects", "get_object", "focus_object",
        "open_navigation_link", "current_element", "field_dropdown_state", "field_dropdown_close", "toggle_field",
        "set_field_value", "activate_field", "choose_field_from_list", "choose_field_first_row", "choose_current_row",
        "set_form_fields", "save_form", "close_form", "form_open_settings",
    },
    "tables": {
        "table_current_row", "table_selected_rows", "table_cell_text", "table_rows", "table_find_row", "table_goto_row",
        "table_move_row", "table_add_row", "table_set_field", "table_set_row_values", "table_choose_field_from_list",
        "table_edit_info", "table_select_all", "table_clear_selection", "table_delete_row", "table_copy_row",
        "table_toggle_delete_mark", "table_begin_edit", "table_end_edit", "table_editing_state",
    },
    "tabular": {"tabular_documents", "tabular_document_text", "tabular_document_save", "tabular_document_state_display"},
    "dynamic": {
        "dynamic_list_metadata_available_fields", "dynamic_list_settings_available_fields", "dynamic_list_set_settings",
        "dynamic_list_current_settings", "dynamic_list_sort_by_column", "dynamic_list_output", "dynamic_list_open_settings",
        "dynamic_list_find", "dynamic_list_clear_settings",
    },
    "rag": {
        "rag_source_add", "rag_sources_add_from_directory", "rag_source_roots_list", "rag_available_configurations",
        "rag_session_autoconfigure", "rag_sources_list", "rag_snapshot_create", "rag_snapshots_list", "rag_index_build",
        "rag_lookup_object", "rag_query", "metadata_objects_from_catalog", "metadata_objects_catalog_cache_clear",
        "configuration_name_version_from_subsystems", "installed_extensions_from_catalog",
    },
    "recording": {"recording_start", "recording_capture", "recording_stop", "screenshot"},
}
TOOL_PROFILE_CORE: set[str] = TOOL_PROFILE_GROUPS["sessions"] | {
    "active_window", "windows_list", "activate_window", "goto_start_page", "goto_previous_window", "goto_next_window",
    "wait_object", "wait_form", "wait_window", "user_messages", "close_user_messages_panel", "current_error_info",
    "client_busy_status", "wait_client_idle",
    "field_info", "field_value_text", "field_dropdown_items", "field_dropdown_state", "field_dropdown_close",
    "toggle_field", "set_field_value", "activate_field", "choose_field_from_list", "choose_field_first_row", "choose_current_row",
    "form_state", "form_default_button", "wait_form_closed", "click_button", "find_object", "search_objects", "get_object",
    "focus_object", "open_navigation_link", "current_element", "table_current_row", "table_selected_rows", "table_cell_text",
    "table_rows", "table_find_row", "table_goto_row", "table_move_row", "table_add_row", "table_set_field", "table_set_row_values",
    "table_choose_field_from_list", "table_edit_info", "table_begin_edit", "table_end_edit", "table_editing_state",
    "tabular_documents", "tabular_document_text", "tabular_document_save", "screenshot",
    "ui_tree",
}
TOOL_PROFILE_ALIASES: dict[str, set[str]] = {
    "core": TOOL_PROFILE_CORE,
    "ui": TOOL_PROFILE_GROUPS["sessions"] | TOOL_PROFILE_GROUPS["windows"] | TOOL_PROFILE_GROUPS["forms"] | TOOL_PROFILE_GROUPS["tables"] | TOOL_PROFILE_GROUPS["tabular"] | TOOL_PROFILE_GROUPS["recording"],
    "all": set(),
    "full": set(),
}


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
        self.manager_pid: int = 0
        self.ibsrv_pid: int = 0
        self.test_client_pid: int = 0
        self.xvfb_pid: int = 0
        self.client_xvfb_pid: int = 0
        self.target_connection_mode: str = ""
        self.target_connection_value: str = ""
        self.bridge = OneCBridgeServer(
            host="127.0.0.1",
            port=ws_port,
            request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "180")),
        )
        self.recorder = RecordingSession()
        self.rag_snapshot: str = ""
        self.rag_enabled: bool = True
        self.idle_timeout_seconds: int = 60 * 60
        self.last_activity_monotonic: float = time.monotonic()
        self.blocked_action: dict[str, Any] | None = None

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
            "target_connection_mode": self.target_connection_mode,
            "target_connection_value": self.target_connection_value,
            "blocked_action": self.blocked_action,
            "manager_pid": self.runtime.manager_process.pid if self.runtime and self.runtime.manager_process and self.runtime.manager_process.poll() is None else self.manager_pid,
            "ibsrv_pid": self.runtime.server_process.pid if self.runtime and self.runtime.server_process and self.runtime.server_process.poll() is None else self.ibsrv_pid,
            "test_client_pid": self.test_client_process.pid if self.test_client_process and self.test_client_process.poll() is None else self.test_client_pid,
            "xvfb_pid": self.xvfb_process.pid if self.xvfb_process and self.xvfb_process.poll() is None else self.xvfb_pid,
            "client_xvfb_pid": self.client_xvfb_process.pid if self.client_xvfb_process and self.client_xvfb_process.poll() is None else self.client_xvfb_pid,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionState:
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
        blocked_action = data.get("blocked_action")
        sess.blocked_action = blocked_action if isinstance(blocked_action, dict) else None
        sess.target_connection_mode = str(data.get("target_connection_mode") or "")
        sess.target_connection_value = str(data.get("target_connection_value") or "")
        sess.manager_pid = int(data.get("manager_pid") or 0)
        sess.ibsrv_pid = int(data.get("ibsrv_pid") or 0)
        sess.test_client_pid = int(data.get("test_client_pid") or 0)
        sess.xvfb_pid = int(data.get("xvfb_pid") or 0)
        sess.client_xvfb_pid = int(data.get("client_xvfb_pid") or 0)
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


def _sanitize_scope_id(scope_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(scope_id or "").strip())
    value = value.strip(".-")
    if not value:
        raise ValueError("Answer42 session scope id must contain at least one safe character")
    return value[:128]


def _state_file_for_scope(scope_id: str) -> tuple[Path, Path, Path]:
    safe = _sanitize_scope_id(scope_id)
    root = Path(os.getenv("ONEC_MCP_SESSION_SCOPES_DIR", str(os_support.default_data_dir() / "session-scopes")))
    scope_dir = root / safe
    return scope_dir, scope_dir / "sessions.json", scope_dir / "answer42.lock"


def _configure_session_scope(scope_id: str) -> None:
    """Switch Answer42 from legacy global state to scoped lock/reconnect mode."""
    global SESSION_SCOPE_ID, STATE_FILE, SCOPE_DIR, SCOPE_LOCK_FILE
    SESSION_SCOPE_ID = str(scope_id or "").strip()
    if not SESSION_SCOPE_ID:
        SCOPE_DIR = None
        SCOPE_LOCK_FILE = None
        STATE_FILE = os_support.runtime_path("ONEC_MCP_SESSION_STATE", "sessions.json")
        return
    SCOPE_DIR, STATE_FILE, SCOPE_LOCK_FILE = _state_file_for_scope(SESSION_SCOPE_ID)


def _acquire_scope_lock_if_configured() -> bool:
    """Acquire the configured scope lock. Return True when a stale lock existed."""
    global _SCOPE_LOCK_HANDLE
    if not SCOPE_LOCK_FILE:
        return False
    SCOPE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    stale = SCOPE_LOCK_FILE.exists()
    handle = SCOPE_LOCK_FILE.open("a+", encoding="utf-8")
    if os_support.IS_WINDOWS:
        import msvcrt  # type: ignore

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise RuntimeError(f"Answer42 session scope {SESSION_SCOPE_ID!r} is locked by another live process") from exc
    else:
        import fcntl  # type: ignore

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"Answer42 session scope {SESSION_SCOPE_ID!r} is locked by another live process") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "scope_id": SESSION_SCOPE_ID, "started_at": datetime.utcnow().isoformat() + "Z"}))
    handle.flush()
    _SCOPE_LOCK_HANDLE = handle
    return stale


def _release_scope_lock() -> None:
    global _SCOPE_LOCK_HANDLE
    if _SCOPE_LOCK_HANDLE is None:
        return
    handle = _SCOPE_LOCK_HANDLE
    _SCOPE_LOCK_HANDLE = None
    try:
        if os_support.IS_WINDOWS:
            import msvcrt  # type: ignore

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _load_global_registry_unlocked() -> dict[str, Any]:
    if not GLOBAL_REGISTRY_FILE.exists():
        return {"sessions": {}}
    try:
        raw = json.loads(GLOBAL_REGISTRY_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("sessions", {})
            if isinstance(raw["sessions"], dict):
                return raw
    except Exception as exc:
        logging.warning("Could not read Answer42 global session registry %s: %s", GLOBAL_REGISTRY_FILE, exc)
    return {"sessions": {}}


def _write_global_registry_unlocked(data: dict[str, Any]) -> None:
    GLOBAL_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GLOBAL_REGISTRY_FILE.with_suffix(GLOBAL_REGISTRY_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(GLOBAL_REGISTRY_FILE)


def _session_registry_key(sess: SessionState) -> str:
    scope = _sanitize_scope_id(SESSION_SCOPE_ID) if SESSION_SCOPE_ID else "legacy"
    return f"{scope}:{sess.session_id}"


def _session_global_registry_record(sess: SessionState, *, status: str = "active") -> dict[str, Any]:
    pids = {
        "manager_pid": sess.runtime.manager_process.pid if sess.runtime and sess.runtime.manager_process and sess.runtime.manager_process.poll() is None else sess.manager_pid,
        "ibsrv_pid": sess.runtime.server_process.pid if sess.runtime and sess.runtime.server_process and sess.runtime.server_process.poll() is None else sess.ibsrv_pid,
        "test_client_pid": sess.test_client_process.pid if sess.test_client_process and sess.test_client_process.poll() is None else sess.test_client_pid,
        "xvfb_pid": sess.xvfb_process.pid if sess.xvfb_process and sess.xvfb_process.poll() is None else sess.xvfb_pid,
        "client_xvfb_pid": sess.client_xvfb_process.pid if sess.client_xvfb_process and sess.client_xvfb_process.poll() is None else sess.client_xvfb_pid,
    }
    return {
        "scope_id": SESSION_SCOPE_ID,
        "session_id": sess.session_id,
        "owner_pid": os.getpid(),
        "status": status,
        "updated_at": time.time(),
        "base_url": sess.base_url,
        "state_file": str(STATE_FILE),
        "data_dir": str(sess.data_dir),
        **pids,
    }


def _update_global_session_registry(sess: SessionState, *, status: str = "active") -> None:
    with os_support.file_lock(GLOBAL_REGISTRY_LOCK):
        data = _load_global_registry_unlocked()
        data["sessions"][_session_registry_key(sess)] = _session_global_registry_record(sess, status=status)
        _write_global_registry_unlocked(data)


def _remove_global_session_registry(sess: SessionState) -> None:
    with os_support.file_lock(GLOBAL_REGISTRY_LOCK):
        data = _load_global_registry_unlocked()
        data.get("sessions", {}).pop(_session_registry_key(sess), None)
        _write_global_registry_unlocked(data)


def _mark_global_sessions_orphaned_for_owner(owner_pid: int | None = None) -> None:
    owner = int(owner_pid or os.getpid())
    now = time.time()
    with os_support.file_lock(GLOBAL_REGISTRY_LOCK):
        data = _load_global_registry_unlocked()
        changed = False
        for rec in data.get("sessions", {}).values():
            if int(rec.get("owner_pid") or 0) != owner:
                continue
            rec["status"] = "orphaned"
            rec["orphaned_at"] = now
            rec["updated_at"] = now
            changed = True
        if changed:
            _write_global_registry_unlocked(data)


def _registry_process_pids(rec: dict[str, Any]) -> list[int]:
    result: list[int] = []
    for name in ("manager_pid", "ibsrv_pid", "test_client_pid", "xvfb_pid", "client_xvfb_pid"):
        try:
            pid = int(rec.get(name) or 0)
        except Exception:
            pid = 0
        if pid > 0 and pid not in result:
            result.append(pid)
    return result


def _reap_global_orphan_sessions(ttl_seconds: int = GLOBAL_ORPHAN_TTL_SECONDS) -> dict[str, Any]:
    """Kill 1C processes recorded for dead Answer42 owners after the TTL."""
    now = time.time()
    reaped: list[str] = []
    orphaned: list[str] = []
    with os_support.file_lock(GLOBAL_REGISTRY_LOCK):
        data = _load_global_registry_unlocked()
        sessions = data.get("sessions", {})
        for key, rec in list(sessions.items()):
            owner_pid = int(rec.get("owner_pid") or 0)
            owner_alive = _pid_is_running(owner_pid)
            if owner_alive and rec.get("status") == "active":
                continue
            if not owner_alive and rec.get("status") == "active":
                rec["status"] = "orphaned"
                rec["orphaned_at"] = now
                rec["updated_at"] = now
                orphaned.append(key)
                continue
            if rec.get("status") != "orphaned":
                continue
            orphaned_at = float(rec.get("orphaned_at") or rec.get("updated_at") or now)
            if now - orphaned_at < ttl_seconds:
                continue
            for pid in _registry_process_pids(rec):
                _kill_pid_group(pid, f"orphaned Answer42 session {key}")
            sessions.pop(key, None)
            reaped.append(key)
        _write_global_registry_unlocked(data)
    return {"orphaned": orphaned, "reaped": reaped, "ttl_seconds": ttl_seconds}


if SESSION_SCOPE_ID:
    _configure_session_scope(SESSION_SCOPE_ID)


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
    for sess in _SESSIONS.values():
        _update_global_session_registry(sess)


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


def _kill_persisted_session_processes(sess: SessionState, reason: str, *, include_test_client: bool) -> None:
    logging.info("Cleaning persisted session %s processes: %s", sess.session_id, reason)
    for pid, label in (
        (sess.manager_pid, "test-manager"),
        (sess.ibsrv_pid, "ibsrv"),
        (sess.xvfb_pid, "xvfb"),
        (sess.client_xvfb_pid, "client-xvfb"),
    ):
        _kill_pid_group(pid, f"{label} for Answer42 session {sess.session_id}")
    if include_test_client:
        _kill_pid_group(sess.test_client_pid, f"test-client for Answer42 session {sess.session_id}")
        _cleanup_stale_session_processes(sess, reason)


async def _reconnect_persisted_session(sess: SessionState) -> bool:
    """Reconnect a scoped session after MCP process restart.

    If the test client is gone, the session is considered finished.  If the
    client is alive and the manager ibsrv is still reachable, restart only the
    test-manager process and attach it to the existing test client port.
    """
    if not _pid_is_running(sess.test_client_pid):
        _kill_persisted_session_processes(sess, "test client is not running during scoped reconnect", include_test_client=False)
        return False
    if sess.ibsrv_pid and not _pid_is_running(sess.ibsrv_pid):
        _kill_persisted_session_processes(sess, "manager ibsrv is not running during scoped reconnect", include_test_client=True)
        return False
    if sess.ibsrv_port and not runtime.is_port_listening("127.0.0.1", sess.ibsrv_port):
        _kill_persisted_session_processes(sess, "manager ibsrv port is not listening during scoped reconnect", include_test_client=True)
        return False

    # Old manager was connected to a dead websocket URL.  Kill it before
    # starting a manager that connects to this fresh MCP process' bridge.
    _kill_pid_group(sess.manager_pid, f"old test-manager for Answer42 session {sess.session_id}")

    await sess.bridge.start()
    log_dir = sess.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    rt = _resolve_runtime(
        platform_dir=sess.platform_dir,
        db_path=sess.db_path,
        ibsrv_address="127.0.0.1",
        ibsrv_port=sess.ibsrv_port,
        ibsrv_name="mcp",
        server_data_dir=sess.server_data_dir,
    )
    rt.log_dir = log_dir
    sess.runtime = rt
    try:
        if sess.ibsrv_port:
            ws_url = runtime.build_thin_client_ws_url("127.0.0.1", sess.ibsrv_port)
            manager_proc = runtime.start_thin_test_manager(
                onec_path=sess.onec_thin,
                ws_url=ws_url,
                websocket_url=sess.bridge.url,
                log_dir=log_dir,
                display=sess.display,
            )
        else:
            manager_proc = runtime.start_thin_test_manager_file_direct(
                onec_path=sess.onec_thin,
                db_path=sess.db_path,
                websocket_url=sess.bridge.url,
                log_dir=log_dir,
                display=sess.display,
            )
        rt.manager_process = manager_proc
        sess.manager_pid = manager_proc.pid
        await _wait_for_bridge_connected(sess, timeout=60.0)
        params = {
            "base_url": sess.base_url,
            "username": sess.username,
            "password": sess.password,
            "port": sess.testclient_port,
            "host": "localhost",
            "client_pid": sess.test_client_pid,
            "connection_mode": "reconnect",
            "connection_value": sess.base_url,
            "target_connection_mode": "reconnect",
            "expected_manager_protocol_version": MANAGER_PROTOCOL_VERSION,
        }
        await sess.bridge.call("attach_test_client", params)
        _SESSIONS[sess.session_id] = sess
        logging.info("Reconnected scoped Answer42 session %s", sess.session_id)
        return True
    except Exception as exc:
        logging.warning("Could not reconnect scoped Answer42 session %s: %s", sess.session_id, exc)
        try:
            await sess.bridge.stop()
        except Exception:
            pass
        _kill_persisted_session_processes(sess, "scoped reconnect failed", include_test_client=True)
        return False


async def _recover_scoped_sessions_if_needed(stale_lock_existed: bool) -> None:
    if not SESSION_SCOPE_ID:
        return
    persisted = _load_persisted_sessions()
    if not persisted:
        return
    if not stale_lock_existed:
        # Normal fresh start in scoped mode: do not attach to old sessions unless
        # we know the previous scoped MCP process died while owning the folder.
        return
    recovered: dict[str, SessionState] = {}
    for sid, sess in persisted.items():
        if await _reconnect_persisted_session(sess):
            recovered[sid] = sess
        else:
            shutil.rmtree(sess.data_dir, ignore_errors=True)
    _SESSIONS.update(recovered)
    _persist_sessions()


async def _global_orphan_reaper_loop(interval_seconds: float = 60.0) -> None:
    while True:
        try:
            _reap_global_orphan_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("Answer42 global orphan reaper failed: %s", exc)
        await asyncio.sleep(interval_seconds)


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
    _remove_global_session_registry(sess)
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


def _record_tool_if_session(
    session_id: str,
    action: str,
    params: dict[str, Any],
    result: Any,
) -> Any:
    """Record a tool call when the live session exists, but allow offline helpers.

    Saved ui_tree resources may outlive the 1C test-client session.  Search/read
    helpers should remain useful for diagnostics even after `stop_session`, so
    they do not require `_record_tool()`'s live-session lookup.
    """
    if session_id in _SESSIONS:
        return _record_tool(session_id, action, params, result)
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
    result = {
        "rag_enabled": _RAG_ENABLED,
        "snapshot": effective_snapshot,
        "session_snapshot": session_snapshot,
        "diagnostics": diagnostics,
    }
    if getattr(_RAG, "summary_backend", None) is not None and _RAG.summary_backend.is_summary_snapshot(effective_snapshot):
        result["summary_backend"] = {
            "enabled": _RAG.summary_backend.enabled,
            "backend": _RAG.summary_backend.backend,
            "project": _RAG.summary_backend.project,
            "ref": _RAG.summary_backend.ref,
            "snapshot": effective_snapshot,
        }
    return result


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


def _walk_ui_snapshot(node: Any):
    """Walk saved ui_tree snapshots, including flat `objects` collections."""
    if not isinstance(node, dict):
        return
    if "type" in node or "name" in node or "title" in node:
        yield node
    for collection_key in ("children", "objects"):
        for child in node.get(collection_key) or []:
            yield from _walk_ui_snapshot(child)


def _tool_result_verbosity() -> str:
    value = os.getenv("ONEC_MCP_RESULT_VERBOSITY", "compact").strip().lower()
    if value in {"full", "verbose"}:
        return "full"
    return "compact"


def _compact_enabled(value: bool | None = None) -> bool:
    if value is not None:
        return bool(value)
    return _tool_result_verbosity() != "full"


def _copy_compact_object(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy_compact_object(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_copy_compact_object(v) for v in value]
    return value




def _ui_tree_type_family(raw_type: Any) -> str:
    normalized = _ui_node_type_key(raw_type)
    if not normalized:
        return "unknown"
    if "кнопка" in normalized or "button" in normalized:
        return "buttons"
    if "таблица" in normalized or "table" in normalized:
        return "tables"
    if "поле" in normalized or "field" in normalized:
        return "fields"
    if "группа" in normalized or "group" in normalized:
        return "groups"
    if "форма" in normalized or "form" in normalized:
        return "forms"
    if "окно" in normalized or "window" in normalized:
        return "windows"
    if "декорац" in normalized or "decoration" in normalized:
        return "decorations"
    return "other"


def _ui_tree_type_statistics(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for node in nodes:
        raw_type = str(node.get("type") or "<unknown>").strip() or "<unknown>"
        by_type[raw_type] = by_type.get(raw_type, 0) + 1
        family = _ui_tree_type_family(raw_type)
        by_family[family] = by_family.get(family, 0) + 1
    return {
        "by_family": dict(sorted(by_family.items())),
        "by_type": dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
    }


def _summarize_ui_tree(tree: Any, include_hidden: bool) -> dict[str, Any]:
    """Return compact diagnostics about omitted/truncated UI tree parts."""
    if isinstance(tree, dict) and tree.get("flat") is True and isinstance(tree.get("objects"), list):
        objects = [node for node in tree["objects"] if isinstance(node, dict)]
        hidden_nodes = [node for node in objects if node.get("visible") is False]
        visited = tree.get("_nodes_visited")
        return {
            "include_hidden": include_hidden,
            "nodes_count": int(visited) if isinstance(visited, int) else len(objects),
            "truncated": False,
            "truncated_nodes_count": 0,
            "hidden_children_count": 0,
            "hidden_nodes_count": len(hidden_nodes),
            "flat_objects_count": len(objects),
            "types_count": _ui_tree_type_statistics(objects),
        }
    nodes = list(_walk_ui_tree(tree))
    truncated_nodes = [node for node in nodes if node.get("truncated")]
    hidden_children_total = 0
    hidden_nodes = []
    for node in nodes:
        hidden_count = node.get("hidden_children_count", 0)
        if isinstance(hidden_count, int):
            hidden_children_total += hidden_count
        if node.get("visible") is False:
            hidden_nodes.append(node)
    return {
        "include_hidden": include_hidden,
        "nodes_count": len(nodes),
        "truncated": bool(truncated_nodes),
        "truncated_nodes_count": len(truncated_nodes),
        "hidden_children_count": hidden_children_total,
        "hidden_nodes_count": len(hidden_nodes),
        "types_count": _ui_tree_type_statistics(nodes),
    }


def _compact_ui_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    keep_keys = (
        "type", "name", "title", "form_name", "navigation_url", "text", "value", "presentation", "data",
        "visible", "available", "read_only", "current", "selected", "data_path", "rag_type", "rag", "hidden_children_count",
        "current_page_name", "current_page", "pages", "path", "index_path", "children_omitted_count",
        "_tree_ms", "_nodes_serialized", "metrics", "flat",
    )
    result = {key: _copy_compact_object(node[key]) for key in keep_keys if key in node and node.get(key) not in (None, "", [], {})}
    if node.get("enabled") is not None and node.get("enabled") != node.get("available"):
        result["enabled"] = node.get("enabled")
    for key in ("main", "home"):
        if node.get(key) is True:
            result[key] = True
    children = node.get("children") or []
    if isinstance(children, list) and children:
        result["children"] = [_compact_ui_node(child) for child in children]
    return result


_UI_TREE_FIELD_PRESETS: dict[str, set[str] | None] = {
    "auto": None,
    "full": None,
    "minimal": {"type", "name", "title"},
    "navigation": {
        "type", "name", "title", "visible", "available", "read_only", "enabled", "main", "home", "current", "selected",
    },
    "diagnostic": {
        "type", "name", "title", "form_name", "navigation_url", "visible", "available", "read_only", "enabled",
        "main", "home", "current", "selected", "data_path", "current_page_name", "current_page", "pages",
        "hidden_children_count", "_tree_ms", "_nodes_serialized", "_nodes_visited", "metrics", "flat", "source",
    },
    "data": {
        "type", "name", "title", "form_name", "navigation_url", "text", "value", "presentation", "data",
        "visible", "available", "read_only", "enabled", "current", "selected", "data_path", "current_page_name",
        "current_page", "pages", "hidden_children_count", "rag_type", "rag", "metrics", "flat",
    },
}


def _ui_tree_field_names(fields: str, profile: str) -> set[str] | None:
    raw = (fields or "auto").strip().lower()
    if not raw or raw == "auto":
        raw = "full" if profile == "full" else "diagnostic" if profile == "diagnostic" else "data" if profile == "data" else "navigation"
    if raw in _UI_TREE_FIELD_PRESETS:
        return _UI_TREE_FIELD_PRESETS[raw]
    if "," in raw:
        parsed = {item.strip() for item in fields.split(",") if item.strip()}
        if parsed:
            parsed.add("children")
            return parsed
    allowed = ", ".join(sorted(_UI_TREE_FIELD_PRESETS)) + ", or a comma-separated field list"
    raise ValueError(f"Unsupported ui_tree fields: {fields!r}. Expected one of: {allowed}.")


def _apply_ui_fields(value: Any, allowed: set[str] | None) -> Any:
    if allowed is None:
        return value
    if isinstance(value, list):
        return [_apply_ui_fields(item, allowed) for item in value]
    if not isinstance(value, dict):
        return value
    always = {
        "children", "objects", "filter", "tree_summary", "profile", "fields", "command_panels", "parent_name",
        "found", "count", "reason", "format", "text", "yaml", "metrics", "flat", "rag_diagnostic",
        "method", "supports", "warnings", "path", "index_path", "children_omitted_count",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in always or key in allowed:
            result[key] = _apply_ui_fields(item, allowed) if key in {"children", "objects", "current_page", "pages"} else item
    return result


def _ui_node_type_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


def _ui_node_matches_filters(node: dict[str, Any], *, name: str = "", title: str = "", type: str = "") -> bool:
    if name and _norm_key(node.get("name")) != _norm_key(name):
        return False
    if title and _norm_key(title) not in _norm_key(node.get("title")):
        return False
    if type:
        node_type = _ui_node_type_key(node.get("type"))
        wanted = _ui_node_type_key(type)
        if wanted not in node_type and node_type not in wanted:
            return False
    return True


def _flat_ui_nodes(tree: Any, *, name: str = "", title: str = "", type: str = "") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in _walk_ui_tree(tree):
        if _ui_node_matches_filters(node, name=name, title=title, type=type):
            flat = dict(node)
            flat.pop("children", None)
            flat.pop("current_page", None)
            flat.pop("pages", None)
            result.append(flat)
    return result


def _find_ui_node_by_name(tree: Any, parent_name: str) -> dict[str, Any] | None:
    if not parent_name:
        return tree if isinstance(tree, dict) else None
    for node in _walk_ui_tree(tree):
        if _norm_key(node.get("name")) == _norm_key(parent_name):
            return node
    return None


def _is_group_node(node: dict[str, Any]) -> bool:
    return "тестируемаягруппа" in _ui_node_type_key(node.get("type"))


def _is_layout_group_node(node: dict[str, Any]) -> bool:
    if not _is_group_node(node):
        return False
    if node.get("current_page_name") or node.get("pages"):
        return False
    title = str(node.get("title") or "").strip()
    name = str(node.get("name") or "").strip().casefold()
    if not title:
        return True
    return name.startswith(("группа", "group")) and title.casefold() in {"группа", name}


def _has_non_group_payload(node: dict[str, Any]) -> bool:
    for key in ("name", "title", "form_name", "navigation_url", "text", "value", "presentation", "data", "current_page_name"):
        if node.get(key) not in (None, "", [], {}):
            return True
    return False


def _is_empty_group_node(node: dict[str, Any]) -> bool:
    return _is_group_node(node) and not (node.get("children") or [])


def _is_page_node(node: dict[str, Any]) -> bool:
    node_type = _ui_node_type_key(node.get("type"))
    name = str(node.get("name") or "").casefold()
    return "страница" in node_type or "page" in node_type or name.startswith(("страница", "page"))


def _is_command_panel_node(node: dict[str, Any]) -> bool:
    haystack = " ".join(str(node.get(key) or "") for key in ("type", "name", "title"))
    return "команднаяпанель" in _ui_node_type_key(haystack)


def _transform_ui_groups(node: Any, group_mode: str) -> Any:
    if not isinstance(node, dict):
        return node
    mode = (group_mode or "include").strip().lower()
    if mode in {"", "include"}:
        return node
    transformed_children: list[Any] = []
    for child in node.get("children") or []:
        transformed = _transform_ui_groups(child, group_mode)
        if isinstance(transformed, list):
            transformed_children.extend(transformed)
        elif transformed is not None:
            transformed_children.append(transformed)
    current = dict(node)
    current["children"] = transformed_children
    if not _is_group_node(current):
        return current
    if mode == "flatten":
        return transformed_children
    if mode == "flatten_layout" and _is_layout_group_node(current):
        return transformed_children
    if mode == "exclude":
        return None
    if mode == "exclude_empty" and _is_empty_group_node(current):
        return None
    if mode == "pages_only" and not (current.get("current_page_name") or current.get("pages") or _is_page_node(current)):
        return transformed_children
    return current


def _dedupe_ui_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for node in nodes:
        key = (
            str(node.get("type") or ""),
            str(node.get("name") or ""),
            str(node.get("title") or ""),
            str(node.get("form_name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(node)
    return result


def _collect_command_panel_nodes(node: Any, inside: bool = False) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    current_inside = inside or _is_command_panel_node(node)
    result: list[dict[str, Any]] = []
    if current_inside:
        copy = dict(node)
        copy.pop("children", None)
        result.append(copy)
    for child in node.get("children") or []:
        result.extend(_collect_command_panel_nodes(child, current_inside))
    return result



def _transform_command_panels(node: Any, mode: str) -> Any:
    if not isinstance(node, dict):
        return node
    mode_key = (mode or "include").strip().lower()
    if mode_key in {"", "include", "full", "with", "all", "auto"}:
        return node
    transformed_children: list[Any] = []
    for child in node.get("children") or []:
        transformed = _transform_command_panels(child, mode_key)
        if isinstance(transformed, list):
            transformed_children.extend(transformed)
        elif transformed is not None:
            transformed_children.append(transformed)
    current = dict(node)
    current["children"] = transformed_children
    if _is_command_panel_node(current):
        if mode_key in {"flatten", "skip"}:
            return transformed_children
        if mode_key in {"exclude", "without", "none", "off", "no"}:
            return None
    return current


_UI_TREE_PROFILES = {"navigation", "diagnostic", "data", "full", "command_panels", "no_command_panels"}
_UI_TREE_COMMAND_PANEL_MODES = {"auto", "include", "full", "with", "all", "exclude", "without", "none", "off", "no", "only"}
_UI_TREE_GROUP_MODES = {"include", "flatten", "flatten_layout", "exclude", "exclude_empty", "pages_only"}
_UI_TREE_FORMATS = {"json", "outline", "yaml", "yml"}


def _validate_ui_tree_choice(kind: str, value: str, allowed: set[str]) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        normalized = "json" if kind == "format" else "auto" if kind == "command_panels" else "include" if kind == "group_mode" else "navigation"
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"Unsupported ui_tree {kind}: {value!r}. Expected one of: {allowed_text}.")
    return "yaml" if kind == "format" and normalized == "yml" else normalized


def _json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _estimate_text_tokens(text: str) -> int:
    """Cheap tokenizer-free estimate for context budgeting.

    We intentionally avoid a tokenizer dependency in Answer42 runtime.  The
    estimate is conservative enough for Russian/English JSON: split words,
    punctuation and CJK characters, then add a character-based floor.
    """
    if not text:
        return 0
    lexical_tokens = len(re.findall(r"[A-Za-zА-Яа-яЁё0-9_]+|[^\sA-Za-zА-Яа-яЁё0-9_]", text))
    char_floor = (len(text) + 3) // 4
    return max(lexical_tokens, char_floor)


def _estimate_json_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return _estimate_text_tokens(text)


def _ui_tree_metrics(tree: Any, *, bridge_ms: float, python_transform_ms: float = 0.0, format_ms: float = 0.0, result: Any = None) -> dict[str, Any]:
    metrics = {
        "bridge_ms": round(bridge_ms, 3),
        "python_transform_ms": round(python_transform_ms, 3),
        "format_ms": round(format_ms, 3),
    }
    if isinstance(tree, dict):
        if isinstance(tree.get("_tree_ms"), (int, float)):
            metrics["onec_tree_ms"] = tree.get("_tree_ms")
        if isinstance(tree.get("_nodes_serialized"), int):
            metrics["nodes_serialized"] = tree.get("_nodes_serialized")
        if isinstance(tree.get("_nodes_visited"), int):
            metrics["nodes_visited"] = tree.get("_nodes_visited")
    payload = result if result is not None else tree
    metrics["json_bytes"] = _json_size_bytes(payload)
    metrics["estimated_tokens"] = _estimate_json_tokens(payload)
    if isinstance(payload, dict) and isinstance(payload.get("objects"), list):
        metrics["objects_count"] = len(payload["objects"])
    return metrics


def _attach_ui_tree_metrics(result: Any, metrics: dict[str, Any]) -> Any:
    if isinstance(result, dict):
        result["metrics"] = metrics
    return result


def _outline_ui_tree(node: Any, indent: int = 0) -> str:
    if not isinstance(node, dict):
        return ""
    label_parts = [str(node.get("type") or "Object")]
    if node.get("name"):
        label_parts.append(str(node["name"]))
    if node.get("title") and node.get("title") != node.get("name"):
        label_parts.append(f"— {node['title']}")
    flags = []
    for key, mark in (("visible", "visible"), ("available", "available"), ("read_only", "read_only")):
        if node.get(key) is True:
            flags.append(mark)
        elif node.get(key) is False and key in node:
            flags.append(f"not_{mark}")
    line = "  " * indent + "- " + " ".join(label_parts)
    if flags:
        line += " [" + ", ".join(flags) + "]"
    lines = [line]
    for child in node.get("children") or []:
        child_text = _outline_ui_tree(child, indent + 1)
        if child_text:
            lines.append(child_text)
    return "\n".join(lines)


def _outline_ui_nodes(nodes: list[dict[str, Any]]) -> str:
    return "\n".join(_outline_ui_tree(node, 0) for node in nodes)


def _yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if "\n" in text:
        return "|\n" + "\n".join("  " + line for line in text.splitlines())
    if text == "" or any(ch in text for ch in ":#{}[],&*?!|-<>=%@`\"'") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _to_simple_yaml(value: Any, indent: int = 0) -> str:
    prefix = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_to_simple_yaml(item, indent + 1))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return prefix + "[]"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_to_simple_yaml(item, indent + 1))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return prefix + _yaml_scalar(value)


def _ui_tree_snapshot_file(snapshot_id: str, session_id: str = "") -> Path:
    safe_id = re.sub(r"[^0-9a-zA-Z_.-]+", "_", str(snapshot_id or "")).strip("._")
    if not safe_id:
        raise ValueError("ui_tree snapshot id is empty")
    if session_id:
        safe_session = re.sub(r"[^0-9a-zA-Z_.-]+", "_", str(session_id or "")).strip("._") or "default"
        return UI_TREE_SNAPSHOTS_DIR / safe_session / f"{safe_id}.json"
    matches = list(UI_TREE_SNAPSHOTS_DIR.glob(f"*/{safe_id}.json"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"ui_tree snapshot {snapshot_id!r} not found")
    raise ValueError(f"ui_tree snapshot id {snapshot_id!r} is ambiguous; pass resource_uri instead")


def _ui_tree_resource_uri(session_id: str, snapshot_id: str) -> str:
    safe_session = re.sub(r"[^0-9a-zA-Z_.-]+", "_", str(session_id or "")).strip("._") or "default"
    return f"answer42://ui-tree/{safe_session}/{snapshot_id}"


def _parse_ui_tree_resource_uri(resource_uri: str) -> tuple[str, str]:
    text = str(resource_uri or "").strip()
    match = re.fullmatch(r"answer42://ui-tree/([^/]+)/([^/?#]+)", text)
    if not match:
        raise ValueError("Expected ui_tree resource_uri like answer42://ui-tree/<session>/<snapshot_id>")
    return match.group(1), match.group(2)


def _node_label(node: dict[str, Any]) -> str:
    parts = []
    for key in ("type", "name", "title"):
        value = str(node.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts) or "Object"


def _annotate_ui_tree_paths(value: Any, index_path: str = "", label_path: str = "") -> Any:
    if isinstance(value, list):
        return [_annotate_ui_tree_paths(item, f"{index_path}/{idx}", label_path) for idx, item in enumerate(value)]
    if not isinstance(value, dict):
        return value
    node = dict(value)
    current_index_path = index_path or "/"
    if "type" in node or "name" in node or "title" in node:
        node.setdefault("index_path", current_index_path)
        current_label = _node_label(node).replace("/", "∕")
        node.setdefault("path", f"{label_path}/{current_label}" if label_path else f"/{current_label}")
        label_path = node["path"]
    for collection_key in ("children", "objects"):
        children = node.get(collection_key)
        if isinstance(children, list):
            node[collection_key] = [
                _annotate_ui_tree_paths(child, f"{current_index_path.rstrip('/')}/{idx}" if current_index_path != "/" else f"/{idx}", label_path)
                for idx, child in enumerate(children)
            ]
    for nested_key in ("current_page", "pages"):
        nested = node.get(nested_key)
        if isinstance(nested, (dict, list)):
            node[nested_key] = _annotate_ui_tree_paths(nested, f"{current_index_path}/{nested_key}", label_path)
    return node


def _ui_tree_resource_preview(payload: Any, max_nodes: int = 40, fmt: str = "outline") -> dict[str, Any]:
    max_nodes = max(1, min(int(max_nodes or 40), 500))
    all_nodes = [node for node in _walk_ui_snapshot(payload) if isinstance(node, dict)]
    nodes: list[dict[str, Any]] = []
    for node in all_nodes[:max_nodes]:
        flat = dict(node)
        flat.pop("children", None)
        flat.pop("current_page", None)
        flat.pop("pages", None)
        nodes.append(_compact_ui_node(flat))
    truncated = len(all_nodes) > len(nodes)
    preview_source: Any = {"objects": nodes}
    if fmt == "yaml":
        text = _to_simple_yaml(preview_source)
        return {"format": "yaml", "yaml": text, "nodes_count": len(nodes), "truncated": truncated}
    text = _outline_ui_nodes(nodes)
    return {"format": "outline", "text": text, "nodes_count": len(nodes), "truncated": truncated}


def _store_ui_tree_snapshot(
    payload: Any,
    *,
    session_id: str,
    request: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    preview_max_nodes: int = 40,
    preview_format: str = "outline",
) -> dict[str, Any]:
    snapshot_id = uuid.uuid4().hex
    resource_uri = _ui_tree_resource_uri(session_id, snapshot_id)
    annotated_payload = _annotate_ui_tree_paths(payload)
    summary = _summarize_ui_tree(annotated_payload, include_hidden=bool(request.get("include_hidden"))) if isinstance(annotated_payload, dict) else {}
    saved_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    document = {
        "kind": "answer42.ui_tree.snapshot",
        "version": 1,
        "snapshot_id": snapshot_id,
        "resource_uri": resource_uri,
        "session_id": session_id,
        "saved_at": saved_at,
        "request": request,
        "summary": summary,
        "metrics": metrics or {},
        "payload": annotated_payload,
    }
    path = _ui_tree_snapshot_file(snapshot_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    try:
        mcp.add_resource(FileResource(
            uri=resource_uri,
            name=f"ui_tree {session_id}/{snapshot_id}",
            title=f"Answer42 ui_tree snapshot {snapshot_id}",
            description="Full ui_tree payload saved outside the tool response; use ui_tree_resource_* tools for targeted reads.",
            mime_type="application/json",
            path=path.resolve(),
        ))
    except Exception as exc:
        logging.debug("Could not register ui_tree resource %s: %s", resource_uri, exc)
    file_bytes = path.stat().st_size if path.exists() else _json_size_bytes(document)
    preview = _ui_tree_resource_preview(annotated_payload, preview_max_nodes, preview_format)
    return {
        "delivery": "resource",
        "resource_uri": resource_uri,
        "snapshot_id": snapshot_id,
        "session_id": session_id,
        "path": str(path),
        "saved_at": saved_at,
        "summary": summary,
        "metrics": metrics or {},
        "json_bytes": file_bytes,
        "preview": preview,
        "usage": {
            "search": "ui_tree_resource_search(resource_uri=..., name/title/type/text=...) returns only matched nodes.",
            "node": "ui_tree_resource_get_node(resource_uri=..., index_path=...) returns one node.",
            "children": "ui_tree_resource_children(resource_uri=..., index_path=..., depth=1) returns a small subtree.",
            "diff": "ui_tree_diff(before_resource_uri=..., after_resource_uri=...) compares two saved snapshots.",
        },
    }


def _ui_tree_should_use_resource(delivery: str, metrics: dict[str, Any] | None = None, payload: Any = None) -> bool:
    """Return whether a ui_tree response should be moved to resource delivery."""
    delivery_key = (delivery or "auto").strip().lower()
    if delivery_key == "resource":
        return True
    if delivery_key != "auto":
        return False
    metric_tokens = 0
    if isinstance(metrics, dict) and isinstance(metrics.get("estimated_tokens"), int):
        metric_tokens = int(metrics["estimated_tokens"])
    payload_tokens = _estimate_json_tokens(payload) if payload is not None else 0
    if max(metric_tokens, payload_tokens) > UI_TREE_RESOURCE_AUTO_THRESHOLD_TOKENS:
        return True
    # Deprecated byte fallback for deployments that explicitly configured it.
    if UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES > 0:
        metric_bytes = 0
        if isinstance(metrics, dict) and isinstance(metrics.get("json_bytes"), int):
            metric_bytes = int(metrics["json_bytes"])
        payload_bytes = _json_size_bytes(payload) if payload is not None else 0
        return max(metric_bytes, payload_bytes) > UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES
    return False


def _ui_tree_maybe_resource_response(
    payload: Any,
    *,
    inline_response: Any,
    delivery: str,
    session_id: str,
    request: dict[str, Any],
    metrics: dict[str, Any] | None,
    preview_max_nodes: int,
    preview_format: str,
) -> Any:
    if not _ui_tree_should_use_resource(delivery, metrics, inline_response):
        return inline_response
    response = _store_ui_tree_snapshot(
        payload,
        session_id=session_id,
        request=request,
        metrics=metrics,
        preview_max_nodes=preview_max_nodes,
        preview_format=preview_format,
    )
    response["delivery_requested"] = delivery
    response["delivery_reason"] = "explicit_resource" if (delivery or "").strip().lower() == "resource" else "auto_threshold_exceeded"
    response["auto_threshold_tokens"] = UI_TREE_RESOURCE_AUTO_THRESHOLD_TOKENS
    if UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES > 0:
        response["auto_threshold_bytes"] = UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES
    return response


def _load_ui_tree_snapshot(resource_uri: str = "", snapshot_id: str = "") -> dict[str, Any]:
    if resource_uri:
        session_part, snap_part = _parse_ui_tree_resource_uri(resource_uri)
        path = _ui_tree_snapshot_file(snap_part, session_part)
    else:
        path = _ui_tree_snapshot_file(snapshot_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"ui_tree snapshot file not found: {path}")


def _ui_tree_payload_from_snapshot(snapshot: dict[str, Any]) -> Any:
    return snapshot.get("payload") if isinstance(snapshot, dict) and "payload" in snapshot else snapshot


def _find_ui_node_by_path(value: Any, path: str = "", index_path: str = "") -> dict[str, Any] | None:
    wanted_path = str(path or "").strip()
    wanted_index = str(index_path or "").strip()
    if not wanted_path and not wanted_index:
        return value if isinstance(value, dict) else None
    for node in _walk_ui_snapshot(value):
        if wanted_index and str(node.get("index_path") or "") == wanted_index:
            return node
        if wanted_path and str(node.get("path") or "") == wanted_path:
            return node
    return None


def _trim_ui_tree_depth(node: Any, depth: int, current: int = 0) -> Any:
    if not isinstance(node, dict):
        return node
    result = dict(node)
    children = result.get("children") or []
    if current >= depth:
        if children:
            result["children_omitted_count"] = len(children)
        result.pop("children", None)
        return result
    result["children"] = [_trim_ui_tree_depth(child, depth, current + 1) for child in children if isinstance(child, dict)]
    return result


def _compact_rows_result(result: Any, *, include_raw: bool = False) -> Any:
    if include_raw or not isinstance(result, dict):
        return result
    rows = result.get("rows")
    if not isinstance(rows, list):
        return result
    compact_rows = []
    for row in rows:
        if not isinstance(row, dict):
            compact_rows.append(row)
            continue
        compact = {key: row[key] for key in ("index", "row", "current", "selected", "expanded", "level") if key in row}
        pairs = row.get("pairs")
        if isinstance(pairs, list):
            compact["values"] = {str(p.get("key", "")): p.get("value", "") for p in pairs if isinstance(p, dict) and p.get("key")}
        compact_rows.append(compact)
    out = dict(result)
    out["rows"] = compact_rows
    out.setdefault("rows_count", len(compact_rows))
    return out


def _compact_tabular_text_result(result: Any, *, include_raw: bool = False) -> Any:
    if include_raw or not isinstance(result, dict):
        return result
    cells = result.get("cells")
    if not isinstance(cells, list):
        return result
    text_lines: list[str] = []
    for row in cells:
        if isinstance(row, list):
            text_lines.append("\t".join(str(cell) for cell in row))
        else:
            text_lines.append(str(row))
    out = {key: result[key] for key in result if key != "cells"}
    out["text"] = "\n".join(text_lines)
    out["rows_count"] = len(cells)
    out["format"] = "tsv"
    return out


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


_ONEC_REF_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _onec_guid_to_ref(guid_value: str) -> str:
    """Convert a textual 1C GUID/UUID to the 32-char e1cib/data ref value.

    1C navigation links store reference UUID bytes in the platform internal
    order: UUID groups 4-5-3-2-1, without hyphens.  Example from a standard BP
    module comment:
    c3314ae6-6899-11e8-8780-b06ebf2faaf6 ->
    8780b06ebf2faaf611e86899c3314ae6.
    """
    normalized = str(uuid.UUID(str(guid_value).strip().strip("{}"))).lower()
    part1, part2, part3, part4, part5 = normalized.split("-")
    return f"{part4}{part5}{part3}{part2}{part1}"


def _onec_ref_to_guid(ref_value: str) -> str:
    ref = str(ref_value or "").strip().lower()
    if not _ONEC_REF_RE.fullmatch(ref):
        raise ValueError("1C ref must contain exactly 32 hexadecimal characters")
    guid_text = f"{ref[24:32]}-{ref[20:24]}-{ref[16:20]}-{ref[0:4]}-{ref[4:16]}"
    return str(uuid.UUID(guid_text))


def _extract_e1cib_data_link(value: str) -> str:
    text = str(value or "").strip()
    marker = "e1cib/data/"
    if marker in text and not text.startswith(marker):
        text = text[text.index(marker):]
    return text


def _parse_e1cib_data_link(value: str) -> tuple[str, str]:
    link = _extract_e1cib_data_link(value)
    if not link.startswith("e1cib/data/"):
        raise ValueError("value is not an e1cib/data navigation link")
    tail = link[len("e1cib/data/"):]
    object_part, _, query = tail.partition("?")
    metadata_name = unquote(object_part).strip()
    ref_values = parse_qs(query, keep_blank_values=True).get("ref", [])
    ref = ref_values[0].strip() if ref_values else ""
    if not ref:
        raise ValueError("e1cib/data navigation link does not contain a ref parameter")
    return metadata_name, ref


def _build_e1cib_data_link(metadata_name: str, ref_value: str) -> str:
    metadata = str(metadata_name or "").strip()
    if not metadata:
        raise ValueError("metadata is required to build an e1cib/data navigation link")
    ref = str(ref_value or "").strip().lower()
    if not _ONEC_REF_RE.fullmatch(ref):
        raise ValueError("1C ref must contain exactly 32 hexadecimal characters")
    return f"e1cib/data/{metadata}?ref={ref}"


def _convert_1c_guid_navigation_link(
    value: str,
    *,
    metadata_name: str = "",
    direction: str = "auto",
) -> dict[str, Any]:
    text = str(value or "").strip()
    mode = str(direction or "auto").strip().lower().replace("-", "_")
    if mode not in {"auto", "guid_to_ref", "ref_to_guid", "guid_to_navigation", "navigation_to_guid"}:
        raise ValueError(
            "direction must be one of: auto, guid_to_ref, ref_to_guid, "
            "guid_to_navigation, navigation_to_guid"
        )

    detected_metadata = ""
    if mode in {"auto", "navigation_to_guid"} and "e1cib/data/" in text:
        detected_metadata, ref = _parse_e1cib_data_link(text)
        guid = _onec_ref_to_guid(ref)
        navigation_link = _build_e1cib_data_link(detected_metadata, ref)
        input_type = "navigation_link"
    elif mode in {"auto", "ref_to_guid"} and _ONEC_REF_RE.fullmatch(text):
        ref = text.lower()
        guid = _onec_ref_to_guid(ref)
        navigation_link = _build_e1cib_data_link(metadata_name, ref) if metadata_name else ""
        input_type = "ref"
    elif mode in {"auto", "guid_to_ref", "guid_to_navigation"}:
        guid = str(uuid.UUID(text.strip("{}")))
        ref = _onec_guid_to_ref(guid)
        navigation_link = _build_e1cib_data_link(metadata_name, ref) if metadata_name else ""
        input_type = "guid"
    else:
        raise ValueError("value does not match the requested conversion direction")

    metadata = detected_metadata or str(metadata_name or "").strip()
    return {
        "input": text,
        "input_type": input_type,
        "metadata_name": metadata,
        "guid": guid,
        "ref": ref,
        "navigation_link": navigation_link,
        "algorithm": "ref = UUID groups 4 + 5 + 3 + 2 + 1, lowercase hex without hyphens; "
        "guid = ref[24:32]-ref[20:24]-ref[16:20]-ref[0:4]-ref[4:16]",
    }


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

    return max(candidates, key=score)


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
    *,
    timeout: float | None = None,
) -> Any:
    sess = await _ensure_restored_session(session_id)
    await _wait_for_bridge_connected(sess, timeout=60.0)
    _touch_session(sess)
    request_timeout = sess.bridge.request_timeout
    method_timeouts = {
        "click_button": ("ONEC_MCP_CLICK_BUTTON_TIMEOUT", request_timeout * 5 / 3),
        "ui_tree": ("ONEC_MCP_UI_TREE_TIMEOUT", request_timeout * 5 / 3),
        "find_object": ("ONEC_MCP_FIND_OBJECT_TIMEOUT", request_timeout),
        "search_objects": ("ONEC_MCP_SEARCH_OBJECTS_TIMEOUT", request_timeout * 5 / 3),
    }
    if method in method_timeouts:
        env_name, default_timeout = method_timeouts[method]
        request_timeout = float(os.getenv(env_name, str(default_timeout)))
    if timeout is not None:
        request_timeout = timeout
    result = await sess.bridge.call(method, params, timeout=request_timeout)
    return _record_tool(session_id, method, params, result)


def _bridge_timeout_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _looks_like_bridge_timeout(exc: BaseException) -> bool:
    text = _bridge_timeout_text(exc).casefold()
    return "timed out" in text or "timeout" in text or "истек" in text or "таймаут" in text


def _blocked_action_elapsed(blocked_action: dict[str, Any] | None) -> float:
    if not blocked_action:
        return 0.0
    try:
        started_time = float(blocked_action.get("started_time") or 0.0)
    except (TypeError, ValueError):
        started_time = 0.0
    if started_time:
        return max(0.0, time.time() - started_time)
    try:
        started = float(blocked_action.get("started_monotonic") or 0.0)
    except (TypeError, ValueError):
        started = 0.0
    return max(0.0, time.monotonic() - started) if started else 0.0


def _public_blocked_action(blocked_action: dict[str, Any] | None) -> dict[str, Any] | None:
    if not blocked_action:
        return None
    public = {key: value for key, value in blocked_action.items() if key != "started_monotonic"}
    public["elapsed_seconds"] = round(_blocked_action_elapsed(blocked_action), 3)
    return public


def _capture_dangerous_action_timeout_screenshot(sess: SessionState, method: str) -> dict[str, Any]:
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "-", sess.session_id).strip(".-") or "session"
    safe_method = re.sub(r"[^A-Za-z0-9_.-]+", "-", method).strip(".-") or "method"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"build/screenshots/watchdog_{safe_session}_{safe_method}_{timestamp}.png"
    try:
        shot = screenshot(path=path, window=True, display="", session_id=sess.session_id)
        if isinstance(shot, dict):
            return {key: value for key, value in shot.items() if key != "base64"}
    except Exception as exc:
        return {"path": path, "error": str(exc)}
    return {"path": path, "error": "screenshot did not return diagnostics"}


def _mark_session_blocked_action(
    sess: SessionState,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
    error: str,
    danger: str,
    screenshot_diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocked_action = {
        "method": method,
        "params": params,
        "danger": danger,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "started_time": time.time(),
        "started_monotonic": time.monotonic(),
        "watchdog_timeout_seconds": timeout,
        "timeout_error": error,
        "reason": "dangerous_action_blocked_interface",
    }
    if screenshot_diagnostic is not None:
        blocked_action["screenshot"] = screenshot_diagnostic
    sess.blocked_action = blocked_action
    with suppress(Exception):
        _persist_sessions()
    return blocked_action


def _clear_session_blocked_action(sess: SessionState) -> None:
    if sess.blocked_action is None:
        return
    sess.blocked_action = None
    with suppress(Exception):
        _persist_sessions()


async def _dangerous_bridge_call_recorded(
    session_id: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
    danger: str,
) -> Any:
    sess = await _ensure_restored_session(session_id)
    try:
        result = await _bridge_call_recorded(session_id, method, params, timeout=timeout)
    except (TimeoutError, BridgeError) as exc:
        if not _looks_like_bridge_timeout(exc):
            raise
        error = _bridge_timeout_text(exc)
        screenshot_diagnostic = _capture_dangerous_action_timeout_screenshot(sess, method)
        blocked_action = _mark_session_blocked_action(
            sess,
            method,
            params,
            timeout=timeout,
            error=error,
            danger=danger,
            screenshot_diagnostic=screenshot_diagnostic,
        )
        result = {
            "ok": False,
            "started_long_action": True,
            "interface_blocked": True,
            "reason": "dangerous_action_watchdog_timeout",
            "message": (
                "Dangerous 1C UI action did not return before the watchdog deadline. "
                "The action may still be running in the thin client and blocking the automation interface. "
                "Use client_busy_status or wait_client_idle before sending further UI commands."
            ),
            "method": method,
            "params": params,
            "watchdog_timeout_seconds": timeout,
            "timeout_error": error,
            "blocked_action": _public_blocked_action(blocked_action),
        }
        return _record_tool(session_id, method, params, result)
    else:
        _clear_session_blocked_action(sess)
        return result


async def _client_busy_status_impl(
    session_id: str,
    *,
    probe_timeout: float = 2.0,
) -> dict[str, Any]:
    sess = await _ensure_restored_session(session_id)
    bridge_status = sess.bridge.status()
    blocked_before_probe = _public_blocked_action(sess.blocked_action)
    result: dict[str, Any] = {
        "session_id": session_id,
        "responsive": False,
        "busy": "unknown",
        "reason": "not_checked",
        "can_continue": False,
        "bridge": bridge_status,
        "blocked_action": blocked_before_probe,
    }
    if not bridge_status.get("connected"):
        result.update({
            "reason": "bridge_not_connected",
            "suggested_action": "start_session_or_restore_bridge",
        })
        return result
    try:
        active = await sess.bridge.call("active_window", {}, timeout=probe_timeout)
    except Exception as exc:
        result.update({
            "responsive": False,
            "busy": True if blocked_before_probe else "unknown",
            "reason": "bridge_probe_timeout" if _looks_like_bridge_timeout(exc) else "bridge_probe_failed",
            "probe_error": _bridge_timeout_text(exc),
            "can_continue": False,
            "suggested_action": "wait_client_idle" if blocked_before_probe else "retry_or_inspect_session_status",
        })
        return result

    if sess.blocked_action is not None:
        _clear_session_blocked_action(sess)
    result.update({
        "responsive": True,
        "busy": False,
        "reason": "bridge_probe_ok",
        "can_continue": True,
        "active_window": active,
    })
    if blocked_before_probe:
        result["recovered_from_blocked_action"] = True
    return result


_TOOL_LOG_MAX_VALUE = int(os.getenv("ONEC_MCP_TOOL_LOG_MAX_VALUE", "4096"))
_TOOL_LOG_REDACT_KEYS = {
    "password", "token", "api_token", "secret", "api_key", "auth",
    "authorization", "bearer", "access_token", "refresh_token",
}


def _redact_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 6 and (
        any(kw in value.lower() for kw in ("password", "token", "secret", "api_key"))
    ):
        return "***"
    if isinstance(value, dict):
        return {k: "***" if k.lower() in _TOOL_LOG_REDACT_KEYS else _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _truncate_value(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > _TOOL_LOG_MAX_VALUE:
        return text[:_TOOL_LOG_MAX_VALUE] + f"... [truncated, total {len(text)}]"
    return text


def _log_tool_call(
    session_id: str,
    method: str,
    params: dict[str, Any],
    result: Any,
    error: str | None,
    duration: float,
) -> None:
    logger = logging.getLogger("mcp_1c.tools")
    if not logger.isEnabledFor(logging.DEBUG) and not logger.handlers:
        return
    status = "error" if error else "ok"
    message = (
        f"tool={method} status={status} duration_ms={duration * 1000:.2f} "
        f"params={_truncate_value(_redact_value(params))} "
        f"result={_truncate_value(_redact_value(result)) if error is None else '<none>'} "
        f"error={error or '<none>'}"
    )
    logger.debug(message, extra={"session_id": session_id, "tool_name": method})


_TOOL_LOGGER_CONFIGURED_PATH: str | None = None


def _configure_tool_logging(log_path: str | Path) -> None:
    """Enable per-call logging of every tool invocation to a rotating file."""
    global _TOOL_LOGGER_CONFIGURED_PATH
    if not log_path:
        return
    path_str = str(log_path)
    if _TOOL_LOGGER_CONFIGURED_PATH == path_str:
        return
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mcp_1c.tools")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] [session=%(session_id)s] [tool=%(tool_name)s] %(message)s"
        )
    )
    logger.addHandler(handler)
    _TOOL_LOGGER_CONFIGURED_PATH = path_str
    logger.info("Tool call logging enabled: %s", path, extra={"session_id": "-", "tool_name": "-"})


_TOOL_CALL_WRAPPER_INSTALLED = False


def _install_tool_call_logger() -> None:
    """Wrap mcp.call_tool so every MCP tool invocation is logged."""
    global _TOOL_CALL_WRAPPER_INSTALLED
    if _TOOL_CALL_WRAPPER_INSTALLED:
        return
    original = mcp.call_tool

    async def _logged_call_tool(*args: Any, **kwargs: Any) -> Any:
        # The SDK may call call_tool(name, arguments) or (ctx, name, arguments).
        # Extract tool name and arguments robustly.
        name = ""
        arguments: dict[str, Any] | None = None
        if len(args) >= 2 and isinstance(args[0], str):
            name = args[0]
            arguments = args[1]
        elif len(args) >= 3:
            name = args[1]
            arguments = args[2]
        else:
            name = kwargs.get("name", "")
            arguments = kwargs.get("arguments")

        params = arguments or {}
        session_id = DEFAULT_SESSION
        if isinstance(params, dict):
            session_id = params.get("session_id", DEFAULT_SESSION)
        start = time.monotonic()
        try:
            result = await original(*args, **kwargs)
            _log_tool_call(session_id, name, params, result, None, time.monotonic() - start)
            return result
        except Exception as exc:
            _log_tool_call(session_id, name, params, None, str(exc), time.monotonic() - start)
            raise

    mcp.call_tool = _logged_call_tool
    _TOOL_CALL_WRAPPER_INSTALLED = True


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


def _json_list(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [part.strip() for part in re.split(r"[,;\n]", text) if part.strip()]


def _effective_dump_connection(
    sess: SessionState,
    connection_mode: str,
    connection_value: str,
    file_infobase_path: str,
) -> tuple[str, str]:
    """Return Designer-compatible dump connection mode/value.

    DumpConfigToFiles cannot work through a web /WS publication.  The caller may
    therefore pass a direct server ref (server/ib) or a file infobase path.  For
    sessions that were started directly, we reuse the stored target connection.
    """
    mode = (connection_mode or "auto").strip().lower()
    value = (connection_value or file_infobase_path or "").strip()
    if mode == "auto" and value:
        mode = _normalize_connection_mode("auto", value, value)
    if mode == "auto" and sess.target_connection_mode:
        mode = sess.target_connection_mode
        value = sess.target_connection_value
    if mode == "auto" and sess.base_url and _normalize_connection_mode("auto", sess.base_url, sess.base_url) != "ws":
        mode = _normalize_connection_mode("auto", sess.base_url, sess.base_url)
        value = sess.base_url
    if mode in {"file", "file-direct", "direct-file", "file_direct", "direct_file"}:
        return "file-direct", str(Path(value).expanduser())
    if mode == "server":
        return "server", value
    if value and _normalize_connection_mode("auto", value, value) in {"server", "file-direct"}:
        normalized = _normalize_connection_mode("auto", value, value)
        return ("file-direct" if normalized == "file-direct" else "server"), value
    raise ValueError(
        "RAG source dump requires a direct infobase connection: server/infobase (/S) "
        "or file infobase path (/F). Web /WS URLs cannot be dumped by Designer; pass "
        "dump_connection_value='server/ib', file_infobase_path, or cf_files/cfe_files."
    )


def _rag_dump_root(session_id: str, root: str = "") -> Path:
    base = Path(root).expanduser() if root else os_support.default_data_dir() / "rag-dumps"
    digest = hashlib.sha1(f"{session_id}:{time.time()}".encode()).hexdigest()[:10]
    return base / f"{_safe_snapshot_part(session_id)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{digest}"


def _register_dumped_source(name: str, path: Path, kind: str) -> dict[str, Any]:
    detected = _RAG.detect_source(str(path))
    effective_format = str(detected.get("format") or "auto")
    result = _RAG.add_source(name=name, path=str(path), kind=kind, format=effective_format)
    result["detected"] = detected
    result["generated"] = True
    return result


def _dump_source_name(prefix: str, runtime_name: str, runtime_version: str, used: set[str]) -> str:
    base = _safe_snapshot_part("_".join(part for part in ["dump", prefix, runtime_name, runtime_version] if part))
    name = base[:120] or "dump_source"
    original = name
    index = 2
    while name in used:
        name = f"{original}_{index}"
        index += 1
    used.add(name)
    return name


def _is_ignored_runtime_extension_name(name: str) -> bool:
    """Return True for service/fresh patch extensions that should not be added to RAG snapshots."""
    return str(name or "").casefold().startswith("ef_")


def _rag_match_needs_dump(match: dict[str, Any], target_version: str) -> bool:
    if not match.get("matched"):
        return True
    # A nearest-version match is useful as a fallback for read-only setups, but
    # if we are allowed to dump the live infobase then the live source is a
    # better/suitable version and should replace the nearest available one.
    return bool(target_version and not _rag_match_public(match).get("exact_version"))


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
    watch: Annotated[bool, "Remember this directory and auto-discover new sources on future RAG listings"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Discover/register 1C configuration and extension source directories under a root directory."""
    params = {"path": path, "prefix": prefix, "recursive": recursive, "watch": watch}
    disabled = _rag_require_enabled(session_id, "rag_sources_add_from_directory", params)
    if disabled is not None:
        return disabled
    return _record_tool(session_id, "rag_sources_add_from_directory", params, _RAG.add_sources_from_directory(path, prefix=prefix, recursive=recursive, max_depth=4, watch=watch))


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
    dump_if_missing: Annotated[bool, "Dump live/configuration sources when an exact RAG source is missing"] = True,
    dump_connection_mode: Annotated[str, "auto | server | file-direct; required for dumping when current session uses a web URL"] = "auto",
    dump_connection_value: Annotated[str, "Direct server/infobase (/S) or file infobase path (/F) used only for source dumping"] = "",
    file_infobase_path: Annotated[str, "Alias for dump_connection_value when dumping from a file infobase"] = "",
    dump_username: Annotated[str, "Optional infobase username for direct Designer dump; defaults to session username"] = "",
    dump_password: Annotated[str, "Optional infobase password for direct Designer dump; defaults to session password"] = "",
    dump_all_extensions: Annotated[bool, "Dump all installed extensions via Designer -AllExtensions when dump_if_missing is enabled"] = True,
    cf_files: Annotated[str, "Optional JSON/comma list of .cf files to load into temporary file IBs and dump"] = "",
    cfe_files: Annotated[str, "Optional JSON/comma list of .cfe files to load into temporary file IBs and dump as extensions"] = "",
    dump_root: Annotated[str, "Optional persistent directory for generated RAG source dumps"] = "",
    cleanup_tmp_infobases: Annotated[bool, "Remove temporary file infobases created while loading CF/CFE files"] = True,
    cleanup_generated_sources: Annotated[bool, "Remove generated XML source dumps after a successful RAG index build"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Auto-configure the session RAG snapshot from the live 1C configuration and installed extensions.

    The tool reads the live main configuration/version from `ВерсииПодсистем`, reads
    installed extensions from `ВерсииРасширений`, matches them against registered
    RAG sources by metadata name and version, and creates/binds a snapshot.

    When no exact/suitable source exists and `dump_if_missing=true`, Answer42 can
    create RAG sources by dumping the configuration and extensions to Designer XML.
    Dumping requires a direct Designer connection: server infobase (`server/ib`) or
    file infobase path.  Web `/WS` URLs cannot be dumped; for web sessions pass
    `dump_connection_value`, `file_infobase_path`, or `cf_files`/`cfe_files`.
    If the direct dump connection has different users than the web publication
    (for example multitenant/separated-data infobases), pass `dump_username` and
    `dump_password`; otherwise the current session credentials are reused.
    Temporary infobases created from CF/CFE files are deleted after dumping by
    default.  Generated XML source directories are removed after a successful
    index build by default, because the SQLite RAG index is self-contained; pass
    `cleanup_generated_sources=false` when the dumped XML source tree should be
    kept under `dump_root` for inspection or reuse.
    """
    params = {
        "snapshot_name": snapshot_name,
        "build_index": build_index,
        "sync_sources": sync_sources,
        "dump_if_missing": dump_if_missing,
        "dump_connection_mode": dump_connection_mode,
        "dump_connection_value": dump_connection_value,
        "file_infobase_path": file_infobase_path,
        "dump_username": dump_username,
        "dump_password": "***" if dump_password else "",
        "dump_all_extensions": dump_all_extensions,
        "cf_files": cf_files,
        "cfe_files": cfe_files,
        "dump_root": dump_root,
        "cleanup_tmp_infobases": cleanup_tmp_infobases,
        "cleanup_generated_sources": cleanup_generated_sources,
    }
    disabled = _rag_require_enabled(session_id, "rag_session_autoconfigure", params)
    if disabled is not None:
        return disabled

    sess = _session(session_id)

    available = _RAG.available_configurations(sync=sync_sources)
    sources = [*available.get("configurations", []), *available.get("extensions", []), *available.get("other_sources", [])]
    base_sources = [src for src in sources if str(src.get("kind") or "") in {"base", "configuration", "standalone"}]
    extension_sources = [src for src in sources if str(src.get("kind") or "") in {"extension", "standalone"}]

    config = await configuration_name_version_from_subsystems(session_id=session_id)
    extensions_info = await installed_extensions_from_catalog(session_id=session_id)
    config_name = str(config.get("configuration_name") or "")
    config_version = str(config.get("configuration_version") or "")
    installed_extensions = extensions_info.get("extensions", []) if isinstance(extensions_info, dict) else []
    extensions_from_designer = False

    warnings: list[str] = []
    summary_candidate = _RAG.summary_backend.find_configuration(config_name, config_version) if getattr(_RAG, "summary_backend", None) is not None else None
    if summary_candidate:
        warnings.extend(summary_candidate.get("warnings") or [])
        extra_layers: list[dict[str, Any]] = []
        extension_matches: list[dict[str, Any]] = []
        skipped_extensions: list[dict[str, Any]] = []
        config_code = str(summary_candidate["config_code"])
        for extension in installed_extensions:
            ext_name = str(extension.get("name") or "")
            ext_version = str(extension.get("version") or "")
            if _is_ignored_runtime_extension_name(ext_name):
                skipped = {"name": ext_name, "version": ext_version, "reason": "ignored_service_extension"}
                skipped_extensions.append(skipped)
                warnings.append(f"Ignored service/fresh patch extension {ext_name!r} from runtime extensions list.")
                continue
            summary_extension = _RAG.summary_backend.find_extension(config_code, ext_name, ext_version)
            if summary_extension is None:
                skipped = {"name": ext_name, "version": ext_version, "reason": "summary_layer_not_found"}
                skipped_extensions.append(skipped)
                warnings.append(f"Summary RAG source for extension {ext_name!r} {ext_version!r} not found; extension was not added to snapshot.")
                continue
            warnings.extend(summary_extension.get("warnings") or [])
            extra_layers.append({
                "layer": str(summary_extension.get("layer") or ""),
                "role": "extension",
                "manifest": summary_extension.get("manifest") or {},
            })
            extension_matches.append({
                "matched": True,
                "runtime_name": ext_name,
                "runtime_version": ext_version,
                "source_name": f"summary:{config_code}:{summary_extension.get('layer') or ''}",
                "source_path": f"{_RAG.summary_backend.project}@{_RAG.summary_backend.ref}:{config_code}/{summary_extension.get('layer') or ''}",
                "source_kind": "extension",
                "source_version": summary_extension.get("version", ""),
                "source_metadata_name": summary_extension.get("name", ext_name),
                "exact_version": not bool(summary_extension.get("warnings")),
            })
        summary_snapshot = _RAG.summary_backend.create_snapshot(
            config_code=config_code,
            layer=str(summary_candidate.get("layer") or "configuration"),
            identifier=str(summary_candidate.get("identifier") or ""),
            version=str(summary_candidate.get("version") or ""),
            name=snapshot_name,
            manifest=summary_candidate.get("manifest") or None,
            warnings=warnings,
            extra_layers=extra_layers,
        )
        sess.rag_snapshot = str(summary_snapshot.get("name") or "")
        if sess.base_url:
            _save_rag_binding(sess.base_url, sess.rag_snapshot)
        _persist_sessions()
        index_result = _RAG.build_index(snapshot=sess.rag_snapshot) if build_index else None
        result = {
            "snapshot": sess.rag_snapshot,
            "snapshot_result": summary_snapshot,
            "index_result": index_result,
            "warnings": warnings,
            "runtime_configuration": {"name": config_name, "version": config_version, "match": {"matched": True, "source_name": f"summary:{config_code}:configuration", "source_kind": "configuration", "source_version": summary_candidate.get("version", ""), "exact_version": not bool(summary_candidate.get("warnings"))}},
            "runtime_extensions": installed_extensions,
            "extensions_from_designer": False,
            "extension_matches": extension_matches,
            "skipped_extensions": skipped_extensions,
            "dumps": [],
            "generated_sources": [],
            "session_snapshot": sess.rag_snapshot,
            "summary_backend": {"enabled": True, "configured": True, "snapshot": sess.rag_snapshot, "project": _RAG.summary_backend.project, "ref": _RAG.summary_backend.ref, "config_code": config_code, "layers": [layer.get("layer") for layer in summary_snapshot.get("summary_layers", [])]},
        }
        return _record_tool(session_id, "rag_session_autoconfigure", params, result)

    dumps: list[dict[str, Any]] = []
    generated_sources: list[dict[str, Any]] = []
    used_generated_names = {str(src.get("name") or "") for src in sources}
    dump_base_dir: Path | None = None

    cf_paths = [Path(item).expanduser() for item in _json_list(cf_files)]
    cfe_paths = [Path(item).expanduser() for item in _json_list(cfe_files)]
    effective_dump_username = dump_username if dump_username else sess.username
    effective_dump_password = dump_password if dump_password else sess.password

    if dump_if_missing and cf_paths:
        dump_base_dir = dump_base_dir or _rag_dump_root(session_id, dump_root)
        for cf_path in cf_paths:
            source_name = _dump_source_name("cf", cf_path.stem, "", used_generated_names)
            out_dir = dump_base_dir / source_name
            dump_info = await asyncio.to_thread(
                dump_cf_to_xml,
                sess.platform_dir,
                cf_path,
                out_dir,
                sess.username,
                sess.password,
                None,
                sess.display,
                cleanup_tmp_infobases,
            )
            dumps.append(dump_info | {"source_name": source_name, "source_kind": "configuration"})
            generated = _register_dumped_source(source_name, out_dir, "configuration")
            generated_sources.append(generated)
            sources.append(generated)
            base_sources.append(generated)

    if dump_if_missing and cfe_paths:
        dump_base_dir = dump_base_dir or _rag_dump_root(session_id, dump_root)
        for cfe_path in cfe_paths:
            source_name = _dump_source_name("cfe", cfe_path.stem, "", used_generated_names)
            out_dir = dump_base_dir / source_name
            dump_info = await asyncio.to_thread(
                dump_cf_to_xml,
                sess.platform_dir,
                cfe_path,
                out_dir,
                sess.username,
                sess.password,
                cfe_path.stem,
                sess.display,
                cleanup_tmp_infobases,
            )
            dumps.append(dump_info | {"source_name": source_name, "source_kind": "extension"})
            generated = _register_dumped_source(source_name, out_dir, "extension")
            generated_sources.append(generated)
            sources.append(generated)
            extension_sources.append(generated)

    if dump_if_missing and dump_all_extensions:
        dump_mode, dump_value = _effective_dump_connection(sess, dump_connection_mode, dump_connection_value, file_infobase_path)
        dump_base_dir = dump_base_dir or _rag_dump_root(session_id, dump_root)
        all_ext_dir = dump_base_dir / "dump_all_extensions"
        all_ext_dump_info = await asyncio.to_thread(
            dump_all_extensions_to_xml,
            sess.platform_dir,
            dump_mode,
            dump_value,
            all_ext_dir,
            effective_dump_username,
            effective_dump_password,
            sess.display,
        )
        dumps.append(all_ext_dump_info | {"source_name": "dump_all_extensions", "source_kind": "extensions_container"})
        ignored_extensions: list[str] = []
        for ext_dump in all_ext_dump_info.get("extensions", []) or []:
            out_dir = Path(str(ext_dump.get("output_dir") or ""))
            ext_meta = _source_metadata(str(out_dir))
            ext_name = ext_meta.get("name") or str(ext_dump.get("name") or out_dir.name)
            ext_version = ext_meta.get("version") or ""
            if _is_ignored_runtime_extension_name(ext_name):
                ignored_extensions.append(ext_name)
                dumps.append(ext_dump | {
                    "source_kind": "extension",
                    "runtime_name": ext_name,
                    "runtime_version": ext_version,
                    "ignored": True,
                    "ignore_reason": "EF_* service/fresh patch extension",
                })
                continue
            source_name = _dump_source_name("ext", ext_name, ext_version, used_generated_names)
            generated = _register_dumped_source(source_name, out_dir, "extension")
            generated["runtime_name"] = ext_name
            generated["runtime_version"] = ext_version
            generated_sources.append(generated)
            sources.append(generated)
            extension_sources.append(generated)
            dumps.append(ext_dump | {
                "source_name": source_name,
                "source_kind": "extension",
                "runtime_name": ext_name,
                "runtime_version": ext_version,
            })
            if not any(str(ext.get("name") or "") == ext_name for ext in installed_extensions):
                installed_extensions.append({"name": ext_name, "version": ext_version, "source": "designer_all_extensions"})
        if ignored_extensions:
            warnings.append(f"Ignored {len(ignored_extensions)} EF_* service/fresh patch extensions from Designer dump.")
        extensions_from_designer = bool(all_ext_dump_info.get("extensions_count"))

    base_match = _best_rag_source_match(config_name, config_version, base_sources, None)
    if _rag_match_needs_dump(base_match, config_version):
        if not dump_if_missing:
            if not base_match.get("matched"):
                raise ValueError(f"RAG source for runtime configuration {config_name!r} {config_version!r} not found")
            warnings.append(str(base_match.get("warning") or ""))
        else:
            dump_mode, dump_value = _effective_dump_connection(sess, dump_connection_mode, dump_connection_value, file_infobase_path)
            dump_base_dir = dump_base_dir or _rag_dump_root(session_id, dump_root)
            source_name = _dump_source_name("base", config_name, config_version, used_generated_names)
            out_dir = dump_base_dir / source_name
            dump_info = await asyncio.to_thread(
                dump_infobase_to_xml,
                sess.platform_dir,
                dump_mode,
                dump_value,
                out_dir,
                effective_dump_username,
                effective_dump_password,
                None,
                sess.display,
            )
            dumps.append(dump_info | {"source_name": source_name, "source_kind": "configuration"})
            generated = _register_dumped_source(source_name, out_dir, "configuration")
            generated_sources.append(generated)
            sources.append(generated)
            base_sources.append(generated)
            base_match = _best_rag_source_match(config_name, config_version, base_sources, None)
            if not base_match.get("matched"):
                # The dumped source is still the only available representation; use it
                # even if metadata matching cannot extract the exact name/version.
                base_match = {"matched": True, "warning": "Using freshly dumped base configuration source; exact metadata match was not detected.", "candidate": {"source": generated, "metadata": _source_metadata(str(out_dir)), "similarity": 1.0, "source_version": config_version, "exact_version": False}}
    if base_match.get("warning"):
        warnings.append(str(base_match["warning"]))
    base_candidate = base_match["candidate"]
    base_source_name = str(base_candidate["source"].get("name") or "")

    extension_source_names: list[str] = []
    extension_matches: list[dict[str, Any]] = []
    used_extension_sources: set[str] = set()
    filtered_installed_extensions = []
    for extension in installed_extensions:
        ext_name = str(extension.get("name") or "")
        if _is_ignored_runtime_extension_name(ext_name):
            warnings.append(f"Ignored EF_* service/fresh patch extension {ext_name!r} from runtime extensions list.")
            continue
        filtered_installed_extensions.append(extension)

    installed_extensions = filtered_installed_extensions
    for extension in installed_extensions:
        ext_name = str(extension.get("name") or "")
        ext_version = str(extension.get("version") or "")
        match = _best_rag_source_match(ext_name, ext_version, extension_sources, None)
        public_match = _rag_match_public(match) | {"runtime_name": ext_name, "runtime_version": ext_version}
        if _rag_match_needs_dump(match, ext_version):
            if dump_if_missing:
                dump_mode, dump_value = _effective_dump_connection(sess, dump_connection_mode, dump_connection_value, file_infobase_path)
                dump_base_dir = dump_base_dir or _rag_dump_root(session_id, dump_root)
                source_name = _dump_source_name("ext", ext_name, ext_version, used_generated_names)
                out_dir = dump_base_dir / source_name
                dump_info = await asyncio.to_thread(
                    dump_infobase_to_xml,
                    sess.platform_dir,
                    dump_mode,
                    dump_value,
                    out_dir,
                    effective_dump_username,
                    effective_dump_password,
                    ext_name,
                    sess.display,
                )
                dumps.append(dump_info | {"source_name": source_name, "source_kind": "extension", "runtime_name": ext_name, "runtime_version": ext_version})
                generated = _register_dumped_source(source_name, out_dir, "extension")
                generated_sources.append(generated)
                sources.append(generated)
                extension_sources.append(generated)
                match = _best_rag_source_match(ext_name, ext_version, extension_sources, None)
                if not match.get("matched"):
                    match = {"matched": True, "warning": "Using freshly dumped extension source; exact metadata match was not detected.", "candidate": {"source": generated, "metadata": _source_metadata(str(out_dir)), "similarity": 1.0, "source_version": ext_version, "exact_version": False}}
                public_match = _rag_match_public(match) | {"runtime_name": ext_name, "runtime_version": ext_version}
            else:
                extension_matches.append(public_match)
                if not match.get("matched"):
                    warnings.append(f"RAG source for extension {ext_name!r} {ext_version!r} not found; extension was not added to snapshot.")
                    continue
        extension_matches.append(public_match)
        if match.get("warning"):
            warnings.append(str(match["warning"]))
        source_name = str(match["candidate"]["source"].get("name") or "")
        if source_name and source_name not in used_extension_sources:
            extension_source_names.append(source_name)
            used_extension_sources.add(source_name)

    effective_snapshot_name = snapshot_name or "+".join([_safe_snapshot_part(config_name), *(_safe_snapshot_part(ext.get("name", "")) for ext in installed_extensions[:20])])
    if len(effective_snapshot_name) > 180 or len(installed_extensions) > 20:
        digest = hashlib.sha1(effective_snapshot_name.encode("utf-8")).hexdigest()[:10]
        effective_snapshot_name = f"{_safe_snapshot_part(config_name)}+extensions-{digest}"

    snapshot = _RAG.create_snapshot(name=effective_snapshot_name, base=base_source_name, extensions=extension_source_names)
    sess.rag_snapshot = str(snapshot.get("name") or effective_snapshot_name)
    if sess.base_url:
        _save_rag_binding(sess.base_url, sess.rag_snapshot)
    _persist_sessions()

    index_result = _RAG.build_index(snapshot=sess.rag_snapshot) if build_index else None
    cleanup_generated_sources_result = None
    if cleanup_generated_sources and index_result is not None and dump_base_dir is not None:
        cleanup_generated_sources_result = _RAG.cleanup_generated_source_files_under(dump_base_dir)
    result = {
        "snapshot": sess.rag_snapshot,
        "snapshot_result": snapshot,
        "index_result": index_result,
        "warnings": warnings,
        "runtime_configuration": {"name": config_name, "version": config_version, "match": _rag_match_public(base_match)},
        "runtime_extensions": installed_extensions,
        "extensions_from_designer": extensions_from_designer,
        "extension_matches": extension_matches,
        "dumps": dumps,
        "generated_sources": generated_sources,
        "generated_sources_count": len(generated_sources),
        "dump_root": str(dump_base_dir) if dump_base_dir else "",
        "cleanup_generated_sources": cleanup_generated_sources_result,
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
            "blocked_action": _public_blocked_action(sess.blocked_action),
            "manager_alive": bool((rt and rt.manager_process and rt.manager_process.poll() is None) or _pid_is_running(sess.manager_pid)),
            "ibsrv_alive": bool((rt and rt.server_process and rt.server_process.poll() is None) or _pid_is_running(sess.ibsrv_pid)),
            "testclient_port": sess.testclient_port,
            "testclient_alive": bool((sess.test_client_process and sess.test_client_process.poll() is None) or _pid_is_running(sess.test_client_pid)),
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
        "target_connection_mode": sess.target_connection_mode,
        "target_connection_value": sess.target_connection_value,
        "version": _platform.platform_version_from_dir(sess.platform_dir),
        "display": sess.display,
        "client_display": sess.client_display,
        "bridge": bridge,
        "blocked_action": _public_blocked_action(sess.blocked_action),
        "manager": None if rt is None else {
            "pid": rt.manager_process.pid if rt.manager_process else (sess.manager_pid or None),
            "alive": (rt.manager_process.poll() is None if rt.manager_process else _pid_is_running(sess.manager_pid)),
            "server_pid": rt.server_process.pid if rt.server_process else (sess.ibsrv_pid or None),
            "server_alive": (rt.server_process.poll() is None if rt.server_process else _pid_is_running(sess.ibsrv_pid)),
            "port": rt.server.port,
            "log_dir": str(rt.log_dir),
        },
        "test_client": {
            "port": sess.testclient_port,
            "pid": sess.test_client_process.pid if sess.test_client_process else (sess.test_client_pid or None),
            "alive": (sess.test_client_process.poll() is None if sess.test_client_process else _pid_is_running(sess.test_client_pid)),
            "base_url": sess.base_url,
            "target_connection_mode": sess.target_connection_mode,
            "target_connection_value": sess.target_connection_value,
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
    credential_account_id = _current_account_id()
    saved_credentials_used = False
    saved_credentials_unverified = False
    input_looks_like_url = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", str(base_url or "")))
    if credential_ref is not None:
        # For saved titles/aliases, replace title -> real URL.  For URL/wildcard matches,
        # keep the original requested URL; using the wildcard pattern itself as /WS value
        # would make the 1C client fail with invalid connection parameters.
        if credential_ref.get("url") and not input_looks_like_url:
            base_url = str(credential_ref["url"])
        original_username = username
        original_password = password
        if not username:
            username = str(credential_ref.get("username", ""))
        if not password:
            password = str(credential_ref.get("password", ""))
        saved_credentials_used = (
            (not original_username or original_username == credential_ref.get("username"))
            and (not original_password or original_password == credential_ref.get("password"))
        )
        saved_credentials_unverified = not bool(credential_ref.get("verified", False))
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
            sess.xvfb_pid = sess.xvfb_process.pid
        # Separate display for the test client so screenshots capture only the client window
        client_display = "" if os_support.IS_WINDOWS else runtime.find_free_display(runtime.DEFAULT_DISPLAY_START + display_offset + 1)
        sess.client_display = client_display
        if client_display:
            sess.client_xvfb_process = runtime.start_xvfb(client_display)
            sess.client_xvfb_pid = sess.client_xvfb_process.pid
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
            sess.ibsrv_pid = server_proc.pid
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
        sess.manager_pid = manager_proc.pid
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
            if saved_credentials_used and saved_credentials_unverified and _looks_like_auth_failure(str(diagnostic_error)):
                _discard_unverified_credentials(connect_base_url or connect_connection_value, username, password, credential_account_id)
            await _cleanup_failed_start_session(sess, "test-client startup failure")
            raise diagnostic_error from exc
        if saved_credentials_used:
            _mark_credentials_verified(connect_base_url or connect_connection_value, username, password, credential_account_id)
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
    return _credentials_store().lookup(base_url, account_id=_current_account_id())


def _resolve_credential_reference(value: str) -> dict[str, str] | None:
    if not _CREDENTIAL_STORE_ENABLED or not value:
        return None
    return _credentials_store().resolve(value, account_id=_current_account_id())


def _mark_credentials_verified(url: str, username: str, password: str, account_id: str | None) -> None:
    if not _CREDENTIAL_STORE_ENABLED or not url or not username:
        return
    try:
        _credentials_store().mark_verified(url, username, password, account_id=account_id)
    except Exception:
        logging.exception("Failed to mark credentials as verified for %s", url)


def _discard_unverified_credentials(url: str, username: str, password: str, account_id: str | None) -> None:
    if not _CREDENTIAL_STORE_ENABLED or not url or not username:
        return
    try:
        removed = _credentials_store().remove_matching(url, username, password, account_id=account_id)
        if removed:
            logging.warning("Removed unverified credentials after authentication failure for %s", url)
    except Exception:
        logging.exception("Failed to remove unverified credentials for %s", url)


@mcp.tool()
def credentials_check(base_url: str) -> dict[str, object]:
    """Check whether credentials exist for a base URL or saved title without returning secrets."""
    if not _CREDENTIAL_STORE_ENABLED:
        return {"base_url": base_url, "found": False, "disabled": True}
    credentials = _credentials_store().resolve(base_url, account_id=_current_account_id())
    if credentials is None:
        return {"base_url": base_url, "found": False}
    result: dict[str, object] = {"base_url": base_url, "found": True, "url": credentials.get("url", "")}
    if credentials.get("title"):
        result["title"] = credentials["title"]
    result["account_id"] = _current_account_id()
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
    without exposing secrets in MCP tool-call arguments.  Entries are scoped to the
    current MCP account.
    """
    if not _CREDENTIAL_STORE_ENABLED:
        raise _credential_store_disabled_error()
    alias_list = [part.strip() for part in aliases.split(",") if part.strip()]
    account_id = _current_account_id()
    updated = _credentials_store().upsert(
        url, username, password, title=title, aliases=alias_list, account_id=account_id
    )
    result: dict[str, object] = {"url": url, "saved": True, "updated": updated, "account_id": account_id}
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
    account_id = _current_account_id()
    removed = _credentials_store().remove(url, account_id=account_id)
    return {"url": url, "removed": removed, "account_id": account_id}


@mcp.tool()
def credentials_list() -> list[dict[str, object]]:
    """List configured credential URL patterns/titles (no usernames or passwords)."""
    if not _CREDENTIAL_STORE_ENABLED:
        return []
    return _credentials_store().list_entries(account_id=_current_account_id())


@mcp.tool()
def credentials_titles() -> list[dict[str, object]]:
    """List saved credential titles/aliases that can be used as start_session base_url."""
    if not _CREDENTIAL_STORE_ENABLED:
        return []
    return _credentials_store().titles(account_id=_current_account_id())


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
    sess.target_connection_mode = str(target_connection_mode)
    sess.target_connection_value = str(r_base if target_connection_mode == "server" else (db_path if target_connection_mode in {"file", "file-direct"} else r_value))
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
    sess.test_client_pid = sess.test_client_process.pid
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
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return all windows of the tested 1C client application."""
    return await _bridge_call_recorded(session_id, "windows_list", {})


@mcp.tool()
async def activate_window(
    index: Annotated[int, "Window index from windows_list; -1 means search by name/title"] = -1,
    title: Annotated[str, "Case-insensitive title substring"] = "",
    name: Annotated[str, "Exact window name when available"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Activate/switch to a tested 1C client window by index, title or name."""
    return await _bridge_call_recorded(
        session_id,
        "activate_window",
        {"index": index, "title": title, "name": name},
    )


@mcp.tool()
async def goto_start_page(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Navigate the main test-client window to the start page."""
    return await _bridge_call_recorded(
        session_id,
        "goto_start_page",
        {},
    )


@mcp.tool()
async def goto_previous_window(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Navigate the main test-client window to the previous open window."""
    return await _bridge_call_recorded(
        session_id,
        "goto_previous_window",
        {},
    )


@mcp.tool()
async def goto_next_window(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Navigate the main test-client window to the next open window."""
    return await _bridge_call_recorded(
        session_id,
        "goto_next_window",
        {},
    )



@mcp.tool()
async def wait_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object title substring"] = "",
    type: Annotated[str, "1C test object type"] = "",
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until an object appears in the tested client UI tree."""
    return await _bridge_call_recorded(session_id, "wait_object", {"name": name, "title": title, "type": type, "timeout": timeout})


@mcp.tool()
async def wait_form(
    form_name: Annotated[str, "1C metadata form name, e.g. Catalog.X.Form.ItemForm"] = "",
    title: Annotated[str, "Form title substring"] = "",
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until a form appears."""
    return await _bridge_call_recorded(session_id, "wait_form", {"form_name": form_name, "title": title, "timeout": timeout})


@mcp.tool()
async def wait_window(
    title: Annotated[str, "Window title substring"] = "",
    name: Annotated[str, "Window exact name when available"] = "",
    timeout: Annotated[int, "Timeout in seconds"] = 15,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until a client window appears."""
    return await _bridge_call_recorded(session_id, "wait_window", {"title": title, "name": name, "timeout": timeout})


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
async def client_busy_status(
    probe_timeout: Annotated[int, "Short probe timeout in seconds; keep small to avoid blocking on a busy thin client"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Check whether the 1C test client/bridge is responsive and whether a dangerous action is blocking the interface."""
    result = await _client_busy_status_impl(session_id, probe_timeout=max(1, int(probe_timeout)))
    return _record_tool(session_id, "client_busy_status", {"probe_timeout": probe_timeout}, result)


@mcp.tool()
async def wait_client_idle(
    timeout: Annotated[int, "Maximum wait time in seconds"] = 60,
    interval: Annotated[int, "Polling interval in seconds"] = 3,
    probe_timeout: Annotated[int, "Short per-probe timeout in seconds"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Wait until the 1C test client/bridge becomes responsive after a long or blocking UI action."""
    started = time.monotonic()
    deadline = started + max(1, int(timeout))
    attempts = 0
    last_status: dict[str, Any] | None = None
    sleep_seconds = max(1, int(interval))
    while True:
        attempts += 1
        last_status = await _client_busy_status_impl(session_id, probe_timeout=max(1, int(probe_timeout)))
        if last_status.get("responsive") and not last_status.get("busy"):
            result = {
                "idle": True,
                "responsive": True,
                "reason": "client_idle",
                "attempts": attempts,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "status": last_status,
            }
            return _record_tool(session_id, "wait_client_idle", {"timeout": timeout, "interval": interval, "probe_timeout": probe_timeout}, result)
        if time.monotonic() >= deadline:
            result = {
                "idle": False,
                "responsive": bool(last_status.get("responsive")) if isinstance(last_status, dict) else False,
                "reason": "timeout_waiting_for_client_idle",
                "attempts": attempts,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "status": last_status,
            }
            return _record_tool(session_id, "wait_client_idle", {"timeout": timeout, "interval": interval, "probe_timeout": probe_timeout}, result)
        await asyncio.sleep(min(sleep_seconds, max(0.0, deadline - time.monotonic())))


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
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open a form field dropdown and return visible choice items when exposed by the 1C test-client API.

    This is a convenience action for the common "open dropdown and read choices" scenario.
    For non-mutating diagnostics of an already-open dropdown use field_dropdown_state.
    """
    return await _bridge_call_recorded(
        session_id,
        "field_dropdown_items",
        {
            "field_name": field_name,
            "search_by_title": search_by_title,
            "timeout": timeout,
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
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return command panel for a found UI object, when supported."""
    return await _bridge_call_recorded(session_id, "object_command_panel", {"name": name, "title": title, "type": type})


@mcp.tool()
async def object_context_menu(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object title substring"] = "",
    type: Annotated[str, "1C test object type"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return context menu for a found UI object, when supported."""
    return await _bridge_call_recorded(session_id, "object_context_menu", {"name": name, "title": title, "type": type})


@mcp.tool()
async def window_command_interface(
    window_title: Annotated[str, "Optional window title substring to activate before reading command interface"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return active window command interface, when supported."""
    return await _bridge_call_recorded(session_id, "window_command_interface", {"window_title": window_title})


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
    window_title: Annotated[str, "Optional window title substring to activate before reading command interface"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return command-interface command tree and flattened command list, optionally filtered by section."""
    return await _bridge_call_recorded(
        session_id,
        "command_interface_commands",
        {"section": section, "window_title": window_title},
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
async def group_current_page(name: Annotated[str, "Group technical name"] = "", title: Annotated[str, "Group title substring"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Return current page of a page-group form element."""
    return await _bridge_call_recorded(session_id, "group_current_page", {"name": name, "title": title})


@mcp.tool()
async def group_activate_page(name: Annotated[str, "Page-group technical name"] = "", title: Annotated[str, "Page-group title substring"] = "", page_name: Annotated[str, "Target page technical name"] = "", page_title: Annotated[str, "Target page title"] = "", *, session_id: Annotated[str, "Session id"] = DEFAULT_SESSION) -> Any:
    """Activate/switch to another page in a page-group form element."""
    return await _bridge_call_recorded(session_id, "group_activate_page", {"name": name, "title": title, "page_name": page_name, "page_title": page_title})


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
async def ui_tree(
    include_rag_types: Annotated[bool, "Enrich UI elements with best-effort types from the local RAG index; disabled by default because it may add lookup cost and verbose metadata"] = False,
    include_hidden: Annotated[bool, "Include invisible form elements/pages; visible/available/read_only flags are returned for serialized nodes"] = False,
    compact: Annotated[bool | None, "Return compact node fields; default follows ONEC_MCP_RESULT_VERBOSITY"] = None,
    include_data: Annotated[bool, "Include data presentations from UI nodes; expensive on large forms, prefer field_value_text/table_rows/tabular_document_text for values"] = False,
    include_command_panels: Annotated[bool | None, "Include command-panel subtrees; overrides profile/command_panels when specified"] = None,
    command_panels: Annotated[str, "Command-panel mode: auto/profile default, include, exclude, or only"] = "auto",
    profile: Annotated[str, "Output profile: navigation (default), diagnostic, data, full, no_command_panels, or command_panels"] = "navigation",
    parent_name: Annotated[str, "Optional exact technical name of a UI node to use as the tree root; children are serialized only under this parent"] = "",
    type: Annotated[str, "Optional UI object type filter. When name, title or type filter is set, ui_tree returns a flat matches list"] = "",
    name: Annotated[str, "Optional exact technical name filter. When name, title or type filter is set, ui_tree returns a flat matches list"] = "",
    title: Annotated[str, "Optional title substring filter. When name, title or type filter is set, ui_tree returns a flat matches list"] = "",
    fields: Annotated[str, "Field-set profile: auto, minimal, navigation, diagnostic, data, full, or comma-separated field names"] = "auto",
    group_mode: Annotated[str, "Group handling mode: include, flatten, flatten_layout, exclude, exclude_empty, or pages_only"] = "include",
    format: Annotated[str, "Response format: json (structured), outline (readable text outline), yaml/yml (readable YAML text)"] = "json",
    rag_snapshot: Annotated[str, "Optional RAG snapshot name/id used for type enrichment"] = "",
    delivery: Annotated[str, "Delivery mode: auto (default) returns small results inline and saves responses above token threshold as resource; inline always returns the selected JSON/outline/yaml; resource always saves full JSON and returns resource_uri + summary + preview"] = "auto",
    preview_max_nodes: Annotated[int, "For delivery=auto/resource, number of top nodes/matches to include in the small inline preview when a resource is returned"] = 40,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return a serialized snapshot of the active 1C UI tree.

    Default profile is `navigation`: visible elements, compact fields, no data
    presentations, no RAG enrichment, and command panels excluded unless requested.
    The tree has no depth limit. Use `include_hidden=true` for hidden pages/elements.
    Pass `parent_name` to serialize only the subtree rooted at a specific form element.
    Use `fields=minimal|navigation|diagnostic|data|full` (or a comma-separated
    field list) to control returned node fields without changing traversal.

    `include_data=true` may call 1C `ПолучитьПредставлениеДанных()` for many nodes;
    do not enable it casually on large forms. Prefer targeted tools for data:
    `field_value_text` for one field, `table_rows` for tables/dynamic lists, and
    `tabular_document_text`/`tabular_document_save` for reports/tabular documents.

    When `name`, `title` or `type` filter is set, the result is a flat `objects` list instead
    of a nested tree. For finding one control, prefer `search_objects`/`find_object`.
    `format=outline` and `format=yaml|yml` return readable text representations
    inside a structured tool result; JSON remains the default machine-readable form.

    `delivery=auto` is the default: small results stay inline, but large results
    above `ONEC_MCP_UI_TREE_RESOURCE_AUTO_THRESHOLD_TOKENS` (default 50k estimated tokens) are
    saved as MCP resources. Use `delivery=inline` to force inline output and
    `delivery=resource` to force resource output for large/verbose diagnostics
    (`profile=full`, `include_hidden=true`, `include_data=true`). Resource
    responses contain only `resource_uri`, `snapshot_id`, summary, metrics and a
    small preview. Then inspect the saved snapshot with
    `ui_tree_resource_search`, `ui_tree_resource_get_node`,
    `ui_tree_resource_children`, or `ui_tree_diff` instead of loading the whole
    tree into the agent context.
    """
    delivery_key = (delivery or "auto").strip().lower()
    if delivery_key not in {"auto", "inline", "resource"}:
        raise ValueError(f"Unsupported ui_tree delivery: {delivery!r}. Expected auto, inline or resource.")
    profile_key = _validate_ui_tree_choice("profile", profile, _UI_TREE_PROFILES)
    command_mode = _validate_ui_tree_choice("command_panels", command_panels, _UI_TREE_COMMAND_PANEL_MODES)
    group_mode_key = _validate_ui_tree_choice("group_mode", group_mode, _UI_TREE_GROUP_MODES)
    fmt = _validate_ui_tree_choice("format", format, _UI_TREE_FORMATS)
    field_names = _ui_tree_field_names(fields, profile_key)
    if profile_key in {"full", "diagnostic", "data"}:
        profile_include_command_panels = True
    elif profile_key == "command_panels":
        profile_include_command_panels = True
        command_mode = "only" if command_mode in {"", "auto"} else command_mode
    else:
        profile_include_command_panels = False
    if profile_key in {"data", "full"} and include_data is False:
        include_data = True
    if compact is None:
        compact = profile_key != "full"
    if include_command_panels is None:
        include_command_panels_effective = profile_include_command_panels
    else:
        include_command_panels_effective = bool(include_command_panels)
    if command_mode in {"include", "full", "with", "all"}:
        include_command_panels_effective = True
    elif command_mode in {"exclude", "without", "none", "off", "no"}:
        include_command_panels_effective = False
    elif command_mode == "only":
        include_command_panels_effective = True

    filtered = bool(name or title or type)
    bridge_params = {
        "include_hidden": include_hidden,
        "include_data": include_data,
        "include_command_panels": include_command_panels_effective,
    }
    if parent_name:
        bridge_params["parent_name"] = parent_name
    if filtered:
        bridge_params.update({"flat": True, "name": name, "title": title, "type": type})
    elif command_mode == "only":
        bridge_params.update({"flat": True, "name": "", "title": "", "type": "", "command_panels_only": True})
    bridge_start = time.monotonic()
    tree = await _bridge_call_recorded(session_id, "ui_tree", bridge_params)
    bridge_ms = (time.monotonic() - bridge_start) * 1000
    transform_start = time.monotonic()
    if parent_name and isinstance(tree, dict) and tree.get("reason") != "parent_not_found" and tree.get("root_parent_name") != parent_name:
        parent_node = _find_ui_node_by_name(tree, parent_name)
        if parent_node is None:
            tree = {"found": False, "parent_name": parent_name, "reason": "parent_not_found"}
        else:
            tree = parent_node
            tree["root_parent_name"] = parent_name
    if isinstance(tree, dict):
        tree["tree_summary"] = _summarize_ui_tree(tree, include_hidden=include_hidden)
    if include_rag_types:
        sess = _session(session_id)
        if _RAG_ENABLED and sess.rag_enabled:
            tree = _enrich_ui_tree_with_rag_types(tree, snapshot=rag_snapshot or sess.rag_snapshot or None)
            if isinstance(tree, dict):
                tree["rag_diagnostic"] = _rag_diagnostic(session_id, rag_snapshot)
        elif isinstance(tree, dict):
            tree["rag_diagnostic"] = _rag_diagnostic(session_id, rag_snapshot)

    if parent_name and isinstance(tree, dict) and tree.get("found") is False and tree.get("reason") == "parent_not_found":
        tree["tree_summary"] = _summarize_ui_tree(tree, include_hidden=include_hidden)
        python_transform_ms = (time.monotonic() - transform_start) * 1000
        if fmt == "outline":
            response = {"format": "outline", "text": "", "tree_summary": tree.get("tree_summary"), "profile": profile_key, "parent_name": parent_name, "found": False, "reason": "parent_not_found"}
            response = _apply_ui_fields(response, field_names)
            response = _attach_ui_tree_metrics(response, _ui_tree_metrics(tree, bridge_ms=bridge_ms, python_transform_ms=python_transform_ms, result=response))
            return _ui_tree_maybe_resource_response(
                response,
                inline_response=response,
                delivery=delivery_key,
                session_id=session_id,
                request={**bridge_params, "profile": profile_key, "fields": fields, "format": fmt, "delivery": delivery_key},
                metrics=response.get("metrics"),
                preview_max_nodes=preview_max_nodes,
                preview_format=fmt,
            )
        if fmt == "yaml":
            response = {"format": "yaml", "yaml": _to_simple_yaml(tree), "tree_summary": tree.get("tree_summary"), "profile": profile_key, "parent_name": parent_name}
            response = _attach_ui_tree_metrics(response, _ui_tree_metrics(tree, bridge_ms=bridge_ms, python_transform_ms=python_transform_ms, result=response))
            return _ui_tree_maybe_resource_response(
                tree,
                inline_response=response,
                delivery=delivery_key,
                session_id=session_id,
                request={**bridge_params, "profile": profile_key, "fields": fields, "format": fmt, "delivery": delivery_key},
                metrics=response.get("metrics"),
                preview_max_nodes=preview_max_nodes,
                preview_format=fmt,
            )
        tree = _apply_ui_fields(tree, field_names)
        tree = _attach_ui_tree_metrics(tree, _ui_tree_metrics(tree, bridge_ms=bridge_ms, python_transform_ms=python_transform_ms, result=tree))
        return _ui_tree_maybe_resource_response(
            tree,
            inline_response=tree,
            delivery=delivery_key,
            session_id=session_id,
            request={**bridge_params, "profile": profile_key, "fields": fields, "format": fmt, "delivery": delivery_key},
            metrics=tree.get("metrics"),
            preview_max_nodes=preview_max_nodes,
            preview_format=fmt,
        )

    summary = tree.get("tree_summary") if isinstance(tree, dict) else None

    if filtered:
        if isinstance(tree, dict) and isinstance(tree.get("objects"), list):
            objects = [obj for obj in tree["objects"] if isinstance(obj, dict)]
        else:
            objects = _flat_ui_nodes(tree, name=name, title=title, type=type)
        if _compact_enabled(compact):
            objects = [_compact_ui_node(obj) for obj in objects]
        result: Any = {
            "found": bool(objects),
            "count": len(objects),
            "objects": objects,
            "filter": {"name": name, "title": title, "type": type},
            "tree_summary": summary,
            "profile": profile_key,
            "fields": fields,
            "command_panels": "only" if command_mode == "only" else ("include" if include_command_panels_effective else "exclude"),
            "flat": True,
        }
        if isinstance(tree, dict) and tree.get("method"):
            result["method"] = tree["method"]
        if parent_name:
            result["parent_name"] = parent_name
    elif command_mode == "only":
        if isinstance(tree, dict) and isinstance(tree.get("objects"), list):
            objects = _dedupe_ui_nodes([obj for obj in tree["objects"] if isinstance(obj, dict)])
        else:
            objects = _dedupe_ui_nodes(_collect_command_panel_nodes(tree))
        if _compact_enabled(compact):
            objects = [_compact_ui_node(obj) for obj in objects]
        result = {
            "found": bool(objects),
            "count": len(objects),
            "objects": objects,
            "tree_summary": summary,
            "profile": profile_key,
            "fields": fields,
            "command_panels": "only",
        }
        if isinstance(tree, dict) and tree.get("method"):
            result["method"] = tree["method"]
        if parent_name:
            result["parent_name"] = parent_name
    else:
        result = tree
        if not include_command_panels_effective:
            result = _transform_command_panels(result, "exclude")
        transformed = _transform_ui_groups(result, group_mode_key)
        if transformed is None:
            transformed = {"type": "filtered_out", "tree_summary": summary}
        result = transformed
        if _compact_enabled(compact) and isinstance(result, dict):
            compact_tree = _compact_ui_node(result)
            if summary:
                compact_tree["tree_summary"] = summary
            if isinstance(tree, dict) and tree.get("rag_diagnostic"):
                compact_tree["rag_diagnostic"] = tree["rag_diagnostic"]
            result = compact_tree

    result = _apply_ui_fields(result, field_names)
    python_transform_ms = (time.monotonic() - transform_start) * 1000
    if fmt == "outline":
        format_start = time.monotonic()
        text = _outline_ui_nodes(result.get("objects", [])) if isinstance(result, dict) and "objects" in result else _outline_ui_tree(result)
        response = {"format": "outline", "text": text, "tree_summary": summary, "profile": profile_key, "fields": fields}
        if parent_name:
            response["parent_name"] = parent_name
        metrics = _ui_tree_metrics(tree, bridge_ms=bridge_ms, python_transform_ms=python_transform_ms, format_ms=(time.monotonic() - format_start) * 1000, result=response)
        response = _attach_ui_tree_metrics(response, metrics)
        return _ui_tree_maybe_resource_response(
            result,
            inline_response=response,
            delivery=delivery_key,
            session_id=session_id,
            request={**bridge_params, "profile": profile_key, "fields": fields, "format": fmt, "delivery": delivery_key},
            metrics=metrics,
            preview_max_nodes=preview_max_nodes,
            preview_format=fmt,
        )
    if fmt == "yaml":
        format_start = time.monotonic()
        response = {"format": "yaml", "yaml": _to_simple_yaml(result), "tree_summary": summary, "profile": profile_key, "fields": fields}
        if parent_name:
            response["parent_name"] = parent_name
        metrics = _ui_tree_metrics(tree, bridge_ms=bridge_ms, python_transform_ms=python_transform_ms, format_ms=(time.monotonic() - format_start) * 1000, result=response)
        response = _attach_ui_tree_metrics(response, metrics)
        return _ui_tree_maybe_resource_response(
            result,
            inline_response=response,
            delivery=delivery_key,
            session_id=session_id,
            request={**bridge_params, "profile": profile_key, "fields": fields, "format": fmt, "delivery": delivery_key},
            metrics=metrics,
            preview_max_nodes=preview_max_nodes,
            preview_format=fmt,
        )
    metrics = _ui_tree_metrics(tree, bridge_ms=bridge_ms, python_transform_ms=python_transform_ms, result=result)
    result = _attach_ui_tree_metrics(result, metrics)
    return _ui_tree_maybe_resource_response(
        result,
        inline_response=result,
        delivery=delivery_key,
        session_id=session_id,
        request={**bridge_params, "profile": profile_key, "fields": fields, "format": fmt, "delivery": delivery_key},
        metrics=metrics,
        preview_max_nodes=preview_max_nodes,
        preview_format=fmt,
    )


@mcp.tool()
async def ui_tree_resource_search(
    resource_uri: Annotated[str, "Resource URI returned by ui_tree(delivery='resource'), e.g. answer42://ui-tree/<session>/<snapshot_id>"] = "",
    snapshot_id: Annotated[str, "Snapshot id returned by ui_tree(delivery='resource'); pass only when resource_uri is unavailable/unambiguous"] = "",
    name: Annotated[str, "Optional exact technical name filter"] = "",
    title: Annotated[str, "Optional title substring filter"] = "",
    type: Annotated[str, "Optional UI object type filter"] = "",
    text: Annotated[str, "Optional case-insensitive substring over type/name/title/text/value/presentation/data_path/path"] = "",
    fields: Annotated[str, "Field-set profile for returned nodes: minimal, navigation, diagnostic, data, full, or comma-separated field names"] = "navigation",
    limit: Annotated[int, "Maximum matched nodes to return"] = 20,
    *,
    session_id: Annotated[str, "Session id (used only for recording/compatibility)"] = DEFAULT_SESSION,
) -> Any:
    """Search a saved ui_tree resource without loading the full tree into context.

    Use after `ui_tree(delivery='resource')` when you need a few controls from a
    large snapshot. The result includes `path` and `index_path`; use either with
    `ui_tree_resource_get_node`, `ui_tree_resource_children`, or
    `ui_tree_resource_action`.
    """
    snapshot = _load_ui_tree_snapshot(resource_uri, snapshot_id)
    payload = _ui_tree_payload_from_snapshot(snapshot)
    max_items = max(1, min(int(limit or 20), 500))
    query_text = _norm_key(text)
    matches: list[dict[str, Any]] = []
    for node in _walk_ui_snapshot(payload):
        if not _ui_node_matches_filters(node, name=name, title=title, type=type):
            continue
        if query_text:
            haystack = " ".join(str(node.get(key) or "") for key in ("type", "name", "title", "text", "value", "presentation", "data_path", "path"))
            if query_text not in _norm_key(haystack):
                continue
        flat = dict(node)
        flat.pop("children", None)
        flat.pop("current_page", None)
        flat.pop("pages", None)
        matches.append(flat)
        if len(matches) >= max_items:
            break
    field_names = _ui_tree_field_names(fields, "navigation")
    result = {
        "found": bool(matches),
        "count": len(matches),
        "objects": _apply_ui_fields([_compact_ui_node(item) for item in matches], field_names),
        "filter": {"name": name, "title": title, "type": type, "text": text, "limit": max_items},
        "resource_uri": snapshot.get("resource_uri") or resource_uri,
        "snapshot_id": snapshot.get("snapshot_id") or snapshot_id,
        "summary": snapshot.get("summary") or {},
    }
    return _record_tool_if_session(session_id, "ui_tree_resource_search", {"resource_uri": resource_uri, "snapshot_id": snapshot_id, "name": name, "title": title, "type": type, "text": text, "fields": fields, "limit": limit}, result)


@mcp.tool()
async def ui_tree_resource_get_node(
    resource_uri: Annotated[str, "Resource URI returned by ui_tree(delivery='resource')"] = "",
    snapshot_id: Annotated[str, "Snapshot id returned by ui_tree(delivery='resource'); pass only when resource_uri is unavailable/unambiguous"] = "",
    path: Annotated[str, "Human-readable node path returned by ui_tree_resource_search/children; use index_path for more stable addressing"] = "",
    index_path: Annotated[str, "Stable numeric path returned as index_path, e.g. /0/3/2"] = "",
    fields: Annotated[str, "Field-set profile: minimal, navigation, diagnostic, data, full, or comma-separated field names"] = "diagnostic",
    *,
    session_id: Annotated[str, "Session id (used only for recording/compatibility)"] = DEFAULT_SESSION,
) -> Any:
    """Return one node from a saved ui_tree resource by `index_path` or `path`."""
    snapshot = _load_ui_tree_snapshot(resource_uri, snapshot_id)
    node = _find_ui_node_by_path(_ui_tree_payload_from_snapshot(snapshot), path=path, index_path=index_path)
    field_names = _ui_tree_field_names(fields, "diagnostic")
    result = {
        "found": node is not None,
        "node": _apply_ui_fields(_compact_ui_node(node) if isinstance(node, dict) else None, field_names) if node is not None else None,
        "path": path,
        "index_path": index_path,
        "resource_uri": snapshot.get("resource_uri") or resource_uri,
        "snapshot_id": snapshot.get("snapshot_id") or snapshot_id,
    }
    return _record_tool_if_session(session_id, "ui_tree_resource_get_node", {"resource_uri": resource_uri, "snapshot_id": snapshot_id, "path": path, "index_path": index_path, "fields": fields}, result)


@mcp.tool()
async def ui_tree_resource_children(
    resource_uri: Annotated[str, "Resource URI returned by ui_tree(delivery='resource')"] = "",
    snapshot_id: Annotated[str, "Snapshot id returned by ui_tree(delivery='resource'); pass only when resource_uri is unavailable/unambiguous"] = "",
    path: Annotated[str, "Parent node path returned by ui_tree_resource_search/get_node"] = "",
    index_path: Annotated[str, "Parent node numeric index_path returned by ui_tree_resource_search/get_node"] = "",
    depth: Annotated[int, "How many child levels to return; use 1-2 for context-friendly reads"] = 1,
    fields: Annotated[str, "Field-set profile: minimal, navigation, diagnostic, data, full, or comma-separated field names"] = "navigation",
    format: Annotated[str, "Response format for the subtree: json, outline, yaml/yml"] = "json",
    *,
    session_id: Annotated[str, "Session id (used only for recording/compatibility)"] = DEFAULT_SESSION,
) -> Any:
    """Return a small subtree from a saved ui_tree resource.

    Prefer this over MCP resource reads when the full snapshot is large. Set
    `depth=1` to inspect direct children, `depth=2` for immediate context.
    """
    fmt = _validate_ui_tree_choice("format", format, _UI_TREE_FORMATS)
    snapshot = _load_ui_tree_snapshot(resource_uri, snapshot_id)
    payload = _ui_tree_payload_from_snapshot(snapshot)
    node = _find_ui_node_by_path(payload, path=path, index_path=index_path) if (path or index_path) else payload
    if node is None:
        result: Any = {"found": False, "path": path, "index_path": index_path, "resource_uri": snapshot.get("resource_uri") or resource_uri, "snapshot_id": snapshot.get("snapshot_id") or snapshot_id}
    else:
        subtree = _trim_ui_tree_depth(node, max(0, min(int(depth or 1), 10)))
        subtree = _apply_ui_fields(_compact_ui_node(subtree), _ui_tree_field_names(fields, "navigation"))
        if fmt == "outline":
            result = {"found": True, "format": "outline", "text": _outline_ui_tree(subtree), "path": path or subtree.get("path"), "index_path": index_path or subtree.get("index_path")}
        elif fmt == "yaml":
            result = {"found": True, "format": "yaml", "yaml": _to_simple_yaml(subtree), "path": path or subtree.get("path"), "index_path": index_path or subtree.get("index_path")}
        else:
            result = {"found": True, "node": subtree, "path": path or subtree.get("path"), "index_path": index_path or subtree.get("index_path")}
        result["resource_uri"] = snapshot.get("resource_uri") or resource_uri
        result["snapshot_id"] = snapshot.get("snapshot_id") or snapshot_id
    return _record_tool_if_session(session_id, "ui_tree_resource_children", {"resource_uri": resource_uri, "snapshot_id": snapshot_id, "path": path, "index_path": index_path, "depth": depth, "fields": fields, "format": format}, result)


@mcp.tool()
async def ui_tree_diff(
    before_resource_uri: Annotated[str, "Resource URI of the earlier ui_tree snapshot"] = "",
    after_resource_uri: Annotated[str, "Resource URI of the later ui_tree snapshot"] = "",
    before_snapshot_id: Annotated[str, "Earlier snapshot id, only if resource URI is unavailable/unambiguous"] = "",
    after_snapshot_id: Annotated[str, "Later snapshot id, only if resource URI is unavailable/unambiguous"] = "",
    fields: Annotated[str, "Field-set profile for changed nodes"] = "navigation",
    limit: Annotated[int, "Maximum added/removed/changed samples per category"] = 50,
    *,
    session_id: Annotated[str, "Session id (used only for recording/compatibility)"] = DEFAULT_SESSION,
) -> Any:
    """Compare two saved ui_tree snapshots and return compact added/removed/changed node samples."""
    before = _load_ui_tree_snapshot(before_resource_uri, before_snapshot_id)
    after = _load_ui_tree_snapshot(after_resource_uri, after_snapshot_id)

    def index(payload: Any) -> dict[str, dict[str, Any]]:
        return {str(node.get("index_path") or node.get("path") or idx): dict(node) for idx, node in enumerate(_walk_ui_snapshot(payload)) if isinstance(node, dict)}

    before_idx = index(_ui_tree_payload_from_snapshot(before))
    after_idx = index(_ui_tree_payload_from_snapshot(after))
    max_items = max(1, min(int(limit or 50), 500))
    field_names = _ui_tree_field_names(fields, "navigation")

    def node_signature(node: dict[str, Any]) -> dict[str, Any]:
        return {k: node.get(k) for k in ("type", "name", "title", "visible", "available", "read_only", "value", "presentation", "data_path") if k in node}

    added_keys = [key for key in after_idx if key not in before_idx]
    removed_keys = [key for key in before_idx if key not in after_idx]
    common_changed = [key for key in after_idx.keys() & before_idx.keys() if node_signature(before_idx[key]) != node_signature(after_idx[key])]
    result = {
        "before_resource_uri": before.get("resource_uri") or before_resource_uri,
        "after_resource_uri": after.get("resource_uri") or after_resource_uri,
        "before_snapshot_id": before.get("snapshot_id") or before_snapshot_id,
        "after_snapshot_id": after.get("snapshot_id") or after_snapshot_id,
        "counts": {"before": len(before_idx), "after": len(after_idx), "added": len(added_keys), "removed": len(removed_keys), "changed": len(common_changed)},
        "added": _apply_ui_fields([_compact_ui_node(after_idx[key]) for key in added_keys[:max_items]], field_names),
        "removed": _apply_ui_fields([_compact_ui_node(before_idx[key]) for key in removed_keys[:max_items]], field_names),
        "changed": [
            {"index_path": key, "before": _apply_ui_fields(_compact_ui_node(before_idx[key]), field_names), "after": _apply_ui_fields(_compact_ui_node(after_idx[key]), field_names)}
            for key in common_changed[:max_items]
        ],
        "limit": max_items,
    }
    return _record_tool_if_session(session_id, "ui_tree_diff", {"before_resource_uri": before_resource_uri, "after_resource_uri": after_resource_uri, "before_snapshot_id": before_snapshot_id, "after_snapshot_id": after_snapshot_id, "fields": fields, "limit": limit}, result)


@mcp.tool()
async def ui_tree_resource_action(
    resource_uri: Annotated[str, "Resource URI returned by ui_tree(delivery='resource')"] = "",
    snapshot_id: Annotated[str, "Snapshot id returned by ui_tree(delivery='resource'); pass only when resource_uri is unavailable/unambiguous"] = "",
    path: Annotated[str, "Node path returned by ui_tree_resource_search/get_node"] = "",
    index_path: Annotated[str, "Node index_path returned by ui_tree_resource_search/get_node"] = "",
    action: Annotated[str, "Action to run on the node: focus, click_button, activate_field, field_value_text, field_info"] = "focus",
    watchdog_timeout: Annotated[int, "For click_button, deadline in seconds before reporting blocked interface; default is 120 seconds because many 1C managed forms open slowly"] = 120,
    *,
    session_id: Annotated[str, "Live session id where the UI action is executed"] = DEFAULT_SESSION,
) -> Any:
    """Run a safe first-class UI action using a node selected from a saved ui_tree snapshot.

    The snapshot is only used to extract stable name/title/type hints; the actual
    action is executed against the current live 1C UI via existing tools. Refresh
    the snapshot after large UI changes before relying on old selectors.
    """
    snapshot = _load_ui_tree_snapshot(resource_uri, snapshot_id)
    node = _find_ui_node_by_path(_ui_tree_payload_from_snapshot(snapshot), path=path, index_path=index_path)
    if not isinstance(node, dict):
        return _record_tool_if_session(session_id, "ui_tree_resource_action", {"resource_uri": resource_uri, "snapshot_id": snapshot_id, "path": path, "index_path": index_path, "action": action}, {"found": False, "reason": "node_not_found"})
    action_key = (action or "focus").strip().lower()
    node_name = str(node.get("name") or "")
    node_title = str(node.get("title") or "")
    node_type = str(node.get("type") or "")
    if action_key == "focus":
        return await focus_object(name=node_name, title=node_title, type=node_type, session_id=session_id)
    if action_key == "click_button":
        return await click_button(name=node_name or node_title, search_by_title=not bool(node_name), watchdog_timeout=watchdog_timeout, session_id=session_id)
    if action_key == "activate_field":
        return await activate_field(field_name=node_name or node_title, search_by_title=not bool(node_name), session_id=session_id)
    if action_key == "field_value_text":
        return await field_value_text(field_name=node_name or node_title, search_by_title=not bool(node_name), session_id=session_id)
    if action_key == "field_info":
        return await field_info(field_name=node_name or node_title, search_by_title=not bool(node_name), session_id=session_id)
    raise ValueError(f"Unsupported ui_tree_resource_action action: {action!r}. Expected focus, click_button, activate_field, field_value_text, or field_info.")

@mcp.tool()
async def command_bar(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Low-level diagnostic: return active window/form command-bar data when available.

    Prefer command_interface_sections/commands/search for application navigation commands,
    and object_command_panel/object_context_menu for commands of a specific UI object.
    """
    return await _bridge_call_recorded(session_id, "command_bar", {})




def _ui_match_text(actual: Any, expected: str, *, substring: bool = False) -> bool:
    expected_norm = str(expected or "").strip().casefold()
    if not expected_norm:
        return True
    actual_norm = str(actual or "").strip().casefold()
    return expected_norm in actual_norm if substring else actual_norm == expected_norm


def _ui_object_matches_query(obj: dict[str, Any], name: str = "", title: str = "", type: str = "") -> bool:
    if not isinstance(obj, dict):
        return False
    if not _ui_match_text(obj.get("name"), name):
        return False
    # Runtime BSL search supports title containment for practical UI lookup; keep
    # Python-side post-filtering aligned so buggy/older managers cannot leak
    # unrelated rows into search_objects results.
    if not _ui_match_text(obj.get("title"), title, substring=True):
        return False
    if type:
        expected_type = str(type).replace(" ", "").strip().casefold()
        actual_type = str(obj.get("type") or "").replace(" ", "").strip().casefold()
        if expected_type not in actual_type and actual_type not in expected_type:
            return False
    return True


def _filter_search_objects_result(result: Any, name: str = "", title: str = "", type: str = "") -> Any:
    if not isinstance(result, dict) or not (name or title or type):
        return result
    objects = result.get("objects")
    if not isinstance(objects, list):
        return result
    filtered = [obj for obj in objects if _ui_object_matches_query(obj, name=name, title=title, type=type)]
    if len(filtered) == len(objects):
        return result
    result = dict(result)
    result["objects"] = filtered
    result["count"] = len(filtered)
    result["found"] = bool(filtered)
    result["post_filter_applied"] = True
    result["post_filter_before_count"] = len(objects)
    return result


@mcp.tool()
async def click_button(
    name: Annotated[str, "Button name or title text"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    watchdog_timeout: Annotated[int, "Deadline in seconds for button press before reporting blocked interface; 0 disables watchdog. Default is 120 seconds because many 1C managed forms open slowly; a timeout means 'inspect/retry', not proof that 1C hung."] = 120,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find a button in the active UI tree and press it."""
    params = {"name": name, "search_by_title": search_by_title}
    if int(watchdog_timeout) <= 0:
        return await _bridge_call_recorded(session_id, "click_button", params)
    return await _dangerous_bridge_call_recorded(
        session_id,
        "click_button",
        params,
        timeout=max(1, int(watchdog_timeout)),
        danger="click_button_may_start_long_ui_action",
    )


@mcp.tool()
async def find_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object visible title/caption"] = "",
    type: Annotated[str, "1C test object type, e.g. ТестируемаяФорма"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find an object in the active UI hierarchy by name, title, or test object type."""
    result = await _bridge_call_recorded(session_id, "find_object", {"name": name, "title": title, "type": type})
    if isinstance(result, dict) and result.get("found") and not _ui_object_matches_query(result.get("object") or {}, name=name, title=title, type=type):
        filtered = dict(result)
        filtered.update({"found": False, "post_filter_applied": True, "post_filter_reason": "manager_returned_non_matching_object"})
        return filtered
    return result


@mcp.tool()
async def search_objects(
    name: Annotated[str, "Object technical name, exact match when specified"] = "",
    title: Annotated[str, "Object visible title/caption, exact match when specified"] = "",
    type: Annotated[str, "1C test object type, e.g. ТестируемаяФорма"] = "",
    limit: Annotated[int, "Maximum number of matched objects to return"] = 50,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find all UI objects matching name, title and/or 1C test object type."""
    result = await _bridge_call_recorded(session_id, "search_objects", {"name": name, "title": title, "type": type, "limit": limit})
    return _filter_search_objects_result(result, name=name, title=title, type=type)


@mcp.tool()
async def get_object(
    name: Annotated[str, "Object name pattern"] = "",
    title: Annotated[str, "Object title pattern"] = "",
    type: Annotated[str, "1C tested object type name, optional"] = "",
    timeout: Annotated[int, "Search timeout in seconds"] = 0,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Low-level escape hatch: call TestedApplication.GetObject with optional wildcard name/title.

    Prefer find_object/search_objects for structured diagnostics and focus_object for changing focus.
    Keep this tool for platform/test-client edge cases where direct GetObject semantics matter.
    """
    return await _bridge_call_recorded(session_id, "get_object", {"name": name, "title": title, "type": type, "timeout": timeout})


@mcp.tool()
async def focus_object(
    name: Annotated[str, "Object technical name"] = "",
    title: Annotated[str, "Object visible title/caption"] = "",
    type: Annotated[str, "1C test object type"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move focus to a UI object in the active test client window.

    This tool only changes focus/activation; it does not change values. For radio
    buttons and dropdowns use set_field_value with the visible caption. To switch
    to an inactive page, focus the page object first, then interact with its
    children.
    """
    return await _bridge_call_recorded(session_id, "focus_object", {"name": name, "title": title, "type": type})


@mcp.tool()
async def convert_1c_guid_navigation_link(
    value: Annotated[
        str,
        "1C GUID/UUID, 32-character e1cib ref, or e1cib/data/... navigation link",
    ],
    metadata_name: Annotated[
        str,
        "Metadata full name for building e1cib/data links, e.g. Справочник.Номенклатура",
    ] = "",
    direction: Annotated[
        str,
        "Conversion direction: auto, guid_to_ref, ref_to_guid, guid_to_navigation, navigation_to_guid",
    ] = "auto",
) -> dict[str, Any]:
    """Convert 1C object reference GUIDs to e1cib/data navigation links and back.

    The pure 1C algorithm is the same as used by platform navigation refs:
    `ref = UUID groups 4 + 5 + 3 + 2 + 1`, all lowercase and without hyphens.
    For example, `c3314ae6-6899-11e8-8780-b06ebf2faaf6` becomes
    `8780b06ebf2faaf611e86899c3314ae6` and can be embedded as
    `e1cib/data/Справочник.ВидыНоменклатуры?ref=8780...`.
    """
    return _convert_1c_guid_navigation_link(
        value,
        metadata_name=metadata_name,
        direction=direction,
    )


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
        suggestions = await _navigation_link_suggestions(session_id, navigation_link)
        raise ValueError(
            f"open_navigation_link accepts only explicit e1cib/... navigation links; rejected: {navigation_link}\n"
            "Use rag_lookup_object/rag_query or metadata_objects_from_catalog to find correct metadata names."
            + _format_navigation_suggestions(suggestions)
        )
    try:
        return await _bridge_call_recorded(session_id, "open_navigation_link", {"navigation_link": navigation_link})
    except Exception as exc:
        suggestions = await _navigation_link_suggestions(session_id, navigation_link)
        raise RuntimeError(str(exc) + _format_navigation_suggestions(suggestions)) from exc


def _query_from_navigation_link(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("e1cib/"):
        text = text.split("?", 1)[0].rstrip("/")
        if "/" in text:
            text = text.rsplit("/", 1)[-1]
    return text


def _format_navigation_suggestions(suggestions: list[dict[str, str]]) -> str:
    if not suggestions:
        return ""
    lines = ["", "Возможно, вы имели в виду:"]
    for item in suggestions[:5]:
        full_name = item.get("full_name") or item.get("object_full_name") or item.get("presentation") or ""
        navigation = item.get("navigation_link") or (f"e1cib/list/{full_name}" if full_name else "")
        source = item.get("source", "")
        suffix = f" ({source})" if source else ""
        if navigation:
            lines.append(f"- {full_name} → {navigation}{suffix}")
        else:
            lines.append(f"- {full_name}{suffix}")
    return "\n" + "\n".join(lines)


async def _navigation_link_suggestions(session_id: str, navigation_link: str, limit: int = 5) -> list[dict[str, str]]:
    query = _query_from_navigation_link(navigation_link)
    if not query:
        return []
    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(full_name: str, source: str, navigation: str = "", presentation: str = "") -> None:
        full_name = str(full_name or "").strip()
        if not full_name or full_name in seen:
            return
        seen.add(full_name)
        suggestions.append({
            "full_name": full_name,
            "presentation": presentation or full_name,
            "navigation_link": navigation or f"e1cib/list/{full_name}",
            "source": source,
        })

    sess = _session(session_id)
    if sess.rag_enabled:
        try:
            effective_snapshot = sess.rag_snapshot or None
            lookup = _RAG.lookup_object(object=query, snapshot=effective_snapshot)
            for item in lookup.get("matches", [])[:limit]:
                nav_items = item.get("navigation") or []
                nav = ""
                if nav_items:
                    nav = str(nav_items[0].get("url") or "")
                add(str(item.get("full_name") or ""), "rag_lookup_object", nav)
        except Exception:
            pass
        if len(suggestions) < limit:
            try:
                result = _RAG.query(text=query, snapshot=sess.rag_snapshot or None, limit=limit)
                for item in (result.get("terms") or []) + (result.get("fts") or []):
                    add(str(item.get("full_name") or item.get("object_full_name") or ""), "rag_query")
                    if len(suggestions) >= limit:
                        break
            except Exception:
                pass

    if len(suggestions) < limit:
        try:
            cache_key = _metadata_cache_key(session_id)
            catalog_data = _METADATA_OBJECTS_CACHE.get(cache_key)
            if catalog_data is None:
                catalog_data = await _load_metadata_objects_from_catalog(session_id, 10000)
                _METADATA_OBJECTS_CACHE[cache_key] = catalog_data
            for item in catalog_data.get("objects", []):
                if item.get("is_group") == "Да":
                    continue
                if _match_metadata_object(item, query):
                    add(str(item.get("full_name") or ""), "metadata_objects_from_catalog", presentation=str(item.get("presentation") or ""))
                    if len(suggestions) >= limit:
                        break
        except Exception:
            pass
    return suggestions[:limit]


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
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return whether a field dropdown is open and its visible choice items."""
    return await _bridge_call_recorded(
        session_id,
        "field_dropdown_state",
        {"field_name": field_name, "search_by_title": search_by_title, "timeout": timeout},
    )


@mcp.tool()
async def field_dropdown_close(
    field_name: Annotated[str, "Field technical name or title"],
    search_by_title: Annotated[bool, "Search field by visible title"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Close an open dropdown for a form field, if it is open."""
    return await _bridge_call_recorded(
        session_id,
        "field_dropdown_close",
        {"field_name": field_name, "search_by_title": search_by_title},
    )


@mcp.tool()
async def table_current_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return current row of a tested table as key/value text mapping."""
    return await _bridge_call_recorded(session_id, "table_current_row", {"name": name, "title": title})


@mcp.tool()
async def table_selected_rows(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return selected rows of a tested table as key/value text mappings."""
    return await _bridge_call_recorded(session_id, "table_selected_rows", {"name": name, "title": title})


@mcp.tool()
async def table_cell_text(
    cell: Annotated[str, "Column name or index; empty returns current cell"] = "",
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return cell text from a tested table via GetCellText()."""
    return await _bridge_call_recorded(session_id, "table_cell_text", {"name": name, "title": title, "cell": cell})


@mcp.tool()
async def tabular_documents(
    include_text: Annotated[bool, "Include tabular document cells"] = False,
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
    return await _bridge_call_recorded(session_id, "tabular_documents", {"include_text": include_text, "row_start": row_start, "row_end": row_end, "column_start": column_start, "column_end": column_end, "max_rows": max_rows, "max_columns": max_columns})


@mcp.tool()
async def tabular_document_text(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    row_start: Annotated[int, "First row to read (1-based)"] = 1,
    row_end: Annotated[int, "Last row to read; 0 = row_start + max_rows - 1"] = 0,
    column_start: Annotated[int, "First column to read (1-based)"] = 1,
    column_end: Annotated[int, "Last column to read; 0 = column_start + max_columns - 1"] = 0,
    max_rows: Annotated[int, "Maximum rows to read when row_end=0; 0 = all rows"] = 50,
    max_columns: Annotated[int, "Maximum columns to read when column_end=0; 0 = all columns"] = 20,
    compact: Annotated[bool | None, "Return TSV text instead of nested cells; default follows ONEC_MCP_RESULT_VERBOSITY"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return dimensions and a cells range of a tabular document field in the active window.

    Use this for small tables or partial reads (headers, totals, spot checks). For
    large reports/tabular documents, prefer tabular_document_save/report export
    and analyze the saved PDF/XLSX/TXT/HTML file instead of reading UI cells one
    by one.
    """
    result = await _bridge_call_recorded(session_id, "tabular_document_text", {"name": name, "title": title, "row_start": row_start, "row_end": row_end, "column_start": column_start, "column_end": column_end, "max_rows": max_rows, "max_columns": max_columns})
    return _compact_tabular_text_result(result, include_raw=not _compact_enabled(compact))


@mcp.tool()
async def tabular_document_save(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    path: Annotated[str, "Output file path; empty = auto-generated temp path"] = "",
    format: Annotated[str, "Output format: pdf | xlsx | xls | html | txt | mxl"] = "pdf",
    exact_filter_only: Annotated[bool, "Deprecated: use only a trusted file-dialog filter instead of probing many filters; unstable formats ignore it"] = False,
    watchdog_timeout: Annotated[int, "Deadline in seconds for dangerous UI save action before reporting blocked interface; 0 disables watchdog. Default is 120 seconds because thin-client file dialogs and large tabular documents can be slow."] = 120,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Save a tabular document field to a file in the selected format."""
    params = {
        "name": name,
        "title": title,
        "path": path,
        "format": format,
        "exact_filter_only": exact_filter_only,
    }
    if exact_filter_only:
        return await _tabular_document_save_filter_by_filter(
            session_id,
            params,
            format,
            timeout=max(1, int(watchdog_timeout)),
        )
    result = await _dangerous_bridge_call_recorded(
        session_id,
        "tabular_document_save",
        params,
        timeout=max(1, int(watchdog_timeout)),
        danger="tabular_document_file_save_may_block_thin_client",
    )
    if isinstance(result, dict) and result.get("interface_blocked"):
        return result
    return _select_saved_tabular_document_file(result, format)


async def _tabular_document_save_filter_by_filter(
    session_id: str,
    params: dict[str, Any],
    format: str,
    *,
    timeout: int,
    danger: str = "tabular_document_file_save_may_block_thin_client",
    max_filter_index: int = 30,
) -> Any:
    """Try file-dialog filters one bridge call at a time until a valid file appears."""
    attempts: list[Any] = []
    filter_indexes = _tabular_document_save_filter_order(format, max_filter_index=max_filter_index)
    for filter_index in filter_indexes:
        attempt_params = dict(params)
        attempt_params["exact_filter_only"] = True
        attempt_params["file_dialog_filter_index"] = filter_index
        result = await _dangerous_bridge_call_recorded(
            session_id,
            "tabular_document_save",
            attempt_params,
            timeout=timeout,
            danger=danger,
        )
        if isinstance(result, dict) and result.get("interface_blocked"):
            result["attempts"] = attempts
            result["last_filter_index"] = filter_index
            return result
        selected = _select_saved_tabular_document_file(result, format)
        attempts.append(dict(selected) if isinstance(selected, dict) else selected)
        if isinstance(selected, dict) and selected.get("saved") is True:
            final_result = dict(selected)
            final_result["attempts"] = attempts
            final_result["attempted_filter_indexes"] = filter_indexes[: len(attempts)]
            final_result["fallback_used"] = len(attempts) > 1
            final_result["method"] = "write_content_to_file_dialog_filters_one_by_one"
            return final_result
    return {
        "saved": False,
        "path": params.get("path", ""),
        "format": format,
        "method": "write_content_to_file_dialog_filters_one_by_one",
        "attempts": attempts,
        "attempted_filter_indexes": filter_indexes,
        "error": f"No produced file matches requested tabular document format after {max_filter_index} file-dialog filters: {format.lower()}",
    }


def _tabular_document_save_filter_order(format: str, *, max_filter_index: int = 30) -> list[int]:
    """Return configured likely file-dialog filter indexes first, then the rest."""
    wanted = format.strip().lower()
    seen: set[int] = set()
    ordered: list[int] = []
    for index in [*TABULAR_DOCUMENT_SAVE_PREFERRED_FILTER_INDEXES.get(wanted, []), *range(1, max_filter_index + 1)]:
        if 1 <= index <= max_filter_index and index not in seen:
            ordered.append(index)
            seen.add(index)
    return ordered


@mcp.tool()
async def tabular_document_state_display(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return the state display of a tabular document field, when exposed by 1C."""
    return await _bridge_call_recorded(session_id, "tabular_document_state_display", {"name": name, "title": title})


def _select_saved_tabular_document_file(result: Any, format: str) -> Any:
    if not isinstance(result, dict):
        return result
    files = result.get("files")
    if not isinstance(files, list) or not files:
        if result.get("found", True) is not False:
            result["saved"] = False
            result.setdefault("error", f"No files were produced for requested tabular document format: {format.lower()}")
        return result
    selected = _find_saved_tabular_document_file_with_retry(files, format)
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


def _find_saved_tabular_document_file_with_retry(files: list[Any], format: str, *, timeout: float = 2.0, interval: float = 0.05) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while True:
        selected = _find_saved_tabular_document_file(files, format)
        if selected is not None:
            return selected
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


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
        if wanted in {"html", "htm"} and data.startswith((b"<", b"\xef\xbb\xbf<")):
            return item
        if wanted == "txt" and _looks_like_plain_text(data):
            return item
        if wanted == "mxl" and _looks_like_mxl(data):
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


def _looks_like_mxl(data: bytes) -> bool:
    return data.startswith(b"MOXCEL") or data.lstrip(b"\xef\xbb\xbf").startswith(b"{8,")



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
    create_if_missing: Annotated[bool, "Allow choosing dropdown items like 'Create: ...' for missing reference values"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a value in a form field by name or title."""
    return await _bridge_call_recorded(session_id, "set_field_value", {
        "field_name": field_name,
        "value": value,
        "search_by_title": search_by_title,
        "create_if_missing": create_if_missing,
    })


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
    compact: Annotated[bool | None, "Return compact row values; default follows ONEC_MCP_RESULT_VERBOSITY"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return rows of a tested table as key/value mappings."""
    result = await _bridge_call_recorded(session_id, "table_rows", {"name": name, "title": title, "limit": limit})
    return _compact_rows_result(result, include_raw=not _compact_enabled(compact))


def _table_row_pairs(row: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(row, dict):
        return result
    values = row.get("values")
    if isinstance(values, dict):
        for key, value in values.items():
            result[str(key)] = str(value)
    pairs = row.get("pairs", [])
    if isinstance(pairs, list):
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            key = str(pair.get("key", ""))
            value = str(pair.get("value", ""))
            result[key] = value
    for key, value in list(result.items()):
        normalized_key = re.sub(r"\s+", " ", key).strip()
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


def _is_metadata_full_name_included(full_name: str) -> bool:
    normalized = (full_name or "").strip().casefold()
    if not normalized:
        return False
    return not normalized.startswith(("роль.", "подсистема."))


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
        if not line or line.startswith(("EF_", "#")):
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
    presentation = _row_value(row, "Ссылка", "Наименование", "Идентификатор объекта метаданных", "ИдентификаторОбъектаМетаданных")
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


def _metadata_objects_catalog_self_entry() -> dict[str, str]:
    return {
        "presentation": "Справочник.ИдентификаторыОбъектовМетаданных",
        "full_name": "Справочник.ИдентификаторыОбъектовМетаданных",
        "type": "Справочник",
        "name": "ИдентификаторыОбъектовМетаданных",
        "synonym": "Идентификаторы объектов метаданных",
        "is_group": "Нет",
    }


def _tabular_state_text(state_result: Any) -> str:
    if not isinstance(state_result, dict):
        return ""
    state_display = state_result.get("state_display")
    if not isinstance(state_display, dict):
        return ""
    return " ".join(
        str(state_display.get(key) or "")
        for key in ("display_variant", "additional_show_mode", "command_name", "text")
    ).strip()


def _tabular_state_looks_busy(state_result: Any) -> bool:
    text = _tabular_state_text(state_result).casefold()
    if not text:
        return False
    busy_markers = (
        "форм",
        "ожидан",
        "загруз",
        "подготов",
        "processing",
        "loading",
        "wait",
    )
    return any(marker in text for marker in busy_markers)


def _tabular_documents_dimensions(result: Any, name: str) -> tuple[int, int]:
    if not isinstance(result, dict):
        return 0, 0
    items = result.get("items") if isinstance(result.get("items"), list) else None
    if items is None:
        tabular_documents = result.get("tabular_documents")
        if isinstance(tabular_documents, dict):
            items = tabular_documents.get("items") if isinstance(tabular_documents.get("items"), list) else None
    if not items:
        return 0, 0
    selected = None
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            selected = item
            break
    if selected is None:
        selected = next((item for item in items if isinstance(item, dict)), None)
    if not isinstance(selected, dict):
        return 0, 0
    return int(selected.get("rows") or 0), int(selected.get("columns") or 0)


async def _wait_tabular_document_ready(
    session_id: str,
    name: str,
    *,
    timeout_seconds: float = 75.0,
    interval_seconds: float = 3.0,
) -> tuple[bool, list[str], dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    warnings: list[str] = []
    diagnostics: dict[str, Any] = {"attempts": 0, "rows": 0, "columns": 0, "state_text": ""}
    last_state_error = ""
    last_dimensions_error = ""
    while time.monotonic() < deadline:
        diagnostics["attempts"] = int(diagnostics.get("attempts") or 0) + 1
        state_result: Any = None
        try:
            state_result = await _bridge_call_recorded(
                session_id,
                "tabular_document_state_display",
                {"name": name},
                timeout=8,
            )
            diagnostics["state_display"] = state_result
            diagnostics["state_text"] = _tabular_state_text(state_result)
        except Exception as exc:
            last_state_error = str(exc)
            diagnostics["state_error"] = last_state_error
        busy = _tabular_state_looks_busy(state_result)
        if not busy:
            try:
                docs_result = await _bridge_call_recorded(
                    session_id,
                    "tabular_documents",
                    {"include_text": False},
                    timeout=10,
                )
                rows, columns = _tabular_documents_dimensions(docs_result, name)
                diagnostics["rows"] = rows
                diagnostics["columns"] = columns
                if rows > 0 and columns > 0:
                    return True, warnings, diagnostics
            except Exception as exc:
                last_dimensions_error = str(exc)
                diagnostics["dimensions_error"] = last_dimensions_error
        await asyncio.sleep(interval_seconds)
    if last_state_error:
        warnings.append(f"Last tabular document state-display error: {last_state_error}")
    if last_dimensions_error:
        warnings.append(f"Last tabular document dimensions error: {last_dimensions_error}")
    warnings.append(
        "Tabular document did not become ready"
        f" after {diagnostics.get('attempts')} attempt(s);"
        f" rows={diagnostics.get('rows')}; columns={diagnostics.get('columns')};"
        f" state={diagnostics.get('state_text')!r}"
    )
    return False, warnings, diagnostics


async def _metadata_rows_from_tabular_document_chunks(
    session_id: str,
    name: str,
    *,
    max_rows: int,
    total_rows: int,
    total_columns: int,
    query: str = "",
    target_matches: int = 0,
    chunk_size: int = 1000,
) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    cells: list[list[Any]] = []
    rows_to_read = min(max(total_rows, 0), max_rows + 20)
    # Universal report metadata rows in live BP expose useful fields as:
    # 2=Имя, 3=Синоним, 4=Полное имя, 5=Полный синоним,
    # 6=Без данных/Пометка удаления.  Reading the whole 9+ column report makes
    # full-cache builds exceed common MCP request deadlines, so keep only the
    # contiguous technical/synonym/delete-mark range needed for normalization.
    column_start = 2 if total_columns >= 6 else 1
    column_end = min(max(total_columns, 0), 6 if total_columns >= 6 else 20)
    columns_to_read = max(column_end - column_start + 1, 0)
    if rows_to_read <= 0 or columns_to_read <= 0:
        return [], ["Universal report chunk read skipped because tabular document dimensions are empty"]
    parsed_rows: list[dict[str, str]] = []
    stop_by_query = bool(query and target_matches > 0)
    for row_start in range(1, rows_to_read + 1, chunk_size):
        row_end = min(rows_to_read, row_start + chunk_size - 1)
        try:
            chunk = await _bridge_call_recorded(session_id, "tabular_document_text", {
                "name": name,
                "row_start": row_start,
                "row_end": row_end,
                "column_start": column_start,
                "column_end": column_end,
                "max_rows": chunk_size,
                "max_columns": columns_to_read,
            }, timeout=45)
        except Exception as exc:
            warnings.append(f"Universal report chunk read failed at rows {row_start}-{row_end}: {exc}")
            break
        chunk_cells = chunk.get("cells", []) if isinstance(chunk, dict) else []
        if not chunk_cells:
            warnings.append(f"Universal report chunk read returned no cells at rows {row_start}-{row_end}")
            break
        cells.extend(chunk_cells)
        parsed_rows = _metadata_catalog_rows_from_tabular_cells(cells)
        if stop_by_query:
            objects = [_metadata_object_from_row(row) for row in parsed_rows if _is_not_deleted_catalog_row(row)]
            matches = [item for item in objects if _match_metadata_object(item, query)]
            if len(matches) >= target_matches:
                warnings.append(
                    "Universal report chunk read stopped after reaching requested matches"
                    f"; matches={len(matches)}; last_row={row_end}"
                )
                break
    if not parsed_rows:
        parsed_rows = _metadata_catalog_rows_from_tabular_cells(cells)
    if parsed_rows:
        warnings.append(
            "Universal report tabular document chunks became parseable"
            f"; chunks_cells={len(cells)}; rows_to_read={rows_to_read};"
            f" columns={columns_to_read}; column_range={column_start}-{column_end}"
        )
    return parsed_rows[:max_rows], warnings


def _metadata_universal_report_filter_specs(query: str = "") -> list[dict[str, str]]:
    """Return report-level filters for a full metadata identifiers snapshot.

    The cache must be built from one Universal report run, not from per-query
    report/probe calls.  Keep the report small enough by excluding service-heavy
    metadata classes and deleted rows before the tabular document is generated.
    Public `query` is intentionally ignored here: search happens later against
    the cached full snapshot.
    """
    del query
    return [
        {"field": "Пометка удаления", "comparison": "Равно", "value": "Нет"},
        {"field": "Полное имя", "comparison": "Не начинается с", "value": "Роль."},
        {"field": "Полное имя", "comparison": "Не начинается с", "value": "Подсистема."},
    ]


async def _add_universal_report_filter(
    session_id: str,
    spec: dict[str, str],
    warnings: list[str],
    field_occurrence: int = 1,
) -> bool:
    field = spec.get("field", "")
    comparison = spec.get("comparison", "Равно")
    value = spec.get("value", "")
    if not field:
        return False
    try:
        rows_result = await _bridge_call_recorded(session_id, "table_rows", {"name": "Отборы", "limit": 50}, timeout=10)
        rows = rows_result.get("rows", []) if isinstance(rows_result, dict) else []
        for row in rows:
            row_pairs = _table_row_pairs(row)
            if _row_value(row_pairs, "Поле") == field and _row_value(row_pairs, "Значение") == value:
                return True
    except Exception as exc:
        warnings.append(f"Universal report existing filters inspection failed for {field}: {exc}")
    try:
        await _bridge_call_recorded(session_id, "group_activate_page", {
            "name": "СтраницыНастроек",
            "page_name": "СтраницаОтборы",
            "page_title": "Фильтры",
        }, timeout=15)
    except Exception as exc:
        warnings.append(f"Universal report filters page activation failed before adding {field}: {exc}")
    try:
        await _bridge_call_recorded(session_id, "click_button", {"name": "Отборы_Добавить", "watchdog_timeout": 8}, timeout=12)
    except Exception:
        try:
            await _bridge_call_recorded(session_id, "click_button", {"name": "Добавить", "search_by_title": True, "watchdog_timeout": 8}, timeout=12)
        except Exception as exc:
            warnings.append(f"Universal report filter add button failed for {field}: {exc}")
            return False
    try:
        await _bridge_call_recorded(session_id, "set_field_value", {
            "field_name": "СтрокаПоискаДоступныеПоля",
            "value": field,
        }, timeout=10)
        await _bridge_call_recorded(session_id, "choose_current_row", {"name": "ДоступныеПоля"}, timeout=15)
    except Exception as exc:
        warnings.append(f"Universal report filter {field!r} could not be selected; continuing without report-level filter: {exc}")
        try:
            active = await _bridge_call_recorded(session_id, "active_window", {}, timeout=5)
            if isinstance(active, dict) and active.get("title") == "Выбор поля отчета":
                await _bridge_call_recorded(session_id, "close_form", {}, timeout=10)
        except Exception:
            pass
        return False
    try:
        await _bridge_call_recorded(session_id, "table_goto_row", {
            "name": "Отборы",
            "text": field,
            "limit": 50,
            "occurrence": field_occurrence,
        }, timeout=10)
    except Exception as exc:
        warnings.append(f"Universal report filter row positioning failed for {field}: {exc}")
    if comparison:
        try:
            await _bridge_call_recorded(session_id, "table_set_field", {
                "table": "Отборы",
                "field": "Условие",
                "value": comparison,
                "search_by_title": True,
            }, timeout=15)
        except Exception as exc:
            warnings.append(f"Universal report filter comparison setup failed for {field}: {exc}")
    if value:
        try:
            await _bridge_call_recorded(session_id, "table_goto_row", {
                "name": "Отборы",
                "text": field,
                "limit": 50,
                "occurrence": field_occurrence,
            }, timeout=10)
        except Exception:
            pass
        try:
            await _bridge_call_recorded(session_id, "table_set_field", {
                "table": "Отборы",
                "field": "Значение",
                "value": value,
                "search_by_title": True,
            }, timeout=15)
        except Exception as exc:
            warnings.append(f"Universal report filter value setup failed for {field}: {exc}")
    return True


async def _close_message_box_from_command_diagnostics(
    session_id: str,
    command_result: Any,
    warnings: list[str],
) -> bool:
    """Close a safe MessageBox reported in command diagnostics without probing active_window."""
    diagnostics = command_result.get("client_diagnostics") if isinstance(command_result, dict) else None
    if not isinstance(diagnostics, dict):
        return False
    modal_form = str(diagnostics.get("modal_form_name") or "")
    has_dialog = bool(diagnostics.get("has_dialog")) or modal_form == "MessageBox"
    is_error_window = bool(diagnostics.get("modal_is_error_window"))
    buttons = diagnostics.get("modal_buttons") if isinstance(diagnostics.get("modal_buttons"), list) else []
    has_ok = any(
        isinstance(button, dict)
        and (str(button.get("title") or "").strip().casefold() == "ok" or str(button.get("name") or "").strip().casefold() == "ok")
        for button in buttons
    )
    if not has_dialog or is_error_window or not has_ok:
        return False
    try:
        await _bridge_call_recorded(
            session_id,
            "click_button",
            {"name": "OK", "search_by_title": True, "watchdog_timeout": 30},
            timeout=35,
        )
        warnings.append("Universal report informational MessageBox was closed before forming the report")
        return True
    except Exception as exc:
        warnings.append(f"Universal report informational MessageBox could not be closed from command diagnostics: {exc}")
        return False


async def _close_universal_report_message_box_if_present(
    session_id: str,
    warnings: list[str],
    *,
    wait_seconds: float = 30.0,
) -> bool:
    """Wait for Universal report UI to become responsive and close a safe MessageBox if it appears."""
    deadline = time.time() + max(0.0, wait_seconds)
    last_error = ""
    while True:
        try:
            active = await _bridge_call_recorded(session_id, "active_window", {}, timeout=5)
        except Exception as exc:
            last_error = str(exc)
            if time.time() >= deadline:
                warnings.append(f"Universal report active-window check before forming failed: {last_error}")
                return False
            await asyncio.sleep(2)
            continue
        if not isinstance(active, dict):
            return False
        form_name = str(active.get("form_name") or "")
        title = str(active.get("title") or "")
        diagnostics = active.get("client_diagnostics") if isinstance(active.get("client_diagnostics"), dict) else {}
        modal_form = str(diagnostics.get("modal_form_name") or form_name)
        if form_name == "MessageBox" or modal_form == "MessageBox":
            try:
                await _bridge_call_recorded(session_id, "click_button", {"name": "OK", "watchdog_timeout": 30}, timeout=35)
                warnings.append("Universal report informational MessageBox was closed before forming the report")
                return True
            except Exception as exc:
                last_error = str(exc)
                if time.time() >= deadline:
                    warnings.append(f"Universal report informational MessageBox could not be closed automatically: {last_error}")
                    return False
                await asyncio.sleep(2)
                continue
        if "Универсальный отчет" in title or form_name == "ОбщаяФорма.ФормаОтчета":
            return False
        # Some start-page/advertising forms can be active immediately before the
        # navigation completes.  Wait a little instead of touching quick settings
        # on the wrong form.
        if time.time() >= deadline:
            return False
        await asyncio.sleep(1)




async def _add_universal_report_selected_field(session_id: str, field: str, warnings: list[str]) -> bool:
    try:
        await _bridge_call_recorded(session_id, "group_activate_page", {
            "name": "СтраницыНастроек",
            "page_name": "СтраницаВыбранныеПоляИСортировки",
            "page_title": "Поля и сортировки",
        }, timeout=15)
    except Exception as exc:
        warnings.append(f"Universal report selected-fields page activation failed before adding {field}: {exc}")
    try:
        await _bridge_call_recorded(session_id, "click_button", {"name": "ВыбранныеПоля_Добавить"}, timeout=25)
    except Exception:
        await _bridge_call_recorded(session_id, "click_button", {"name": "Добавить", "search_by_title": True}, timeout=25)
    try:
        await _bridge_call_recorded(session_id, "set_field_value", {
            "field_name": "СтрокаПоискаДоступныеПоля",
            "value": field,
        }, timeout=10)
        await _bridge_call_recorded(session_id, "choose_current_row", {"name": "ДоступныеПоля"}, timeout=15)
        return True
    except Exception as exc:
        warnings.append(f"Universal report selected field {field!r} could not be added: {exc}")
        return False


async def _set_universal_report_quick_setting_by_titles(
    session_id: str,
    titles: list[str],
    value: str,
    warnings: list[str],
) -> bool:
    local_warnings: list[str] = []
    for title in titles:
        try:
            info = await _bridge_call_recorded(session_id, "field_info", {
                "field_name": title,
                "search_by_title": True,
            }, timeout=10)
            if not isinstance(info, dict) or not info.get("found"):
                raise RuntimeError(f"Поле не найдено по заголовку: {title}")
            current = str(info.get("data_text") or "").strip()
            if current == value or (value and value in current):
                return True
            field_name = str(info.get("name") or title)
            await _bridge_call_recorded(session_id, "set_field_value", {
                "field_name": field_name,
                "value": value,
            }, timeout=15)
            return True
        except Exception as exc:
            local_warnings.append(f"Universal report quick setting {title!r} was not set by title: {exc}")
    warnings.extend(local_warnings)
    return False


async def _configure_universal_report_metadata_settings(session_id: str, query: str = "") -> list[str]:
    del query
    warnings: list[str] = []
    await _bridge_call_recorded(session_id, "click_button", {"name": "ВсеНастройки"}, timeout=30)
    try:
        await _bridge_call_recorded(session_id, "group_activate_page", {
            "name": "СтраницыНастроек",
            "page_name": "СтраницаВыбранныеПоляИСортировки",
            "page_title": "Поля и сортировки",
        }, timeout=15)
    except Exception as exc:
        warnings.append(f"Universal report selected-fields page activation failed: {exc}")
    selected_fields = []
    try:
        rows_result = await _bridge_call_recorded(session_id, "table_rows", {"name": "ВыбранныеПоля", "limit": 80}, timeout=10)
        rows = rows_result.get("rows", []) if isinstance(rows_result, dict) else []
        for row in rows:
            row_pairs = _table_row_pairs(row)
            field = _row_value(row_pairs, "Поле")
            use = _row_value(row_pairs, "Использование")
            if field and field != "Поля":
                selected_fields.append((field, use))
    except Exception as exc:
        warnings.append(f"Universal report selected fields inspection failed: {exc}")
    required = {"Полное имя", "Синоним"}
    selected_names = {field for field, _use in selected_fields}
    enabled_names = {field for field, use in selected_fields if _is_truthy_1c(use)}
    missing = sorted(required - selected_names)
    disabled = sorted(required - enabled_names)
    if missing:
        warnings.append(
            "Universal report selected fields were not detected in selected-fields table;"
            f" automatic modal add skipped: {', '.join(missing)}"
        )
    if disabled:
        warnings.append(
            "Universal report selected fields are not enabled in UI settings;"
            f" leaving report settings unchanged and relying on parse/post-filtering: {', '.join(disabled)}"
        )
    filter_occurrences: dict[str, int] = {}
    for spec in _metadata_universal_report_filter_specs():
        field = spec.get("field", "")
        filter_occurrences[field] = filter_occurrences.get(field, 0) + 1
        await _add_universal_report_filter(session_id, spec, warnings, filter_occurrences[field])
    await _bridge_call_recorded(session_id, "click_button", {"name": "ЗакрытьИСформировать"}, timeout=35)
    return warnings


async def _metadata_rows_from_universal_report(
    session_id: str,
    max_rows: int,
    *,
    query: str = "",
    target_matches: int = 0,
) -> tuple[list[dict[str, str]], list[str]]:
    """Read metadata identifiers through the standard Universal report.

    The metadata-identifiers catalog list usually shows only its `Ссылка`
    column.  The Universal report for the same catalog exposes technical fields
    such as `Имя` and `Полное имя`, which are needed for precise navigation
    hints without synonym-based guessing.
    """
    warnings: list[str] = []
    navigation_link = "e1cib/app/Отчет.УниверсальныйОтчет"
    try:
        open_result = await _bridge_call_recorded(session_id, "open_navigation_link", {"navigation_link": navigation_link})
        await _close_message_box_from_command_diagnostics(session_id, open_result, warnings)
        # Do not probe or mutate Universal report quick settings in the primary path.
        # On some live web/thin-client sessions the report opens already on the
        # required metadata-identifiers source, but title-based `field_info`
        # probing of quick settings makes the bridge unresponsive before the
        # report can be formed.  Prefer the stable navigation-link source and
        # validate the produced XLSX contents after save.
        warnings.append(
            "Universal report quick settings probing skipped;"
            " using the source opened by the navigation link and validating XLSX rows"
        )
        # Do not open the Universal report settings form in the primary path.
        # Live thin-client/web sessions can visually open `ВсеНастройки` but
        # leave the test bridge unresponsive for minutes; adding the first
        # filter field can also end in `Потеряно соединение`.  The primary path
        # must therefore form the report with the selected source and apply the
        # mandatory filtering/normalization on the Python side.  Settings remain
        # available as a separate best-effort diagnostic helper, not as a
        # correctness-critical step of metadata cache building.
        warnings.append("Universal report settings UI skipped; filtering is applied after reading rows")
        # Avoid active_window probes before forming the report: on live web/thin
        # sessions they can make the bridge unresponsive even when the report
        # form is visually ready.  If navigation returned no safe MessageBox
        # diagnostics, go straight to the form command.
        form_result = await _dangerous_bridge_call_recorded(
            session_id,
            "click_button",
            {"name": "СформироватьОтчет"},
            timeout=180,
            danger="metadata_objects_universal_report_form_may_block_thin_client",
        )
        if isinstance(form_result, dict) and form_result.get("started_long_action"):
            warnings.append(
                "Universal report form action is still running or blocked the interface;"
                f" form_result={_truncate_value(form_result)}"
            )
            return [], warnings
        ready, readiness_warnings, readiness = await _wait_tabular_document_ready(
            session_id,
            "ОтчетТабличныйДокумент",
            timeout_seconds=75,
            interval_seconds=3,
        )
        warnings.extend(readiness_warnings)
        if not ready:
            return [], [
                *warnings,
                (
                    "Universal report tabular document was not ready for XLSX save"
                    f"; readiness={_truncate_value(readiness)}"
                ),
            ]
        rows: list[dict[str, str]] = []
        read_warnings: list[str] = []
        last_saved_path = ""
        save_diagnostics: Any = {}
        mxl_path = Path("/tmp/answer42") / f"metadata_objects_{session_id}_{uuid.uuid4().hex}.mxl"
        try:
            selected_result = await _tabular_document_save_filter_by_filter(
                session_id,
                {
                    "name": "ОтчетТабличныйДокумент",
                    "path": str(mxl_path),
                    "format": "mxl",
                    "exact_filter_only": True,
                },
                "mxl",
                timeout=120,
                danger="metadata_objects_universal_report_file_save_may_block_thin_client",
            )
            if isinstance(selected_result, dict) and selected_result.get("interface_blocked"):
                read_warnings.append(
                    "Universal report MXL save is still running or blocked the interface;"
                    f" save_result={_truncate_value(selected_result)}"
                )
                return [], [*warnings, *read_warnings]
            save_diagnostics = selected_result
            selected = selected_result.get("selected") if isinstance(selected_result, dict) else None
            selected_path = selected.get("copied_to") or selected.get("path") if isinstance(selected, dict) else ""
            if selected_path:
                last_saved_path = str(selected_path)
                rows = _metadata_catalog_rows_from_mxl(Path(selected_path))
        except Exception as exc:
            read_warnings.append(f"Universal report MXL save/read failed: {exc}")
        if rows:
            warnings.extend(read_warnings)
            warnings.append(
                "Universal report MXL became parseable"
                f"; rows={readiness.get('rows')}; columns={readiness.get('columns')}; path={last_saved_path}"
            )
            return rows[:max_rows], warnings

        report_path = Path("/tmp/answer42") / f"metadata_objects_{session_id}_{uuid.uuid4().hex}.xlsx"
        try:
            selected_result = await _tabular_document_save_filter_by_filter(
                session_id,
                {
                    "name": "ОтчетТабличныйДокумент",
                    "path": str(report_path),
                    "format": "xlsx",
                    "exact_filter_only": True,
                },
                "xlsx",
                timeout=180,
                danger="metadata_objects_universal_report_file_save_may_block_thin_client",
            )
            if isinstance(selected_result, dict) and selected_result.get("interface_blocked"):
                read_warnings.append(
                    "Universal report XLSX save is still running or blocked the interface;"
                    f" save_result={_truncate_value(selected_result)}"
                )
                return [], [*warnings, *read_warnings]
            save_diagnostics = selected_result
            selected = selected_result.get("selected") if isinstance(selected_result, dict) else None
            selected_path = selected.get("copied_to") or selected.get("path") if isinstance(selected, dict) else ""
            if selected_path:
                last_saved_path = str(selected_path)
                rows = _metadata_catalog_rows_from_xlsx(Path(selected_path))
        except Exception as exc:
            read_warnings.append(f"Universal report XLSX save/read failed: {exc}")
        if not rows:
            warning = (
                "Universal report returned no parseable metadata rows"
                f" after ready state; last_saved_path={last_saved_path};"
                f" readiness={_truncate_value(readiness)};"
                f" save_result={_truncate_value(save_diagnostics)}"
            )
            return [], [*warnings, *read_warnings, warning]
        warnings.extend(read_warnings)
        if last_saved_path:
            warnings.append(
                "Universal report XLSX became parseable"
                f"; rows={readiness.get('rows')}; columns={readiness.get('columns')}; path={last_saved_path}"
            )
        return rows[:max_rows], warnings
    except Exception as exc:
        return [], [f"Universal report metadata read failed: {exc}"]


async def _metadata_catalog_rows_from_output(session_id: str, max_rows: int) -> tuple[list[dict[str, str]], list[str]]:
    """Try to read the metadata catalog via the standard dynamic-list output command.

    Many dynamic lists can be exported to a tabular document with all visible
    rows/columns.  That path is preferable to UI row navigation when available;
    if the current form does not expose "Вывести список", callers fall back to
    table navigation.
    """
    warnings: list[str] = []
    try:
        output = await _bridge_call_recorded(session_id, "dynamic_list_output", {
            "button": "",
            "wait_seconds": 3,
        })
        tabular_docs = output.get("tabular_documents", {}) if isinstance(output, dict) else {}
        items = tabular_docs.get("items", []) if isinstance(tabular_docs, dict) else []
        if not items:
            return [], ["dynamic_list_output did not return tabular documents"]
        doc_name = items[0].get("name", "")
        text_result = await _bridge_call_recorded(session_id, "tabular_document_text", {
            "name": doc_name,
            "max_rows": max_rows,
            "max_columns": 20,
        })
        cells = text_result.get("cells", []) if isinstance(text_result, dict) else []
        return _metadata_catalog_rows_from_tabular_cells(cells), warnings
    except Exception as exc:
        return [], [f"dynamic_list_output metadata read failed: {exc}"]



def _metadata_catalog_rows_from_xlsx(path: Path) -> list[dict[str, str]]:
    import xml.etree.ElementTree as ET
    import zipfile

    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    def text_of(element: ET.Element) -> str:
        return "".join(t.text or "" for t in element.findall(".//a:t", ns)).strip()

    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [text_of(si) for si in root.findall("a:si", ns)]

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        rel_by_id = {rel.attrib.get("Id", ""): rel.attrib.get("Target", "") for rel in rels.findall("r:Relationship", rel_ns)}
        first_sheet = workbook.find("a:sheets/a:sheet", ns)
        if first_sheet is None:
            return []
        rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rel_by_id.get(rel_id, "worksheets/sheet1.xml")
        sheet_name = "xl/" + target.lstrip("/")
        if sheet_name not in archive.namelist() and target.startswith("/xl/"):
            sheet_name = target.lstrip("/")
        sheet = ET.fromstring(archive.read(sheet_name))

        cells: list[list[str]] = []
        for row in sheet.findall(".//a:sheetData/a:row", ns):
            row_values: list[str] = []
            current_col = 1
            for cell in row.findall("a:c", ns):
                ref = cell.attrib.get("r", "")
                match = re.match(r"([A-Z]+)", ref)
                if match:
                    col = 0
                    for char in match.group(1):
                        col = col * 26 + ord(char) - ord("A") + 1
                    while current_col < col:
                        row_values.append("")
                        current_col += 1
                value = ""
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    value = text_of(cell)
                else:
                    v = cell.find("a:v", ns)
                    if v is not None and v.text is not None:
                        if cell_type == "s":
                            with suppress(Exception):
                                value = shared_strings[int(v.text)]
                        else:
                            value = v.text
                row_values.append(value.strip())
                current_col += 1
            cells.append(row_values)
    return _metadata_catalog_rows_from_tabular_cells(cells)


def _metadata_catalog_rows_from_mxl(path: Path) -> list[dict[str, str]]:
    """Parse the textual MOXCEL stream saved by 1C tabular documents.

    1C native MXL files are not XLSX-like archives.  For Universal report output
    they contain a MOXCEL header followed by a text stream with cell literals in
    forms like `{"#","Синоним"}`.  The metadata identifiers report used by
    `metadata_objects_from_catalog` exposes a repeated six-text-field row
    layout: presentation/name/synonym/full_name/full_synonym/no_data.  Some
    rows are followed by a numeric collection-order value.  Keep the parser
    intentionally narrow and let callers fall back to XLSX if a particular MXL
    layout is different.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if not _looks_like_mxl(data):
        return []
    text = data.decode("utf-8", errors="ignore")
    values = [
        match.group(1).replace('""', '"').strip()
        for match in re.finditer(r'\{"#","((?:""|[^\"])*)"\}', text, re.DOTALL)
    ]
    if not values:
        return []
    header_idx = -1
    for index in range(len(values) - 5):
        header_window = [value.casefold() for value in values[index : index + 6]]
        if header_window[:5] == [
            "идентификатор объекта метаданных",
            "имя",
            "синоним",
            "полное имя",
            "полный синоним",
        ]:
            header_idx = index
            break
    if header_idx < 0:
        return []

    technical_types = (
        "БизнесПроцесс",
        "Документ",
        "ЖурналДокументов",
        "Задача",
        "Константа",
        "Обработка",
        "Отчет",
        "ПланВидовРасчета",
        "ПланВидовХарактеристик",
        "ПланОбмена",
        "ПланСчетов",
        "РегистрБухгалтерии",
        "РегистрНакопления",
        "РегистрСведений",
        "Справочник",
        "Роль",
        "Подсистема",
    )
    technical_type_pattern = "|".join(re.escape(item) for item in technical_types)
    full_name_re = re.compile(
        r"^\??\s*(?:" + technical_type_pattern + r")"
        r"\.[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*"
        r"(?:\.[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)*"
        r"(?:\s+\([0-9a-fA-F-]{36}\))?$"
    )

    def normalize_mxl_full_name(value: str) -> str:
        candidate = value.strip()
        if not full_name_re.match(candidate) or re.search(r"\.\s", candidate):
            return ""
        return re.sub(r"\s+\([0-9a-fA-F-]{36}\)$", "", candidate).strip()

    rows: list[dict[str, str]] = []
    index = header_idx + 6
    while index + 5 < len(values):
        no_data = values[index + 5].strip()
        if no_data.casefold() not in {"да", "нет"}:
            index += 1
            continue
        raw_full_name = values[index + 3].strip()
        full_name = normalize_mxl_full_name(raw_full_name)
        if not full_name and raw_full_name and "." not in raw_full_name and not raw_full_name.startswith("?"):
            # Universal report also contains section/group rows such as
            # `Справочники`, `Документы`, `Роли`.  The XLSX path keeps them as
            # <unknown> rows, so preserve them for parity; metadata-object
            # filtering later still excludes roles/subsystems by full name.
            full_name = raw_full_name
        if full_name:
            rows.append({
                "Идентификатор объекта метаданных": values[index].strip(),
                "Имя": values[index + 1].strip(),
                "Синоним": values[index + 2].strip(),
                "Полное имя": full_name,
                "Полный синоним": values[index + 4].strip(),
                "Без данных": no_data,
            })
            index += 6
            if index < len(values) and values[index].strip().isdigit():
                index += 1
            continue
        index += 1
    return rows

def _metadata_catalog_rows_from_tabular_cells(cells: list[list[Any]]) -> list[dict[str, str]]:
    if not cells:
        return []
    header_idx = -1
    headers: list[str] = []
    known_headers = {
        "ссылка",
        "наименование",
        "идентификатор объекта метаданных",
        "имя",
        "синоним",
        "полное имя",
        "полноеимя",
        "полный синоним",
        "пометка удаления",
        "пометкаудаления",
    }
    for i, row in enumerate(cells[:200]):
        normalized = [str(cell).strip() for cell in row]
        if any(cell.casefold() in known_headers for cell in normalized):
            header_idx = i
            headers = normalized
            break
    if header_idx < 0:
        # Common list-output layout: the first non-empty column is the list
        # presentation.  Treat every non-empty row as a Ссылка-only row.
        rows: list[dict[str, str]] = []
        for row in cells:
            value = next((str(cell).strip() for cell in row if str(cell).strip()), "")
            if value:
                rows.append({"Ссылка": value})
        return rows
    rows = []
    for row in cells[header_idx + 1:]:
        item: dict[str, str] = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            value = str(row[idx]).strip() if idx < len(row) else ""
            if value:
                item[header] = value
        if item:
            rows.append(item)
    return rows


async def _metadata_rows_from_direct_catalog_query(
    session_id: str,
    limit: int,
    *,
    query: str = "",
) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    try:
        result = await _bridge_call_recorded(
            session_id,
            "metadata_catalog_query",
            {"query": query, "limit": limit},
            timeout=20,
        )
    except Exception as exc:
        return [], [f"Direct metadata catalog query failed: {exc}"]
    rows = result.get("rows", []) if isinstance(result, dict) else []
    parsed_rows: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed_rows.append({str(key): str(value) for key, value in row.items()})
    if isinstance(result, dict):
        if result.get("non_deleted_filter") is not True:
            warnings.append("Direct metadata catalog query did not confirm non-deleted filter")
        if not parsed_rows:
            diagnostics = []
            for key in ("source", "rows_count", "scanned_count", "query"):
                if key in result:
                    diagnostics.append(f"{key}={result.get(key)!r}")
            if diagnostics:
                warnings.append("Direct metadata catalog query returned no rows (" + ", ".join(diagnostics) + ")")
    return parsed_rows, warnings


async def _metadata_rows_from_target_catalog_list(
    session_id: str,
    limit: int,
) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    navigation_link = "e1cib/list/Справочник.ИдентификаторыОбъектовМетаданных"
    open_result = await _dangerous_bridge_call_recorded(
        session_id,
        "open_navigation_link",
        {"navigation_link": navigation_link},
        timeout=15,
        danger="metadata_target_catalog_list_open_may_block_thin_client",
    )
    if isinstance(open_result, dict) and open_result.get("started_long_action"):
        warnings.append("Target metadata catalog list open watchdog timeout")
        return [], warnings
    try:
        result = await _bridge_call_recorded(
            session_id,
            "table_rows",
            {"name": "Список", "limit": limit, "compact": True},
            timeout=45,
        )
    except Exception as exc:
        return [], [f"Target metadata catalog list read failed: {exc}"]
    raw_rows = result.get("rows", []) if isinstance(result, dict) else []
    parsed_rows: list[dict[str, str]] = []
    for row in raw_rows:
        row_pairs = _table_row_pairs(row)
        if row_pairs:
            parsed_rows.append(row_pairs)
    if not parsed_rows:
        warnings.append("Target metadata catalog list returned no rows")
    return parsed_rows, warnings


def _metadata_catalog_data_from_rows(
    parsed_rows: list[dict[str, str]],
    warnings: list[str],
    *,
    navigation_link: str,
    source_method: str,
    limit: int,
    partial_for_query: bool = False,
) -> dict[str, Any]:
    actual_rows = []
    for row in parsed_rows:
        if not _is_not_deleted_catalog_row(row):
            continue
        item = _metadata_object_from_row(row)
        if _is_metadata_full_name_included(item.get("full_name", "")):
            actual_rows.append(row)
    objects = [_metadata_object_from_row(row) for row in actual_rows]
    self_entry = _metadata_objects_catalog_self_entry()
    if not any(item.get("full_name") == self_entry["full_name"] for item in objects):
        # Some configurations do not store a row for the metadata-identifiers
        # catalog itself in that same catalog.  Expose the currently opened
        # metadata object explicitly so the lookup tool remains useful for
        # navigation-link diagnostics and self-checks.
        objects.append(self_entry)
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
        "source_method": source_method,
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
        "limit": limit,
        "warnings": warnings,
        "partial_for_query": partial_for_query,
    }


def _metadata_query_is_exact_full_name(query: str) -> bool:
    if not query or "." not in query:
        return False
    metadata_type, metadata_name = query.split(".", 1)
    return bool(metadata_type.strip() and metadata_name.strip())


async def _load_metadata_objects_from_catalog(session_id: str, limit: int, query: str = "", max_results: int = 200) -> dict[str, Any]:
    """Build a metadata object snapshot through Universal report.

    Public search parameters are intentionally not pushed into the live 1C UI.
    The Universal report tabular document is saved to XLSX and parsed on the
    Python side.  This path deliberately avoids reading thousands of tabular
    document cells through the test-client API, because that can block
    web/thin-client sessions.
    """
    del query, max_results
    navigation_link = "e1cib/app/Отчет.УниверсальныйОтчет"
    requested_limit = max(1, int(limit or 1))
    parsed_rows, warnings = await _metadata_rows_from_universal_report(session_id, requested_limit)
    if not parsed_rows:
        warnings.append("Universal report returned no metadata rows for cache build")
    data = _metadata_catalog_data_from_rows(
        parsed_rows,
        warnings,
        navigation_link=navigation_link,
        source_method="universal_report",
        limit=requested_limit,
    )
    data["requested_limit"] = requested_limit
    data["effective_limit"] = requested_limit
    data["snapshot_truncated_by_safe_limit"] = False
    return data


@mcp.tool()
async def _metadata_objects_from_catalog_impl(
    *,
    query: str,
    limit: int,
    max_results: int,
    refresh_cache: bool,
    include_groups: bool,
    session_id: str,
) -> dict[str, Any]:
    cache_key = _metadata_cache_key(session_id)
    cache_hit = cache_key in _METADATA_OBJECTS_CACHE and not refresh_cache
    if cache_hit:
        catalog_data = _METADATA_OBJECTS_CACHE[cache_key]
    else:
        catalog_data = await _load_metadata_objects_from_catalog(session_id, limit, query=query, max_results=max_results)
        if not catalog_data.get("partial_for_query"):
            _METADATA_OBJECTS_CACHE[cache_key] = catalog_data

    all_objects = catalog_data["objects"]
    searchable = all_objects if include_groups else [item for item in all_objects if item["is_group"] != "Да"]
    matches = [item for item in searchable if _match_metadata_object(item, query)]
    warnings = list(catalog_data.get("warnings", []))
    fallback_search_used = False

    return {
        "found": bool(matches),
        "cache_hit": cache_hit,
        "query": query,
        "objects": matches[:max_results],
        "matched_count": len(matches),
        "returned_count": min(len(matches), max_results),
        "truncated": len(matches) > max_results,
        "fallback_search_used": fallback_search_used,
        "type_counts": catalog_data["type_counts"],
        "rows_count": catalog_data["rows_count"],
        "non_deleted_rows_count": catalog_data["non_deleted_rows_count"],
        "objects_count": catalog_data["objects_count"],
        "groups_count": catalog_data["groups_count"],
        "metadata_objects_count": catalog_data["metadata_objects_count"],
        "navigation_link": catalog_data["navigation_link"],
        "source": catalog_data["source"],
        "source_method": catalog_data.get("source_method", ""),
        "loaded_at": catalog_data["loaded_at"],
        "requested_limit": catalog_data.get("requested_limit", catalog_data.get("limit")),
        "effective_limit": catalog_data.get("effective_limit", catalog_data.get("limit")),
        "snapshot_truncated_by_safe_limit": bool(catalog_data.get("snapshot_truncated_by_safe_limit")),
        "warnings": warnings,
    }


@mcp.tool()
async def metadata_objects_from_catalog(
    query: Annotated[str, "Substring to search in presentation, full name, type, name or synonym"] = "",
    limit: Annotated[int, "Maximum rows to parse from the saved Universal report when cache is refreshed"] = 10000,
    max_results: Annotated[int, "Maximum matched objects to return"] = 200,
    refresh_cache: Annotated[bool, "Force rebuilding the Universal report snapshot instead of using the session cache"] = False,
    include_groups: Annotated[bool, "Include hierarchy group rows in results"] = False,
    watchdog_timeout: Annotated[int, "Maximum seconds to wait for the metadata report/catalog read before returning busy diagnostics; 0 disables"] = 90,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Return/search metadata objects from `Справочник.ИдентификаторыОбъектовМетаданных`.

    The first call opens the standard Universal report for the metadata-identifiers
    catalog, reads a normalized snapshot with metadata names/synonyms, excludes
    deleted rows plus roles/subsystems, and stores the result in an in-memory
    per-session cache. Later calls search that cache unless `refresh_cache=true`.
    """
    params = {
        "query": query,
        "limit": limit,
        "max_results": max_results,
        "refresh_cache": refresh_cache,
        "include_groups": include_groups,
    }
    coroutine = _metadata_objects_from_catalog_impl(
        query=query,
        limit=limit,
        max_results=max_results,
        refresh_cache=refresh_cache,
        include_groups=include_groups,
        session_id=session_id,
    )
    if watchdog_timeout and watchdog_timeout > 0:
        try:
            return await asyncio.wait_for(coroutine, timeout=float(watchdog_timeout))
        except TimeoutError as exc:
            sess = await _ensure_restored_session(session_id)
            blocked_action = _mark_session_blocked_action(
                sess,
                "metadata_objects_from_catalog",
                params,
                timeout=float(watchdog_timeout),
                error=_bridge_timeout_text(exc),
                danger="metadata_objects_universal_report_may_block_thin_client",
            )
            return {
                "found": False,
                "cache_hit": False,
                "query": query,
                "objects": [],
                "matched_count": 0,
                "returned_count": 0,
                "truncated": False,
                "fallback_search_used": False,
                "started_long_action": True,
                "interface_blocked": True,
                "reason": "metadata_objects_watchdog_timeout",
                "message": (
                    "Metadata objects lookup did not finish before the watchdog deadline. "
                    "The Universal report or catalog read may still be running in the thin client. "
                    "Use client_busy_status or wait_client_idle before sending further UI commands."
                ),
                "watchdog_timeout_seconds": watchdog_timeout,
                "blocked_action": _public_blocked_action(blocked_action),
                "type_counts": {},
                "rows_count": 0,
                "non_deleted_rows_count": 0,
                "objects_count": 0,
                "groups_count": 0,
                "metadata_objects_count": 0,
                "navigation_link": "e1cib/app/Отчет.УниверсальныйОтчет",
                "source": "Справочник.ИдентификаторыОбъектовМетаданных",
                "source_method": "universal_report",
                "loaded_at": datetime.now().isoformat(timespec="seconds"),
                "warnings": [
                    "metadata_objects_from_catalog watchdog timeout before lookup finished",
                ],
            }
    return await coroutine


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
    rows_result = await _bridge_call_recorded(session_id, "table_rows", {"name": "Список", "title": "", "limit": 500})
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
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Find a tested table row containing text."""
    return await _bridge_call_recorded(session_id, "table_find_row", {"name": name, "title": title, "text": text, "limit": limit})


def _table_tool_bridge_error_result(tool: str, name: str, title: str, exc: Exception) -> dict[str, Any]:
    error_text = str(exc).strip() or f"{tool} failed without error text"
    return {
        "found": None,
        "table_name": name,
        "table_title": title,
        "reason": "bridge_error",
        "tool": tool,
        "error": error_text,
    }


@mcp.tool()
async def table_goto_row(
    text: Annotated[str, "Substring to search in serialized row values"],
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    limit: Annotated[int, "Maximum rows to scan"] = 200,
    occurrence: Annotated[int, "1-based occurrence to select when multiple rows contain the same text"] = 1,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move cursor to the Nth tested table row containing text."""
    return await _bridge_call_recorded(session_id, "table_goto_row", {
        "name": name,
        "title": title,
        "text": text,
        "limit": limit,
        "occurrence": occurrence,
    })


@mcp.tool()
async def table_move_row(
    direction: Annotated[str, "first | last | next | previous | expand | collapse | level_down | level_up"] = "next",
    steps: Annotated[int, "Number of row moves"] = 1,
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move current row using official test-client table navigation methods."""
    return await _bridge_call_recorded(session_id, "table_move_row",
        {"name": name, "title": title, "direction": direction, "steps": steps},
    )


@mcp.tool()
async def table_add_row(
    name: Annotated[str, "Table technical name"] = "",
    title: Annotated[str, "Table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Add a row to the active tested table via the 1C test-client API (Insert / plus)."""
    try:
        return await _bridge_call_recorded(session_id, "table_add_row", {"name": name, "title": title})
    except BridgeError as bridge_exc:
        return _table_tool_bridge_error_result("table_add_row", name, title, bridge_exc)


@mcp.tool()
async def table_set_field(
    table: Annotated[str, "Table technical name"],
    field: Annotated[str, "Column field technical name or title"],
    value: Annotated[str, "Value to set"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    create_if_missing: Annotated[bool, "Allow choosing dropdown items like 'Create: ...' for missing reference values"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set a field in the currently edited row of a form table.

    Finds the editor field for the column (e.g. ТоварыНоменклатура), activates it,
    delegates to the general set_field_value logic, ends row editing, and verifies
    the value in the current table row."""
    return await _bridge_call_recorded(session_id, "table_set_field", {
        "table": table,
        "field": field,
        "value": value,
        "search_by_title": search_by_title,
        "create_if_missing": create_if_missing,
    })


@mcp.tool()
async def table_set_row_values(
    table: Annotated[str, "Table technical name"],
    values: Annotated[str, "JSON object mapping column names/titles to values"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    create_if_missing: Annotated[bool, "Allow choosing dropdown items like 'Create: ...' for missing reference values"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set multiple fields in the current row of a form table and verify each value."""
    return await _bridge_call_recorded(session_id, "table_set_row_values", {
        "table": table,
        "values": values,
        "search_by_title": search_by_title,
        "create_if_missing": create_if_missing,
    })


@mcp.tool()
async def set_form_fields(
    values: Annotated[str, "JSON object mapping field names/titles to values"],
    search_by_title: Annotated[bool, "If true, match visible title instead of object name"] = False,
    create_if_missing: Annotated[bool, "Allow choosing dropdown items like 'Create: ...' for missing reference values"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Set multiple ordinary form fields using the same logic as set_field_value."""
    return await _bridge_call_recorded(session_id, "set_form_fields", {
        "values": values,
        "search_by_title": search_by_title,
        "create_if_missing": create_if_missing,
    })


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
    try:
        return await _bridge_call_recorded(session_id, "table_begin_edit", {"name": name, "title": title})
    except BridgeError as bridge_exc:
        return _table_tool_bridge_error_result("table_begin_edit", name, title, bridge_exc)


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
async def dynamic_list_metadata_available_fields(
    object: Annotated[str, "1C metadata object full name, e.g. Catalog.ПС_Справочник1; empty = derive from active window"] = "",
    rag_snapshot: Annotated[str, "Optional RAG snapshot name/id"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return RAG/metadata-known fields for a metadata object; this does not inspect the live UI settings form."""
    sess = _session(session_id)
    disabled = _rag_require_enabled(session_id, "dynamic_list_metadata_available_fields", {"object": object, "rag_snapshot": rag_snapshot})
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
async def dynamic_list_settings_available_fields(
    section: Annotated[str, "Settings section: 'filter' (Отбор), 'order' (Сортировка), 'conditional' (Условное оформление), 'group' (Группировка)"] = "filter",
    expand_tree: Annotated[bool, "Expand tree rows to discover nested fields (slower on large live lists)"] = False,
    limit: Annotated[int, "Max fields to return"] = 500,
    close: Annotated[bool, "Close the settings form after reading"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open the dynamic-list settings form, switch to the requested section tab, and read the 'Доступные поля' table.

    Returns the live list of fields available for filtering/ordering/conditional-formatting/grouping
    in the current dynamic list.  This is the authoritative source — it reflects the actual form,
    not RAG metadata.
    """
    return await _bridge_call_recorded(session_id, "dynamic_list_settings_available_fields", {
        "section": section,
        "expand_tree": expand_tree,
        "limit": limit,
        "close": close,
    })


@mcp.tool()
async def dynamic_list_set_settings(
    filters: Annotated[str, "JSON array of filter objects [{field, value, comparison?}] or empty string"] = "",
    orders: Annotated[str, "JSON array of order objects [{field, direction?}] or empty string"] = "",
    clear_first: Annotated[bool, "Reset list settings before applying new filters/orders"] = False,
    close: Annotated[bool, "Close the settings form after applying"] = True,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open the dynamic-list settings form and add filter/order elements through the standard dialog.

    Filters: [{field, value, comparison?}] — comparison defaults to 'Равно'.
    Orders:  [{field, direction?}] — direction defaults to empty (uses dialog default).

    This is the preferred way to apply settings; it uses the same dialog the user sees.
    """
    filter_list = json.loads(filters) if filters else []
    order_list = json.loads(orders) if orders else []
    return await _bridge_call_recorded(session_id, "dynamic_list_set_settings", {
        "filters": filter_list,
        "orders": order_list,
        "clear_first": clear_first,
        "close": close,
    })


@mcp.tool()
async def dynamic_list_current_settings(
    section: Annotated[str, "Settings section to read: 'filter' (Отбор), 'order' (Сортировка), 'conditional' (Условное оформление), 'group' (Группировка)"] = "filter",
    close: Annotated[bool, "Close the settings form after reading"] = False,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open the dynamic-list settings form and read currently configured filter/order/etc. items.

    This reads the selected section's configured elements table, not the 'Доступные поля' table.
    Use dynamic_list_settings_available_fields when you need fields that can be added.
    """
    return await _bridge_call_recorded(session_id, "dynamic_list_current_settings", {
        "section": section,
        "close": close,
    })


@mcp.tool()
async def dynamic_list_sort_by_column(
    column_title: Annotated[str, "Visible column title to sort by using 1C УстановитьПорядок(<ЗаголовокКолонки>)"],
    name: Annotated[str, "Dynamic list/table technical name"] = "Список",
    title: Annotated[str, "Dynamic list/table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Quickly sort the active dynamic list by a visible column title via the 1C test-client API УстановитьПорядок; this is not the settings-form Sort tab."""
    return await _bridge_call_recorded(session_id, "dynamic_list_sort_by_column", {"name": name, "title": title, "column_title": column_title})


@mcp.tool()
async def dynamic_list_output(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries common Вывести список buttons"] = "",
    wait_seconds: Annotated[int, "Seconds to wait for the tabular document form"] = 2,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Press dynamic list 'Вывести список' button and return detected tabular document(s), including text when possible."""
    return await _bridge_call_recorded(session_id, "dynamic_list_output", {"button": button, "wait_seconds": wait_seconds})


@mcp.tool()
async def dynamic_list_open_settings(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries common Настроить список buttons"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open dynamic-list settings (filter/order/conditional formatting/grouping) via 'Настроить список'."""
    return await _bridge_call_recorded(session_id, "dynamic_list_open_settings", {"button": button})


@mcp.tool()
async def dynamic_list_find(
    text: Annotated[str, "Text/pattern to enter in the standard 'Найти' dialog"],
    field: Annotated[str, "Optional visible field name in 'Где искать', for example 'Наименование в программе' or 'ИНН'; empty uses dialog default"] = "",
    compare_type: Annotated[str, "Optional visible compare mode in 'Как искать'; empty uses dialog default"] = "",
    name: Annotated[str, "Dynamic list/table technical name"] = "Список",
    title: Annotated[str, "Dynamic list/table visible title"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Search a dynamic list through context menu 'Расширенный поиск' and the standard modal 'Найти' dialog."""
    return await _bridge_call_recorded(session_id, "dynamic_list_find", {
        "name": name,
        "title": title,
        "field": field,
        "text": text,
        "compare_type": compare_type,
    })


@mcp.tool()
async def dynamic_list_clear_settings(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries common reset/clear settings buttons"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Quickly reset/clear dynamic-list filter/order/conditional formatting/grouping settings."""
    return await _bridge_call_recorded(session_id, "dynamic_list_clear_settings", {"button": button})


@mcp.tool()
async def form_open_settings(
    button: Annotated[str, "Optional exact 1C button name/title; empty tries Изменить форму buttons"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Open the current form customization dialog via 'Изменить форму'."""
    return await _bridge_call_recorded(session_id, "form_open_settings", {"button": button})



@mcp.tool()
async def save_form(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Save/write the active form."""
    return await _bridge_call_recorded(session_id, "save_form", {})


def _find_message_box_texts_and_buttons(node: Any) -> tuple[str, list[str], list[dict[str, str]]]:
    texts: list[str] = []
    buttons: list[dict[str, str]] = []

    def walk(current: Any) -> None:
        if not isinstance(current, dict):
            return
        node_type = str(current.get("type") or "")
        title = str(current.get("title") or "")
        name = str(current.get("name") or "")
        visible = current.get("current_visible", current.get("visible", True))
        available = current.get("current_available", current.get("available", True))
        if title and visible:
            texts.append(title)
        if "кнопка" in node_type.lower() and visible and available:
            buttons.append({"name": name, "title": title})
        for child in current.get("children") or []:
            walk(child)
        current_page = current.get("current_page")
        if current_page:
            walk(current_page)

    walk(node)
    # Keep order but remove duplicates/noise.
    unique_texts: list[str] = []
    seen_texts: set[str] = set()
    for text in texts:
        if text in seen_texts:
            continue
        seen_texts.add(text)
        unique_texts.append(text)
    return "\n".join(unique_texts), unique_texts, buttons


@mcp.tool()
async def close_form(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Close the active form."""
    try:
        return await _bridge_call_recorded(session_id, "close_form", {})
    except (TimeoutError, BridgeError) as exc:
        error_text = str(exc).strip() or "close_form failed without error text"
        try:
            window = await _bridge_call_recorded(session_id, "active_window", {})
        except Exception as diag_exc:
            return {
                "closed": False,
                "reason": "close_failed",
                "close_error": error_text,
                "diagnostics_error": str(diag_exc) or type(diag_exc).__name__,
            }
        modal_text, modal_texts, modal_buttons = _find_message_box_texts_and_buttons(window)
        result: dict[str, Any] = {
            "closed": False,
            "reason": "close_failed",
            "close_error": error_text,
            "active_window": window,
            "modal_title": window.get("title", "") if isinstance(window, dict) else "",
            "modal_text": modal_text,
            "modal_texts": modal_texts,
            "modal_buttons": modal_buttons,
        }
        if isinstance(window, dict) and any(
            child.get("form_name") == "MessageBox" for child in window.get("children", []) if isinstance(child, dict)
        ):
            result["reason"] = "close_blocked_by_modal_dialog"
            result["modal_form_name"] = "MessageBox"
            result["modal_looks_like_dialog"] = True
        return _record_tool(session_id, "close_form", {}, result)


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

        geometry: dict[str, Any] | None = None
        fallback = ""
        fallback_reason = ""
        if os_support.IS_WINDOWS:
            geometry = _active_external_1c_window_geometry() if window else None
            if window and geometry is None:
                fallback = "full_display"
                fallback_reason = "1C window geometry was not detected"
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
                geometry = _active_external_1c_window_geometry(display=resolved_display) if window else None
                if window and geometry is None:
                    fallback = "full_display"
                    fallback_reason = (
                        "Cannot capture 1C window screenshot: window geometry was not detected; "
                        "captured the whole virtual display instead."
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
    if fallback:
        result["fallback"] = fallback
        result["fallback_reason"] = fallback_reason
    return result


async def _run(stale_scope_lock_existed: bool = False) -> None:
    reaper_task: asyncio.Task[Any] | None = None
    tools_changed_task: asyncio.Task[Any] | None = None
    try:
        await _recover_scoped_sessions_if_needed(stale_scope_lock_existed)
        reaper_task = asyncio.create_task(_global_orphan_reaper_loop())
        tools_changed_task = _schedule_startup_tools_changed_notifications()
        await mcp.run_stdio_async()
    finally:
        if reaper_task is not None:
            reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await reaper_task
        if tools_changed_task is not None:
            tools_changed_task.cancel()
            with suppress(asyncio.CancelledError):
                await tools_changed_task
        await _cleanup_sessions()
        _release_scope_lock()


async def _run_http(args: argparse.Namespace) -> None:
    """Run the MCP server over StreamableHTTP with static Bearer-token auth."""
    import uvicorn

    token_arg = args.http_token or ""
    token_file_arg = args.http_token_file or ""
    account_id_arg = args.http_account_id or ""

    config = load_static_token_config(
        token=token_arg or None,
        token_file=token_file_arg or None,
        account_id=account_id_arg or None,
    )

    # Attach auth to the module-level MCPServer instance. streamable_http_app
    # reads these attributes at build time, so we can safely patch them here
    # without re-creating the server.
    mcp.settings.auth = build_auth_settings(config)
    mcp._token_verifier = build_token_verifier(config)

    logging.info(
        "StreamableHTTP mode: host=%s port=%d account_id=%s stateless=%s path=/mcp",
        args.http_host,
        args.http_port,
        config.account_id,
        args.http_stateless,
    )

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=args.http_stateless,
        host=args.http_host,
    )
    config_uvicorn = uvicorn.Config(
        app,
        host=args.http_host,
        port=args.http_port,
        log_level=logging.getLevelName(logging.getLogger().level).lower(),
    )
    server = uvicorn.Server(config_uvicorn)
    tools_changed_task = _schedule_startup_tools_changed_notifications()
    try:
        await server.serve()
    finally:
        if tools_changed_task is not None:
            tools_changed_task.cancel()
            with suppress(asyncio.CancelledError):
                await tools_changed_task
        await _cleanup_sessions()


async def _publish_tools_list_changed(reason: str = "manual") -> None:
    """Publish a standard MCP tools/list_changed event to active subscriptions/listen streams."""
    try:
        await mcp._subscriptions.publish(ToolsListChanged())
        logging.info("Published MCP tools/list_changed notification (%s)", reason)
    except Exception as exc:
        logging.warning("Could not publish MCP tools/list_changed notification (%s): %s", reason, exc)


async def _startup_tools_changed_notifications(delays: tuple[float, ...] = TOOLS_CHANGED_STARTUP_DELAYS) -> None:
    for delay in delays:
        if delay > 0:
            await asyncio.sleep(delay)
        await _publish_tools_list_changed("startup")


def _schedule_startup_tools_changed_notifications() -> asyncio.Task[Any] | None:
    if not NOTIFY_TOOLS_CHANGED_ON_STARTUP:
        return None
    if not TOOLS_CHANGED_STARTUP_DELAYS:
        return None
    return asyncio.create_task(_startup_tools_changed_notifications())


async def _cleanup_sessions() -> None:
    """Stop all running sessions and release resources."""
    for sid, sess in list(_SESSIONS.items()):
        if SESSION_SCOPE_ID:
            if sess.runtime is not None:
                runtime.kill_process(sess.runtime.manager_process, "test-manager")
                sess.manager_pid = 0
            try:
                await sess.bridge.stop()
            except Exception:
                pass
            continue
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
    if SESSION_SCOPE_ID:
        _persist_sessions()
        _mark_global_sessions_orphaned_for_owner()
    else:
        for sess in list(_SESSIONS.values()):
            _remove_global_session_registry(sess)
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


def _tool_names_for_profile(profile: str) -> set[str] | None:
    parts = [part.strip().lower() for part in re.split(r"[,;\s]+", profile or "") if part.strip()]
    if not parts or any(part in {"all", "full"} for part in parts):
        return None
    selected: set[str] = set()
    unknown: list[str] = []
    for part in parts:
        names = TOOL_PROFILE_ALIASES.get(part) or TOOL_PROFILE_GROUPS.get(part)
        if names is None:
            unknown.append(part)
            continue
        selected.update(names)
    if unknown:
        known = sorted({*TOOL_PROFILE_GROUPS, *TOOL_PROFILE_ALIASES})
        raise ValueError(f"unknown tool profile(s): {', '.join(unknown)}; known profiles: {', '.join(known)}")
    return selected


def _apply_tool_profile(profile: str) -> dict[str, Any]:
    allowed = _tool_names_for_profile(profile)
    if allowed is None:
        return {"profile": profile or "full", "mode": "full", "removed": []}
    existing = set(mcp._tool_manager._tools.keys())
    removed: list[str] = []
    for name in sorted(existing - allowed):
        try:
            mcp.remove_tool(name)
            removed.append(name)
        except Exception:
            pass
    remaining = sorted(set(mcp._tool_manager._tools.keys()))
    return {"profile": profile, "mode": "filtered", "remaining_count": len(remaining), "removed_count": len(removed), "removed": removed}


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
    parser.add_argument(
        "--session-scope",
        default=os.getenv("ONEC_MCP_SESSION_SCOPE_ID") or os.getenv("ONEC_MCP_SESSION_SCOPE") or "",
        help="enable scoped lock/reconnect mode for the given session scope id; without this flag Answer42 uses legacy no-lock global state",
    )
    parser.add_argument(
        "--tool-profile",
        default=os.getenv("ONEC_MCP_TOOL_PROFILE", "full"),
        help="comma-separated tool profile groups: core, ui, sessions, windows, forms, tables, tabular, dynamic, rag, recording, full, all",
    )
    parser.add_argument(
        "--tool-log",
        default=os.getenv("ONEC_MCP_TOOL_LOG", ""),
        help="path to a rotating log file for every MCP tool call (env: ONEC_MCP_TOOL_LOG).",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        default=os.getenv("ONEC_MCP_HTTP", "").strip().lower() in {"1", "true", "yes", "on"},
        help="run the MCP server over StreamableHTTP instead of stdio",
    )
    parser.add_argument(
        "--http-host",
        default=os.getenv("ONEC_MCP_HTTP_HOST", "127.0.0.1"),
        help="host to bind the StreamableHTTP server to",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.getenv("ONEC_MCP_HTTP_PORT", "8080")),
        help="port to bind the StreamableHTTP server to",
    )
    parser.add_argument(
        "--http-token",
        default=os.getenv("ONEC_MCP_HTTP_TOKEN", ""),
        help="static Bearer token required by StreamableHTTP clients",
    )
    parser.add_argument(
        "--http-token-file",
        default=os.getenv("ONEC_MCP_HTTP_TOKEN_FILE", ""),
        help="file containing the static Bearer token for StreamableHTTP",
    )
    parser.add_argument(
        "--http-account-id",
        default=os.getenv("ONEC_MCP_ACCOUNT_ID", ""),
        help="MCP account id used to namespace credentials in HTTP mode; defaults to a hash of the token",
    )
    parser.add_argument(
        "--http-stateless",
        action="store_true",
        default=os.getenv("ONEC_MCP_HTTP_STATELESS", "1").strip().lower() in {"1", "true", "yes", "on"},
        help="use stateless StreamableHTTP mode (default); disable for resumable sessions",
    )
    args = parser.parse_args(argv)

    _configure_tool_logging(args.tool_log)

    _RAG_ENABLED = not args.disable_rag
    _CREDENTIAL_STORE_ENABLED = not args.disable_credential_store
    _DEV_TOOLS_ENABLED = bool(args.dev_tools)
    _register_dev_tools(_DEV_TOOLS_ENABLED)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if _RAG_ENABLED:
        _register_startup_rag_source_dirs(args.rag_source_dir or [], args.rag_source_prefix, not args.rag_source_no_recursive, args.rag_source_max_depth)
    else:
        logging.info("Built-in Answer42 RAG is disabled")
    profile_result = _apply_tool_profile(args.tool_profile)
    logging.info("Tool profile: %s (mode=%s, remaining=%d, removed=%d)", profile_result["profile"], profile_result["mode"], profile_result.get("remaining_count", 0), profile_result.get("removed_count", 0))
    _enable_tools_list_changed_capability()
    _start_autoreload_watcher(args.dev, args.dev_restart_timeout)
    if args.session_scope:
        _configure_session_scope(args.session_scope)
        stale = _acquire_scope_lock_if_configured()
    else:
        stale = False
    _reap_global_orphan_sessions()
    if args.http:
        asyncio.run(_run_http(args))
    else:
        asyncio.run(_run(stale))


_install_tool_call_logger()


if __name__ == "__main__":
    main()
