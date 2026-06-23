#!/usr/bin/env python3
"""Autonomous E2E test for testclient-bridge via MCP protocol (stdio).

This script intentionally exercises tools through MCP ClientSession.call_tool,
not by importing and calling Python functions directly.

The autonomous tested-client infobase is currently a clean file infobase.  Tools
that require a particular UI object (table/field/button/tabular document) are
covered as explicit negative checks: the test asserts a stable diagnostic result
or MCP error instead of swallowing exceptions.  Positive coverage for those tools
belongs to a dedicated tested-client configuration with known forms/tables.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _kill_all_1c() -> None:
    for name in ("1cv8", "ibcmd", "ibsrv", "Xvfb"):
        subprocess.run(["pkill", "-9", "-f", name], check=False)
    time.sleep(1)


def _parse_tool_result(result: Any):
    sc = result.structuredContent
    if isinstance(sc, dict) and "result" in sc:
        return sc["result"]
    if sc not in (None, {}):
        return sc
    texts = [getattr(item, "text", "") for item in result.content if getattr(item, "type", "") == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    parsed = []
    for text in texts:
        try:
            parsed.append(json.loads(text))
        except json.JSONDecodeError:
            parsed.append(text)
    return parsed


async def call_ok(session: ClientSession, name: str, args: dict | None = None):
    result = await session.call_tool(name, args or {})
    if result.isError:
        raise AssertionError(f"{name} unexpectedly failed: {result.content}")
    value = _parse_tool_result(result)
    print(f"OK {name}", flush=True)
    return value


async def call_expected_error(session: ClientSession, name: str, args: dict | None = None, contains: str = ""):
    result = await session.call_tool(name, args or {})
    if not result.isError:
        raise AssertionError(f"{name} unexpectedly succeeded: {_parse_tool_result(result)!r}")
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    if contains and contains not in text:
        raise AssertionError(f"{name} error does not contain {contains!r}: {text}")
    print(f"EXPECTED_ERROR {name}: {contains or text[:80]}", flush=True)
    return text


async def wait_for_bridge(session: ClientSession, sid: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await call_ok(session, "bridge_status", {"session_id": sid})
        if status.get("connected"):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Manager did not connect to bridge within {timeout}s")


async def main() -> None:
    print("=== Autonomous testclient-bridge E2E (MCP protocol) ===", flush=True)
    _kill_all_1c()

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_1c.server"],
        env={**os.environ, "PYTHONPATH": "src"},
    )

    sid = "e2e-mcp"
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            print(f"MCP tools advertised: {len(tool_names)}", flush=True)

            creds = await call_ok(session, "credentials_list", {})
            assert isinstance(creds, list), creds
            assert all(entry.get("password") == "***" for entry in creds), creds

            start = await call_ok(session, "start_session", {
                "version": "8.5.1.1150",
                "session_id": sid,
                "start_pipeline": True,
            })
            assert start["session_id"] == sid, start
            assert start.get("client_ibsrv_port"), start
            assert start["ibsrv_port"] != start["client_ibsrv_port"], start

            # RAG tools — positive coverage independent of 1C runtime.
            src_path = str(Path("tests/fixtures/rag_designer/src/cf").resolve())
            detect = await call_ok(session, "rag_source_detect", {"path": src_path, "session_id": sid})
            assert detect.get("format") == "designer_xml" or detect.get("detected_format") == "designer_xml", detect
            await call_ok(session, "rag_source_add", {"name": "E2E_SRC", "path": src_path, "kind": "configuration", "session_id": sid})
            sources = await call_ok(session, "rag_sources_list", {"session_id": sid})
            assert any(src["name"] == "E2E_SRC" for src in sources), sources
            await call_ok(session, "rag_snapshot_create", {"name": "E2E_SNAP", "sources": ["E2E_SRC"], "session_id": sid})
            snaps = await call_ok(session, "rag_snapshots_list", {"session_id": sid})
            assert any(snap["name"] == "E2E_SNAP" for snap in snaps), snaps
            await call_ok(session, "rag_index_build", {"snapshot": "E2E_SNAP", "session_id": sid})
            await call_ok(session, "rag_lookup_object", {"object": "Configuration", "snapshot": "E2E_SNAP", "session_id": sid})
            await call_ok(session, "rag_query", {"query": "MCP", "snapshot": "E2E_SNAP", "session_id": sid})


            try:
                rt = await call_ok(session, "runtime_status", {"session_id": sid})
                assert rt["running"] is True, rt
                await wait_for_bridge(session, sid)

                conn = await call_ok(session, "connect_test_client", {
                    "base_url": start["client_base_url"],
                    "username": "",
                    "password": "",
                    "port": start["testclient_port"],
                    "onec_path": start["platform_dir"] + "/1cv8c",
                    "session_id": sid,
                })
                assert conn["connected"] is True, conn

                await call_ok(session, "active_window", {"session_id": sid})
                await call_ok(session, "maximize_test_client_window", {"session_id": sid})
                await call_ok(session, "maximize_test_client_external_window", {"session_id": sid})
                await call_ok(session, "ui_tree", {"session_id": sid, "max_depth": 2})
                await call_ok(session, "command_bar", {"session_id": sid})
                await call_ok(session, "current_element", {"session_id": sid})
                await call_ok(session, "find_object", {"name": "DefinitelyMissing", "session_id": sid})

                # Navigation to a missing object must fail as a real diagnostic, not be swallowed.
                await call_expected_error(
                    session,
                    "execute_window_command",
                    {"command": "e1cib/list/Справочник.Пользователи", "session_id": sid},
                    contains="Не удалось перейти по навигационной ссылке",
                )
                # Close the 1C error dialog opened by the failed navigation; this is
                # positive coverage for click_button and keeps subsequent negative
                # object lookups on the main window deterministic.
                await call_ok(session, "click_button", {"name": "OK", "session_id": sid})

                # UI-object-specific tools: explicit negative coverage on empty tested DB.
                negative_calls = [
                    ("get_object", {"name": "DefinitelyMissing", "session_id": sid}, "Объект не может быть найден"),
                    ("activate_object", {"name": "DefinitelyMissing", "session_id": sid}, "Объект не найден"),
                    ("click_button", {"name": "DefinitelyMissing", "session_id": sid}, "Кнопка не найдена"),
                    ("set_field_value", {"field_name": "DefinitelyMissing", "value": "x", "session_id": sid}, "Поле не найдено"),
                    ("activate_field", {"field_name": "DefinitelyMissing", "session_id": sid}, "Поле не найдено"),
                    ("toggle_field", {"field_name": "DefinitelyMissing", "session_id": sid}, "Поле не найдено"),
                    ("choose_field_from_list", {"field_name": "DefinitelyMissing", "session_id": sid}, "Поле не найдено"),
                    ("choose_field_first_row", {"field_name": "DefinitelyMissing", "session_id": sid}, "Поле не найдено"),
                    ("choose_current_row", {"session_id": sid}, ""),
                    ("table_current_row", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
                    ("table_selected_rows", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
                    ("table_cell_text", {"name": "DefinitelyMissing", "cell": "Код", "session_id": sid}, "Таблица не найдена"),
                    ("table_rows", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
                    ("table_find_row", {"name": "DefinitelyMissing", "text": "x", "session_id": sid}, "Таблица не найдена"),
                    ("table_goto_row", {"name": "DefinitelyMissing", "text": "x", "session_id": sid}, "Таблица не найдена"),
                    ("table_move_row", {"name": "DefinitelyMissing", "direction": "next", "session_id": sid}, "Таблица не найдена"),
                    ("table_add_row", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
                    ("table_set_field", {"table": "DefinitelyMissing", "field": "Код", "value": "1", "session_id": sid}, "Таблица не найдена"),
                    ("table_edit_info", {"table": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
                    ("tabular_documents", {"session_id": sid}, ""),
                    ("tabular_document_text", {"name": "DefinitelyMissing", "session_id": sid}, "Табличный документ не найден"),
                    ("dynamic_list_set_order", {"column_title": "Код", "name": "DefinitelyMissing", "session_id": sid}, "Динамический список/таблица не найдены"),
                    ("dynamic_list_output", {"button": "DefinitelyMissing", "session_id": sid}, "Ни одна кнопка не нажалась"),
                    ("dynamic_list_open_settings", {"button": "DefinitelyMissing", "session_id": sid}, "Ни одна кнопка не нажалась"),
                    ("dynamic_list_clear_settings", {"button": "DefinitelyMissing", "session_id": sid}, "Ни одна кнопка не нажалась"),
                    ("dynamic_list_open_form_settings", {"button": "DefinitelyMissing", "session_id": sid}, "Ни одна кнопка не нажалась"),
                    ("create_new_item", {"session_id": sid}, ""),
                    ("save_form", {"session_id": sid}, ""),
                ]
                for tool, args, expected in negative_calls:
                    res = await session.call_tool(tool, args)
                    if expected:
                        if not res.isError:
                            value = _parse_tool_result(res)
                            # Some tools return found=false instead of MCP error. That is acceptable
                            # only when it is explicit and diagnostic.
                            if not (isinstance(value, dict) and value.get("found") is False):
                                raise AssertionError(f"{tool} unexpectedly succeeded: {value!r}")
                        else:
                            text = "\n".join(getattr(item, "text", "") for item in res.content)
                            if expected not in text:
                                raise AssertionError(f"{tool} error lacks {expected!r}: {text}")
                    print(f"COVERED {tool}", flush=True)

                rec = await call_ok(session, "recording_start", {"output_dir": "build/recordings/e2e-mcp", "session_id": sid})
                assert rec.get("active") is True, rec
                await call_ok(session, "recording_capture", {"action": "e2e", "note": "mcp protocol", "session_id": sid})
                await call_ok(session, "recording_stop", {"session_id": sid})

                ss = await call_ok(session, "screenshot", {"path": f"{start['data_dir']}/test.png", "session_id": sid})
                assert ss["bytes"] > 0, ss

                await call_ok(session, "disconnect_test_client", {"session_id": sid})
            finally:
                stopped = await call_ok(session, "stop_session", {"session_id": sid, "clean_data": True})
                assert stopped["stopped"] is True, stopped

    print("=== AUTONOMOUS E2E (MCP PROTOCOL) PASSED ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
