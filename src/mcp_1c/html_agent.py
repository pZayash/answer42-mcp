"""Live access to web pages in HTML document fields through the embedded Web Inspector.

The test-client API only exposes the HTML a field had when it finished loading,
so pages that render themselves with JavaScript (chats, SPAs) cannot be read or
driven through it.  The Windows thin client renders HTML fields with an
embedded WebKit whose Web Inspector opens on Ctrl+Alt+Shift+F12 (developer
extras are on), but it has no remote-inspector server.  So, for each WebKit
view of the client (one per HTML field), Answer42:

* sends the inspector hot key; 1C docks the inspector front end into the form
  as one more WebKit view;
* focuses the front end's console prompt (a click, then Ctrl+`) and types
  ``loader_js``: a short line that the console runs in the inspected page; it
  opens this session's local WebSocket with a one-time token in the URL, and the
  server answers with ``PAGE_BRIDGE_JS``, which evaluates the expressions
  Answer42 sends;
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
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from . import os_support, webkit_patch, win_desktop

LOGGER = logging.getLogger(__name__)

WEBVIEW_CLASS = "WebViewWindowClass"
INSPECTOR_WINDOW_CLASS = "WebInspectorWindowClass"
INSPECTOR_OPEN_SECONDS = 10.0  # only a limit: the first opening after a cold start can take several seconds
CONNECT_TIMEOUT_SECONDS = 15.0
# The first evaluation in a fresh console can take a second; then the loader is typed again.
RETYPE_SECONDS = 2.0
PAGE_PATH = "/answer42-page/"
PATCHED_CONNECT_SECONDS = 6.0

# Typed into the console character by character, so as short as possible: it only
# opens the socket; the server's first message installs PAGE_BRIDGE_JS.
LOADER_JS = (
    "__answer42=new WebSocket('ws://127.0.0.1:__PORT__" + PAGE_PATH + "__TOKEN__');"
    "__answer42.onmessage=function(e){(0,eval)(e.data)(this)}"
)

PAGE_BRIDGE_JS = r"""(function (w) {
  try { delete window.__answer42; } catch (ignore) {}
  w.onmessage = function (e) {
    var m = JSON.parse(e.data), r;
    try { r = { type: 'reply', id: m.id, value: (0, eval)(m.expression) }; }
    catch (x) { r = { type: 'reply', id: m.id, error: String((x && x.message) || x) }; }
    w.send(JSON.stringify(r));
  };
  w.send(JSON.stringify({ type: 'hello' }));
})"""


def loader_js(port: int, token: str) -> str:
    """The line typed into the inspector console (ASCII only, no line breaks)."""
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
    fetch: function (a) {
      var url = a.url;
      if (!url) {
        var el = one(a);
        url = el.href || el.getAttribute('href') || el.getAttribute('data-href') || el.src || '';
        if (!url) { throw new Error('the element has no link (href, data-href or src)'); }
      }
      var link = document.createElement('a');
      link.href = url;
      var x = new XMLHttpRequest();
      x.open('GET', link.href, false);
      x.overrideMimeType('text/plain; charset=x-user-defined');
      x.send(null);
      var body = x.responseText || '', size = body.length;
      if (a.max_bytes && size > a.max_bytes) { throw new Error('the file is too large: ' + size + ' bytes, the limit is ' + a.max_bytes); }
      var chunks = [], step = 0x6000;
      for (var i = 0; i < size; i += step) {
        var part = body.substr(i, step), bytes = new Array(part.length);
        for (var j = 0; j < part.length; j++) { bytes[j] = part.charCodeAt(j) & 0xff; }
        chunks.push(String.fromCharCode.apply(null, bytes));
      }
      return { url: x.responseURL || link.href, status: x.status, content_type: x.getResponseHeader('Content-Type') || '',
               disposition: x.getResponseHeader('Content-Disposition') || '', size: size, base64: btoa(chunks.join('')) };
    },
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


def _protocol_value(reply: dict[str, Any]) -> Any:
    """The JSON value of a Runtime.evaluate reply (returnByValue)."""
    if "error" in reply:
        error = reply["error"]
        raise HtmlAgentError(f"Inspector error: {error.get('message') if isinstance(error, dict) else error}")
    result = reply.get("result") or {}
    value = result.get("result") or {}
    if result.get("wasThrown"):
        raise HtmlAgentError(str(value.get("description") or value.get("value") or "page script failed"))
    return value.get("value")


def ops_expression(op: str, args: dict[str, Any]) -> str:
    """Build the Runtime.evaluate expression that runs one page operation."""
    return f"({OPS_JS})({json.dumps(op)}, {json.dumps(args, ensure_ascii=False)})"


# --- Windows: find the client's WebKit views, open their inspectors, type into the console ---

_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_F12, _VK_RETURN, _VK_A, _VK_C, _VK_HOME, _VK_DELETE = (
    0x10, 0x11, 0x12, 0x7B, 0x0D, 0x41, 0x43, 0x24, 0x2E,
)
_VK_LSHIFT, _VK_LCONTROL, _VK_LMENU = 0xA0, 0xA2, 0xA4
_WM_KEYDOWN, _WM_KEYUP, _WM_CHAR, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x100, 0x101, 0x102, 0x104, 0x105
_WM_SETFOCUS = 0x7


def _user32() -> Any:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.SendMessageTimeoutW.argtypes = [
        wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    return user32


_SMTO_ABORTIFHUNG = 0x2
_SEND_TIMEOUT_MS = 3000


def _send(user32: Any, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
    """SendMessage that returns once the window has handled the message (or the thread hangs)."""
    import ctypes

    result = ctypes.c_size_t()
    return bool(user32.SendMessageTimeoutW(hwnd, message, wparam, lparam, _SMTO_ABORTIFHUNG, _SEND_TIMEOUT_MS, ctypes.byref(result)))


def _enum_client_windows(desktop: str, pid: int, visit: Any) -> None:
    """Call ``visit(hwnd, class_name)`` for top-level windows of ``pid`` (on a hidden desktop when one is named)."""
    import ctypes
    import ctypes.wintypes as wt

    user32 = _user32()
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def top(hwnd: int, _lparam: int) -> bool:
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            visit(int(hwnd), _class_name(user32, hwnd))
        return True

    if win_desktop.is_hidden_desktop_name(desktop):
        handle = win_desktop._open_desktop(desktop)
        try:
            user32.EnumDesktopWindows(handle, proc(top), 0)
        finally:
            user32.CloseDesktop(handle)
    else:
        user32.EnumWindows(proc(top), 0)


def _class_name(user32: Any, hwnd: int) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def find_inspector_windows(desktop: str, pid: int) -> dict[int, str]:
    """The client's (hidden) Web Inspector windows and their titles.

    WebKit names the window "Web Inspector — <url>" once the front end has loaded
    and reported the inspected page: the sign that its console accepts input.
    """
    import ctypes

    user32 = _user32()
    found: dict[int, str] = {}

    def visit(hwnd: int, class_name: str) -> None:
        if class_name == INSPECTOR_WINDOW_CLASS:
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            found[hwnd] = buf.value

    _enum_client_windows(desktop, pid, visit)
    return found


def find_webviews(desktop: str, pid: int) -> list[int]:
    """Visible WebKit view windows of process ``pid`` (on a hidden desktop when one is named)."""
    import ctypes
    import ctypes.wintypes as wt

    user32 = _user32()
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    found: list[int] = []

    def child(hwnd: int, _lparam: int) -> bool:
        if _class_name(user32, hwnd) == WEBVIEW_CLASS and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    def visit(hwnd: int, class_name: str) -> None:
        # An undocked inspector window holds a WebKit view of the same process: never inspect it.
        if class_name != INSPECTOR_WINDOW_CLASS:
            user32.EnumChildWindows(hwnd, proc(child), 0)

    _enum_client_windows(desktop, pid, visit)
    return found


def _post_shortcut(hwnd: int, vk: int, modifiers: tuple[int, ...] = (), system: bool = False, char: int | None = None) -> bool:
    """A key with modifiers to a window of another thread, without the real keyboard.

    The modifiers are put into the keyboard state shared with the window's
    thread (AttachThreadInput + SetKeyboardState); the key messages are *sent*, so
    the window has handled them before the state is restored and no delay has to
    be guessed.  This works on a hidden desktop, where SendInput cannot reach.
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
        handled = _send(user32, hwnd, down, vk, flags)
        if char is not None:
            _send(user32, hwnd, _WM_CHAR, char, flags)
        _send(user32, hwnd, up, vk, flags | (3 << 30))
        return handled
    finally:
        user32.SetKeyboardState(previous)
        if attached:
            user32.AttachThreadInput(me, target, False)


