import asyncio
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from mcp_1c import runtime, server
from mcp_1c.server import _SESSIONS, SessionState, _decode_log_bytes, mcp

MANAGER_MODULE = (
    Path(__file__).resolve().parents[1]
    / "src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl"
)


def test_tool_crash_text_reaches_the_client():
    """The SDK masks plain exceptions as a bare "Error executing tool X"."""

    @mcp.tool(name="unit_crashing_tool")
    def unit_crashing_tool() -> str:
        raise RuntimeError("ibsrv failed: [FATAL] Ошибка исключительной блокировки информационной базы.")

    try:
        with pytest.raises(ToolError) as info:
            asyncio.run(mcp.call_tool("unit_crashing_tool", {}))
    finally:
        mcp.remove_tool("unit_crashing_tool")

    assert not isinstance(info.value, UnexpectedToolError)
    message = str(info.value)
    assert message.startswith("Error executing tool unit_crashing_tool: RuntimeError: ")
    assert "исключительной блокировки" in message


def test_export_eventlog_tool_is_removed():
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "export_eventlog" not in names


@pytest.mark.parametrize("encoding", ["cp866", "cp1251"])
def test_decode_8bit_cyrillic_detects_oem_and_ansi(encoding):
    text = "[FATAL] Ошибка исключительной блокировки информационной базы."
    assert runtime.decode_8bit_cyrillic(text.encode(encoding)) == text
    assert _decode_log_bytes(text.encode(encoding)) == text


class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.pid = 4242

    def poll(self):
        return self.returncode


def _listening_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def test_standalone_server_fatal_after_port_open_is_reported(tmp_path, monkeypatch):
    """ibsrv opens its HTTP port before locking the infobase, then exits with FATAL."""
    log_path = tmp_path / "ibsrv.log"
    log_path.write_bytes("old run: Stand-alone Server ready.\n".encode("cp866"))
    offset = log_path.stat().st_size
    log_path.write_bytes(
        log_path.read_bytes()
        + "[FATAL] Ошибка исключительной блокировки информационной базы.\n".encode("cp866")
    )
    monkeypatch.setattr(runtime, "kill_process", lambda proc, label: None)
    with _listening_socket() as sock:
        port = sock.getsockname()[1]
        with pytest.raises(RuntimeError) as info:
            runtime._wait_standalone_server_ready(
                _FakeProc(), "127.0.0.1", port, 5, log_path, offset,
                db_path=Path("C:/Infobases/bp"), poll=0.01,
            )
    message = str(info.value)
    assert "[FATAL] Ошибка исключительной блокировки" in message
    assert "уже открыта другим процессом" in message
    assert "old run" not in message


def test_standalone_server_ready_line_is_accepted(tmp_path):
    log_path = tmp_path / "ibsrv.log"
    log_path.write_text("1C:Enterprise 8.5 Stand-alone Server started.\nStand-alone Server ready.\n", encoding="utf-8")
    with _listening_socket() as sock:
        runtime._wait_standalone_server_ready(
            _FakeProc(), "127.0.0.1", sock.getsockname()[1], 5, log_path, 0, poll=0.01,
        )


def test_standalone_server_early_exit_is_not_a_60s_wait(tmp_path, monkeypatch):
    log_path = tmp_path / "ibsrv.log"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime, "kill_process", lambda proc, label: None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    with pytest.raises(RuntimeError, match="exited with code 1"):
        runtime._wait_standalone_server_ready(_FakeProc(returncode=1), "127.0.0.1", free_port, 60, log_path, 0, poll=0.01)


def test_find_free_port_skips_port_listening_on_loopback():
    with _listening_socket() as sock:
        busy = sock.getsockname()[1]
        assert runtime._port_bindable(busy) is False
        assert runtime.find_free_port(busy, busy + 20) != busy


