import os
import subprocess
import sys
import time

import pytest

from mcp_1c import runtime, win_desktop, window_control


def test_desktop_mode_defaults_to_current(monkeypatch):
    monkeypatch.delenv(win_desktop.DESKTOP_MODE_ENV, raising=False)
    assert win_desktop.desktop_mode() == "current"
    monkeypatch.setenv(win_desktop.DESKTOP_MODE_ENV, "Hidden")
    assert win_desktop.desktop_mode() == "hidden"
    monkeypatch.setenv(win_desktop.DESKTOP_MODE_ENV, "current")
    assert win_desktop.desktop_mode() == "current"


def test_session_desktop_name_has_no_backslashes():
    name = win_desktop.session_desktop_name(r"scope\with spaces-сессия", "client")
    assert name.startswith(win_desktop.DESKTOP_PREFIX)
    assert "\\" not in name and " " not in name
    assert name.endswith("-client")


def test_session_displays_windows_current_desktop(monkeypatch):
    monkeypatch.setattr(runtime.os_support, "IS_WINDOWS", True)
    monkeypatch.setattr(win_desktop, "IS_WINDOWS", True)
    monkeypatch.delenv(win_desktop.DESKTOP_MODE_ENV, raising=False)
    assert runtime.session_displays(3, session_key="s") == ("", "", False)


def test_session_displays_windows_hidden_desktops(monkeypatch):
    monkeypatch.setattr(runtime.os_support, "IS_WINDOWS", True)
    monkeypatch.setattr(win_desktop, "IS_WINDOWS", True)
    monkeypatch.setenv(win_desktop.DESKTOP_MODE_ENV, "hidden")
    manager, client, starts_xvfb = runtime.session_displays(3, session_key="scope-sid")
    assert (manager, client, starts_xvfb) == ("Answer42-scope-sid-manager", "Answer42-scope-sid-client", False)


def test_popen_on_display_uses_desktop_popen_for_hidden_desktop(monkeypatch):
    calls = {}

    class FakeDesktopPopen:
        def __init__(self, argv, *, desktop, env, **kwargs):
            calls.update(argv=argv, desktop=desktop, env=env, kwargs=kwargs)

    monkeypatch.setattr(win_desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(win_desktop, "ensure_desktop", lambda name: calls.setdefault("ensured", name))
    monkeypatch.setattr(win_desktop, "DesktopPopen", FakeDesktopPopen)
    runtime._popen_on_display(["1cv8c.exe"], "Answer42-x-client", stdin=subprocess.DEVNULL)
    assert calls["ensured"] == "Answer42-x-client"
    assert calls["desktop"] == "Answer42-x-client"
    assert "DISPLAY" not in calls["env"] or calls["env"]["DISPLAY"] == os.environ.get("DISPLAY")


def test_hidden_desktop_window_lookup_accepts_generic_caption(monkeypatch):
    monkeypatch.setattr(win_desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(win_desktop, "list_windows", lambda name: [
        {"hwnd": 1, "title": "", "pid": 5, "left": 0, "top": 0, "width": 50, "height": 50},
        {"hwnd": 2, "title": "Конфигурация", "pid": 5, "left": 10, "top": 20, "width": 1200, "height": 800},
    ])
    chosen = window_control._find_windows_1c_window("", desktop="Answer42-x-client")
    assert chosen["hwnd"] == 2
    assert chosen["geometry"]["desktop"] == "Answer42-x-client"
    assert window_control._find_windows_1c_window("Отчет", desktop="Answer42-x-client") is None


def test_png_bytes_is_valid_png():
    data = win_desktop._png_bytes(2, 1, bytes([0, 0, 255, 0, 255, 0, 0, 0]))
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in data and data.endswith(b"IEND\xaeB`\x82")


@pytest.mark.skipif(os.name != "nt", reason="Windows hidden desktops only")
def test_desktop_popen_runs_process_on_hidden_desktop(tmp_path):
    name = win_desktop.session_desktop_name(f"pytest-{os.getpid()}-{time.time_ns()}", "probe")
    win_desktop.ensure_desktop(name)
    out = tmp_path / "out.txt"
    script = (
        "import ctypes, sys\n"
        "u = ctypes.windll.user32\n"
        "h = u.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())\n"
        "buf = ctypes.create_unicode_buffer(256); n = ctypes.c_uint(0)\n"
        "u.GetUserObjectInformationW(ctypes.c_void_p(h), 2, buf, 512, ctypes.byref(n))\n"
        "print(buf.value)\n"
    )
    try:
        with out.open("wb") as handle:
            proc = win_desktop.DesktopPopen(
                [sys.executable, "-c", script], desktop=name,
                stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
            assert proc.wait(timeout=30) == 0
        assert out.read_text().strip() == name
    finally:
        win_desktop.release_desktop(name)
