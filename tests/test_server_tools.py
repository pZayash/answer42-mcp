import asyncio
import builtins
import json
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from mcp_1c import __version__, runtime
from mcp_1c import platform as onec_platform
from mcp_1c.server import (
    _SESSIONS,
    DEFAULT_SESSION,
    SessionState,
    _active_x11_window_geometry,
    _append_available_users_to_auth_error,
    _cf_marker_matches,
    _configure_session_scope,
    _decode_log_bytes,
    _effective_dump_connection,
    _ensure_restored_session,
    _extract_error_lines,
    _format_navigation_suggestions,
    _is_ignored_runtime_extension_name,
    _is_placeholder_password,
    _json_list,
    _load_global_registry_unlocked,
    _match_metadata_object,
    _metadata_catalog_rows_from_tabular_cells,
    _metadata_objects_catalog_self_entry,
    _normalize_connection_mode,
    _parse_users_plain_text,
    _rag_diagnostic,
    _reap_global_orphan_sessions,
    _register_dev_tools,
    _state_file_for_scope,
    _test_client_startup_error_from_logs,
    _tool_names_for_profile,
    _update_global_session_registry,
    _users_url_for_base_url,
    _wait_for_test_client_ready,
    _write_cf_marker,
    configuration_name_version_from_subsystems,
    credentials_check,
    credentials_list,
    mcp,
    screenshot,
)
from mcp_1c.server import (
    main as mcp_main,
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
    "dynamic_list_metadata_available_fields",
    "dynamic_list_settings_available_fields",
    "dynamic_list_set_settings",
    "dynamic_list_current_settings",
    "dynamic_list_sort_by_column",
    "form_open_settings",
    "dynamic_list_find",
    "dynamic_list_filter_by_current_value",
    "rag_sources_add_from_directory",
    "rag_source_roots_list",
    "rag_available_configurations",
    "rag_session_autoconfigure",
    "tabular_document_state_display",
    "search_objects",
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


def test_default_session_is_scoped_to_answer42_process_pid():
    expected = os.getenv("ONEC_MCP_DEFAULT_SESSION") or f"default-{os.getpid()}"

    assert DEFAULT_SESSION == expected


def test_default_session_env_override_for_compatibility():
    env = os.environ.copy()
    env["ONEC_MCP_DEFAULT_SESSION"] = "custom-default-session"
    completed = subprocess.run(
        [sys.executable, "-c", "from mcp_1c.server import DEFAULT_SESSION; print(DEFAULT_SESSION)"],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )

    assert completed.stdout.strip() == "custom-default-session"


def test_session_scope_maps_to_dedicated_state_and_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEC_MCP_SESSION_SCOPES_DIR", str(tmp_path))

    scope_dir, state_file, lock_file = _state_file_for_scope("beta/ms:main")

    assert scope_dir == tmp_path / "beta-ms-main"
    assert state_file == scope_dir / "sessions.json"
    assert lock_file == scope_dir / "answer42.lock"


def test_configure_empty_session_scope_keeps_legacy_state(monkeypatch, tmp_path):
    from mcp_1c import server

    legacy = tmp_path / "legacy-sessions.json"
    monkeypatch.setenv("ONEC_MCP_SESSION_STATE", str(legacy))

    _configure_session_scope("")

    assert server.SESSION_SCOPE_ID == ""
    assert server.SCOPE_DIR is None
    assert server.SCOPE_LOCK_FILE is None
    assert server.STATE_FILE == legacy


def test_configure_session_scope_enables_scoped_state(monkeypatch, tmp_path):
    from mcp_1c import server

    monkeypatch.setenv("ONEC_MCP_SESSION_SCOPES_DIR", str(tmp_path))


    _configure_session_scope("sort-live")

    assert server.SESSION_SCOPE_ID == "sort-live"
    assert server.SCOPE_DIR == tmp_path / "sort-live"
    assert server.SCOPE_LOCK_FILE == tmp_path / "sort-live" / "answer42.lock"
    assert server.STATE_FILE == tmp_path / "sort-live" / "sessions.json"

    _configure_session_scope("")


def test_global_registry_tracks_session_process_pids(monkeypatch, tmp_path):
    from mcp_1c import server

    monkeypatch.setattr(server, "GLOBAL_REGISTRY_FILE", tmp_path / "global-sessions.json")
    monkeypatch.setattr(server, "GLOBAL_REGISTRY_LOCK", tmp_path / "global-sessions.lock")
    monkeypatch.setattr(server, "SESSION_SCOPE_ID", "scope-a")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / "scope-a" / "sessions.json")
    sess = SessionState(
        session_id="s1",
        display=":99",
        platform_dir=tmp_path / "platform",
        ws_port=12345,
        ibsrv_port=23456,
        testclient_port=34567,
        data_dir=tmp_path / "session-s1",
    )
    sess.base_url = "https://example.invalid/base"
    sess.manager_pid = 111
    sess.ibsrv_pid = 222
    sess.test_client_pid = 333

    _update_global_session_registry(sess)
    data = _load_global_registry_unlocked()
    records = data["sessions"]

    assert "scope-a:s1" in records
    assert records["scope-a:s1"]["manager_pid"] == 111
    assert records["scope-a:s1"]["ibsrv_pid"] == 222
    assert records["scope-a:s1"]["test_client_pid"] == 333
    assert records["scope-a:s1"]["owner_pid"] == os.getpid()


