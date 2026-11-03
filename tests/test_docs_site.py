import asyncio
import json
from pathlib import Path

from mcp_1c import server

API_RU = Path(__file__).resolve().parents[1] / "docs-site" / "api_ru.json"


def test_api_reference_has_a_russian_translation_for_every_tool_and_parameter():
    """The documentation site shows tool docs in Russian: a new tool or parameter text needs docs-site/api_ru.json."""
    translations = json.loads(API_RU.read_text(encoding="utf-8"))
    server._register_dev_tools(True)  # the site documents dev tools too
    try:
        tools = asyncio.run(server.mcp.list_tools())
    finally:
        server._register_dev_tools(False)
    missing_tools = sorted(t.name for t in tools if not translations["tools"].get(t.name, "").strip())
    texts = {
        schema.get("description", "")
        for tool in tools
        for schema in (tool.input_schema or {}).get("properties", {}).values()
    } - {""}
    missing_params = sorted(text for text in texts if not translations["params"].get(text, "").strip())
    assert not missing_tools, f"add Russian descriptions to docs-site/api_ru.json for: {missing_tools}"
    assert not missing_params, f"add Russian parameter texts to docs-site/api_ru.json for: {missing_params}"
    stale = sorted(set(translations["tools"]) - {t.name for t in tools})
    assert not stale, f"remove translations of tools that no longer exist: {stale}"
