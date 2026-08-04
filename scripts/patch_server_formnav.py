#!/usr/bin/env python3
"""Patch server.py to add --tool-log and Python form navigation."""
import re
import pathlib

path = pathlib.Path("/home/openclaw/.openclaw/workspace/answer42/src/mcp_1c/server.py")
text = path.read_text(encoding="utf-8")

# 1. Add --tool-log argument after --http-stateless
old_arg = '''    parser.add_argument(
        "--http-stateless",
        action="store_true",
        default=os.getenv("ONEC_MCP_HTTP_STATELESS", "1").strip().lower() in {"1", "true", "yes", "on"},
        help="use stateless StreamableHTTP mode (default); disable for resumable sessions",
    )
    args = parser.parse_args(argv)'''
new_arg = '''    parser.add_argument(
        "--http-stateless",
        action="store_true",
        default=os.getenv("ONEC_MCP_HTTP_STATELESS", "1").strip().lower() in {"1", "true", "yes", "on"},
        help="use stateless StreamableHTTP mode (default); disable for resumable sessions",
    )
    parser.add_argument(
        "--tool-log",
        default=os.getenv("ONEC_MCP_TOOL_LOG", ""),
        help="path to a rotating log file that records every MCP tool call with params, result and duration",
    )
    args = parser.parse_args(argv)'''
if old_arg in text:
    text = text.replace(old_arg, new_arg, 1)
    print("added --tool-log argument")
else:
    print("WARNING: --http-stateless block not found; --tool-log not added")

# 2. Enable tool logging in main
old_main = '''    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if args.disable_rag:'''
new_main = '''    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if args.tool_log:
        _configure_tool_logging(args.tool_log)
    if args.disable_rag:'''
if old_main in text:
    text = text.replace(old_main, new_main, 1)
    print("enabled tool logging in main")
else:
    print("WARNING: logging.basicConfig block not found; tool logging not enabled")

# 3. Replace form_next_element / form_previous_element with Python navigation
old_forms = '''@mcp.tool()
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
    return await _bridge_call_recorded(session_id, "form_previous_element", {})'''

new_forms = '''_FOCUSABLE_UI_TYPES = {
    "ТестируемоеПолеФормы",
    "ТестируемаяКнопкаФормы",
    "ТестируемаяТаблицаФормы",
    "ТестируемыйПереключатель",
    "ТестируемыйФлажок",
    "ТестируемоеПолеВводаСписком",
}


def _flatten_ui_tree(node: Any, out: list[dict[str, Any]]) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") in _FOCUSABLE_UI_TYPES and node.get("visible") and node.get("available"):
        out.append(node)
    for key in ("children", "pages"):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                _flatten_ui_tree(child, out)
    current_page = node.get("current_page")
    if isinstance(current_page, dict):
        _flatten_ui_tree(current_page, out)


async def _form_navigate(session_id: str, direction: int) -> Any:
    """Move focus to the next/previous focusable form element using the UI tree."""
    current = await current_element(session_id=session_id)
    current_info = current.get("element") if isinstance(current, dict) else None
    current_name = current_info.get("name", "") if isinstance(current_info, dict) else ""
    tree = await ui_tree(session_id=session_id, include_hidden=False)
    elements: list[dict[str, Any]] = []
    _flatten_ui_tree(tree, elements)
    if not elements:
        return {
            "performed": False,
            "reason": "no_focusable_elements",
            "error": "No focusable elements found in the active form",
        }
    index = -1
    if current_name:
        for i, elem in enumerate(elements):
            if elem.get("name") == current_name:
                index = i
                break
    if index < 0:
        index = len(elements) if direction < 0 else -1
    new_index = index + direction
    while 0 <= new_index < len(elements):
        candidate = elements[new_index]
        focus_result = await focus_object(
            name=candidate.get("name", ""), session_id=session_id
        )
        if isinstance(focus_result, dict) and focus_result.get("activated"):
            return {
                "performed": True,
                "current_element": candidate,
            }
        new_index += direction
    return {
        "performed": False,
        "reason": "no_next_focusable",
        "error": "No next/previous focusable element available",
    }


@mcp.tool()
async def form_next_element(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move focus to the next form element and return the current element.

    Implemented in Python because the 1C test-client tab-navigation method
    (ПерейтиКСледующемуЭлементу) is not available on all platform versions.
    """
    return await _form_navigate(session_id, 1)


@mcp.tool()
async def form_previous_element(*,
    session_id: Annotated[str, "Session id"] = DEFAULT_SESSION,
) -> Any:
    """Move focus to the previous form element and return the current element.

    Implemented in Python because the 1C test-client tab-navigation method
    (ПерейтиКПредыдущемуЭлементу) is not available on all platform versions.
    """
    return await _form_navigate(session_id, -1)'''

if old_forms in text:
    text = text.replace(old_forms, new_forms, 1)
    print("replaced form navigation")
else:
    print("WARNING: form_next_element block not found")

path.write_text(text, encoding="utf-8")
