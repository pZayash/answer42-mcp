"""Composite-type fields: 1C asks for the type in «Выбор типа данных» before it takes a value."""

import asyncio

import pytest

from mcp_1c import server
from mcp_1c.bridge import BridgeError

TYPES = ["Контрагент", "Нематериальный актив", "Номенклатура", "Номенклатурная группа", "Основное средство"]


class FakeClient:
    """Just enough of the test client: the type dialog, a selection form and a table cell."""

    def __init__(self, cursor=4, typing_opens_dialog=True):
        self.window = "Выбор типа данных"
        self.form = ""
        self.cursor = cursor  # a scan of the table leaves it on the last row, as the real client did
        self.typing_opens_dialog = typing_opens_dialog
        self.calls = []
        self.chosen_type = ""
        self.cell = ""

    async def call(self, session_id, method, params, **_kwargs):
        self.calls.append((method, dict(params)))
        if method == "active_window":
            return {"title": self.window}
        if method == "table_rows":
            return {"found": True, "rows": [{"available": True, "pairs": [{"key": "Колонка1", "value": name}]} for name in TYPES], "count": len(TYPES)}
        if method == "table_move_row":
            if params["direction"] == "first":
                self.cursor = 0
            elif params["direction"] == "next":
                self.cursor += params["steps"]
            return {"moved": True}
        if method == "table_current_row":
            if params.get("name") == "TypeTree":
                return {"current_row": {"pairs": [{"key": "Колонка1", "value": TYPES[self.cursor]}]}}
            return {"current_row": {"pairs": [{"key": "Субконто2 Дт", "value": self.cell}]}}
        if method == "click_button":
            self.chosen_type = TYPES[self.cursor]
            self.window, self.form = self.chosen_type, f"Справочник.{self.chosen_type}.ФормаВыбора"
            return {"pressed": True, "window_title_after": self.window, "form_name_after": self.form}
        if method == "choose_current_row":
            self.cell = params["expected_text"]
            self.window, self.form = "Операция", "Документ.Операция.ФормаДокумента"
            return {"selected": True}
        if method in {"table_set_field", "set_field_value"}:
            if self.typing_opens_dialog and not self.chosen_type:
                self.window = "Выбор типа данных"
                raise BridgeError("Выпадающий список редактора поля пуст после ввода значения")
            self.cell = params["value"]
            return {"set": True}
        raise AssertionError(f"unexpected call {method}")


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(server, "_bridge_call_recorded", fake.call)
    return fake


def test_the_type_is_found_by_exact_name_before_a_part_of_it():
    assert server._match_value_type(TYPES, "номенклатура") == 2  # not the group "Номенклатурная группа"
    assert server._match_value_type(TYPES, "основное") == 4
    assert server._match_value_type(TYPES, "Номенкл") is None  # two names contain it
    assert server._match_value_type(TYPES, "") is None


def test_the_type_is_picked_by_position_not_by_the_cursor_left_by_a_table_read(client):
    result = asyncio.run(server.choose_value_type("Номенклатура", session_id="s"))

    assert result["selected"] is True and result["selected_type"] == "Номенклатура"
    assert result["form_name_after"] == "Справочник.Номенклатура.ФормаВыбора"
    assert client.chosen_type == "Номенклатура"  # the cursor was at the last row ("Основное средство") before


def test_unknown_type_lists_the_available_ones_and_does_not_press_ok(client):
    result = asyncio.run(server.choose_value_type("Ценная бумага", session_id="s"))

    assert result["selected"] is False and result["error"] == "type_not_found"
    assert result["available_types"] == TYPES
    assert not [call for call in client.calls if call[0] == "click_button"]


def test_without_the_dialog_the_tool_says_so(client):
    client.window = "Операция"

    assert asyncio.run(server.choose_value_type("Номенклатура", session_id="s")) == {
        "dialog_open": False, "hint": "The «Выбор типа данных» dialog is not the active window.",
    }


def test_without_a_type_the_tool_lists_the_types(client):
    result = asyncio.run(server.choose_value_type(session_id="s"))

    assert result == {"dialog_open": True, "available_types": TYPES}


def test_a_table_entry_that_opens_the_type_dialog_reports_the_types(client):
    result = asyncio.run(server.table_set_field("Хозрасчетный", "СубконтоДт2", "Мишка", session_id="s"))

    assert result["set"] is False and result["type_selection_required"] is True
    assert result["available_types"] == TYPES and "value_type" in result["hint"]


def test_a_table_entry_with_value_type_goes_through_the_type_and_the_selection_form(client):
    result = asyncio.run(
        server.table_set_field("Хозрасчетный", "СубконтоДт2", "Мишка", value_type="Номенклатура", session_id="s")
    )

    assert result["set"] is True and result["type_selected"] == "Номенклатура"
    assert client.cell == "Мишка"
    assert ("choose_current_row", {"name": "Список", "expected_text": "Мишка"}) in client.calls


def test_a_field_entry_with_a_wrong_value_type_stays_unset(client):
    result = asyncio.run(server.set_field_value("Субконто", "Мишка", value_type="Склад", session_id="s"))

    assert result["set"] is False and result["error"] == "type_not_found"