def _press_inspector_hotkey(hwnd: int) -> None:
    """Ctrl+Alt+Shift+F12: open the Web Inspector of a WebKit view."""
    _post_shortcut(hwnd, _VK_F12, (_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LSHIFT, _VK_LCONTROL, _VK_LMENU), system=True)


def _type_into_console(desktop: str, front_end: int, text: str) -> None:
    """Run ``text`` in the console of an inspector front end.

    WM_SETFOCUS makes WebKit treat the front end as focused, which its key
    shortcuts need, without moving the focus inside its document (a click would:
    on the toolbar it takes the focus from the prompt, in the Elements tree it
    selects a node and paints its highlight over the page).  Ctrl+Alt+C (matched by key code,
    so the keyboard layout does not matter) shows the Console tab, which focuses
    its prompt; the inspector remembers the tab, so later it opens there.  Keys
    must never reach the Elements tab: its DOM tree takes the focus when it
    finishes loading, and keys typed into it, Enter above all, edit the page's
    DOM.  A typed space brings the focus back to the prompt if an earlier attempt
    left it in the message list, and Ctrl+A selects what is in the prompt.

    The text is typed behind ``//``: the prompt's autocompletion answers typed
    code asynchronously and may replace what was typed after it, but it never
    completes inside a comment.  Home and two Deletes then uncomment the line, and
    Enter with the cursor not at the end runs it at once (at the end the prompt
    first asks the page whether the code is complete).  The prompt takes the focus
    asynchronously and may swap the first two characters, which "//" absorbs.
    Esc must not be used: 1C handles it first and closes the form.
    """
    user32 = _user32()
    _send(user32, front_end, _WM_SETFOCUS, 0, 0)
    ctrl = (_VK_CONTROL, _VK_LCONTROL)
    _run_on_client_desktop(desktop, lambda: _post_shortcut(front_end, _VK_C, (*ctrl, _VK_MENU, _VK_LMENU)))
    time.sleep(0.05)
    _send(user32, front_end, _WM_CHAR, ord(" "), 1)
    time.sleep(0.05)
    _run_on_client_desktop(desktop, lambda: _post_shortcut(front_end, _VK_A, ctrl))
    for char in "//" + text:
        _send(user32, front_end, _WM_CHAR, ord(char), 1)
    time.sleep(0.1)  # the prompt reads typed text asynchronously
    _post_shortcut(front_end, _VK_HOME)
    _post_shortcut(front_end, _VK_DELETE)
    _post_shortcut(front_end, _VK_DELETE)
    _post_shortcut(front_end, _VK_RETURN, char=13)


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


