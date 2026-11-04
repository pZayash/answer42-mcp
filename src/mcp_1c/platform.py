"""1С platform discovery helpers.

* detect_version_from_url - fetch the public login page and parse VERSION.
* find_platform_dir - locate the requested (or latest) platform dir.
* require_websocket_support - reject unsupported 1C platform versions.
"""

from __future__ import annotations

import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import os_support


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


_LOCALE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}$")


def normalize_infobase_url(url: str) -> str:
    """Return a canonical web infobase URL: no language suffix, trailing slash."""
    value = str(url or "").strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        return value
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and _LOCALE_RE.match(segments[-1]):
        segments.pop()
    path = "/" + "/".join(segments) + "/" if segments else "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def detect_version_from_url(url: str) -> str | None:
    """Fetch a public base_url and extract VERSION from the HTML.

    If the page is behind Basic Auth or simply unavailable, this is not an
    error; we return None and let the caller pick a default platform version.
    """
    try:
        with urllib.request.urlopen(normalize_infobase_url(url), timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'var\s+VERSION\s*=\s*"(\d+\.\d+\.\d+\.\d+)"', html)
            if m:
                return m.group(1)
    except (urllib.error.URLError, OSError):
        pass
    except Exception:
        pass
    return None


def _missing_required_tools(path: Path) -> list[str]:
    return [
        tool for tool in ("1cv8c", "ibcmd")
        if not (path / os_support.executable_name(tool)).exists()
    ]


def _is_platform_candidate(path: Path) -> bool:
    return not _missing_required_tools(path)


def _platform_component_score(path: Path) -> tuple[int, int]:
    """Prefer fuller installations when the same version exists in several roots."""
    return (
        1 if (path / os_support.executable_name("ibsrv")).exists() else 0,
        1 if (path / os_support.executable_name("1cv8")).exists() else 0,
    )


def installed_platform_dirs() -> list[Path]:
    """Platform directories (with 1cv8c and ibcmd) in the standard installation locations."""
    candidates: list[Path] = []
    if os_support.IS_WINDOWS:
        bases = []
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.getenv(env_name)
            if root:
                bases.append(Path(root) / "1cv8")
        for base in bases:
            if not base.exists():
                continue
            for sub in base.iterdir():
                bin_dir = sub / "bin"
                if bin_dir.is_dir() and _is_platform_candidate(bin_dir):
                    candidates.append(bin_dir)
                elif sub.is_dir() and _is_platform_candidate(sub):
                    candidates.append(sub)
    else:
        for base in [Path("/opt/1cv8/x86_64"), Path("/opt/1cv8/x86"), Path("/opt/1c/x86_64"), Path("/opt/1c/x86")]:
            if not base.exists():
                continue
            for sub in base.iterdir():
                if sub.is_dir() and _is_platform_candidate(sub):
                    candidates.append(sub)
    return candidates


def find_platform_dir(version: str | None = None) -> Path:
    """Locate the platform directory for a given version.

    If `version` is omitted the maximum available version is selected.
    Searches standard Linux and Windows installation locations. The
    ``ONEC_PLATFORM_DIR`` environment variable can point directly to the
    platform ``bin`` directory and takes precedence.
    """
    override = os.getenv("ONEC_PLATFORM_DIR")
    if override:
        path = Path(override)
        missing = _missing_required_tools(path)
        if not missing:
            return path
        raise ValueError(f"ONEC_PLATFORM_DIR does not contain required 1C tools {missing}: {path}")

    candidates = installed_platform_dirs()
    if not candidates:
        raise RuntimeError(
            "No suitable 1C platform directories found. "
            "Answer42 requires 1cv8c and ibcmd in the platform bin directory. "
            "Set ONEC_PLATFORM_DIR to a full platform installation."
        )
    if version:
        matching = [c for c in candidates if platform_version_from_dir(c) == version]
        if matching:
            return max(matching, key=_platform_component_score)
        raise ValueError(f"Версия {version} не найдена среди установленных платформ 1С")
    # Numeric compare using the first three components to avoid treating the
    # build number as the most significant part. Windows candidates usually
    # point to .../<version>/bin, so the version is in the parent directory.
    return max(
        candidates,
        key=lambda p: (_version_tuple(platform_version_from_dir(p)), _platform_component_score(p)),
    )


def platform_version_from_dir(platform_dir: Path) -> str:
    """Return the 1C version represented by a platform directory.

    Linux installs typically use /opt/1cv8/x86_64/<version>/1cv8c, while
    Windows installs use C:\\Program Files\\1cv8\\<version>\\bin\\1cv8c.exe.
    For Windows bin directories, the version is therefore the parent name.
    """
    path = Path(platform_dir)
    if path.name.lower() == "bin" and path.parent.name:
        return path.parent.name
    return path.name


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
