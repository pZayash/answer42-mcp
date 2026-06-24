"""1С platform discovery helpers.

* detect_version_from_url - fetch the public login page and parse VERSION.
* find_platform_dir - locate the requested (or latest) platform dir.
* require_websocket_support - reject unsupported 1C platform versions.
"""

from __future__ import annotations

import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def find_free_display(start: int = 99) -> str:
    """Find the first free Xvfb display starting from `start`."""
    for n in range(start, 200):
        path = f"/tmp/.X11-unix/X{n}"
        if not Path(path).exists():
            return f":{n}"
    raise RuntimeError("No free X display found")


def find_free_port(start: int = 8765, end: int | None = None) -> int:
    """Find a free TCP port in the range `start..start+500` (or `start..end`)."""
    end = end or start + 500
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}..{end}")


def detect_version_from_url(url: str) -> Optional[str]:
    """Fetch a public base_url and extract VERSION from the HTML.

    If the page is behind Basic Auth or simply unavailable, this is not an
    error; we return None and let the caller pick a default platform version.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'var\s+VERSION\s*=\s*"(\d+\.\d+\.\d+\.\d+)"', html)
            if m:
                return m.group(1)
    except (urllib.error.URLError, OSError):
        pass
    except Exception:
        pass
    return None


def find_platform_dir(version: Optional[str] = None) -> Path:
    """Locate the platform directory for a given version.

    If `version` is omitted the maximum available version is selected.
    Searches `/opt/1cv8/x86_64`, `/opt/1cv8/x86`. No other paths are used.
    """
    candidates: list[Path] = []
    for base in [Path("/opt/1cv8/x86_64"), Path("/opt/1cv8/x86"), Path("/opt/1c/x86_64"), Path("/opt/1c/x86")]:
        if not base.exists():
            continue
        for sub in base.iterdir():
            if sub.is_dir() and (sub / "1cv8c").exists():
                candidates.append(sub)
    if not candidates:
        raise RuntimeError("No platform directories found under /opt/1cv8/x86_64 or /opt/1cv8/x86")
    if version:
        for c in candidates:
            if c.name == version:
                return c
        raise ValueError(f"Версия {version} не найдена в /opt/1cv8/x86_64 или /opt/1cv8/x86")
    # Numeric compare using the first three components to avoid treating the
    # build number as the most significant part.
    return max(
        candidates,
        key=lambda p: _version_tuple(p.name),
    )


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = version.split(".")
    nums: list[int] = []
    for p in parts[:4]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 4:
        nums.append(0)
    return (nums[0], nums[1], nums[2], nums[3])


def require_websocket_support(version: str) -> None:
    """Answer42 requires 1С:Предприятие 8.3.27+."""
    major, minor, patch, _ = _version_tuple(version)
    if (major, minor, patch) < (8, 3, 27):
        raise ValueError(f"Версия {version} не поддерживается. Требуется 1С:Предприятие 8.3.27+.")