def console_entry(message: dict[str, Any]) -> dict[str, Any]:
    """A Console.messageAdded message of the inspector protocol, as html_console reports it."""
    return {
        "level": str(message.get("level") or "log"),
        "text": str(message.get("text") or ""),
        "url": str(message.get("url") or ""),
        "line": int(message.get("line") or 0),
        "column": int(message.get("column") or 0),
        "source": str(message.get("source") or ""),
        **({"repeat": int(message["repeatCount"])} if int(message.get("repeatCount") or 1) > 1 else {}),
    }


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
        log.append(console_entry(message))
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



# --- Linux, local inspector (html_mode patched) ---
#
# 1C opens the WebKitGTK inspector of the focused HTML field on Ctrl+Alt+Shift+F12, docked into
# the form.  GTK widgets are no X11 windows, so the field gets the focus through the test client
# (activate_field) and the keys go to the client's window with xdotool (XTEST).  patched: GLib's
# G_RESOURCE_OVERLAYS replaces one resource of the inspector front end, its localized strings
# (the UI falls back to the English keys), with a file that also carries the session's port,
# token and LINUX_BRIDGE_JS; no system file changes and no administrator rights.

INSPECTOR_STRINGS_RESOURCE = "/org/webkit/inspector/Localizations/en.lproj"
INSPECTOR_STRINGS_FILE = "localizedStrings.js"
LINUX_INSPECTOR_KEYS = "ctrl+alt+shift+F12"

