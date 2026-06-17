from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any


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

    Priority: wmctrl -> xdotool -> python-xlib fallback. This function controls only
    window geometry; it doesn't click or type in the application.
    """
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
        pattern = title_contains or ".*"
        ids = subprocess.check_output(["xdotool", "search", "--name", pattern], env=env, text=True).split()
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
    if title_contains and title_contains.lower() not in title.lower():
        return False
    if "Конфигурация" in title:
        return False
    return any(s in title for s in ["DEV-AREA", "Начальная", "Приложения", "1С:Предприятие", "ЭмФешн", "Время создания"])


def _maximize_with_xlib(title_contains: str, display: str, width: int | None, height: int | None, env: dict[str, str]) -> WindowControlResult:
    try:
        from Xlib import X, display as xdisplay
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
            if _title_matches(title, title_contains):
                chosen=(win, item)
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
