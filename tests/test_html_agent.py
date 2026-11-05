import asyncio
import json
import struct
import time

import pytest
import websockets

from mcp_1c import html_agent


def test_ops_expression_calls_the_ops_function_with_json_args():
    expression = html_agent.ops_expression("type", {"selector": "textarea", "value": 'Привет "мир"'})
    assert expression.startswith("(function (op, args) {")
    assert expression.endswith(')("type", {"selector": "textarea", "value": "Привет \\"мир\\""})')


def test_loader_is_one_short_ascii_line_with_port_and_token():
    loader = html_agent.loader_js(4321, "tok-EN_1")
    assert "ws://127.0.0.1:4321/answer42-page/tok-EN_1'" in loader
    assert loader.isascii() and "\n" not in loader and "__PORT__" not in loader and "__TOKEN__" not in loader
    assert len(loader) < 140  # typed character by character into the console


async def _fake_page(port, token, replies, origin="https://chat.test"):
    """Play the page side: open the socket with the token, take the bridge, say hello, answer each expression."""
    url = f"ws://127.0.0.1:{port}{html_agent.PAGE_PATH}{token}"
    async with websockets.connect(url, additional_headers={"Origin": origin}) as ws:
        bridge = await ws.recv()
        assert bridge == html_agent.PAGE_BRIDGE_JS
        await ws.send(json.dumps({"type": "hello"}))
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
                    if inner["method"] == "Console.enable":
                        self.received.append("Console.enable " + outer["params"]["targetId"])
                        for level, text in (("error", "SyntaxError: Unexpected token '.'"), ("log", "hello")):
                            self.frontend({"method": "Target.dispatchMessageFromTarget", "params": {
                                "targetId": outer["params"]["targetId"], "message": json.dumps({"method": "Console.messageAdded", "params": {
                                    "message": {"source": "javascript", "level": level, "text": text, "url": "https://chat.test/app.js", "line": 3}}})}})
                        continue
                    self.frontend({"method": "Target.dispatchMessageFromTarget", "params": {
                        "targetId": outer["params"]["targetId"], "message": json.dumps(self.reply(outer["params"]["targetId"], inner))}})
        except asyncio.IncompleteReadError:
            pass

    def reply(self, target_id, inner):
        expression = inner["params"]["expression"]
        if '"info"' in expression:
            value = {"ok": True, "result": {"url": "https://chat.test/", "title": "Chat", "target": target_id}}
        elif '"check_scripts"' in expression:
            value = {"ok": True, "result": {"scripts": 2, "checked": 2, "errors": [{"script": "https://chat.test/app.js", "error": "SyntaxError: Unexpected token '.'"}]}}
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
            console = await inspector.console("", clear=True)
            assert [m["text"] for m in console["messages"]] == ["SyntaxError: Unexpected token '.'"]
            assert console["messages"][0]["line"] == 3 and console["collected_since"] == "page load"
            assert console["scripts"]["errors"][0]["script"] == "https://chat.test/app.js"
            assert (await inspector.console("", levels=frozenset(), check_scripts=False))["messages"] == []
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
    assert received[:3] == ["SetupInspectorClient", "Setup", "SendMessageToBackend"]
    assert {"Console.enable page-1", "Console.enable page-2"} <= set(received)


def test_console_through_the_page_bridge():
    async def scenario():
        inspector = html_agent.HtmlInspector()
        port = await inspector.start()
        token, connected = inspector.expect(hwnd=7)
        messages = [{"level": "error", "text": "boom", "source": "javascript"}, {"level": "log", "text": "fine"}]
        page_side = asyncio.create_task(_fake_page(port, token, [
            {"value": {"ok": True, "result": {"hooked": True, "messages": messages}}},
            {"value": {"ok": True, "result": {"scripts": 1, "checked": 1, "errors": []}}},
        ]))
        try:
            await asyncio.wait_for(connected, 5)
            inspector.open_inspectors = _no_new_pages
            result = await inspector.console("")
            await page_side
            return result
        finally:
            await inspector.stop()

    result = asyncio.run(scenario())
    assert [m["text"] for m in result["messages"]] == ["boom"] and result["total"] == 1
    assert result["scripts"] == {"scripts": 1, "checked": 1, "errors": []}


async def _no_new_pages():
    return []


