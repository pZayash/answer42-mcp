import asyncio
import json

import pytest
import websockets

from mcp_1c import html_agent, webkit_patch

MAIN_HTML = '<html><head>\n<script src="Base/Main.js"></script>\n</head><body></body></html>'


def _fake_platform(root, version="8.5.1.1343"):
    bin_dir = root / version / "bin"
    ui = bin_dir / "WebKit.resources" / "WebInspectorUI"
    ui.mkdir(parents=True)
    (ui / "Main.html").write_text(MAIN_HTML, encoding="utf-8")
    return bin_dir


def test_install_and_uninstall_patch_the_inspector_front_end(tmp_path):
    bin_dir = _fake_platform(tmp_path)
    ui = webkit_patch.ui_dir(bin_dir)
    assert webkit_patch.platform_status(bin_dir)["installed"] is False

    webkit_patch._apply_install(bin_dir)
    webkit_patch._apply_install(bin_dir)  # idempotent
    main = (ui / "Main.html").read_text(encoding="utf-8")
    assert main.count(webkit_patch.BRIDGE_JS_NAME) == 1
    assert main.index(webkit_patch.CONFIG_NAME) < main.index(webkit_patch.BRIDGE_JS_NAME) < main.index("</head>")
    assert (ui / ("Main.html" + webkit_patch.BACKUP_SUFFIX)).read_text(encoding="utf-8") == MAIN_HTML
    assert (ui / webkit_patch.CONFIG_NAME).read_text(encoding="utf-8") == webkit_patch.EMPTY_CONFIG
    status = webkit_patch.platform_status(bin_dir)
    assert status["version"] == "8.5.1.1343" and status["installed"] and status["current"] and status["ready"]

    (ui / webkit_patch.BRIDGE_JS_NAME).write_text("// an older bridge", encoding="utf-8")
    assert webkit_patch.is_ready(bin_dir) is False  # an outdated bridge falls back to the console loader

    webkit_patch._apply_uninstall(bin_dir)
    assert (ui / "Main.html").read_text(encoding="utf-8") == MAIN_HTML
    assert sorted(p.name for p in ui.iterdir()) == ["Main.html"]


def test_change_patches_writable_platforms_without_elevation_and_grants_the_config(tmp_path, monkeypatch):
    bin_dir = _fake_platform(tmp_path)
    calls = []
    monkeypatch.setattr(webkit_patch.os_support, "IS_WINDOWS", True)
    monkeypatch.setattr(webkit_patch, "current_user_sid", lambda: "S-1-5-21-42")
    monkeypatch.setattr(webkit_patch.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(webkit_patch, "_run_elevated", lambda *a: pytest.fail("a writable platform needs no elevation"))
    result = webkit_patch.change("install", [bin_dir])
    assert result["elevated"] is False and "errors" not in result
    assert result["platforms"][0]["ready"] is True
    assert calls == [["icacls", str(webkit_patch.ui_dir(bin_dir) / webkit_patch.CONFIG_NAME), "/grant", "*S-1-5-21-42:M"]]


def test_change_is_windows_only(tmp_path, monkeypatch):
    monkeypatch.setattr(webkit_patch.os_support, "IS_WINDOWS", False)
    with pytest.raises(webkit_patch.WebKitPatchError, match="Windows"):
        webkit_patch.change("install", [_fake_platform(tmp_path)])


def test_opening_inspector_fills_the_config_only_while_held(tmp_path, monkeypatch):
    bin_dir = _fake_platform(tmp_path)
    webkit_patch._apply_install(bin_dir)
    monkeypatch.setenv("ONEC_MCP_WEBKIT_LOCK_DIR", str(tmp_path / "locks"))
    config = webkit_patch.ui_dir(bin_dir) / webkit_patch.CONFIG_NAME
    with webkit_patch.opening_inspector(bin_dir, 4321, "tok"):
        text = config.read_text(encoding="utf-8")
        assert json.loads(text.split("=", 1)[1].rstrip(";\n")) == {"port": 4321, "token": "tok"}
    assert config.read_text(encoding="utf-8") == webkit_patch.EMPTY_CONFIG


async def _fake_front_end(port, token, results):
    """Play a patched inspector front end: relay Runtime.evaluate replies and one console message."""
    async with websockets.connect(f"ws://127.0.0.1:{port}{webkit_patch.INSPECTOR_PATH}{token}") as ws:
        await ws.send(json.dumps({"type": "hello", "version": 1}))
        await ws.send(json.dumps({"type": "event", "message": {"method": "Console.messageAdded", "params": {
            "message": {"source": "javascript", "level": "error", "text": "SyntaxError: Unexpected token '.'", "line": 4}}}}))
        async for raw in ws:
            sent = json.loads(raw)
            assert sent["type"] == "send" and sent["message"]["id"] >= webkit_patch.BRIDGE_BASE_ID
            assert sent["message"]["method"] == "Runtime.evaluate" and sent["message"]["params"]["returnByValue"] is True
            await ws.send(json.dumps({"type": "message", "message": {"id": sent["message"]["id"], "result": results.pop(0)}}))
            if not results:
                return


def _value(value):
    return {"result": {"type": "object", "value": value}, "wasThrown": False}


def test_patched_front_end_bridge_runs_operations_and_collects_console():
    async def scenario():
        inspector = html_agent.HtmlInspector()
        port = await inspector.start()
        token, connected = inspector.expect(hwnd=7, path=webkit_patch.INSPECTOR_PATH)
        front = asyncio.create_task(_fake_front_end(port, token, [
            _value({"ok": True, "result": "hello"}),
            {"result": {"type": "object", "description": "TypeError: boom"}, "wasThrown": True},
            _value({"ok": True, "result": {"scripts": 1, "checked": 1, "errors": []}}),
        ]))
        try:
            page = await asyncio.wait_for(connected, 5)
            assert page.front_end and page.hwnd == 7
            text = await inspector.run(page, "text", {}, timeout=5)
            with pytest.raises(html_agent.HtmlAgentError, match="TypeError: boom"):
                await inspector.run(page, "click", {"selector": "#x"}, timeout=5)

            async def no_new_pages():
                return []

            inspector.open_inspectors = no_new_pages
            console = await inspector.console("")
            await front
            return text, console
        finally:
            await inspector.stop()

    text, console = asyncio.run(scenario())
    assert text == "hello"
    assert console["collected_since"] == "page load"
    assert [m["text"] for m in console["messages"]] == ["SyntaxError: Unexpected token '.'"]


def test_a_page_token_does_not_open_the_front_end_path():
    async def scenario():
        inspector = html_agent.HtmlInspector()
        port = await inspector.start()
        token, _ = inspector.expect()  # a console-loader token
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}{webkit_patch.INSPECTOR_PATH}{token}") as ws:
                with pytest.raises(websockets.ConnectionClosed):
                    await ws.recv()
            return token in inspector._tokens
        finally:
            await inspector.stop()

    assert asyncio.run(scenario()) is True