def _hidden_windows():
    return [
        # EnumDesktopWindows order: topmost first.
        {"hwnd": 11, "title": "", "pid": 7, "left": 900, "top": 900, "width": 50, "height": 50},
        {"hwnd": 12, "title": "", "pid": 7, "left": 787, "top": 346, "width": 985, "height": 10},
        {"hwnd": 13, "title": "Настройка MCP", "pid": 7, "left": 797, "top": 356, "width": 965, "height": 680},
        {"hwnd": 14, "title": "Конфигурация", "pid": 7, "left": -8, "top": -8, "width": 2576, "height": 1408},
        {"hwnd": 15, "title": "Чужое окно", "pid": 99, "left": 100, "top": 100, "width": 400, "height": 300},
    ]


@pytest.mark.parametrize(("window", "expected"), [(True, [13, 14]), (False, [13, 14, 15])])
def test_hidden_desktop_screenshot_includes_floating_form_windows(tmp_path, monkeypatch, window, expected):
    captured = {}
    monkeypatch.setattr(server.win_desktop, "list_windows", lambda desktop: _hidden_windows())
    monkeypatch.setattr(
        server,
        "_active_external_1c_window_geometry",
        lambda display="": {"left": -8, "top": -8, "width": 2576, "height": 1408, "title": "Конфигурация", "hwnd": 14},
    )

    def fake_capture(desktop, windows, canvas, path):
        captured["hwnds"] = [w["hwnd"] for w in windows]
        captured["canvas"] = canvas
        Path(path).write_bytes(b"png")
        return Path(path)

    monkeypatch.setattr(server.win_desktop, "capture_windows_png", fake_capture)
    geometry, fallback, _ = server._capture_hidden_desktop_screenshot("Answer42-unit-client", tmp_path / "s.png", window=window)

    assert captured["hwnds"] == expected
    assert captured["canvas"] == {"left": -8, "top": -8, "width": 2576, "height": 1408}
    assert fallback == ""
    if window:
        assert [w["title"] for w in geometry["windows"]] == ["Настройка MCP", "Конфигурация"]
    else:
        assert geometry is None


@pytest.mark.skipif(sys.platform != "win32", reason="WinAPI capture is Windows-only")
def test_capture_windows_png_nudges_every_captured_window(tmp_path, monkeypatch):
    """PrintWindow freezes the 1C test client until the window is re-laid out."""
    from mcp_1c import win_desktop

    api = win_desktop._winapi()
    nudged = []

    class User32:
        def __getattr__(self, name):
            return getattr(api.user32, name)

        def PrintWindow(self, hwnd, dc, flags):
            return 1

        def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
            nudged.append((hwnd, flags))
            return 1

    fake_api = type("Api", (), {})()
    fake_api.__dict__.update(api.__dict__)
    fake_api.user32 = User32()
    monkeypatch.setattr(win_desktop, "_winapi", lambda: fake_api)
    monkeypatch.setattr(win_desktop, "run_on_desktop", lambda name, fn, timeout=30.0: fn())

    windows = [
        {"hwnd": 101, "left": 10, "top": 10, "width": 20, "height": 20},
        {"hwnd": 102, "left": 0, "top": 0, "width": 40, "height": 40},
    ]
    out = win_desktop.capture_windows_png("Answer42-unit", windows, {"left": 0, "top": 0, "width": 40, "height": 40}, tmp_path / "c.png")

    assert out.read_bytes().startswith(b"\x89PNG")
    assert sorted(hwnd for hwnd, _ in nudged) == [101, 102]
    assert all(flags & 0x0020 and flags & 0x0010 for _, flags in nudged)  # FRAMECHANGED | NOACTIVATE


_FORM_PROPERTIES = (
    "Заголовок", "ТекущийЭлемент", "ТолькоПросмотр", "Доступность", "Ширина", "Высота", "Окно",
    "Модифицированность", "ВводДоступен", "ВладелецФормы", "Элементы", "Команды", "РежимОткрытияОкна",
    "ИмяФормы", "КлючУникальности", "ТекущаяСтраница", "Подсказка", "Видимость",
)


