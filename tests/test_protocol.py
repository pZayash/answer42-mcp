import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_1c.bridge import _bridge_error_text
from mcp_1c.protocol import MANAGER_PROTOCOL_VERSION, BridgeRequest, BridgeResponse


def _bsl_function_body(module: str, name: str) -> str:
    start = module.index(f"Функция {name}")
    end = module.index("КонецФункции", start) + len("КонецФункции")
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
    find_body = _bsl_function_body(module, "НайтиВДинамическомСпискеMCP")

    assert "СовпадениеНайдено" in find_body
    assert "no_current_row_after_search" in find_body
    assert "match_method" in find_body
    assert "found,searched,matched,match_reason,match_method" in find_body




def test_dynamic_list_find_returns_clean_pre_search_match_before_opening_find_dialog():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    find_body = _bsl_function_body(module, "НайтиВДинамическомСпискеMCP")

    pre_index = find_body.index('ПредварительныйКандидат = ПерейтиКСтрокеТаблицыMCP')
    return_index = find_body.index('match_method,table,field,text,compare_type,errors,current_row')
    dialog_index = find_body.index('Кнопка = НайтиКнопку')
    assert pre_index < return_index < dialog_index
    assert '"pre_search_navigation"' in find_body
    assert 'Новый Массив, СвойствоСтруктуры(ПредварительныйКандидат, "row"' in find_body

def test_dynamic_list_find_preserves_row_when_standard_search_loses_current_row():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    find_body = _bsl_function_body(module, "НайтиВДинамическомСпискеMCP")

    assert "ПредварительныйКандидат" in find_body
    assert "pre_search_navigation_candidate" in find_body
    assert "ПерейтиКСтрокеТаблицыMCP" in find_body
    assert find_body.index("ПредварительныйКандидат = ПерейтиКСтрокеТаблицыMCP") < find_body.index("Кнопка.Нажать()")
    assert 'ТекущаяСтрока = СвойствоСтруктуры(ПредварительныйКандидат, "row", ТекущаяСтрока)' in find_body


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
    ibcmd = platform_dir / "ibcmd"
    onec = platform_dir / "1cv8"
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
        "--dynamic=force",
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
    check_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
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
    assert "ПерейтиКСтрокеТаблицыMCP(ПараметрыПерехода)" in table_helper
    assert "УстановитьПолеТаблицыMCP(ПараметрыКолонки)" in table_helper
    assert "АктивироватьРазделНастроекСпискаMCP(Раздел)" in table_helper
    assert "table_goto_row + table_set_field" in table_helper
    assert "ЗакончитьРедактированиеСтроки(Ложь)" in table_helper
    assert "ТекущаяСтрокаНастройкиУжеСодержитЗначениеMCP" in table_helper
    assert 'Новый Структура("name,title,text,limit", ИмяТаблицы, "", ПолеНастройки, 200)' in table_helper
    assert "ДозаполнитьБыстрыеПоляОтборовНастройкиСпискаMCP(Параметры.filters)" not in set_settings
    assert "ДозаполнитьТабличныеПоляОтборовНастройкиСпискаMCP" in module
    assert "per-field quick-группы" not in set_settings


def test_dynamic_list_filter_by_current_value_bridge_method_is_removed():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    server = Path("src/mcp_1c/server.py").read_text(encoding="utf-8")

    assert "dynamic_list_filter_by_current_value" not in server
    assert "dynamic_list_filter_by_current_value" not in module
    assert "ОтборДинамическогоСпискаПоТекущемуЗначениюMCP" not in module
    assert "КнопкаКонтекстногоМенюТаблицыMCP" not in module


def test_dynamic_list_set_settings_repositions_to_added_setting_row_before_section_specific_handling():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")

    assert "ПерейтиКСтрокеНастройкиСпискаПоПолюMCP" in module
    assert "АктивизироватьТекущуюСтрокуНастройкиСпискаMCP" in module
    assert "ПозиционированиеСтроки = ПерейтиКСтрокеНастройкиСпискаПоПолюMCP(Таблица, Поле, 200)" in add_setting
    position_index = add_setting.index("ПозиционированиеСтроки = ПерейтиКСтрокеНастройкиСпискаПоПолюMCP")
    filter_index = add_setting.index("НомерЭлементаНастроекСпискаMCP(Раздел) = 0")
    assert position_index < filter_index


def test_dynamic_list_set_settings_order_direction_uses_settings_table_editor_not_quick_fallback():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")
    add_setting = _bsl_function_body(module, "ДобавитьЭлементНастройкиСпискаMCP")

    # Direction must be written in the settings form table itself, using the same
    # table-editor path as table_set_field.  Do not mask failures with quick form
    # fields or post-close list ordering fallback.
    assert "УстановитьПолеТаблицыMCP(ПараметрыНаправления)" in add_setting
    assert 'Новый Структура("table,field,value,search_by_title", ИмяТаблицы, "Направление сортировки"' in add_setting
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
    assert "fast_platform_search" in module
    assert "dfs_fallback" in module
    assert "search_stats" in module
    assert "НайтиОбъектРекурсивно(АктивноеОкно, Имя, Заголовок, ТипОбъекта, 0, СчетчикУзлов, КешПоиска)" in module
    assert "СтрНайти(НормализованныйЗаголовок, НРег(ИскомыйЗаголовокСтрокой))" in module


def test_manager_search_objects_serializes_matches_shallow_by_default():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert 'ВключатьДетей = СвойствоСтруктуры(Параметры, "include_children", Ложь)' in module
    assert "ОбъектВСтруктуру(РодительскийОбъект, 0, Истина, Истина, Истина, Ложь, КешПоиска, Истина, ВключатьДетей)" in module
    assert "ПостФильтрНайденныхОбъектовMCP" in module
    assert "СтрНайти(ЗаголовокОбъекта, ИскомыйЗаголовокСтрокой)" in module


def test_ui_tree_current_page_is_shallow_and_command_panels_can_be_disabled():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert 'ВключатьКомандныеПанели = СвойствоСтруктуры(Параметры, "include_command_panels", Истина)' in module
    assert 'Результат.Вставить("current_page_name", ИмяТекущейСтраницы)' in module
    assert "КешСериализации, ВключатьДанные, Ложь, СчетчикУзлов, Ложь" in module
    assert "Если ВключатьКомандныеПанели И (ТипОбъекта" in module
