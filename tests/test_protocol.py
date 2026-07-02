from pathlib import Path

from mcp_1c.protocol import BridgeRequest, BridgeResponse, MANAGER_PROTOCOL_VERSION


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
