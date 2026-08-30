import asyncio
import builtins
import json
import os
import subprocess
import sys
import types
from pathlib import Path, PureWindowsPath

import pytest

from mcp_1c import __version__, runtime
from mcp_1c import platform as onec_platform
from mcp_1c.server import (
    _SESSIONS,
    DEFAULT_SESSION,
    SessionState,
    _append_available_users_to_auth_error,
    _auto_configure_summary_rag_on_start,
    _cf_marker_matches,
    _configuration_from_sorted_first_row,
    _configure_session_scope,
    _configure_summary_rag_from_config_alias,
    _configure_universal_report_metadata_settings,
    _convert_1c_guid_navigation_link,
    _decode_log_bytes,
    _effective_dump_connection,
    _ensure_restored_session,
    _extract_error_lines,
    _find_main_configuration_in_row_pairs,
    _format_navigation_suggestions,
    _is_ignored_runtime_extension_name,
    _is_placeholder_password,
    _json_list,
    _load_global_registry_unlocked,
    _match_metadata_object,
    _metadata_catalog_rows_from_tabular_cells,
    _metadata_object_from_row,
    _metadata_objects_catalog_self_entry,
    _metadata_rows_from_direct_catalog_query,
    _metadata_universal_report_filter_specs,
    _normalize_connection_mode,
    _parse_users_plain_text,
    _rag_diagnostic,
    _reap_global_orphan_sessions,
    _register_dev_tools,
    _state_file_for_scope,
    _summary_rag_config_from_42clouds_url,
    _table_row_pairs,
    _tabular_cells_from_binary_mxl_literals,
    _tabular_cells_from_mxl,
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
    "client_busy_status",
    "wait_client_idle",
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
    "rag_sources_add_from_directory",
    "rag_source_roots_list",
    "rag_available_configurations",
    "rag_session_autoconfigure",
    "tabular_document_state_display",
    "search_objects",
    "convert_1c_guid_navigation_link",
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


def test_removed_mcp_tools_are_not_registered():
    _register_dev_tools(False)
    tool_names = set(_tools_by_name())

    assert "dynamic_list_filter_by_current_value" not in tool_names
    assert "create_new_item" not in tool_names
    assert "performance_get" not in tool_names
    assert "performance_clear" not in tool_names


def test_convert_1c_guid_navigation_link_known_bp_example():
    result = _convert_1c_guid_navigation_link(
        "c3314ae6-6899-11e8-8780-b06ebf2faaf6",
        metadata_name="Справочник.ВидыНоменклатуры",
    )

    assert result["guid"] == "c3314ae6-6899-11e8-8780-b06ebf2faaf6"
    assert result["ref"] == "8780b06ebf2faaf611e86899c3314ae6"
    assert result["navigation_link"] == (
        "e1cib/data/Справочник.ВидыНоменклатуры?ref=8780b06ebf2faaf611e86899c3314ae6"
    )


def test_convert_1c_guid_navigation_link_ref_and_navigation_to_guid():
    ref = "8780b06ebf2faaf611e86899c3314ae6"

    from_ref = _convert_1c_guid_navigation_link(ref, direction="ref_to_guid")
    from_link = _convert_1c_guid_navigation_link(
        f"e1cib/data/Справочник.ВидыНоменклатуры?ref={ref}",
        direction="navigation_to_guid",
    )

    assert from_ref["guid"] == "c3314ae6-6899-11e8-8780-b06ebf2faaf6"
    assert from_ref["navigation_link"] == ""
    assert from_link["guid"] == "c3314ae6-6899-11e8-8780-b06ebf2faaf6"
    assert from_link["metadata_name"] == "Справочник.ВидыНоменклатуры"
    assert from_link["navigation_link"].endswith(f"?ref={ref}")



def test_reference_setters_pass_create_if_missing_to_bridge(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        calls.append((session_id, method, params, timeout))
        return {"ok": True}

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)

    asyncio.run(server.set_field_value("Номенклатура", "Новый товар", create_if_missing=True, session_id="unit"))
    asyncio.run(server.table_set_field("Товары", "Номенклатура", "Новый товар", create_if_missing=True, session_id="unit"))
    asyncio.run(server.table_set_row_values("Товары", '{"Номенклатура":"Новый товар"}', create_if_missing=True, session_id="unit"))
    asyncio.run(server.set_form_fields('{"Контрагент":"Новый контрагент"}', create_if_missing=True, session_id="unit"))

    assert calls == [
        ("unit", "set_field_value", {"field_name": "Номенклатура", "value": "Новый товар", "search_by_title": False, "create_if_missing": True}, None),
        ("unit", "table_set_field", {"table": "Товары", "field": "Номенклатура", "value": "Новый товар", "search_by_title": False, "create_if_missing": True}, None),
        ("unit", "table_set_row_values", {"table": "Товары", "values": '{"Номенклатура":"Новый товар"}', "search_by_title": False, "create_if_missing": True}, None),
        ("unit", "set_form_fields", {"values": '{"Контрагент":"Новый контрагент"}', "search_by_title": False, "create_if_missing": True}, None),
    ]

def test_selected_new_tool_schemas():
    tools = _tools_by_name()

    assert tools["wait_object"].input_schema["properties"]["timeout"]["default"] == 15
    assert tools["set_file_dialog_result"].input_schema["required"] == ["paths"]
    assert tools["field_info"].input_schema["required"] == ["field_name"]
    assert tools["field_dropdown_items"].input_schema["required"] == ["field_name"]
    assert tools["field_dropdown_items"].input_schema["properties"]["timeout"]["default"] == 10
    assert tools["set_field_value"].input_schema["properties"]["create_if_missing"]["default"] is False
    assert tools["table_set_field"].input_schema["properties"]["create_if_missing"]["default"] is False
    assert tools["table_set_row_values"].input_schema["properties"]["create_if_missing"]["default"] is False
    assert tools["set_form_fields"].input_schema["properties"]["create_if_missing"]["default"] is False
    assert "max_depth" not in tools["field_dropdown_items"].input_schema["properties"]
    assert "search_depth" not in tools["field_dropdown_items"].input_schema["properties"]
    assert "max_depth" not in tools["toggle_field"].input_schema["properties"]
    assert "max_depth" not in tools["click_button"].input_schema["properties"]
    assert tools["click_button"].input_schema["properties"]["watchdog_timeout"]["default"] == 120
    assert tools["decoration_click_link"].input_schema["properties"]["presentation"]["default"] == ""
    assert tools["command_interface_commands"].input_schema["properties"]["section"]["default"] == ""
    assert "max_depth" not in tools["command_interface_commands"].input_schema["properties"]
    assert tools["command_interface_search"].input_schema["required"] == ["query"]
    assert tools["command_interface_search"].input_schema["properties"]["max_results"]["default"] == 20
    assert tools["command_interface_search"].input_schema["properties"]["include_timings"]["default"] is False
    assert tools["command_bar"].input_schema["properties"]["include_timings"]["default"] is False
    assert "session_id" in tools["configuration_name_version_from_subsystems"].input_schema["properties"]
    assert "session_id" in tools["installed_extensions_from_catalog"].input_schema["properties"]
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["query"]["default"] == ""
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["refresh_cache"]["default"] is False
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["include_groups"]["default"] is False
    assert tools["metadata_objects_from_catalog"].input_schema["properties"]["watchdog_timeout"]["default"] == 90
    assert "session_id" in tools["metadata_objects_catalog_cache_clear"].input_schema["properties"]
    assert tools["window_command_interface"].input_schema["properties"]["window_title"]["default"] == ""
    assert tools["start_session"].input_schema["properties"]["rag_enabled"]["default"] is False
    assert tools["start_session"].input_schema["properties"]["summary_rag_config"]["default"] == ""
    assert tools["start_session"].input_schema["properties"]["include_timings"]["default"] is False
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
    assert tools["tabular_document_save"].input_schema["properties"]["watchdog_timeout"]["default"] == 120
    assert tools["client_busy_status"].input_schema["properties"]["probe_timeout"]["default"] == 2
    assert tools["wait_client_idle"].input_schema["properties"]["timeout"]["default"] == 60
    assert "max_depth" not in tools["tabular_document_state_display"].input_schema["properties"]
    assert tools["search_objects"].input_schema["properties"]["limit"]["default"] == 50
    assert "max_depth" not in tools["search_objects"].input_schema["properties"]



def test_command_diagnostic_timings_are_opt_in(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        calls.append((session_id, method, params, timeout))
        return {"available": False, "reason": "not_supported", "_timings": {"total_ms": 2}}

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)

    without_timings = asyncio.run(server.command_bar(session_id="unit"))
    with_timings = asyncio.run(server.command_bar(include_timings=True, session_id="unit"))
    search_without_timings = asyncio.run(server.command_interface_search("Номенклатура", session_id="unit"))
    search_with_timings = asyncio.run(server.command_interface_search("Номенклатура", include_timings=True, session_id="unit"))

    assert "_timings" not in without_timings
    assert with_timings["_timings"] == {"total_ms": 2}
    assert "_timings" not in search_without_timings
    assert search_with_timings["_timings"] == {"total_ms": 2}
    assert calls == [
        ("unit", "command_bar", {}, None),
        ("unit", "command_bar", {}, None),
        ("unit", "command_interface_search", {"query": "Номенклатура", "section": "", "max_results": 20, "window_title": ""}, None),
        ("unit", "command_interface_search", {"query": "Номенклатура", "section": "", "max_results": 20, "window_title": ""}, None),
    ]


def test_start_session_test_client_result_is_compact_and_timings_opt_in():
    from mcp_1c.server import _compact_start_test_client_result

    result = {
        "connected": True,
        "base_url": "https://example/ib",
        "port": 1538,
        "maximized": {
            "maximized": True,
            "method": "activate_only",
            "window": {"type": "Window", "title": "Start", "children": [{"type": "Form", "title": "Huge"}]},
        },
        "_timings": {"total_seconds": 12.3},
        "debug_noise": {"children": [1, 2, 3]},
    }

    compact = _compact_start_test_client_result(result)
    compact_with_timings = _compact_start_test_client_result(result, include_timings=True)

    assert compact["connected"] is True
    assert compact["maximized"]["window"]["title"] == "Start"
    assert "children" not in compact["maximized"]["window"]
    assert "debug_noise" not in compact
    assert "_timings" not in compact
    assert compact_with_timings["_timings"] == {"total_seconds": 12.3}

def test_metadata_objects_catalog_self_entry_is_searchable():
    item = _metadata_objects_catalog_self_entry()
    assert item["full_name"] == "Справочник.ИдентификаторыОбъектовМетаданных"
    assert item["type"] == "Справочник"
    assert item["name"] == "ИдентификаторыОбъектовМетаданных"
    assert item["is_group"] == "Нет"
    assert _match_metadata_object(item, "ИдентификаторыОбъектовМетаданных")
    assert _match_metadata_object(item, "metadata") is False


def test_table_row_pairs_accepts_compact_values_rows():
    from mcp_1c.server import _table_row_pairs

    row = {"values": {"Поле": "Полное имя", "Использование": "Да"}}

    assert _table_row_pairs(row) == {"Поле": "Полное имя", "Использование": "Да"}


def test_metadata_catalog_rows_from_tabular_cells_with_headers():
    rows = _metadata_catalog_rows_from_tabular_cells([
        ["Идентификаторы объектов метаданных"],
        ["Ссылка", "Синоним", "Полное имя"],
        ["? Справочник.PL_СервисыМаркета", "Сервисы", "Справочник.PL_СервисыМаркета"],
    ])
    assert rows == [{"Ссылка": "? Справочник.PL_СервисыМаркета", "Синоним": "Сервисы", "Полное имя": "Справочник.PL_СервисыМаркета"}]


def test_metadata_universal_report_filter_specs_build_full_snapshot_filters():
    expected = [
        {"field": "Пометка удаления", "comparison": "Равно", "value": "Нет"},
        {"field": "Полное имя", "comparison": "Не начинается с", "value": "Роль."},
        {"field": "Полное имя", "comparison": "Не начинается с", "value": "Подсистема."},
    ]
    assert _metadata_universal_report_filter_specs("") == expected
    assert _metadata_universal_report_filter_specs("Справочник.Номенклатура") == expected
    assert _metadata_universal_report_filter_specs("Номенклатура") == expected


def test_metadata_catalog_rows_from_universal_report_cells():
    rows = _metadata_catalog_rows_from_tabular_cells([
        ["Параметры:", "Тип объекта: Справочник", ""],
        ["Отбор:", "Пометка удаления Равно \"Нет\"", ""],
        ["Идентификатор объекта метаданных", "Имя", "Синоним", "Полное имя", "Полный синоним"],
        [
            "Номенклатура (Справочник)",
            "Номенклатура",
            "Номенклатура",
            "Справочник.Номенклатура",
            "Справочник. Номенклатура",
        ],
    ])
    assert rows == [{
        "Идентификатор объекта метаданных": "Номенклатура (Справочник)",
        "Имя": "Номенклатура",
        "Синоним": "Номенклатура",
        "Полное имя": "Справочник.Номенклатура",
        "Полный синоним": "Справочник. Номенклатура",
    }]
    item = _metadata_object_from_row(rows[0])
    assert item["presentation"] == "Номенклатура (Справочник)"
    assert item["full_name"] == "Справочник.Номенклатура"
    assert item["type"] == "Справочник"
    assert item["name"] == "Номенклатура"
    assert item["synonym"] == "Номенклатура"
    assert item["is_group"] == "Нет"




def test_metadata_catalog_rows_from_xlsx(tmp_path):
    import zipfile

    from mcp_1c.server import _metadata_catalog_rows_from_xlsx

    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    shared = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="10" uniqueCount="10">
 <si><t>Параметры:</t></si>
 <si><t>Идентификатор объекта метаданных</t></si>
 <si><t>Имя</t></si>
 <si><t>Синоним</t></si>
 <si><t>Полное имя</t></si>
 <si><t>Полный синоним</t></si>
 <si><t>Номенклатура (Справочник)</t></si>
 <si><t>Номенклатура</t></si>
 <si><t>Справочник.Номенклатура</t></si>
 <si><t>Справочник. Номенклатура</t></si>
</sst>
"""
    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData>
  <row r="1"><c r="A1" t="s"><v>0</v></c></row>
  <row r="2">
   <c r="A2" t="s"><v>1</v></c><c r="B2" t="s"><v>2</v></c><c r="C2" t="s"><v>3</v></c><c r="D2" t="s"><v>4</v></c><c r="E2" t="s"><v>5</v></c>
  </row>
  <row r="3">
   <c r="A3" t="s"><v>6</v></c><c r="B3" t="s"><v>7</v></c><c r="C3" t="s"><v>7</v></c><c r="D3" t="s"><v>8</v></c><c r="E3" t="s"><v>9</v></c>
  </row>
 </sheetData>
</worksheet>
"""
    xlsx = tmp_path / "metadata.xlsx"
    with zipfile.ZipFile(xlsx, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)

    rows = _metadata_catalog_rows_from_xlsx(xlsx)

    assert rows == [{
        "Идентификатор объекта метаданных": "Номенклатура (Справочник)",
        "Имя": "Номенклатура",
        "Синоним": "Номенклатура",
        "Полное имя": "Справочник.Номенклатура",
        "Полный синоним": "Справочник. Номенклатура",
    }]


def test_metadata_catalog_rows_from_mxl(tmp_path):
    from mcp_1c.server import _metadata_catalog_rows_from_mxl

    mxl = tmp_path / "metadata.mxl"
    mxl.write_bytes(
        b"MOXCEL\x00\x08\x00\x01\x00\x0c\x00"
        + """
{16,3,
{1,1,
{"#","Идентификатор объекта метаданных"}
},0},1,
{16,3,
{1,1,
{"#","Имя"}
},0},1,
{16,3,
{1,1,
{"#","Синоним"}
},0},1,
{16,3,
{1,1,
{"#","Полное имя"}
},0},7,0,2,0,
{16,3,
{1,1,
{"#","Полный синоним"}
},0},7,0,2,0,
{16,3,
{1,1,
{"#","Без данных"}
},0},7,0,2,0,
{20,4,
{"#",3031edd8-c3df-47b2-98ca-47f628d4ec18,
{2}
},
{1,1,
{"#","Номенклатура (Справочник)"}
},0},1,
{20,4,
{"#",3031edd8-c3df-47b2-98ca-47f628d4ec18,
{2}
},
{1,1,
{"#","Номенклатура"}
},0},1,
{20,4,
{"#",3031edd8-c3df-47b2-98ca-47f628d4ec18,
{2}
},
{1,1,
{"#","Номенклатура"}
},0},1,
{20,5,
{"#",3031edd8-c3df-47b2-98ca-47f628d4ec18,
{3}
},
{1,1,
{"#","Справочник.Номенклатура"}
},0},8,0,2,0,
{20,5,
{"#",3031edd8-c3df-47b2-98ca-47f628d4ec18,
{3}
},
{1,1,
{"#","Справочник. Номенклатура"}
},0},8,0,2,0,
{20,5,
{"#",3031edd8-c3df-47b2-98ca-47f628d4ec18,
{3}
},
{1,1,
{"#","Нет"}
},0},8,0,2,0,
""".encode()
    )

    rows = _metadata_catalog_rows_from_mxl(mxl)

    assert rows == [{
        "Идентификатор объекта метаданных": "Номенклатура (Справочник)",
        "Имя": "Номенклатура",
        "Синоним": "Номенклатура",
        "Полное имя": "Справочник.Номенклатура",
        "Полный синоним": "Справочник. Номенклатура",
        "Без данных": "Нет",
    }]

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
                    {"truncated": True, "truncated_reason": "node_limit", "type": "ТестируемаяКнопкаФормы", "name": "Button1", "title": "Кнопка"},
                    {"truncated": True, "truncated_reason": "node_limit", "type": "ТестируемоеПолеФормы", "name": "Field1", "title": "Поле"},
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
    assert summary["types_count"]["by_family"]["buttons"] == 1
    assert summary["types_count"]["by_family"]["fields"] == 1
    assert summary["types_count"]["by_family"]["forms"] == 2
    assert summary["types_count"]["by_type"]["ТестируемаяФорма"] == 2


def test_ui_tree_summary_reports_type_counts_for_flat_objects():
    from mcp_1c.server import _summarize_ui_tree

    summary = _summarize_ui_tree({
        "flat": True,
        "objects": [
            {"type": "ТестируемаяКнопкаФормы", "visible": True},
            {"type": "ТестируемаяКнопкаФормы", "visible": True},
            {"type": "ТестируемоеПолеФормы", "visible": False},
            {"type": "ТестируемаяТаблицаФормы", "visible": True},
        ],
    }, include_hidden=True)

    assert summary["flat_objects_count"] == 4
    assert summary["hidden_nodes_count"] == 1
    assert summary["types_count"]["by_family"] == {"buttons": 2, "fields": 1, "tables": 1}
    assert summary["types_count"]["by_type"]["ТестируемаяКнопкаФормы"] == 2


def test_configuration_name_version_reads_main_configuration_from_mxl(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((method, params))
        if method == "dynamic_list_sort_by_column":
            return {"ordered": True}
        if method == "table_move_row":
            return {"moved": True, "direction": params.get("direction")}
        if method == "table_current_row":
            return {"pairs": [{"key": "Это основная конфигурация", "value": "Нет"}]}
        if method == "dynamic_list_output":
            return {"tabular_documents": {"items": [{"name": "ТабличныйДокумент"}]}}
        if method == "tabular_document_text":
            raise AssertionError("configuration_name_version_from_subsystems must read saved MXL instead of UI cells")
        return {"ok": True}

    async def fake_save_to_mxl(session_id, doc_name, path_prefix, warnings, *, timeout=120):
        calls.append(("_save_output_tabular_document_to_mxl", {"session_id": session_id, "doc_name": doc_name, "path_prefix": path_prefix, "timeout": timeout}))
        warnings.append("List output MXL became parseable; path=/tmp/unit.mxl; rows=4")
        return [
            ["Имя подсистемы", "Версия", "Это основная конфигурация"],
            ["Platform42", "1.1.9.728", "Нет"],
            ["МенеджерСервиса", "1.1.56.26", "Да"],
            ["СтандартныеПодсистемы", "2.3.2.44", "Нет"],
        ]

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)
    monkeypatch.setattr(server, "_save_output_tabular_document_to_mxl", fake_save_to_mxl)

    result = asyncio.run(configuration_name_version_from_subsystems(session_id="unit"))

    assert result["found"] is True
    assert result["configuration_name"] == "МенеджерСервиса"
    assert result["configuration_version"] == "1.1.56.26"
    assert result["source_method"] == "list_output_mxl"
    assert result["warnings"] == [
        "Sorted edge list rows did not expose the main configuration; falling back to MXL list output",
        "List output MXL became parseable; path=/tmp/unit.mxl; rows=4",
    ]
    assert calls == [
        ("open_navigation_link", {"navigation_link": "e1cib/list/РегистрСведений.ВерсииПодсистем"}),
        ("dynamic_list_sort_by_column", {"name": "Список", "title": "", "column_title": "Это основная конфигурация"}),
        ("table_move_row", {"name": "Список", "title": "", "direction": "first", "steps": 1}),
        ("table_current_row", {"name": "Список", "title": ""}),
        ("table_move_row", {"name": "Список", "title": "", "direction": "last", "steps": 1}),
        ("table_current_row", {"name": "Список", "title": ""}),
        ("dynamic_list_output", {"button": "", "wait_seconds": 3, "include_text": False}),
        ("_save_output_tabular_document_to_mxl", {"session_id": "unit", "doc_name": "ТабличныйДокумент", "path_prefix": "configuration_name_version_from_subsystems", "timeout": 120}),
    ]


def test_configuration_name_version_from_subsystems_prefers_sorted_first_row(monkeypatch):
    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((method, params))
        if method == "dynamic_list_sort_by_column":
            return {"ordered": True}
        if method == "table_move_row":
            return {"moved": True, "direction": params.get("direction")}
        if method == "table_current_row":
            return {"pairs": [
                {"key": "Имя подсистемы", "value": "МенеджерСервиса"},
                {"key": "Версия", "value": "1.1.56.26"},
                {"key": "Это основная конфигурация", "value": "Да"},
            ]}
        if method == "dynamic_list_output":
            raise AssertionError("MXL fallback should not be used when sorted first row is main configuration")
        return {"ok": True}

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(configuration_name_version_from_subsystems(session_id="unit"))

    assert result["found"] is True
    assert result["configuration_name"] == "МенеджерСервиса"
    assert result["configuration_version"] == "1.1.56.26"
    assert result["source_method"] == "sorted_edge_row"
    assert result["rows_count"] == 1
    assert result["sort_attempts"] == [
        {"attempt": 1, "ordered": True, "move_direction": "first", "move_result": {"moved": True, "direction": "first"}, "read_method": "first_after_sort", "row_is_main": True, "row_main_marker": "Да"}
    ]
    assert calls == [
        ("open_navigation_link", {"navigation_link": "e1cib/list/РегистрСведений.ВерсииПодсистем"}),
        ("dynamic_list_sort_by_column", {"name": "Список", "title": "", "column_title": "Это основная конфигурация"}),
        ("table_move_row", {"name": "Список", "title": "", "direction": "first", "steps": 1}),
        ("table_current_row", {"name": "Список", "title": ""}),
    ]


def test_table_row_pairs_accepts_current_row_wrapper():
    wrapped = {
        "found": True,
        "current_row": {
            "available": True,
            "pairs": [
                {"key": "Имя подсистемы", "value": "БухгалтерияПредприятия"},
                {"key": "Версия", "value": "3.0.204.22"},
                {"key": "Это основная конфигурация", "value": "Да"},
            ],
        },
    }

    assert _find_main_configuration_in_row_pairs([_table_row_pairs(wrapped)]) == {
        "configuration_name": "БухгалтерияПредприятия",
        "configuration_version": "3.0.204.22",
        "Это основная конфигурация": "Да",
    }


def test_configuration_from_sorted_first_row_requires_main_marker():
    assert _configuration_from_sorted_first_row({
        "Имя подсистемы": "БухгалтерияПредприятия",
        "Версия": "3.0.204.22",
        "Это основная конфигурация": "Да",
    }) == {
        "configuration_name": "БухгалтерияПредприятия",
        "configuration_version": "3.0.204.22",
        "Это основная конфигурация": "Да",
    }
    assert _configuration_from_sorted_first_row({
        "Имя подсистемы": "БСП",
        "Версия": "3.1",
        "Это основная конфигурация": "Нет",
    }) == {}


def test_find_main_configuration_in_row_pairs():
    rows = [
        {"Имя подсистемы": "БСП", "Версия": "3.1", "Это основная конфигурация": "Нет"},
        {"Имя подсистемы": "БухгалтерияПредприятия", "Версия": "3.0.204.22", "Это основная конфигурация": "Да"},
    ]

    assert _find_main_configuration_in_row_pairs(rows) == {
        "configuration_name": "БухгалтерияПредприятия",
        "configuration_version": "3.0.204.22",
        "Это основная конфигурация": "Да",
    }


def test_tabular_cells_from_binary_mxl_literals_parses_subsystems_table():
    # Real binary MXL UTF-8 strings are separated by binary control bytes;
    # simulate that so the fallback extractor sees the same literal order.
    text = (
        "MOXCEL\x00Arial\x00Имя подсистемы\x00Версия\x000Это основная конфигурация"
        "\x00WВыполнена регистрация отложенных обработчиков\x00Библиотека1СПлюс"
        "\x001.0.5.5\x00Нет\x00Да\x002БухгалтерияПредприятия\x003.0.204.22\x00Да\x00Да"
    )

    assert _tabular_cells_from_binary_mxl_literals(text) == [
        ["Имя подсистемы", "Версия", "Это основная конфигурация", "Выполнена регистрация отложенных обработчиков"],
        ["Библиотека1СПлюс", "1.0.5.5", "Нет", "Да"],
        ["БухгалтерияПредприятия", "3.0.204.22", "Да", "Да"],
    ]


def test_tabular_cells_from_mxl_parses_simple_saved_cells(tmp_path):
    path = tmp_path / "subsystems.mxl"
    path.write_text(
        'MOXCEL\n'
        '{16,1,{1,1,{"#","Имя подсистемы"}},0},1,\n'
        '{16,1,{1,1,{"#","Версия"}},0},2,\n'
        '{16,1,{1,1,{"#","Это основная конфигурация"}},0},3,\n'
        '{20,2,{"#",11111111-1111-1111-1111-111111111111,{2}},{1,1,{"#","МенеджерСервиса"}},0},1,\n'
        '{20,2,{"#",11111111-1111-1111-1111-111111111111,{3}},{1,1,{"#","1.1.56.26"}},0},2,\n'
        '{20,2,{"#",11111111-1111-1111-1111-111111111111,{4}},{1,1,{"#","Да"}},0},3,\n',
        encoding="utf-8",
    )

    assert _tabular_cells_from_mxl(path) == [
        ["Имя подсистемы", "Версия", "Это основная конфигурация"],
        ["МенеджерСервиса", "1.1.56.26", "Да"],
    ]


def test_dynamic_list_output_passes_include_text_and_limits_to_bridge(monkeypatch):
    from mcp_1c.server import dynamic_list_output

    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((session_id, method, params))
        return {"ok": True}

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(dynamic_list_output(button="СписокВывестиСписок", wait_seconds=1, include_text=False, max_rows=5, max_columns=7, session_id="unit"))

    assert result == {"ok": True}
    assert calls == [("unit", "dynamic_list_output", {"button": "СписокВывестиСписок", "wait_seconds": 1, "include_text": False, "max_rows": 5, "max_columns": 7})]


def test_rag_diagnostic_reports_missing_session_snapshot():
    _SESSIONS["rag_diag_test"] = SessionState("rag_diag_test", "", Path("."), 10001, 10002, 10003, Path("/tmp/rag_diag_test"))
    try:
        diagnostic = _rag_diagnostic("rag_diag_test", None)
    finally:
        _SESSIONS.pop("rag_diag_test", None)

    assert diagnostic["diagnostics"]
    assert diagnostic["diagnostics"][0]["code"] == "snapshot_not_specified"


def test_session_status_reports_rag_notice(tmp_path):
    sess = SessionState("rag_notice_status", "", Path("."), 10011, 10012, 10013, tmp_path)
    sess.rag_notice = {"enabled": True, "message": "Summary RAG enabled automatically"}
    _SESSIONS[sess.session_id] = sess
    try:
        result = __import__("mcp_1c.server", fromlist=["session_status"]).session_status(session_id=sess.session_id)
    finally:
        _SESSIONS.pop(sess.session_id, None)

    assert result["rag_notice"] == sess.rag_notice


def test_auto_configure_summary_rag_on_start(monkeypatch, tmp_path):
    class FakeBridge:
        async def call(self, method, params, timeout=None):
            assert method == "goto_start_page"
            return {"ok": True}

    class FakeSummaryBackend:
        enabled = True
        backend = "gitlab-stream"
        project = "platform42/configurations/summary"
        ref = "beta"

        def is_summary_snapshot(self, snapshot):
            return isinstance(snapshot, str) and snapshot.startswith("summary:")

        def find_configuration(self, config_name, config_version=""):
            assert config_name == "Бухгалтерия предприятия"
            assert config_version == "3.0.204.22"
            return {
                "config_code": "bp",
                "layer": "configuration",
                "identifier": "Accounting30",
                "version": "3.0.204.22",
                "manifest": {"identifier": "Accounting30", "version": "3.0.204.22", "chunkCount": 591902},
                "warnings": [],
            }

        def find_extension(self, config_code, extension_name, extension_version=""):
            assert config_code == "bp"
            assert extension_name == "fresh"
            assert extension_version == "3.0.204.22"
            return {
                "config_code": "bp",
                "layer": "fresh",
                "role": "extension",
                "name": "fresh",
                "version": "3.0.204.22",
                "manifest": {"identifier": "Accounting30", "extensionVersion": "3.0.204.22", "chunkCount": 4531},
                "warnings": [],
            }

        def create_snapshot(self, **kwargs):
            assert kwargs["config_code"] == "bp"
            assert kwargs["extra_layers"][0]["layer"] == "fresh"
            return {
                "name": "summary:bp:configuration+fresh:Accounting30:3.0.204.22",
                "config_code": "bp",
                "identifier": "Accounting30",
                "version": "3.0.204.22",
                "summary_layers": [{"layer": "configuration"}, {"layer": "fresh"}],
            }

    fake_rag = types.SimpleNamespace(
        summary_backend=FakeSummaryBackend(),
        snapshot_has_index=lambda snapshot: True,
    )

    async def fake_configuration_name_version(*, session_id):
        assert session_id == "auto_summary"
        return {"found": True, "configuration_name": "Бухгалтерия предприятия", "configuration_version": "3.0.204.22"}

    async def fake_installed_extensions(*, session_id):
        assert session_id == "auto_summary"
        return {"extensions": [{"name": "fresh", "version": "3.0.204.22"}]}

    saved_bindings = []
    monkeypatch.setattr("mcp_1c.server._RAG", fake_rag)
    monkeypatch.setattr("mcp_1c.server._RAG_ENABLED", True)
    monkeypatch.setattr("mcp_1c.server.AUTO_SUMMARY_RAG_ON_START", True)
    monkeypatch.setattr("mcp_1c.server.configuration_name_version_from_subsystems", fake_configuration_name_version)
    monkeypatch.setattr("mcp_1c.server.installed_extensions_from_catalog", fake_installed_extensions)
    monkeypatch.setattr("mcp_1c.server._save_rag_binding", lambda base_url, snapshot: saved_bindings.append((base_url, snapshot)))

    sess = SessionState("auto_summary", "", Path("."), 10021, 10022, 10023, tmp_path, base_url="https://example.invalid/bp/")
    sess.bridge = FakeBridge()

    notice = asyncio.run(_auto_configure_summary_rag_on_start(sess))

    assert notice["enabled"] is True
    assert notice["backend"] == "configuration-summary"
    assert notice["summary_backend"]["backend"] == "gitlab-stream"
    assert notice["snapshot"] == "summary:bp:configuration+fresh:Accounting30:3.0.204.22"
    assert notice["layers"] == ["configuration", "fresh"]
    assert sess.rag_snapshot == notice["snapshot"]
    assert saved_bindings == [("https://example.invalid/bp/", notice["snapshot"])]


def test_configure_summary_rag_from_config_alias_enables_bp_fresh(tmp_path, monkeypatch):
    class FakeSummaryBackend:
        enabled = True
        backend = "gitlab-stream"
        project = "platform42/configurations/summary"
        ref = "beta"

        def find_configuration(self, config_name, config_version=""):
            assert config_name == "bp"
            assert config_version == ""
            return {
                "config_code": "bp",
                "layer": "configuration",
                "identifier": "Accounting30",
                "version": "3.0.204.22",
                "manifest": {"identifier": "Accounting30", "version": "3.0.204.22", "chunkCount": 591902},
                "warnings": [],
            }

        def find_extension(self, config_code, extension_name, extension_version=""):
            assert config_code == "bp"
            assert extension_name == "fresh"
            assert extension_version == ""
            return {
                "config_code": "bp",
                "layer": "fresh",
                "role": "extension",
                "name": "fresh",
                "version": "3.0.204.22",
                "manifest": {"identifier": "Accounting30", "extensionVersion": "3.0.204.22", "chunkCount": 4531},
                "warnings": [],
            }

        def create_snapshot(self, **kwargs):
            assert kwargs["config_code"] == "bp"
            assert kwargs["layer"] == "configuration"
            assert kwargs["extra_layers"][0]["layer"] == "fresh"
            return {
                "name": "summary:bp:configuration+fresh:Accounting30:3.0.204.22",
                "config_code": "bp",
                "identifier": "Accounting30",
                "version": "3.0.204.22",
                "summary_layers": [{"layer": "configuration"}, {"layer": "fresh"}],
            }

    fake_rag = types.SimpleNamespace(summary_backend=FakeSummaryBackend())
    saved_bindings = []
    monkeypatch.setattr("mcp_1c.server._RAG", fake_rag)
    monkeypatch.setattr("mcp_1c.server._RAG_ENABLED", True)
    monkeypatch.setattr("mcp_1c.server._save_rag_binding", lambda base_url, snapshot: saved_bindings.append((base_url, snapshot)))

    sess = SessionState("summary_alias", "", Path("."), 10021, 10022, 10023, tmp_path, base_url="https://example.invalid/bp/")
    sess.rag_enabled = False

    notice = _configure_summary_rag_from_config_alias(sess, "bp")

    assert notice["enabled"] is True
    assert notice["auto"] is False
    assert notice["requested_config"] == "bp"
    assert notice["backend"] == "configuration-summary"
    assert notice["summary_backend"]["backend"] == "gitlab-stream"
    assert notice["snapshot"] == "summary:bp:configuration+fresh:Accounting30:3.0.204.22"
    assert notice["layers"] == ["configuration", "fresh"]
    assert sess.rag_enabled is True
    assert sess.rag_snapshot == notice["snapshot"]
    assert saved_bindings == [("https://example.invalid/bp/", notice["snapshot"])]


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



def test_external_window_geometry_accepts_test_client_title_and_clamps_to_screen(monkeypatch):
    from mcp_1c import window_control

    def fake_run(args, **kwargs):
        class Result:
            pass
        result = Result()
        if args[0] == "xwininfo":
            result.stdout = (
                '0x100001 "Клиент тестирования (1С:Предприятие) - Бухгалтерия предприятия Marvin [42_19_3364]" '
                '("1cv8c" "1cv8c") 1918x1005+0+0 +0+0\n'
            )
            return result
        raise AssertionError(args)

    monkeypatch.setattr("mcp_1c.window_control.os.name", "posix")
    monkeypatch.setattr("mcp_1c.window_control.subprocess.run", fake_run)
    monkeypatch.setattr(window_control, "_screen_size", lambda display, env: (1280, 1024))

    geometry = window_control.active_1c_window_geometry(display=":99")
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


def test_screenshot_window_geometry_falls_back_to_full_display(tmp_path, monkeypatch):
    from mcp_1c import server

    sess = _session_with_client_log_dir(tmp_path)
    sess.display = ":99"
    _SESSIONS[sess.session_id] = sess

    class FakeShot:
        rgb = b"rgb"
        size = (1, 1)

    class FakeMss:
        def __init__(self):
            self.monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1200}]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def grab(self, monitor):
            assert monitor == self.monitors[0]
            return FakeShot()

    mss_module = types.ModuleType("mss")
    mss_module.__path__ = []
    mss_module.mss = FakeMss
    tools_module = types.ModuleType("mss.tools")

    def fake_to_png(rgb, size, output):
        Path(output).write_bytes(b"png")

    tools_module.to_png = fake_to_png
    mss_module.tools = tools_module

    monkeypatch.setitem(sys.modules, "mss", mss_module)
    monkeypatch.setitem(sys.modules, "mss.tools", tools_module)
    monkeypatch.setattr(server.os_support, "IS_WINDOWS", False)
    monkeypatch.setattr(server, "_active_external_1c_window_geometry", lambda display="": None)
    try:
        result = screenshot(path=str(tmp_path / "shot.png"), window=True, session_id=sess.session_id)
    finally:
        _SESSIONS.pop(sess.session_id, None)

    assert result["bytes"] == 3
    assert result["window_geometry"] is None
    assert result["fallback"] == "full_display"
    assert "geometry was not detected" in result["fallback_reason"]


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
        "Admin",
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


