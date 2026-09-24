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
    assert body.count("ВосстановитьТекущуюСтрокуТаблицыMCP(Таблица, ИсходнаяСтрока)") == 2
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