# Like webkit_patch.BRIDGE_JS, plus the Target domain: WebKitGTK's front end talks to the page
# through Target.sendMessageToTarget / Target.dispatchMessageFromTarget.
LINUX_BRIDGE_JS = r"""(function () {
  var config = window.ANSWER42_BRIDGE;
  window.ANSWER42_BRIDGE = null;
  if (!config || !config.port || !config.token) { return; }
  var BASE_ID = __BASE_ID__;
  function start() {
    if (!window.InspectorFrontendAPI || !window.InspectorFrontendHost || !InspectorFrontendAPI.dispatchMessageAsync) { setTimeout(start, 50); return; }
    var ws = new WebSocket('ws://127.0.0.1:' + config.port + '__PATH__' + config.token), queue = [];
    function post(obj) { var text = JSON.stringify(obj); if (ws.readyState === 1) { ws.send(text); } else { queue.push(text); } }
    function parse(message) {
      if (typeof message !== 'string') { return message; }
      if (message.indexOf('"id":') < 0 && message.indexOf('"Target.') < 0 && message.indexOf('Console.messageAdded') < 0) { return null; }
      try { return JSON.parse(message); } catch (e) { return null; }
    }
    function divert(obj) {
      if (!obj) { return false; }
      if (typeof obj.id === 'number' && obj.id >= BASE_ID) { post({ type: 'message', message: obj }); return true; }
      if (obj.method === 'Target.dispatchMessageFromTarget') {
        var inner = obj.params && obj.params.message, o = null;
        if (typeof inner === 'string' && (inner.indexOf('"id":') >= 0 || inner.indexOf('Console.messageAdded') >= 0)) {
          try { o = JSON.parse(inner); } catch (e) { o = null; }
        }
        if (o && typeof o.id === 'number' && o.id >= BASE_ID) { post({ type: 'message', message: o }); return true; }
        if (o && o.method === 'Console.messageAdded') { post({ type: 'event', message: o }); }
        return false;
      }
      if (obj.method && obj.method.indexOf('Target.') === 0) { post({ type: 'target', message: obj }); }
      if (obj.method === 'Console.messageAdded') { post({ type: 'event', message: obj }); }
      return false;
    }
    ['dispatchMessageAsync', 'dispatchMessage'].forEach(function (name) {
      var original = InspectorFrontendAPI[name];
      if (!original) { return; }
      InspectorFrontendAPI[name] = function (message) {
        if (divert(parse(message))) { return undefined; }
        return original.apply(this, arguments);
      };
    });
    ws.onopen = function () { ws.send(JSON.stringify({ type: 'hello', version: 1 })); while (queue.length) { ws.send(queue.shift()); } };
    ws.onmessage = function (event) {
      var m;
      try { m = JSON.parse(event.data); } catch (e) { return; }
      if (m.type === 'send') { InspectorFrontendHost.sendMessageToBackend(JSON.stringify(m.message)); }
    };
  }
  start();
})();
""".replace("__BASE_ID__", str(webkit_patch.BRIDGE_BASE_ID)).replace("__PATH__", webkit_patch.INSPECTOR_PATH)