def test_summary_rag_config_from_42clouds_url_detects_publication_prefix():
    assert _summary_rag_config_from_42clouds_url("https://base.42clouds.com/bp/123456") == "bp"
    assert _summary_rag_config_from_42clouds_url("https://beta-base.42clouds.com/bp_base54-int") == "bp"
    assert _summary_rag_config_from_42clouds_url("https://dev-area-06.42clouds.com/bp_base732/46678") == "bp"
    assert _summary_rag_config_from_42clouds_url("https://dev-area-06.42clouds.com/ka_base7/123") == "ka"
    assert _summary_rag_config_from_42clouds_url("https://example.invalid/bp/123456") == ""
    assert _summary_rag_config_from_42clouds_url("dev-area-06.42clouds.com/bp_base732/46678") == ""


def test_effective_dump_connection_uses_direct_session_target(tmp_path):
    sess = SessionState("s", ":99", Path("/opt/1cv8/x86_64/8.5.1.1150"), 8765, 8314, 1538, tmp_path)
    sess.target_connection_mode = "server"
    sess.target_connection_value = "dev-area-01.42clouds.com/ms"

    assert _effective_dump_connection(sess, "auto", "", "") == ("server", "dev-area-01.42clouds.com/ms")


def test_effective_dump_connection_rejects_web_without_direct_target(tmp_path):
    sess = SessionState("s", ":99", Path("/opt/1cv8/x86_64/8.5.1.1150"), 8765, 8314, 1538, tmp_path, base_url="https://example.invalid/ib/")

    with pytest.raises(ValueError, match="Web /WS URLs cannot be dumped"):
        _effective_dump_connection(sess, "auto", "", "")


