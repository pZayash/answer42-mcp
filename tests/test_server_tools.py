import asyncio
import pytest
from pathlib import Path, PureWindowsPath

from mcp_1c import __version__
from mcp_1c import platform as onec_platform
from mcp_1c import runtime
from mcp_1c.server import (
    SessionState,
    _SESSIONS,
    _append_available_users_to_auth_error,
    _extract_error_lines,
    _test_client_startup_error_from_logs,
    _wait_for_test_client_ready,
    credentials_check,
    credentials_list,
    _is_placeholder_password,
    _normalize_connection_mode,
    _parse_users_plain_text,
    _rag_diagnostic,
    _users_url_for_base_url,
    main as mcp_main,
    mcp,
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
    assert "--disable-credential-store" in capsys.readouterr().out


def test_credentials_tools_report_disabled_when_credential_store_is_off(monkeypatch):
    monkeypatch.setattr("mcp_1c.server._CREDENTIAL_STORE_ENABLED", False)

    assert credentials_check("https://example.invalid/ib") == {
        "base_url": "https://example.invalid/ib",
        "found": False,
        "disabled": True,
    }
    assert credentials_list() == []


def test_new_mcp_tools_are_registered():
    tool_names = set(_tools_by_name())

    assert EXPECTED_NEW_TOOLS <= tool_names


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
    assert _parse_users_plain_text("\ufeffAdmin\n\nuser1\r\nAdmin\n") == ["user0", "user1"]


def test_auth_error_enrichment_with_available_users():
    text = _append_available_users_to_auth_error(
        "Пользователь ИБ не идентифицирован",
        {"url": "https://example.invalid/ib/ru_RU/e1cib/users", "users": ["user0", "user1"], "count": 2},
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
