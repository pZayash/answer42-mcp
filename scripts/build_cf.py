#!/usr/bin/env python3
"""Cross-platform builder for Answer42 test configurations (.cf).

Usage:
  python scripts/build_cf.py [SRC_DIR] [OUT_CF]

Environment:
  PLATFORM_DIR / ONEC_PLATFORM_DIR - 1C platform bin directory override
  DB_PATH                         - temporary file infobase directory
  BUILD_CF_LOG                    - designer log path
  DISPLAY                         - used on Linux/X11 only
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_1c import os_support  # noqa: E402
from mcp_1c.platform import find_platform_dir, platform_version_from_dir, require_websocket_support  # noqa: E402


def _platform_dir() -> Path:
    override = os.getenv("PLATFORM_DIR") or os.getenv("ONEC_PLATFORM_DIR")
    if override:
        return Path(override)
    return find_platform_dir(os.getenv("ONEC_VERSION") or None)


def _onec_designer_executable(platform_dir: Path) -> Path:
    """Return the thick-client executable required for DESIGNER mode.

    1cv8c.exe is the thin client and does not include Конфигуратор/DESIGNER.
    Building .cf from XML sources therefore requires 1cv8.exe (Windows) or
    1cv8 (Linux) from the thick client / Configurator installation.
    """
    candidate = platform_dir / os_support.executable_name("1cv8")
    if candidate.exists():
        return candidate
    raise SystemExit(
        "1C Designer executable not found: "
        f"{candidate}. Install 1C thick client / Конфигуратор for this "
        "platform version, or provide a prebuilt .cf artifact. "
        "1cv8c.exe is a thin client and cannot build .cf from XML sources."
    )


def _run(argv: list[str], *, env: dict[str, str], timeout: int = 300) -> None:
    print("+", " ".join(map(str, argv)), flush=True)
    completed = subprocess.run(argv, cwd=ROOT, env=env, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise SystemExit(f"Command failed rc={completed.returncode}: {' '.join(map(str, argv))}")


def main(argv: list[str]) -> int:
    src_dir = Path(argv[1]) if len(argv) > 1 else Path("src/cf")
    out_cf = Path(argv[2]) if len(argv) > 2 else Path("build/MCPTestManager.cf")
    db_path = Path(os.getenv("DB_PATH") or str(os_support.default_data_dir() / "ib-build-cf"))
    log_path = Path(os.getenv("BUILD_CF_LOG") or "build/build_cf.log")

    platform_dir = _platform_dir()
    version_name = platform_version_from_dir(platform_dir)
    require_websocket_support(version_name)
    onec = _onec_designer_executable(platform_dir)
    if not src_dir.exists():
        raise SystemExit(f"Source directory not found: {src_dir}")

    if db_path.exists():
        shutil.rmtree(db_path, ignore_errors=True)
    out_cf.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if not os_support.IS_WINDOWS and "DISPLAY" not in env:
        env["DISPLAY"] = ":99"

    _run([str(onec), "CREATEINFOBASE", f"File={db_path}", "/DisableStartupDialogs"], env=env, timeout=180)
    _run([
        str(onec), "DESIGNER", "/F", str(db_path),
        "/LoadConfigFromFiles", str(src_dir),
        "/Out", str(log_path),
        "/DisableStartupDialogs",
    ], env=env, timeout=600)
    _run([
        str(onec), "DESIGNER", "/F", str(db_path),
        "/UpdateDBCfg",
        "/Out", str(log_path),
        "/DisableStartupDialogs",
    ], env=env, timeout=600)
    _run([
        str(onec), "DESIGNER", "/F", str(db_path),
        "/DumpCfg", str(out_cf),
        "/Out", str(log_path),
        "/DisableStartupDialogs",
    ], env=env, timeout=600)
    print(out_cf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