def test_tabular_document_save_exact_mode_falls_back_filter_by_filter(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_dangerous_bridge_call(session_id, method, params, *, timeout, danger):
        assert session_id == "unit"
        assert method == "tabular_document_save"
        assert timeout == 7
        assert danger == "tabular_document_file_save_may_block_thin_client"
        calls.append(dict(params))
        return {
            "saved": True,
            "path": params["path"],
            "format": params["format"],
            "files": [{"path": f"{params['path']}.filter{params['file_dialog_filter_index']}"}],
        }

    def fake_select_saved_file(result, format):
        filter_index = int(result["files"][0]["path"].rsplit("filter", 1)[1])
        selected = dict(result)
        selected["selected"] = result["files"][0]
        selected["saved"] = filter_index == 2
        if filter_index != 2:
            selected["error"] = "No produced file matches requested tabular document format: xlsx"
        return selected

    monkeypatch.setattr(server, "_dangerous_bridge_call_recorded", fake_dangerous_bridge_call)
    monkeypatch.setattr(server, "_select_saved_tabular_document_file", fake_select_saved_file)

    result = asyncio.run(server._tabular_document_save_filter_by_filter(
        "unit",
        {"name": "TD", "path": "out.xlsx", "format": "xlsx", "exact_filter_only": True},
        "xlsx",
        timeout=7,
        max_filter_index=3,
    ))

    assert result["saved"] is True
    assert result["fallback_used"] is True
    assert result["attempted_filter_indexes"] == [1, 2]
    assert [call["file_dialog_filter_index"] for call in calls] == [1, 2]
    assert all(call["exact_filter_only"] is True for call in calls)
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["saved"] is False
    assert result["attempts"][1]["saved"] is True


def test_tabular_document_save_filter_order_uses_likely_format_index_first():
    from mcp_1c.server import _tabular_document_save_filter_order

    assert _tabular_document_save_filter_order("xlsx", max_filter_index=5) == [1, 2, 3, 4, 5]
    assert _tabular_document_save_filter_order("mxl", max_filter_index=5) == [1, 2, 3, 4, 5]
    assert _tabular_document_save_filter_order("mxl", max_filter_index=12) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert _tabular_document_save_filter_order("mxl", max_filter_index=12, preferred_filter_indexes=[12, 1]) == [12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert _tabular_document_save_filter_order("pdf", max_filter_index=5) == [2, 1, 3, 4, 5]
    assert _tabular_document_save_filter_order("html", max_filter_index=5) == [4, 1, 2, 3, 5]
    assert _tabular_document_save_filter_order("unknown", max_filter_index=3) == [1, 2, 3]


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


def test_ui_tree_tool_schema_defaults_and_new_arguments():
    tools = _tools_by_name()
    props = tools["ui_tree"].input_schema["properties"]

    assert props["include_data"]["default"] is False
    assert props["include_rag_types"]["default"] is False
    assert props["profile"]["default"] == "navigation"
    assert props["format"]["default"] == "json"
    assert props["command_panels"]["default"] == "auto"
    assert props["group_mode"]["default"] == "include"
    assert props["name"]["default"] == ""
    assert props["type"]["default"] == ""
    assert props["title"]["default"] == ""
    assert props["parent_name"]["default"] == ""
    assert props["fields"]["default"] == "auto"
    assert "limit" not in props
    assert "max_depth" not in props


def test_ui_tree_filtering_and_presentations():
    from mcp_1c.server import (
        _apply_ui_fields,
        _dedupe_ui_nodes,
        _flat_ui_nodes,
        _outline_ui_tree,
        _to_simple_yaml,
        _transform_ui_groups,
        _ui_tree_field_names,
        _ui_tree_state_options,
        _validate_ui_tree_choice,
    )

    tree = {
        "type": "Тестируемая форма",
        "name": "",
        "title": "Форма",
        "visible": True,
        "available": True,
        "children": [
            {
                "type": "Тестируемая группа формы",
                "name": "Группа",
                "title": "Группа",
                "visible": True,
                "available": True,
                "children": [
                    {"type": "Тестируемая таблица формы", "name": "Товары", "title": "Товары", "visible": True, "available": True},
                    {"type": "Тестируемая кнопка формы", "name": "ФормаЗаписать", "title": "Записать", "visible": True, "available": True},
                ],
            }
        ],
    }

    assert [node["name"] for node in _flat_ui_nodes(tree, type="Тестируемая таблица формы")] == ["Товары"]
    assert [node["name"] for node in _flat_ui_nodes(tree, name="Товары")] == ["Товары"]
    assert [node["name"] for node in _flat_ui_nodes(tree, title="вар") ] == ["Товары"]
    flattened = _transform_ui_groups(tree, "flatten")
    assert flattened["children"][0]["name"] == "Товары"
    assert _transform_ui_groups({"type": "Тестируемая группа формы", "name": "ГруппаПустая", "children": []}, "exclude_empty") is None
    assert _dedupe_ui_nodes([{"type": "Тестируемая командная панель", "name": "Панель"}, {"type": "Тестируемая командная панель", "name": "Панель"}]) == [{"type": "Тестируемая командная панель", "name": "Панель"}]
    assert _validate_ui_tree_choice("format", "yml", {"json", "yaml", "yml"}) == "yaml"
    assert _ui_tree_field_names("minimal", "navigation") == {"type", "name", "title"}
    assert _ui_tree_state_options({"type", "name", "title"}, include_hidden=False) == {
        "include_visibility_state": True,
        "include_availability_state": False,
        "include_read_only_state": False,
        "include_current_state": False,
    }
    assert _ui_tree_state_options({"type", "name", "title"}, include_hidden=True) == {
        "include_visibility_state": False,
        "include_availability_state": False,
        "include_read_only_state": False,
        "include_current_state": False,
    }
    assert _apply_ui_fields({"type": "T", "name": "N", "data": "D", "children": []}, {"type", "name"}) == {"type": "T", "name": "N", "children": []}
    outline = _outline_ui_tree(flattened)
    assert "Тестируемая таблица формы Товары" in outline
    yaml_text = _to_simple_yaml({"objects": _flat_ui_nodes(tree, name="Товары")})
    assert "objects:" in yaml_text
    assert "name: Товары" in yaml_text


def test_ui_tree_default_params_and_filtered_flat_result(monkeypatch):
    from mcp_1c.server import ui_tree

    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((session_id, method, params))
        return {
            "found": True,
            "count": 1,
            "objects": [{"type": "Тестируемая таблица формы", "name": "Товары", "title": "Товары", "visible": True, "available": True}],
            "filter": {"name": "Товары", "title": "", "type": ""},
            "flat": True,
            "method": "fast_platform_search_objects",
            "_tree_ms": 12,
            "_nodes_serialized": 1,
            "_nodes_visited": 0,
        }

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(ui_tree(name="Товары", fields="minimal", session_id="unit"))

    assert calls == [("unit", "ui_tree", {"include_hidden": False, "include_data": False, "include_command_panels": False, "include_timings": False, "include_visibility_state": True, "include_availability_state": False, "include_read_only_state": False, "include_current_state": False, "flat": True, "name": "Товары", "title": "", "type": ""})]
    assert result["found"] is True
    assert result["count"] == 1
    assert result["objects"] == [{"type": "Тестируемая таблица формы", "name": "Товары", "title": "Товары"}]
    assert result["filter"] == {"name": "Товары", "title": "", "type": ""}
    assert result["flat"] is True
    assert result["method"] == "fast_platform_search_objects"
    assert result["metrics"]["objects_count"] == 1
    assert result["metrics"]["json_bytes"] > 0
    assert result["metrics"]["estimated_tokens"] > 0



def test_ui_tree_timings_are_opt_in(monkeypatch):
    from mcp_1c.server import ui_tree

    async def fake_bridge_call(session_id, method, params):
        return {
            "type": "Window",
            "title": "Test",
            "children": [],
            "_tree_ms": 10,
            "_nodes_serialized": 1,
            "_timings": {"active_window_ms": 1, "serialize_tree_ms": 9, "total_ms": 10},
        }

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    without_timings = asyncio.run(ui_tree(session_id="unit"))
    with_timings = asyncio.run(ui_tree(include_timings=True, session_id="unit"))

    assert "_timings" not in without_timings
    assert with_timings["_timings"]["onec"] == {"active_window_ms": 1, "serialize_tree_ms": 9, "total_ms": 10}
    assert "bridge_ms" in with_timings["_timings"]

def test_ui_tree_parent_name_is_passed_to_bridge(monkeypatch):
    from mcp_1c.server import ui_tree

    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((session_id, method, params))
        return {
            "type": "Тестируемая группа формы",
            "name": "Родитель",
            "title": "Родитель",
            "visible": True,
            "available": True,
            "root_parent_name": "Родитель",
            "children": [
                {"type": "Тестируемая кнопка формы", "name": "Кнопка", "title": "Кнопка", "visible": True, "available": True}
            ],
        }

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(ui_tree(parent_name="Родитель", format="outline", session_id="unit"))

    assert calls == [(
        "unit",
        "ui_tree",
        {"include_hidden": False, "include_data": False, "include_command_panels": False, "include_timings": False, "include_visibility_state": True, "include_availability_state": True, "include_read_only_state": True, "include_current_state": False, "parent_name": "Родитель"},
    )]
    assert result["parent_name"] == "Родитель"
    assert "Тестируемая кнопка формы Кнопка" in result["text"]


def test_ui_tree_parent_not_found_result(monkeypatch):
    from mcp_1c.server import ui_tree

    async def fake_bridge_call(session_id, method, params):
        return {"found": False, "reason": "parent_not_found", "parent_name": "НетТакого"}

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(ui_tree(parent_name="НетТакого", session_id="unit"))

    assert result["found"] is False
    assert result["reason"] == "parent_not_found"
    assert result["parent_name"] == "НетТакого"


def test_ui_tree_profile_full_enables_expensive_options(monkeypatch):
    from mcp_1c.server import ui_tree

    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append(params)
        return {"type": "Тестируемое окно клиентского приложения", "children": []}

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(ui_tree(profile="full", include_rag_types=False, session_id="unit"))

    assert calls == [{
        "include_hidden": False,
        "include_data": True,
        "include_command_panels": True,
        "include_timings": False,
        "include_visibility_state": True,
        "include_availability_state": True,
        "include_read_only_state": True,
        "include_current_state": True,
    }]
    assert result["type"] == "Тестируемое окно клиентского приложения"
    assert "metrics" in result


def test_ui_tree_rejects_unknown_modes(monkeypatch):
    from mcp_1c.server import ui_tree

    async def fake_bridge_call(session_id, method, params):
        raise AssertionError("bridge must not be called for invalid ui_tree choices")

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    with pytest.raises(ValueError, match="Unsupported ui_tree profile"):
        asyncio.run(ui_tree(profile="wide", session_id="unit"))
    with pytest.raises(ValueError, match="Unsupported ui_tree group_mode"):
        asyncio.run(ui_tree(group_mode="hide_empty", session_id="unit"))
    with pytest.raises(ValueError, match="Unsupported ui_tree command_panels"):
        asyncio.run(ui_tree(command_panels="maybe", session_id="unit"))
    with pytest.raises(ValueError, match="Unsupported ui_tree format"):
        asyncio.run(ui_tree(format="xml", session_id="unit"))


def test_redact_value_masks_secrets():
    from mcp_1c.server import _redact_value

    assert _redact_value({"password": "secret123"}) == {"password": "***"}
    assert _redact_value({"username": "esdl"}) == {"username": "esdl"}
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


def test_tabular_state_busy_detection():
    from mcp_1c.server import _tabular_state_looks_busy, _tabular_state_text

    state = {"state_display": {"display_variant": "Text", "text": "Формирование отчета..."}}
    assert "Формирование" in _tabular_state_text(state)
    assert _tabular_state_looks_busy(state) is True
    assert _tabular_state_looks_busy({"state_display": {"text": ""}}) is False


def test_tabular_documents_dimensions_picks_requested_name():
    from mcp_1c.server import _tabular_documents_dimensions

    result = {"items": [
        {"name": "Другой", "rows": 1, "columns": 2},
        {"name": "ОтчетТабличныйДокумент", "rows": 6073, "columns": 9},
    ]}
    assert _tabular_documents_dimensions(result, "ОтчетТабличныйДокумент") == (6073, 9)


def test_wait_tabular_document_ready_uses_state_and_dimensions(monkeypatch):
    from mcp_1c.server import _wait_tabular_document_ready

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        calls.append((method, params, timeout))
        if method == "tabular_document_state_display":
            if len([call for call in calls if call[0] == method]) == 1:
                return {"state_display": {"text": "Формирование отчета..."}}
            return {"state_display": {"text": ""}}
        if method == "tabular_documents":
            return {"items": [{"name": "ОтчетТабличныйДокумент", "rows": 10, "columns": 3}]}
        raise AssertionError(method)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)
    monkeypatch.setattr("mcp_1c.server.asyncio.sleep", fake_sleep)

    ready, warnings, diagnostics = asyncio.run(_wait_tabular_document_ready("unit", "ОтчетТабличныйДокумент", timeout_seconds=30, interval_seconds=1))

    assert ready is True
    assert warnings == []
    assert diagnostics["rows"] == 10
    assert diagnostics["columns"] == 3
    assert [call[0] for call in calls] == ["tabular_document_state_display", "tabular_document_state_display", "tabular_documents"]


def test_dangerous_bridge_call_returns_blocked_status_on_watchdog_timeout(monkeypatch, tmp_path):
    from mcp_1c import server
    from mcp_1c.bridge import BridgeError

    sess = SessionState(
        session_id="unit",
        display=":99",
        platform_dir=tmp_path / "platform",
        ws_port=12345,
        ibsrv_port=23456,
        testclient_port=34567,
        data_dir=tmp_path / "session-unit",
    )
    recorded = []

    async def fake_ensure_restored_session(session_id):
        assert session_id == "unit"
        return sess

    async def fake_bridge_call_recorded(session_id, method, params, *, timeout=None):
        assert (session_id, method, params, timeout) == ("unit", "tabular_document_save", {"name": "TD"}, 5)
        raise BridgeError("1C bridge command 'tabular_document_save' timed out after 5s without response")

    screenshot = {"path": "build/screenshots/watchdog_unit_tabular_document_save_20260819_174000.png", "bytes": 123}

    monkeypatch.setattr(server, "_ensure_restored_session", fake_ensure_restored_session)
    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call_recorded)
    monkeypatch.setattr(server, "_capture_dangerous_action_timeout_screenshot", lambda sess, method: screenshot)
    monkeypatch.setattr(server, "_persist_sessions", lambda: None)
    monkeypatch.setattr(server, "_record_tool", lambda session_id, method, params, result: recorded.append((session_id, method, params, result)) or result)

    result = asyncio.run(server._dangerous_bridge_call_recorded(
        "unit",
        "tabular_document_save",
        {"name": "TD"},
        timeout=5,
        danger="unit_test_danger",
    ))

    assert result["started_long_action"] is True
    assert result["interface_blocked"] is True
    assert result["reason"] == "dangerous_action_watchdog_timeout"
    assert result["blocked_action"]["danger"] == "unit_test_danger"
    assert result["blocked_action"]["screenshot"] == screenshot
    assert "base64" not in result["blocked_action"]["screenshot"]
    assert sess.blocked_action is not None
    assert recorded[0][1] == "tabular_document_save"


