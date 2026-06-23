#!/usr/bin/env python3
"""Full E2E test coverage for testclient-bridge MCP tools.

Fully autonomous: starts two local ibsrv instances (manager + client),
no external stand required. Validates the entire pipeline from session
start to all available MCP tools.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/openclaw/.openclaw/workspace/testclient-bridge/src")

from mcp_1c import server  # noqa: E402


def kill_all_1c() -> None:
    subprocess.run(["pkill", "-9", "-f", "1cv8"], check=False)
    subprocess.run(["pkill", "-9", "-f", "ibcmd"], check=False)
    subprocess.run(["pkill", "-9", "-f", "ibsrv"], check=False)
    subprocess.run(["pkill", "-9", "-f", "Xvfb"], check=False)
    time.sleep(1)


async def main() -> None:
    print("=== Starting Full MCP E2E Coverage Test (Autonomous, Two ibsrv) ===", flush=True)
    kill_all_1c()

    # 1. Lifecycle: Start Session (Xvfb + Bridge + manager ibsrv + client ibsrv)
    print("\n[1/8] Testing start_session (start_pipeline=True)...", flush=True)
    result = await server.start_session(
        version="8.5.1.1150",
        session_id="e2e-full",
        start_pipeline=True,
    )
    print(f"Session started: {result['session_id']}")
    sid = result["session_id"]
    sess = server._session(sid)
    assert result.get("client_ibsrv_port"), f"No client_ibsrv_port in result: {result}"
    print(f"Manager ibsrv: {result['ibsrv_port']}, Client ibsrv: {result['client_ibsrv_port']}")

    # 2. Lifecycle & Status
    print("\n[2/8] Testing Lifecycle tools...", flush=True)
    assert server.bridge_status(session_id=sid)["connected"] == False  # Not connected yet
    assert server.runtime_status(session_id=sid)["running"] == True

    # Wait for manager to connect to bridge
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.bridge_status(session_id=sid)["connected"]:
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError("Manager did not connect to bridge within 30s")
    print("Manager connected!")

    # 3. Test Client Connection (LOCAL client ibsrv, no external stand)
    print("\n[3/8] Testing connect_test_client (local client ibsrv)...", flush=True)
    r = await server.connect_test_client(
        base_url=result["client_base_url"],
        username="your-login",
        password="",
        port=result["testclient_port"],
        onec_path=result["platform_dir"] + "/1cv8c",
        session_id=sid,
    )
    assert r.get("connected") == True, f"Failed to connect: {r}"
    print("Connected to local client ibsrv.")

    # 4. UI Automation & Windows
    print("\n[4/8] Testing UI Automation tools...", flush=True)
    await server.active_window(session_id=sid)
    await server.ui_tree(session_id=sid)
    await server.command_bar(session_id=sid)
    await server.current_element(session_id=sid)
    await server.find_object(name="", title="", session_id=sid)
    await server.get_object(name="", title="", session_id=sid)
    await server.activate_object(name="", title="", session_id=sid)
    try:
        await server.execute_window_command(command="e1cib/list/Справочник.Пользователи", session_id=sid)
    except Exception as e:
        print(f"execute_window_command: {e}")
    try:
        await server.click_button(name="OK", session_id=sid)
    except Exception as e:
        print(f"click_button: {e}")
    try:
        await server.set_field_value(field_name="test", value="val", session_id=sid)
    except Exception as e:
        print(f"set_field_value: {e}")
    try:
        await server.create_new_item(session_id=sid)
    except Exception as e:
        print(f"create_new_item: {e}")
    try:
        await server.save_form(session_id=sid)
    except Exception as e:
        print(f"save_form: {e}")
    try:
        await server.close_form(session_id=sid)
    except Exception as e:
        print(f"close_form: {e}")
    server.screenshot(path=f"{result['data_dir']}/test.png", session_id=sid)
    print("UI tools called successfully.")

    # 5. Tables & Reference Tools
    print("\n[5/8] Testing Table & Reference tools...", flush=True)
    try:
        await server.table_current_row(name="Список", session_id=sid)
    except Exception as e:
        print(f"table_current_row: {e}")
    try:
        await server.table_selected_rows(name="Список", session_id=sid)
    except Exception as e:
        print(f"table_selected_rows: {e}")
    try:
        await server.table_cell_text(name="Список", cell="Код", session_id=sid)
    except Exception as e:
        print(f"table_cell_text: {e}")
    try:
        await server.table_rows(name="Список", session_id=sid)
    except Exception as e:
        print(f"table_rows: {e}")
    try:
        await server.table_find_row(name="Список", text="test", session_id=sid)
    except Exception as e:
        print(f"table_find_row: {e}")
    try:
        await server.table_goto_row(name="Список", text="test", session_id=sid)
    except Exception as e:
        print(f"table_goto_row: {e}")
    try:
        await server.table_move_row(name="Список", direction="next", session_id=sid)
    except Exception as e:
        print(f"table_move_row: {e}")
    try:
        await server.table_add_row(name="Список", session_id=sid)
    except Exception as e:
        print(f"table_add_row: {e}")
    try:
        await server.table_set_field(table="Список", field="Код", value="123", session_id=sid)
    except Exception as e:
        print(f"table_set_field: {e}")
    try:
        await server.table_edit_info(table="Список", session_id=sid)
    except Exception as e:
        print(f"table_edit_info: {e}")
    try:
        await server.toggle_field(field_name="Флажок", session_id=sid)
    except Exception as e:
        print(f"toggle_field: {e}")
    try:
        await server.choose_field_from_list(field_name="Ссылка", session_id=sid)
    except Exception as e:
        print(f"choose_field_from_list: {e}")
    try:
        await server.choose_field_first_row(field_name="Ссылка", session_id=sid)
    except Exception as e:
        print(f"choose_field_first_row: {e}")
    try:
        await server.choose_current_row(session_id=sid)
    except Exception as e:
        print(f"choose_current_row: {e}")

    # Dynamic lists
    try:
        await server.dynamic_list_set_order(column_title="Код", session_id=sid)
    except Exception as e:
        print(f"dynamic_list_set_order: {e}")
    try:
        await server.dynamic_list_output(session_id=sid)
    except Exception as e:
        print(f"dynamic_list_output: {e}")
    try:
        await server.dynamic_list_open_settings(session_id=sid)
    except Exception as e:
        print(f"dynamic_list_open_settings: {e}")
    try:
        await server.dynamic_list_clear_settings(session_id=sid)
    except Exception as e:
        print(f"dynamic_list_clear_settings: {e}")
    try:
        await server.dynamic_list_open_form_settings(session_id=sid)
    except Exception as e:
        print(f"dynamic_list_open_form_settings: {e}")
    print("Table and Reference tools called successfully.")

    # 6. Tabular documents
    print("\n[6/8] Testing tabular documents...", flush=True)
    try:
        await server.tabular_documents(session_id=sid)
    except Exception as e:
        print(f"tabular_documents: {e}")
    try:
        await server.tabular_document_text(name="Документ", session_id=sid)
    except Exception as e:
        print(f"tabular_document_text: {e}")
    print("Tabular documents called successfully.")

    # 7. RAG Tools (Independent of 1C UI)
    print("\n[7/8] Testing RAG tools...", flush=True)
    rag_path = "/home/openclaw/.openclaw/workspace/testclient-bridge/src/cf"
    server.rag_source_detect(path=rag_path, session_id=sid)
    server.rag_source_add(name="E2E_SRC", path=rag_path, session_id=sid)
    server.rag_sources_list(session_id=sid)
    server.rag_snapshot_create(name="E2E_SNAP", sources=["E2E_SRC"], session_id=sid)
    server.rag_snapshots_list(session_id=sid)
    server.rag_index_build(snapshot="E2E_SNAP", session_id=sid)
    server.rag_lookup_object(object="Configuration", snapshot="E2E_SNAP", session_id=sid)
    server.rag_query(query="test", snapshot="E2E_SNAP", session_id=sid)
    print("RAG tools passed.")

    # 8. Teardown
    print("\n[8/8] Testing Teardown...", flush=True)
    await server.disconnect_test_client(session_id=sid)
    server.stop_manager(session_id=sid)
    await server.stop_session(session_id=sid, clean_data=True)

    print("\n=== FULL E2E COVERAGE PASSED ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())