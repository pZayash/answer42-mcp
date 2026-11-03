import asyncio
import json
import struct

import pytest
import websockets

from mcp_1c import html_agent


def test_ops_expression_calls_the_ops_function_with_json_args():
    expression = html_agent.ops_expression("type", {"selector": "textarea", "value": 'Привет "мир"'})
    assert expression.startswith("(function (op, args) {")
    assert expression.endswith(')("type", {"selector": "textarea", "value": "Привет \\"мир\\""})')


def test_loader_is_one_ascii_line_with_port_and_token():
    loader = html_agent.loader_js(4321, "tok-EN_1")
    assert "ws://127.0.0.1:4321/" in loader and "'tok-EN_1'" in loader
    assert loader.isascii() and "\n" not in loader and "__" not in loader
    assert loader.endswith(")") and loader.count("(") == loader.count(")")


async def _fake_page(port, token, replies, origin="https://chat.test"):
    """Play the page side of the loader: hello with the token, then answer each expression in turn."""
    async with websockets.connect(f"ws://127.0.0.1:{port}/answer42-page", additional_headers={"Origin": origin}) as ws:
        await ws.send(json.dumps({"type": "hello", "token": token}))
        async for raw in ws:
            sent = json.loads(raw)
            assert sent["expression"].startswith("(function (op, args) {")
            await ws.send(json.dumps({"type": "reply", "id": sent["id"], **replies.pop(0)}))
            if not replies:
                return


def test_bridge_runs_page_operations_and_reports_page_errors():
    async def scenario():
        inspector = html_agent.HtmlInspector()
        port = await inspector.start()
        token, connected = inspector.expect(hwnd=42)
        page_side = asyncio.create_task(_fake_page(port, token, [
            {"value": {"ok": True, "result": {"url": "https://chat.test/", "title": "Chat"}}},
            {"value": {"ok": True, "result": "hello"}},
            {"value": {"ok": False, "error": "element not found: #x"}},
            {"error": "SyntaxError: bad"},
        ]))
        try:
            page = await asyncio.wait_for(connected, 5)
            await inspector.refresh(page)
            assert inspector.resolve_page("").page_id == page.page_id
            assert inspector.resolve_page("chat.test") is page
            text = await inspector.run(page, "text", {"selector": "body"}, timeout=5)
            with pytest.raises(html_agent.HtmlAgentError, match="element not found"):
                await inspector.run(page, "click", {"selector": "#x"}, timeout=5)
            with pytest.raises(html_agent.HtmlAgentError, match="SyntaxError"):
                await inspector.run(page, "eval", {"code": "("}, timeout=5)
            await page_side
            for _ in range(50):
                if page.closed:
                    break
                await asyncio.sleep(0.02)
            return page, text, inspector.status()
        finally:
            await inspector.stop()

    page, text, status = asyncio.run(scenario())
    assert text == "hello" and page.hwnd == 42
    assert page.to_json()["title"] == "Chat"
    assert page.closed and status["pages_alive"] == 0


def test_bridge_rejects_unknown_and_reused_tokens():
    async def scenario():
        inspector = html_agent.HtmlInspector()
        port = await inspector.start()
        try:
            with pytest.raises(websockets.ConnectionClosed):
                await _fake_page(port, "guessed", [{"value": None}])
            token, connected = inspector.expect()
            first = asyncio.create_task(_fake_page(port, token, [{"value": {"ok": True, "result": None}}]))
            page = await asyncio.wait_for(connected, 5)
            with pytest.raises(websockets.ConnectionClosed):
                await _fake_page(port, token, [{"value": None}])
            await inspector.run(page, "info", {}, timeout=5)
            await first
            return list(inspector.pages)
        finally:
            await inspector.stop()

    assert asyncio.run(scenario()) == ["p1"]


def test_resolve_page_errors():
    inspector = html_agent.HtmlInspector()
    inspector.last_error = "did not connect"
    with pytest.raises(html_agent.HtmlAgentError, match="No live HTML page.*did not connect"):
        inspector.resolve_page("")
    inspector.pages["p1"] = html_agent.InspectorPage("p1", None, url="https://one.test/", title="One")
    inspector.pages["p2"] = html_agent.InspectorPage("p2", None, url="https://two.test/", title="Two")
    with pytest.raises(html_agent.HtmlAgentError, match="Several live HTML pages"):
        inspector.resolve_page("")
    assert inspector.resolve_page("two").page_id == "p2"
    inspector.pages["p2"].closed = True
    assert inspector.resolve_page("").page_id == "p1"


def _gv_target(target_id, kind, title, url):
    """Serialize one (tsssb) target the way GVariant does (little endian)."""
    parts = [kind.encode() + b"\0", title.encode() + b"\0", url.encode() + b"\0"]
    data, ends = struct.pack("<Q", target_id), []
    for part in parts:
        data += part
        ends.append(len(data))
    data += b"\0"  # hasLocalDebugger
    width = 1 if len(data) + 3 <= 0xFF else 2
    return data + b"".join(end.to_bytes(width, "little") for end in reversed(ends))