def test_client_busy_status_clears_blocked_action_when_probe_succeeds(monkeypatch, tmp_path):
    from mcp_1c import server

    class DummyBridge:
        def status(self):
            return {"connected": True, "pending": 0}

        async def call(self, method, params, timeout=None):
            assert (method, params, timeout) == ("active_window_summary", {}, 2)
            return {"title": "1С:Предприятие", "source": "tested_application_active_window_summary"}

    sess = SessionState(
        session_id="unit",
        display=":99",
        platform_dir=tmp_path / "platform",
        ws_port=12345,
        ibsrv_port=23456,
        testclient_port=34567,
        data_dir=tmp_path / "session-unit",
    )
    sess.bridge = DummyBridge()
    sess.blocked_action = {"method": "tabular_document_save", "started_time": server.time.time() - 3}

    async def fake_ensure_restored_session(session_id):
        assert session_id == "unit"
        return sess

    monkeypatch.setattr(server, "_ensure_restored_session", fake_ensure_restored_session)
    monkeypatch.setattr(server, "_persist_sessions", lambda: None)

    result = asyncio.run(server._client_busy_status_impl("unit", probe_timeout=2))

    assert result["responsive"] is True
    assert result["busy"] is False
    assert result["can_continue"] is True
    assert result["probe_method"] == "active_window_summary"
    assert result["active_window"]["source"] == "tested_application_active_window_summary"
    assert result["recovered_from_blocked_action"] is True
    assert sess.blocked_action is None