def test_an_error_without_the_dialog_is_raised_as_before(monkeypatch):
    async def call(session_id, method, params, **_kwargs):
        if method == "active_window":
            return {"title": "Операция"}
        raise BridgeError("Таблица не найдена")

    monkeypatch.setattr(server, "_bridge_call_recorded", call)

    with pytest.raises(BridgeError):
        asyncio.run(server.table_set_field("Хозрасчетный", "СубконтоДт2", "Мишка", session_id="s"))


# --- hierarchical lists: a value inside a group -------------------------------------------------------------------


def _choose_client(monkeypatch, *, found: bool, in_group: bool = True):
    calls = []

    async def call(session_id, method, params, **_kwargs):
        calls.append((method, dict(params)))
        if method == "choose_current_row":
            if in_group and not any(name == "dynamic_list_find" for name, _ in calls):
                raise BridgeError("Строка для выбора не найдена в таблице Список: Мишка")
            return {"selected": True}
        if method == "dynamic_list_find":
            return {"matched": found}
        raise AssertionError(method)

    monkeypatch.setattr(server, "_bridge_call_recorded", call)
    return calls


def test_a_value_in_a_group_is_found_by_the_list_search(monkeypatch):
    calls = _choose_client(monkeypatch, found=True)

    result = asyncio.run(server.choose_current_row(expected_text="Мишка", session_id="s"))

    assert result == {"selected": True, "found_by_search": True}
    assert [name for name, _ in calls] == ["choose_current_row", "dynamic_list_find", "choose_current_row"]


def test_a_value_the_search_cannot_find_keeps_the_original_error(monkeypatch):
    _choose_client(monkeypatch, found=False)

    with pytest.raises(BridgeError, match="не найдена в таблице"):
        asyncio.run(server.choose_current_row(expected_text="Мишка", session_id="s"))


def test_a_value_on_the_shown_level_needs_no_search(monkeypatch):
    calls = _choose_client(monkeypatch, found=True, in_group=False)

    assert asyncio.run(server.choose_current_row(expected_text="Услуги", session_id="s")) == {"selected": True}
    assert [name for name, _ in calls] == ["choose_current_row"]


def test_a_composite_entry_reaches_a_value_inside_a_group_of_the_selected_type(client):
    original = client.call

    async def call(session_id, method, params, **kwargs):
        if method == "choose_current_row" and not any(name == "dynamic_list_find" for name, _ in client.calls):
            client.calls.append((method, dict(params)))
            raise BridgeError("Строка для выбора не найдена в таблице Список: Мишка")
        if method == "dynamic_list_find":
            client.calls.append((method, dict(params)))
            return {"matched": True}
        return await original(session_id, method, params, **kwargs)

    server._bridge_call_recorded = call  # restored by the fixture's monkeypatch teardown
    result = asyncio.run(
        server.table_set_field("Хозрасчетный", "СубконтоДт2", "Мишка", value_type="Номенклатура", session_id="s")
    )

    assert result["set"] is True and result["choose"]["found_by_search"] is True


# --- close_form that only asked a question; screenshot of a visible desktop --------------------------------------


def test_close_form_that_opened_a_question_is_not_reported_as_closed(monkeypatch):
    async def call(session_id, method, params, **_kwargs):
        return {
            "closed": True, "form_name_after": "MessageBox",
            "client_diagnostics": {
                "modal_opened": True, "modal_texts": ["Данные были изменены. Сохранить изменения?"],
                "modal_buttons": [{"name": "Button0", "title": "Да"}, {"name": "Button1", "title": "Нет"}],
            },
        }

    monkeypatch.setattr(server, "_bridge_call_recorded", call)

    result = asyncio.run(server.close_form(session_id="s"))

    assert result["closed"] is False and result["reason"] == "confirmation_required"
    assert result["confirmation_buttons"] == ["Да", "Нет"]
    assert "Сохранить изменения" in result["confirmation_text"]


def test_a_closed_form_stays_closed(monkeypatch):
    async def call(session_id, method, params, **_kwargs):
        return {"closed": True, "client_diagnostics": {"modal_opened": False, "modal_buttons": []}}

    monkeypatch.setattr(server, "_bridge_call_recorded", call)

    assert asyncio.run(server.close_form(session_id="s"))["closed"] is True


def test_a_screenshot_without_the_1c_window_does_not_capture_a_visible_desktop(tmp_path, monkeypatch):
    import sys
    import types

    mss_module = types.ModuleType("mss")
    mss_module.__path__ = []
    tools_module = types.ModuleType("mss.tools")
    monkeypatch.setitem(sys.modules, "mss", mss_module)
    monkeypatch.setitem(sys.modules, "mss.tools", tools_module)
    monkeypatch.setattr(server.os_support, "IS_WINDOWS", True)
    monkeypatch.setattr(server.win_desktop, "is_hidden_desktop_name", lambda _name: False)
    monkeypatch.setattr(server, "_active_external_1c_window_geometry", lambda display="": None)
    monkeypatch.setattr(server, "_session", lambda _sid: types.SimpleNamespace(client_display="", display=""))

    with pytest.raises(RuntimeError, match="window=false"):
        server.screenshot(path=str(tmp_path / "shot.png"), window=True, session_id="s")
