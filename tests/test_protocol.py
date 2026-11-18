import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_1c import os_support
from mcp_1c.bridge import _bridge_error_text
from mcp_1c.protocol import MANAGER_PROTOCOL_VERSION, BridgeRequest, BridgeResponse
from mcp_1c.server import _decode_log_bytes


def _bsl_function_body(module: str, name: str) -> str:
    start = module.index(f"Функция {name}")
    end = module.index("КонецФункции", start) + len("КонецФункции")
    return module[start:end]


def _bsl_procedure_body(module: str, name: str) -> str:
    start = module.index(f"Процедура {name}")
    end = module.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    return module[start:end]


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


def test_ruff_check_is_part_of_pytest_suite():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src/mcp_1c", "tests"],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 and "No module named ruff" in result.stderr:
        pytest.skip("ruff is not installed")
    assert result.returncode == 0, result.stdout + result.stderr



def test_bridge_timeout_error_includes_last_progress_event(monkeypatch):
    from mcp_1c.bridge import _format_timeout_progress

    monkeypatch.setattr("mcp_1c.bridge.time.monotonic", lambda: 15.0)
    text = _format_timeout_progress({
        "phase": "search_done",
        "method": "click_button",
        "elapsed_ms": 1234,
        "received_monotonic": 10.0,
    })

    assert "last progress" in text
    assert "phase='search_done'" in text
    assert "method='click_button'" in text
    assert "elapsed_ms=1234" in text
    assert "last_progress_age=5.0s" in text


def test_bridge_timeout_deadline_extends_after_recent_progress(monkeypatch):
    from mcp_1c import bridge

    monkeypatch.setattr(bridge, "PROGRESS_GRACE_TIMEOUT", 10.0)

    assert bridge._effective_timeout_deadline(20.0, None) == 20.0
    assert bridge._effective_timeout_deadline(20.0, {"received_monotonic": 5.0}) == 20.0
    assert bridge._effective_timeout_deadline(20.0, {"received_monotonic": 16.4}) == 26.4



def test_command_interface_search_without_section_scans_sections():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    search = _bsl_function_body(module, "ПоискКомандногоИнтерфейсаMCP")
    all_sections = _bsl_function_body(module, "КомандыВсехРазделовКомандногоИнтерфейса")

    assert "КомандыВсехРазделовКомандногоИнтерфейса(АктивноеОкно, Интерфейс, СтрокаПоиска, Максимум, Тайминги)" in search
    assert 'Тайминги.Вставить("sections_count", Разделы.Количество())' in all_sections
    assert 'КомандныйИнтерфейсСлужебныйРазделMCP(ЗаголовокРаздела)' in all_sections
    assert 'КнопкаРаздела.Нажать();' in all_sections
    assert 'Команда.Вставить("section", ЗаголовокРаздела);' in all_sections
    assert 'Тайминги.Вставить("sections_scanned", ПросмотреноРазделов)' in all_sections
    assert "Совпадения = ПоискВКомандахКомандногоИнтерфейса(Результат, СтрокаПоиска, Максимум)" not in all_sections


def test_command_interface_search_ranks_matches_by_score():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    search_commands = _bsl_function_body(module, "ПоискВКомандахКомандногоИнтерфейса")

    assert 'Оценка = КомандныйИнтерфейсОценкаСовпадения(Заголовок, ТекстПути, Ссылка, НормПоиск)' in search_commands
    assert 'Результат.Вставить(ИндексВставки, Совпадение);' in search_commands
    assert 'Результат.Удалить(Результат.Количество() - 1);' in search_commands
    assert 'КлючиСовпадений = Новый Соответствие;' in search_commands
    assert 'КомандныйИнтерфейсКлючСовпадения(Заголовок, Ссылка)' in search_commands
    assert 'Если Максимум > 0 И Результат.Количество() >= Максимум Тогда' not in search_commands


def test_test_client_maximize_returns_compact_window_summary():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    maximize = _bsl_function_body(module, "РазвернутьОкноКлиентаТестирования")

    assert "КраткоеОписаниеОбъектаMCP(АктивноеОкно)" in maximize
    assert "ОбъектВСтруктуру(АктивноеОкно, 0)" not in maximize

def test_form_close_does_not_block_on_save_discard_message_box():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ДобавитьСостояниеОкнаПослеДействияMCP" in module
    assert "если 1С открыла штатный" in module
    assert "MessageBox" in module
    assert "Сохранить изменения?" in module
    assert "close_blocked_by_modal_dialog" in module
    close_form = module[module.index("Функция ЗакрытьФорму()"):module.index("Функция ПроверитьСостояниеКлиентаПослеШага")]
    assert "ДобавитьСостояниеОкнаПослеДействияMCP(Результат, ЗаголовокДоЗакрытия, ФормаДоЗакрытия, Ложь)" in close_form
    assert "ДобавитьСостояниеОкнаПослеДействияMCP(Результат, ЗаголовокДоЗакрытия, ФормаДоЗакрытия, Истина)" not in close_form


def test_set_field_value_suggests_creating_missing_reference_values():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ПодсказкаСозданияСсылочногоЗначенияMCP" in module
    assert "Возможно значение '" in module
    assert "откройте форму выбора" in module
    assert "создайте элемент справочника/вида/единицы" in module


def test_dropdown_create_variants_are_opt_in_for_field_input():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    selector = _bsl_function_body(module, "ИндексРелевантногоВариантаСпискаВыбораMCP")
    setter = _bsl_function_body(module, "УстановитьЗначениеПоля")

    assert "Функция ЭтоВариантСозданияСпискаВыбораMCP" in module
    assert 'СтрНайти(ТекстНРег, "создать:") = 1' in module
    assert "СоздатьЕслиОтсутствует = Ложь" in selector
    assert "ЭтоВариантСозданияСпискаВыбораMCP(Представление) И Не СоздатьЕслиОтсутствует" in selector
    assert "Продолжить;" in selector
    assert 'СвойствоСтруктуры(Параметры, "create_if_missing", Ложь)' in setter
    assert "ИндексРелевантногоВариантаСпискаВыбораMCP(Представления, Значение, СоздатьЕслиОтсутствует)" in setter
    assert "window_opened" in setter


def test_dropdown_create_variants_are_opt_in_for_table_input():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    table_setter = _bsl_function_body(module, "УстановитьПолеТаблицыMCP")
    editor = _bsl_function_body(module, "ВвестиТекстВРедакторТаблицыMCP")
    row_values = _bsl_function_body(module, "УстановитьЗначенияСтрокиТаблицыMCP")

    assert 'СвойствоСтруктуры(Параметры, "create_if_missing", Ложь)' in table_setter
    assert "ВвестиТекстВРедакторТаблицыMCP(Таблица, Поле, ИмяПоля, Значение, СоздатьЕслиОтсутствует)" in table_setter
    assert "СоздатьЕслиОтсутствует = Ложь" in editor
    assert "ИндексРелевантногоВариантаСпискаВыбораMCP(Представления, Значение, СоздатьЕслиОтсутствует)" in editor
    assert "window_opened" in table_setter
    assert "ДобавитьДиагностикуМодальногоОкнаЕслиОткрылосьMCP" in editor
    assert "table,field,value,search_by_title,create_if_missing" in row_values