def test_global_registry_reaper_marks_dead_owner_before_ttl(monkeypatch, tmp_path):
    from mcp_1c import server

    monkeypatch.setattr(server, "GLOBAL_REGISTRY_FILE", tmp_path / "global-sessions.json")
    monkeypatch.setattr(server, "GLOBAL_REGISTRY_LOCK", tmp_path / "global-sessions.lock")
    server.GLOBAL_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    server.GLOBAL_REGISTRY_FILE.write_text(json.dumps({
        "sessions": {
            "scope-a:s1": {"owner_pid": 999999999, "status": "active", "manager_pid": 0, "updated_at": 1}
        }
    }), encoding="utf-8")

    result = _reap_global_orphan_sessions(ttl_seconds=1800)
    data = _load_global_registry_unlocked()

    assert result["orphaned"] == ["scope-a:s1"]
    assert result["reaped"] == []
    assert data["sessions"]["scope-a:s1"]["status"] == "orphaned"
    assert "orphaned_at" in data["sessions"]["scope-a:s1"]


def test_global_registry_reaper_kills_expired_orphan(monkeypatch, tmp_path):
    from mcp_1c import server

    killed: list[int] = []
    monkeypatch.setattr(server, "GLOBAL_REGISTRY_FILE", tmp_path / "global-sessions.json")
    monkeypatch.setattr(server, "GLOBAL_REGISTRY_LOCK", tmp_path / "global-sessions.lock")
    monkeypatch.setattr(server, "_kill_pid_group", lambda pid, label: killed.append(pid))
    server.GLOBAL_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    server.GLOBAL_REGISTRY_FILE.write_text(json.dumps({
        "sessions": {
            "scope-a:s1": {
                "owner_pid": 999999999,
                "status": "orphaned",
                "orphaned_at": 1,
                "updated_at": 1,
                "manager_pid": 111,
                "ibsrv_pid": 222,
                "test_client_pid": 333,
            }
        }
    }), encoding="utf-8")

    result = _reap_global_orphan_sessions(ttl_seconds=0)
    data = _load_global_registry_unlocked()

    assert result["reaped"] == ["scope-a:s1"]
    assert killed == [111, 222, 333]
    assert data["sessions"] == {}


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
        assert tools["dev_eval"].input_schema["required"] == ["code"]
    finally:
        _register_dev_tools(False)


def test_new_mcp_tools_do_not_include_performance_metrics():
    tool_names = set(_tools_by_name())

    assert "performance_get" not in tool_names
    assert "performance_clear" not in tool_names


