"""Regression tests for tool-list size, response compaction and tool fixes."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_1c import server
from mcp_1c.server import SessionState, mcp

MODULE = Path(__file__).resolve().parents[1] / "src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl"


def _bsl_function(name: str) -> str:
    text = MODULE.read_text(encoding="utf-8-sig")
    start = text.index(f"Функция {name}(")
    return text[start:text.index("КонецФункции", start)]


def _tools() -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


# --- published tool list -------------------------------------------------


def test_tool_parameters_publish_annotated_descriptions():
    tools = _tools()
    button = tools["click_button"].input_schema["properties"]
    assert button["name"]["description"] == "Button name or title text"
    total = sum(len(t.input_schema.get("properties", {})) for t in tools.values())
    described = sum(
        1 for t in tools.values() for p in t.input_schema.get("properties", {}).values() if p.get("description")
    )
    assert described / total > 0.95


def test_tool_schemas_have_no_auto_titles_but_keep_title_parameters():
    tools = _tools()
    dumped = json.dumps([t.input_schema for t in tools.values()], ensure_ascii=False)
    assert '"title": "' not in dumped.replace('"title": {', "")
    assert "title" in tools["activate_window"].input_schema["properties"]


def test_session_id_schema_is_stable_across_restarts():
    schema = _tools()["click_button"].input_schema["properties"]["session_id"]
    assert "default" not in schema  # was "default-<pid>": changed on every restart
    assert schema["description"] == server._SESSION_ID_SCHEMA_DESCRIPTION


def test_untyped_dict_results_are_not_sent_twice():
    tools = _tools()
    assert tools["start_session"].output_schema is None
    result = asyncio.run(mcp.call_tool("sessions_list", {}))
    assert result.structured_content is None


def test_internal_catalog_implementation_is_not_a_tool():
    assert "_metadata_objects_from_catalog_impl" not in _tools()


# --- outbound compaction --------------------------------------------------


_NODE = {
    "type": "Тестируемое поле формы", "name": "Поле", "title": "", "form_name": "", "navigation_url": "",
    "main": False, "home": False, "visible": True, "available": True, "enabled": True, "read_only": False,
    "current_visible": True, "current_available": True, "current_read_only": False, "data": "",
}


def test_compact_payload_drops_default_node_fields_and_empty_diagnostics():
    payload = {
        "found": False,
        "object": dict(_NODE),
        "client_diagnostics": {
            "user_messages": [], "error_info": "", "modal_closed": False, "modal_opened": False,
            "modal_title": "Контрагенты", "modal_buttons": [], "modal_texts": [], "modal_text": "",
            "events": [{"x": 1}], "user_message_texts": [], "has_dialog": False,
            "active_window_title": "Контрагенты", "active_form_name": "Справочник.Контрагенты.Форма.ФормаСписка",
        },
    }
    compact = server._compact_payload(payload)
    assert compact["found"] is False  # top-level booleans are meaningful
    assert compact["object"] == {"type": "Тестируемое поле формы", "name": "Поле"}
    assert compact["client_diagnostics"] == {
        "active_window_title": "Контрагенты",
        "active_form_name": "Справочник.Контрагенты.Форма.ФормаСписка",
    }


def test_compact_payload_keeps_non_default_node_state():
    node = dict(_NODE, visible=False, current_visible=True, read_only=True)
    compact = server._compact_outbound_ui_node(node)
    assert compact["visible"] is False
    assert compact["current_visible"] is True  # differs from the effective flag
    assert compact["read_only"] is True


def _text_result(payload):
    class Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class Result:
        is_error = False

        def __init__(self, text):
            self.content = [Block(text)]

    return Result(json.dumps(payload, ensure_ascii=False, indent=2))


def test_window_tools_return_children_count_unless_requested():
    window = dict(_NODE, type="Тестируемое окно клиентского приложения", children=[dict(_NODE), dict(_NODE)])
    stripped = json.loads(server._compact_tool_result(_text_result(window), "active_window", {}).content[0].text)
    assert stripped["children_count"] == 2 and "children" not in stripped

    listed = json.loads(server._compact_tool_result(_text_result({"windows": [window]}), "windows_list", {}).content[0].text)
    assert listed["windows"][0]["children_count"] == 2

    full = json.loads(
        server._compact_tool_result(_text_result(window), "active_window", {"include_children": True}).content[0].text
    )
    assert len(full["children"]) == 2


def test_ui_tree_keeps_its_subtree():
    tree = dict(_NODE, type="Тестируемое окно клиентского приложения", children=[dict(_NODE)])
    kept = json.loads(server._compact_tool_result(_text_result(tree), "ui_tree", {}).content[0].text)
    assert len(kept["children"]) == 1


def test_json_text_is_sent_without_indentation():
    text = server._compact_tool_result(_text_result({"a": [1, 2], "b": "текст"}), "x", {}).content[0].text
    assert text == '{"a":[1,2],"b":"текст"}'


def test_misdetected_dialog_texts_are_capped():
    payload = {"modal_texts": [f"text {i}" for i in range(200)]}
    compact = server._compact_payload(payload)
    assert len(compact["modal_texts"]) == server._MODAL_TEXTS_LIMIT
    assert compact["modal_texts_truncated"] == 200


def test_error_text_diagnostics_are_compacted():
    diagnostics = {"user_messages": [], "modal_opened": False, "active_window_title": "Окно", "events": []}
    text = "Кнопка не найдена: Нет\nclient_diagnostics=" + json.dumps(diagnostics, ensure_ascii=False, indent=2)
    assert server._compact_error_text(text) == 'Кнопка не найдена: Нет\nclient_diagnostics={"active_window_title":"Окно"}'


# --- value verification ----------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "expected", "match"),
    [
        ("100", "10", False),  # stale value must not verify a shorter number
        ("10,00", "10", True),
        ("5,000", "5", True),
        ("0", "", True),  # cleared number field
        ("  .  .    ", "", True),  # cleared date field
        ("Ромашка ООО", "Ромашка", True),  # reference presentation
        ("01.01.2026 0:00:00", "01.01.2026", True),
        ("Другое", "Ромашка", False),
        ("HARD-A42S55", "HARD-A42S55 ИЗМЕНЕНО", False),  # shorter actual: input did not happen
        ("Ромашка", "Ромашка ООО", False),
        ("<custom>", "", True),  # cleared reference cell shows a placeholder
        ("<Empty value>", "", True),
        ("<custom>", "x", False),
        (None, "", False),  # unreadable field is unknown, not empty
        (None, "x", False),
        ("строка¶вторая", "строка\nвторая", True),  # 1C shows a line break as ¶
    ],
)
def test_values_match_for_verification(actual, expected, match):
    assert server._values_match_for_verification(actual, expected) is match


# --- set_form_fields -------------------------------------------------------


def test_set_form_fields_sets_each_field_and_reports_failures(monkeypatch):
    calls = []

    async def fake_set_field_value(field_name, value, **kwargs):
        calls.append((field_name, value, kwargs["search_by_title"]))
        if field_name == "Нет поля":
            raise RuntimeError("Поле не найдено: Нет поля")
        return {"set": True, "value": value, "client_diagnostics": {"active_window_title": "Форма"}}

    monkeypatch.setattr(server, "set_field_value", fake_set_field_value)
    result = asyncio.run(server.set_form_fields(
        '{"Дата документа": "01.01.2026", "Нет поля": "x", "Сумма": 5}', search_by_title=True, session_id="unit",
    ))

    assert [c[0] for c in calls] == ["Дата документа", "Нет поля", "Сумма"]  # keeps going after a failure
    assert result["set"] is False
    assert result["failed"] == ["Нет поля"]
    assert result["results"][2] == {"set": True, "value": "5", "field": "Сумма"}
    assert "Поле не найдено" in result["results"][1]["error"]


@pytest.mark.parametrize("values", ["не json", "[1, 2]"])
def test_set_form_fields_rejects_non_object_json(values):
    with pytest.raises(ToolError, match="JSON object"):
        asyncio.run(server.set_form_fields(values, session_id="unit"))


def test_dynamic_list_settings_accept_single_object_and_reject_garbage():
    assert server._json_array_argument('{"field": "Наименование"}', "filters") == [{"field": "Наименование"}]
    assert server._json_array_argument("", "filters") == []
    with pytest.raises(ToolError, match="JSON array"):
        server._json_array_argument("{", "filters")


# --- bridge timeouts ---------------------------------------------------------


class _FakeBridge:
    request_timeout = 180.0

    def __init__(self):
        self.timeouts = []

    async def call(self, method, params, timeout=None):
        self.timeouts.append(timeout)
        return {"ok": True}


def _session_with_fake_bridge(tmp_path, monkeypatch):
    sess = SessionState(
        session_id="unit-timeout", display="", platform_dir=tmp_path, ws_port=1, ibsrv_port=2,
        testclient_port=3, data_dir=tmp_path / "data",
    )
    sess.bridge = _FakeBridge()

    async def restored(session_id):
        return sess

    async def connected(sess, timeout=0):
        return {"connected": True}

    monkeypatch.setattr(server, "_ensure_restored_session", restored)
    monkeypatch.setattr(server, "_wait_for_bridge_connected", connected)
    monkeypatch.setattr(server, "_record_tool", lambda sid, method, params, result: result)
    return sess


def test_bridge_waits_at_least_as_long_as_the_1c_command(tmp_path, monkeypatch):
    sess = _session_with_fake_bridge(tmp_path, monkeypatch)
    asyncio.run(server._bridge_call_recorded("unit-timeout", "wait_form", {"timeout": 300}))
    asyncio.run(server._bridge_call_recorded("unit-timeout", "wait_form", {"timeout": 15}))
    asyncio.run(server._bridge_call_recorded("unit-timeout", "dynamic_list_output", {"wait_seconds": 400}))
    assert sess.bridge.timeouts == [330.0, 180.0, 430.0]


def test_watchdog_timeout_zero_disables_the_watchdog(tmp_path, monkeypatch):
    sess = _session_with_fake_bridge(tmp_path, monkeypatch)
    asyncio.run(server._dangerous_bridge_call_recorded("unit-timeout", "tabular_document_save", {}, timeout=0, danger="x"))
    assert sess.bridge.timeouts == [180.0]  # ordinary bridge timeout, not a 1-second watchdog


# --- tabular document save leftovers -----------------------------------------


def test_tabular_document_save_removes_probe_files(tmp_path):
    probes = []
    for index in range(1, 4):
        path = tmp_path / f"doc_filter{index}.txt"
        path.write_text("x", encoding="utf-8")
        probes.append({"path": str(path), "filter_index": index})
    requested = tmp_path / "doc.txt"
    requested.write_text("x", encoding="utf-8")
    result = {"path": str(requested), "selected": dict(probes[1], copied_to=str(requested))}

    server._discard_unselected_tabular_document_files(result, probes)

    assert requested.exists()
    assert not any(Path(p["path"]).exists() for p in probes)
    assert result["probe_files_count"] == 3 and result["probe_files_removed"] == 3
    assert len(result["files"]) == 1


# --- shared ibsrv release -----------------------------------------------------


def test_discarded_session_releases_shared_servers(tmp_path, monkeypatch):
    sess = SessionState(
        session_id="unit-release", display="", platform_dir=tmp_path, ws_port=1, ibsrv_port=2,
        testclient_port=3, data_dir=tmp_path / "data",
    )
    sess.shared_file_key = "file:bp"
    sess.shared_client_key = "demo:x"
    released = []
    monkeypatch.setattr(server, "_release_shared_file_server", lambda key, sid: released.append((key, sid)))
    monkeypatch.setattr(server, "_kill_pid_group", lambda pid, label: None)
    monkeypatch.setattr(server, "_cleanup_stale_session_processes", lambda s, reason: None)

    server._kill_persisted_session_processes(sess, "unit", include_test_client=False)

    assert sorted(released) == [("demo:x", "unit-release"), ("file:bp", "unit-release")]
    assert sess.shared_file_key == "" and sess.shared_client_key == ""


# --- BSL fixes (static) -------------------------------------------------------


def test_file_dialog_result_passes_result_flag_and_accepts_plain_path():
    body = _bsl_function("УстановитьРезультатДиалогаФайлаMCP")
    assert "УстановитьРезультатДиалогаВыбораФайла(Истина, Пути)" in body
    assert 'СтрНачинаетсяС(ПутиJSON, "[")' in body


def test_get_object_reports_not_found_instead_of_raising():
    body = _bsl_function("ПолучитьОбъектПриложенияMCP")
    assert 'Новый Структура("found,name,title,type", Ложь' in body
    assert "не может быть найден" in body
    assert not re.search(r"^\s+Объект\s*=", body, re.MULTILINE)  # form attribute, not a local


@pytest.mark.parametrize(
    ("function", "expected_type"),
    [
        ("УдалитьСтрокуТаблицыMCP", "ТестируемаяТаблицаФормы"),
        ("ВыделитьВсеСтрокиТаблицыMCP", "ТестируемаяТаблицаФормы"),
        ("РазвернутьГруппуMCP", "ТестируемаяГруппаФормы"),
        ("СостояниеГруппыMCP", "ТестируемаяГруппаФормы"),
    ],
)
def test_table_and_group_commands_do_not_target_the_window(function, expected_type):
    body = _bsl_function(function)
    assert f'НайтиОбъектРекурсивно(АктивноеОкно, Имя, Заголовок, "{expected_type}", 0)' in body


def test_decoration_click_requires_a_target():
    assert "Укажите name или title элемента." in _bsl_function("НажатьДекорациюMCP")


def test_wait_form_closed_reports_timeout():
    body = _bsl_function("ОжидатьЗакрытиеФормыMCP")
    assert '"timeout"' in body and "Результат <> Ложь" in body


def test_wait_object_rereads_the_active_window():
    body = _bsl_function("ОжидатьОбъектMCP")
    loop = body[body.index("Пока ТекущаяУниверсальнаяДатаВМиллисекундах()"):]
    assert "ПолучитьАктивноеОкно()" in loop


def test_table_rows_restore_the_cursor():
    body = _bsl_function("ПолучитьСтрокиТаблицыКакМассив")
    assert body.count("ВосстановитьТекущуюСтрокуТаблицыMCP(Таблица, ИсходнаяСтрока") == 2
    assert "ВосстановитьТекущуюСтрокуТаблицыMCP(Таблица, ИсходнаяСтрока, Результат)" in body  # by index when the row description is refused
    assert "Если Лимит <= 0 Тогда" in body


def test_windows_list_serializes_children_only_on_request():
    body = _bsl_function("СписокОконКакСтруктуру")
    assert '"include_children"' in body and "КраткоеОписаниеОбъектаMCP(ТекущееОкно)" in body


def test_button_errors_join_the_attempts():
    text = MODULE.read_text(encoding="utf-8-sig")
    assert '"Ни одна кнопка не нажалась: " + Строка(Ошибки)' not in text  # printed "Массив"


def test_save_form_verifies_the_write():
    text = MODULE.read_text(encoding="utf-8-sig")
    assert "АктивноеОкно.Записать()" not in text  # no such method on a tested window
    assert '"form_still_modified"' in text


# --- merged tools ----------------------------------------------------------

_REMOVED_TOOLS = {
    "goto_start_page", "goto_previous_window", "goto_next_window", "group_expand", "group_collapse",
    "group_current_page", "table_select_all", "table_clear_selection", "table_begin_edit", "table_end_edit",
    "table_editing_state", "field_dropdown_items", "field_dropdown_state", "field_value_text", "field_tooltip",
    "credentials_titles", "command_bar", "tabular_document_text",
}


def test_merged_tools_replace_the_old_names():
    names = set(_tools())
    assert not (_REMOVED_TOOLS & names)
    assert {"goto_window", "group_set_expanded", "table_selection", "table_edit", "field_dropdown"} <= names
    profiled = set().union(*server.TOOL_PROFILE_GROUPS.values()) | server.TOOL_PROFILE_CORE
    assert not (_REMOVED_TOOLS & profiled)


def _record_bridge(monkeypatch, responses=None):
    calls = []

    async def fake(session_id, method, params, *, timeout=None):
        calls.append((method, params))
        return (responses or {}).get(method, {"found": True})

    monkeypatch.setattr(server, "_bridge_call_recorded", fake)
    return calls


@pytest.mark.parametrize(
    ("call", "method"),
    [
        (lambda: server.goto_window("start_page", session_id="u"), "goto_start_page"),
        (lambda: server.goto_window("previous", session_id="u"), "goto_previous_window"),
        (lambda: server.goto_window("next", session_id="u"), "goto_next_window"),
        (lambda: server.group_set_expanded(True, name="Г", session_id="u"), "group_expand"),
        (lambda: server.group_set_expanded(False, name="Г", session_id="u"), "group_collapse"),
        (lambda: server.table_selection(True, name="Т", session_id="u"), "table_select_all"),
        (lambda: server.table_selection(False, name="Т", session_id="u"), "table_clear_selection"),
        (lambda: server.table_edit("begin", name="Т", session_id="u"), "table_begin_edit"),
        (lambda: server.table_edit("end", name="Т", session_id="u"), "table_end_edit"),
        (lambda: server.table_edit("state", name="Т", session_id="u"), "table_editing_state"),
        (lambda: server.field_dropdown("Поле", session_id="u"), "field_dropdown_items"),
        (lambda: server.field_dropdown("Поле", open=False, session_id="u"), "field_dropdown_state"),
        (lambda: server.object_command_panel(session_id="u"), "command_bar"),
        (lambda: server.object_command_panel(name="Список", session_id="u"), "object_command_panel"),
        (lambda: server.tabular_documents(session_id="u"), "tabular_documents"),
    ],
)
def test_merged_tools_route_to_the_bridge_commands(monkeypatch, call, method):
    calls = _record_bridge(monkeypatch)
    asyncio.run(call())
    assert calls[0][0] == method


def test_merged_tools_reject_unknown_actions():
    with pytest.raises(ToolError, match="start_page, previous or next"):
        asyncio.run(server.goto_window("sideways", session_id="u"))
    with pytest.raises(ToolError, match="begin, end or state"):
        asyncio.run(server.table_edit("pause", session_id="u"))


def test_group_state_includes_current_page(monkeypatch):
    calls = _record_bridge(monkeypatch, {
        "group_state": {"found": True, "expanded": True},
        "group_current_page": {"found": True, "page": {"type": "Тестируемая группа формы", "name": "СтраницаТовары"}},
    })
    result = asyncio.run(server.group_state(name="ГруппаСтраницы", session_id="u"))
    assert [c[0] for c in calls] == ["group_state", "group_current_page"]
    assert result["current_page"]["name"] == "СтраницаТовары"


def test_field_info_includes_tooltip(monkeypatch):
    _record_bridge(monkeypatch, {
        "field_info": {"found": True, "data_text": "A42"},
        "field_tooltip": {"found": True, "tooltip": "Начните с наименования"},
    })
    result = asyncio.run(server.field_info("Наименование", session_id="u"))
    assert result["data_text"] == "A42" and result["tooltip"] == "Начните с наименования"


def test_tabular_documents_reads_one_document_by_name(monkeypatch):
    calls = _record_bridge(monkeypatch, {"tabular_document_text": {"found": True, "rows": 2, "cells": [["a"], ["b"]]}})
    asyncio.run(server.tabular_documents(name="Результат", max_rows=5, session_id="u"))
    method, params = calls[0]
    assert method == "tabular_document_text"
    assert params["name"] == "Результат" and params["max_rows"] == 5


def test_field_dropdown_reports_missing_field_consistently(monkeypatch):
    async def fake(session_id, method, params, *, timeout=None):
        raise server.BridgeError("Поле не найдено: Нет")

    monkeypatch.setattr(server, "_bridge_call_recorded", fake)
    result = asyncio.run(server.field_dropdown("Нет", open=False, session_id="u"))
    assert result["found"] is False and result["diagnostics"] == ["field_not_found"]


def test_dynamic_list_output_hides_timings_unless_asked(monkeypatch):
    _record_bridge(monkeypatch, {"dynamic_list_output": {"found": True, "timings": {"total_ms": 1}}})
    assert "timings" not in asyncio.run(server.dynamic_list_output())
    assert asyncio.run(server.dynamic_list_output(include_timings=True))["timings"] == {"total_ms": 1}


@pytest.mark.parametrize(
    ("actual", "expected", "match"),
    [
        ("1,000", "1000", True),
        ("82,020", "82020.00", True),
        ("1,000.50", "1000.5", True),
        ("1 000,50", "1000.5", True),
        ("12.000", "12", True),
        ("82,020", "82021", False),
        ("100", "10", False),
        ("9/30/2026 12:00:00 AM", "09/30/2026", True),
        ("30.09.2026 0:00:00", "30.09.2026", True),
        ("9/30/2026 12:00:00 AM", "10/01/2026", False),
    ],
)
def test_verification_understands_grouped_numbers_and_dates(actual, expected, match):
    from mcp_1c.server import _values_match_for_verification

    assert _values_match_for_verification(actual, expected) is match


def test_english_ui_headers_resolve_to_russian_row_keys():
    from mcp_1c.server import _row_value

    row = {"Subsystem name": "УправлениеПредприятием", "Version": "2.5.27.79", "This is main configuration": "Yes"}
    assert _row_value(row, "Имя подсистемы", "ИмяПодсистемы") == "УправлениеПредприятием"
    assert _row_value(row, "Версия") == "2.5.27.79"
    assert _row_value({"Sequence\nnumber": "1"}, "Порядковый номер", "Код", "Порядковый\nномер") == "1"
    assert _row_value({"Порядковый номер": "2"}, "Порядковый\nномер") == "2"


def test_creation_window_is_recognised_in_english_interface():
    from mcp_1c.server import _normalize_creation_window_result

    result = {"set": True, "client_diagnostics": {"active_window_changed": True, "active_window_title": "Sales order (create)", "active_form_name": "Документ.ЗаказКлиента.Форма.ФормаДокумента"}}
    normalized = _normalize_creation_window_result(result, create_if_missing=True)
    assert normalized["set"] is False and normalized["window_opened"] is True


# --- token economy of responses --------------------------------------------


def _sent(payload, tool="x", arguments=None):
    return json.loads(server._compact_tool_result(_text_result(payload), tool, arguments or {}).content[0].text)


def test_table_rows_are_columnar_and_mark_cut_cells():
    rows = {"found": True, "count": 2, "rows": [
        {"values": {"Имя": "А", "Комментарий": "x" * 20}},
        {"values": {"Имя": "Б", "Комментарий": ""}},
    ]}
    full = server._columnar_rows_result(rows, max_cell_chars=0)
    assert full["columns"] == ["Имя", "Комментарий"]
    assert full["rows"] == [["А", "x" * 20], ["Б", ""]]
    assert "truncated_cells" not in full and "rows_count" not in full

    cut = server._columnar_rows_result(rows, max_cell_chars=5)
    assert cut["rows"][0][1] == "xxxxx…[+15 chars]"
    assert cut["truncated_cells"] == 1 and "max_cell_chars=0" in cut["truncation"]

    picked = server._columnar_rows_result(rows, columns=" имя , Нет", max_cell_chars=0)
    assert picked["columns"] == ["Имя"] and picked["rows"] == [["А"], ["Б"]]
    assert picked["other_columns"] == ["Комментарий"] and picked["unknown_columns"] == ["Нет"]


def test_table_rows_keep_row_attributes():
    rows = {"rows": [{"index": 3, "current": True, "values": {"Имя": "А"}}]}
    assert server._columnar_rows_result(rows)["rows"] == [{"index": 3, "current": True, "cells": ["А"]}]


def test_repeated_column_titles_do_not_overwrite_each_other():
    pairs = [{"key": "Сумма", "value": "1"}, {"key": "Сумма", "value": "2"}, {"key": "", "value": ""}]
    assert server._row_values_from_pairs(pairs) == {"Сумма": "1", "Сумма (2)": "2"}


def test_single_rows_are_sent_as_values_once():
    row = {"available": True, "pairs": [{"key": "Имя", "value": "А"}, {"key": "Код", "value": ""}]}
    sent = _sent({"selected": True, "current_row": row, "positioning": {"found": True, "index": 5, "row": row}})
    assert sent["current_row"] == {"available": True, "values": {"Имя": "А", "Код": ""}}
    assert sent["positioning"] == {"found": True, "index": 5}
    assert _sent({"current_row": row}, arguments={"compact": False})["current_row"] == row

    cell = _sent({"found": True, "cell": {"cell_text": "А", "current_row": row}}, "table_cell_text")
    assert cell["cell"] == {"cell_text": "А"}


def test_search_diagnostics_are_sent_only_with_timings():
    payload = {"pressed": True, "search_ms": 3, "press_ms": 5, "search_stats": {"nodes": 0}}
    assert _sent(payload, "click_button") == {"pressed": True}
    assert _sent(payload, "click_button", {"include_timings": True}) == payload


def test_unchanged_window_state_is_not_repeated():
    diagnostics = {"active_window_title": "Б", "active_form_name": "Форма.Б"}
    same = {
        "pressed": True, "window_title_before": "А", "window_title_after": "А", "form_name_before": "Ф",
        "form_name_after": "Ф", "window_changed": False,
    }
    assert _sent(same) == {"pressed": True, "window_changed": False}
    changed = {
        "closed": True, "window_title_before": "", "window_title_after": "Б", "form_name_before": "Форма.А",
        "form_name_after": "Форма.Б", "window_changed": True, "client_diagnostics": diagnostics,
    }
    assert _sent(changed) == {
        "closed": True, "form_name_before": "Форма.А", "window_changed": True, "client_diagnostics": diagnostics,
    }
    undiagnosed = {"window_title_before": "А", "window_title_after": "Б"}
    assert _sent(undiagnosed) == undiagnosed


def test_start_session_response_keeps_what_the_caller_acts_on():
    window = {"type": "Тестируемое окно клиентского приложения", "title": "Окно"}
    payload = {
        "session_id": "s", "version": "8.5.1.1", "platform_dir": "C:/1c", "display": "d", "client_display": "c",
        "ws_port": 1, "ibsrv_port": 2, "testclient_port": 3, "data_dir": "x", "bridge_url": "ws://", "manager_pid": 4,
        "ws_url": "http://", "log_dir": "l", "rag_snapshot": "", "rag_enabled": False, "server_rag_enabled": True,
        "rag_notice": {
            "enabled": False, "auto": True, "summary_backend": {"enabled": True}, "layers": [], "reason": "r", "message": "r",
        },
        "configuration_name": "БП", "configuration_version": "3.0", "extension_names": [], "extension_names_loaded": False,
        "web_runtime_probe": {"attempted": False, "status": "not_applicable"}, "idle_timeout_seconds": 60,
        "test_client": {
            "connected": True, "base_url": "b", "connection_mode": "ws", "port": 3, "client_pid": 5, "client_log": "log",
            "auto_maximize": {"ok": True}, "maximized": {"maximized": True, "method": "m", "window": window},
        },
    }
    assert _sent(payload, "start_session") == {
        "session_id": "s", "version": "8.5.1.1", "rag_enabled": False, "rag_notice": {"enabled": False, "reason": "r"},
        "configuration_name": "БП", "configuration_version": "3.0", "extension_names": [], "extension_names_loaded": False,
        "idle_timeout_seconds": 60, "test_client": {"connected": True, "base_url": "b", "window": window},
    }
    probed = _sent({"web_runtime_probe": {"attempted": True, "status": "auth_failed"}}, "start_session")
    assert probed["web_runtime_probe"]["status"] == "auth_failed"


def test_ui_tree_response_has_no_echo_and_no_zero_counters():
    summary = {
        "include_hidden": False, "nodes_count": 2, "truncated": False, "truncated_nodes_count": 0,
        "hidden_children_count": 0, "hidden_nodes_count": 0,
        "types_count": {"by_family": {"forms": 2}, "by_type": {"Тестируемая форма": 2}},
    }
    payload = {
        "format": "outline", "text": "- Тестируемая форма", "tree_summary": summary, "profile": "navigation",
        "fields": "auto", "metrics": {"bridge_ms": 1.0},
    }
    assert _sent(payload, "ui_tree") == {
        "format": "outline", "text": "- Тестируемая форма",
        "tree_summary": {"nodes_count": 2, "types_count": {"Тестируемая форма": 2}},
    }
    assert _sent(payload, "ui_tree", {"include_timings": True})["metrics"] == {"bridge_ms": 1.0}
    truncated = _sent({"tree_summary": dict(summary, truncated=True, truncated_nodes_count=1)}, "ui_tree")
    assert truncated["tree_summary"]["truncated"] is True and truncated["tree_summary"]["truncated_nodes_count"] == 1


def test_outline_shows_only_non_default_state_and_keeps_other_fields():
    tree = {
        "type": "Тестируемая форма", "title": "Заказ", "form_name": "Документ.Заказ.Форма.ФормаДокумента",
        "visible": True, "available": True, "read_only": False,
        "children": [
            {
                "type": "Тестируемое поле формы", "name": "Сумма", "title": "Сумма", "visible": True, "available": False,
                "enabled": False, "read_only": True, "current": True, "value": "1 000",
            },
            {
                "type": "Тестируемая группа формы", "name": "Страницы", "current_page_name": "Основное",
                "pages": [{"name": "Основное"}, {"name": "Прочее"}], "current_page": {"name": "Основное"},
            },
        ],
    }
    assert server._outline_ui_tree(tree).splitlines() == [
        '- Тестируемая форма — Заказ form_name="Документ.Заказ.Форма.ФормаДокумента"',
        '  - Тестируемое поле формы Сумма [not_available, read_only, current] value="1 000"',
        '  - Тестируемая группа формы Страницы current_page_name="Основное" pages=["Основное","Прочее"]',
    ]


def test_table_rows_tool_returns_columns_once(monkeypatch):
    async def fake_bridge_call(session_id, method, params):
        return {"found": True, "count": 1, "rows": [{"pairs": [{"key": "Имя", "value": "А"}, {"key": "Код", "value": "1"}]}]}

    monkeypatch.setattr(server, "_bridge_call_recorded", fake_bridge_call)
    result = asyncio.run(server.table_rows(name="Список", columns="Код", session_id="unit"))
    assert result == {"found": True, "count": 1, "columns": ["Код"], "rows": [["1"]], "other_columns": ["Имя"]}
    raw = asyncio.run(server.table_rows(name="Список", compact=False, session_id="unit"))
    assert raw["rows"][0]["pairs"][0] == {"key": "Имя", "value": "А"}


# --- RAG is opt-in -----------------------------------------------------------


def _rag_enabled_in_fresh_process(env: dict) -> str:
    import os
    import subprocess
    import sys

    clean = {k: v for k, v in os.environ.items() if k not in {"ONEC_MCP_ENABLE_RAG", "ONEC_MCP_DISABLE_RAG"}}
    code = "from mcp_1c import server; print(server._RAG_ENABLED)"
    return subprocess.run([sys.executable, "-c", code], env={**clean, **env}, capture_output=True, text=True, check=True).stdout.strip()


def test_rag_is_off_unless_the_server_enables_it():
    assert _rag_enabled_in_fresh_process({}) == "False"
    assert _rag_enabled_in_fresh_process({"ONEC_MCP_ENABLE_RAG": "1"}) == "True"
    assert _rag_enabled_in_fresh_process({"ONEC_MCP_ENABLE_RAG": "1", "ONEC_MCP_DISABLE_RAG": "1"}) == "False"


def test_disabled_rag_tells_how_to_enable_it(monkeypatch):
    monkeypatch.setattr(server, "_RAG_ENABLED", False)
    result = server._rag_disabled_result()
    assert result["reason"] == "rag_disabled" and "--enable-rag" in result["message"]
