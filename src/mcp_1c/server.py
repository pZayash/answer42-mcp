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
import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from . import platform as _platform
from . import runtime
from .bridge import OneCBridgeServer
from .credentials import get_store as _credentials_store
from .rag import RagService
from .window_control import maximize_test_client_window as _maximize_external_window
from .recorder import RecordingSession

mcp = FastMCP("Answer42")
_RAG = RagService()
DEFAULT_SESSION = "default"
STATE_FILE = Path(os.getenv("ONEC_MCP_SESSION_STATE", "/tmp/onec-mcp/sessions.json"))
AUTORELOAD_PID_FILE = Path(os.getenv("ONEC_MCP_AUTORELOAD_PID_FILE", "/tmp/onec-mcp/answer42-autoreload.pid"))
AUTORELOAD_LOG_FILE = Path(os.getenv("ONEC_MCP_AUTORELOAD_LOG_FILE", "/tmp/onec-mcp/answer42-autoreload.log"))
RAG_BINDINGS_FILE = Path(os.getenv("ONEC_MCP_RAG_BINDINGS", "/tmp/onec-mcp/rag-bindings.json"))

_PORT_ALLOC_LOCK = Path(os.getenv("ONEC_MCP_PORT_ALLOC_LOCK", "/tmp/onec-mcp/port-allocation.lock"))


@contextmanager
def _port_allocation_lock():
    _PORT_ALLOC_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _PORT_ALLOC_LOCK.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


_PORT_BAND_SIZE = 20
_PORT_SCAN_WIDTH = 10


def _stable_session_port_offset(session_id: str) -> int:
    digest = hashlib.sha1(session_id.encode("utf-8")).digest()
    return (int.from_bytes(digest[:2], "big") % 100) * _PORT_BAND_SIZE


def _find_session_port(base: int, offset: int, *, sub_offset: int = 0) -> int:
    start = base + offset + sub_offset
    return runtime.find_free_port(start, start + _PORT_SCAN_WIDTH)


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
        self.file_client_ibsrv_port = 0  # to be set when a file infobase target is served through ibsrv
        self.file_client_runtime: runtime.ManagerRuntime | None = None
        self.xvfb_process: subprocess.Popen | None = None
        self.runtime: runtime.ManagerRuntime | None = None
        self.bridge = OneCBridgeServer(
            host="127.0.0.1",
            port=ws_port,
            request_timeout=float(os.getenv("ONEC_MCP_REQUEST_TIMEOUT", "60")),
        )
        self.recorder = RecordingSession()
        self.rag_snapshot: str = ""
        self.idle_timeout_seconds: int = 60 * 60
        self.last_activity_monotonic: float = time.monotonic()

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "display": self.display,
            "platform_dir": str(self.platform_dir),
            "ws_port": self.ws_port,
            "ibsrv_port": self.ibsrv_port,
            "testclient_port": self.testclient_port,
            "data_dir": str(self.data_dir),
            "base_url": self.base_url,
            "username": self.username,
            # Password is intentionally not persisted.
            "client_ibsrv_port": self.client_ibsrv_port,
            "file_client_ibsrv_port": self.file_client_ibsrv_port,
            "rag_snapshot": self.rag_snapshot,
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
        sess.file_client_ibsrv_port = int(data.get("file_client_ibsrv_port") or 0)
        sess.rag_snapshot = str(data.get("rag_snapshot", ""))
        sess.idle_timeout_seconds = int(data.get("idle_timeout_seconds") or 60 * 60)
        return sess

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
                start_new_session=True,
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


def _normalize_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


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


async def _ensure_restored_session(session_id: str = DEFAULT_SESSION) -> SessionState:
    if session_id in _SESSIONS:
        return _SESSIONS[session_id]
    persisted = _load_persisted_sessions()
    sess = persisted.get(session_id)
    if sess is None:
        raise ValueError(f"Session {session_id!r} not found. Call start_session first.")
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
    for field in fields:
        key = (_norm_key(field.get("name")), _norm_key(field.get("title")))
        dedup.setdefault(key, field)
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
            onec_path=str(platform_dir / "1cv8c"),
        ),
    )


# ---------------------------------------------------------------------------
# 1C configuration RAG / metadata index tools
# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
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
    detected = _RAG.detect_source(path) if format == "auto" else {"format": format}
    effective_format = str(detected.get("format") or format)
    result = _RAG.add_source(name=name, path=path, kind=kind, format=effective_format)
    if isinstance(result, dict):
        result["detected"] = detected
    return _record_tool(session_id, "rag_source_add", {"name": name, "path": path, "kind": kind, "format": format}, result)