def test_client_busy_status_reports_blocked_action_when_probe_times_out(monkeypatch, tmp_path):
    from mcp_1c import server
    from mcp_1c.bridge import BridgeError

    class DummyBridge:
        def status(self):
            return {"connected": True, "pending": 0}

        async def call(self, method, params, timeout=None):
            assert (method, params, timeout) == ("active_window_summary", {}, 2)
            raise BridgeError("1C bridge command 'active_window_summary' timed out after 2s without response")

    sess = SessionState(
        session_id="unit",
        display=":99",
        platform_dir=tmp_path / "platform",
        ws_port=12345,
        ibsrv_port=23456,
        testclient_port=34567,
        data_dir=tmp_path / "session-unit",
    )
    sess.bridge = DummyBridge()
    sess.blocked_action = {"method": "tabular_document_save", "started_time": server.time.time() - 3}

    async def fake_ensure_restored_session(session_id):
        assert session_id == "unit"
        return sess

    monkeypatch.setattr(server, "_ensure_restored_session", fake_ensure_restored_session)

    result = asyncio.run(server._client_busy_status_impl("unit", probe_timeout=2))

    assert result["responsive"] is False
    assert result["busy"] is True
    assert result["reason"] == "bridge_probe_timeout"
    assert result["probe_method"] == "active_window_summary"
    assert result["blocked_action"]["method"] == "tabular_document_save"