@pytest.mark.parametrize(("windows", "mode", "expected"), [
    (True, "auto", "auto"), (True, "", "auto"), (True, "Console", "console"), (True, "patched", "patched"),
    (False, "auto", "remote"), (False, "remote", "remote"), (False, "patched", "patched"),
    (True, "disabled", "disabled"), (False, "disabled", "disabled"),
])
def test_resolve_mode(monkeypatch, windows, mode, expected):
    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", windows)
    assert html_agent.resolve_mode(mode) == expected


@pytest.mark.parametrize(("windows", "mode", "message"), [
    (True, "remote", "Linux"), (True, "off", "Unknown"), (False, "off", "Unknown"),
    (False, "console", "not available on Linux"),  # XTEST keys would land in the page, not the inspector
])
def test_resolve_mode_rejects_modes_of_the_other_platform(monkeypatch, windows, mode, message):
    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", windows)
    with pytest.raises(html_agent.HtmlAgentError, match=message):
        html_agent.resolve_mode(mode)


def test_patched_mode_needs_the_bridge_in_the_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", True)
    inspector = html_agent.HtmlInspector("patched")
    assert inspector.remote_port == 0 and inspector.status()["mode"] == "patched"
    with pytest.raises(html_agent.HtmlAgentError, match="html_inspector_patch"):
        inspector.check_platform(str(tmp_path / "8.5.1.1343" / "bin" / "1cv8c.exe"))
    html_agent.HtmlInspector("console").check_platform(str(tmp_path / "1cv8c.exe"))  # console needs nothing


def test_windows_auto_prefers_the_patched_inspector(tmp_path, monkeypatch):
    from mcp_1c import webkit_patch

    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", True)
    bin_dir = tmp_path / "8.5.1.1343" / "bin"
    ui = webkit_patch.ui_dir(bin_dir)
    ui.mkdir(parents=True)
    (ui / "Main.html").write_text("<html><head></head></html>", encoding="utf-8")
    plain = html_agent.HtmlInspector("auto")
    plain.check_platform(str(bin_dir / "1cv8c.exe"))
    assert plain.mode == "console"
    webkit_patch._apply_install(bin_dir)
    patched = html_agent.HtmlInspector("auto")
    patched.check_platform(str(bin_dir / "1cv8c.exe"))
    assert patched.mode == "patched"


def test_linux_patched_mode_overlays_the_inspector_strings(tmp_path, monkeypatch):
    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", False)
    inspector = html_agent.HtmlInspector("patched")
    inspector.overlay_dir = tmp_path / "overlay"
    env = inspector.client_env()
    assert env == {"G_RESOURCE_OVERLAYS": f"{html_agent.INSPECTOR_STRINGS_RESOURCE}={tmp_path / 'overlay'}"}
    strings = (tmp_path / "overlay" / html_agent.INSPECTOR_STRINGS_FILE).read_text(encoding="utf-8")
    assert strings.startswith("var localizedStrings = {};") and "window.ANSWER42_BRIDGE = null;" in strings
    html_agent.write_inspector_strings(tmp_path / "overlay", 4321, "tok")
    strings = (tmp_path / "overlay" / html_agent.INSPECTOR_STRINGS_FILE).read_text(encoding="utf-8")
    assert 'window.ANSWER42_BRIDGE = {"port": 4321, "token": "tok"};' in strings and "Target.dispatchMessageFromTarget" in strings
    assert html_agent.HtmlInspector("remote").client_env()["WEBKIT_INSPECTOR_SERVER"].startswith("127.0.0.1:")


def test_linux_local_modes_need_xdotool(monkeypatch):
    monkeypatch.setattr(html_agent.os_support, "IS_WINDOWS", False)
    monkeypatch.setattr(html_agent.shutil, "which", lambda name: None)
    with pytest.raises(html_agent.HtmlAgentError, match="xdotool"):
        html_agent.HtmlInspector("patched").check_platform("/opt/1cv8/x86_64/8.5.1.1343/1cv8c")
    html_agent.HtmlInspector("remote").check_platform("/opt/1cv8/x86_64/8.5.1.1343/1cv8c")  # remote needs nothing