def test_selected_new_tool_schemas():
    tools = _tools_by_name()

    assert tools["wait_object"].input_schema["properties"]["timeout"]["default"] == 15
    assert tools["set_file_dialog_result"].input_schema["required"] == ["paths"]
    assert tools["field_info"].input_schema["required"] == ["field_name"]
    assert tools["field_dropdown_items"].input_schema["required"] == ["field_name"]
    assert tools["field_dropdown_items"].input_schema["properties"]["timeout"]["default"] == 10
    assert "max_depth" not in tools["field_dropdown_items"].input_schema["properties"]
    assert "search_depth" not in tools["field_dropdown_items"].input_schema["properties"]
    assert "max_depth" not in tools["toggle_field"].input_schema["properties"]
    assert "max_depth" not in tools["click_button"].input_schema["properties"]
    assert tools["decoration_click_link"].input_schema["properties"]["presentation"]["default"] == ""
    assert tools["command_interface_commands"].input_schema["properties"]["section"]["default"] == ""
    assert "max_depth" not in tools["command_interface_commands"].input_schema["properties"]
    assert tools["command_interface_search"].input_schema["required"] == ["query"]
    assert tools["command_interface_search"].input_schema["properties"]["max_results"]["default"] == 20
    assert "session_id" in tools["configuration_name_version_from_subsystems"].input_schema["properties"]
    assert "session_id" in tools["installed_extensions_from_catalog"].input_schema["properties"]
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["query"]["default"] == ""
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["refresh_cache"]["default"] is False
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["include_groups"]["default"] is False
    assert "session_id" in tools["metadata_objects_catalog_cache_clear"].input_schema["properties"]
    assert tools["window_command_interface"].input_schema["properties"]["window_title"]["default"] == ""
    assert tools["start_session"].input_schema["properties"]["rag_enabled"]["default"] is True
    assert tools["rag_sources_add_from_directory"].input_schema["required"] == ["path"]
    assert tools["rag_sources_add_from_directory"].input_schema["properties"]["watch"]["default"] is True
    assert tools["rag_available_configurations"].input_schema["properties"]["sync"]["default"] is True
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["snapshot_name"]["default"] == ""
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["build_index"]["default"] is True
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["sync_sources"]["default"] is True
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["dump_username"]["default"] == ""
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["dump_password"]["default"] == ""
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["dump_all_extensions"]["default"] is True
    assert tools["rag_session_autoconfigure"].input_schema["properties"]["cleanup_generated_sources"]["default"] is True
    assert "connection_mode" not in tools["start_session"].input_schema["properties"]
    assert "connection_value" not in tools["start_session"].input_schema["properties"]
    assert tools["credentials_save"].input_schema["required"] == ["url", "username", "password"]
    assert tools["credentials_remove"].input_schema["required"] == ["url"]
    assert tools["tabular_document_save"].input_schema["properties"]["format"]["default"] == "pdf"
    assert "max_depth" not in tools["tabular_document_state_display"].input_schema["properties"]
    assert tools["search_objects"].input_schema["properties"]["limit"]["default"] == 50
    assert "max_depth" not in tools["search_objects"].input_schema["properties"]


def test_metadata_objects_catalog_self_entry_is_searchable():
    item = _metadata_objects_catalog_self_entry()
    assert item["full_name"] == "Справочник.ИдентификаторыОбъектовМетаданных"
    assert item["type"] == "Справочник"
    assert item["name"] == "ИдентификаторыОбъектовМетаданных"
    assert item["is_group"] == "Нет"
    assert _match_metadata_object(item, "ИдентификаторыОбъектовМетаданных")
    assert _match_metadata_object(item, "metadata") is False


def test_metadata_catalog_rows_from_tabular_cells_with_headers():
    rows = _metadata_catalog_rows_from_tabular_cells([
        ["Идентификаторы объектов метаданных"],
        ["Ссылка", "Синоним", "Полное имя"],
        ["? Справочник.PL_СервисыМаркета", "Сервисы", "Справочник.PL_СервисыМаркета"],
    ])
    assert rows == [{"Ссылка": "? Справочник.PL_СервисыМаркета", "Синоним": "Сервисы", "Полное имя": "Справочник.PL_СервисыМаркета"}]


