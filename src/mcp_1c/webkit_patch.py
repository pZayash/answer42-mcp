"""Optional (Windows): the Answer42 bridge built into the Web Inspector of an installed 1C platform.

Without it, the html_* tools type a one-line loader into the console of the
embedded Web Inspector (``html_agent``).  With it, the inspector front end of the
platform (``bin\\WebKit.resources\\WebInspectorUI\\Main.html``) loads
``Answer42Bridge.js``: when Answer42 opens an inspector, the front end connects
to the session's WebSocket by itself and relays inspector protocol messages
both ways, so nothing is typed, the bridge survives page reloads, and console
messages are available from the page load.

The bridge reads the session's port and a one-time token from
``Answer42BridgeConfig.js``, which Answer42 writes right before it opens an
inspector (under a lock, as several sessions may share a platform) and clears
right after.  An inspector opened by a person shows nothing new: the config is
empty then.

``Program Files`` is read-only for the user, so install/uninstall re-run this
module elevated (one UAC prompt for all chosen platforms); the elevated run also
lets the user modify the config file, so sessions need no elevation later.

    answer42 webkit-inspector status|install|uninstall [--version 8.5.1.1343 ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import os_support
from . import platform as onec_platform

INSPECTOR_UI = Path("WebKit.resources") / "WebInspectorUI"
BRIDGE_JS_NAME = "Answer42Bridge.js"
CONFIG_NAME = "Answer42BridgeConfig.js"
BACKUP_SUFFIX = ".answer42-original"
BRIDGE_BASE_ID = 1_000_000_000
INSPECTOR_PATH = "/answer42-inspector/"
EMPTY_CONFIG = "window.ANSWER42_BRIDGE = null;\n"
ELEVATION_TIMEOUT_SECONDS = 180
# Some hosts (agent sandboxes, services) cannot show a UAC prompt: ShellExecuteEx then never returns.
ELEVATION_PROMPT_SECONDS = 90

BRIDGE_JS = r"""// Answer42 inspector bridge (mcp_1c/webkit_patch.py): relays the inspector protocol messages of Answer42.
(function () {
  var config = window.ANSWER42_BRIDGE;
  window.ANSWER42_BRIDGE = null;
  if (!config || !config.port || !config.token || !window.InspectorFrontendAPI || !window.InspectorFrontendHost) { return; }
  var BASE_ID = __BASE_ID__;
  var ws = new WebSocket('ws://127.0.0.1:' + config.port + '__PATH__' + config.token);
  function post(obj) { if (ws.readyState === 1) { ws.send(JSON.stringify(obj)); } }
  function parse(message) {
    if (typeof message !== 'string') { return message; }
    if (message.indexOf('"id":') < 0 && message.indexOf('Console.message') < 0) { return null; }
    try { return JSON.parse(message); } catch (e) { return null; }
  }
  ['dispatchMessageAsync', 'dispatchMessage'].forEach(function (name) {
    var original = InspectorFrontendAPI[name];
    if (!original) { return; }
    InspectorFrontendAPI[name] = function (message) {
      var obj = parse(message);
      if (obj && typeof obj.id === 'number' && obj.id >= BASE_ID) { post({ type: 'message', message: obj }); return undefined; }
      if (obj && obj.method === 'Console.messageAdded') { post({ type: 'event', message: obj }); }
      return original.apply(this, arguments);
    };
  });
  ws.onopen = function () { post({ type: 'hello', version: 1 }); };
  ws.onmessage = function (event) {
    var m;
    try { m = JSON.parse(event.data); } catch (e) { return; }
    if (m.type === 'send') { InspectorFrontendHost.sendMessageToBackend(JSON.stringify(m.message)); }
  };
})();
""".replace("__BASE_ID__", str(BRIDGE_BASE_ID)).replace("__PATH__", INSPECTOR_PATH)

BRIDGE_TAGS = f'    <script src="{CONFIG_NAME}"></script>\n    <script src="{BRIDGE_JS_NAME}"></script>\n'


class WebKitPatchError(RuntimeError):
    pass


def ui_dir(bin_dir: str | Path) -> Path:
    return Path(bin_dir) / INSPECTOR_UI


def platform_status(bin_dir: str | Path) -> dict[str, Any]:
    """Whether the platform's inspector front end has the current bridge and a writable config."""
    ui = ui_dir(bin_dir)
    main_html = ui / "Main.html"
    status: dict[str, Any] = {
        "version": onec_platform.platform_version_from_dir(Path(bin_dir)),
        "bin": str(bin_dir),
        "inspector_ui": main_html.is_file(),
        "installed": False,
        "current": False,
        "config_writable": False,
    }
    if not main_html.is_file():
        return status
    status["installed"] = BRIDGE_JS_NAME in main_html.read_text(encoding="utf-8", errors="replace")
    bridge = ui / BRIDGE_JS_NAME
    status["current"] = status["installed"] and bridge.is_file() and bridge.read_text(encoding="utf-8", errors="replace") == BRIDGE_JS
    status["config_writable"] = (ui / CONFIG_NAME).is_file() and os.access(ui / CONFIG_NAME, os.W_OK)
    status["ready"] = status["current"] and status["config_writable"]
    return status


