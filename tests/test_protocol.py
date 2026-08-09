from pathlib import Path

from mcp_1c.bridge import _bridge_error_text
from mcp_1c.protocol import MANAGER_PROTOCOL_VERSION, BridgeRequest, BridgeResponse


def test_manager_protocol_version_is_embedded_in_cf_sources():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    assert f'Возврат "{MANAGER_PROTOCOL_VERSION}"' in module


def test_bridge_request_shape():
    request = BridgeRequest(id="1", method="ping", params={"x": 1})
    assert request.to_json() == {"type": "request", "id": "1", "method": "ping", "params": {"x": 1}}


def test_bridge_response_shape():
    response = BridgeResponse.from_json({"type": "response", "id": "1", "ok": True, "result": {"pong": True}})
    assert response.id == "1"
    assert response.ok is True
    assert response.result == {"pong": True}


def test_bridge_error_text_never_empty():
    assert _bridge_error_text(BridgeResponse(id="1", ok=False, error=""), "click_button") == (
        "1C bridge command 'click_button' failed without error text"
    )
    assert _bridge_error_text(BridgeResponse(id="1", ok=False, error="  \n  "), "click_button") == (
        "1C bridge command 'click_button' failed without error text"
    )
    assert _bridge_error_text(BridgeResponse(id="1", ok=False, error=" boom "), "click_button") == "boom"


def test_form_close_does_not_block_on_save_discard_message_box():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ДобавитьСостояниеОкнаПослеДействияMCP" in module
    assert "если 1С открыла штатный" in module
    assert "MessageBox" in module
    assert "Сохранить изменения?" in module
    assert "close_blocked_by_modal_dialog" in module
    close_form = module[module.index("Функция ЗакрытьФорму()"):module.index("Функция ПроверитьСостояниеКлиентаПослеШага()")]
    assert "ДобавитьСостояниеОкнаПослеДействияMCP(Результат, ЗаголовокДоЗакрытия, ФормаДоЗакрытия, Ложь)" in close_form
    assert "ДобавитьСостояниеОкнаПослеДействияMCP(Результат, ЗаголовокДоЗакрытия, ФормаДоЗакрытия, Истина)" not in close_form


def test_set_field_value_suggests_creating_missing_reference_values():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ПодсказкаСозданияСсылочногоЗначенияMCP" in module
    assert "Возможно значение '" in module
    assert "откройте форму выбора" in module
    assert "создайте элемент справочника/вида/единицы" in module


def test_table_set_field_suggests_missing_reference_when_value_not_confirmed():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "Если это ссылочное поле, значение могло не найтись в справочнике" in module
    assert 'ПодсказкаСозданияСсылочногоЗначенияMCP(ИмяПоля, Значение)' in module


def test_table_set_field_requires_edit_mode_before_cell_input():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ПроверитьРедактированиеРодительскойТаблицыПередВводомMCP" in module
    assert "ВвестиТекстВРедакторТаблицыMCP" in module
    assert "table_editor.ВвестиТекст" in module
    assert "table_editor.ВвестиТекст+ВыполнитьВыборИзСпискаВыбора" in module
    assert "ОткрытВыпадающийСписокПоляMCP(Поле)" in module
    assert "поле таблицы нельзя изменять, пока таблица не находится в режиме редактирования строки" in module
    assert "ТекущийРежимРедактирование() остался Ложь" in module
    assert "ввод в поле " in module
    assert "запрещен" in module


def test_close_form_returns_structured_message_box_diagnostics_instead_of_blank_error():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ДобавитьДиагностикуАктивногоОкнаMCP" in module
    assert "ЛокальноеИмяФормыДиалога" in module
    assert "MessageBox" in module
    assert 'Префикс + "_looks_like_dialog"' in module
    assert "close_failed" in module
    assert "Возврат Результат" in module
    assert "Ошибка закрытия формы" not in module


def test_object_search_prefers_visible_available_non_readonly_form_fields():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ОбъектИнтерактивноДоступенMCP" in module
    assert "ТекущееТолькоПросмотр" in module
    assert 'ТипОбъекта = "тестируемоеполеформы"' in module
    assert "Возврат Не ТолькоПросмотр" in module
    assert "ОбъектВидимИДоступенMCP" in module


def test_dynamic_list_find_distinguishes_search_execution_from_row_match():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "СовпадениеНайдено" in module
    assert "no_current_row_after_search" in module
    assert "found,searched,matched,match_reason" in module


def test_client_diagnostics_collects_user_messages_via_test_application_api_and_clears_them():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    server = Path("src/mcp_1c/server.py").read_text(encoding="utf-8")

    assert "Функция ТекстИсключенияMCP(Инфо, Метод)" in module
    assert 'ТекстОшибки = "Команда " + Метод + " завершилась исключением без текста ошибки"' in module
    assert "АктивноеОкно.ПолучитьТекстыСообщенийПользователю()" in module
    assert "АктивноеОкно.ЗакрытьПанельСообщенийПользователю()" in module
    assert 'Результат.Вставить("user_message_texts", СнимокСообщений.message_texts)' in module
    assert 'Результат.Вставить("user_messages_cleared", СнимокСообщений.cleared)' in module
    assert '"user_messages_panel_closed"' in module
    assert 'ПричинаСообщений <> "user_messages_panel_closed"' in module
    assert "ПолучитьСообщенияПользователю()" not in module
    assert "ТестируемоеПриложение.ОчиститьСообщения()" not in module
    assert 'ТекстОшибки = ТекстОшибки + Символы.ПС + "client_diagnostics=" + ДиагностикаJSON' in module
    assert "except BridgeError as exc:" not in server