def test_set_field_value_closes_missing_dropdown_variant_before_fallbacks():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    setter = _bsl_function_body(module, "УстановитьЗначениеПоля")

    assert "ЗакрытьВыпадающийСписокПоляИАктуализироватьMCP" in module
    assert "Это не доказывает, что" in setter
    assert "web-клиент 8.5 может зависнуть" in setter
    assert "настоящий элемент формы" in module
    assert "ЗакрытьВсеВыпадающиеСпискиMCP" in module
    assert "Предыдущий неудачный ввод мог оставить открытые dropdown-ы" in setter
    assert setter.index("ЗакрытьВсеВыпадающиеСпискиMCP(АктивноеОкно);") < setter.index("Поле = НайтиОбъектРекурсивно")
    assert setter.index("ЗакрытьВсеВыпадающиеСпискиMCP(ТестируемоеПриложение.ПолучитьАктивноеОкно())") < setter.index('ВызватьИсключение "Не удалось установить поле ')
    explicit_open = setter[setter.index("// Поля со стандартным выпадающим списком."):setter.index("// Последние fallback-варианты")]
    cleanup_call = "Поле = ЗакрытьВыпадающийСписокПоляИАктуализироватьMCP(Поле, ИскомоеИмяПоля, ЗаголовокПоля);"
    assert cleanup_call in explicit_open
    assert "Прямой ВыполнитьВыборИзСпискаВыбора пропущен: create_if_missing=false" in explicit_open
    assert explicit_open.index("Если СоздатьЕслиОтсутствует Тогда") < explicit_open.index("Поле.ВыполнитьВыборИзСпискаВыбора(Значение);")
    assert "ИндексРелевантногоВариантаСпискаВыбораMCP(Представления, Значение, СоздатьЕслиОтсутствует)" in explicit_open
    assert explicit_open.index("ИндексВыбора = Неопределено") < explicit_open.index(cleanup_call)
    assert setter.index(cleanup_call) < setter.index("Поле.Значение = Значение")
    assert "оставить фокус на временном редакторе" in setter
    assert setter.index("Поле.Активизировать();") < setter.index("Поле.ВвестиТекст(Значение);")
    assert "СписокВыбораПроверенИЗначениеНеНайдено" in setter
    assert "Если Не СписокВыбораПроверенИЗначениеНеНайдено Тогда" in setter
    assert "Fallback-и выбора из списка пропущены" in setter
    assert setter.index("СписокВыбораПроверенИЗначениеНеНайдено = Истина") < setter.index("Если Не СписокВыбораПроверенИЗначениеНеНайдено Тогда")
    assert "retry_after_exception" in setter
    assert "ИскомоеИмяПоля = ?(ИскатьПоЗаголовку, \"\", ИмяПоля);" in setter
    assert "НайтиОбъектРекурсивно(АктивноеОкноПослеОшибкиВвода, ИскомоеИмяПоля, ЗаголовокПоля, \"ТестируемоеПолеФормы\", 0)" in setter
    assert "ФормаПоля.ПерейтиКСледующемуЭлементу()" in module
    assert "ВосстановитьПолеПослеНеудачногоВводаMCP" in module
    assert "У ТестируемоеПолеФормы нет отдельного метода ОтменитьРедактирование" in module
    assert "Поле.ВвестиТекст(ПредставлениеВосстановления);" in module
    assert "восстановление редактора" in setter
    assert setter.index("РезультатВосстановленияРедактора = ВосстановитьПолеПослеНеудачногоВводаMCP") < setter.index("Fallback-и выбора из списка пропущены")



def test_set_field_value_has_no_new_python_x11_editor_reset():
    server = Path("src/mcp_1c/server.py").read_text(encoding="utf-8")

    assert "needs_x11_editor_reset" not in server
    assert "_x11_reset_test_client_editor" not in server
    assert "ONEC_MCP_DISABLE_X11_EDITOR_RESET" not in server
    assert 'xdotool", "key' not in server


def test_set_field_value_direct_choice_fallbacks_require_bsl_confirmation():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    setter = _bsl_function_body(module, "УстановитьЗначениеПоля")

    optimistic_returns = [
        'Возврат Новый Структура("set,field,value,method", Истина, ИмяПоля, Значение, "ВвестиТекст.retry_after_exception")',
        'Возврат Новый Структура("set,field,value,method", Истина, ИмяПоля, Значение, "Форма.ВыполнитьВыборИзСписка")',
        'Возврат Новый Структура("set,field,value,method", Истина, ИмяПоля, Значение, "Форма.ВыполнитьВыборИзМеню")',
        'Возврат Новый Структура("set,field,value,method", Истина, ИмяПоля, Значение, "Поле.ВыполнитьВыборИзСписка")',
        'Возврат Новый Структура("set,field,value,method", Истина, ИмяПоля, Значение, "Поле.ВыполнитьВыборИзМеню")',
    ]
    for optimistic_return in optimistic_returns:
        assert optimistic_return not in setter

    assert "ПроверкаПовторногоВвода = ПодтвердитьУстановкуПоляMCP" in setter
    assert "ПроверкаВыбораФормы = ПодтвердитьУстановкуПоляMCP" in setter
    assert "ПроверкаВыбораМенюФормы = ПодтвердитьУстановкуПоляMCP" in setter
    assert "ПроверкаВыбораПоля = ПодтвердитьУстановкуПоляMCP" in setter
    assert "ПроверкаВыбораМенюПоля = ПодтвердитьУстановкуПоляMCP" in setter


def test_table_row_text_matching_does_not_use_json_for_user_values():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    find_row = _bsl_function_body(module, "НайтиСтрокуТаблицы")
    goto_row = _bsl_function_body(module, "ПерейтиКСтрокеТаблицыMCP")
    choose_positioning = _bsl_function_body(module, "ПерейтиКСтрокеТаблицыПоТекстуMCP")

    assert "Процедура ДобавитьТекстЗначенияСтрокиТаблицыMCP" in module
    assert 'Тип("Структура") Или ТипЗнч(Значение) = Тип("Соответствие")' in module
    for body in (find_row, goto_row, choose_positioning):
        assert "ТекстСтрокиТаблицыMCP(СтрокаТаблицы)" in body
        assert "СтрНайти(НРег(ЗначениеВJSON(СтрокаТаблицы))" not in body


