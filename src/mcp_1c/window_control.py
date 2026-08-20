from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)

TEST_CLIENT_WINDOW_TITLE = "Клиент тестирования (1С:Предприятие)"


@dataclass
class WindowControlResult:
    ok: bool
    method: str
    title: str = ""
    geometry: dict[str, int] | None = None
    candidates: list[dict[str, Any]] | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "title": self.title,
            "geometry": self.geometry or {},
            "candidates": self.candidates or [],
            "error": self.error,
        }


def maximize_test_client_window(
    title_contains: str = "",
    display: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Maximize/resize a 1C test-client window.

    Priority: Windows WinAPI on Windows; wmctrl -> xdotool -> python-xlib on X11.
    This function controls only window geometry; it doesn't click or type in the application.
    """
    if os.name == "nt":
        return _maximize_with_winapi(title_contains, width, height).as_dict()

    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    display = env.get("DISPLAY", "")

    for fn in (_maximize_with_wmctrl, _maximize_with_xdotool, _maximize_with_xlib):
        result = fn(title_contains=title_contains, display=display, width=width, height=height, env=env)
        if result.ok:
            return result.as_dict()
        last = result
    return last.as_dict()


def active_1c_window_geometry(title_contains: str = "", display: str | None = None) -> dict[str, int] | None:
    """Return best visible 1C window geometry for screenshots.

    This inspects OS window geometry only.  It must not be used to click,
    type, or decide whether a 1C UI command succeeded; UI automation goes
    through the 1C test-client API.
    """
    chosen = _find_windows_1c_window(title_contains) if os.name == "nt" else _find_x11_1c_window(title_contains, display)
    if not chosen:
        return None
    return chosen.get("geometry")


def _find_x11_1c_window(title_contains: str = "", display: str | None = None) -> dict[str, Any] | None:
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    try:
        tree = subprocess.run(
            ["xwininfo", "-root", "-tree"],
            env=env,
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    screen_width, screen_height = _screen_size(env.get("DISPLAY", ""), env)
    candidates: list[dict[str, Any]] = []
    for line in tree.splitlines():
        if '("1cv8c" "1cv8c")' not in line and '("1cv8" "1cv8")' not in line:
            continue
        match = re.search(r'0x[0-9a-f]+ "([^"]*)".*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
        if not match:
            continue
        title = match.group(1)
        if not _title_matches(title, title_contains):
            continue
        width, height, left, top = map(int, match.groups()[1:])
        if width < 300 or height < 150:
            continue
        capture_left = max(0, left)
        capture_top = max(0, top)
        capture_width = min(width - max(0, -left), max(0, screen_width - capture_left))
        capture_height = min(height - max(0, -top), max(0, screen_height - capture_top))
        if capture_width < 300 or capture_height < 150:
            continue
        priority = min(capture_width * capture_height // 10000, 50)
        if "1С:Предприятие" in title:
            priority += 50
        if title and title not in {"1cv8c", "1cv8", "Конфигурация"}:
            priority += 100
        candidates.append({
            "title": title,
            "priority": priority,
            "geometry": {
                "left": capture_left,
                "top": capture_top,
                "width": capture_width,
                "height": capture_height,
                "title": title,
                "xwininfo_left": left,
                "xwininfo_top": top,
                "xwininfo_width": width,
                "xwininfo_height": height,
            },
        })
    if not candidates:
        return None
    candidates.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    return candidates[0]


def _screen_size(display: str, env: dict[str, str]) -> tuple[int, int]:
    try:
        out = subprocess.check_output(["xdpyinfo", "-display", display], env=env, text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "dimensions:" in line:
                part = line.split("dimensions:", 1)[1].strip().split()[0]
                w, h = part.split("x")
                return int(w), int(h)
    except Exception:
        pass
    return 1920, 1080


def _maximize_with_wmctrl(title_contains: str, display: str, width: int | None, height: int | None, env: dict[str, str]) -> WindowControlResult:
    if not shutil.which("wmctrl"):
        return WindowControlResult(False, "wmctrl", error="wmctrl not installed")
    try:
        sw, sh = (width, height) if width and height else _screen_size(display, env)
        out = subprocess.check_output(["wmctrl", "-l"], env=env, text=True)
        candidates=[]
        chosen=None
        for line in out.splitlines():
            parts=line.split(None, 3)
            if len(parts) < 4:
                continue
            wid, _, _, title = parts
            item={"id": wid, "title": title}
            candidates.append(item)
            if _title_matches(title, title_contains):
                chosen=item
        if chosen is None and not title_contains and len(candidates) == 1:
            chosen = candidates[0]
        if chosen is None:
            return WindowControlResult(False, "wmctrl", candidates=candidates, error="window not found")
        wid=chosen["id"]
        subprocess.check_call(["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"], env=env)
        subprocess.check_call(["wmctrl", "-i", "-r", wid, "-e", f"0,0,0,{sw},{sh}"], env=env)
        subprocess.check_call(["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"], env=env)
        return WindowControlResult(True, "wmctrl", title=chosen["title"], geometry={"x":0,"y":0,"width":sw,"height":sh}, candidates=candidates)
    except Exception as e:
        return WindowControlResult(False, "wmctrl", error=str(e))


def _maximize_with_xdotool(title_contains: str, display: str, width: int | None, height: int | None, env: dict[str, str]) -> WindowControlResult:
    if not shutil.which("xdotool"):
        return WindowControlResult(False, "xdotool", error="xdotool not installed")
    try:
        sw, sh = (width, height) if width and height else _screen_size(display, env)
        pattern = title_contains or TEST_CLIENT_WINDOW_TITLE
        ids = subprocess.check_output(["xdotool", "search", "--name", pattern], env=env, text=True).split()
        if not ids and not title_contains:
            all_ids = subprocess.check_output(["xdotool", "search", "--class", "1cv8"], env=env, text=True).split()
            if len(all_ids) == 1:
                ids = all_ids
        if not ids:
            return WindowControlResult(False, "xdotool", error="window not found")
        wid=ids[-1]
        title = subprocess.check_output(["xdotool", "getwindowname", wid], env=env, text=True).strip()
        subprocess.check_call(["xdotool", "windowsize", wid, str(sw), str(sh)], env=env)
        subprocess.check_call(["xdotool", "windowmove", wid, "0", "0"], env=env)
        subprocess.check_call(["xdotool", "windowactivate", wid], env=env)
        return WindowControlResult(True, "xdotool", title=title, geometry={"x":0,"y":0,"width":sw,"height":sh})
    except Exception as e:
        return WindowControlResult(False, "xdotool", error=str(e))


def _title_matches(title: str, title_contains: str) -> bool:
    if title_contains:
        return title_contains.lower() in title.lower()
    if "Конфигурация" in title:
        return False
    return TEST_CLIENT_WINDOW_TITLE.lower() in title.lower()


def _maximize_with_xlib(title_contains: str, display: str, width: int | None, height: int | None, env: dict[str, str]) -> WindowControlResult:
    try:
        from Xlib import X
        from Xlib import display as xdisplay
        disp = xdisplay.Display(display or None)
        root = disp.screen().root
        sw = width or disp.screen().width_in_pixels
        sh = height or disp.screen().height_in_pixels
        candidates=[]
        def walk(win):
            yield win
            try:
                children = win.query_tree().children
            except Exception:
                return
            for child in children:
                yield from walk(child)
        def get_name(win):
            for atom_name in ("_NET_WM_NAME", "WM_NAME"):
                try:
                    prop=win.get_full_property(disp.intern_atom(atom_name), X.AnyPropertyType)
                    if prop and prop.value is not None:
                        value=prop.value
                        if isinstance(value, bytes):
                            return value.decode("utf-8", "ignore")
                        try:
                            return bytes(value).decode("utf-8", "ignore")
                        except Exception:
                            return str(value)
                except Exception:
                    pass
            return ""
        chosen=None
        candidate_windows=[]
        for win in walk(root):
            title=get_name(win)
            if not title:
                continue
            try:
                geo=win.get_geometry()
            except Exception:
                continue
            if geo.width < 300 or geo.height < 200:
                continue
            item={"title": title, "width": geo.width, "height": geo.height}
            candidates.append(item)
            candidate_windows.append((win, item))
            if _title_matches(title, title_contains):
                chosen=(win, item)
        if chosen is None and not title_contains and len(candidate_windows) == 1:
            chosen = candidate_windows[0]
        if chosen is None:
            return WindowControlResult(False, "xlib", candidates=candidates, error="window not found")
        win,item=chosen
        win.configure(x=0, y=0, width=sw, height=sh, border_width=0, stack_mode=X.Above)
        try:
            win.set_input_focus(X.RevertToParent, X.CurrentTime)
        except Exception:
            pass
        win.map()
        disp.sync()
        time.sleep(0.2)
        return WindowControlResult(True, "xlib", title=item["title"], geometry={"x":0,"y":0,"width":sw,"height":sh}, candidates=candidates)
    except Exception as e:
        return WindowControlResult(False, "xlib", error=str(e))


def _find_windows_1c_window(title_contains: str = "") -> dict[str, Any] | None:
    user32 = ctypes.windll.user32
    candidates: list[dict[str, Any]] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if not _title_matches(title, title_contains):
                return True
            rect = ctypes.wintypes.RECT()  # type: ignore[attr-defined]
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width < 300 or height < 150:
                return True
            priority = min(width * height // 10000, 50)
            if "1С:Предприятие" in title:
                priority += 50
            if title and title not in {"1cv8c", "1cv8", "Конфигурация"}:
                priority += 100
            candidates.append({
                "hwnd": hwnd,
                "title": title,
                "priority": priority,
                "geometry": {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height, "title": title},
            })
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(callback), 0)
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    return candidates[0]


def _maximize_with_winapi(title_contains: str, width: int | None, height: int | None) -> WindowControlResult:
    try:
        import ctypes.wintypes  # type: ignore

        user32 = ctypes.windll.user32
        chosen = _find_windows_1c_window(title_contains)
        if chosen is None:
            return WindowControlResult(False, "winapi", error="window not found")
        hwnd = int(chosen["hwnd"])
        screen_w = width or user32.GetSystemMetrics(0) or 1920
        screen_h = height or user32.GetSystemMetrics(1) or 1080
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.MoveWindow(hwnd, 0, 0, int(screen_w), int(screen_h), True)
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return WindowControlResult(
            True,
            "winapi",
            title=str(chosen.get("title") or ""),
            geometry={"x": 0, "y": 0, "width": int(screen_w), "height": int(screen_h)},
            candidates=[{"title": str(chosen.get("title") or ""), "hwnd": hwnd}],
        )
    except Exception as e:
        return WindowControlResult(False, "winapi", error=str(e))
