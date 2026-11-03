"""Live access to web pages in HTML document fields through the embedded Web Inspector.

The test-client API only exposes the HTML a field had when it finished loading,
so pages that render themselves with JavaScript (chats, SPAs) cannot be read or
driven through it.  The Windows thin client renders HTML fields with an
embedded WebKit whose Web Inspector opens on Ctrl+Alt+Shift+F12 (developer
extras are on), but it has no remote-inspector server.  So, for each WebKit
view of the client (one per HTML field), Answer42:

* sends the inspector hot key; 1C docks the inspector front end into the form
  as one more WebKit view;
* focuses the front end's console prompt (a click, then Ctrl+`) and types ``loader_js``: a one-line
  bridge that the console runs in the inspected page; it connects to this
  session's local WebSocket with a one-time token and evaluates the
  expressions Answer42 sends;
* runs page operations as ``OPS_JS``, a self-contained ES5 function (text,
  query, click, type, eval) evaluated in the live page.

The platform is used as installed.  The bridge lives in the page, so a reload
or navigation drops it; the next html_* call types it into the console again.

The Linux thin client renders HTML fields with the system WebKitGTK, which
serves a remote inspector when started with ``WEBKIT_INSPECTOR_SERVER``.  There
the test client gets that variable (``client_env``) and Answer42 talks to the
server as a front end (``RemoteInspectorClient``): no inspector UI, no typing.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import itertools
import json
import logging
import secrets
import socket
import struct
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import websockets

from . import os_support, win_desktop

LOGGER = logging.getLogger(__name__)

WEBVIEW_CLASS = "WebViewWindowClass"
INSPECTOR_WINDOW_CLASS = "WebInspectorWindowClass"
INSPECTOR_OPEN_SECONDS = 4.0
CONNECT_TIMEOUT_SECONDS = 20.0
RETYPE_SECONDS = 5.0

LOADER_JS = (
    "(function(){var w=new WebSocket('ws://127.0.0.1:__PORT__/answer42-page');"
    "w.onopen=function(){w.send(JSON.stringify({type:'hello',token:'__TOKEN__'}))};"
    "w.onmessage=function(e){var m=JSON.parse(e.data),r;"
    "try{r={type:'reply',id:m.id,value:(0,eval)(m.expression)}}"
    "catch(x){r={type:'reply',id:m.id,error:String(x&&x.message||x)}}"
    "w.send(JSON.stringify(r))}})()"
)


def loader_js(port: int, token: str) -> str:
    """The one-line page bridge typed into the inspector console (ASCII only, no line breaks)."""
    return LOADER_JS.replace("__PORT__", str(int(port))).replace("__TOKEN__", token)

OPS_JS = r"""function (op, args) {
  function trim(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').replace(/^ | $/g, ''); }
  function visible(el) { return !!(el.offsetWidth || el.offsetHeight || (el.getClientRects && el.getClientRects().length)); }
  function textOf(el) { return el.innerText != null ? el.innerText : el.textContent; }
  function describe(el, index) {
    var d = { index: index, tag: el.tagName ? el.tagName.toLowerCase() : '', text: trim(textOf(el)).slice(0, 300) };
    if (el.id) { d.id = el.id; }
    if (el.className && typeof el.className === 'string') { d['class'] = el.className; }
    if ('value' in el && el.tagName !== 'BUTTON' && el.tagName !== 'LI') { d.value = String(el.value); }
    var attrs = ['name', 'type', 'href', 'placeholder', 'role', 'aria-label', 'title'];
    for (var i = 0; i < attrs.length; i++) { var v = el.getAttribute && el.getAttribute(attrs[i]); if (v) { d[attrs[i]] = v; } }
    if (el.disabled) { d.disabled = true; }
    d.visible = visible(el);
    return d;
  }
  var CLICKABLE = 'a,button,input[type=button],input[type=submit],[role=button],[onclick]';
  function matches(a) {
    var list = document.querySelectorAll(a.selector || (a.text ? CLICKABLE : 'body'));
    var out = [];
    for (var i = 0; i < list.length; i++) {
      if (a.text) {
        var t = trim(textOf(list[i]) || list[i].value || list[i].getAttribute('aria-label') || '');
        if (a.exact ? t !== a.text : t.indexOf(a.text) < 0) { continue; }
      }
      out.push(list[i]);
    }
    return out;
  }
  function one(a) {
    var list = matches(a);
    var index = a.index || 0;
    if (!list.length || index >= list.length) {
      throw new Error('element not found: ' + (a.selector || '') + (a.text ? ' text=' + a.text : '') + ' index=' + index + ' (matches: ' + list.length + ')');
    }
    return list[index];
  }
  function fire(el, type) {
    var e = document.createEvent('Event');
    e.initEvent(type, true, true);
    return el.dispatchEvent(e);
  }
  function mouse(el, type) {
    var e = document.createEvent('MouseEvents');
    e.initMouseEvent(type, true, true, window, 1, 0, 0, 0, 0, false, false, false, false, 0, null);
    el.dispatchEvent(e);
  }
  function key(el, type) {
    var e;
    try { e = new KeyboardEvent(type, { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }); }
    catch (ignore) { e = document.createEvent('Event'); e.initEvent(type, true, true); }
    try { Object.defineProperty(e, 'keyCode', { get: function () { return 13; } }); Object.defineProperty(e, 'which', { get: function () { return 13; } }); } catch (ignore2) {}
    return el.dispatchEvent(e);
  }
  function setValue(el, value) {
    if (el.isContentEditable) { el.textContent = value; return; }
    var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
  }
  function plain(v, depth) {
    if (v == null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') { return v; }
    if (v.nodeType === 1) { return describe(v, 0); }
    if (v.nodeType) { return trim(v.textContent); }
    if (depth > 3) { return String(v); }
    if (typeof v.length === 'number' && typeof v !== 'function') {
      var a = [];
      for (var i = 0; i < v.length && i < 500; i++) { a.push(plain(v[i], depth + 1)); }
      return a;
    }
    try { return JSON.parse(JSON.stringify(v)); } catch (e) { return String(v); }
  }
  var OPS = {
    info: function () {
      return { url: String(location.href).split('#')[0], title: document.title, ready_state: document.readyState,
               text_length: document.body ? textOf(document.body).length : 0 };
    },
    text: function (a) {
      var list = matches(a), out = [];
      for (var i = 0; i < list.length; i++) { out.push(textOf(list[i])); }
      return a.all ? out : (out.length ? out[a.index || 0] : null);
    },
    html: function (a) { var el = one(a); return a.inner ? el.innerHTML : el.outerHTML; },
    query: function (a) {
      var list = matches(a), out = [], limit = a.limit || 50;
      for (var i = 0; i < list.length && out.length < limit; i++) {
        if (a.visible_only && !visible(list[i])) { continue; }
        out.push(describe(list[i], i));
      }
      return { count: list.length, items: out };
    },
    click: function (a) {
      var el = one(a);
      if (el.scrollIntoView) { el.scrollIntoView(false); }
      if (el.focus) { el.focus(); }
      mouse(el, 'mousedown'); mouse(el, 'mouseup');
      el.click();
      return describe(el, a.index || 0);
    },
    type: function (a) {
      var el = one(a);
      if (el.focus) { el.focus(); }
      var value = a.append ? String(el.value || el.textContent || '') + a.value : a.value;
      setValue(el, value);
      fire(el, 'input');
      fire(el, 'change');
      if (a.submit) {
        var go = key(el, 'keydown');
        key(el, 'keypress');
        key(el, 'keyup');
        if (go && el.form) { if (fire(el.form, 'submit')) { el.form.submit(); } }
      }
      return describe(el, a.index || 0);
    },
    eval: function (a) { return (0, eval)(a.code); },
    hook_console: function () {
      if (window.__answer42Console) { return false; }
      var log = window.__answer42Console = [];
      function push(level, text, url, line, column, source) {
        log.push({ level: level, text: String(text), url: url || '', line: line || 0, column: column || 0, source: source, time: new Date().toISOString() });
        if (log.length > 500) { log.shift(); }
      }
      window.addEventListener('error', function (e) {
        var el = e.target;
        if (el && el !== window && el.tagName) {
          push('error', 'Failed to load ' + el.tagName.toLowerCase() + ': ' + (el.src || el.href || ''), el.src || el.href, 0, 0, 'network');
          return;
        }
        push('error', e.message || (e.error && e.error.message) || 'Script error', e.filename, e.lineno, e.colno, 'javascript');
      }, true);
      window.addEventListener('unhandledrejection', function (e) {
        var r = e.reason;
        push('error', 'Unhandled promise rejection: ' + ((r && r.message) || r), '', 0, 0, 'javascript');
      });
      ['error', 'warn'].forEach(function (name) {
        var original = console[name];
        console[name] = function () {
          try {
            var parts = [];
            for (var i = 0; i < arguments.length; i++) {
              var x = arguments[i];
              try { parts.push(typeof x === 'string' ? x : (x && x.message) || JSON.stringify(x)); } catch (ignore) { parts.push(String(x)); }
            }
            push(name === 'warn' ? 'warning' : 'error', parts.join(' '), '', 0, 0, 'console-api');
          } catch (ignore2) {}
          return original.apply(console, arguments);
        };
      });
      return true;
    },
    console: function (a) {
      var log = window.__answer42Console;
      if (!log) { return { hooked: false, messages: [] }; }
      var out = log.slice(-(a.limit || 200));
      if (a.clear) { log.length = 0; }
      return { hooked: true, messages: out };
    },
    check_scripts: function () {
      var list = document.scripts, errors = [], checked = 0;
      for (var i = 0; i < list.length; i++) {
        var s = list[i], type = (s.type || '').toLowerCase();
        if (type && !/^(text|application)\/(x-)?(javascript|ecmascript)$/.test(type)) { continue; }
        var name = s.src || ('inline script #' + i), code = s.text;
        if (s.src) {
          try {
            var x = new XMLHttpRequest();
            x.open('GET', s.src, false);
            x.send(null);
            if (x.status && (x.status < 200 || x.status >= 300)) { errors.push({ script: name, error: 'HTTP ' + x.status }); continue; }
            code = x.responseText;
          } catch (e) { errors.push({ script: name, error: 'cannot fetch: ' + ((e && e.message) || e) }); continue; }
        }
        checked++;
        try { new Function(code); }
        catch (e) {
          var d = { script: name, error: (e && e.name ? e.name + ': ' : '') + ((e && e.message) || e) };
          if (e && e.line) { d.line = e.line; }
          if (!(e instanceof SyntaxError)) { d.unchecked = true; }
          errors.push(d);
        }
      }
      return { scripts: list.length, checked: checked, errors: errors };
    }
  };
  try { return { ok: true, result: plain(OPS[op](args || {}), 0) }; }
  catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
}"""


class HtmlAgentError(RuntimeError):
    pass


def ops_expression(op: str, args: dict[str, Any]) -> str:
    """Build the Runtime.evaluate expression that runs one page operation."""
    return f"({OPS_JS})({json.dumps(op)}, {json.dumps(args, ensure_ascii=False)})"


# --- Windows: find the client's WebKit views, open their inspectors, type into the console ---

_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_F12, _VK_RETURN, _VK_OEM_3, _VK_A, _VK_HOME = 0x10, 0x11, 0x12, 0x7B, 0x0D, 0xC0, 0x41, 0x24
_VK_LSHIFT, _VK_LCONTROL, _VK_LMENU = 0xA0, 0xA2, 0xA4
_WM_KEYDOWN, _WM_KEYUP, _WM_CHAR, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x100, 0x101, 0x102, 0x104, 0x105
_WM_MOUSEMOVE, _WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON = 0x200, 0x201, 0x202, 0x1


def _user32() -> Any:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    return user32


def find_webviews(desktop: str, pid: int) -> list[int]:
    """Visible WebKit view windows of process ``pid`` (on a hidden desktop when one is named)."""
    import ctypes
    import ctypes.wintypes as wt

    user32 = _user32()
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    found: list[int] = []

    def class_name(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        return buf.value

    def child(hwnd: int, _lparam: int) -> bool:
        if class_name(hwnd) == WEBVIEW_CLASS and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    def top(hwnd: int, _lparam: int) -> bool:
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        # An undocked inspector window holds a WebKit view of the same process: never inspect it.
        if owner.value == pid and class_name(hwnd) != INSPECTOR_WINDOW_CLASS:
            user32.EnumChildWindows(hwnd, proc(child), 0)
        return True

    if win_desktop.is_hidden_desktop_name(desktop):
        handle = win_desktop._open_desktop(desktop)
        try:
            user32.EnumDesktopWindows(handle, proc(top), 0)
        finally:
            user32.CloseDesktop(handle)
    else:
        user32.EnumWindows(proc(top), 0)
    return found


def _post_shortcut(hwnd: int, vk: int, modifiers: tuple[int, ...], system: bool = False, hold: float = 0.3) -> None:
    """A key with modifiers to a window of another thread, without the real keyboard.

    The modifiers are put into the keyboard state shared with the window's
    thread (AttachThreadInput + SetKeyboardState); the key is a posted key message.
    This works on a hidden desktop, where SendInput cannot reach.
    """
    import ctypes

    user32 = _user32()
    kernel32 = ctypes.WinDLL("kernel32")
    target = user32.GetWindowThreadProcessId(hwnd, None)
    me = kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(me, target, True))
    previous = (ctypes.c_ubyte * 256)()
    user32.GetKeyboardState(previous)
    state = (ctypes.c_ubyte * 256)(*previous)
    for modifier in modifiers:
        state[modifier] = 0x80
    down, up = (_WM_SYSKEYDOWN, _WM_SYSKEYUP) if system else (_WM_KEYDOWN, _WM_KEYUP)
    flags = 1 | (1 << 29 if system else 0)  # bit 29: Alt is down
    try:
        user32.SetKeyboardState(state)
        user32.PostMessageW(hwnd, down, vk, flags)
        time.sleep(hold)  # the key is handled asynchronously while the modifiers must still be down
        user32.PostMessageW(hwnd, up, vk, flags | (3 << 30))
        time.sleep(0.3)
    finally:
        user32.SetKeyboardState(previous)
        if attached:
            user32.AttachThreadInput(me, target, False)


def _press_inspector_hotkey(hwnd: int) -> None:
    """Ctrl+Alt+Shift+F12: open the Web Inspector of a WebKit view."""
    _post_shortcut(hwnd, _VK_F12, (_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LSHIFT, _VK_LCONTROL, _VK_LMENU), system=True, hold=1.0)


def _type_into_console(desktop: str, front_end: int, text: str) -> None:
    """Run ``text`` in the console of an inspector front end.

    A click in the middle of the front end gives it the focus that its key
    shortcuts need; Ctrl+` (the "focus console prompt" shortcut, matched by key
    code so the keyboard layout does not matter) then focuses the quick console
    prompt wherever it is, and Ctrl+A selects what earlier attempts left there.
    The text goes in as WM_CHAR and Enter runs it.  The prompt takes the focus
    asynchronously and may put the first character after the second, so the
    text starts with a space that absorbs this.  Esc must not be used: 1C
    handles it first and closes the form.
    """
    import ctypes
    import ctypes.wintypes as wt

    user32 = _user32()
    rect = wt.RECT()
    user32.GetClientRect(wt.HWND(front_end), ctypes.byref(rect))
    middle = ((rect.bottom // 2) << 16) | (rect.right // 2)
    user32.PostMessageW(front_end, _WM_MOUSEMOVE, 0, middle)
    user32.PostMessageW(front_end, _WM_LBUTTONDOWN, _MK_LBUTTON, middle)
    user32.PostMessageW(front_end, _WM_LBUTTONUP, 0, middle)
    time.sleep(0.5)
    ctrl = (_VK_CONTROL, _VK_LCONTROL)
    _run_on_client_desktop(desktop, lambda: _post_shortcut(front_end, _VK_OEM_3, ctrl))
    time.sleep(1.0)
    _run_on_client_desktop(desktop, lambda: _post_shortcut(front_end, _VK_A, ctrl))
    for char in " " + text:
        user32.PostMessageW(front_end, _WM_CHAR, ord(char), 1)
    # The prompt reads typed text asynchronously.  With the cursor at the end, Enter first
    # asks the page whether the code is complete and may insert a new line instead; with
    # the cursor elsewhere (Home) it runs the code at once.
    time.sleep(1.0)
    user32.PostMessageW(front_end, _WM_KEYDOWN, _VK_HOME, 1)
    user32.PostMessageW(front_end, _WM_KEYUP, _VK_HOME, 1 | (3 << 30))
    time.sleep(0.2)
    user32.PostMessageW(front_end, _WM_KEYDOWN, _VK_RETURN, 1)
    user32.PostMessageW(front_end, _WM_CHAR, 13, 1)
    user32.PostMessageW(front_end, _WM_KEYUP, _VK_RETURN, 1 | (3 << 30))


def _run_on_client_desktop(desktop: str, fn: Any) -> Any:
    if win_desktop.is_hidden_desktop_name(desktop):
        return win_desktop.run_on_desktop(desktop, fn)
    return fn()


# --- Linux: the WebKitGTK remote inspector server of the client (WEBKIT_INSPECTOR_SERVER) ---
#
# Messages are framed as: body size (uint32, big endian), flags (uint8; 1 =
# little-endian GVariant), body = NUL-terminated name + GVariant parameters.
# A malformed message can bring the client down, so only the shapes below are sent.

_REMOTE_LITTLE_ENDIAN = 1
REMOTE_SETUP_SECONDS = 10.0


def _gv_offset_size(size: int) -> int:
    return 1 if size <= 0xFF else 2 if size <= 0xFFFF else 4 if size <= 0xFFFFFFFF else 8


def _gv_offset(data: bytes, position: int, width: int) -> int:
    return int.from_bytes(data[position:position + width], "little")


def _gv_string(data: bytes) -> str:
    return data.rstrip(b"\0").decode("utf-8", "replace")


def _gv_target(data: bytes) -> tuple[int, str, str, str]:
    """One (tsssb) target: id, type, name, url."""
    width = _gv_offset_size(len(data))
    ends = [_gv_offset(data, len(data) - width * (i + 1), width) for i in range(3)]
    return (
        struct.unpack_from("<Q", data, 0)[0],
        _gv_string(data[8:ends[0]]),
        _gv_string(data[ends[0]:ends[1]]),
        _gv_string(data[ends[1]:ends[2]]),
    )


def decode_target_list(body: bytes) -> tuple[int, list[tuple[int, str, str, str]]]:
    """SetTargetList parameters (ta(tsssb)): connection id and its targets."""
    connection_id = struct.unpack_from("<Q", body, 0)[0]
    array = body[8:]
    if not array:
        return connection_id, []
    width = _gv_offset_size(len(array))
    table = _gv_offset(array, len(array) - width, width)
    targets, start = [], 0
    for i in range((len(array) - table) // width):
        end = _gv_offset(array, table + i * width, width)
        start = (start + 7) // 8 * 8
        targets.append(_gv_target(array[start:end]))
        start = end
    return connection_id, targets


def remote_message(name: str, body: bytes = b"") -> bytes:
    payload = name.encode("ascii") + b"\0" + body
    return struct.pack(">IB", len(payload), _REMOTE_LITTLE_ENDIAN) + payload


def _ids_body(connection_id: int, target_id: int, text: str | None = None) -> bytes:
    """(tt), or (tts) with a text."""
    body = struct.pack("<QQ", connection_id, target_id)
    return body if text is None else body + text.encode("utf-8") + b"\0"


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RemoteInspectorClient:
    """A front-end connection to the client's WebKitGTK remote inspector server.

    Each inspected page is a ``WebPage`` target; its backend multiplexes the
    page through the ``Target`` domain, so ``Runtime.evaluate`` goes inside
    ``Target.sendMessageToTarget`` to the current page target (it changes on
    navigation).
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.targets: dict[tuple[int, int], tuple[str, str, str]] = {}
        self.closed = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._page_targets: dict[tuple[int, int], str] = {}
        self._changed = asyncio.Event()
        self._setup: set[tuple[int, int]] = set()
        self.console: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._console_enabled: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    async def connect(self, timeout: float = REMOTE_SETUP_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self.port)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise HtmlAgentError(
                        f"The 1C client's WebKit remote inspector is not listening on 127.0.0.1:{self.port}"
                    ) from None
                await asyncio.sleep(0.5)
        self._task = asyncio.create_task(self._read_loop())
        await self._send("SetupInspectorClient")  # (ay) with an empty backend commands hash

    async def wait_targets(self, timeout: float) -> None:
        """Give the server a moment to report its targets (it sends a list per web process)."""
        with suppress(TimeoutError):
            while True:
                self._changed.clear()
                await asyncio.wait_for(self._changed.wait(), timeout)
                timeout = 0.5

    async def close(self) -> None:
        if self._writer is not None and not self.closed:
            for key in self._setup:
                with suppress(Exception):
                    await self._send("FrontendDidClose", _ids_body(*key))
            with suppress(Exception):
                self._writer.close()
        if self._task is not None:
            self._task.cancel()
        for task in list(self._tasks):
            task.cancel()
        self._fail_pending("remote inspector connection closed")
        self.closed = True

    async def _send(self, name: str, body: bytes = b"") -> None:
        if self._writer is None or self.closed:
            raise HtmlAgentError("The WebKit remote inspector connection is closed")
        self._writer.write(remote_message(name, body))
        await self._writer.drain()

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HtmlAgentError(reason))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                size, flags = struct.unpack(">IB", await self._reader.readexactly(5))
                payload = await self._reader.readexactly(size)
                name, _, body = payload.partition(b"\0")
                if not flags & _REMOTE_LITTLE_ENDIAN:
                    LOGGER.warning("WebKit remote inspector: ignoring a big-endian %s message", name)
                    continue
                self._dispatch(name.decode("ascii", "replace"), body)
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.closed = True
            self._fail_pending("The WebKit remote inspector connection closed (was the client stopped?)")
            self._changed.set()

    def _dispatch(self, name: str, body: bytes) -> None:
        if name == "SetTargetList":
            connection_id, targets = decode_target_list(body)
            for key in [k for k in self.targets if k[0] == connection_id]:
                del self.targets[key]
            for target_id, kind, title, url in targets:
                self.targets[(connection_id, target_id)] = (kind, title, url)
            self._changed.set()
        elif name == "SendMessageToFrontend" and len(body) > 16:
            connection_id, target_id = struct.unpack_from("<QQ", body, 0)
            with suppress(ValueError):
                self._frontend_message((connection_id, target_id), json.loads(_gv_string(body[16:])))

    def _frontend_message(self, key: tuple[int, int], message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params") or {}
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and not info.get("isProvisional"):
                self._page_targets[key] = str(info.get("targetId"))
                self._changed.set()
                self._enable_console_soon(key)
        elif method == "Target.didCommitProvisionalTarget":
            self._page_targets[key] = str(params.get("newTargetId"))
            self._changed.set()
            self._enable_console_soon(key)
        elif method == "Target.targetDestroyed":
            if self._page_targets.get(key) == params.get("targetId"):
                self._page_targets.pop(key, None)
        elif method == "Target.dispatchMessageFromTarget":
            with suppress(ValueError, TypeError):
                inner = json.loads(params.get("message") or "")
                if inner.get("method") == "Console.messageAdded":
                    self._console_message(key, (inner.get("params") or {}).get("message") or {})
                    return
                future = self._pending.pop(inner.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(inner)
        elif "id" in message and "error" in message:
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                future.set_result(message)

    def _console_message(self, key: tuple[int, int], message: dict[str, Any]) -> None:
        log = self.console.setdefault(key, [])
        log.append({
            "level": str(message.get("level") or "log"),
            "text": str(message.get("text") or ""),
            "url": str(message.get("url") or ""),
            "line": int(message.get("line") or 0),
            "column": int(message.get("column") or 0),
            "source": str(message.get("source") or ""),
            **({"repeat": int(message["repeatCount"])} if int(message.get("repeatCount") or 1) > 1 else {}),
        })
        del log[:-500]

    def _enable_console_soon(self, key: tuple[int, int]) -> None:
        task = asyncio.get_running_loop().create_task(self._enable_console(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _enable_console(self, key: tuple[int, int]) -> None:
        """Console.enable on the current page target: WebKit replays the messages since the page loaded."""
        page_target = self._page_targets.get(key)
        if not page_target or page_target in self._console_enabled or key not in self._setup:
            return
        self._console_enabled.add(page_target)
        message_id = next(self._ids)
        inner = {"id": message_id, "method": "Console.enable"}
        outer = {"id": message_id, "method": "Target.sendMessageToTarget",
                 "params": {"targetId": page_target, "message": json.dumps(inner)}}
        with suppress(HtmlAgentError, OSError):
            await self._send("SendMessageToBackend", _ids_body(*key, json.dumps(outer)))

    async def inspect(self, key: tuple[int, int]) -> None:
        """Attach to a WebPage target and wait for its page target."""
        if key not in self._setup:
            await self._send("Setup", _ids_body(*key))
            self._setup.add(key)
        await self._page_target(key, REMOTE_SETUP_SECONDS)
        await self._enable_console(key)

    async def _page_target(self, key: tuple[int, int], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while key not in self._page_targets:
            if self.closed or key not in self.targets:
                raise HtmlAgentError("The page is gone (its form was closed)")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HtmlAgentError(f"WebKit target {key[1]} has no page target within {timeout:g}s")
            self._changed.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)
        return self._page_targets[key]

    async def evaluate(self, key: tuple[int, int], expression: str, timeout: float) -> Any:
        page_target = await self._page_target(key, min(timeout, REMOTE_SETUP_SECONDS))
        message_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        inner = {"id": message_id, "method": "Runtime.evaluate",
                 "params": {"expression": expression, "returnByValue": True, "doNotPauseOnExceptionsAndMuteConsole": True}}
        # The outer command shares the id: an error about routing comes back with it.
        outer = {"id": message_id, "method": "Target.sendMessageToTarget",
                 "params": {"targetId": page_target, "message": json.dumps(inner, ensure_ascii=False)}}
        try:
            await self._send("SendMessageToBackend", _ids_body(*key, json.dumps(outer, ensure_ascii=False)))
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise HtmlAgentError(f"No reply from the page within {timeout:g}s") from None
        finally:
            self._pending.pop(message_id, None)
        if "error" in reply:
            error = reply["error"]
            raise HtmlAgentError(f"Inspector error: {error.get('message') if isinstance(error, dict) else error}")
        result = reply.get("result") or {}
        value = result.get("result") or {}
        if result.get("wasThrown"):
            raise HtmlAgentError(str(value.get("description") or value.get("value") or "page script failed"))
        return value.get("value")


@dataclass
class InspectorPage:
    page_id: str
    connection: Any
    hwnd: int = 0
    target: tuple[int, int] | None = None
    url: str = ""
    title: str = ""
    opened_at: float = field(default_factory=time.time)
    closed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "page": self.page_id,
            "url": self.url,
            "title": self.title,
            "alive": not self.closed,
            "opened_at": _dt.datetime.fromtimestamp(self.opened_at, _dt.UTC).isoformat(timespec="seconds"),
        }


class HtmlInspector:
    """A session's page bridge server plus the bookkeeping of the client's WebKit views."""

    MAX_ATTEMPTS = 2

    def __init__(self) -> None:
        self.port = 0
        self.pages: dict[str, InspectorPage] = {}
        self.desktop = ""
        self.client_pid = 0
        self.last_error = ""
        self._server: Any = None
        self._ids = itertools.count(1)
        self._page_numbers = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tokens: dict[str, tuple[int, asyncio.Future]] = {}
        self._front_ends: dict[int, int] = {}
        self._known_front_ends: set[int] = set()
        self._attempts: dict[int, int] = {}
        self._open_lock = asyncio.Lock()
        # Linux: the client is started with its WebKit remote inspector server on this port.
        self.remote_port = 0 if os_support.IS_WINDOWS else free_local_port()
        self._remote: RemoteInspectorClient | None = None

    async def start(self) -> int:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = int(next(iter(self._server.sockets)).getsockname()[1])
        return self.port

    async def stop(self) -> None:
        if self._remote is not None:
            await self._remote.close()
            self._remote = None
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
            self._server = None
        for future in [*self._pending.values(), *(f for _, f in self._tokens.values())]:
            if not future.done():
                future.set_exception(HtmlAgentError("HTML page bridge stopped"))
        self._pending.clear()
        self._tokens.clear()

    def client_env(self) -> dict[str, str]:
        """Environment for the test client: Linux clients serve the WebKit remote inspector."""
        return {"WEBKIT_INSPECTOR_SERVER": f"127.0.0.1:{self.remote_port}"} if self.remote_port else {}

    def attach(self, desktop: str, pid: int) -> None:
        self.desktop, self.client_pid = desktop, int(pid or 0)

    def status(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "running": self._server is not None,
            "pages_alive": sum(1 for p in self.pages.values() if not p.closed),
            **({"remote_inspector_port": self.remote_port} if self.remote_port else {}),
            **({"last_error": self.last_error} if self.last_error else {}),
        }

    # --- bridge connections ---

    def expect(self, hwnd: int = 0) -> tuple[str, asyncio.Future]:
        """Register a one-time token for the bridge about to be typed into ``hwnd``'s console."""
        token = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        self._tokens[token] = (hwnd, future)
        return token, future

    async def _handle(self, connection: Any) -> None:
        # The bridge runs in the inspected page, so its Origin is the page's own:
        # only a one-time token typed into the console admits a connection.
        page: InspectorPage | None = None
        try:
            async for raw in connection:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if page is None:
                    expected = self._tokens.pop(str(message.get("token") or ""), None) if message.get("type") == "hello" else None
                    if expected is None:
                        LOGGER.warning("HTML page bridge: rejected a connection without a valid token")
                        await connection.close(1008, "token required")
                        return
                    hwnd, future = expected
                    page = InspectorPage(f"p{next(self._page_numbers)}", connection, hwnd=hwnd)
                    self.pages[page.page_id] = page
                    if not future.done():
                        future.set_result(page)
                elif message.get("type") == "reply":
                    future = self._pending.pop(message.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            if page is not None:
                page.closed = True

    async def evaluate(self, page: InspectorPage, expression: str, timeout: float = 30.0) -> Any:
        """Evaluate a JavaScript expression in the page and return its JSON value."""
        if page.closed:
            raise HtmlAgentError(f"Page {page.page_id} is closed (its form was closed or it reloaded)")
        if page.target is not None:
            if self._remote is None or self._remote.closed or page.target not in self._remote.targets:
                page.closed = True
                raise HtmlAgentError(f"Page {page.page_id} is closed (its form was closed)")
            return await self._remote.evaluate(page.target, expression, timeout)
        message_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await page.connection.send(json.dumps({"id": message_id, "expression": expression}, ensure_ascii=False))
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise HtmlAgentError(f"No reply from page {page.page_id} within {timeout:g}s") from None
        except websockets.ConnectionClosed:
            page.closed = True
            raise HtmlAgentError(f"Page {page.page_id} disconnected") from None
        finally:
            self._pending.pop(message_id, None)
        if "error" in reply:
            raise HtmlAgentError(str(reply["error"]))
        return reply.get("value")

    async def run(self, page: InspectorPage, op: str, args: dict[str, Any], timeout: float = 30.0) -> Any:
        outcome = await self.evaluate(page, ops_expression(op, args), timeout=timeout) or {}
        if not outcome.get("ok"):
            raise HtmlAgentError(str(outcome.get("error") or "page operation failed"))
        return outcome.get("result")

    async def refresh(self, page: InspectorPage) -> None:
        info = await self.run(page, "info", {}, timeout=10)
        page.url, page.title = str(info.get("url") or ""), str(info.get("title") or "")

    # --- connecting pages ---

    async def _views(self) -> list[int]:
        return await asyncio.to_thread(find_webviews, self.desktop, self.client_pid)

    async def open_inspectors(self) -> list[str]:
        """Bridge every WebKit view of the client that has no live page yet; return new page ids."""
        if not self.client_pid:
            raise HtmlAgentError("The test client is not running for the HTML page bridge")
        if not os_support.IS_WINDOWS:
            async with self._open_lock:
                return await self._open_remote()
        async with self._open_lock:
            live = {p.hwnd for p in self.pages.values() if not p.closed}
            opened: list[str] = []
            for view in await self._views():
                if view in self._known_front_ends or view in live or self._attempts.get(view, 0) >= self.MAX_ATTEMPTS:
                    continue
                page = await self._connect(view)
                if page is not None:
                    opened.append(page.page_id)
            return opened

    async def _open_remote(self) -> list[str]:
        """Linux: attach to every WebPage target of the remote inspector that shows a page."""
        if self._remote is None or self._remote.closed:
            self._remote = RemoteInspectorClient(self.remote_port)
            await self._remote.connect()
            await self._remote.wait_targets(3.0)
        remote = self._remote
        for page in self.pages.values():
            if page.target is not None and page.target not in remote.targets:
                page.closed = True
        live = {p.target for p in self.pages.values() if not p.closed}
        opened: list[str] = []
        for key, (kind, title, url) in list(remote.targets.items()):
            if kind != "WebPage" or not url or key in live:
                continue
            try:
                await remote.inspect(key)
            except HtmlAgentError as exc:
                self.last_error = str(exc)
                LOGGER.warning("%s", self.last_error)
                continue
            page = InspectorPage(f"p{next(self._page_numbers)}", None, target=key, url=url, title=title)
            try:
                info = await self.run(page, "info", {}, timeout=10)
            except HtmlAgentError as exc:
                self.last_error = str(exc)
                continue
            page.url, page.title = str(info.get("url") or url), str(info.get("title") or "")
            # The client keeps hidden, empty WebKit views; an HTML field with text content is about:blank too.
            if page.url in {"", "about:blank"} and not info.get("text_length"):
                continue
            self.pages[page.page_id] = page
            opened.append(page.page_id)
        return opened

    async def _front_end(self, view: int) -> int:
        """The docked inspector front end of ``view``, opening the inspector when needed."""
        views = set(await self._views())
        front_end = self._front_ends.get(view)
        if front_end in views:
            return front_end
        await asyncio.to_thread(_run_on_client_desktop, self.desktop, lambda: _press_inspector_hotkey(view))
        deadline = time.monotonic() + INSPECTOR_OPEN_SECONDS
        new: set[int] = set()
        while not new and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            new = set(await self._views()) - views
        # Whatever appeared is a front end and must never be inspected itself.
        self._known_front_ends.update(new)
        if not new:
            raise HtmlAgentError(f"The inspector of WebKit view {view} did not open")
        front_end = min(new)
        self._front_ends[view] = front_end
        await asyncio.sleep(2.0)  # let the front end load its UI before the console is used
        return front_end

    async def _connect(self, view: int) -> InspectorPage | None:
        self._attempts[view] = self._attempts.get(view, 0) + 1
        token, future = self.expect(view)
        try:
            front_end = await self._front_end(view)
            # A front end that is still loading drops the click and the keys: type again until the page connects.
            deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
            while not future.done() and time.monotonic() < deadline:
                await asyncio.to_thread(_type_into_console, self.desktop, front_end, loader_js(self.port, token))
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(future), RETYPE_SECONDS)
            page = await asyncio.wait_for(future, 0.1)
        except (HtmlAgentError, TimeoutError) as exc:
            self.last_error = str(exc) or f"The page of WebKit view {view} did not connect within {CONNECT_TIMEOUT_SECONDS:g}s"
            LOGGER.warning("%s", self.last_error)
            return None
        finally:
            self._tokens.pop(token, None)
        self._attempts.pop(view, None)
        with suppress(HtmlAgentError):
            await self.run(page, "hook_console", {}, timeout=10)  # errors from now on; see console()
            await self.refresh(page)
        return page

    # --- page selection ---

    async def list_pages(self, include_dead: bool = False) -> list[dict[str, Any]]:
        for page in self.pages.values():
            if not page.closed:
                with suppress(HtmlAgentError):
                    await self.refresh(page)
        return [p.to_json() for p in self.pages.values() if include_dead or not p.closed]

    def resolve_page(self, selector: str = "") -> InspectorPage:
        alive = [p for p in self.pages.values() if not p.closed]
        if selector:
            if selector in self.pages:
                return self.pages[selector]
            found = [p for p in alive if selector.lower() in p.url.lower() or selector.lower() in p.title.lower()]
            if len(found) == 1:
                return found[0]
            raise HtmlAgentError(
                f"{'Several' if found else 'No'} live HTML pages match {selector!r}; pages: "
                + json.dumps([p.to_json() for p in alive], ensure_ascii=False)
            )
        if len(alive) == 1:
            return alive[0]
        if not alive:
            hint = f" ({self.last_error})" if self.last_error else ""
            raise HtmlAgentError(f"No live HTML page: open a form with an HTML document field showing a web page{hint}")
        raise HtmlAgentError("Several live HTML pages; pass page: " + json.dumps([p.to_json() for p in alive], ensure_ascii=False))

    async def page(self, selector: str = "") -> InspectorPage:
        """Resolve a page, first opening inspectors of WebKit views that appeared since the last call."""
        await self.open_inspectors()
        return self.resolve_page(selector)

    async def console(self, selector: str = "", levels: frozenset[str] = frozenset({"error", "warning"}),
                      limit: int = 100, clear: bool = False, check_scripts: bool = True) -> dict[str, Any]:
        """Console errors/warnings of a page, plus scripts this WebKit cannot parse.

        Linux reads the inspector's Console domain, which keeps messages since
        the page loaded.  The Windows page bridge hooks errors only once it is
        connected, so earlier load-time errors are missing there; the script
        check (every script compiled with ``new Function``) still reports
        syntax this WebKit does not support, such as ``?.``.
        """
        page = await self.page(selector)
        result: dict[str, Any] = {"page": page.page_id, "url": page.url}
        if page.target is not None and self._remote is not None:
            log = self._remote.console.get(page.target, [])
            messages = list(log)
            if clear:
                log.clear()
            result["collected_since"] = "page load"
        else:
            hooked = await self.run(page, "console", {"limit": 500, "clear": clear}, timeout=10)
            if not hooked.get("hooked"):
                await self.run(page, "hook_console", {}, timeout=10)
            messages = list(hooked.get("messages") or [])
            result["collected_since"] = _dt.datetime.fromtimestamp(page.opened_at, _dt.UTC).isoformat(timespec="seconds")
        selected = [m for m in messages if not levels or m.get("level") in levels]
        result["messages"] = selected[-limit:] if limit > 0 else selected
        result["total"] = len(selected)
        if check_scripts:
            result["scripts"] = await self.run(page, "check_scripts", {}, timeout=60)
        return result

    async def command(self, selector: str, op: str, args: dict[str, Any], timeout: float = 30.0) -> Any:
        return await self.run(await self.page(selector), op, args, timeout=timeout)