def test_structured_table_row_matching_requires_all_tokens():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    matcher = _bsl_function_body(module, "СтрокаСоответствуетТекстамMCP")
    selector = _bsl_function_body(module, "ВыбратьСтрокуТаблицыПоТекстамMCP")
    table_setter = _bsl_function_body(module, "УстановитьПолеТаблицыMCP")

    assert "ВсеТекстыОбязательны" in matcher
    assert "Возврат ВсеТекстыОбязательны И ЕстьНепустойПоиск" in matcher
    assert "ВыбратьСтрокуТаблицыПоТекстамMCP(Таблица, ТекстыПоиска, Лимит = 1000, ВсеТекстыОбязательны = Ложь)" in selector
    assert "СпецификацияСтрокиТаблицыТребуетВсехТекстовMCP" in module
    assert "ВсеТекстыСтрокиОбязательны = СпецификацияСтрокиТаблицыТребуетВсехТекстовMCP(СпецификацияСтроки)" in table_setter
    assert "ВыбратьСтрокуТаблицыПоТекстамMCP(Таблица, ТекстыПоискаСтроки, ЛимитПоискаСтроки, ВсеТекстыСтрокиОбязательны)" in table_setter


def test_choose_current_row_confirms_selection_window_changed():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    chooser = _bsl_function_body(module, "ВыбратьТекущуюСтрокуMCP")
    form_selector = _bsl_function_body(module, "ВыбратьЗначениеПоляЧерезФормуВыбораMCP")

    assert "current_row_unavailable" in chooser
    assert "selection_confirmed" in chooser
    assert "ОкноЗакрылосьИлиСменилось" in chooser
    assert "selection_closed" in form_selector
    assert 'Возврат Новый Структура("selected,source_window,selection_window,search,selection_closed,active_window_after", Ложь' in form_selector


def test_table_set_field_suggests_missing_reference_when_value_not_confirmed():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "Если это ссылочное поле, значение могло не найтись в справочнике" in module
    assert 'ПодсказкаСозданияСсылочногоЗначенияMCP(ИмяПоля, Значение)' in module




def test_set_field_value_reports_creation_window_after_direct_input():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    setter = _bsl_function_body(module, "УстановитьЗначениеПоля")
    modal_diagnostics = _bsl_procedure_body(module, "ДобавитьДиагностикуМодальногоОкнаЕслиОткрылосьMCP")

    assert 'РезультатВвода = Новый Структура("set,field,value,method", Истина, ИмяПоля, Значение, "ВвестиТекст")' in setter
    # третий аргумент — ключ быстрого ожидания "ИмяФормы|ИмяПоля" (см. test_text_input_waits_are_adaptive)
    assert "ДобавитьДиагностикуМодальногоОкнаЕслиОткрылосьMCP(РезультатВвода, ЗаголовокОкнаДоВвода," in setter
    assert 'РезультатВвода.Вставить("window_opened", Истина)' in setter
    assert "РезультатВвода.set = Ложь" in setter
    assert "Возврат РезультатВвода" in setter
    assert "НачалоОжиданияОкна = ТекущаяУниверсальнаяДатаВМиллисекундах()" in modal_diagnostics
    assert "ТаймаутОжиданияОкна = 2000" in modal_diagnostics
    assert "ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоОжиданияОкна < ТаймаутОжиданияОкна" in modal_diagnostics
    assert 'Результат.Вставить("modal_form_name", БезопасноеСвойство(ФормаАктивногоОкна, "ИмяФормы", ""))' in modal_diagnostics
    assert 'СвойствоСтруктуры(ДиагностикаКлиента, "active_window_changed", Ложь)' in module
    assert 'Результат.Вставить("window_opened", Истина)' in module
    assert "Результат.set = Ложь" in module


def test_table_set_field_checkbox_fast_path_requires_boolean_like_value():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    setter = _bsl_function_body(module, "УстановитьПолеТаблицыMCP")

    checkbox_detector = _bsl_function_body(module, "НайтиПолеФлажкаТекущейСтрокиТаблицыMCP")

    assert "ЭтоБулевоПоСтроке = ПроверкаФлажкаДоРедактирования.found И ЗначениеПохожеНаБулевоMCP(ПроверкаФлажкаДоРедактирования.value)" in setter
    assert "ЭтоБулевоПоПолю = ЗначениеПохожеНаБулевоMCP(ПолеФлажкаТекущейСтроки.presentation)" in setter
    assert 'ЭтоПолеФлажка = СвойствоСтруктуры(ПолеФлажкаТекущейСтроки, "kind", "") = "Поле флажка"' in setter
    assert "Если ПолеФлажкаТекущейСтроки.found И (ЭтоПолеФлажка Или ЭтоБулевоПоСтроке Или ЭтоБулевоПоПолю) Тогда" in setter
    assert "Если ПолеФлажкаТекущейСтроки.found И (ПроверкаФлажкаДоРедактирования.found Или ЗначениеПохожеНаБулевоMCP" not in setter
    assert 'ВидПоля = Строка(Поле.Вид)' in checkbox_detector
    assert 'Если ВидПоля = "Поле флажка" Или ЗначениеПохожеНаБулевоMCP(Представление) Тогда' in checkbox_detector
    assert 'Возврат Новый Структура("found,field,presentation,kind,control", Истина, Кандидат, Представление, ВидПоля, Поле)' in checkbox_detector
    assert 'ПереключитьПолеОбъектMCP(ПолеФлажкаТекущейСтроки.control, ПолеФлажкаТекущейСтроки.field, Значение)' in setter
    assert 'НажатьПослеОшибкиУстановитьОтметку' in module
    assert 'НажатьПослеОшибкиСнятьОтметку' in module

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


def test_table_field_matcher_rejects_longer_neighbor_column_names():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    matcher = _bsl_function_body(module, "ПолеТаблицыСовпадаетMCP")

    assert "ТоварыНоменклатураЕдиницаИзмерения" in matcher
    assert "ИмяОканчиваетсяНаПоле" in matcher
    assert "Прав(НормИмяТек, СтрДлина(НормИмяПоля)) = НормИмяПоля" in matcher
    assert "СтрНайти(НормИмяТек, НормИмяПоля) > 0" not in matcher


def test_table_set_field_reenters_edit_mode_after_row_positioning_race():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    setter = _bsl_function_body(module, "УстановитьПолеТаблицыMCP")

    assert "РезультатПозиционированияСтроки = ВыбратьСтрокуТаблицыПоТекстамMCP" in setter
    assert "РезультатПовторногоРедактированияПослеАктивизации = ОбеспечитьРедактированиеТекущейСтрокиТаблицыMCP" in setter
    assert "РезультатАктивизацииКолонки = АктивизироватьКолонкуТаблицыMCP(Таблица, ИмяПоля, ИскатьПоЗаголовку)" in setter
    assert "РезультатПовторногоРедактированияПередВводом = ОбеспечитьРедактированиеТекущейСтрокиТаблицыMCP" in setter
    assert "АктивноеОкноПовторногоВвода = ТестируемоеПриложение.ПолучитьАктивноеОкно()" in setter
    assert "ПовторноеПоле = НайтиПолеРедактораТаблицыMCP" in setter
    assert "Таблица " + '" + ИмяТаблицы + "' + " вышла из режима редактирования перед вводом" in setter


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
    find_body = _bsl_function_body(module, "НайтиВДинамическомСпискеMCP")

    assert "СовпадениеНайдено" in find_body
    assert "no_current_row_after_search" in find_body
    assert "match_method" in find_body
    assert "found,searched,matched,match_reason,match_method" in find_body