def test_client_busy_status_falls_back_to_ping_for_old_manager(monkeypatch, tmp_path):
    from mcp_1c import server
    from mcp_1c.bridge import BridgeError

    calls = []

    class DummyBridge:
        def status(self):
            return {"connected": True, "pending": 0}

        async def call(self, method, params, timeout=None):
            calls.append((method, params, timeout))
            if method == "active_window_summary":
                raise BridgeError("Unknown method: active_window_summary")
            assert method == "ping"
            return {"pong": True, "manager_protocol_version": "2026.08.26.2"}

    sess = SessionState(
        session_id="unit",
        display=":99",
        platform_dir=tmp_path / "platform",
        ws_port=12345,
        ibsrv_port=23456,
        testclient_port=34567,
        data_dir=tmp_path / "session-unit",
    )
    sess.bridge = DummyBridge()

    async def fake_ensure_restored_session(session_id):
        assert session_id == "unit"
        return sess

    monkeypatch.setattr(server, "_ensure_restored_session", fake_ensure_restored_session)

    result = asyncio.run(server._client_busy_status_impl("unit", probe_timeout=2))

    assert result["responsive"] is True
    assert result["busy"] is False
    assert result["probe_method"] == "ping"
    assert result["active_window"] == {
        "source": "manager_ping",
        "ping": {"pong": True, "manager_protocol_version": "2026.08.26.2"},
    }
    assert calls == [("active_window_summary", {}, 2), ("ping", {}, 2)]

def test_metadata_objects_from_catalog_watchdog_returns_blocked_status(monkeypatch, tmp_path):
    from mcp_1c import server

    sess = SessionState(
        session_id="unit",
        display=":99",
        platform_dir=tmp_path / "platform",
        ws_port=12345,
        ibsrv_port=23456,
        testclient_port=34567,
        data_dir=tmp_path / "session-unit",
    )

    async def fake_ensure_restored_session(session_id):
        assert session_id == "unit"
        return sess

    async def fake_load(*_args, **_kwargs):
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setattr(server, "_ensure_restored_session", fake_ensure_restored_session)
    monkeypatch.setattr(server, "_load_metadata_objects_from_catalog", fake_load)
    monkeypatch.setattr(server, "_persist_sessions", lambda: None)

    result = asyncio.run(server.metadata_objects_from_catalog(
        query="Справочник.Номенклатура",
        max_results=10,
        refresh_cache=True,
        watchdog_timeout=0.01,
        session_id="unit",
    ))

    assert result["started_long_action"] is True
    assert result["interface_blocked"] is True
    assert result["reason"] == "metadata_objects_watchdog_timeout"
    assert result["blocked_action"]["method"] == "metadata_objects_from_catalog"
    assert result["blocked_action"]["danger"] == "metadata_objects_universal_report_may_block_thin_client"
    assert sess.blocked_action is not None


def test_direct_metadata_catalog_query_parses_non_deleted_rows(monkeypatch):
    from mcp_1c import server

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        assert method == "metadata_catalog_query"
        assert params == {"query": "Справочник.Номенклатура", "limit": 10}
        assert timeout == 20
        return {
            "non_deleted_filter": True,
            "rows": [{
                "Идентификатор объекта метаданных": "Номенклатура (Справочник)",
                "Имя": "Номенклатура",
                "Синоним": "Номенклатура",
                "Полное имя": "Справочник.Номенклатура",
                "Полный синоним": "Справочник. Номенклатура",
                "Пометка удаления": "Нет",
            }],
        }

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)

    rows, warnings = asyncio.run(_metadata_rows_from_direct_catalog_query("unit", 10, query="Справочник.Номенклатура"))

    assert warnings == []
    assert rows == [{
        "Идентификатор объекта метаданных": "Номенклатура (Справочник)",
        "Имя": "Номенклатура",
        "Синоним": "Номенклатура",
        "Полное имя": "Справочник.Номенклатура",
        "Полный синоним": "Справочник. Номенклатура",
        "Пометка удаления": "Нет",
    }]




def test_load_metadata_objects_builds_full_cache_from_universal_report(monkeypatch):
    from mcp_1c import server

    async def fake_universal_report(session_id, limit, *, query="", target_matches=0):
        assert session_id == "unit"
        assert limit == 10
        assert query == ""
        assert target_matches == 0
        return [
            {"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура", "Пометка удаления": "Нет"},
            {"Полное имя": "Документ.РеализацияТоваров", "Синоним": "Реализация товаров", "Пометка удаления": "Нет"},
            {"Полное имя": "Роль.ПолныеПрава", "Синоним": "Полные права", "Пометка удаления": "Нет"},
            {"Полное имя": "Подсистема.Продажи", "Синоним": "Продажи", "Пометка удаления": "Нет"},
            {"Полное имя": "Справочник.Старый", "Синоним": "Старый", "Пометка удаления": "Да"},
        ], ["universal report used"]

    async def fail_target_list(*args, **kwargs):
        raise AssertionError("target catalog list must not be used for metadata cache build")

    async def fail_direct(*args, **kwargs):
        raise AssertionError("direct metadata catalog query must not be used for metadata cache build")

    monkeypatch.setattr(server, "_metadata_rows_from_universal_report", fake_universal_report)
    monkeypatch.setattr(server, "_metadata_rows_from_target_catalog_list", fail_target_list)
    monkeypatch.setattr(server, "_metadata_rows_from_direct_catalog_query", fail_direct)

    result = asyncio.run(server._load_metadata_objects_from_catalog("unit", 10, query="Номенклатура", max_results=10))

    assert result["source_method"] == "universal_report"
    assert result["navigation_link"] == "e1cib/app/Отчет.УниверсальныйОтчет"
    assert result["warnings"] == ["universal report used"]
    assert result["partial_for_query"] is False
    assert result["rows_count"] == 5
    assert result["non_deleted_rows_count"] == 2
    assert result["objects_count"] == 3  # two real rows + explicit self-entry
    assert result["type_counts"] == {"Документ": 1, "Справочник": 2}
    assert [item["full_name"] for item in result["objects"]] == [
        "Справочник.Номенклатура",
        "Документ.РеализацияТоваров",
        "Справочник.ИдентификаторыОбъектовМетаданных",
    ]


def test_metadata_objects_from_catalog_searches_cached_universal_report_snapshot(monkeypatch):
    from mcp_1c import server

    server._METADATA_OBJECTS_CACHE.pop("unit", None)
    calls = []

    async def fake_universal_report(session_id, limit, *, query="", target_matches=0):
        calls.append((session_id, limit, query, target_matches))
        return [
            {"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура", "Пометка удаления": "Нет"},
            {"Полное имя": "Документ.РеализацияТоваров", "Синоним": "Реализация товаров", "Пометка удаления": "Нет"},
        ], []

    async def fail_direct(*args, **kwargs):
        raise AssertionError("query must search the cache, not call direct catalog query")

    monkeypatch.setattr(server, "_metadata_rows_from_universal_report", fake_universal_report)
    monkeypatch.setattr(server, "_metadata_rows_from_direct_catalog_query", fail_direct)

    first = asyncio.run(server._metadata_objects_from_catalog_impl(
        query="Справочник.Номенклатура",
        limit=10,
        max_results=10,
        refresh_cache=True,
        include_groups=False,
        session_id="unit",
    ))
    second = asyncio.run(server._metadata_objects_from_catalog_impl(
        query="Реализация",
        limit=10,
        max_results=10,
        refresh_cache=False,
        include_groups=False,
        session_id="unit",
    ))

    assert calls == [("unit", 10, "", 0)]
    assert first["cache_hit"] is False
    assert first["found"] is True
    assert [item["full_name"] for item in first["objects"]] == ["Справочник.Номенклатура"]
    assert second["cache_hit"] is True
    assert second["found"] is True
    assert [item["full_name"] for item in second["objects"]] == ["Документ.РеализацияТоваров"]
    assert second["source_method"] == "universal_report"


