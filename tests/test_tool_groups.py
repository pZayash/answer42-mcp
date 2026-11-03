"""Per-client tool visibility: default profile, ?tools=, tool_groups, RAG gating."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_1c import server, tool_groups


class _Session:
    def __init__(self):
        self.notified = 0

    async def send_tool_list_changed(self):
        self.notified += 1


class _Request:
    def __init__(self, query=None, headers=None):
        self.query_params = query or {}
        self.headers = headers or {}


def _context(session=None, request=None):
    return SimpleNamespace(session=session, request=request)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_TOOL_GROUPS", {tool_groups.CORE})
    monkeypatch.setattr(server, "_RAG_ENABLED", True)
    monkeypatch.setattr(server, "_SESSIONS", {})
    server._PROCESS_TOOL_GROUPS.clear()
    server._HTTP_SESSION_TOOL_GROUPS.clear()


def _call_groups(ctx, **kwargs):
    token = server._CURRENT_MCP_REQUEST.set(ctx)
    try:
        return asyncio.run(server.manage_tool_groups(**kwargs))
    finally:
        server._CURRENT_MCP_REQUEST.reset(token)


def test_every_tool_belongs_to_exactly_one_group():
    registered = set(server.mcp._tool_manager._tools) - server.DEV_TOOL_NAMES
    seen: dict[str, str] = {}
    for group, tools in tool_groups.GROUPS.items():
        for tool in tools:
            assert tool not in seen, f"{tool} is in {seen.get(tool)} and {group}"
            seen[tool] = group
    assert registered == set(seen) - server.DEV_TOOL_NAMES


def test_default_client_sees_core_only():
    visible = server._visible_tool_names(_context(_Session()))
    assert visible == set(tool_groups.GROUPS[tool_groups.CORE])
    assert "tool_groups" in visible and "dynamic_list_output" not in visible


def test_url_parameter_selects_groups_per_client():
    ctx = _context(_Session(), _Request(query={"tools": "lists,commands"}))
    visible = server._visible_tool_names(ctx)
    assert "dynamic_list_output" in visible and "command_interface_search" in visible
    assert "rag_query" not in visible


def test_bad_url_parameter_falls_back_to_default():
    ctx = _context(_Session(), _Request(query={"tools": "nonsense"}))
    assert server._visible_tool_names(ctx) == set(tool_groups.GROUPS[tool_groups.CORE])


def _http(session_id):
    return _context(_Session(), _Request(headers={"mcp-session-id": session_id}))


def test_tool_groups_enable_and_disable_per_session():
    first, second = _http("first"), _http("second")
    result = _call_groups(first, action="enable", groups="lists")
    assert "dynamic_list_output" in server._visible_tool_names(first)
    assert "dynamic_list_output" not in server._visible_tool_names(second)  # other clients unaffected
    assert next(g for g in result["groups"] if g["name"] == "lists")["visible"] is True

    _call_groups(first, action="disable", groups="lists")
    assert "dynamic_list_output" not in server._visible_tool_names(first)


def test_rag_tools_appear_only_while_rag_is_active():
    ctx = _context(_Session())
    with pytest.raises(ToolError, match="RAG is not active"):
        _call_groups(ctx, action="enable", groups="rag")
    assert "rag_query" not in server._visible_tool_names(_context(_Session(), _Request(query={"tools": "full"})))

    server._SESSIONS["s1"] = SimpleNamespace(rag_enabled=True)
    assert "rag_query" in server._visible_tool_names(ctx)

    server._RAG_ENABLED = False  # --disable-rag hides them regardless
    assert "rag_query" not in server._visible_tool_names(ctx)


def test_modern_protocol_keeps_groups_per_client_id_or_account(monkeypatch):
    """MCP 2026-07-28 and stateless HTTP have no session: key by ?client= or by the token's account."""
    monkeypatch.setattr(server, "_current_account_id", lambda: "acct-1")
    notified = []

    async def fake_publish(reason="manual"):
        notified.append(reason)

    monkeypatch.setattr(server, "_publish_tools_list_changed", fake_publish)
    first = _context(_Session(), _Request(query={"client": "claude-a"}))
    before = server._visible_tool_names(first)
    result = _call_groups(first, action="enable", groups="lists")
    assert result["scope"] == "client"
    assert "dynamic_list_output" in server._visible_tool_names(_context(_Session(), _Request(query={"client": "claude-a"})))
    assert "dynamic_list_output" not in server._visible_tool_names(_context(_Session(), _Request(query={"client": "claude-b"})))
    asyncio.run(server._notify_tool_list_changed_if_needed(first, before))
    assert notified  # delivered through the subscriptions/listen bus

    anonymous = _context(_Session(), _Request())
    assert _call_groups(anonymous, action="enable", groups="commands")["scope"] == "account"
    assert "command_interface_search" in server._visible_tool_names(_context(_Session(), _Request()))