def test_dynamic_list_find_uses_standard_find_dialog_without_row_scan_fallback():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    find_body = _bsl_function_body(module, "НайтиВДинамическомСпискеMCP")

    assert "ПредварительныйКандидат" not in find_body
    assert "pre_search_navigation" not in find_body
    assert "navigation_fallback" not in find_body
    assert "ПерейтиКСтрокеТаблицыMCP" not in find_body
    assert "УстановитьПолеГдеИскатьОкнаПоискаMCP" in find_body
    assert "УстановитьРежимПоискаОкнаПоискаMCP" in find_body
    assert '"standard_find_dialog"' in find_body


def test_dynamic_list_find_search_mode_maps_user_terms_to_find_dialog_compare_type():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    normalize_body = _bsl_function_body(module, "НормализоватьРежимПоискаДинамическогоСпискаMCP")
    code_body = _bsl_function_body(module, "КодРежимаПоискаДинамическогоСпискаMCP")

    assert 'Норм = "содержит"' in normalize_body
    assert 'Норм = "подстрока"' in normalize_body
    assert 'Возврат "По части строки"' in normalize_body
    assert 'Возврат "По началу строки"' in normalize_body
    assert 'Возврат "По точному совпадению"' in normalize_body
    assert 'Возврат 0' in code_body
    assert 'Возврат 1' in code_body
    assert 'Возврат 2' in code_body


