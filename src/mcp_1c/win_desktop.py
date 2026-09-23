"""Hidden Windows desktops for background 1C test-manager/test-client runs.

This is the Windows counterpart of the Linux Xvfb displays.  A separate
desktop object (``CreateDesktopW``) is created inside the interactive window
station ``WinSta0``; 1C processes started with ``STARTUPINFO.lpDesktop``
pointing at it render their windows there, so nothing appears on the user's
current desktop and focus is never stolen.

The desktop is still part of the user's logon session, so the MCP server must
run in an interactive session (not a service in session 0).  Screenshots use
``PrintWindow(PW_RENDERFULLCONTENT)`` from a helper thread attached to the
desktop, because ``BitBlt`` of a desktop that is not the input desktop yields
black frames.

The module imports ctypes WinAPI bindings lazily so that importing Answer42 on
POSIX does not fail.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import subprocess
import threading
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

LOGGER = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

DESKTOP_MODE_ENV = "ONEC_MCP_WINDOWS_DESKTOP"
DESKTOP_MODE_CURRENT = "current"
DESKTOP_MODE_HIDDEN = "hidden"
DESKTOP_PREFIX = "Answer42-"
WINDOW_STATION = "WinSta0"

_GENERIC_ALL = 0x10000000
_STARTF_USESTDHANDLES = 0x00000100
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_PW_RENDERFULLCONTENT = 0x00000002
_SW_RESTORE = 9
_SW_MAXIMIZE = 3

_T = TypeVar("_T")

_DESKTOPS: dict[str, int] = {}
_DESKTOPS_LOCK = threading.Lock()
_api: Any = None


def desktop_mode() -> str:
    """Return the configured Windows desktop mode: ``current`` or ``hidden``."""
    value = (os.getenv(DESKTOP_MODE_ENV) or "").strip().lower()
    if value in {"hidden", "background", "1", "true", "yes", "on"}:
        return DESKTOP_MODE_HIDDEN
    return DESKTOP_MODE_CURRENT


def hidden_desktop_enabled() -> bool:
    return IS_WINDOWS and desktop_mode() == DESKTOP_MODE_HIDDEN


def session_desktop_name(session_key: str, role: str) -> str:
    """Build a stable desktop name; desktop names must not contain backslashes."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{session_key}-{role}").strip("_") or role
    return f"{DESKTOP_PREFIX}{safe}"[:200]


def is_hidden_desktop_name(value: str) -> bool:
    return bool(IS_WINDOWS and value and value.startswith(DESKTOP_PREFIX))


def lpdesktop(name: str) -> str:
    return f"{WINDOW_STATION}\\{name}"


def _winapi() -> Any:
    """Lazily bind the WinAPI functions used here."""
    global _api
    if _api is not None:
        return _api
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
            ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
            ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD),
        ]

    user32.CreateDesktopW.restype = wt.HANDLE
    user32.CreateDesktopW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    user32.OpenDesktopW.restype = wt.HANDLE
    user32.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]
    user32.CloseDesktop.argtypes = [wt.HANDLE]
    user32.SetThreadDesktop.argtypes = [wt.HANDLE]
    user32.EnumDesktopWindows.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPARAM]
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    user32.GetDC.restype = wt.HDC
    user32.GetDC.argtypes = [wt.HWND]
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
    kernel32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
        ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]

    class _Api:
        pass

    api = _Api()
    api.ctypes = ctypes
    api.wt = wt
    api.user32 = user32
    api.kernel32 = kernel32
    api.gdi32 = gdi32
    api.STARTUPINFOW = STARTUPINFOW
    api.PROCESS_INFORMATION = PROCESS_INFORMATION
    api.BITMAPINFOHEADER = BITMAPINFOHEADER
    api.EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _api = api
    return api


def _last_error(api: Any, what: str) -> OSError:
    err = api.ctypes.get_last_error()
    return OSError(err, f"{what} failed: {api.ctypes.FormatError(err).strip()}")


def ensure_desktop(name: str) -> None:
    """Create (or open) the named desktop and keep a handle until release_desktop()."""
    if not IS_WINDOWS:
        raise RuntimeError("Hidden desktops are only available on Windows")
    api = _winapi()
    with _DESKTOPS_LOCK:
        if name in _DESKTOPS:
            return
        handle = api.user32.CreateDesktopW(name, None, None, 0, _GENERIC_ALL, None)
        if not handle:
            raise _last_error(api, f"CreateDesktopW({name!r})")
        _DESKTOPS[name] = int(handle)
    LOGGER.info("Hidden desktop ready: %s", lpdesktop(name))


def release_desktop(name: str) -> None:
    """Close our handle; Windows destroys the desktop after its last process exits."""
    if not name or not IS_WINDOWS:
        return
    with _DESKTOPS_LOCK:
        handle = _DESKTOPS.pop(name, None)
    if handle:
        _winapi().user32.CloseDesktop(handle)
        LOGGER.info("Released hidden desktop %s", name)


def _open_desktop(name: str) -> int:
    api = _winapi()
    handle = api.user32.OpenDesktopW(name, 0, False, _GENERIC_ALL)
    if not handle:
        raise _last_error(api, f"OpenDesktopW({name!r})")
    return int(handle)


