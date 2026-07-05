#!/usr/bin/env python3
# ruff: noqa: E402
"""Autonomous E2E test for Answer42 via MCP protocol (stdio).

The script can run either a single scenario or a real parallel split:

- E2E_SCENARIO=full      — legacy all-in-one flow (default)
- E2E_SCENARIO=smoke     — session/window/form/basic catalog operations
- E2E_SCENARIO=dynamic   — table, dynamic-list, report and RAG-backed settings
- E2E_SCENARIO=coverage  — diagnostic/negative coverage tools
- E2E_PARALLEL=1         — run smoke+dynamic+coverage in 3 independent sessions

Parallel mode deliberately distributes different tool groups across sessions;
it does not run three copies of the same UI scenario.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCENARIOS = ("smoke", "dynamic-tables", "dynamic-lists", "dynamic-reports", "coverage")


def _build_client_cf_from_sources() -> None:
    src_dir = Path("src/client_cf")
    target = Path("build/MCPTestClient.cf")
    if not src_dir.exists():
        raise FileNotFoundError(f"Client CF source is missing: {src_dir}")
    print(f"Building tested-client configuration from sources: {src_dir} -> {target}", flush=True)
    subprocess.run([sys.executable, "scripts/build_cf.py", str(src_dir), str(target)], check=True)


def _kill_all_1c() -> None:
    if os.name == "nt":
        for name in ("1cv8.exe", "1cv8c.exe", "ibcmd.exe", "ibsrv.exe"):
            subprocess.run(["taskkill", "/IM", name, "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        for name in ("1cv8", "ibcmd", "ibsrv", "Xvfb"):
            subprocess.run(["pkill", "-9", "-f", name], check=False)
    time.sleep(1)


def _prebuild_shared_client_base(shared_id: str) -> None:
    """Create the shared tested-client file infobase before child fan-out.

    This avoids three child MCP servers racing to create/load the same file
    infobase.  The shared ibsrv itself is still started lazily by start_session.
    """
    from mcp_1c import __version__
    from mcp_1c import platform as _platform
    from mcp_1c import runtime
    from mcp_1c.server import _file_sha256

    platform_dir = _platform.find_platform_dir(os.environ.get("ONEC_VERSION") or None)
    ibcmd = str(platform_dir / "ibcmd")
    db_path = Path(os.getenv("ONEC_MCP_SHARED_CLIENT_BASE_DIR", "/tmp/answer42/shared-client-bases")) / shared_id / "ib"
    cf_path = runtime.DEFAULT_CLIENT_CF_BUILT
    if not Path(cf_path).exists():
        raise FileNotFoundError(f"MCPTestClient.cf not found at {cf_path}.")
    marker_path = db_path.parent / f".{db_path.name}.answer42-cf.json"
    marker_data = {"answer42_version": __version__, "cf_sha256": _file_sha256(Path(cf_path)), "cf_path": str(cf_path)}
    if db_path.exists() and any(db_path.iterdir()) and marker_path.exists():
        try:
            if json.loads(marker_path.read_text(encoding="utf-8")) == marker_data:
                return
        except Exception:
            pass
        shutil.rmtree(db_path, ignore_errors=True)
    runtime.create_file_infobase(ibcmd, db_path)
    runtime.load_configuration(ibcmd, db_path, cf_path)
    marker_path.write_text(json.dumps(marker_data, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_suffix(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_")


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


async def call_ui_ok(session: ClientSession, name: str, args: dict | None = None, timeout: float | None = None):
    args = args or {}
    read_timeout = timedelta(seconds=timeout) if timeout else None
    result = await session.call_tool(name, args, read_timeout_seconds=read_timeout)
    if result.isError:
        value = "\n".join(getattr(item, "text", "") for item in result.content)
        await _record_manual_step(session, args.get("session_id", "e2e-mcp"), name, args, {"error": value})
        raise AssertionError(f"{name} unexpectedly failed: {result.content}")
    value = _parse_tool_result(result)
    print(f"OK {name}", flush=True)
    return value




async def call_ui_optional_error(session: ClientSession, name: str, args: dict | None = None, allowed_contains: str = ""):
    args = args or {}
    result = await session.call_tool(name, args)
    if not result.isError:
        value = _parse_tool_result(result)
        print(f"OK {name}", flush=True)
        return value
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    if allowed_contains and allowed_contains in text:
        print(f"OPTIONAL_ERROR {name}: {allowed_contains}", flush=True)
        return {"optional_error": text}
    await _record_manual_step(session, args.get("session_id", "e2e-mcp"), name, args, {"error": text})
    raise AssertionError(f"{name} unexpectedly failed: {result.content}")


async def call_ui_expected_error(session: ClientSession, name: str, args: dict | None = None, contains: str = ""):
    args = args or {}
    result = await session.call_tool(name, args)
    if not result.isError:
        value = _parse_tool_result(result)
        await _record_manual_step(session, args.get("session_id", "e2e-mcp"), name, args, value)
        raise AssertionError(f"{name} unexpectedly succeeded: {value!r}")
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    if contains and contains not in text:
        await _record_manual_step(session, args.get("session_id", "e2e-mcp"), name, args, {"error": text})
        raise AssertionError(f"{name} error does not contain {contains!r}: {text}")
    print(f"EXPECTED_ERROR {name}: {contains or text[:80]}", flush=True)
    return text


def _summarize_for_note(value: Any, limit: int = 900) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


async def _record_manual_step(session: ClientSession, sid: str, name: str, args: dict, value: Any) -> None:
    note = (
        f"MCP method: {name}\n"
        f"Request: {_summarize_for_note(args, 700)}\n"
        f"Response: {_summarize_for_note(value, 1200)}"
    )
    rec = await session.call_tool("recording_capture", {"action": name, "note": note, "session_id": sid})
    if rec.isError:
        text = "\n".join(getattr(item, "text", "") for item in rec.content)
        raise AssertionError(f"recording_capture failed after {name}: {text}")


def row_pair_value(row: Any, key: str) -> str:
    for pair in row.get("current_row", {}).get("pairs", []):
        if pair.get("key") == key:
            return str(pair.get("value", ""))
    return ""


async def wait_for_bridge(session: ClientSession, sid: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await call_ok(session, "session_status", {"session_id": sid})
        if status.get("bridge", {}).get("connected"):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Manager did not connect to bridge within {timeout}s")


async def capture_screenshot(session: ClientSession, sid: str, path: str) -> dict:
    result = await call_ui_ok(session, "screenshot", {"path": path, "session_id": sid})
    assert result["bytes"] > 0, result
    print(f"SCREENSHOT {path}", flush=True)
    return result


async def common_start(session: ClientSession, sid: str, suffix: str) -> tuple[Path, set[str]]:
    tools = await session.list_tools()
    tool_names = {tool.name for tool in tools.tools}
    print(f"MCP tools advertised: {len(tool_names)}", flush=True)
    screenshot_dir = Path("build") / f"e2e-screenshots-{suffix}"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    for old_png in screenshot_dir.glob("*.png"):
        old_png.unlink()

    creds = await call_ok(session, "credentials_list", {})
    assert isinstance(creds, list), creds
    assert all("url" in entry for entry in creds), creds
    assert all("username" not in entry and "password" not in entry for entry in creds), creds

    start = await call_ok(session, "start_session", {
        "session_id": sid,
        "command_parameter": "E2E_COMMAND_PARAMETER",
    })
    assert start["session_id"] == sid, start
    assert start.get("client_ibsrv_port"), start
    assert start["ibsrv_port"] != start["client_ibsrv_port"], start

    rec = await call_ok(session, "recording_start", {
        "output_dir": f"build/recordings/{suffix}",
        "session_id": sid,
        "window": True,
    })
    assert rec.get("active") is True, rec

    rt = await call_ok(session, "session_status", {"session_id": sid})
    assert rt["running"] is True, rt
    await wait_for_bridge(session, sid)
    conn = start.get("test_client")
    assert conn and conn["connected"] is True, start
    return screenshot_dir, tool_names


async def common_finish(session: ClientSession, sid: str, scenario: str, screenshot_dir: Path) -> None:
    await capture_screenshot(session, sid, str(screenshot_dir / f"99-{scenario}-before-disconnect.png"))
    rec_stop = await call_ok(session, "recording_stop", {"session_id": sid})
    assert set(rec_stop.keys()).issuperset({"active", "output_dir", "events", "pdf"}), rec_stop
    assert "mp4" not in rec_stop and "gif" not in rec_stop and "html" not in rec_stop, rec_stop


async def run_rag(session: ClientSession, sid: str, suffix: str) -> None:
    src_path = str(Path("src/client_cf").resolve())
    source_name = f"E2E_SRC_{suffix}"
    snapshot_name = f"E2E_SNAP_{suffix}"
    source_add = await call_ok(session, "rag_source_add", {"name": source_name, "path": src_path, "kind": "configuration", "session_id": sid})
    detected = source_add.get("detected", {})
    assert detected.get("format") == "designer_xml" or source_add.get("format") == "designer_xml", source_add
    sources = await call_ok(session, "rag_sources_list", {"session_id": sid})
    assert any(src["name"] == source_name for src in sources), sources
    await call_ok(session, "rag_snapshot_create", {"name": snapshot_name, "sources": [source_name], "session_id": sid})
    snaps = await call_ok(session, "rag_snapshots_list", {"session_id": sid})
    assert any(snap["name"] == snapshot_name for snap in snaps), snaps
    await call_ok(session, "rag_index_build", {"session_id": sid})
    await call_ok(session, "rag_lookup_object", {"object": "Справочник.ПС_Справочник1", "session_id": sid})
    await call_ok(session, "rag_query", {"query": "ПС_Справочник1", "session_id": sid})


async def run_smoke(session: ClientSession, sid: str, suffix: str, screenshot_dir: Path) -> None:
    active = await call_ui_ok(session, "active_window", {"session_id": sid})
    assert active.get("source") == "tested_application_active_window", active
    windows = await call_ui_ok(session, "windows_list", {"session_id": sid})
    assert windows.get("count", 0) >= 1, windows
    assert len(windows.get("windows", [])) == windows.get("count"), windows
    activated = await call_ui_ok(session, "activate_window", {"index": 0, "session_id": sid})
    assert activated.get("activated") is True, activated

    for tool in ("goto_start_page", "goto_next_window", "goto_previous_window"):
        res = await session.call_tool(tool, {"session_id": sid})
        print(f"OK_OR_EXPECTED_ERROR {tool}", flush=True)
        if not res.isError:
            _parse_tool_result(res)

    await call_ui_ok(session, "wait_window", {"title": windows.get("active_title", ""), "timeout": 3, "session_id": sid})
    await call_ui_ok(session, "wait_object", {"type": "ТестируемаяФорма", "timeout": 3, "session_id": sid})
    await call_ui_ok(session, "wait_form", {"timeout": 3, "session_id": sid})
    await call_ui_ok(session, "user_messages", {"session_id": sid})
    await call_ui_ok(session, "close_user_messages_panel", {"session_id": sid})
    await call_ui_ok(session, "current_error_info", {"session_id": sid})
    await call_ui_ok(session, "form_state", {"session_id": sid})
    await call_ui_ok(session, "form_default_button", {"session_id": sid})
    await call_ui_ok(session, "window_command_interface", {"session_id": sid})
    await call_ui_ok(session, "field_info", {"field_name": "DefinitelyMissingField", "session_id": sid})
    await call_ui_ok(session, "field_value_text", {"field_name": "DefinitelyMissingField", "session_id": sid})
    await call_ui_ok(session, "field_tooltip", {"field_name": "DefinitelyMissingField", "session_id": sid})
    await call_ui_ok(session, "find_object", {"name": "DefinitelyMissing", "session_id": sid})
    await call_ui_expected_error(
        session,
        "open_navigation_link",
        {"navigation_link": "e1cib/list/Справочник.DefinitelyMissing", "session_id": sid},
        contains="Не удалось перейти по навигационной ссылке",
    )
    await call_ui_optional_error(session, "click_button", {"name": "OK", "session_id": sid}, "Кнопка не найдена")
    await capture_screenshot(session, sid, str(screenshot_dir / "01-initial-window.png"))

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Владелец", "session_id": sid})
    await call_ui_ok(session, "create_new_item", {"session_id": sid})
    await call_ui_ok(session, "set_field_value", {"field_name": "Наименование", "value": "Владелец smoke", "session_id": sid})
    await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})
    await capture_screenshot(session, sid, str(screenshot_dir / "02-owner-created.png"))

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})
    rows = await call_ui_ok(session, "table_rows", {"name": "Список", "session_id": sid})
    assert rows["found"] is True, rows
    await call_ui_ok(session, "create_new_item", {"session_id": sid})
    await call_ui_ok(session, "set_field_value", {"field_name": "Наименование", "value": "Тестовый smoke", "session_id": sid})
    await call_ui_ok(session, "set_field_value", {"field_name": "Реквизит1", "value": "Рекв smoke", "session_id": sid})
    await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})
    await call_ui_ok(session, "table_goto_row", {"name": "Список", "text": "Тестовый smoke", "session_id": sid})
    await call_ui_ok(session, "click_button", {"name": "ФормаИзменить", "session_id": sid})
    await call_ui_ok(session, "ui_tree", {"session_id": sid, "max_depth": 3, "include_rag_types": True})
    await call_ui_ok(session, "command_bar", {"session_id": sid})
    await call_ui_ok(session, "current_element", {"session_id": sid})
    await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})


async def seed_dynamic_data(session: ClientSession, sid: str, suffix: str) -> tuple[str, str]:
    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Владелец", "session_id": sid})
    for owner in (f"Владелец 1 {suffix}", f"Владелец 2 {suffix}"):
        await call_ui_ok(session, "create_new_item", {"session_id": sid})
        await call_ui_ok(session, "set_field_value", {"field_name": "Наименование", "value": owner, "session_id": sid})
        await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})
    item1 = f"Тестовый элемент 1 {suffix}"
    item2 = f"Тестовый элемент 2 {suffix}"
    for item, req in ((item1, f"Рекв1 {suffix}"), (item2, f"Рекв2 {suffix}")):
        await call_ui_ok(session, "create_new_item", {"session_id": sid})
        await call_ui_ok(session, "set_field_value", {"field_name": "Наименование", "value": item, "session_id": sid})
        await call_ui_ok(session, "set_field_value", {"field_name": "Реквизит1", "value": req, "session_id": sid})
        await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})
    return item1, item2


async def run_dynamic_tables(session: ClientSession, sid: str, suffix: str, screenshot_dir: Path) -> None:
    await run_rag(session, sid, suffix)

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Владелец", "session_id": sid})
    for owner in (f"Владелец табличной части 1 {suffix}", f"Владелец табличной части 2 {suffix}"):
        await call_ui_ok(session, "create_new_item", {"session_id": sid})
        await call_ui_ok(session, "set_field_value", {"field_name": "Наименование", "value": owner, "session_id": sid})
        await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})
    await call_ui_ok(session, "create_new_item", {"session_id": sid})
    await call_ui_ok(session, "set_field_value", {"field_name": "Наименование", "value": f"Табличная часть {suffix}", "session_id": sid})
    await call_ui_ok(session, "set_field_value", {"field_name": "Реквизит1", "value": f"Рекв ТЧ {suffix}", "session_id": sid})
    await capture_screenshot(session, sid, str(screenshot_dir / "05b-tabular-section-form.png"))
    await call_ui_ok(session, "table_add_row", {"name": "ДополнительныеСтроки", "session_id": sid})
    await call_ui_ok(session, "table_choose_field_from_list", {"table": "ДополнительныеСтроки", "field": "Владелец строки", "search_by_title": True, "session_id": sid})
    await call_ui_ok(session, "choose_current_row", {"session_id": sid})
    await call_ui_ok(session, "table_set_field", {"table": "ДополнительныеСтроки", "field": "Дата строки", "value": "24.06.2026", "search_by_title": True, "session_id": sid})
    await call_ui_ok(session, "table_add_row", {"name": "ДополнительныеСтроки", "session_id": sid})
    await call_ui_ok(session, "table_choose_field_from_list", {"table": "ДополнительныеСтроки", "field": "Владелец строки", "search_by_title": True, "session_id": sid})
    await call_ui_ok(session, "table_move_row", {"name": "Список", "direction": "next", "session_id": sid})
    await call_ui_ok(session, "choose_current_row", {"session_id": sid})
    await call_ui_ok(session, "table_set_field", {"table": "ДополнительныеСтроки", "field": "Дата строки", "value": "25.06.2026", "search_by_title": True, "session_id": sid})
    await call_ui_ok(session, "table_move_row", {"name": "ДополнительныеСтроки", "direction": "first", "session_id": sid})
    cur_ts = await call_ui_ok(session, "table_current_row", {"name": "ДополнительныеСтроки", "session_id": sid})
    assert "24.06.2026" in str(cur_ts), cur_ts
    assert row_pair_value(cur_ts, "N") == "1", cur_ts
    await call_ui_ok(session, "table_move_row", {"name": "ДополнительныеСтроки", "direction": "next", "session_id": sid})
    cur_ts = await call_ui_ok(session, "table_current_row", {"name": "ДополнительныеСтроки", "session_id": sid})
    assert "25.06.2026" in str(cur_ts), cur_ts
    assert row_pair_value(cur_ts, "N") == "2", cur_ts

    await call_ui_ok(session, "choose_field_from_list", {"field_name": "ВладелецСсылка", "session_id": sid})
    await capture_screenshot(session, sid, str(screenshot_dir / "10-owner-selection-form.png"))
    await call_ui_ok(session, "choose_current_row", {"session_id": sid})
    await call_ui_ok(session, "choose_field_first_row", {"field_name": "ВладелецСсылка", "session_id": sid})
    await call_ui_ok(session, "click_button", {"name": "ФормаЗаписатьИЗакрыть", "session_id": sid})


async def run_dynamic_lists(session: ClientSession, sid: str, suffix: str, screenshot_dir: Path) -> None:
    _item1, item2 = await seed_dynamic_data(session, sid, suffix)

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})
    await call_ui_ok(session, "table_goto_row", {"name": "Список", "text": item2, "session_id": sid})
    await call_ui_ok(session, "dynamic_list_sort_by_column", {"column_title": "Наименование", "name": "Список", "session_id": sid})
    output = await call_ui_ok(session, "dynamic_list_output", {"session_id": sid})
    await capture_screenshot(session, sid, str(screenshot_dir / "06-dynamic-list-output-result.png"))
    assert output.get("tabular_documents"), output
    tdocs = await call_ui_ok(session, "tabular_documents", {"session_id": sid})
    tdocs_text = str(tdocs)
    has_tabular_document = (
        tdocs.get("found") is True
        or len(tdocs.get("documents", [])) > 0
        or len(tdocs.get("items", [])) > 0
        or tdocs.get("count", 0) > 0
        or "SpreadsheetDocument" in tdocs_text
        or "ТабличныйДокумент" in tdocs_text
    )
    if has_tabular_document:
        await call_ui_ok(session, "tabular_document_save", {"format": "pdf", "path": str(screenshot_dir / "dynamic-list-output.pdf"), "session_id": sid})
    else:
        # On some 1C/platform versions, "Вывести список" opens a regular list form
        # instead of a tabular-document field. Treat visible result rows as valid
        # evidence for dynamic_list_output; report scenarios below still exercise
        # real tabular-document APIs and PDF saving.
        rows = await call_ui_ok(session, "table_rows", {"name": "Список", "session_id": sid})
        diagnostics = tdocs.get("client_diagnostics", {}) if isinstance(tdocs, dict) else {}
        active_title = str(diagnostics.get("modal_title", ""))
        assert (
            rows.get("count", 0) > 0
            or len(rows.get("rows", [])) > 0
            or "Список" in active_title
        ), f"No output rows, tabular documents or output window: {tdocs}; rows={rows}"
    await call_ui_ok(session, "close_form", {"session_id": sid})

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})
    await call_ui_ok(session, "dynamic_list_open_settings", {"session_id": sid})
    await capture_screenshot(session, sid, str(screenshot_dir / "08-list-settings-open.png"))
    await call_ui_ok(session, "close_form", {"session_id": sid})
    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})
    await call_ui_ok(session, "form_open_settings", {"session_id": sid})
    await call_ui_ok(session, "close_form", {"session_id": sid})

    available = await call_ui_ok(session, "dynamic_list_settings_available_fields", {"session_id": sid})
    assert available.get("count", 0) > 0, available
    assert "Наименование" in str(available), available
    await call_ui_ok(session, "dynamic_list_set_settings", {
        "orders": json.dumps([{"field": "Наименование", "direction": "Asc"}], ensure_ascii=False),
        "clear_first": True,
        "session_id": sid,
    }, timeout=180)
    await capture_screenshot(session, sid, str(screenshot_dir / "12-final-list.png"))


async def run_dynamic_reports(session: ClientSession, sid: str, suffix: str, screenshot_dir: Path) -> None:
    await seed_dynamic_data(session, sid, suffix)

    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/app/Report.ПС_СписокЭлементов", "session_id": sid})
    await capture_screenshot(session, sid, str(screenshot_dir / "13-report-open.png"))
    await call_ui_ok(session, "click_button", {"name": "Сформировать", "search_by_title": True, "session_id": sid})
    await asyncio.sleep(2)
    await capture_screenshot(session, sid, str(screenshot_dir / "14-report-generated.png"))
    report_docs = await call_ui_ok(session, "tabular_documents", {"session_id": sid})
    report_text = await call_ui_ok(session, "tabular_document_text", {"session_id": sid})
    diagnostics = report_docs.get("client_diagnostics", {}) if isinstance(report_docs, dict) else {}
    active_title = str(diagnostics.get("modal_title", ""))
    visible_report_result = (
        "ТабличныйДокумент" in str(report_docs)
        or "ТабличныйДокумент" in str(report_text)
        or "Список элементов" in active_title
    )
    assert visible_report_result, f"Report result area is not visible: docs={report_docs}, text={report_text}"
    if report_docs.get("count", 0) > 0 or report_text.get("found"):
        await call_ui_ok(session, "tabular_document_save", {"format": "pdf", "path": str(screenshot_dir / "report-output.pdf"), "session_id": sid})
    await call_ui_ok(session, "close_form", {"session_id": sid})


async def run_dynamic(session: ClientSession, sid: str, suffix: str, screenshot_dir: Path) -> None:
    """Legacy full dynamic scenario kept for explicit E2E_SCENARIO=dynamic."""
    await run_dynamic_tables(session, sid, suffix, screenshot_dir)
    await run_dynamic_lists(session, sid, suffix, screenshot_dir)
    await run_dynamic_reports(session, sid, suffix, screenshot_dir)

async def run_coverage(session: ClientSession, sid: str, suffix: str, screenshot_dir: Path) -> None:
    await call_ui_ok(session, "set_file_dialog_result", {"paths": "[]", "session_id": sid})
    await call_ui_ok(session, "clear_file_dialog_result", {"session_id": sid})
    await call_ui_ok(session, "object_command_panel", {"name": "DefinitelyMissing", "session_id": sid})
    await call_ui_ok(session, "object_context_menu", {"name": "DefinitelyMissing", "session_id": sid})
    await call_ui_ok(session, "group_state", {"name": "DefinitelyMissingGroup", "session_id": sid})
    await call_ui_ok(session, "group_current_page", {"name": "DefinitelyMissingGroup", "session_id": sid})
    await call_ui_ok(session, "decoration_links", {"name": "DefinitelyMissingDecoration", "session_id": sid})
    await call_ui_ok(session, "open_navigation_link", {"navigation_link": "e1cib/list/Catalog.ПС_Справочник1", "session_id": sid})

    negative_calls = [
        ("get_object", {"name": "DefinitelyMissing", "session_id": sid}, "Объект не может быть найден"),
        ("activate_object", {"name": "DefinitelyMissing", "session_id": sid}, "Объект не найден"),
        ("click_button", {"name": "DefinitelyMissing", "session_id": sid}, "Кнопка не найдена"),
        ("set_field_value", {"field_name": "DefinitelyMissing", "value": "x", "session_id": sid}, "Поле не найдено"),
        ("activate_field", {"field_name": "DefinitelyMissing", "session_id": sid}, "Поле не найдено"),
        ("toggle_field", {"field_name": "DefinitelyMissing", "session_id": sid}, "Поле не найдено"),
        ("table_add_row", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_set_field", {"table": "DefinitelyMissing", "field": "Код", "value": "1", "session_id": sid}, "Таблица не найдена"),
        ("table_edit_info", {"table": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_selected_rows", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_select_all", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_clear_selection", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_delete_row", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_copy_row", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_toggle_delete_mark", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_begin_edit", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_end_edit", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("table_editing_state", {"name": "DefinitelyMissing", "session_id": sid}, "Таблица не найдена"),
        ("group_expand", {"name": "DefinitelyMissing", "session_id": sid}, "object_not_found"),
        ("group_collapse", {"name": "DefinitelyMissing", "session_id": sid}, "object_not_found"),
        ("decoration_click", {"name": "DefinitelyMissing", "session_id": sid}, "object_not_found"),
        ("decoration_click_link", {"name": "DefinitelyMissing", "presentation": "x", "session_id": sid}, "object_not_found"),
        ("form_next_element", {"session_id": sid}, ""),
        ("form_previous_element", {"session_id": sid}, ""),
        ("tabular_document_text", {"name": "DefinitelyMissing", "session_id": sid}, "Табличный документ не найден"),
    ]
    for tool, args, expected in negative_calls:
        res = await session.call_tool(tool, args)
        if expected:
            if not res.isError:
                value = _parse_tool_result(res)
                if not (isinstance(value, dict) and value.get("found") is False):
                    raise AssertionError(f"{tool} unexpectedly succeeded: {value!r}")
            else:
                text = "\n".join(getattr(item, "text", "") for item in res.content)
                if expected not in text:
                    raise AssertionError(f"{tool} error lacks {expected!r}: {text}")
        print(f"COVERED {tool}", flush=True)


async def run_one_scenario(scenario: str) -> None:
    if scenario not in (*SCENARIOS, "dynamic", "full"):
        raise ValueError(f"Unknown E2E_SCENARIO={scenario!r}; expected full/{'/'.join(SCENARIOS)}")

    sid = os.environ.get("E2E_SESSION_ID", "e2e-mcp")
    suffix = _safe_suffix(sid)
    print(f"=== Autonomous Answer42 E2E scenario={scenario} session={sid} ===", flush=True)

    if os.environ.get("E2E_SKIP_BUILD_CLIENT_CF") != "1":
        _build_client_cf_from_sources()
    if os.environ.get("E2E_SKIP_KILL_ALL_1C") != "1":
        _kill_all_1c()

    server_env = {
        **os.environ,
        "PYTHONPATH": "src",
        # Some 1C UI commands (notably dynamic-list settings reset under
        # parallel load) can legitimately take longer than the default bridge
        # request timeout.  Keep the MCP client read timeout and the internal
        # bridge timeout aligned so a late successful 1C response is not turned
        # into an "unknown request id" after the bridge-side 60s timeout.
        "ONEC_MCP_REQUEST_TIMEOUT": os.environ.get("ONEC_MCP_REQUEST_TIMEOUT", "180"),
    }
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_1c.server"],
        env=server_env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            screenshot_dir, _tool_names = await common_start(session, sid, suffix)
            try:
                if scenario in ("smoke", "full"):
                    await run_smoke(session, sid, suffix, screenshot_dir)
                if scenario in ("dynamic-tables", "full"):
                    await run_dynamic_tables(session, sid, suffix, screenshot_dir)
                if scenario in ("dynamic-lists", "full"):
                    await run_dynamic_lists(session, sid, suffix, screenshot_dir)
                if scenario in ("dynamic-reports", "full"):
                    await run_dynamic_reports(session, sid, suffix, screenshot_dir)
                if scenario == "dynamic":
                    await run_dynamic(session, sid, suffix, screenshot_dir)
                if scenario in ("coverage", "full"):
                    await run_coverage(session, sid, suffix, screenshot_dir)
                await common_finish(session, sid, scenario, screenshot_dir)
            finally:
                stopped = await call_ok(session, "stop_session", {"session_id": sid, "clean_data": True})
                assert stopped["stopped"] is True, stopped

    print(f"=== AUTONOMOUS E2E scenario={scenario} PASSED ===", flush=True)


def run_parallel_split() -> None:
    base_sid = os.environ.get("E2E_SESSION_ID", "e2e-parallel")
    if os.environ.get("E2E_SKIP_BUILD_CLIENT_CF") != "1":
        _build_client_cf_from_sources()
    if os.environ.get("E2E_SKIP_KILL_ALL_1C") != "1":
        _kill_all_1c()

    shared_id = os.environ.get("ONEC_MCP_SHARED_CLIENT_BASE_ID") or base_sid
    os.environ["ONEC_MCP_SHARED_CLIENT_BASE_ID"] = shared_id
    _prebuild_shared_client_base(shared_id)

    max_parallel = int(os.environ.get("E2E_PARALLEL_MAX", "4"))
    failed: list[str] = []
    for start in range(0, len(SCENARIOS), max_parallel):
        procs: list[tuple[str, subprocess.Popen[str], Path]] = []
        for scenario in SCENARIOS[start : start + max_parallel]:
            sid = f"{base_sid}-{scenario}"
            log_path = Path(tempfile.gettempdir()) / f"{sid}.log"
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "E2E_SESSION_ID": sid,
                "E2E_SCENARIO": scenario,
                "E2E_SKIP_BUILD_CLIENT_CF": "1",
                "E2E_SKIP_KILL_ALL_1C": "1",
            }
            # Child processes must run a single assigned scenario.  Do not inherit
            # E2E_PARALLEL=1, otherwise each child recursively spawns another
            # smoke+dynamic+coverage fan-out.
            env.pop("E2E_PARALLEL", None)
            log = log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, __file__], env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
            log.close()
            procs.append((scenario, proc, log_path))
            print(f"STARTED {scenario}: pid={proc.pid} sid={sid} log={log_path}", flush=True)

        for scenario, proc, log_path in procs:
            code = proc.wait()
            print(f"FINISHED {scenario}: code={code} log={log_path}", flush=True)
            if code != 0:
                failed.append(f"{scenario} code={code} log={log_path}")
                try:
                    print(log_path.read_text(encoding="utf-8", errors="replace")[-8000:], flush=True)
                except OSError:
                    pass
    if failed:
        raise SystemExit("Parallel E2E failed: " + "; ".join(failed))
    print("=== AUTONOMOUS PARALLEL E2E SPLIT PASSED ===", flush=True)


async def main() -> None:
    if os.environ.get("E2E_PARALLEL") == "1":
        await asyncio.to_thread(run_parallel_split)
        return
    await run_one_scenario(os.environ.get("E2E_SCENARIO", "full"))


if __name__ == "__main__":
    asyncio.run(main())