def _run_1c_command(argv: list[str], *, timeout: int = 300, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_manager_cf_passes_1c_configurator_syntax_check(tmp_path):
    platform_dir = Path(os.getenv("ONEC_PLATFORM_DIR") or os.getenv("PLATFORM_DIR") or "/opt/1cv8/x86_64/8.5.1.1150")
    ibcmd = platform_dir / os_support.executable_name("ibcmd")
    onec = platform_dir / os_support.executable_name("1cv8")
    if not ibcmd.exists() or not onec.exists():
        pytest.skip(f"1C platform tools not found in {platform_dir}")

    source_dir = Path("src/cf")
    cf_path = tmp_path / "MCPTestManager.cf"
    db_path = tmp_path / "ib"
    log_path = tmp_path / "check-modules.log"

    build = _run_1c_command([str(ibcmd), "config", "import", f"--out={cf_path}", str(source_dir)], timeout=600)
    assert build.returncode == 0, build.stdout + build.stderr

    create = _run_1c_command([str(ibcmd), "infobase", "create", f"--db-path={db_path}", "--create-database"], timeout=180)
    assert create.returncode == 0, create.stdout + create.stderr

    load = _run_1c_command([str(ibcmd), "config", "load", f"--db-path={db_path}", str(cf_path)], timeout=300)
    assert load.returncode == 0, load.stdout + load.stderr

    apply = _run_1c_command([
        str(ibcmd),
        "config",
        "apply",
        f"--db-path={db_path}",
        "--force",
        "--session-terminate=force",
    ], timeout=300, input_text="3\n")
    assert apply.returncode == 0, apply.stdout + apply.stderr

    designer_cmd = [
        str(onec),
        "DESIGNER",
        "/F",
        str(db_path),
        "/CheckModules",
        "-ThinClient",
        "-Server",
        "/Out",
        str(log_path),
        "/DisableStartupDialogs",
    ]
    if os.name != "nt" and not os.getenv("DISPLAY") and shutil.which("xvfb-run"):
        designer_cmd = ["xvfb-run", "-a", *designer_cmd]

    check = _run_1c_command(designer_cmd, timeout=300)
    # Windows Designer writes /Out in UTF-8 with BOM or in the ANSI code page.
    check_log = _decode_log_bytes(log_path.read_bytes()) if log_path.exists() else ""
    assert check.returncode == 0, check.stdout + check.stderr + check_log
    assert "Синтаксических ошибок не обнаружено" in check_log


def test_dynamic_list_settings_available_fields_uses_lightweight_timeout_bounded_scan():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    available_fields = _bsl_function_body(module, "ПолучитьДоступныеПоляНастроекСпискаMCP")

    assert "ЛегкаяСтрокаДоступногоПоляНастроекСпискаMCP" in module
    assert "МаксДлительностьМС = 5000" in available_fields
    assert "available_fields traversal timeout" in available_fields
    assert "ТекстТекущегоДоступногоПоляНастроекСпискаMCP(Таблица)" in available_fields
    assert 'Таблица.ПолучитьТекстЯчейки("Доступные поля")' in module
    assert "ЗначениеВJSON(СтруктураСтроки)" not in available_fields
    assert 'Новый Структура("fields,count,errors,timeout"' in available_fields
    assert 'РезультатПолей.Вставить("expanded", Разворачиваний)' in available_fields


def test_dynamic_list_set_settings_does_not_bypass_table_edit_guard():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    set_field = _bsl_function_body(module, "УстановитьЗначениеПоля")
    settings_setter = _bsl_function_body(module, "УстановитьПолеЭлементаНастройкиСпискаMCP")

    assert "skip_table_edit_check" not in module
    assert "ПропуститьПроверкуРедактированияТаблицы" not in module
    assert "ПроверитьРедактированиеРодительскойТаблицыПередВводомMCP" in set_field
    assert "ОбеспечитьРедактированиеТекущейСтрокиТаблицыMCP" in settings_setter
    assert "строка настройки не перешла в режим редактирования" in settings_setter
    assert "ВвестиТекст(Значение)" not in settings_setter


def test_dynamic_list_set_settings_refreshes_active_window_after_available_field_choice():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")

    choose_index = add_setting.index("КнопкаВыбрать.Нажать()")
    refresh_index = add_setting.index("АктивноеОкно = ТестируемоеПриложение.ПолучитьАктивноеОкно()", choose_index)
    table_lookup_index = add_setting.index("Таблица = НайтиТаблицуПоПараметрам", choose_index)

    assert "После выбора форма перестраивается" in add_setting
    assert choose_index < refresh_index < table_lookup_index


def test_dynamic_list_set_settings_filter_values_are_set_via_settings_table_editor():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")
    set_settings = _bsl_function_body(module, "УстановитьНастройкиДинамическогоСпискаMCP")
    table_helper = _bsl_function_body(module, "УстановитьКолонкуСтрокиНастройкиСпискаВТаблицеMCP")

    assert "Quick-поля формы здесь не используем" in add_setting
    assert "Для отбора здесь только добавляем строку" in add_setting
    assert "ДозаполнитьТабличныеПоляОтборовНастройкиСпискаMCP(Параметры.filters)" in set_settings
    assert "ПерейтиКСтрокеНастройкиСпискаПоПолюMCP(РабочаяТаблица, ПолеНастройки, 200, НомерСовпадения)" in table_helper
    assert "УстановитьПолеТаблицыMCP(ПараметрыКолонки)" in table_helper
    set_table_field = _bsl_function_body(module, "УстановитьПолеТаблицыMCP")
    assert "ОбеспечитьРедактированиеТекущейСтрокиТаблицыMCP(Таблица, ИмяТаблицы)" in set_table_field
    assert "ВвестиТекстВВидимыйРедакторТаблицыБезРежимаMCP" in set_table_field
    assert "РезультатНачалаРедактированияВыбора = ОбеспечитьРедактированиеТекущейСтрокиТаблицыMCP(Таблица, ИмяТаблицы)" in set_table_field
    visible_fallback = _bsl_function_body(module, "ВвестиТекстВВидимыйРедакторТаблицыБезРежимаMCP")
    assert "visible_table_editor_without_confirmed_edit_mode.ВыполнитьВыборИзСпискаВыбора" in visible_fallback
    assert 'ВыбратьИзСпискаРедактораТаблицыMCP(Поле, ИмяПоля, Значение, СоздатьЕслиОтсутствует, "visible_table_editor_without_confirmed_edit_mode")' in visible_fallback
    assert "ВидимыйРедакторТаблицыДоступенMCP" in visible_fallback
    assert "АктивироватьРазделНастроекСпискаMCP(Раздел)" in table_helper
    assert "table_goto_row + table_set_field" in table_helper
    assert "ЗакончитьРедактированиеСтроки(Ложь)" in table_helper
    assert "ТекущаяСтрокаНастройкиУжеСодержитЗначениеMCP" in table_helper
    assert "ДозаполнитьБыстрыеПоляОтборовНастройкиСпискаMCP(Параметры.filters)" not in set_settings
    assert "ДозаполнитьТабличныеПоляОтборовНастройкиСпискаMCP" in module
    assert "per-field quick-группы" not in set_settings


def test_removed_bridge_methods_are_not_registered():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    server = Path("src/mcp_1c/server.py").read_text(encoding="utf-8")

    assert "dynamic_list_filter_by_current_value" not in server
    assert "dynamic_list_filter_by_current_value" not in module
    assert "ОтборДинамическогоСпискаПоТекущемуЗначениюMCP" not in module
    assert "КнопкаКонтекстногоМенюТаблицыMCP" not in module
    assert "create_new_item" not in server
    assert "create_new_item" not in module
    assert "СоздатьНовыйЭлемент" not in module


def test_dynamic_list_set_settings_repositions_to_added_setting_row_before_section_specific_handling():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")

    assert "ПерейтиКСтрокеНастройкиСпискаПоПолюMCP" in module
    assert "АктивизироватьТекущуюСтрокуНастройкиСпискаMCP" in module
    assert "ПозиционированиеСтроки = ПерейтиКСтрокеНастройкиСпискаПоПолюMCP(Таблица, Поле, 200)" in add_setting
    position_index = add_setting.index("ПозиционированиеСтроки = ПерейтиКСтрокеНастройкиСпискаПоПолюMCP")
    filter_index = add_setting.index("НомерЭлементаНастроекСпискаMCP(Раздел) = 0")
    assert position_index < filter_index


def test_table_goto_row_supports_occurrence_for_duplicate_filter_rows():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    goto_row = _bsl_function_body(module, "ПерейтиКСтрокеТаблицыMCP")

    assert 'СвойствоСтруктуры(Параметры, "occurrence", 1)' in goto_row
    assert "НайденоСовпадений = НайденоСовпадений + 1" in goto_row
    assert "НайденоСовпадений = ПорядковыйНомерСовпадения" in goto_row
    assert 'Новый Структура("found,row,index,occurrence,matches"' in goto_row
    assert 'Новый Структура("found,rows_checked,matches,occurrence"' in goto_row


def test_dynamic_list_set_settings_uses_occurrence_when_post_filling_duplicate_filter_fields():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    fill_filters = _bsl_function_body(module, "ДозаполнитьТабличныеПоляОтборовНастройкиСпискаMCP")
    table_helper = _bsl_function_body(module, "УстановитьКолонкуСтрокиНастройкиСпискаВТаблицеMCP")
    wrapper = _bsl_function_body(module, "УстановитьКолонкуСтрокиНастройкиСпискаЧерезТаблицуMCP")

    assert "СчетчикиПолей = Новый Соответствие" in fill_filters
    assert "НомерСовпаденияПоля = НомерСовпаденияПоля + 1" in fill_filters
    assert 'СчетчикиПолей.Вставить(ИмяПоляОтбора, НомерСовпаденияПоля)' in fill_filters
    assert "ПерейтиКСтрокеНастройкиСпискаПоПолюMCP(РабочаяТаблица, ПолеНастройки, 200, НомерСовпадения)" in table_helper
    assert "УстановитьКолонкуСтрокиНастройкиСпискаВТаблицеMCP(Неопределено" in wrapper
    assert "НомерСовпадения" in wrapper
    assert '"filter", ИмяПоляОтбора, "Вид сравнения", СравнениеОтбора, Истина, Ошибки, НомерСовпаденияПоля' in fill_filters
    assert '"filter", ИмяПоляОтбора, "Значение", ЗначениеОтбора, Истина, Ошибки, НомерСовпаденияПоля' in fill_filters


def test_dynamic_list_set_settings_order_direction_uses_settings_table_editor_not_quick_fallback():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")

    # Direction must be written in the settings form table itself, using the same
    # table-editor path as table_set_field.  Do not mask failures with quick form
    # fields or post-close list ordering fallback.
    assert "УстановитьПолеТаблицыMCP(ПараметрыНаправления)" in add_setting
    assert 'Новый Структура("table,field,value,search_by_title", ИмяТаблицы, ЗаголовокКолонкиНаправления' in add_setting
    assert "УстановитьБыстрыеПоляПорядкаНастройкиСпискаMCP" not in add_setting
    assert 'УстановитьПолеЭлементаНастройкиСпискаMCP(Таблица, ИмяТаблицы, "ТипУпорядочивания"' not in add_setting


def test_dynamic_list_set_settings_order_direction_activates_direction_column_by_title():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    editor = _bsl_function_body(module, "НайтиПолеРедактораТаблицыMCP")

    assert 'АктивизироватьКолонкуТаблицыMCP(Таблица, "Направление сортировки", Истина)' in editor
    assert 'Таблица.ПерейтиКСледующемуЭлементу();' not in editor

def test_choice_list_variant_prefers_display_text_for_settings_enum_structures():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    variant = _bsl_function_body(module, "ПредставлениеВариантаСпискаВыбораMCP")

    display_index = variant.index('Представление.Свойство("ОтображаемыйТекст")')
    data_index = variant.index('Представление.Свойство("ПредставлениеДанных")')
    assert display_index < data_index
    assert 'ПредставлениеДанных="Структура"' in variant



def test_dynamic_list_set_settings_desc_order_must_be_set_in_settings_form_not_list_fallback():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")
    set_settings = _bsl_function_body(module, "УстановитьНастройкиДинамическогоСпискаMCP")

    assert "УстановитьПорядокДинамическогоСпискаMCP" not in set_settings
    assert "ПрименитьНаправленияПорядкаКСпискуMCP" not in module
    assert "list_order" not in set_settings
    assert "Направление сортировки для поля" in add_setting

def test_dynamic_list_set_settings_does_not_bypass_table_edit_guard_for_generic_tables():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    setter = _bsl_function_body(module, "УстановитьПолеЭлементаНастройкиСпискаMCP")

    assert 'Лев(ИмяТаблицы, СтрДлина("КомпоновщикНастроек")) = "КомпоновщикНастроек"' in setter
    assert "РазрешитьБезРежимаРедактирования" in module


def test_tabular_document_save_fast_mode_returns_after_first_successful_attempt():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    save_body = _bsl_function_body(module, "СохранитьТабличныйДокументMCP")
    index_body = _bsl_function_body(module, "ИндексФильтраДиалогаТабличногоДокументаMCP")

    assert 'ИндексФильтраПараметр = СвойствоСтруктуры(Параметры, "file_dialog_filter_index", 0)' in save_body
    assert 'БыстроеСохранение = СвойствоСтруктуры(Параметры, "exact_filter_only", Ложь) Или ИндексФильтраПараметр > 0' in save_body
    assert "Если БыстроеСохранение Тогда" in save_body
    assert "НачальныйФильтр = Макс(ИндексФильтраПараметр, Макс(ИндексТочногоФильтра, 1))" in save_body
    assert "КонечныйФильтр = НачальныйФильтр" in save_body
    assert "write_content_to_file_dialog_filters_fast" in save_body
    assert "Возврат Новый Структура" in save_body
    assert "НачальныйФильтр = 1" in save_body
    assert "КонечныйФильтр = 30" in save_body
    assert "перебора" in index_body
    assert "Возврат 0;" in index_body
    assert 'Возврат 2;' not in index_body
    assert 'Возврат 7;' not in index_body
    assert 'Возврат 6;' not in index_body



def test_tabular_document_save_uses_last_dot_when_appending_attempt_suffix():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    body = _bsl_function_body(module, "ДобавитьСуффиксКПутиMCP")

    assert "Пока Индекс >= 1 Цикл" in body
    assert "Индекс = Индекс - 1;" in body
    assert 'Возврат Лев(Путь, ПозицияТочки - 1) + Суффикс + Сред(Путь, ПозицияТочки);' in body

def test_tabular_document_save_clears_file_dialog_result_queue_around_each_attempt():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    body = _bsl_function_body(module, "СохранитьТабличныйДокументMCP")

    clear_call = "ТестируемоеПриложение.ОчиститьРезультатДиалогаВыбораФайла();"
    set_call = "ТестируемоеПриложение.УстановитьРезультатДиалогаВыбораФайла(Истина, ПутьПопытки, ИндексФильтра);"
    write_call = "Поле.ЗаписатьСодержимоеВФайл(СохранитьКак);"

    assert clear_call in body
    assert body.index(clear_call) < body.index(set_call) < body.index(write_call)
    assert body.count(clear_call) >= 3
    assert "ошибка очистки очереди результата диалога выбора файла" in body


def test_client_diagnostics_collects_user_messages_via_test_application_api_and_clears_them():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    server = Path("src/mcp_1c/server.py").read_text(encoding="utf-8")

    assert "Функция ТекстИсключенияMCP(Инфо, Метод)" in module
    assert 'ТекстОшибки = "Команда " + Метод + " завершилась исключением без текста ошибки"' in module
    assert "АктивноеОкно.ПолучитьТекстыСообщенийПользователю()" in module
    assert "АктивноеОкно.ЗакрытьПанельСообщенийПользователю()" in module
    assert 'Результат.Вставить("user_message_texts", СнимокСообщений.message_texts)' in module
    assert 'Результат.Вставить("user_messages_cleared", СнимокСообщений.cleared)' in module
    assert 'Результат.Вставить("has_user_messages", Ложь)' in module
    assert 'Результат.has_user_messages = СообщенияПользователю.Количество() > 0' in module
    assert 'Результат.Вставить("has_dialog", Ложь)' in module
    assert 'Результат.Вставить("events", Новый Массив)' in module
    assert 'Новый Структура("kind,texts,messages", "user_messages"' in module
    assert 'Новый Структура("kind,title,form_name,text,buttons,is_error_window", "dialog"' in module
    assert 'Результат.Вставить("active_window_changed"' in module
    assert 'ДиагностикаКлиента = ПроверитьСостояниеКлиентаПослеШага(ЗаголовокОкнаДоКоманды, ИмяФормыОкнаДоКоманды)' in module
    assert '"user_messages_panel_closed"' in module
    assert 'ПричинаСообщений <> "user_messages_panel_closed"' in module
    assert "ПолучитьСообщенияПользователю()" not in module
    assert "ТестируемоеПриложение.ОчиститьСообщения()" not in module
    assert 'ТекстОшибки = ТекстОшибки + Символы.ПС + "client_diagnostics=" + ДиагностикаJSON' in module
    assert "except BridgeError as exc:" not in server


def test_bridge_default_timeout_is_longer_for_slow_1c_tools():
    from mcp_1c.bridge import OneCBridgeServer

    assert OneCBridgeServer().request_timeout == 180.0


def test_manager_logs_click_button_search_stats():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "click_button: search_done" in module
    assert "search_stats" in module
    assert "fast_platform_search" in module
    assert "dfs_fallback" in module
    assert "Статистика.buttons_checked" in module
    assert "Статистика.nodes" in module


def test_post_step_diagnostics_are_not_skipped_for_confirm_buttons():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    click_body = _bsl_function_body(module, "НажатьКнопку")

    assert "skip_post_step_diagnostics" not in module
    assert "НужноПропуститьДиагностикуПослеНажатияКнопкиMCP" not in module
    assert "ПропуститьДиагностикуПослеШага" not in click_body
    assert "ДобавитьСостояниеОкнаПослеДействияMCP(Результат, ЗаголовокДоНажатия, ФормаДоНажатия, ОжидатьЗакрытияФормы)" in click_body


def test_post_step_diagnostics_emit_progress_phases():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    body = _bsl_function_body(module, "ПроверитьСостояниеКлиентаПослеШага")

    for phase in [
        "post_step_active_window_start",
        "post_step_active_window_done",
        "post_step_user_messages_start",
        "post_step_user_messages_done",
        "post_step_error_info_start",
        "post_step_error_info_done",
        "post_step_validation_start",
        "post_step_validation_done",
        "post_step_window_summary_start",
        "post_step_window_summary_done",
        "post_step_error_window_check_start",
        "post_step_error_window_check_done",
        "post_step_modal_details_start",
        "post_step_modal_details_done",
    ]:
        assert phase in body
    assert "post_step_diagnostics_done" in module
    assert "response_payload_ready" in module


def test_post_step_diagnostics_do_not_scan_whole_window_for_error_window():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    body = _bsl_function_body(module, "ПроверитьСостояниеКлиентаПослеШага")

    assert 'Результат.modal_is_error_window = ИмяФормыАктивногоОкна = "ErrorWindow"' in body
    assert "ЕстьФормаОшибкиРекурсивно(АктивноеОкно, 0)" not in body


def test_manager_uses_strict_match_for_form_command_names():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert 'СтрНайти(ИмяКнопки, "Форма") = 1' in module
    assert "ФормаПровести совпадает" in module


def test_manager_sends_progress_events_for_slow_ui_commands():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ОтправитьСобытиеПрогрессаMCP" in module
    assert 'Событие.Вставить("type", "event")' in module
    assert 'Событие.Вставить("event", "progress")' in module
    assert 'Событие.Вставить("id", ТекущийИдентификаторЗапросаMCP)' in module
    assert 'ОтправитьСобытиеПрогрессаMCP("click_button", "search_done"' in module
    assert 'ОтправитьСобытиеПрогрессаMCP("ui_tree", "active_window_done"' in module
    assert 'ОтправитьСобытиеПрогрессаMCP("find_object", "fast_done"' in module
    assert 'ОтправитьСобытиеПрогрессаMCP("search_objects", "visited"' in module


def test_manager_find_object_has_fast_path_and_search_stats():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "НайтиОбъектБыстроMCP" in module
    assert "ТипТестируемогоОбъектаMCP" in module
    assert "fast_platform_search" in module
    assert "dfs_fallback" in module
    assert "search_stats" in module
    assert "ПустаяСтрока(Имя) И ПустаяСтрока(Заголовок) И ПустаяСтрока(ТипОбъектаСтрокой)" in module
    assert "НайтиОбъектРекурсивно(АктивноеОкно, Имя, Заголовок, ТипОбъекта, 0, СчетчикУзлов, КешПоиска)" in module
    assert "Кандидат = РодительскийОбъект.НайтиОбъект(ТипОбъекта, ПараметрЗаголовок, ПараметрИмя, 0)" in module
    assert "СтрНайти(НормализованныйЗаголовок, НРег(ИскомыйЗаголовокСтрокой))" in module


def test_manager_search_objects_serializes_matches_shallow_by_default():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert 'ВключатьДетей = СвойствоСтруктуры(Параметры, "include_children", Ложь)' in module
    assert "НайтиВсеОбъектыБыстроMCP" in module
    assert "ПараметрПоискаТестируемогоОбъектаMCP" in module
    assert "Кандидаты = РодительскийОбъект.НайтиОбъекты(ТипОбъекта, ПараметрЗаголовок, ПараметрИмя, 0)" in module
    assert "fast_platform_search_objects" in module
    assert "ОбъектВСтруктуру(РодительскийОбъект, 0, Истина, Истина, Истина, Ложь, КешПоиска, Истина, ВключатьДетей)" in module
    assert "ПостФильтрНайденныхОбъектовMCP" in module
    assert "СтрНайти(ЗаголовокОбъекта, ИскомыйЗаголовокСтрокой)" in module
    assert "СтрНайти(ТипОбъекта, ИскомыйТипНорм)" in module



def test_manager_find_button_fast_path_uses_unspecified_empty_search_parameters():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert 'Варианты.Добавить(Новый Структура("title,name", Неопределено, ИмяКнопки))' in module
    assert 'Варианты.Добавить(Новый Структура("title,name", ЗаголовокКнопки, Неопределено))' in module
    assert 'Кнопка = НайтиКнопкуБыстроMCP(АктивноеОкно, ИмяКнопки, ЗаголовокКнопки, КешПоиска)' in module
    assert 'Кнопка = НайтиКнопку(АктивноеОкно, ИмяКнопки, ЗаголовокКнопки, 1, Истина, Истина, Ложь, КешПоиска)' in module
    assert 'Сначала проверяем все варианты только быстрым платформенным поиском' in module
    assert 'dfs_fallback_after_fast_misses' in module
    assert 'Тайминги.Вставить(Префикс + "_find_method", "fast_platform_search")' in module
    assert 'Тайминги.Вставить(Префикс + "_find_nodes", КешПоиска.Статистика.nodes)' in module
    assert module.index('Новый Структура("name,title", "ФормаВывестиСписок", "")') < module.index('Новый Структура("name,title", "СписокВывестиСписок", "")')
    assert module.index('Новый Структура("name,title", "", "ОК")') < module.index('Новый Структура("name,title", "", "Сформировать")')

def test_ui_tree_current_page_is_shallow_and_command_panels_can_be_disabled():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert 'ВключатьКомандныеПанели = СвойствоСтруктуры(Параметры, "include_command_panels", Ложь)' in module
    assert 'ИмяРодителя = СвойствоСтруктуры(Параметры, "parent_name", "")' in module
    assert 'КорневойОбъект = НайтиОбъектБыстроMCP(АктивноеОкно, ИмяРодителя, "", "", КешСериализации)' in module
    assert 'Результат.Вставить("root_parent_name", ИмяРодителя)' in module
    assert 'ПлоскийРежим = БулевоИзЗначенияMCP(СвойствоСтруктуры(Параметры, "flat", Ложь), Ложь)' in module
    assert 'ПлоскоеДеревоИнтерфейсаMCP' in module
    assert 'НайтиУзлыДереваИнтерфейсаПлоскоMCP' in module
    assert 'НайтиУзлыДереваИнтерфейсаБыстроMCP' in module
    assert 'Метод = "fast_platform_search_objects"' in module
    assert 'СвойствоСтруктуры(Параметры, "limit"' not in _bsl_function_body(module, "ДеревоИнтерфейса")
    assert 'СвойствоСтруктуры(Параметры, "limit"' not in _bsl_function_body(module, "ПлоскоеДеревоИнтерфейсаMCP")
    assert 'Результат.Вставить("current_page_name", ИмяТекущейСтраницы)' in module
    assert "КешСериализации, ВключатьДанные, Ложь, СчетчикУзлов, Ложь" in module
    assert "Если ЭтоГруппаСтраницДереваИнтерфейсаMCP(Объект, ТипОбъекта) Тогда" in module
    assert 'ВидГруппы = НРег(СтрЗаменить(ВидГруппыФормы(Объект), " ", ""))' in module
    assert "Если Не ТипУзлаДереваИнтерфейсаКонтейнерMCP(ТипОбъекта) Тогда" in module

    body = _bsl_function_body(module, "ОбъектВСтруктуру")
    decoration_branch = body[body.index("ИначеЕсли ЭтоДекорация Тогда"):body.index("ИначеЕсли Не ВключатьСостояниеДоступности Тогда")]
    assert "ТекущаяДоступность" not in decoration_branch
    assert "ТекущееТолькоПросмотр" not in decoration_branch
    assert "Если ВключатьКомандныеПанели И (ТипОбъекта" in module




def test_command_interface_search_restores_workspace_after_scan():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    search = _bsl_function_body(module, "ПоискКомандногоИнтерфейсаMCP")
    cleanup = _bsl_procedure_body(module, "ЗакрытьКомандныйИнтерфейсПослеРаботыMCP")

    assert "ИсходноеАктивноеОкно = ТестируемоеПриложение.ПолучитьАктивноеОкно()" in search
    assert "ЗакрытьКомандныйИнтерфейсПослеРаботыMCP(АктивноеОкно, КнопкаРаздела, ИсходноеАктивноеОкно)" in search
    assert "ОкноДляАктивации = ИсходноеАктивноеОкно" in cleanup
    assert "ОкноДляАктивации.Активизировать();" in cleanup
    assert "Форма = НайтиОбъектРекурсивно(ОкноДляАктивации" in cleanup
    assert "ПерейтиКНачальнойСтранице" not in cleanup
    assert "ВыполнитьКоманду(ИсходнаяСсылка)" not in module

def test_command_interface_search_closes_help_panel_after_cleanup():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    help_window = _bsl_function_body(module, "ЭтоОкноПомощиКомандногоИнтерфейсаMCP")
    service_section = _bsl_function_body(module, "КомандныйИнтерфейсСлужебныйРазделMCP")

    all_sections = _bsl_function_body(module, "КомандыВсехРазделовКомандногоИнтерфейса")

    assert "Процедура ЗакрытьКомандныйИнтерфейсПослеРаботыMCP" in module
    assert "Процедура ЗакрытьПанелиПомощиПослеКомандногоИнтерфейсаMCP" in module
    assert "ЗакрытьПанелиПомощиПослеКомандногоИнтерфейсаMCP(Окно)" in module
    assert "ТестируемоеПриложение.ПолучитьАктивноеОкно()" in module
    assert "АктивноеОкно.Закрыть();" in module
    assert "ТекущееОкно.Закрыть();" in module
    assert 'СтрНайти(ЗаголовокОкна, "помощ") > 0' in help_window
    assert 'Заголовок = "помощь" Или Заголовок = "help"' in service_section
    assert "КнопкаРаздела.Нажать();" in all_sections
    assert all_sections.count("КнопкаРаздела.Нажать();") >= 3


# ---------------------------------------------------------------------------
# Производительность менеджера на больших формах (БП): быстрые пути и адаптивные ожидания
# ---------------------------------------------------------------------------

def _manager_module() -> str:
    return Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")


def test_find_object_has_guarded_fast_path_with_dfs_fallback():
    body = _bsl_function_body(_manager_module(), "НайтиОбъектРекурсивно")
    fast = body.index("НайтиОбъектБыстроMCP(РодительскийОбъект, Имя, \"\", ТипОбъекта, КешПоиска)")
    # быстрый путь только на верхнем уровне, для точного имени и без заголовка (у заголовка своя семантика совпадения)
    guard = body[body.rindex("Если", 0, fast):fast]
    assert "ТекущаяГлубина = 0" in guard and "Не ПустаяСтрока(Имя)" in guard and "ПустаяСтрока(Заголовок)" in guard
    # прежний обход остаётся запасным путём
    assert "БезопасныеПодчиненные(РодительскийОбъект)" in body[fast:]
    assert body.index("Возврат БыстрыйРезультат") < body.index("БезопасныеПодчиненные(РодительскийОбъект)")


def test_find_table_has_fast_path_only_for_exact_name():
    body = _bsl_function_body(_manager_module(), "НайтиТаблицуПоПараметрам")
    fast = body.index('НайтиОбъектБыстроMCP(РодительскийОбъект, ИмяТаблицы, "", "ТестируемаяТаблицаФормы")')
    guard = body[body.rindex("Если", 0, fast):fast]
    assert "ТекущаяГлубина = 0" in guard and "Не ПустаяСтрока(ИмяТаблицы)" in guard and "ПустаяСтрока(ЗаголовокТаблицы)" in guard
    assert "БезопасныеПодчиненные(РодительскийОбъект)" in body[fast:]


def test_close_dropdowns_uses_platform_search_with_recursive_fallback():
    module = _manager_module()
    body = _bsl_procedure_body(module, "ЗакрытьВсеВыпадающиеСпискиMCP")
    assert 'НайтиОбъекты(Тип("ТестируемоеПолеФормы")' in body
    assert "ЗакрытьВсеВыпадающиеСпискиРекурсивноMCP(РодительскийОбъект, 0)" in body  # запасной путь сохранён
    # вызов по-прежнему идёт до поиска поля (порядок проверяет test_set_field_value_closes_stale_dropdowns...)
    assert "ЗакрытьВсеВыпадающиеСпискиРекурсивноMCP" in module


def test_text_input_waits_are_adaptive():
    module = _manager_module()
    # перечень полей/колонок, для которых ожидание выпадающего списка пропускается, и его инвалидация
    for var in ("ПоляБезВыпадающегоСписка", "ПоляБезМодальногоОкна"):
        assert f"Перем {var};" in module
    setter = _bsl_function_body(module, "УстановитьЗначениеПоля")
    assert "ПоляБезВыпадающегоСписка.Вставить(КлючБезСписка, Истина)" in setter
    assert "ПоляБезВыпадающегоСписка.Удалить(КлючБезСписка)" in setter  # открывшийся список снимает запись
    cell = _bsl_function_body(module, "ВвестиТекстВРедакторТаблицыMCP")
    assert "ПоляБезВыпадающегоСписка.Вставить(КлючБезСписка, Истина)" in cell
    assert "ПоляБезВыпадающегоСписка.Удалить(КлючБезСписка)" in cell
    modal = _bsl_procedure_body(module, "ДобавитьДиагностикуМодальногоОкнаЕслиОткрылосьMCP")
    assert "КлючБыстрогоОжидания = \"\"" in module  # необязательный параметр: прочие вызовы ждут по-старому
    assert "ТаймаутОжиданияОкна = 300" in modal
    assert modal.count("ПоляБезМодальногоОкна.Удалить(КлючБыстрогоОжидания)") == 2  # окно открылось — снова ждём полностью