def _gv_target_list(connection_id, targets):
    array, ends = b"", []
    for target in targets:
        array += b"\0" * (-len(array) % 8) + _gv_target(*target)
        ends.append(len(array))
    if ends:
        width = 1 if len(array) + len(ends) <= 0xFF else 2
        array += b"".join(end.to_bytes(width, "little") for end in ends)
    return struct.pack("<Q", connection_id) + array


def test_decode_target_list_roundtrip():
    body = _gv_target_list(7, [(5, "WebPage", "", ""), (2, "WebPage", "НейроСотрудник", "https://chat.test/x?token=1")])
    assert html_agent.decode_target_list(body) == (7, [(5, "WebPage", "", ""), (2, "WebPage", "НейроСотрудник", "https://chat.test/x?token=1")])
    assert html_agent.decode_target_list(struct.pack("<Q", 3)) == (3, [])


class _FakeRemoteInspector:
    """The WebKitGTK remote inspector server side: target list, Setup, Target-domain routing."""

    def __init__(self):
        self.received = []
        self.writer = None

    async def handle(self, reader, writer):
        self.writer = writer
        try:
            while True:
                size, flags = struct.unpack(">IB", await reader.readexactly(5))
                name, _, body = (await reader.readexactly(size)).partition(b"\0")
                assert flags == 1
                name = name.decode()
                self.received.append(name)
                if name == "SetupInspectorClient":
                    self.send("DidSetupInspectorClient", b"")
                    self.send("SetTargetList", _gv_target_list(9, []))
                    self.send("SetTargetList", _gv_target_list(1, [(3, "WebPage", "", ""), (2, "WebPage", "Chat", "https://chat.test/")]))
                elif name == "Setup":
                    assert struct.unpack("<QQ", body) == (1, 2)
                    self.frontend({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "page-1", "type": "page"}}})
                elif name == "SendMessageToBackend":
                    outer = json.loads(body[16:].rstrip(b"\0"))
                    inner = json.loads(outer["params"]["message"])
                    self.frontend({"result": {}, "id": outer["id"]})
                    self.frontend({"method": "Target.dispatchMessageFromTarget", "params": {
                        "targetId": outer["params"]["targetId"], "message": json.dumps(self.reply(outer["params"]["targetId"], inner))}})
        except asyncio.IncompleteReadError:
            pass

    def reply(self, target_id, inner):
        expression = inner["params"]["expression"]
        if '"info"' in expression:
            value = {"ok": True, "result": {"url": "https://chat.test/", "title": "Chat", "target": target_id}}
        elif '"click"' in expression:
            return {"id": inner["id"], "result": {"result": {"type": "object", "description": "TypeError: boom"}, "wasThrown": True}}
        else:
            value = {"ok": True, "result": target_id}
        return {"id": inner["id"], "result": {"result": {"type": "object", "value": value}, "wasThrown": False}}

    def send(self, name, body):
        self.writer.write(html_agent.remote_message(name, body))

    def frontend(self, message):
        self.send("SendMessageToFrontend", struct.pack("<QQ", 1, 2) + json.dumps(message).encode() + b"\0")


def test_remote_inspector_pages_on_linux(monkeypatch):
    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", False)

    async def scenario():
        fake = _FakeRemoteInspector()
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        inspector = html_agent.HtmlInspector()
        inspector.remote_port = server.sockets[0].getsockname()[1]
        assert inspector.client_env() == {"WEBKIT_INSPECTOR_SERVER": f"127.0.0.1:{inspector.remote_port}"}
        inspector.attach("", 1234)
        try:
            opened = await inspector.open_inspectors()
            page = inspector.resolve_page("")
            first = await inspector.run(page, "text", {}, timeout=5)
            with pytest.raises(html_agent.HtmlAgentError, match="TypeError: boom"):
                await inspector.run(page, "click", {"selector": "#x"}, timeout=5)
            fake.frontend({"method": "Target.didCommitProvisionalTarget", "params": {"oldTargetId": "page-1", "newTargetId": "page-2"}})
            await asyncio.sleep(0.1)
            second = await inspector.run(page, "text", {}, timeout=5)
            assert await inspector.open_inspectors() == []
            fake.send("SetTargetList", _gv_target_list(1, []))
            await asyncio.sleep(0.1)
            with pytest.raises(html_agent.HtmlAgentError, match="closed"):
                await inspector.run(page, "text", {}, timeout=5)
            return opened, page, first, second, list(fake.received)
        finally:
            await inspector.stop()
            server.close()

    opened, page, first, second, received = asyncio.run(scenario())
    assert opened == ["p1"] and page.target == (1, 2) and page.title == "Chat"
    assert (first, second) == ("page-1", "page-2")
    assert received[:2] == ["SetupInspectorClient", "Setup"]