def test_stateful_http_session_remembers_groups():
    ctx = _context(_Session(), _Request(headers={"mcp-session-id": "abc"}))
    _call_groups(ctx, action="enable", groups="tables")
    assert "table_selection" in server._visible_tool_names(ctx)


def test_unknown_group_and_action_are_rejected():
    with pytest.raises(ToolError, match="unknown tool group"):
        _call_groups(_context(_Session()), action="enable", groups="nope")
    with pytest.raises(ToolError, match="expected list, enable or disable"):
        _call_groups(_context(_Session()), action="toggle", groups="lists")


def test_list_changed_is_sent_only_when_visibility_changes():
    session = _Session()
    ctx = _context(session)
    before = server._visible_tool_names(ctx)
    asyncio.run(server._notify_tool_list_changed_if_needed(ctx, before))
    assert session.notified == 0
    server._enabled_tool_groups(ctx).add("commands")
    asyncio.run(server._notify_tool_list_changed_if_needed(ctx, before))
    assert session.notified == 1


def test_ui_tree_saved_as_resource_enables_snapshot_tools():
    ctx = _context(_Session())
    result = SimpleNamespace(content=[SimpleNamespace(text='{"resource_uri":"answer42://ui-tree/1","summary":{}}')])
    server._auto_enable_tool_groups(ctx, "ui_tree", result)
    assert "ui_tree_resource_search" in server._visible_tool_names(ctx)


def test_list_tools_handler_filters_by_client():
    ctx = _context(_Session(), _Request(query={"tools": "recording"}))
    result = asyncio.run(server._handle_list_tools_for_client(ctx, None))
    names = {tool.name for tool in result.tools}
    assert "recording_start" in names and "dynamic_list_output" not in names


def test_profile_parsing_keeps_old_names():
    assert tool_groups.parse_profile("ui") == {"core", "forms", "tables", "lists"}
    assert tool_groups.parse_profile("dynamic") == {"core", "lists"}
    assert tool_groups.parse_profile("") == {"core"}
    assert server._tool_names_for_profile("full") is None
    with pytest.raises(ValueError, match="unknown tool group"):
        tool_groups.parse_profile("core,bogus")


def test_default_profile_is_core():
    parser_default = tool_groups.DEFAULT_PROFILE
    assert parser_default == "core"


def test_stateful_http_state_survives_new_session_objects_per_request():
    """The SDK builds a new session object for every stateful HTTP request."""
    headers = {"mcp-session-id": "client-1"}
    _call_groups(_context(_Session(), _Request(headers=headers)), action="enable", groups="lists")
    next_request = _context(_Session(), _Request(headers=headers))
    assert "dynamic_list_output" in server._visible_tool_names(next_request)
    other_client = _context(_Session(), _Request(headers={"mcp-session-id": "client-2"}))
    assert "dynamic_list_output" not in server._visible_tool_names(other_client)


def test_stdio_groups_persist_across_requests():
    """stdio serves one client per process; each request has a fresh session object."""
    _call_groups(_context(_Session()), action="enable", groups="recording")
    assert "recording_start" in server._visible_tool_names(_context(_Session()))


def test_static_mode_lists_the_profile_without_gating(monkeypatch):
    """--tool-groups off: the previous static list; rag not gated, no tool_groups."""
    monkeypatch.setattr(server, "_DYNAMIC_TOOL_GROUPS", False)
    monkeypatch.setattr(server, "_DEFAULT_TOOL_GROUPS", set(tool_groups.ALL_GROUPS))
    visible = server._visible_tool_names(_context(_Session()))
    assert "rag_query" in visible and "dynamic_list_output" in visible
    assert "tool_groups" not in visible
    with pytest.raises(ToolError, match="switched off"):
        _call_groups(_context(_Session()), action="list")


def test_tool_groups_switch_and_default_profile_follow_the_mode(monkeypatch):
    captured = {}

    def fake_configure(profile):
        captured["profile"] = profile
        raise SystemExit(0)

    monkeypatch.setattr(server, "_DYNAMIC_TOOL_GROUPS", True)
    monkeypatch.setattr(server, "_configure_default_tool_profile", fake_configure)
    monkeypatch.delenv("ONEC_MCP_TOOL_PROFILE", raising=False)
    for argv, expected_mode, expected_profile in (
        (["--tool-groups", "off"], False, "full"),
        ([], True, "core"),
        (["--tool-groups", "off", "--tool-profile", "core,lists"], False, "core,lists"),
    ):
        with pytest.raises(SystemExit):
            server.main(argv)
        assert server._DYNAMIC_TOOL_GROUPS is expected_mode
        assert captured["profile"] == expected_profile
