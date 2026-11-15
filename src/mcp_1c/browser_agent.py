"""Test client in a browser: launch the 1C web client in Chrome/Edge and drive its page like a site.

``start_session(client_mode="browser")`` opens the publication URL with the web client's own
test-client switches (``?TESTCLIENT&TESTCLIENTID=<id>``): the web client then answers the test
manager under that identifier, so every ``form_*``/``table_*``/``click_button`` tool works as with
the thin client.  The browser is started with a DevTools (CDP) port, which gives the ``html_*``
tools the whole page of the 1C web client: DOM queries, text, clicks, typing, scripts, console.

``BrowserBridge`` has the interface of :class:`html_agent.HtmlInspector` (pages, ``command``,
``console`` ...), so the same tools serve HTML document fields and the browser.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import websockets

from . import os_support
from .html_agent import HtmlAgentError, free_local_port, ops_expression

LOGGER = logging.getLogger(__name__)

CLIENT_MODES = ("thin", "browser")
BROWSER_START_SECONDS = 30.0
_BROWSER_ENV = "ONEC_MCP_BROWSER_PATH"
_ID_RE = re.compile(r"[^A-Za-z0-9_-]")

# A web client rendered inside the page is "ready" when its interface exists.
_READY_JS = (
    r"(function(){var b=document.body;return !!(b&&b.innerText&&b.innerText.replace(/\s+/g,'').length>0&&"
    "document.readyState!=='loading');})()"
)


_LOGIN_FORM_GONE_JS = "!document.querySelector('input[type=password]')"
_LOGIN_FOCUS_JS = r"""function (which) {
  function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
  function shown(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector)).filter(visible)[0];
  }
  var password = shown('input[type=password]');
  if (!password) { return { form: false }; }
  var el = password;
  if (which === 'user') {
    el = shown('input:not([type=password]):not([type=hidden]):not([type=checkbox]):not([type=radio])'
               + ':not([type=submit]):not([type=button])');
    if (!el) { return { form: true, field: false }; }
  }
  el.focus();
  if (el.select) { el.select(); }
  return { form: true, field: true };
}"""


def resolve_client_mode(mode: str = "") -> str:
    """thin (the default, or ONEC_MCP_CLIENT_MODE) or browser."""
    mode = (mode or os.getenv("ONEC_MCP_CLIENT_MODE", "thin")).strip().lower() or "thin"
    if mode not in CLIENT_MODES:
        raise HtmlAgentError(f"Unknown client_mode {mode!r}: use {', '.join(CLIENT_MODES)}")
    return mode


def _browser_candidates() -> list[str]:
    if os_support.IS_WINDOWS:
        roots = [os.environ.get(name, "") for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        suffixes = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chromium\Application\chrome.exe",
        ]
        return [str(Path(root) / suffix) for suffix in suffixes for root in roots if root]
    names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "microsoft-edge-stable")
    return [found for found in (shutil.which(name) for name in names) if found]


def find_browser(explicit: str = "") -> str:
    """Chrome/Edge executable: the explicit path, ONEC_MCP_BROWSER_PATH, then the installed ones."""
    chosen = explicit or os.getenv(_BROWSER_ENV, "")
    if chosen:
        if shutil.which(chosen) or Path(chosen).exists():
            return shutil.which(chosen) or chosen
        raise HtmlAgentError(f"Browser not found: {chosen}")
    for candidate in _browser_candidates():
        if Path(candidate).exists():
            return candidate
    raise HtmlAgentError(
        f"No Chrome or Edge found: install one or set {_BROWSER_ENV} (or start_session's browser_path)"
    )


def new_client_id() -> str:
    """A test-client identifier; 1C requires it to follow the name rules, so it cannot start with a digit."""
    return "A42" + uuid.uuid4().hex


def browser_client_url(base_url: str, client_id: str) -> str:
    """The publication URL that starts the web client as a test client with this identifier.

    ``TESTCLIENT`` and ``TESTCLIENTID`` are the web client's command-line switches
    (``vrs_test``/``vrs_test_id``); existing query parameters are kept.
    """
    client_id = _ID_RE.sub("", client_id or "")
    if not client_id:
        raise HtmlAgentError("client_id is required")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise HtmlAgentError(f"The browser client needs a web publication URL (http/https), got {base_url!r}")
    query = "&".join(item for item in (parts.query, "TESTCLIENT", f"TESTCLIENTID={client_id}") if item)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def manager_endpoint(base_url: str) -> tuple[str, int]:
    """Host and port of the web server the manager reaches the browser client through."""
    parts = urlsplit(base_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname or "localhost", port


def browser_argv(
    browser: str, url: str, cdp_port: int, profile_dir: Path, *, headless: bool = False, window: str = "1440,900"
) -> list[str]:
    argv = [
        browser,
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--ignore-certificate-errors",
        f"--window-size={window}",
    ]
    if headless:
        argv.append("--headless=new")
    argv.append(url)
    return argv


@dataclass
class BrowserPage:
    page_id: str
    target_id: str
    ws_url: str
    connection: Any = None
    url: str = ""
    title: str = ""
    opened_at: float = field(default_factory=time.time)
    closed: bool = False
    console_log: list[dict[str, Any]] = field(default_factory=list)
    reader: asyncio.Task | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "page": self.page_id,
            "url": self.url,
            "title": self.title,
            "alive": not self.closed,
            "opened_at": _dt.datetime.fromtimestamp(self.opened_at, _dt.UTC).isoformat(timespec="seconds"),
        }


def _console_entry(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """A console/log/exception CDP event as a html_console entry."""
    now = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    if method == "Runtime.consoleAPICalled":
        level = {"warning": "warning", "error": "error", "assert": "error", "info": "info", "debug": "debug"}.get(
            str(params.get("type")), "log"
        )
        parts = []
        for arg in params.get("args") or []:
            value = arg.get("value", arg.get("description", ""))
            parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        frame = ((params.get("stackTrace") or {}).get("callFrames") or [{}])[0]
        return {"level": level, "text": " ".join(parts), "url": frame.get("url", ""), "line": frame.get("lineNumber", 0),
                "column": frame.get("columnNumber", 0), "source": "console-api", "time": now}
    if method == "Runtime.exceptionThrown":
        details = params.get("exceptionDetails") or {}
        exception = details.get("exception") or {}
        return {"level": "error", "text": str(exception.get("description") or details.get("text") or "Script error"),
                "url": details.get("url", ""), "line": details.get("lineNumber", 0),
                "column": details.get("columnNumber", 0), "source": "javascript", "time": now}
    if method == "Log.entryAdded":
        entry = params.get("entry") or {}
        level = {"warning": "warning", "error": "error", "info": "info", "verbose": "debug"}.get(str(entry.get("level")), "log")
        return {"level": level, "text": str(entry.get("text") or ""), "url": entry.get("url", ""),
                "line": entry.get("lineNumber", 0), "column": 0, "source": str(entry.get("source") or "network"), "time": now}
    return None


class BrowserBridge:
    """A launched Chrome/Edge and its pages, with the interface of ``HtmlInspector``."""

    mode = "browser"
    MAX_LOG = 500

    def __init__(self, browser: str = "", headless: bool = False) -> None:
        self.browser = find_browser(browser)
        self.headless = headless
        self.cdp_port = 0
        self.process: subprocess.Popen[Any] | None = None
        self.profile_dir: Path | None = None
        self.pages: dict[str, BrowserPage] = {}
        self.last_error = ""
        self.start_url = ""
        self.client_id = ""
        self.test_traffic: dict[str, dict[str, int]] = {}  # e1cib/test/<kind> -> {http status: count}
        self.field_driver: Any = None
        self.overlay_dir: Path | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._numbers = itertools.count(1)
        self._lock = asyncio.Lock()

    # --- lifecycle ---

    async def launch(self, url: str, profile_dir: Path, client_id: str = "") -> None:
        self.start_url, self.client_id = url, client_id
        self.profile_dir = profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.cdp_port = free_local_port()
        argv = browser_argv(self.browser, url, self.cdp_port, profile_dir, headless=self.headless)
        LOGGER.info("Starting browser client: %s", " ".join(argv))
        self.process = await asyncio.to_thread(
            lambda: subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **os_support.subprocess_creation_kwargs(),
            )
        )
        deadline = time.monotonic() + BROWSER_START_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise HtmlAgentError(
                    f"The browser exited at start (code {self.process.returncode}); another instance with the same "
                    f"profile or a wrong {self.browser}"
                )
            with suppress(Exception):
                if await self._targets():
                    return
            await asyncio.sleep(0.3)
        raise HtmlAgentError(f"The browser did not open its DevTools port {self.cdp_port} in {BROWSER_START_SECONDS:g}s")

    async def start(self) -> int:  # HtmlInspector compatibility: the browser is launched by launch()
        return self.cdp_port

    async def stop(self) -> None:
        for page in self.pages.values():
            page.closed = True
            if page.reader is not None:
                page.reader.cancel()
            if page.connection is not None:
                with suppress(Exception):
                    await page.connection.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HtmlAgentError("Browser stopped"))
        self._pending.clear()
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), 8)
            except TimeoutError:
                process.kill()
        if self.profile_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self.profile_dir, True)

    def client_env(self) -> dict[str, str]:
        return {}

    def attach(self, desktop: str, pid: int, client_path: str = "") -> None:
        return None

    def check_platform(self, client_path: str) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "mode": "browser",
            "browser": self.browser,
            "cdp_port": self.cdp_port,
            "browser_pid": self.process.pid if self.process else 0,
            "alive": bool(self.process and self.process.poll() is None),
            "client_id": self.client_id,
            "test_traffic": self.test_traffic,
            "url": self.start_url,
            "pages": len([p for p in self.pages.values() if not p.closed]),
        }

    # --- CDP ---

    async def _targets(self) -> list[dict[str, Any]]:
        def fetch() -> list[dict[str, Any]]:
            reply = requests.get(f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=3)
            reply.raise_for_status()
            return [t for t in reply.json() if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]

        return await asyncio.to_thread(fetch)

    async def _read_loop(self, page: BrowserPage) -> None:
        try:
            async for raw in page.connection:
                message = json.loads(raw)
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if message.get("method") == "Network.responseReceived":
                    self._note_test_request(message.get("params") or {})
                    continue
                entry = _console_entry(str(message.get("method") or ""), message.get("params") or {})
                if entry is not None:
                    page.console_log.append(entry)
                    del page.console_log[: -self.MAX_LOG]
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            page.closed = True

    def _note_test_request(self, params: dict[str, Any]) -> None:
        """Remember the web client's own test-client traffic (e1cib/test/*): it shows whether it registered."""
        response = params.get("response") or {}
        url = str(response.get("url") or "")
        if "e1cib/test/" not in url:
            return
        kind = url.split("e1cib/test/", 1)[1].split("?", 1)[0]
        counter = self.test_traffic.setdefault(kind, {})
        status = str(response.get("status"))
        counter[status] = counter.get(status, 0) + 1

    async def send(self, page: BrowserPage, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if page.closed or page.connection is None:
            raise HtmlAgentError(f"Page {page.page_id} is closed")
        message_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await page.connection.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise HtmlAgentError(f"No reply from page {page.page_id} within {timeout:g}s") from None
        except websockets.ConnectionClosed:
            page.closed = True
            raise HtmlAgentError(f"Page {page.page_id} disconnected") from None
        finally:
            self._pending.pop(message_id, None)
        if "error" in reply:
            error = reply["error"]
            raise HtmlAgentError(f"Browser error: {error.get('message') if isinstance(error, dict) else error}")
        return reply.get("result") or {}

    async def open_inspectors(self) -> list[str]:
        """Connect the browser's pages that have no live connection yet; return new page ids."""
        async with self._lock:
            if self.process is None or self.process.poll() is not None:
                raise HtmlAgentError("The browser was closed")
            targets = await self._targets()
            known = {p.target_id: p for p in self.pages.values()}
            alive_ids = {t["id"] for t in targets}
            for page in self.pages.values():
                if page.target_id not in alive_ids:
                    page.closed = True
            fresh: list[str] = []
            for target in targets:
                existing = known.get(target["id"])
                if existing is not None and not existing.closed:
                    continue
                page = BrowserPage(f"browser{next(self._numbers)}", target["id"], target["webSocketDebuggerUrl"],
                                   url=str(target.get("url") or ""), title=str(target.get("title") or ""))
                try:
                    page.connection = await websockets.connect(page.ws_url, max_size=None, ping_interval=None)
                except Exception as exc:
                    self.last_error = f"cannot connect to {target.get('url')}: {exc}"
                    continue
                page.reader = asyncio.create_task(self._read_loop(page))
                for domain in ("Runtime.enable", "Log.enable", "Network.enable"):
                    with suppress(HtmlAgentError):
                        await self.send(page, domain, timeout=10)
                # A page that thinks it is in the background (no focus, hidden window) is throttled, and the web
                # client then answers the test manager slowly or not at all.
                with suppress(HtmlAgentError):
                    await self.send(page, "Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=10)
                    await self.send(page, "Page.bringToFront", timeout=10)
                self.pages[page.page_id] = page
                fresh.append(page.page_id)
            return fresh

    # --- the HtmlInspector interface ---

    async def evaluate(self, page: BrowserPage, expression: str, timeout: float = 30.0) -> Any:
        result = await self.send(
            page, "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True}, timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            exception = details.get("exception") or {}
            raise HtmlAgentError(str(exception.get("description") or details.get("text") or "page script failed"))
        return (result.get("result") or {}).get("value")

    async def run(self, page: BrowserPage, op: str, args: dict[str, Any], timeout: float = 30.0) -> Any:
        outcome = await self.evaluate(page, ops_expression(op, args), timeout=timeout) or {}
        if not outcome.get("ok"):
            raise HtmlAgentError(str(outcome.get("error") or "page operation failed"))
        return outcome.get("result")

    async def refresh(self, page: BrowserPage) -> None:
        info = await self.run(page, "info", {}, timeout=10)
        page.url, page.title = str(info.get("url") or ""), str(info.get("title") or "")

    async def list_pages(self, include_dead: bool = False) -> list[dict[str, Any]]:
        for page in self.pages.values():
            if not page.closed:
                with suppress(HtmlAgentError):
                    await self.refresh(page)
        return [p.to_json() for p in self.pages.values() if include_dead or not p.closed]

    def resolve_page(self, selector: str = "") -> BrowserPage:
        alive = [p for p in self.pages.values() if not p.closed]
        if selector:
            if selector in self.pages:
                return self.pages[selector]
            found = [p for p in alive if selector.lower() in p.url.lower() or selector.lower() in p.title.lower()]
            if len(found) == 1:
                return found[0]
            raise HtmlAgentError(
                f"{'Several' if found else 'No'} live browser pages match {selector!r}; pages: "
                + json.dumps([p.to_json() for p in alive], ensure_ascii=False)
            )
        if len(alive) == 1:
            return alive[0]
        if not alive:
            raise HtmlAgentError("No live browser page: the browser was closed" + (f" ({self.last_error})" if self.last_error else ""))
        raise HtmlAgentError("Several browser pages; pass page: " + json.dumps([p.to_json() for p in alive], ensure_ascii=False))

    async def page(self, selector: str = "") -> BrowserPage:
        await self.open_inspectors()
        return self.resolve_page(selector)

    async def command(self, selector: str, op: str, args: dict[str, Any], timeout: float = 30.0) -> Any:
        return await self.run(await self.page(selector), op, args, timeout=timeout)

    async def console(self, selector: str = "", levels: frozenset[str] = frozenset({"error", "warning"}),
                      limit: int = 100, clear: bool = False, check_scripts: bool = True) -> dict[str, Any]:
        page = await self.page(selector)
        messages = list(page.console_log)
        if clear:
            page.console_log.clear()
        selected = [m for m in messages if not levels or m.get("level") in levels]
        result: dict[str, Any] = {"page": page.page_id, "url": page.url, "collected_since": "page connection"}
        result["messages"] = selected[-limit:] if limit > 0 else selected
        result["total"] = len(selected)
        if check_scripts:
            result["scripts"] = await self.run(page, "check_scripts", {}, timeout=60)
        return result

    # --- browser-only operations ---

    async def screenshot(self, selector: str = "", full_page: bool = False) -> bytes:
        page = await self.page(selector)
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": bool(full_page)}
        result = await self.send(page, "Page.captureScreenshot", params, timeout=30)
        return base64.b64decode(result.get("data") or "")

    async def press_key(self, key: str, selector: str = "", modifiers: int = 0) -> dict[str, Any]:
        """A real key press (keyDown, keyUp) in the focused element of the page."""
        page = await self.page(selector)
        known = {
            "Enter": (13, "\r"), "Tab": (9, ""), "Escape": (27, ""), "Backspace": (8, ""), "Delete": (46, ""),
            "ArrowUp": (38, ""), "ArrowDown": (40, ""), "ArrowLeft": (37, ""), "ArrowRight": (39, ""),
            "Home": (36, ""), "End": (35, ""), "PageUp": (33, ""), "PageDown": (34, ""),
            "F1": (112, ""), "F2": (113, ""), "F4": (115, ""), "F5": (116, ""), "F8": (119, ""),
        }
        code, text = known.get(key, (ord(key.upper()) if len(key) == 1 else 0, key if len(key) == 1 else ""))
        base: dict[str, Any] = {"key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
                                "modifiers": int(modifiers)}
        await self.send(page, "Input.dispatchKeyEvent", {**base, "type": "keyDown", **({"text": text} if text else {})})
        await self.send(page, "Input.dispatchKeyEvent", {**base, "type": "keyUp"})
        return {"page": page.page_id, "key": key, "modifiers": int(modifiers)}

    async def navigate(self, url: str, selector: str = "") -> dict[str, Any]:
        page = await self.page(selector)
        result = await self.send(page, "Page.navigate", {"url": url}, timeout=60)
        if result.get("errorText"):
            raise HtmlAgentError(f"Navigation failed: {result['errorText']}")
        return {"page": page.page_id, "url": url}

    async def login(self, username: str, password: str, timeout: float = 120.0) -> dict[str, Any]:
        """Type the credentials into the web client's login form when it is shown (real key input, as a user would).

        A no-op when the page has no form (no users, SSO).  An empty password is allowed.
        """
        page = await self.page()
        deadline = time.monotonic() + 15
        form: dict[str, Any] = {}
        while time.monotonic() < deadline:  # the form is drawn a moment after the splash screen
            form = await self.evaluate(page, f"({_LOGIN_FOCUS_JS})('user')", timeout=15) or {}
            if form.get("form"):
                break
            if len(self.pages) and not await self.evaluate(page, _LOGIN_FORM_GONE_JS, timeout=10):
                break
            await asyncio.sleep(0.5)
            if time.monotonic() > deadline - 12 and not form.get("form"):
                return {"login_form": False}
        if not form.get("form"):
            return {"login_form": False}
        if form.get("field") and username:
            await self.send(page, "Input.insertText", {"text": username})
        await self.evaluate(page, f"({_LOGIN_FOCUS_JS})('password')", timeout=15)
        if password:
            await self.send(page, "Input.insertText", {"text": password})
        await self.press_key("Enter")
        end = time.monotonic() + timeout
        clicked = False
        started = time.monotonic()
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            if not clicked and time.monotonic() - started > 3:
                clicked = True  # Enter was not enough (the button enables a moment after the input): press it
                with suppress(HtmlAgentError):
                    await self.run(await self.page(), "click", {"selector": "#authWindow_basic_okButton"}, timeout=10)
            with suppress(HtmlAgentError):
                if await self.evaluate(await self.page(), _LOGIN_FORM_GONE_JS, timeout=10):
                    return {"login_form": True, "logged_in": True}
        raise HtmlAgentError(
            "The 1C web client still shows its login form: check the user name and password"
        )

    async def wait_ready(self, timeout: float = 60.0) -> dict[str, Any]:
        """Wait until the first page shows content; the 1C login form or the interface."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                page = await self.page()
                if await self.evaluate(page, _READY_JS, timeout=10):
                    await self.refresh(page)
                    return page.to_json()
            except HtmlAgentError as exc:
                last = str(exc)
            await asyncio.sleep(0.5)
        raise HtmlAgentError(f"The browser page did not load within {timeout:g}s{': ' + last if last else ''}")
