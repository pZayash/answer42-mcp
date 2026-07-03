import asyncio
import builtins
import json
import pytest
from pathlib import Path, PureWindowsPath

from mcp_1c import __version__
from mcp_1c import platform as onec_platform
from mcp_1c import runtime
from mcp_1c.server import (
    SessionState,
    _SESSIONS,
    _append_available_users_to_auth_error,
    _cf_marker_matches,
    _decode_log_bytes,
    _ensure_restored_session,
    _extract_error_lines,
    _test_client_startup_error_from_logs,
    _active_x11_window_geometry,
    _wait_for_test_client_ready,
    _write_cf_marker,
    credentials_check,
    credentials_list,
    _is_placeholder_password,
    _normalize_connection_mode,
    _parse_users_plain_text,
    _rag_diagnostic,
    configuration_name_version_from_subsystems,
    _register_dev_tools,
    _users_url_for_base_url,
    main as mcp_main,
    mcp,
    screenshot,
)


EXPECTED_NEW_TOOLS = {
    "wait_object",
    "wait_form",
    "wait_window",
    "user_messages",
    "close_user_messages_panel",
    "current_error_info",
    "set_file_dialog_result",
    "clear_file_dialog_result",
    "field_info",
    "field_value_text",
    "field_tooltip",
    "field_dropdown_items",
    "form_next_element",
    "form_previous_element",
    "form_state",
    "form_default_button",
    "wait_form_closed",
    "object_command_panel",
    "object_context_menu",
    "window_command_interface",
    "command_interface_sections",
    "command_interface_commands",
    "command_interface_search",
    "group_expand",
    "group_collapse",
    "group_current_page",
    "group_state",
    "decoration_links",
    "decoration_click_link",
    "decoration_click",
    "configuration_name_version_from_subsystems",
    "installed_extensions_from_catalog",
    "metadata_objects_from_catalog",
    "metadata_objects_catalog_cache_clear",
    "table_select_all",
    "table_clear_selection",
    "table_delete_row",
    "table_copy_row",
    "table_toggle_delete_mark",
    "table_begin_edit",
    "table_end_edit",
    "table_editing_state",
    "rag_sources_add_from_directory",
    "rag_source_roots_list",
    "rag_available_configurations",
    "rag_session_autoconfigure",
    "tabular_document_state_display",
}


def _tools_by_name():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_cli_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        mcp_main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"answer42 {__version__}"


