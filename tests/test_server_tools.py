import asyncio
from pathlib import Path, PureWindowsPath

from mcp_1c import platform as onec_platform
from mcp_1c.server import _normalize_connection_mode, mcp


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
    "form_next_element",
    "form_previous_element",
    "form_state",
    "form_default_button",
    "wait_form_closed",
    "object_command_panel",
    "object_context_menu",
    "window_command_interface",
    "group_expand",
    "group_collapse",
    "group_current_page",
    "group_state",
    "decoration_links",
    "decoration_click_link",
    "decoration_click",
    "table_select_all",
    "table_clear_selection",
    "table_delete_row",
    "table_copy_row",
    "table_toggle_delete_mark",
    "table_begin_edit",
    "table_end_edit",
    "table_editing_state",
}


def _tools_by_name():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


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
    assert tools["decoration_click_link"].inputSchema["properties"]["presentation"]["default"] == ""
    assert tools["start_session"].inputSchema["properties"]["connection_mode"]["default"] == "auto"
    assert tools["start_session"].inputSchema["properties"]["connection_value"]["default"] == ""


def test_platform_version_from_windows_bin_dir():
    assert onec_platform.platform_version_from_dir(PureWindowsPath(r"C:\\Program Files\\1cv8\\8.5.1.1150\\bin")) == "8.5.1.1150"
    assert onec_platform.platform_version_from_dir(Path("/opt/1cv8/x86_64/8.5.1.1150")) == "8.5.1.1150"


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