def write_inspector_strings(overlay_dir: Path, port: int = 0, token: str = "") -> None:
    """The overlay localizedStrings.js: empty strings, the bridge config (or none) and LINUX_BRIDGE_JS."""
    overlay_dir.mkdir(parents=True, exist_ok=True)
    config = json.dumps({"port": int(port), "token": token}) if port and token else "null"
    text = f"var localizedStrings = {{}};\nwindow.ANSWER42_BRIDGE = {config};\n{LINUX_BRIDGE_JS}"
    tmp = overlay_dir / (INSPECTOR_STRINGS_FILE + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(overlay_dir / INSPECTOR_STRINGS_FILE)


def _xdotool(display: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DISPLAY": display} if display else None
    return subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True, timeout=30)


def _client_x_window(display: str, pid: int) -> str:
    """The client's main X11 window: the visible window of the pid that has a title."""
    for window in _xdotool(display, "search", "--pid", str(pid), "--onlyvisible", "--name", "").stdout.split():
        if _xdotool(display, "getwindowname", window).stdout.strip():
            return window
    raise HtmlAgentError(f"No visible window of the test client (pid {pid}) on display {display or '$DISPLAY'}")


def _x_keys(display: str, pid: int, *keys: str) -> None:
    window = _client_x_window(display, pid)
    _xdotool(display, "windowfocus", "--sync", window)
    for key in keys:
        _xdotool(display, "key", "--clearmodifiers", key)



@dataclass
class InspectorPage:
    page_id: str
    connection: Any
    hwnd: int = 0
    target: tuple[int, int] | None = None
    # patched: the connection is the inspector front end, not the page.
    front_end: bool = False
    # WebKitGTK front end: the page is reached through this target of the Target domain.
    page_target: str = ""
    # Linux console / patched: the HTML field the page belongs to.
    field_name: str = ""
    console_log: list[dict[str, Any]] = field(default_factory=list)
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


HTML_MODES = ("auto", "console", "patched", "remote", "disabled")


def resolve_mode(mode: str = "auto") -> str:
    """The way to reach live pages: auto stays auto on Windows (see check_platform), remote on Linux.

    console (Windows): open the embedded Web Inspector with its hot key and type
    the loader into its console; patched (Windows): the bridge built into the
    platform's Web Inspector (webkit_patch); remote (Linux): the WebKitGTK remote
    inspector of a client started with WEBKIT_INSPECTOR_SERVER.
    """
    mode = (mode or "auto").strip().lower()
    if mode not in HTML_MODES:
        raise HtmlAgentError(f"Unknown html_mode {mode!r}: use {', '.join(HTML_MODES)}")
    if mode == "auto":
        return "auto" if os_support.IS_WINDOWS else "remote"
    if mode == "remote" and os_support.IS_WINDOWS:
        raise HtmlAgentError("html_mode=remote is for the Linux client (WebKitGTK); on Windows use console or patched")
    if mode == "console" and not os_support.IS_WINDOWS:
        # The docked WebKitGTK inspector is no X11 window: keys sent with XTEST go to the focused widget,
        # which stays the page (its input field), so the loader would be typed and submitted there.
        raise HtmlAgentError(
            "html_mode=console is not available on Linux: keys cannot be routed to the WebKitGTK inspector "
            "console and would land in the page itself; use patched (nothing is typed) or remote"
        )
    return mode


class HtmlInspector:
    """A session's page bridge server plus the bookkeeping of the client's WebKit views."""

    MAX_ATTEMPTS = 2

    def __init__(self, mode: str = "auto") -> None:
        self.mode = resolve_mode(mode)
        self.port = 0
        self.pages: dict[str, InspectorPage] = {}
        self.desktop = ""
        self.client_pid = 0
        self.client_bin = ""
        self.last_error = ""
        self._server: Any = None
        self._ids = itertools.count(1)
        self._page_numbers = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tokens: dict[str, tuple[int, asyncio.Future, str]] = {}
        self._front_ends: dict[int, int] = {}
        self._known_front_ends: set[int] = set()
        self._attempts: dict[int, int] = {}
        self._open_lock = asyncio.Lock()
        self.last_connect: dict[str, Any] = {}  # Windows: timings of the latest page connection
        # remote: the client is started with its WebKit remote inspector server on this port.
        self.remote_port = free_local_port() if self.mode == "remote" else 0
        # Linux console / patched: finds and focuses HTML fields through the test client (set by the server).
        self.field_driver: Any = None
        self.overlay_dir: Path | None = None  # Linux patched: the session's G_RESOURCE_OVERLAYS directory
        self._outer: dict[int, int] = {}  # Target.sendMessageToTarget id -> the inner message id
        self._field_keys: dict[str, int] = {}
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
        for future in [*self._pending.values(), *(f for _, f, _ in self._tokens.values())]:
            if not future.done():
                future.set_exception(HtmlAgentError("HTML page bridge stopped"))
        self._pending.clear()
        self._tokens.clear()

    def client_env(self) -> dict[str, str]:
        """Environment for the test client: the WebKit remote inspector (remote) or the inspector overlay (Linux patched)."""
        if self.remote_port:
            return {"WEBKIT_INSPECTOR_SERVER": f"127.0.0.1:{self.remote_port}"}
        if self._linux_local() and self.mode == "patched" and self.overlay_dir is not None:
            write_inspector_strings(self.overlay_dir)
            return {"G_RESOURCE_OVERLAYS": f"{INSPECTOR_STRINGS_RESOURCE}={self.overlay_dir}"}
        return {}

    def _linux_local(self) -> bool:
        return not os_support.IS_WINDOWS and self.mode in {"console", "patched"}

    def attach(self, desktop: str, pid: int, client_path: str = "") -> None:
        self.desktop, self.client_pid = desktop, int(pid or 0)
        self.client_bin = str(Path(client_path).parent) if client_path else ""

    def check_platform(self, client_path: str) -> None:
        """Settle auto for this client's platform; fail early when the chosen mode cannot work with it.

        Windows auto prefers the bridge built into the platform's Web Inspector
        (webkit_patch) when it is installed and current, and types into the
        console otherwise.
        """
        if self.mode == "auto":
            self.mode = "patched" if webkit_patch.is_ready(Path(client_path).parent) else "console"
        if self._linux_local():
            if not shutil.which("xdotool"):  # Ctrl+Alt+Shift+F12 that opens the inspector
                raise HtmlAgentError(f"html_mode={self.mode} on Linux needs xdotool (apt install xdotool), or use html_mode=remote")
            return
        if self.mode == "patched":
            bin_dir = Path(client_path).parent
            if not webkit_patch.is_ready(bin_dir):
                raise HtmlAgentError(
                    f"html_mode=patched: the Web Inspector of the platform in {bin_dir} has no current Answer42 bridge; "
                    f"install it with html_inspector_patch(action='install', version='{bin_dir.parent.name}') "
                    "(or `answer42 webkit-inspector install`), or use html_mode=console"
                )

    def status(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "running": self._server is not None,
            "pages_alive": sum(1 for p in self.pages.values() if not p.closed),
            **({"remote_inspector_port": self.remote_port} if self.remote_port else {}),
            "mode": self.mode,
            **({"last_connect": self.last_connect} if self.last_connect else {}),
            **({"last_error": self.last_error} if self.last_error else {}),
        }

    # --- bridge connections ---

    def expect(self, hwnd: int = 0, path: str = PAGE_PATH) -> tuple[str, asyncio.Future]:
        """Register a one-time token for the bridge about to connect for ``hwnd``.

        ``PAGE_PATH``: the loader typed into the console; ``webkit_patch.INSPECTOR_PATH``:
        the bridge of a patched inspector front end.
        """
        token = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        self._tokens[token] = (hwnd, future, path)
        return token, future

    async def _handle(self, connection: Any) -> None:
        # The bridge runs in the inspected page, so its Origin is the page's own:
        # only a one-time token typed into the console admits a connection.
        path = str(getattr(getattr(connection, "request", None), "path", "") or "")
        expected = None
        for prefix in (PAGE_PATH, webkit_patch.INSPECTOR_PATH):
            token = path[len(prefix):] if path.startswith(prefix) else ""
            if token and self._tokens.get(token, (0, None, ""))[2] == prefix:
                expected = self._tokens.pop(token)
        if expected is None:
            LOGGER.warning("HTML page bridge: rejected a connection without a valid token")
            await connection.close(1008, "token required")
            return
        hwnd, future, kind = expected
        front_end = kind == webkit_patch.INSPECTOR_PATH
        page: InspectorPage | None = None
        try:
            if not front_end:
                await connection.send(PAGE_BRIDGE_JS)
            async for raw in connection:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if page is None:
                    if message.get("type") != "hello":
                        continue
                    page = InspectorPage(f"p{next(self._page_numbers)}", connection, hwnd=hwnd, front_end=front_end)
                    self.pages[page.page_id] = page
                    if not future.done():
                        future.set_result(page)
                elif message.get("type") == "reply":
                    waiting = self._pending.pop(message.get("id"), None)
                    if waiting is not None and not waiting.done():
                        waiting.set_result(message)
                elif message.get("type") == "message":  # a patched front end relays a protocol reply
                    reply = message.get("message") or {}
                    inner = self._outer.pop(reply.get("id"), None)
                    if inner is not None:  # the Target.sendMessageToTarget reply: only an error matters
                        waiting = self._pending.pop(inner, None) if "error" in reply else None
                    else:
                        waiting = self._pending.pop(reply.get("id"), None)
                    if waiting is not None and not waiting.done():
                        waiting.set_result(reply)
                elif message.get("type") == "target":  # WebKitGTK front end: the page target and its changes
                    event = message.get("message") or {}
                    params = event.get("params") or {}
                    info = params.get("targetInfo") or {}
                    if event.get("method") == "Target.targetCreated" and info.get("type") == "page" and not info.get("isProvisional"):
                        page.page_target = str(info.get("targetId") or "")
                    elif event.get("method") == "Target.didCommitProvisionalTarget":
                        page.page_target = str(params.get("newTargetId") or "")
                elif message.get("type") == "event":  # ... or a console message
                    params = (message.get("message") or {}).get("params") or {}
                    page.console_log.append(console_entry(params.get("message") or {}))
                    del page.console_log[:-500]
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
        if page.front_end:
            message_id += webkit_patch.BRIDGE_BASE_ID  # the front end relays only ids from BRIDGE_BASE_ID
            evaluate = {"id": message_id, "method": "Runtime.evaluate", "params": {
                "expression": expression, "returnByValue": True, "doNotPauseOnExceptionsAndMuteConsole": True}}
            if page.page_target:
                outer_id = next(self._ids) + webkit_patch.BRIDGE_BASE_ID
                self._outer[outer_id] = message_id
                evaluate = {"id": outer_id, "method": "Target.sendMessageToTarget", "params": {
                    "targetId": page.page_target, "message": json.dumps(evaluate, ensure_ascii=False)}}
            outgoing: dict[str, Any] = {"type": "send", "message": evaluate}
        else:
            outgoing = {"id": message_id, "expression": expression}
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await page.connection.send(json.dumps(outgoing, ensure_ascii=False))
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise HtmlAgentError(f"No reply from page {page.page_id} within {timeout:g}s") from None
        except websockets.ConnectionClosed:
            page.closed = True
            raise HtmlAgentError(f"Page {page.page_id} disconnected") from None
        finally:
            self._pending.pop(message_id, None)
        if page.front_end:
            return _protocol_value(reply)
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
        if self.mode == "remote":
            async with self._open_lock:
                return await self._open_remote()
        if self._linux_local():
            async with self._open_lock:
                return await self._open_linux_local()
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

    async def _open_linux_local(self) -> list[str]:
        """Linux console / patched: open the inspector of each HTML field of the active form without a live page."""
        if self.field_driver is None:
            raise HtmlAgentError("The HTML page bridge cannot reach the test client's form fields")
        live = {p.field_name for p in self.pages.values() if not p.closed}
        opened: list[str] = []
        for name in await self.field_driver.html_fields():
            key = self._field_keys.setdefault(name, len(self._field_keys) + 1)
            if name in live or self._attempts.get(key, 0) >= self.MAX_ATTEMPTS:
                continue
            page = await self._connect_linux(name, key)
            if page is not None:
                opened.append(page.page_id)
        return opened

    async def _connect_linux(self, name: str, key: int) -> InspectorPage | None:
        self._attempts[key] = self._attempts.get(key, 0) + 1
        started = time.monotonic()
        timings: dict[str, Any] = {"field": name, "mode": self.mode}
        self.last_connect = timings
        patched = True  # console is refused on Linux (resolve_mode)
        token, future = self.expect(key, webkit_patch.INSPECTOR_PATH)
        try:
            if self.overlay_dir is not None:
                await asyncio.to_thread(write_inspector_strings, self.overlay_dir, self.port, token)
            await self.field_driver.focus(name)
            await asyncio.to_thread(_x_keys, self.desktop, self.client_pid, LINUX_INSPECTOR_KEYS)
            page = await asyncio.wait_for(future, PATCHED_CONNECT_SECONDS)
            for _ in range(60):  # the page target arrives right after the front end connects
                if page.page_target or page.closed:
                    break
                await asyncio.sleep(0.05)
            timings["connected_seconds"] = round(time.monotonic() - started, 2)
        except (HtmlAgentError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
            self.last_error = str(exc) or f"The page of HTML field {name} did not connect within the timeout"
            LOGGER.warning("%s", self.last_error)
            return None
        finally:
            self._tokens.pop(token, None)
            if patched and self.overlay_dir is not None:
                await asyncio.to_thread(write_inspector_strings, self.overlay_dir)
        page.field_name = name
        self._attempts.pop(key, None)
        with suppress(HtmlAgentError):
            await self.refresh(page)
        return page

    async def _front_end(self, view: int) -> int:
        """The docked inspector front end of ``view``, opening the inspector when needed."""
        views = set(await self._views())
        front_end = self._front_ends.get(view)
        if front_end in views:
            return front_end
        inspectors = set(await asyncio.to_thread(find_inspector_windows, self.desktop, self.client_pid))
        await asyncio.to_thread(_run_on_client_desktop, self.desktop, lambda: _press_inspector_hotkey(view))
        deadline = time.monotonic() + INSPECTOR_OPEN_SECONDS
        new: set[int] = set()
        loaded = False
        while not (new and loaded) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            new = new or set(await self._views()) - views
            titles = await asyncio.to_thread(find_inspector_windows, self.desktop, self.client_pid)
            loaded = any(title for hwnd, title in titles.items() if hwnd not in inspectors)
        # Whatever appeared is a front end and must never be inspected itself.
        self._known_front_ends.update(new)
        if not new:
            raise HtmlAgentError(f"The inspector of WebKit view {view} did not open")
        front_end = min(new)
        self._front_ends[view] = front_end
        return front_end

    async def _connect_patched(self, view: int, timings: dict[str, Any]) -> InspectorPage | None:
        """Open the inspector of a patched platform: its front end connects by itself."""
        token, future = self.expect(view, webkit_patch.INSPECTOR_PATH)
        started = time.monotonic()
        holder = webkit_patch.opening_inspector(self.client_bin, self.port, token)
        await asyncio.to_thread(holder.__enter__)
        try:
            await self._front_end(view)
            page = await asyncio.wait_for(future, PATCHED_CONNECT_SECONDS)
        except (HtmlAgentError, TimeoutError) as exc:
            timings["patched_error"] = str(exc) or "the patched inspector did not connect"
            return None
        finally:
            await asyncio.to_thread(holder.__exit__, None, None, None)
            self._tokens.pop(token, None)
        timings["mode"] = "patched"
        timings["connected_seconds"] = round(time.monotonic() - started, 2)
        return page

    async def _connect(self, view: int) -> InspectorPage | None:
        self._attempts[view] = self._attempts.get(view, 0) + 1
        started = time.monotonic()
        timings: dict[str, Any] = {"view": view, "typed": 0}
        self.last_connect = timings
        if self.mode == "patched":
            if view in self._front_ends and self._front_ends[view] in set(await self._views()):
                self.last_error = (f"The inspector of WebKit view {view} is open without the Answer42 bridge "
                                   "(it was opened before or its bridge closed): close it and call again")
                return None
            patched = await self._connect_patched(view, timings)
            if patched is None:
                self.last_error = str(timings.get("patched_error") or "the patched inspector did not connect")
                LOGGER.warning("%s", self.last_error)
                return None
            self._attempts.pop(view, None)
            with suppress(HtmlAgentError):
                await self.refresh(patched)
            return patched
        token, future = self.expect(view)
        try:
            front_end = await self._front_end(view)
            timings["front_end_seconds"] = round(time.monotonic() - started, 2)
            # A front end that is still loading drops the click and the keys: type again until the page connects.
            deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
            while not future.done() and time.monotonic() < deadline:
                await asyncio.to_thread(_type_into_console, self.desktop, front_end, loader_js(self.port, token))
                timings["typed"] += 1
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(future), RETYPE_SECONDS)
            page = await asyncio.wait_for(future, 0.1)
            timings["connected_seconds"] = round(time.monotonic() - started, 2)
        except (HtmlAgentError, TimeoutError) as exc:
            self.last_error = str(exc) or f"The page of WebKit view {view} did not connect within {CONNECT_TIMEOUT_SECONDS:g}s"
            LOGGER.warning("%s", self.last_error)
            return None
        finally:
            self._tokens.pop(token, None)
        self._attempts.pop(view, None)
        timings["mode"] = "console"
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

        Linux and a patched Windows inspector (webkit_patch) read the inspector's
        Console domain, which keeps messages since the page loaded.  The console
        loader hooks errors only once it is connected, so earlier load-time
        errors are missing there; the script
        check (every script compiled with ``new Function``) still reports
        syntax this WebKit does not support, such as ``?.``.
        """
        page = await self.page(selector)
        result: dict[str, Any] = {"page": page.page_id, "url": page.url}
        if page.front_end or (page.target is not None and self._remote is not None):
            log = page.console_log if page.front_end or self._remote is None else self._remote.console.get(page.target, [])
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