def test_metadata_catalog_rows_from_tabular_cells_without_headers():
    rows = _metadata_catalog_rows_from_tabular_cells([
        ["? Константа.Test"],
        ["", "? Справочник.Items"],
    ])
    assert rows == [{"Ссылка": "? Константа.Test"}, {"Ссылка": "? Справочник.Items"}]


def test_format_navigation_suggestions():
    text = _format_navigation_suggestions([
        {"full_name": "Справочник.PL_СервисыМаркета", "navigation_link": "e1cib/list/Справочник.PL_СервисыМаркета", "source": "rag_lookup_object"}
    ])
    assert "Возможно, вы имели в виду" in text
    assert "e1cib/list/Справочник.PL_СервисыМаркета" in text


def test_ui_tree_summary_reports_truncation_and_hidden_children():
    from mcp_1c.server import _summarize_ui_tree
    tree = {
        "type": "ТестируемоеОкноКлиентскогоПриложения",
        "name": "MainWindow",
        "title": "1С:Предприятие",
        "visible": True,
        "hidden_children_count": 3,
        "children": [
            {
                "type": "ТестируемаяФорма",
                "name": "Список",
                "title": "Версии подсистем",
                "visible": True,
                "hidden_children_count": 5,
                "children": [
                    {"truncated": True, "truncated_reason": "max_depth", "type": "ТестируемаяКнопкаФормы", "name": "Button1", "title": "Кнопка"},
                    {"truncated": True, "truncated_reason": "max_depth", "type": "ТестируемоеПолеФормы", "name": "Field1", "title": "Поле"},
                ],
            },
            {
                "type": "ТестируемаяФорма",
                "name": "СкрытаяФорма",
                "title": "Скрытая",
                "visible": False,
                "children": [],
            },
        ],
    }
    summary = _summarize_ui_tree(tree, include_hidden=False)
    assert summary["include_hidden"] is False
    assert summary["truncated"] is True
    assert summary["truncated_nodes_count"] == 2
    assert summary["hidden_children_count"] == 8
    assert summary["hidden_nodes_count"] == 1


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






def test_ignored_runtime_extension_names_skip_ef_prefix():
    assert _is_ignored_runtime_extension_name("EF_60033159")
    assert _is_ignored_runtime_extension_name("ef_00_00843470")
    assert not _is_ignored_runtime_extension_name("MCP")
    assert not _is_ignored_runtime_extension_name("ConnectionToSM")


def test_dump_all_extensions_to_xml_uses_designer_all_extensions(monkeypatch, tmp_path):
    from mcp_1c.rag import dump as rag_dump

    platform_dir = tmp_path / "platform"
    platform_dir.mkdir()
    (platform_dir / "1cv8").write_text("", encoding="utf-8")
    output_dir = tmp_path / "extensions"

    captured = {}

    def fake_run(argv, *, timeout=600, display=""):
        captured["argv"] = argv
        captured["display"] = display
        ext_dir = output_dir / "TestExt"
        ext_dir.mkdir(parents=True)
        (ext_dir / "Configuration.xml").write_text("<MetaDataObject/>", encoding="utf-8")
        (ext_dir / "CommonModules.xml").write_text("<x/>", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rag_dump, "_run_designer", fake_run)
    monkeypatch.setattr(rag_dump, "_find_free_display", lambda: ":77")

    result = rag_dump.dump_all_extensions_to_xml(
        platform_dir,
        "server",
        "cluster/ib",
        output_dir,
        "user0",
        "secret",
    )

    assert "/S" in captured["argv"]
    assert "cluster/ib" in captured["argv"]
    assert "-AllExtensions" in captured["argv"]
    assert captured["display"] == ":77"
    assert result["extensions_count"] == 1
    assert result["extensions"][0]["name"] == "TestExt"
    assert result["extensions"][0]["files_count"] == 2


def test_json_list_accepts_json_and_delimited_strings():
    assert _json_list('["a.cf", "b.cfe"]') == ["a.cf", "b.cfe"]
    assert _json_list("a.cf,b.cfe\nc.cf") == ["a.cf", "b.cfe", "c.cf"]
    assert _json_list("") == []