def test_manager_form_module_does_not_assign_form_properties_as_locals():
    """In a managed-form module an undeclared `Заголовок = ...` writes the form caption.

    That silently broke title filters of wait_form/wait_object/activate_window:
    the parameter value was replaced with the manager form's own caption.
    """
    text = MANAGER_MODULE.read_text(encoding="utf-8-sig")
    offenders = []
    for chunk in re.split(r"\n(?=(?:Функция|Процедура) )", text):
        signature = re.match(r"(?:Функция|Процедура) (\S+?)\((.*?)\)", chunk)
        if not signature:
            continue
        params = {part.split("=")[0].replace("Знач ", "").strip() for part in signature.group(2).split(",")}
        declared = set()
        for line in re.findall(r"^\s*Перем ([^;]+);", chunk, re.MULTILINE):
            declared.update(name.strip() for name in line.split(","))
        for prop in _FORM_PROPERTIES:
            if prop in params or prop in declared:
                continue
            if re.search(r"^\t+" + prop + r"\s*=(?!=)", chunk, re.MULTILINE):
                offenders.append(f"{signature.group(1)}: {prop}")
    assert offenders == []


def test_failed_start_releases_shared_file_server_instead_of_killing_it(tmp_path, monkeypatch):
    sess = SessionState(
        session_id="unit-shared", display="", platform_dir=tmp_path, ws_port=1, ibsrv_port=2,
        testclient_port=3, data_dir=tmp_path / "data",
    )
    shared_proc = subprocess.Popen([sys.executable, "-c", "pass"])
    shared_proc.wait()
    sess.file_client_runtime = runtime.ManagerRuntime(
        infobase=runtime.InfobaseSpec(db_path=tmp_path),
        server=runtime.StandaloneServerSpec(),
        manager=runtime.TestManagerSpec(),
        server_process=shared_proc,
    )
    sess.shared_file_key = "file:unit"
    killed, released = [], []

    async def fake_call(*args, **kwargs):
        raise RuntimeError("manager busy")

    async def fake_stop():
        return None

    monkeypatch.setattr(sess.bridge, "call", fake_call)
    monkeypatch.setattr(sess.bridge, "stop", fake_stop)
    monkeypatch.setattr(server.runtime, "kill_process", lambda proc, label: killed.append(label))
    monkeypatch.setattr(server, "_release_shared_file_server", lambda key, sid: released.append((key, sid)))
    monkeypatch.setattr(server, "_cleanup_stale_session_processes", lambda *a, **k: None)
    monkeypatch.setattr(server, "_persist_sessions", lambda: None)
    monkeypatch.setattr(server, "_remove_global_session_registry", lambda s: None)
    _SESSIONS[sess.session_id] = sess

    asyncio.run(server._cleanup_failed_start_session(sess, "unit"))

    assert released == [("file:unit", "unit-shared")]
    assert "ibsrv-file-client" not in killed
    assert sess.shared_file_key == ""
    assert sess.session_id not in _SESSIONS


