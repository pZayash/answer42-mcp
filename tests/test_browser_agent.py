import asyncio
import json

import pytest
import websockets

from mcp_1c import browser_agent, html_agent


def test_client_mode_defaults_to_thin_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("ONEC_MCP_CLIENT_MODE", raising=False)
    assert browser_agent.resolve_client_mode("") == "thin"
    assert browser_agent.resolve_client_mode("Browser") == "browser"
    monkeypatch.setenv("ONEC_MCP_CLIENT_MODE", "browser")
    assert browser_agent.resolve_client_mode("") == "browser"
    with pytest.raises(html_agent.HtmlAgentError):
        browser_agent.resolve_client_mode("firefox")


def test_client_id_follows_1c_name_rules():
    for _ in range(50):
        client_id = browser_agent.new_client_id()
        assert client_id[0].isalpha() and client_id.isalnum()


def test_browser_client_url_adds_test_client_switches():
    url = browser_agent.browser_client_url("https://host/base/ru_RU/?a=1", "A42x")
    assert url == "https://host/base/ru_RU/?a=1&TESTCLIENT&TESTCLIENTID=A42x"
    assert browser_agent.browser_client_url("http://h:8080/b", "A42x").endswith("/b?TESTCLIENT&TESTCLIENTID=A42x")
    with pytest.raises(html_agent.HtmlAgentError):
        browser_agent.browser_client_url("/tmp/file-base", "A42x")


def test_manager_endpoint_uses_default_ports():
    assert browser_agent.manager_endpoint("https://host/base") == ("host", 443)
    assert browser_agent.manager_endpoint("http://host/base") == ("host", 80)
    assert browser_agent.manager_endpoint("http://127.0.0.1:8324") == ("127.0.0.1", 8324)


def test_find_browser_rejects_missing_explicit_path(tmp_path):
    with pytest.raises(html_agent.HtmlAgentError):
        browser_agent.find_browser(str(tmp_path / "nope.exe"))


def test_browser_argv_has_cdp_port_and_url(tmp_path):
    argv = browser_agent.browser_argv("chrome", "http://x/", 9333, tmp_path, headless=True)
    assert "--remote-debugging-port=9333" in argv and "--headless=new" in argv and argv[-1] == "http://x/"


def test_console_entries_from_cdp_events():
    entry = browser_agent._console_entry("Runtime.consoleAPICalled", {"type": "warning", "args": [{"value": "hi"}]})
    assert entry["level"] == "warning" and entry["text"] == "hi"
    error = browser_agent._console_entry("Runtime.exceptionThrown", {"exceptionDetails": {"text": "boom", "lineNumber": 3}})
    assert error["level"] == "error" and error["line"] == 3
    assert browser_agent._console_entry("Other.event", {}) is None


def test_bridge_runs_page_operations_over_cdp():
    async def scenario():
        seen = []

        async def handler(connection):
            async for raw in connection:
                message = json.loads(raw)
                seen.append(message["method"])
                value = {"ok": True, "result": {"count": 1}} if message["method"] == "Runtime.evaluate" else {}
                await connection.send(json.dumps({"id": message["id"], "result": {"result": {"value": value}}}))

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            bridge = browser_agent.BrowserBridge.__new__(browser_agent.BrowserBridge)
            browser_agent.BrowserBridge.__init__(bridge, "chrome") if False else None
            import itertools

            bridge._ids = itertools.count(1)
            bridge._pending = {}
            bridge.MAX_LOG = 500
            page = browser_agent.BrowserPage("browser1", "t1", f"ws://127.0.0.1:{port}")
            page.connection = await websockets.connect(page.ws_url)
            page.reader = asyncio.create_task(bridge._read_loop(page))
            result = await bridge.run(page, "query", {"selector": "a"})
            page.reader.cancel()
            await page.connection.close()
            return result, seen

    result, seen = asyncio.run(scenario())
    assert result == {"count": 1} and seen == ["Runtime.evaluate"]