def is_ready(bin_dir: str | Path) -> bool:
    """True when sessions on this platform can use the built-in bridge."""
    try:
        return bool(platform_status(bin_dir).get("ready"))
    except OSError:
        return False


# --- install / uninstall (need write access to the platform directory) ---

def _apply_install(bin_dir: Path, grant_sid: str = "") -> None:
    ui = ui_dir(bin_dir)
    main_html = ui / "Main.html"
    if not main_html.is_file():
        raise WebKitPatchError(f"No Web Inspector front end in {bin_dir}: {main_html} is missing")
    (ui / BRIDGE_JS_NAME).write_text(BRIDGE_JS, encoding="utf-8")
    config = ui / CONFIG_NAME
    if not config.is_file():
        config.write_text(EMPTY_CONFIG, encoding="utf-8")
    text = main_html.read_text(encoding="utf-8")
    if BRIDGE_JS_NAME not in text:
        if "</head>" not in text:
            raise WebKitPatchError(f"Unexpected Web Inspector Main.html (no </head>): {main_html}")
        backup = main_html.with_name(main_html.name + BACKUP_SUFFIX)
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        main_html.write_text(text.replace("</head>", BRIDGE_TAGS + "</head>", 1), encoding="utf-8")
    if grant_sid and os_support.IS_WINDOWS:
        # Sessions rewrite the config before each inspector they open, without elevation.
        subprocess.run(["icacls", str(config), "/grant", f"*{grant_sid}:M"], capture_output=True, check=True)


def _apply_uninstall(bin_dir: Path) -> None:
    ui = ui_dir(bin_dir)
    main_html = ui / "Main.html"
    backup = main_html.with_name(main_html.name + BACKUP_SUFFIX)
    if backup.is_file():
        main_html.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink()
    elif main_html.is_file():
        main_html.write_text(main_html.read_text(encoding="utf-8").replace(BRIDGE_TAGS, ""), encoding="utf-8")
    for name in (BRIDGE_JS_NAME, CONFIG_NAME):
        (ui / name).unlink(missing_ok=True)


def _writable(directory: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".answer42-", delete=True):
            return True
    except OSError:
        return False


def current_user_sid() -> str:
    output = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout
    return output.strip().split(",")[-1].strip().strip('"')


def manual_command(action: str, bins: list[Path]) -> str:
    """The PowerShell command that does the same in a terminal started as administrator."""
    args = ["-m", "mcp_1c.webkit_patch", action]
    for bin_dir in bins:
        args += ["--version", onec_platform.platform_version_from_dir(bin_dir)]
    if action == "install":
        args += ["--grant-sid", current_user_sid()]
    return f'(PowerShell) & "{sys.executable}" ' + subprocess.list2cmdline(args)


def _run_elevated(action: str, bins: list[Path]) -> dict[str, Any]:
    """Re-run this module as administrator (a UAC prompt) and return its JSON result."""
    import ctypes
    import ctypes.wintypes as wt

    result_file = Path(tempfile.mkdtemp(prefix="answer42-webkit-")) / "result.json"
    args = ["-m", "mcp_1c.webkit_patch", "_elevated", action, "--result", str(result_file)]
    if action == "install":
        args += ["--grant-sid", current_user_sid()]
    args += [str(b) for b in bins]
    parameters = subprocess.list2cmdline(args)

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND), ("lpVerb", wt.LPCWSTR),
            ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wt.HINSTANCE), ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
            ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE),
        ]

    see_mask_nocloseprocess, see_mask_noasync, sw_hide = 0x40, 0x100, 0
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess | see_mask_noasync
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = parameters
    info.lpDirectory = str(Path(__file__).resolve().parents[1])
    info.nShow = sw_hide
    outcome: dict[str, int] = {}
    done = threading.Event()

    def launch() -> None:
        # ShellExecuteEx needs COM, and NOASYNC in a thread without a message loop.
        ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)  # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
        outcome["ok"] = int(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)))
        outcome["error"] = ctypes.GetLastError()
        done.set()

    threading.Thread(target=launch, name="answer42-uac", daemon=True).start()
    if not done.wait(ELEVATION_PROMPT_SECONDS):
        raise WebKitPatchError(
            f"Windows did not show or answer the administrator rights request (UAC) within {ELEVATION_PROMPT_SECONDS}s; "
            "this host may be unable to ask for elevation.  Run in a terminal started as administrator: "
            + manual_command(action, bins)
        )
    if not outcome.get("ok"):
        if outcome.get("error") == 1223:  # ERROR_CANCELLED
            raise WebKitPatchError("The administrator rights request (UAC) was declined; nothing was changed")
        raise WebKitPatchError(f"Cannot start the elevated helper (error {outcome.get('error')}); run as administrator: "
                               + manual_command(action, bins))
    kernel32 = ctypes.windll.kernel32
    try:
        if kernel32.WaitForSingleObject(info.hProcess, ELEVATION_TIMEOUT_SECONDS * 1000) != 0:
            raise WebKitPatchError(f"The elevated helper did not finish within {ELEVATION_TIMEOUT_SECONDS}s")
    finally:
        kernel32.CloseHandle(info.hProcess)
    try:
        return json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WebKitPatchError(f"The elevated helper left no result ({exc})") from None
    finally:
        result_file.unlink(missing_ok=True)
        result_file.parent.rmdir()


