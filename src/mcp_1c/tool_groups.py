"""Tool groups: which Answer42 tools an MCP client sees.

Every tool belongs to exactly one group.  A client always sees ``core``; other
groups are added by the server profile (``--tool-profile``), by the client's
connection URL (``?tools=lists,commands``) or at run time with the
``tool_groups`` tool.  The ``rag`` group is shown only while RAG is active.
"""

from __future__ import annotations

import re

CORE = "core"
RAG = "rag"

GROUPS: dict[str, frozenset[str]] = {
    CORE: frozenset({
        "start_session", "stop_session", "session_status", "sessions_list", "tool_groups",
        "active_window", "windows_list", "activate_window", "open_navigation_link",
        "ui_tree", "find_object", "search_objects",
        "click_button", "set_field_value", "field_info", "field_dropdown", "choose_field_from_list",
        "choose_current_row", "toggle_field", "close_form", "save_form",
        "form_state", "user_messages", "current_error_info", "client_busy_status", "wait_client_idle", "wait_form",
        "table_rows", "table_find_row", "table_current_row", "table_add_row", "table_set_field", "table_delete_row",
        "tabular_documents", "tabular_document_save", "screenshot",
    }),
    "forms": frozenset({
        "activate_field", "choose_field_first_row", "field_dropdown_close", "set_form_fields",
        "focus_object", "get_object", "current_element", "form_next_element", "form_previous_element",
        "form_default_button", "form_open_settings", "wait_object", "wait_window", "wait_form_closed",
        "close_user_messages_panel", "goto_window", "set_file_dialog_result", "clear_file_dialog_result",
        "group_state", "group_set_expanded", "group_activate_page",
        "decoration_links", "decoration_click_link", "decoration_click",
        "html_field_content", "html_field_click_link", "html_field_input",
    }),
    "tables": frozenset({
        "table_goto_row", "table_move_row", "table_cell_text", "table_selected_rows", "table_selection",
        "table_copy_row", "table_toggle_delete_mark", "table_edit", "table_edit_info", "table_set_row_values",
        "table_choose_field_from_list", "tabular_document_state_display",
    }),
    "lists": frozenset({
        "dynamic_list_output", "dynamic_list_find", "dynamic_list_sort_by_column", "dynamic_list_open_settings",
        "dynamic_list_settings_available_fields", "dynamic_list_set_settings", "dynamic_list_current_settings",
        "dynamic_list_clear_settings",
    }),
    "commands": frozenset({
        "command_interface_sections", "command_interface_commands", "command_interface_search",
        "window_command_interface", "object_command_panel", "object_context_menu",
    }),
    "ui_snapshots": frozenset({
        "ui_tree_diff", "ui_tree_resource_search", "ui_tree_resource_get_node", "ui_tree_resource_children",
        "ui_tree_resource_action",
    }),
    "metadata": frozenset({
        "metadata_objects_from_catalog", "metadata_objects_catalog_cache_clear",
        "configuration_name_version_from_subsystems", "installed_extensions_from_catalog",
        "convert_1c_guid_navigation_link",
    }),
    RAG: frozenset({
        "rag_source_add", "rag_sources_add_from_directory", "rag_source_roots_list", "rag_available_configurations",
        "rag_session_autoconfigure", "rag_sources_list", "rag_snapshot_create", "rag_snapshots_list",
        "rag_index_build", "rag_lookup_object", "rag_query", "dynamic_list_metadata_available_fields",
    }),
    "html": frozenset({
        "html_pages", "html_text", "html_query", "html_click", "html_type", "html_wait", "html_eval", "html_console",
        "html_inspector_patch", "html_download",
    }),
    "recording": frozenset({"recording_start", "recording_capture", "recording_stop"}),
    "credentials": frozenset({"credentials_check", "credentials_save", "credentials_remove", "credentials_list"}),
}

DESCRIPTIONS: dict[str, str] = {
    CORE: "sessions, windows, navigation, buttons, fields, basic table and report work (always on)",
    "forms": "more form work: focus/element navigation, waits, groups and pages, decorations, HTML document fields, file dialogs, batch field input",
    "tables": "more table work: cursor moves, cells, selection, copy/delete marks, edit mode, row batches",
    "lists": "dynamic list settings: filters, sorting, search, output to a tabular document",
    "commands": "command interface sections/commands/search, command panels and context menus",
    "ui_snapshots": "inspect saved large ui_tree snapshots (search, nodes, children, diff, actions)",
    "metadata": "configuration metadata from the live UI: metadata catalog, subsystem and extension versions",
    RAG: "configuration source RAG: lookup, search, sources and snapshots (shown only while RAG is active)",
    "html": "live web pages in HTML document fields through the embedded WebKit inspector (start_session bridges them by default): text, query, click, type, wait, console errors; html_eval only with --dev-tools",
    "recording": "evidence recording: screenshots and slides for each step, PDF report",
    "credentials": "manage saved infobase credentials",
}

ALL_GROUPS: frozenset[str] = frozenset(GROUPS)

# Profile names accepted by --tool-profile and ?tools=; old names are kept so
# existing configurations keep working.
ALIASES: dict[str, frozenset[str]] = {
    "full": ALL_GROUPS,
    "all": ALL_GROUPS,
    "ui": frozenset({CORE, "forms", "tables", "lists"}),
    "sessions": frozenset({CORE, "credentials"}),
    "windows": frozenset({CORE, "forms"}),
    "tabular": frozenset({CORE, "tables"}),
    "dynamic": frozenset({CORE, "lists"}),
}

DEFAULT_PROFILE = CORE

_TOOL_GROUP: dict[str, str] = {tool: group for group, tools in GROUPS.items() for tool in tools}


def group_of(tool: str) -> str | None:
    return _TOOL_GROUP.get(tool)


def parse_profile(profile: str | None) -> set[str]:
    """Resolve a comma-separated profile (groups and aliases) to group names; core is always included."""
    groups = {CORE}
    unknown: list[str] = []
    for part in (p.strip().lower() for p in re.split(r"[,;\s]+", profile or "") if p.strip()):
        if part in GROUPS:
            groups.add(part)
        elif part in ALIASES:
            groups.update(ALIASES[part])
        else:
            unknown.append(part)
    if unknown:
        known = ", ".join(sorted({*GROUPS, *ALIASES}))
        raise ValueError(f"unknown tool group(s): {', '.join(unknown)}; known: {known}")
    return groups


def tools_for_groups(groups: set[str] | frozenset[str]) -> set[str]:
    names: set[str] = set()
    for group in groups:
        names.update(GROUPS.get(group, ()))
    return names