def test_effective_dump_connection_uses_direct_session_target(tmp_path):
    sess = SessionState("s", ":99", Path("/opt/1cv8/x86_64/8.5.1.1150"), 8765, 8314, 1538, tmp_path)
    sess.target_connection_mode = "server"
    sess.target_connection_value = "<DIRECT_SERVER_INFOBASE>"

    assert _effective_dump_connection(sess, "auto", "", "") == ("server", "<DIRECT_SERVER_INFOBASE>")


def test_effective_dump_connection_rejects_web_without_direct_target(tmp_path):
    sess = SessionState("s", ":99", Path("/opt/1cv8/x86_64/8.5.1.1150"), 8765, 8314, 1538, tmp_path, base_url="https://example.invalid/ib/")

    with pytest.raises(ValueError, match="Web /WS URLs cannot be dumped"):
        _effective_dump_connection(sess, "auto", "", "")


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


def test_tabular_document_file_selection_waits_for_delayed_xlsx_materialization(tmp_path):
    import threading
    import time
    import zipfile

    from mcp_1c.server import _select_saved_tabular_document_file

    actual_xlsx = tmp_path / "actual.xlsx"
    requested_xlsx = tmp_path / "requested.xlsx"
    result = {"saved": True, "path": str(requested_xlsx), "format": "xlsx", "files": [{"path": str(actual_xlsx)}]}

    def write_later():
        time.sleep(0.1)
        with zipfile.ZipFile(actual_xlsx, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")

    writer = threading.Thread(target=write_later)
    writer.start()
    try:
        selected = _select_saved_tabular_document_file(result, "xlsx")
    finally:
        writer.join(timeout=1)

    assert selected["saved"] is True
    assert selected["selected"]["copied_to"] == str(requested_xlsx)
    assert requested_xlsx.is_file()
    with zipfile.ZipFile(requested_xlsx) as archive:
        assert "xl/workbook.xml" in archive.namelist()


def test_tool_profile_core_is_compact_and_keeps_common_ui_tools():
    core = _tool_names_for_profile("core")
    assert core is not None
    assert "start_session" in core
    assert "click_button" in core
    assert "table_set_field" in core
    assert "screenshot" in core
    assert "rag_session_autoconfigure" not in core
    assert "dynamic_list_output" not in core
    assert len(core) < 70


def test_tool_profile_can_compose_groups():
    selected = _tool_names_for_profile("core,rag")
    assert selected is not None
    assert "start_session" in selected
    assert "rag_session_autoconfigure" in selected
    assert "dynamic_list_output" not in selected
    assert _tool_names_for_profile("full") is None
    assert _tool_names_for_profile("all") is None


def test_compact_rows_result_converts_pairs_to_values():
    from mcp_1c.server import _compact_rows_result
    result = {
        "rows": [
            {"index": 1, "current": True, "pairs": [{"key": "Имя", "value": "Тест"}, {"key": "Код", "value": "001"}], "raw": "big"}
        ],
        "more": False,
    }

    compact = _compact_rows_result(result)

    assert compact["rows"] == [{"index": 1, "current": True, "values": {"Имя": "Тест", "Код": "001"}}]
    assert compact["rows_count"] == 1
    assert "raw" not in compact["rows"][0]


def test_compact_tabular_text_result_returns_tsv():
    from mcp_1c.server import _compact_tabular_text_result
    compact = _compact_tabular_text_result({"cells": [["A", "B"], [1, 2]], "rows": 2})

    assert compact["text"] == "A\tB\n1\t2"
    assert compact["rows_count"] == 2
    assert compact["format"] == "tsv"
    assert "cells" not in compact


def test_compact_ui_node_keeps_actionable_fields_and_omits_noise():
    from mcp_1c.server import _compact_ui_node
    node = {"type": "button", "name": "Ok", "title": "OK", "debug": "noise", "children": [{"type": "field", "name": "F", "value": "42", "debug": "noise"}]}

    compact = _compact_ui_node(node)

    assert compact == {"type": "button", "name": "Ok", "title": "OK", "children": [{"type": "field", "name": "F", "value": "42"}]}


def test_redact_value_masks_secrets():
    from mcp_1c.server import _redact_value

    assert _redact_value({"password": "secret123"}) == {"password": "***"}
    assert _redact_value({"username": "user"}) == {"username": "user"}
    assert _redact_value([{"token": "abc"}, {"api_key": "xyz"}]) == [{"token": "***"}, {"api_key": "***"}]
    assert _redact_value("my secret token here") == "***"


def test_truncate_value_limits_length():
    from mcp_1c.server import _truncate_value

    short = {"ok": True}
    assert _truncate_value(short) == '{"ok": true}'
    long = {"x": "a" * 10000}
    result = _truncate_value(long)
    assert result.endswith("... [truncated, total 10009]")
    assert len(result) < 5000


def test_configure_tool_logging_creates_rotating_handler(tmp_path):
    from mcp_1c.server import _configure_tool_logging

    log_file = tmp_path / "tools.log"
    logger_name = "mcp_1c.tools"
    # Clear any pre-existing handlers for a clean test
    import logging

    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()

    _configure_tool_logging(log_file)
    handlers = [h for h in logging.getLogger(logger_name).handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(handlers) == 1
    assert handlers[0].baseFilename == str(log_file)


# ---------------------------------------------------------------------------
# Form navigation tests (Python-side fallback for form_next_element)
# ---------------------------------------------------------------------------



def test_slow_ui_tools_use_method_specific_timeouts(monkeypatch):
    from mcp_1c import server

    class DummyBridge:
        request_timeout = 180.0

        async def call(self, method, params, timeout=None):
            self.called = (method, params, timeout)
            return {"ok": True}

    class DummySession:
        bridge = DummyBridge()

    async def fake_ensure_restored_session(session_id):
        return DummySession()

    async def fake_wait_for_bridge_connected(sess, timeout=60.0):
        return None

    monkeypatch.setattr(server, "_ensure_restored_session", fake_ensure_restored_session)
    monkeypatch.setattr(server, "_wait_for_bridge_connected", fake_wait_for_bridge_connected)
    monkeypatch.setattr(server, "_record_tool", lambda session_id, method, params, result: result)

    expected = {
        "click_button": 300.0,
        "ui_tree": 300.0,
        "find_object": 180.0,
        "search_objects": 300.0,
        "active_window": 180.0,
    }
    for method, timeout in expected.items():
        result = asyncio.run(server._bridge_call_recorded("unit", method, {"name": "ФормаПровести"}))
        assert result == {"ok": True}
        assert DummySession.bridge.called == (method, {"name": "ФормаПровести"}, timeout)


def test_search_objects_post_filters_manager_results_by_title_and_type():
    from mcp_1c.server import _filter_search_objects_result

    result = _filter_search_objects_result(
        {
            "found": True,
            "count": 2,
            "objects": [
                {"name": "КомандаЗакрыть", "title": "Закрыть", "type": "Тестируемая кнопка формы"},
                {"name": "КомандаОповещения", "title": "Отключить оповещения", "type": "Тестируемая кнопка формы"},
            ],
        },
        title="Закрыть",
        type="Тестируемая кнопка формы",
    )

    assert result["found"] is True
    assert result["count"] == 1
    assert result["objects"][0]["name"] == "КомандаЗакрыть"
    assert result["post_filter_applied"] is True
    assert result["post_filter_before_count"] == 2


def test_find_object_match_accepts_title_substring():
    from mcp_1c.server import _ui_object_matches_query

    assert _ui_object_matches_query(
        {"name": "КомандаЗакрыть", "title": "Закрыть форму", "type": "Тестируемая кнопка формы"},
        title="Закрыть",
        type="ТестируемаяКнопкаФормы",
    )
    assert not _ui_object_matches_query(
        {"name": "КомандаОповещения", "title": "Отключить оповещения", "type": "Тестируемая кнопка формы"},
        title="Закрыть",
        type="ТестируемаяКнопкаФормы",
    )