def _apply(action: str, bins: list[Path], grant_sid: str = "") -> dict[str, Any]:
    errors: dict[str, str] = {}
    for bin_dir in bins:
        try:
            if action == "install":
                _apply_install(bin_dir, grant_sid)
            else:
                _apply_uninstall(bin_dir)
        except (OSError, subprocess.CalledProcessError, WebKitPatchError) as exc:
            errors[str(bin_dir)] = str(exc)
    return {"errors": errors}


def change(action: str, bins: list[Path], grant_sid: str = "") -> dict[str, Any]:
    """Install or remove the bridge on the given platforms, elevating once for those that need it.

    ``grant_sid``: the account allowed to rewrite the config (default: the current
    one; give the developer's SID when running elevated as another administrator).
    """
    if action not in {"install", "uninstall"}:
        raise WebKitPatchError(f"Unknown action {action!r}: use install or uninstall")
    if not os_support.IS_WINDOWS:
        raise WebKitPatchError("The Web Inspector bridge is for the Windows 1C client; Linux clients need no patch")
    direct = [b for b in bins if _writable(ui_dir(b))]
    elevated = [b for b in bins if b not in direct]
    errors: dict[str, str] = {}
    if direct:
        sid = (grant_sid or current_user_sid()) if action == "install" else ""
        errors.update(_apply(action, direct, sid)["errors"])
    if elevated:
        errors.update(_run_elevated(action, elevated).get("errors", {}))
    return {
        "action": action,
        "elevated": bool(elevated),
        "platforms": [platform_status(b) for b in bins],
        **({"errors": errors} if errors else {}),
    }


def select_platforms(versions: list[str] | None = None) -> list[Path]:
    """Installed platforms that have a Web Inspector front end, optionally only the given versions."""
    bins = [b for b in onec_platform.installed_platform_dirs() if (ui_dir(b) / "Main.html").is_file()]
    if versions:
        wanted = set(versions)
        bins = [b for b in bins if onec_platform.platform_version_from_dir(b) in wanted]
        missing = wanted - {onec_platform.platform_version_from_dir(b) for b in bins}
        if missing:
            raise WebKitPatchError(f"No installed 1C platform with a Web Inspector front end: {', '.join(sorted(missing))}")
    return sorted(bins, key=lambda b: onec_platform.platform_version_from_dir(b))


# --- per session: the config the bridge reads when an inspector opens ---

@contextmanager
def opening_inspector(bin_dir: str | Path, port: int, token: str) -> Iterator[None]:
    """Hold the platform's config for one inspector: port and token in, cleared afterwards."""
    config = ui_dir(bin_dir) / CONFIG_NAME
    key = hashlib.sha1(str(Path(bin_dir).resolve()).lower().encode()).hexdigest()[:12]
    lock = os_support.runtime_path("ONEC_MCP_WEBKIT_LOCK_DIR", "webkit-inspector") / f"{key}.lock"
    with os_support.file_lock(lock):
        config.write_text(f"window.ANSWER42_BRIDGE = {json.dumps({'port': int(port), 'token': token})};\n", encoding="utf-8")
        try:
            yield
        finally:
            config.write_text(EMPTY_CONFIG, encoding="utf-8")


# --- command line ---

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_elevated":
        parser = argparse.ArgumentParser(prog="answer42 webkit-inspector _elevated")
        parser.add_argument("action", choices=["install", "uninstall"])
        parser.add_argument("--result", required=True)
        parser.add_argument("--grant-sid", default="")
        parser.add_argument("bins", nargs="+")
        args = parser.parse_args(argv[1:])
        result = _apply(args.action, [Path(b) for b in args.bins], args.grant_sid)
        Path(args.result).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0
    parser = argparse.ArgumentParser(
        prog="answer42 webkit-inspector",
        description="Build the Answer42 bridge into the Web Inspector of installed 1C platforms (Windows). "
        "install/uninstall ask for administrator rights (UAC) when the platform directory is read-only.",
    )
    parser.add_argument("action", choices=["status", "install", "uninstall"])
    parser.add_argument("--version", action="append", default=[], help="platform version; repeat for several (default: all installed)")
    parser.add_argument("--grant-sid", default="", help="install: account (SID) allowed to rewrite the session config; default: the current one")
    args = parser.parse_args(argv)
    try:
        bins = select_platforms(args.version)
        if args.action == "status":
            result: dict[str, Any] = {"platforms": [platform_status(b) for b in bins]}
        else:
            result = change(args.action, bins, args.grant_sid)
    except WebKitPatchError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