def run_on_desktop(name: str, fn: Callable[[], _T], timeout: float = 30.0) -> _T:
    """Run ``fn`` in a helper thread attached to the named desktop.

    SetThreadDesktop only succeeds for a thread without windows or hooks, so
    a fresh short-lived thread is used for each call.
    """
    api = _winapi()
    box: dict[str, Any] = {}

    def worker() -> None:
        handle = 0
        try:
            handle = _open_desktop(name)
            if not api.user32.SetThreadDesktop(handle):
                raise _last_error(api, f"SetThreadDesktop({name!r})")
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc
        finally:
            if handle:
                api.user32.CloseDesktop(handle)

    thread = threading.Thread(target=worker, name=f"answer42-desktop-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"Operation on desktop {name!r} did not finish within {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def list_windows(name: str) -> list[dict[str, Any]]:
    """Return visible top-level windows of the named desktop."""
    api = _winapi()
    user32 = api.user32
    wt = api.wt
    ctypes = api.ctypes
    windows: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            rect = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append({
                "hwnd": int(hwnd),
                "title": buf.value,
                "pid": int(pid.value),
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
            })
        except Exception:
            pass
        return True

    handle = _open_desktop(name)
    try:
        user32.EnumDesktopWindows(handle, api.EnumWindowsProc(callback), 0)
    finally:
        user32.CloseDesktop(handle)
    return windows


def _png_bytes(width: int, height: int, bgra: bytes) -> bytes:
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rows.append(0)
        rows.extend(rgb)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def capture_window_png(name: str, hwnd: int, width: int, height: int, path: Path | str) -> Path:
    """Render a window of the named desktop into a PNG via PrintWindow."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid window size {width}x{height}")
    api = _winapi()
    output_path = Path(path)

    def capture() -> bytes:
        user32, gdi32, ctypes = api.user32, api.gdi32, api.ctypes
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        previous = gdi32.SelectObject(mem_dc, bitmap)
        try:
            if not user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT):
                raise _last_error(api, "PrintWindow")
            header = api.BITMAPINFOHEADER(ctypes.sizeof(api.BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                raise _last_error(api, "GetDIBits")
            return buffer.raw
        finally:
            gdi32.SelectObject(mem_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    bgra = run_on_desktop(name, capture)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_png_bytes(width, height, bgra))
    return output_path


def maximize_window(name: str, hwnd: int, width: int, height: int) -> None:
    """Resize and maximize a window on the named desktop (no foreground change)."""
    api = _winapi()

    def apply() -> None:
        api.user32.ShowWindow(hwnd, _SW_RESTORE)
        api.user32.MoveWindow(hwnd, 0, 0, int(width), int(height), True)
        api.user32.ShowWindow(hwnd, _SW_MAXIMIZE)

    run_on_desktop(name, apply)


def _environment_block(env: dict[str, str] | None) -> Any:
    if env is None:
        return None
    items = sorted(((str(k), str(v)) for k, v in env.items()), key=lambda kv: kv[0].upper())
    block = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return _winapi().ctypes.create_unicode_buffer(block, len(block))


class DesktopPopen(subprocess.Popen):
    """``subprocess.Popen`` that starts the child on a specific Windows desktop.

    CPython's ``STARTUPINFO`` does not expose ``lpDesktop``, so only process
    creation is replaced; pipes, ``poll()``, ``wait()`` and ``kill()`` keep the
    standard implementation.
    """

    def __init__(self, *args: Any, desktop: str, **kwargs: Any) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("DesktopPopen is only available on Windows")
        self._answer42_desktop = desktop
        super().__init__(*args, **kwargs)

    def _execute_child(  # type: ignore[override]
        self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, startupinfo,
        creationflags, shell, p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite, *unused,
    ) -> None:
        api = _winapi()
        ctypes = api.ctypes
        if shell:
            raise ValueError("DesktopPopen does not support shell=True")
        command_line = args if isinstance(args, str) else subprocess.list2cmdline(args)
        si = api.STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = lpdesktop(self._answer42_desktop)
        use_std_handles = -1 not in (p2cread, c2pwrite, errwrite)
        if use_std_handles:
            si.dwFlags |= _STARTF_USESTDHANDLES
            si.hStdInput = int(p2cread)
            si.hStdOutput = int(c2pwrite)
            si.hStdError = int(errwrite)
        pi = api.PROCESS_INFORMATION()
        env_block = _environment_block(env)
        flags = int(creationflags) | (_CREATE_UNICODE_ENVIRONMENT if env_block is not None else 0)
        cmd_buffer = ctypes.create_unicode_buffer(command_line)
        try:
            ok = api.kernel32.CreateProcessW(
                executable, cmd_buffer, None, None, bool(use_std_handles), flags,
                env_block, os.fspath(cwd) if cwd is not None else None,
                ctypes.byref(si), ctypes.byref(pi),
            )
            if not ok:
                raise _last_error(api, "CreateProcessW")
        finally:
            # Same as CPython: drop the parent's copies of child-only handles.
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
        api.kernel32.CloseHandle(pi.hThread)
        self._child_created = True
        self._handle = subprocess.Handle(pi.hProcess)
        self.pid = int(pi.dwProcessId)