def test_configure_universal_report_metadata_settings_filters_and_disables_extra_fields(monkeypatch):
    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        calls.append((method, params, timeout))
        if method == "table_rows" and params["name"] == "ВыбранныеПоля":
            return {"rows": [
                {"pairs": [{"key": "Поле", "value": "Авто"}, {"key": "Использование", "value": "Да"}]},
                {"pairs": [{"key": "Поле", "value": "Синоним"}, {"key": "Использование", "value": "Да"}]},
                {"pairs": [{"key": "Поле", "value": "Полное имя"}, {"key": "Использование", "value": "Да"}]},
                {"pairs": [{"key": "Поле", "value": "Порядок коллекции"}, {"key": "Использование", "value": "Да"}]},
            ]}
        if method == "table_rows" and params["name"] == "Отборы":
            return {"rows": []}
        return {"ok": True}

    monkeypatch.setattr("mcp_1c.server._bridge_call_recorded", fake_bridge_call)

    warnings = asyncio.run(_configure_universal_report_metadata_settings("unit", "Справочник.Номенклатура"))

    assert warnings == []
    assert ("click_button", {"name": "ВсеНастройки"}, 30) in calls
    assert not any(call[0] == "table_set_field" and call[1].get("table") == "ВыбранныеПоля" for call in calls)
    assert not any(call == ("click_button", {"name": "ВыбранныеПоляВыбранныеПоля_СнятьФлажки"}, 20) for call in calls)
    assert any(call == ("table_set_field", {
        "table": "Отборы",
        "field": "Значение",
        "value": "Нет",
        "search_by_title": True,
    }, 15) for call in calls)
    assert any(call == ("table_set_field", {
        "table": "Отборы",
        "field": "Условие",
        "value": "Не начинается с",
        "search_by_title": True,
    }, 15) for call in calls)
    assert any(call == ("table_set_field", {
        "table": "Отборы",
        "field": "Значение",
        "value": "Роль.",
        "search_by_title": True,
    }, 15) for call in calls)
    assert any(call == ("table_set_field", {
        "table": "Отборы",
        "field": "Значение",
        "value": "Подсистема.",
        "search_by_title": True,
    }, 15) for call in calls)
    assert ("table_goto_row", {"name": "Отборы", "text": "Полное имя", "limit": 50, "occurrence": 1}, 10) in calls
    assert ("table_goto_row", {"name": "Отборы", "text": "Полное имя", "limit": 50, "occurrence": 2}, 10) in calls
    assert not any(call == ("table_set_field", {
        "table": "Отборы",
        "field": "Значение",
        "value": "Справочник.Номенклатура",
        "search_by_title": True,
    }, 15) for call in calls)
    assert ("click_button", {"name": "ЗакрытьИСформировать"}, 35) in calls


def test_table_goto_row_passes_occurrence_to_bridge(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        calls.append((session_id, method, params, timeout))
        return {"found": True}

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(server.table_goto_row("Полное имя", name="Отборы", limit=50, occurrence=2, session_id="unit"))

    assert result == {"found": True}
    assert calls == [("unit", "table_goto_row", {
        "name": "Отборы",
        "title": "",
        "text": "Полное имя",
        "limit": 50,
        "occurrence": 2,
    }, None)]


def test_universal_report_message_box_is_closed_before_forming(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        calls.append((method, params, timeout))
        if method == "active_window":
            return {"form_name": "MessageBox", "client_diagnostics": {"modal_form_name": "MessageBox"}}
        if method == "click_button":
            assert params == {"name": "OK", "watchdog_timeout": 30}
            return {"pressed": True}
        raise AssertionError(method)

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)
    warnings = []

    result = asyncio.run(server._close_universal_report_message_box_if_present("unit", warnings))

    assert result is True
    assert calls == [
        ("active_window", {}, 5),
        ("click_button", {"name": "OK", "watchdog_timeout": 30}, 35),
    ]
    assert warnings == ["Universal report informational MessageBox was closed before forming the report"]


def test_metadata_universal_report_primary_path_skips_settings_ui(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        calls.append((method, params, timeout))
        if method == "field_info":
            raise AssertionError("primary metadata path must not probe Universal report quick settings")
        if method == "set_field_value":
            raise AssertionError("primary metadata path must not mutate Universal report quick settings")
        if method == "active_window":
            raise AssertionError("primary metadata path must not probe active_window before forming the report")
        if method == "open_navigation_link":
            return {"opened": True, "client_diagnostics": {"has_dialog": False, "modal_form_name": "ОбщаяФорма.ФормаОтчета"}}
        return {"ok": True}

    async def fail_configure(session_id, query=""):
        raise AssertionError("primary metadata path must not open Universal report settings UI")

    async def fake_ready(session_id, name, *, timeout_seconds=75.0, interval_seconds=3.0):
        assert session_id == "unit"
        assert name == "ОтчетТабличныйДокумент"
        return True, [], {"rows": 2, "columns": 6}

    async def fake_dangerous_bridge_call(session_id, method, params, *, timeout, danger):
        assert session_id == "unit"
        calls.append((method, params, timeout, danger))
        if method == "click_button":
            assert params == {"name": "СформироватьОтчет"}
            assert timeout == 180
            assert danger == "metadata_objects_universal_report_form_may_block_thin_client"
            return {"pressed": True}
        assert method == "tabular_document_save"
        assert params["name"] == "ОтчетТабличныйДокумент"
        assert params["format"] == "mxl"
        assert params["exact_filter_only"] is True
        assert timeout == 120
        assert danger == "metadata_objects_universal_report_file_save_may_block_thin_client"
        return {"files": [{"path": params["path"]}]}

    def fake_select_saved_file(result, format):
        assert format == "mxl"
        assert result["files"][0]["path"].endswith(".mxl")
        return {"saved": True, "selected": {"path": result["files"][0]["path"]}}

    def fake_rows_from_mxl(path):
        assert str(path).endswith(".mxl")
        return [{"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура"}]

    def fake_rows_from_xlsx(path):
        raise AssertionError("primary metadata path must prefer MXL before XLSX fallback")

    async def fail_chunks(*args, **kwargs):
        raise AssertionError("primary metadata path must save XLSX, not read UI chunks")

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)
    monkeypatch.setattr(server, "_dangerous_bridge_call_recorded", fake_dangerous_bridge_call)
    monkeypatch.setattr(server, "_select_saved_tabular_document_file", fake_select_saved_file)
    monkeypatch.setattr(server, "_configure_universal_report_metadata_settings", fail_configure)
    monkeypatch.setattr(server, "_wait_tabular_document_ready", fake_ready)
    monkeypatch.setattr(server, "_metadata_catalog_rows_from_mxl", fake_rows_from_mxl)
    monkeypatch.setattr(server, "_metadata_catalog_rows_from_xlsx", fake_rows_from_xlsx)
    monkeypatch.setattr(server, "_metadata_rows_from_tabular_document_chunks", fail_chunks)

    rows, warnings = asyncio.run(server._metadata_rows_from_universal_report("unit", 10))

    assert rows == [{"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура"}]
    assert any("quick settings probing skipped" in warning for warning in warnings)
    assert any("settings UI skipped" in warning for warning in warnings)
    assert not any(method == "field_info" for method, *_rest in calls)
    assert not any(method == "set_field_value" for method, *_rest in calls)
    assert not any(method == "active_window" for method, *_rest in calls)
    assert ("click_button", {"name": "СформироватьОтчет"}, 180, "metadata_objects_universal_report_form_may_block_thin_client") in calls
    assert any(call[0] == "tabular_document_save" and call[1].get("format") == "mxl" for call in calls)
    assert any("MXL became parseable" in warning for warning in warnings)
    assert not any(call[0] == "click_button" and call[1].get("name") == "ВсеНастройки" for call in calls)


def test_metadata_universal_report_falls_back_to_xlsx_when_mxl_has_no_rows(monkeypatch):
    from mcp_1c import server

    save_formats = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        if method == "open_navigation_link":
            return {"opened": True, "client_diagnostics": {"has_dialog": False}}
        if method in {"field_info", "set_field_value", "active_window"}:
            raise AssertionError(f"primary metadata path must not call {method}")
        return {"ok": True}

    async def fake_ready(session_id, name, *, timeout_seconds=75.0, interval_seconds=3.0):
        assert session_id == "unit"
        assert name == "ОтчетТабличныйДокумент"
        return True, [], {"rows": 6202, "columns": 9}

    async def fake_dangerous_bridge_call(session_id, method, params, *, timeout, danger):
        assert session_id == "unit"
        if method == "click_button":
            return {"pressed": True}
        assert method == "tabular_document_save"
        assert danger == "metadata_objects_universal_report_file_save_may_block_thin_client"
        save_formats.append(params["format"])
        return {"files": [{"path": params["path"]}]}

    def fake_select_saved_file(result, format):
        return {"saved": True, "selected": {"path": result["files"][0]["path"]}}

    def fake_rows_from_mxl(path):
        assert str(path).endswith(".mxl")
        return []

    def fake_rows_from_xlsx(path):
        assert str(path).endswith(".xlsx")
        return [{"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура"}]

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)
    monkeypatch.setattr(server, "_dangerous_bridge_call_recorded", fake_dangerous_bridge_call)
    monkeypatch.setattr(server, "_select_saved_tabular_document_file", fake_select_saved_file)
    monkeypatch.setattr(server, "_wait_tabular_document_ready", fake_ready)
    monkeypatch.setattr(server, "_metadata_catalog_rows_from_mxl", fake_rows_from_mxl)
    monkeypatch.setattr(server, "_metadata_catalog_rows_from_xlsx", fake_rows_from_xlsx)

    rows, warnings = asyncio.run(server._metadata_rows_from_universal_report("unit", 10))

    assert rows == [{"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура"}]
    assert save_formats == ["mxl", "xlsx"]
    assert any("XLSX became parseable" in warning for warning in warnings)


def test_metadata_universal_report_quick_settings_try_labels_in_order(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        calls.append((method, params, timeout))
        if method == "field_info":
            if params["field_name"] == "Тип объекта":
                return {"found": True, "name": "КомпоновщикНастроекПользовательскиеНастройкиЭлемент0Значение"}
            if params["field_name"] == "Тип данных":
                return {"found": True, "name": "КомпоновщикНастроекПользовательскиеНастройкиЭлемент1Значение"}
            raise RuntimeError("label absent")
        if method == "set_field_value":
            return {"ok": True}
        raise AssertionError(method)

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)
    warnings = []

    result = asyncio.run(server._set_universal_report_quick_setting_by_titles(
        "unit",
        ["Тип объекта", "Тип данных", "Раздел"],
        "Справочники",
        warnings,
    ))

    assert result is True
    assert calls == [
        ("field_info", {"field_name": "Тип объекта", "search_by_title": True}, 10),
        ("set_field_value", {"field_name": "КомпоновщикНастроекПользовательскиеНастройкиЭлемент0Значение", "value": "Справочники"}, 15),
    ]
    assert warnings == []


