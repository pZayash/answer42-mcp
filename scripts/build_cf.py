#!/usr/bin/env python3
"""Cross-platform builder for Answer42 test configurations (.cf).

Usage:
  python scripts/build_cf.py [SRC_DIR] [OUT_CF]

Build order:
  1. Prefer ibcmd: create a temporary file infobase, then run
     ``ibcmd infobase config import`` and ``ibcmd config save``.
  2. If ibcmd is not installed, fall back to Configurator/DESIGNER via 1cv8.

Environment:
  PLATFORM_DIR / ONEC_PLATFORM_DIR - 1C platform bin directory override
  DB_PATH                         - temporary file infobase directory for DESIGNER fallback
  BUILD_CF_LOG                    - build log path
  DISPLAY                         - used by DESIGNER fallback on Linux/X11 only
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_1c import os_support
from mcp_1c.platform import find_platform_dir, platform_version_from_dir, require_websocket_support


def _platform_dir() -> Path:
    override = os.getenv("PLATFORM_DIR") or os.getenv("ONEC_PLATFORM_DIR")
    if override:
        return Path(override)
    return find_platform_dir(os.getenv("ONEC_VERSION") or None)


def _ibcmd_executable(platform_dir: Path) -> Path | None:
    """Return ibcmd when available in the platform bin directory."""
    candidate = platform_dir / os_support.executable_name("ibcmd")
    return candidate if candidate.exists() else None


def _onec_designer_executable(platform_dir: Path) -> Path:
    """Return the thick-client executable required for DESIGNER fallback.

    1cv8c.exe is the thin client and does not include Конфигуратор/DESIGNER.
    The fallback therefore requires 1cv8.exe (Windows) or 1cv8 (Linux).
    """
    candidate = platform_dir / os_support.executable_name("1cv8")
    if candidate.exists():
        return candidate
    raise SystemExit(
        "Cannot build .cf: neither ibcmd nor 1C Designer executable was found. "
        f"Missing ibcmd and {candidate}. Install server administration tools "
        "(ibcmd) or thick client / Конфигуратор for this platform version, "
        "or provide a prebuilt .cf artifact. 1cv8c.exe is a thin client and "
        "cannot build .cf from XML sources."
    )


def _run(argv: list[str], *, env: dict[str, str], timeout: int = 300) -> None:
    print("+", " ".join(map(str, argv)), flush=True)
    completed = subprocess.run(argv, cwd=ROOT, env=env, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise SystemExit(f"Command failed rc={completed.returncode}: {' '.join(map(str, argv))}")


def _build_with_ibcmd(
    ibcmd: Path, src_dir: Path, out_cf: Path, db_path: Path, *, env: dict[str, str]
) -> None:
    """Build a portable CF through a temporary file infobase.

    ``config import --out`` can preserve a platform requirement from an
    earlier export. Importing XML into a fresh infobase and saving the actual
    configuration makes the generated CF compatible with the target platform.
    """
    if db_path.exists():
        shutil.rmtree(db_path, ignore_errors=True)
    _run([
        str(ibcmd), "infobase", "create",
        f"--data={db_path}", f"--db-path={db_path / 'db-data'}", "--force",
    ], env=env, timeout=180)
    _run([
        str(ibcmd), "infobase", "config", "import",
        f"--data={db_path}", f"--db-path={db_path / 'db-data'}", str(src_dir),
    ], env=env, timeout=600)
    _run([
        str(ibcmd), "config", "save",
        f"--data={db_path}", f"--db-path={db_path / 'db-data'}", str(out_cf),
    ], env=env, timeout=600)


def _build_with_designer(onec: Path, src_dir: Path, out_cf: Path, db_path: Path, log_path: Path, *, env: dict[str, str]) -> None:
    """Legacy fallback through Configurator/DESIGNER."""
    if db_path.exists():
        shutil.rmtree(db_path, ignore_errors=True)
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


def main(argv: list[str]) -> int:
    src_dir = Path(argv[1]) if len(argv) > 1 else Path("src/cf")
    out_cf = Path(argv[2]) if len(argv) > 2 else Path("build/MCPTestManager.cf")
    db_path = Path(os.getenv("DB_PATH") or str(os_support.default_data_dir() / "ib-build-cf"))
    log_path = Path(os.getenv("BUILD_CF_LOG") or "build/build_cf.log")

    platform_dir = _platform_dir()
    version_name = platform_version_from_dir(platform_dir)
    require_websocket_support(version_name)
    if not src_dir.exists():
        raise SystemExit(f"Source directory not found: {src_dir}")

    out_cf.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if not os_support.IS_WINDOWS and "DISPLAY" not in env:
        env["DISPLAY"] = ":99"

    ibcmd = _ibcmd_executable(platform_dir)
    try:
        if ibcmd is not None:
            print(f"Building {out_cf} via ibcmd infobase config import ({ibcmd})", flush=True)
            _build_with_ibcmd(ibcmd, src_dir, out_cf, db_path, env=env)
        else:
            onec = _onec_designer_executable(platform_dir)
            print(f"ibcmd not found; building {out_cf} via DESIGNER ({onec})", flush=True)
            _build_with_designer(onec, src_dir, out_cf, db_path, log_path, env=env)
    finally:
        # The standalone-server caches (~650 MB) are regenerated on every start: do not leave them in the temp folder.
        if not os.getenv("DB_PATH"):
            for name in ("session-data", "temp", "perf-data"):
                shutil.rmtree(db_path / name, ignore_errors=True)

    print(out_cf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