def test_locked_file_infobase_falls_back_to_direct_file_connection(tmp_path, monkeypatch):
    """ibsrv needs an exclusive lock; a base opened elsewhere is still usable via /F."""
    db_path = tmp_path / "bp"
    db_path.mkdir()
    sess = SessionState(
        session_id="unit-fallback", display="", platform_dir=tmp_path, ws_port=1, ibsrv_port=2,
        testclient_port=3, data_dir=tmp_path / "data",
    )
    launched = {}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_protocol(sess):
        return {}

    async def fake_call(method, params, timeout=None):
        launched["attach"] = params
        return {"connected": True, "connection_mode": params["connection_mode"]}

    def locked(**kwargs):
        raise RuntimeError(
            "ibsrv failed to start on 127.0.0.1:9304: ibsrv exited with code 4294967295.\n"
            "[FATAL] Ошибка исключительной блокировки информационной базы."
        )

    class FakeProc:
        pid = 777

        def poll(self):
            return None

    def fake_start(argv, **kwargs):
        launched["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(server, "_wait_for_bridge_connected", fake_ready)
    monkeypatch.setattr(server, "_verify_manager_protocol", fake_protocol)
    monkeypatch.setattr(server, "_wait_for_test_client_ready", fake_ready)
    monkeypatch.setattr(server, "_acquire_shared_file_server", locked)
    monkeypatch.setattr(server, "_lookup_credentials", lambda base: None)
    monkeypatch.setattr(server, "_snapshot_for_base_url", lambda base: "")
    monkeypatch.setattr(server.runtime, "start_test_client", fake_start)
    monkeypatch.setattr(sess.bridge, "call", fake_call)

    _SESSIONS[sess.session_id] = sess
    try:
        result = asyncio.run(
            server._connect_test_client_impl(sess, base_url=str(db_path), connection_value=str(db_path))
        )
    finally:
        _SESSIONS.pop(sess.session_id, None)

    assert launched["attach"]["connection_mode"] == "file-direct"
    assert launched["attach"]["target_connection_mode"] == "file-direct"
    assert any(str(db_path) in arg for arg in launched["argv"])
    assert result["connection_fallback"]["reason"] == "infobase_exclusive_lock"
    assert "исключительной блокировки" in result["connection_fallback"]["ibsrv_error"]
    assert sess.shared_file_key == ""
    compact = server._compact_start_test_client_result(result)
    assert compact["connection_fallback"]["to"] == "file-direct"


def _call_tool_error(name, arguments):
    with pytest.raises(ToolError) as info:
        asyncio.run(mcp.call_tool(name, arguments))
    return str(info.value)


def test_http_mode_requires_explicit_session_id(monkeypatch):
    """One StreamableHTTP server serves all clients: no silently shared default session."""
    monkeypatch.setattr(server, "_REQUIRE_EXPLICIT_SESSION_ID", True)
    monkeypatch.setattr(server, "_load_persisted_sessions", dict)

    for tool in ("session_status", "active_window", "stop_session"):
        assert "session_id обязателен" in _call_tool_error(tool, {})

    explicit = _call_tool_error("active_window", {"session_id": "unit-unknown"})
    assert "session_id обязателен" not in explicit
    assert "unit-unknown" in explicit
    status = asyncio.run(mcp.call_tool("session_status", {"session_id": "unit-unknown"}))
    assert not status.is_error


def test_stdio_mode_keeps_default_session(monkeypatch):
    monkeypatch.setattr(server, "_REQUIRE_EXPLICIT_SESSION_ID", False)
    monkeypatch.setattr(server, "_load_persisted_sessions", dict)
    message = _call_tool_error("active_window", {})
    assert "session_id обязателен" not in message
    assert server.DEFAULT_SESSION in message


def test_http_mode_start_session_generates_its_own_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_REQUIRE_EXPLICIT_SESSION_ID", True)
    monkeypatch.setattr(server, "_new_session_id", lambda: "sess_unit_generated")
    monkeypatch.setattr(server._platform, "find_platform_dir", lambda version=None: tmp_path)
    monkeypatch.setattr(server._platform, "platform_version_from_dir", lambda path: "8.5.1.1343")
    monkeypatch.setattr(server._platform, "require_websocket_support", lambda version: None)
    sentinel = SessionState(
        session_id="sess_unit_generated", display="", platform_dir=tmp_path, ws_port=1, ibsrv_port=2,
        testclient_port=3, data_dir=tmp_path / "data",
    )
    _SESSIONS[sentinel.session_id] = sentinel
    try:
        # The generated id is already taken, so start_session stops right after choosing it.
        message = _call_tool_error("start_session", {})
    finally:
        _SESSIONS.pop(sentinel.session_id, None)
    assert "sess_unit_generated" in message
    assert server.DEFAULT_SESSION not in message


def test_session_port_band_falls_back_when_whole_band_is_reserved(monkeypatch):
    """A Windows excluded port range can swallow a whole 10-port session band."""
    reserved = range(3475, 3575)
    monkeypatch.setattr(runtime, "_port_bindable", lambda port: port not in reserved)
    port = server._find_session_port(1541, 1960)
    assert port == 3575