def test_cli_disable_credential_store_flag_is_registered(capsys):
    with pytest.raises(SystemExit) as exc_info:
        mcp_main(["--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--disable-credential-store" in help_text
    assert "--dev-tools" in help_text


def test_credentials_tools_report_disabled_when_credential_store_is_off(monkeypatch):
    monkeypatch.setattr("mcp_1c.server._CREDENTIAL_STORE_ENABLED", False)

    assert credentials_check("https://example.invalid/ib") == {
        "base_url": "https://example.invalid/ib",
        "found": False,
        "disabled": True,
    }
    assert credentials_list() == []




def test_default_data_dir_uses_answer42_temp_root(monkeypatch, tmp_path):
    from mcp_1c.os_support import default_data_dir

    monkeypatch.delenv("ONEC_MCP_DATA_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    assert default_data_dir() == tmp_path / "answer42"


def test_default_data_dir_honors_legacy_env_override(monkeypatch, tmp_path):
    from mcp_1c.os_support import default_data_dir

    override = tmp_path / "custom-runtime"
    monkeypatch.setenv("ONEC_MCP_DATA_DIR", str(override))

    assert default_data_dir() == override

def test_new_mcp_tools_are_registered():
    _register_dev_tools(False)
    tool_names = set(_tools_by_name())

    assert EXPECTED_NEW_TOOLS <= tool_names


def test_dev_eval_tool_is_registered_only_when_dev_tools_enabled():
    _register_dev_tools(False)
    assert "dev_eval" not in set(_tools_by_name())

    try:
        _register_dev_tools(True)
        tools = _tools_by_name()
        assert "dev_eval" in tools
        assert tools["dev_eval"].inputSchema["required"] == ["code"]
    finally:
        _register_dev_tools(False)


def test_new_mcp_tools_do_not_include_performance_metrics():
    tool_names = set(_tools_by_name())

    assert "performance_get" not in tool_names
    assert "performance_clear" not in tool_names


def test_selected_new_tool_schemas():
    tools = _tools_by_name()

    assert tools["wait_object"].inputSchema["properties"]["timeout"]["default"] == 15
    assert tools["set_file_dialog_result"].inputSchema["required"] == ["paths"]
    assert tools["field_info"].inputSchema["required"] == ["field_name"]
    assert tools["field_dropdown_items"].inputSchema["required"] == ["field_name"]
    assert tools["field_dropdown_items"].inputSchema["properties"]["timeout"]["default"] == 10
    assert tools["field_dropdown_items"].inputSchema["properties"]["max_depth"]["default"] == 3
    assert tools["field_dropdown_items"].inputSchema["properties"]["search_depth"]["default"] == 0
    assert tools["toggle_field"].inputSchema["properties"]["max_depth"]["default"] == 20
    assert tools["decoration_click_link"].inputSchema["properties"]["presentation"]["default"] == ""
    assert tools["command_interface_commands"].inputSchema["properties"]["section"]["default"] == ""
    assert tools["command_interface_commands"].inputSchema["properties"]["max_depth"]["default"] == 4
    assert tools["command_interface_search"].inputSchema["required"] == ["query"]
    assert tools["command_interface_search"].inputSchema["properties"]["max_results"]["default"] == 20
    assert "session_id" in tools["configuration_name_version_from_subsystems"].inputSchema["properties"]
    assert "session_id" in tools["installed_extensions_from_catalog"].inputSchema["properties"]
    assert tools["metadata_objects_from_catalog"].inputSchema["properties"]["query"]["default"] == ""
    assert tools["metadata_objects_from_catalog"].inputSchema["properties"]["refresh_cache"]["default"] is False
    assert tools["metadata_objects_from_catalog"].inputSchema["properties"]["include_groups"]["default"] is False
    assert "session_id" in tools["metadata_objects_catalog_cache_clear"].inputSchema["properties"]
    assert tools["window_command_interface"].inputSchema["properties"]["window_title"]["default"] == ""
    assert tools["start_session"].inputSchema["properties"]["rag_enabled"]["default"] is True
    assert tools["rag_sources_add_from_directory"].inputSchema["required"] == ["path"]
    assert tools["rag_sources_add_from_directory"].inputSchema["properties"]["watch"]["default"] is True
    assert tools["rag_available_configurations"].inputSchema["properties"]["sync"]["default"] is True
    assert tools["rag_session_autoconfigure"].inputSchema["properties"]["snapshot_name"]["default"] == ""
    assert tools["rag_session_autoconfigure"].inputSchema["properties"]["build_index"]["default"] is True
    assert tools["rag_session_autoconfigure"].inputSchema["properties"]["sync_sources"]["default"] is True
    assert "connection_mode" not in tools["start_session"].inputSchema["properties"]
    assert "connection_value" not in tools["start_session"].inputSchema["properties"]
    assert tools["credentials_save"].inputSchema["required"] == ["url", "username", "password"]
    assert tools["credentials_remove"].inputSchema["required"] == ["url"]
    assert tools["tabular_document_save"].inputSchema["properties"]["format"]["default"] == "pdf"
    assert tools["tabular_document_state_display"].inputSchema["properties"]["max_depth"]["default"] == 8


def test_configuration_name_version_reads_main_configuration_from_list_output(monkeypatch):
    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((method, params))
        if method == "dynamic_list_output":
            return {"tabular_documents": {"items": [{"name": "ТабличныйДокумент"}]}}
        if method == "tabular_document_text":
            return {
                "cells": [
                    ["Имя подсистемы", "Версия", "Это основная конфигурация"],
                    ["Platform42", "1.1.9.728", "Нет"],
                    ["МенеджерСервиса", "1.1.56.26", "Да"],
                    ["СтандартныеПодсистемы", "2.3.2.44", "Нет"],
                ]
            }
        return {"ok": True}

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(configuration_name_version_from_subsystems(session_id="unit"))

    assert result["found"] is True
    assert result["configuration_name"] == "МенеджерСервиса"
    assert result["configuration_version"] == "1.1.56.26"
    assert calls[0] == ("open_navigation_link", {"navigation_link": "e1cib/list/РегистрСведений.ВерсииПодсистем"})
    assert calls[1][0] == "dynamic_list_output"
    assert calls[2][0] == "tabular_document_text"


def test_rag_diagnostic_reports_missing_session_snapshot():
    _SESSIONS["rag_diag_test"] = SessionState("rag_diag_test", "", Path("."), 10001, 10002, 10003, Path("/tmp/rag_diag_test"))
    try:
        diagnostic = _rag_diagnostic("rag_diag_test", None)
    finally:
        _SESSIONS.pop("rag_diag_test", None)

    assert diagnostic["diagnostics"]
    assert diagnostic["diagnostics"][0]["code"] == "snapshot_not_specified"


def test_platform_version_from_windows_bin_dir():
    assert onec_platform.platform_version_from_dir(PureWindowsPath(r"C:\\Program Files\\1cv8\\8.5.1.1150\\bin")) == "8.5.1.1150"
    assert onec_platform.platform_version_from_dir(Path("/opt/1cv8/x86_64/8.5.1.1150")) == "8.5.1.1150"


def test_normalize_infobase_url_removes_language_and_adds_trailing_slash():
    assert onec_platform.normalize_infobase_url("https://example.invalid/ib/123") == "https://example.invalid/ib/123/"
    assert onec_platform.normalize_infobase_url("https://example.invalid/ib/123/") == "https://example.invalid/ib/123/"
    assert onec_platform.normalize_infobase_url("https://example.invalid/ib/123/ru_RU/") == "https://example.invalid/ib/123/"


def test_platform_candidate_requires_ibcmd(tmp_path):
    platform = tmp_path / "8.5.1.1236" / "bin"
    platform.mkdir(parents=True)
    (platform / onec_platform.os_support.executable_name("1cv8c")).write_text("", encoding="utf-8")

    assert not onec_platform._is_platform_candidate(platform)

    (platform / onec_platform.os_support.executable_name("ibcmd")).write_text("", encoding="utf-8")
    assert onec_platform._is_platform_candidate(platform)


def test_build_test_client_argv_keeps_program_files_path_as_single_arg():
    argv = runtime.build_test_client_argv(
        r"C:\\Program Files\\1cv8\\8.5.1.1150\\bin\\1cv8c.exe",
        connection_mode="ws",
        connection_value="https://example.invalid/ib/",
        port=3318,
        username="testuser",
        password="secret",
        out_log=Path(r"C:\\Temp\\test client.log"),
    )

    assert argv[0] == r"C:\\Program Files\\1cv8\\8.5.1.1150\\bin\\1cv8c.exe"
    assert argv[1:4] == ["ENTERPRISE", "/WS", "https://example.invalid/ib/"]
    assert "/TESTCLIENT" in argv
    assert "-TPort3318" in argv
    assert argv[-4:] == ["/N", "testuser", "/P", "secret"]


def test_placeholder_password_detection():
    assert _is_placeholder_password("***")
    assert _is_placeholder_password("********")
    assert not _is_placeholder_password("")
    assert not _is_placeholder_password("real***pass")
    assert not _is_placeholder_password("password")


def test_available_users_url_and_plain_text_parsing():
    assert _users_url_for_base_url("https://example.invalid/ib") == "https://example.invalid/ib/ru_RU/e1cib/users"
    assert _users_url_for_base_url("https://example.invalid/ib/") == "https://example.invalid/ib/ru_RU/e1cib/users"
    assert _parse_users_plain_text("\ufeffAdmin\n\nuser1\r\nAdmin\n") == ["Admin", "user1"]


def test_auth_error_enrichment_with_available_users():
    text = _append_available_users_to_auth_error(
        "Пользователь ИБ не идентифицирован",
        {"url": "https://example.invalid/ib/ru_RU/e1cib/users", "users": ["Admin", "user1"], "count": 2},
    )

    assert "Доступные пользователи" in text
    assert "- Admin" in text
    assert "- user1" in text


def test_extract_error_lines_prefers_errors():
    text = "info line\nОшибка запуска\nplain tail\nERROR detail"

    assert _extract_error_lines(text) == "Ошибка запуска\nERROR detail"


def _session_with_client_log_dir(tmp_path: Path) -> SessionState:
    sess = SessionState(
        session_id="auth",
        display="",
        platform_dir=tmp_path,
        ws_port=1,
        ibsrv_port=2,
        testclient_port=3,
        data_dir=tmp_path,
    )
    sess.client_log_dir.mkdir(parents=True, exist_ok=True)
    return sess


def test_test_client_startup_error_from_logs_prefers_auth_error(tmp_path):
    sess = _session_with_client_log_dir(tmp_path)
    (sess.client_log_dir / "test_client_1c_out.log").write_text(
        "ordinary startup line\nПользователь ИБ не идентифицирован\nmore details\n",
        encoding="utf-8",
    )

    assert _test_client_startup_error_from_logs(sess) == "Пользователь ИБ не идентифицирован"


def test_decode_log_bytes_supports_windows_utf16le_without_bom():
    text = "ordinary startup line\nПользователь ИБ не идентифицирован\nmore details\n"

    assert _decode_log_bytes(text.encode("utf-16-le")) == text


def test_test_client_startup_error_from_logs_detects_windows_utf16le_auth_error(tmp_path):
    sess = _session_with_client_log_dir(tmp_path)
    (sess.client_log_dir / "test_client_1c_out.log").write_bytes(
        "ordinary startup line\nПользователь ИБ не идентифицирован\nmore details\n".encode("utf-16-le")
    )

    assert _test_client_startup_error_from_logs(sess) == "Пользователь ИБ не идентифицирован"


def test_test_client_startup_error_from_logs_detects_windows_cp1251_auth_error(tmp_path):
    sess = _session_with_client_log_dir(tmp_path)
    (sess.client_log_dir / "test_client_1c_out.log").write_bytes(
        "ordinary startup line\nПользователь ИБ не идентифицирован\nmore details\n".encode("cp1251")
    )

    assert _test_client_startup_error_from_logs(sess) == "Пользователь ИБ не идентифицирован"


def test_wait_for_test_client_ready_reports_invalid_credentials_from_1c_out_log(tmp_path):
    sess = _session_with_client_log_dir(tmp_path)
    (sess.client_log_dir / "test_client_1c_out.log").write_text(
        "\ufeffПользователь ИБ не идентифицирован\n",
        encoding="utf-8",
    )

    try:
        asyncio.run(_wait_for_test_client_ready(sess, "localhost", 65530, timeout=1))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("_wait_for_test_client_ready must fail on invalid 1C credentials")

    assert "Пользователь ИБ не идентифицирован" in message
    assert "test_client_1c_out.log" in message
    assert "did not open test port" not in message


def test_test_client_startup_error_from_logs_detects_generic_1c_error(tmp_path):
    sess = _session_with_client_log_dir(tmp_path)
    (sess.client_log_dir / "test_client_1c_out.log").write_text(
        "ordinary startup line\nОшибка при выполнении операции\nmore details\n",
        encoding="utf-8",
    )

    assert _test_client_startup_error_from_logs(sess) == "Ошибка при выполнении операции"


def test_attach_test_client_surfaces_auth_error_from_logs(tmp_path):
    """When attach_test_client fails and the /Out log contains an auth error,
    the error message must include the auth failure text and log paths."""
    from mcp_1c.server import _append_file_tail, _looks_like_auth_failure

    sess = _session_with_client_log_dir(tmp_path)
    (sess.client_log_dir / "test_client_1c_out.log").write_text(
        "\ufeffПользователь ИБ не идентифицирован\n",
        encoding="utf-8",
    )
    (sess.client_log_dir / "test_client_stdout.log").write_text(
        "stdout line\n",
        encoding="utf-8",
    )

    attach_error = _test_client_startup_error_from_logs(sess)
    assert attach_error == "Пользователь ИБ не идентифицирован"
    assert _looks_like_auth_failure(attach_error)

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

    assert "Test client attach error:" in message
    assert "Пользователь ИБ не идентифицирован" in message
    assert "test_client_1c_out.log" in message
    assert "test_client_stdout.log" in message


def test_default_xvfb_resolution_is_large_enough_for_1c_windows():
    assert runtime.DEFAULT_DISPLAY_RES == "1920x1200x24"



def test_active_x11_window_geometry_accepts_test_client_title_and_clamps_to_screen(monkeypatch):
    def fake_run(args, **kwargs):
        class Result:
            pass
        result = Result()
        if args[0] == "xdpyinfo":
            result.stdout = "screen #0:\n  dimensions:    1280x1024 pixels\n"
            return result
        if args[0] == "xwininfo":
            result.stdout = (
                '0x100001 "Клиент тестирования (1С:Предприятие) - Бухгалтерия предприятия Marvin [42_19_3364]" '
                '("1cv8c" "1cv8c") 1918x1005+0+0 +0+0\n'
            )
            return result
        raise AssertionError(args)

    monkeypatch.setattr("mcp_1c.server.subprocess.run", fake_run)

    geometry = _active_x11_window_geometry(":99")
    assert geometry is not None
    assert geometry["title"] == "Клиент тестирования (1С:Предприятие) - Бухгалтерия предприятия Marvin [42_19_3364]"
    assert geometry["left"] == 0
    assert geometry["top"] == 0
    assert geometry["width"] == 1280
    assert geometry["height"] == 1005
    assert geometry["xwininfo_width"] == 1918


def test_screenshot_missing_mss_hint_uses_pypi_package_name(tmp_path, monkeypatch):
    sess = _session_with_client_log_dir(tmp_path)
    _SESSIONS[sess.session_id] = sess
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mss" or name.startswith("mss."):
            raise ImportError("No module named mss")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            screenshot(path=str(tmp_path / "shot.png"), session_id=sess.session_id)
    finally:
        _SESSIONS.pop(sess.session_id, None)

    message = str(exc_info.value)
    assert "answer42[screenshot]" in message
    assert "pipx inject answer42 mss" in message
    assert ".[screenshot]" not in message


def test_cf_marker_detects_changed_manager_configuration(tmp_path):
    db_path = tmp_path / "ib"
    db_path.mkdir()
    cf_path = tmp_path / "MCPTestManager.cf"
    cf_path.write_bytes(b"old cf")

    _write_cf_marker(db_path, cf_path)
    assert _cf_marker_matches(db_path, cf_path)

    cf_path.write_bytes(b"new cf")
    assert not _cf_marker_matches(db_path, cf_path)


def test_restore_drops_session_with_stale_manager_cf_marker(tmp_path, monkeypatch):
    data_dir = tmp_path / "session"
    db_path = data_dir / "ib"
    db_path.mkdir(parents=True)
    cf_path = tmp_path / "MCPTestManager.cf"
    cf_path.write_bytes(b"current cf")
    stale_cf = tmp_path / "old.cf"
    stale_cf.write_bytes(b"old cf")
    _write_cf_marker(db_path, stale_cf)

    state_file = tmp_path / "sessions.json"
    sess = SessionState("stale", "", tmp_path, 10001, 10002, 10003, data_dir)
    state_file.write_text(json.dumps({"stale": sess.to_json()}), encoding="utf-8")
    monkeypatch.setattr("mcp_1c.server.STATE_FILE", state_file)
    monkeypatch.setattr("mcp_1c.server._ensure_cf_built", lambda *args, **kwargs: str(cf_path))

    with pytest.raises(ValueError, match="older Answer42 manager CF"):
        asyncio.run(_ensure_restored_session("stale"))

    assert not data_dir.exists()
    assert json.loads(state_file.read_text(encoding="utf-8")) == {}


def test_cp866_process_output_decoding():
    assert runtime._decode_process_output("Ошибка исключительной блокировки".encode("cp866")) == "Ошибка исключительной блокировки"


def test_thin_client_ws_url_is_unquoted():
    assert runtime.build_thin_client_ws_url("127.0.0.1", 8314) == "http://127.0.0.1:8314"


def test_test_manager_argv_contains_startup_suppression_and_separate_ws_args():
    argv = runtime.build_thin_test_manager_argv("1cv8c", "http://127.0.0.1:8314", "ws://bridge")

    assert "/DisableStartupMessages" in argv
    assert "/DisableStartupDialogs" in argv
    assert argv[argv.index("/WS") + 1] == "http://127.0.0.1:8314"
    assert not any(part.startswith('/WS"') for part in argv)


def test_1c_out_args_are_before_testmanager():
    argv = ["1cv8c", "ENTERPRISE", "/WS", "http://127.0.0.1:8314", "/TESTMANAGER", "/C", "ws://bridge"]

    result = runtime.add_1c_out_args_before_testmanager(argv, "manager.log")

    assert result.index("/Out") < result.index("/TESTMANAGER")
    assert result[result.index("/Out") + 1] == "manager.log"


def test_connection_mode_normalization(tmp_path):
    file_ib = tmp_path / "ib"
    file_ib.mkdir()

    assert _normalize_connection_mode("auto", "https://example.invalid/ib", "") == "ws"
    assert _normalize_connection_mode("http", "https://example.invalid/ib", "") == "ws"
    assert _normalize_connection_mode("file", str(file_ib), "") == "file"
    assert _normalize_connection_mode("file-direct", str(file_ib), "") == "file-direct"
    assert _normalize_connection_mode("direct-file", str(file_ib), "") == "file-direct"
    assert _normalize_connection_mode("auto", str(file_ib), "") == "file"
    assert _normalize_connection_mode("server", "cluster/ib", "") == "server"
    assert _normalize_connection_mode("auto", "cluster/ib", "") == "server"


def test_tabular_document_file_selection_reports_format_mismatch(tmp_path):
    from mcp_1c.server import _select_saved_tabular_document_file

    wrong_pdf = tmp_path / "wrong.pdf"
    wrong_pdf.write_bytes(b"not a pdf")
    result = {"saved": True, "path": str(tmp_path / "requested.pdf"), "format": "pdf", "files": [{"path": str(wrong_pdf)}]}

    selected = _select_saved_tabular_document_file(result, "pdf")

    assert selected["saved"] is False
    assert selected["selected"] is None
    assert "No produced file matches" in selected["error"]


def test_tabular_document_file_selection_accepts_requested_format(tmp_path):
    from mcp_1c.server import _select_saved_tabular_document_file

    actual_pdf = tmp_path / "actual.pdf"
    actual_pdf.write_bytes(b"%PDF-1.7\n")
    requested_pdf = tmp_path / "requested.pdf"
    result = {"saved": True, "path": str(requested_pdf), "format": "pdf", "files": [{"path": str(actual_pdf)}]}

    selected = _select_saved_tabular_document_file(result, "pdf")

    assert selected["saved"] is True
    assert selected["selected"]["copied_to"] == str(requested_pdf)
    assert requested_pdf.read_bytes().startswith(b"%PDF")