async def _fake_gtk_front_end(port, token, inner_results, outer_error=False):
    """A WebKitGTK front end: the page is the target page-7, replies come back inside it."""
    url = f"ws://127.0.0.1:{port}{html_agent.webkit_patch.INSPECTOR_PATH}{token}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "version": 1}))
        await ws.send(json.dumps({"type": "target", "message": {"method": "Target.targetCreated", "params": {
            "targetInfo": {"targetId": "page-7", "type": "page"}}}}))
        async for raw in ws:
            outer = json.loads(raw)["message"]
            assert outer["method"] == "Target.sendMessageToTarget" and outer["params"]["targetId"] == "page-7"
            inner = json.loads(outer["params"]["message"])
            assert inner["method"] == "Runtime.evaluate" and inner["id"] != outer["id"]
            if outer_error:
                await ws.send(json.dumps({"type": "message", "message": {"id": outer["id"], "error": {"message": "No target"}}}))
                return
            await ws.send(json.dumps({"type": "message", "message": {"id": outer["id"], "result": {}}}))
            await ws.send(json.dumps({"type": "message", "message": {"id": inner["id"], "result": inner_results.pop(0)}}))
            if not inner_results:
                return


def test_gtk_front_end_bridge_goes_through_the_target_domain():
    async def scenario():
        inspector = html_agent.HtmlInspector()
        port = await inspector.start()
        try:
            token, connected = inspector.expect(hwnd=1, path=html_agent.webkit_patch.INSPECTOR_PATH)
            front = asyncio.create_task(_fake_gtk_front_end(port, token, [_value({"ok": True, "result": "hello"})]))
            page = await asyncio.wait_for(connected, 5)
            for _ in range(50):
                if page.page_target:
                    break
                await asyncio.sleep(0.02)
            text = await inspector.run(page, "text", {}, timeout=5)
            await front
            token, connected = inspector.expect(hwnd=2, path=html_agent.webkit_patch.INSPECTOR_PATH)
            front = asyncio.create_task(_fake_gtk_front_end(port, token, [], outer_error=True))
            failing = await asyncio.wait_for(connected, 5)
            for _ in range(50):
                if failing.page_target:
                    break
                await asyncio.sleep(0.02)
            with pytest.raises(html_agent.HtmlAgentError, match="No target"):
                await inspector.run(failing, "text", {}, timeout=5)
            await front
            return page, text
        finally:
            await inspector.stop()

    page, text = asyncio.run(scenario())
    assert page.page_target == "page-7" and text == "hello"


def _value(value):
    return {"result": {"type": "object", "value": value}, "wasThrown": False}


def _fake_windows(monkeypatch, inspector, views, opens_on_press):
    """Fake client windows: the inspector front end appears on the given press of the hot key."""
    presses = []

    def press(view):
        presses.append(view)
        if len(presses) == opens_on_press:
            views.append(900)

    monkeypatch.setattr(html_agent, "HOTKEY_REPEAT_SECONDS", 0.2)
    monkeypatch.setattr(html_agent, "INSPECTOR_OPEN_SECONDS", 2.0)
    monkeypatch.setattr(html_agent, "_run_on_client_desktop", lambda desktop, fn: fn())
    monkeypatch.setattr(html_agent, "_press_inspector_hotkey", press)
    monkeypatch.setattr(html_agent, "find_inspector_windows",
                        lambda desktop, pid: {901: "Web Inspector — about:blank"} if 900 in views else {})

    async def current_views():
        return list(views)

    monkeypatch.setattr(inspector, "_views", current_views)
    return presses


def test_front_end_repeats_a_missed_inspector_hot_key(monkeypatch):
    inspector = html_agent.HtmlInspector()
    views = [100]
    presses = _fake_windows(monkeypatch, inspector, views, opens_on_press=2)
    front_end = asyncio.run(inspector._front_end(100))
    assert front_end == 900 and presses == [100, 100]
    assert 900 in inspector._known_front_ends


def test_front_end_reports_a_view_that_disappeared(monkeypatch):
    inspector = html_agent.HtmlInspector()
    views = [100]
    _fake_windows(monkeypatch, inspector, views, opens_on_press=0)
    views.clear()  # the client recreated the field: its old view is gone
    started = time.monotonic()
    with pytest.raises(html_agent.ViewGoneError):
        asyncio.run(inspector._front_end(100))
    assert time.monotonic() - started < 1.0
