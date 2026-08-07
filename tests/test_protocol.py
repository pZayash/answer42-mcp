from pathlib import Path

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


def test_form_close_and_save_uses_platform_wait_for_form_close():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ДобавитьСостояниеОкнаПослеДействияMCP" in module
    assert "ОжидатьЗакрытие()" in module
    assert 'Результат.closed = СвойствоСтруктуры(Результат, "form_closed", Ложь)' in module
    assert "форма не подтвердила закрытие через ОжидатьЗакрытие" in module


def test_set_field_value_suggests_creating_missing_reference_values():
    module = Path("src/cf/DataProcessors/MCPTestManager/Forms/Форма/Ext/Form/Module.bsl").read_text(encoding="utf-8")

    assert "ПодсказкаСозданияСсылочногоЗначенияMCP" in module
    assert "Возможно значение '" in module
    assert "откройте форму выбора" in module
    assert "создайте элемент справочника/вида/единицы" in module