@mcp.tool()
def rag_sources_list(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> list[dict[str, Any]]:
    """List registered 1C source trees."""
    return _record_tool(session_id, "rag_sources_list", {}, _RAG.list_sources())


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
    return _record_tool(session_id, "rag_snapshots_list", {}, _RAG.list_snapshots())


@mcp.tool()
def rag_index_build(
    snapshot: Annotated[str | None, "Snapshot name/id to index"] = None,
    source: Annotated[str | None, "Single source name to index"] = None,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Build/rebuild the SQLite/FTS RAG index for a source or snapshot."""
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
    sess = _session(session_id)
    effective_snapshot = snapshot or sess.rag_snapshot or None
    return _record_tool(session_id, "rag_lookup_object", {"object": object, "snapshot": effective_snapshot}, _RAG.lookup_object(object=object, snapshot=effective_snapshot))


@mcp.tool()
def rag_query(
    query: Annotated[str, "Natural-language or metadata search query"],
    snapshot: Annotated[str | None, "Optional snapshot name/id"] = None,
    limit: Annotated[int, "Max result count"] = 10,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Search business terms and full-text RAG chunks in the local SQLite index."""
    sess = _session(session_id)
    effective_snapshot = snapshot or sess.rag_snapshot or None
    return _record_tool(session_id, "rag_query", {"query": query, "snapshot": effective_snapshot, "limit": limit}, _RAG.query(text=query, snapshot=effective_snapshot, limit=limit))


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
    return {
        "running": True,
        "session_id": session_id,
        "base_url": sess.base_url,
        "version": sess.platform_dir.name,
        "display": sess.display,
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
            "server_alive": client_rt.server_process.poll() is None if client_rt and client_rt.server_process else False,
            "file_client_ibsrv_port": sess.file_client_ibsrv_port,
            "file_client_server_alive": file_client_rt.server_process.poll() is None if file_client_rt and file_client_rt.server_process else False,
        },
        "rag_snapshot": sess.rag_snapshot,
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
    connection_mode: Annotated[str, "Test-client connection mode: auto, ws (HTTP/WS URL), file (file infobase via ibsrv), file-direct (direct /F), or server (server/infobase)"] = "auto",
    connection_value: Annotated[str, "Connection target: URL for ws, directory path for file, server/infobase for server"] = "",
) -> dict[str, Any]:
    """One-shot session factory: detect version, start infrastructure and connect the 1C test client."""
    initial_connection_mode = _normalize_connection_mode(connection_mode, connection_value or base_url, base_url)
    resolved_version = version or None
    if not resolved_version and base_url and initial_connection_mode == "ws":
        resolved_version = _platform.detect_version_from_url(base_url)
    if resolved_version:
        _platform.require_websocket_support(resolved_version)

    platform_dir = _platform.find_platform_dir(resolved_version)
    actual_version = platform_dir.name
    _platform.require_websocket_support(actual_version)
    logging.info("start_session: version=%s platform_dir=%s", actual_version, platform_dir)

    sid = session_id or DEFAULT_SESSION
    if sid in _SESSIONS:
        raise ValueError(f"Session {sid!r} already exists.")

    data_dir = Path(f"/tmp/onec-mcp/sessions/{sid}")
    with _port_allocation_lock():
        port_offset = _stable_session_port_offset(sid)
        display_offset = port_offset // _PORT_BAND_SIZE
        display = runtime.find_free_display(runtime.DEFAULT_DISPLAY_START + display_offset)
        ws_port = _find_session_port(runtime.DEFAULT_WS_PORT, port_offset, sub_offset=0)
        ibsrv_port = _find_session_port(runtime.DEFAULT_IBSRV_HTTP_PORT, port_offset, sub_offset=0)
        ibsrv_direct_regport = _find_session_port(runtime.DEFAULT_IBSRV_DIRECT_PORT, port_offset, sub_offset=0)
        testclient_port = _find_session_port(runtime.DEFAULT_TEST_CLIENT_PORT, port_offset, sub_offset=0)

        sess = SessionState(
            session_id=sid, display=display, platform_dir=platform_dir,
            ws_port=ws_port, ibsrv_port=ibsrv_port, testclient_port=testclient_port,
            data_dir=data_dir, base_url=base_url, username=username, password=password
        )
        sess.idle_timeout_seconds = max(0, int(idle_timeout_minutes)) * 60
        if base_url:
            bound_snapshot = _snapshot_for_base_url(base_url)
            if bound_snapshot:
                sess.rag_snapshot = bound_snapshot
        sess.xvfb_process = runtime.start_xvfb(display)
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

    with _port_allocation_lock():
        runtime.create_file_infobase(sess.ibcmd, sess.db_path)
        cf_path = runtime.DEFAULT_CF_BUILT
        if not Path(cf_path).exists():
            raise FileNotFoundError(f"MCPTestManager.cf not found at {cf_path}.")
        runtime.load_configuration(sess.ibcmd, sess.db_path, cf_path)

    server_proc = runtime.start_standalone_server(
        ibsrv=sess.ibsrv, address=ibsrv_address, port=ibsrv_port,
        name=ibsrv_name, data_dir=sess.server_data_dir,
        log_dir=log_dir, db_path=sess.db_path,
        direct_regport=ibsrv_direct_regport,
    )
    runtime_state.server_process = server_proc

    ws_url = runtime.build_thin_client_ws_url(ibsrv_address, ibsrv_port)
    manager_proc = runtime.start_thin_test_manager(
        onec_path=sess.onec_thin, ws_url=ws_url,
        websocket_url=sess.bridge.url, log_dir=log_dir, display=display,
    )
    runtime_state.manager_process = manager_proc
    sess.runtime = runtime_state
    await _wait_for_bridge_connected(sess, timeout=60.0)
    _persist_sessions()

    pipeline_info: dict[str, Any] = {"manager_pid": manager_proc.pid, "ws_url": ws_url, "log_dir": str(log_dir)}

    # --- Built-in demo client ibsrv (second autonomous server for /TESTCLIENT) ---
    # It is only needed for self-contained E2E sessions.  When the caller
    # passed a target URL/file/server infobase, connect the test client to
    # that target instead of creating an extra demo infobase.
    if not (base_url or connection_value):
        with _port_allocation_lock():
            client_ibsrv_port = _find_session_port(runtime.DEFAULT_IBSRV_HTTP_PORT, port_offset, sub_offset=10)
            client_direct_regport = _find_session_port(runtime.DEFAULT_IBSRV_DIRECT_PORT, port_offset, sub_offset=10)
        client_log_dir = sess.client_log_dir
        client_log_dir.mkdir(parents=True, exist_ok=True)
        client_ibsrv_name = "mcpclient"

        with _port_allocation_lock():
            runtime.create_file_infobase(sess.ibcmd, sess.client_db_path)
            # Load a dedicated tested-client configuration.  It intentionally does
            # not contain MCPTestManager code (which uses /TESTMANAGER-only types),
            # but it does contain real metadata objects for positive UI E2E.
            client_cf_path = runtime.DEFAULT_CLIENT_CF_BUILT
            if not Path(client_cf_path).exists():
                raise FileNotFoundError(f"MCPTestClient.cf not found at {client_cf_path}.")
            runtime.load_configuration(sess.ibcmd, sess.client_db_path, client_cf_path)

        client_server_proc = runtime.start_standalone_server(
            ibsrv=sess.ibsrv, address=ibsrv_address, port=client_ibsrv_port,
            name=client_ibsrv_name, data_dir=sess.client_server_data_dir,
            log_dir=client_log_dir, db_path=sess.client_db_path,
            direct_regport=client_direct_regport,
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
        pipeline_info.update({
            "client_ibsrv_port": client_ibsrv_port, "client_ws_url": client_ws_url,
            "client_base_url": f"http://{ibsrv_address}:{client_ibsrv_port}",
        })

    connect_result: Any = None
    connect_base_url = base_url
    if not connect_base_url and pipeline_info.get("client_base_url"):
        connect_base_url = str(pipeline_info["client_base_url"])
    if connect_base_url or connection_value:
        connect_result = await _connect_test_client_impl(
            sess, base_url=connect_base_url, username=username, password=password,
            port=testclient_port, onec_path=sess.onec_thin, host="localhost",
            extra_args=extra_args, execute=execute, command_parameter=command_parameter,
            connection_mode=connection_mode, connection_value=connection_value,
        )
        _auto_maximize_test_client_result(connect_result, sess.display)

    if sess.idle_timeout_seconds > 0:
        asyncio.create_task(_idle_watch_session(sid))

    _persist_sessions()
    return {
        "session_id": sid, "version": actual_version,
        "platform_dir": str(platform_dir), "display": display,
        "ws_port": ws_port, "ibsrv_port": ibsrv_port,
        "testclient_port": testclient_port, "data_dir": str(data_dir),
        "bridge_url": sess.bridge.url, "rag_snapshot": sess.rag_snapshot,
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
    if sess.runtime is not None:
        runtime.kill_process(sess.runtime.manager_process, "test-manager")
        runtime.kill_process(sess.runtime.server_process, "ibsrv")
        stopped.append("pipeline")
    if sess.client_runtime is not None:
        runtime.kill_process(sess.client_runtime.server_process, "ibsrv-client")
        stopped.append("client-ibsrv")
    if sess.file_client_runtime is not None:
        runtime.kill_process(sess.file_client_runtime.server_process, "ibsrv-file-client")
        stopped.append("file-client-ibsrv")
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
    """Export the 1C event log."""
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
def credentials_check(base_url: str) -> dict[str, object]:
    """Check whether credentials exist for a base URL without returning secrets."""
    credentials = _credentials_store().lookup(base_url)
    if credentials is None:
        return {"base_url": base_url, "found": False}
    return {
        "base_url": base_url,
        "found": True,
        "username": credentials.get("username", ""),
        "password": "***",
    }


@mcp.tool()
def credentials_list() -> list[dict[str, str]]:
    """List configured credential URL patterns with passwords redacted."""
    return _credentials_store().list_entries()


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
    """Launch 1cv8c /TESTCLIENT inside the manager processor and connect."""
    await _wait_for_bridge_connected(sess, timeout=60.0)
    r_base = base_url or sess.base_url
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
            credentials = _credentials_store().lookup(r_base)
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
        if sess.file_client_runtime is not None:
            runtime.kill_process(sess.file_client_runtime.server_process, "ibsrv-file-client")
            sess.file_client_runtime = None
        with _port_allocation_lock():
            port_offset = _stable_session_port_offset(sess.session_id)
            file_client_port = _find_session_port(runtime.DEFAULT_IBSRV_HTTP_PORT, port_offset, sub_offset=15)
            file_client_direct_regport = _find_session_port(runtime.DEFAULT_IBSRV_DIRECT_PORT, port_offset, sub_offset=15)
        file_client_log_dir = sess.file_client_log_dir
        file_client_log_dir.mkdir(parents=True, exist_ok=True)
        server_proc = runtime.start_standalone_server(
            ibsrv=sess.ibsrv,
            address="127.0.0.1",
            port=file_client_port,
            name="mcpfileclient",
            data_dir=sess.file_client_server_data_dir,
            log_dir=file_client_log_dir,
            db_path=db_path,
            direct_regport=file_client_direct_regport,
        )
        sess.file_client_ibsrv_port = file_client_port
        sess.file_client_runtime = runtime.ManagerRuntime(
            infobase=runtime.InfobaseSpec(db_path=db_path),
            server=runtime.StandaloneServerSpec(
                address="127.0.0.1", port=file_client_port,
                name="mcpfileclient", data_dir=sess.file_client_server_data_dir,
            ),
            manager=runtime.TestManagerSpec(onec_path=sess.onec_thin),
            server_process=server_proc,
            log_dir=file_client_log_dir,
            extra={"target_connection_mode": "file"},
        )
        r_base = str(db_path)
        r_value = f"http://127.0.0.1:{file_client_port}"
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
        credentials = _credentials_store().lookup(r_base)
        if credentials:
            r_user = credentials["username"]
            r_pass = credentials["password"]

    sess.base_url = r_base
    sess.username = r_user
    sess.password = r_pass
    if sess.rag_snapshot and r_base:
        _save_rag_binding(r_base, sess.rag_snapshot)
    else:
        bound_snapshot = _snapshot_for_base_url(r_base) if r_base else ""
        if bound_snapshot:
            sess.rag_snapshot = bound_snapshot
    params = {"base_url": r_base, "username": r_user, "password": r_pass,
              "port": r_port, "onec_path": r_onec, "host": host,
              "extra_args": extra_args, "execute": execute,
              "command_parameter": command_parameter,
              "connection_mode": mode, "connection_value": r_value,
              "target_connection_mode": target_connection_mode}
    _touch_session(sess)
    result = await sess.bridge.call("connect_test_client", params)
    return _record_tool(sess.session_id, "connect_test_client", params, result)


def _auto_maximize_on_error(display: str) -> None:
    try:
        _maximize_external_window(title_contains="", display=display, width=None, height=None)
    except Exception as exc:
        logging.debug("auto maximize after failed connect_test_client failed: %s", exc)


def _auto_maximize_test_client_result(result: Any, display: str) -> None:
    try:
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
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return active window command interface, when supported."""
    return await _bridge_call_recorded(session_id, "window_command_interface", {"max_depth": max_depth})


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
async def maximize_test_client_window(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Activate and maximize the 1C test-client OS window via wmctrl/xdotool/Xlib."""
    sess = _session(session_id)
    return _maximize_external_window(title_contains="", display=sess.display, width=None, height=None)


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
    return _maximize_external_window(
        title_contains=title_contains,
        display=display or sess.display,
        width=width or None,
        height=height or None,
    )


@mcp.tool()
async def ui_tree(max_depth: Annotated[int, "Maximum UI tree depth"] = 4,
    include_rag_types: Annotated[bool, "Enrich UI elements with best-effort types from the local RAG index"] = True,
    rag_snapshot: Annotated[str, "Optional RAG snapshot name/id used for type enrichment"] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Return a serialized tree of the active window/forms/elements.

    When RAG data is available, each matching element gets `rag_type` and `rag`
    fields with metadata-derived type/source information.  Runtime 1C UI data is
    returned unchanged when the RAG index has no match.
    """
    tree = await _bridge_call_recorded(session_id, "ui_tree", {"max_depth": max_depth})
    if include_rag_types:
        sess = _session(session_id)
        tree = _enrich_ui_tree_with_rag_types(tree, snapshot=rag_snapshot or sess.rag_snapshot or None)
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
async def tabular_document_save(
    name: Annotated[str, "Tabular document field technical name"] = "",
    title: Annotated[str, "Tabular document field visible title"] = "",
    path: Annotated[str, "Output file path; empty = auto-generated temp path"] = "",
    format: Annotated[str, "Output format: pdf | xlsx | xls | mxl | html | txt"] = "pdf",
    max_depth: Annotated[int, "Maximum search depth"] = 8,
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Save a tabular document field to a file in the selected format."""
    return await _bridge_call_recorded(session_id, "tabular_document_save", {"name": name, "title": title, "path": path, "format": format, "max_depth": max_depth})


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
    if not object:
        try:
            win = await _bridge_call_recorded(session_id, "active_window", {})
            object = _metadata_name_from_navigation_url(str(win.get("navigation_url") or ""))
        except Exception:
            object = ""
    effective_snapshot = rag_snapshot or sess.rag_snapshot or None
    fields = _rag_fields_for_object(object, snapshot=effective_snapshot)
    return {"object": object, "rag_object": _metadata_name_to_rag_name(object), "snapshot": effective_snapshot, "count": len(fields), "fields": fields}


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
    """Stop recording and build PDF/MP4/HTML from captured slides."""
    return _session(session_id).recorder.stop()


@mcp.tool()
def screenshot(
    path: Annotated[str, "Output image path"] = "build/screenshots/screenshot.png",
    window: Annotated[bool, "Capture the best visible 1C window instead of the whole virtual display"] = True,
    display: Annotated[str, "X11 display to inspect/capture. Defaults to the session's display."] = "",
    *,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> dict[str, Any]:
    """Capture a screenshot from the MCP host. Defaults to the active visible 1C window."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sess = _session(session_id)
    resolved_display = display or sess.display

    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore

        if not resolved_display:
            raise RuntimeError("Cannot capture screenshot: no display set for session and no display argument provided")

        previous_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = resolved_display
        try:
            geometry = _active_x11_window_geometry(resolved_display) if window else None
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
            if sess.client_runtime is not None:
                runtime.kill_process(sess.client_runtime.server_process, "ibsrv-client")
            try:
                await sess.bridge.stop()
            except Exception:
                pass
            runtime.stop_xvfb(sess.xvfb_process)
        _SESSIONS.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for 1C:Enterprise test manager")
    parser.add_argument("--log-level", default=os.getenv("ONEC_MCP_LOG_LEVEL", "INFO"))
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
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    _start_autoreload_watcher(args.dev, args.dev_restart_timeout)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