def test_metadata_universal_report_quick_settings_keep_warnings_local_until_all_titles_fail(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        calls.append((method, params, timeout))
        if method == "field_info":
            if params["field_name"] == "Объект":
                raise RuntimeError("first label absent")
            if params["field_name"] == "Имя объекта":
                return {
                    "found": True,
                    "name": "КомпоновщикНастроекПользовательскиеНастройкиЭлемент1Значение",
                    "data_text": "ИдентификаторыОбъектовМетаданных",
                }
        raise AssertionError(method)

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)
    warnings = []

    result = asyncio.run(server._set_universal_report_quick_setting_by_titles(
        "unit",
        ["Объект", "Имя объекта"],
        "ИдентификаторыОбъектовМетаданных",
        warnings,
    ))

    assert result is True
    assert warnings == []
    assert calls == [
        ("field_info", {"field_name": "Объект", "search_by_title": True}, 10),
        ("field_info", {"field_name": "Имя объекта", "search_by_title": True}, 10),
    ]


def test_load_metadata_objects_uses_requested_limit_for_saved_report(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_universal_report(session_id, limit, *, query="", target_matches=0):
        calls.append((session_id, limit, query, target_matches))
        return [
            {"Полное имя": "Справочник.Номенклатура", "Синоним": "Номенклатура", "Пометка удаления": "Нет"},
        ], []

    monkeypatch.setattr(server, "_metadata_rows_from_universal_report", fake_universal_report)

    result = asyncio.run(server._load_metadata_objects_from_catalog("unit", 10000, query="Номенклатура", max_results=20))

    assert calls == [("unit", 10000, "", 0)]
    assert result["requested_limit"] == 10000
    assert result["effective_limit"] == 10000
    assert result["limit"] == 10000
    assert result["snapshot_truncated_by_safe_limit"] is False
    assert not any("capped" in warning for warning in result["warnings"])


def test_metadata_universal_report_chunk_read_uses_query_early_stop(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params, *, timeout=None):
        assert session_id == "unit"
        assert method == "tabular_document_text"
        calls.append(params)
        if params["row_start"] == 1:
            return {
                "cells": [
                    ["Ссылка", "Имя", "Синоним", "Полное имя", "Полный синоним", "Это группа"],
                    ["Номенклатура (Справочник)", "Номенклатура", "Номенклатура", "Справочник.Номенклатура", "Справочник. Номенклатура", "Нет"],
                ]
            }
        raise AssertionError("chunk reader did not stop after first matching chunk")

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)

    rows, warnings = asyncio.run(server._metadata_rows_from_tabular_document_chunks(
        "unit",
        "ОтчетТабличныйДокумент",
        max_rows=5000,
        total_rows=5000,
        total_columns=9,
        query="Справочник.Номенклатура",
        target_matches=1,
        chunk_size=250,
    ))

    assert len(calls) == 1
    assert rows[0]["Полное имя"] == "Справочник.Номенклатура"
    assert any("stopped after reaching requested matches" in warning for warning in warnings)


def test_click_button_uses_dangerous_watchdog_by_default(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_dangerous_call(session_id, method, params, *, timeout, danger):
        calls.append((session_id, method, params, timeout, danger))
        return {"pressed": True}

    monkeypatch.setattr(server, "_dangerous_bridge_call_recorded", fake_dangerous_call)

    result = asyncio.run(server.click_button("СформироватьОтчет", session_id="unit"))

    assert result == {"pressed": True}
    assert calls == [(
        "unit",
        "click_button",
        {"name": "СформироватьОтчет", "search_by_title": False},
        120,
        "click_button_may_start_long_ui_action",
    )]


def test_click_button_watchdog_can_be_disabled(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_bridge_call(session_id, method, params):
        calls.append((session_id, method, params))
        return {"pressed": True}

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)

    result = asyncio.run(server.click_button("OK", watchdog_timeout=0, session_id="unit"))

    assert result == {"pressed": True}
    assert calls == [("unit", "click_button", {"name": "OK", "search_by_title": False})]


def test_publish_tools_list_changed_uses_subscription_bus(monkeypatch):
    from mcp.shared.subscriptions import ToolsListChanged

    from mcp_1c import server

    events = []

    class DummySubscriptions:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr(server.mcp, "_subscriptions", DummySubscriptions())

    asyncio.run(server._publish_tools_list_changed("unit"))

    assert len(events) == 1
    assert isinstance(events[0], ToolsListChanged)


def test_startup_tools_changed_notifications_publish_after_configured_delays(monkeypatch):
    from mcp_1c import server

    calls = []

    async def fake_sleep(delay):
        calls.append(("sleep", delay))

    async def fake_publish(reason):
        calls.append(("publish", reason))

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(server, "_publish_tools_list_changed", fake_publish)

    asyncio.run(server._startup_tools_changed_notifications((0, 2)))

    assert calls == [("publish", "startup"), ("sleep", 2), ("publish", "startup")]


def test_enable_tools_list_changed_capability_sets_legacy_initialize_bit(monkeypatch):
    from mcp_1c import server

    monkeypatch.setattr(server, "_TOOLS_LIST_CHANGED_CAPABILITY_ENABLED", False)

    server._enable_tools_list_changed_capability()
    opts = server.mcp._lowlevel_server.create_initialization_options()

    assert opts.capabilities.tools is not None
    assert opts.capabilities.tools.list_changed is True


def test_ui_tree_resource_snapshot_helpers_roundtrip(tmp_path, monkeypatch):
    from mcp_1c import server

    monkeypatch.setattr(server, "UI_TREE_SNAPSHOTS_DIR", tmp_path)
    tree = {
        "type": "Тестируемая форма",
        "name": "Форма",
        "title": "Форма",
        "visible": True,
        "children": [
            {"type": "Тестируемая кнопка формы", "name": "ФормаЗаписать", "title": "Записать", "visible": True},
            {"type": "Тестируемая таблица формы", "name": "Товары", "title": "Товары", "visible": True},
        ],
    }

    stored = server._store_ui_tree_snapshot(
        tree,
        session_id="unit",
        request={"profile": "navigation", "include_hidden": False},
        metrics={"json_bytes": 123},
        preview_max_nodes=2,
    )
    loaded = server._load_ui_tree_snapshot(stored["resource_uri"])
    payload = server._ui_tree_payload_from_snapshot(loaded)

    assert stored["delivery"] == "resource"
    assert stored["resource_uri"].startswith("answer42://ui-tree/unit/")
    assert stored["summary"]["nodes_count"] == 3
    assert stored["preview"]["nodes_count"] == 2
    assert stored["preview"]["truncated"] is True
    assert "ФормаЗаписать" in stored["preview"]["text"]
    assert "Товары" not in stored["preview"]["text"]
    assert "children" not in stored["preview"]["text"]
    assert payload["index_path"] == "/"
    assert payload["children"][0]["index_path"] == "/0"
    assert "ФормаЗаписать" in payload["children"][0]["path"]


def test_ui_tree_resource_preview_respects_node_limit():
    from mcp_1c import server

    tree = server._annotate_ui_tree_paths({
        "type": "Root",
        "name": "Root",
        "children": [
            {"type": "Child", "name": "One", "children": [{"type": "Grand", "name": "Nested"}]},
            {"type": "Child", "name": "Two"},
        ],
    })

    preview = server._ui_tree_resource_preview(tree, max_nodes=1, fmt="outline")

    assert preview["nodes_count"] == 1
    assert preview["truncated"] is True
    assert "Root" in preview["text"]
    assert "One" not in preview["text"]
    assert "Nested" not in preview["text"]


def test_ui_tree_resource_tools_search_get_children_and_diff(tmp_path, monkeypatch):
    from mcp_1c import server

    monkeypatch.setattr(server, "UI_TREE_SNAPSHOTS_DIR", tmp_path)
    before = {"type": "Form", "name": "F", "children": [{"type": "Button", "name": "Save", "title": "Save"}]}
    after = {"type": "Form", "name": "F", "children": [{"type": "Button", "name": "Save", "title": "Write"}, {"type": "Field", "name": "Number", "title": "Number"}]}
    a = server._store_ui_tree_snapshot(before, session_id="unit", request={"include_hidden": False})
    b = server._store_ui_tree_snapshot(after, session_id="unit", request={"include_hidden": False})

    found = asyncio.run(server.ui_tree_resource_search(resource_uri=b["resource_uri"], name="Number", session_id="missing-session"))
    assert found["found"] is True
    assert found["objects"][0]["name"] == "Number"
    idx = found["objects"][0]["index_path"]

    node = asyncio.run(server.ui_tree_resource_get_node(resource_uri=b["resource_uri"], index_path=idx, session_id="missing-session"))
    assert node["found"] is True
    assert node["node"]["title"] == "Number"

    children = asyncio.run(server.ui_tree_resource_children(resource_uri=b["resource_uri"], depth=1, format="outline", session_id="missing-session"))
    assert children["found"] is True
    assert "Field Number" in children["text"]

    diff = asyncio.run(server.ui_tree_diff(before_resource_uri=a["resource_uri"], after_resource_uri=b["resource_uri"], session_id="missing-session"))
    assert diff["counts"]["added"] == 1
    assert diff["counts"]["changed"] == 1


def test_ui_tree_resource_tool_schema():
    tools = _tools_by_name()
    ui_props = tools["ui_tree"].input_schema["properties"]
    assert ui_props["delivery"]["default"] == "auto"
    assert ui_props["preview_max_nodes"]["default"] == 40
    for name in [
        "ui_tree_resource_search",
        "ui_tree_resource_get_node",
        "ui_tree_resource_children",
        "ui_tree_diff",
        "ui_tree_resource_action",
    ]:
        assert name in tools


def test_ui_tree_resource_auto_threshold_helpers(tmp_path, monkeypatch):
    from mcp_1c import server

    monkeypatch.setattr(server, "UI_TREE_SNAPSHOTS_DIR", tmp_path)
    monkeypatch.setattr(server, "UI_TREE_RESOURCE_AUTO_THRESHOLD_TOKENS", 20)
    monkeypatch.setattr(server, "UI_TREE_RESOURCE_AUTO_THRESHOLD_BYTES", 0)
    small = {"type": "Form", "name": "F"}
    large = {"type": "Form", "name": "F", "children": [{"type": "Field", "name": "N", "title": "X" * 100}]}

    assert server._ui_tree_should_use_resource("inline", payload=large) is False
    assert server._ui_tree_should_use_resource("resource", payload=small) is True
    assert server._ui_tree_should_use_resource("auto", payload=small) is False
    assert server._ui_tree_should_use_resource("auto", payload=large) is True

    inline = server._ui_tree_maybe_resource_response(
        small,
        inline_response=small,
        delivery="auto",
        session_id="unit",
        request={"include_hidden": False},
        metrics={"json_bytes": 10, "estimated_tokens": 5},
        preview_max_nodes=10,
        preview_format="outline",
    )
    assert inline is small

    resource = server._ui_tree_maybe_resource_response(
        large,
        inline_response=large,
        delivery="auto",
        session_id="unit",
        request={"include_hidden": False},
        metrics={"json_bytes": 1000, "estimated_tokens": 1000},
        preview_max_nodes=10,
        preview_format="outline",
    )
    assert resource["delivery"] == "resource"
    assert resource["delivery_requested"] == "auto"
    assert resource["delivery_reason"] == "auto_threshold_exceeded"
    assert resource["auto_threshold_tokens"] == 20
