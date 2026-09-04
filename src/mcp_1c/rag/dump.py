"""Dump 1C configurations and extensions to XML source trees for RAG indexing.

Supports three dump paths:
1. Live infobase (server /S or file /F) via Designer DumpConfigToFiles
2. CF/CFE files loaded into a temporary file infobase, then dumped
3. Cleanup of temporary infobases after dump
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import os_support
from .. import platform as _platform

LOGGER = logging.getLogger(__name__)


def _run_designer(argv: list[str], *, timeout: float = 600, display: str = "") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    LOGGER.info("Running DESIGNER: %s", " ".join(argv))
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)


def _find_free_display() -> str:
    if os_support.IS_WINDOWS:
        return ""
    return _platform.find_free_display(99)


def dump_infobase_to_xml(
    platform_dir: Path,
    connection_mode: str,
    connection_value: str,
    output_dir: Path,
    username: str = "",
    password: str = "",
    extension_name: str | None = None,
    display: str = "",
) -> dict[str, Any]:
    """Dump a live infobase configuration/extension to XML source files via Designer.

    Args:
        platform_dir: 1C platform bin directory.
        connection_mode: 'server' or 'file-direct'.
        connection_value: server/infobase string or file path.
        output_dir: where to write XML sources.
        username, password: infobase credentials.
        extension_name: if set, dump this extension instead of main config.
        display: X11 display; auto-detected when empty.
    """
    onec = str(platform_dir / os_support.executable_name("1cv8"))
    if not Path(onec).exists():
        raise FileNotFoundError(f"1cv8 (Designer/thick client) not found: {onec}")

    output_dir.mkdir(parents=True, exist_ok=True)
    effective_display = display or _find_free_display()

    argv = [onec, "DESIGNER"]
    if connection_mode == "server":
        argv.extend(["/S", connection_value])
    elif connection_mode in ("file-direct", "file"):
        argv.extend(["/F", connection_value])
    else:
        raise ValueError(f"Unsupported connection mode for dump: {connection_mode!r}")

    if username:
        argv.extend(["/N", username])
    if password:
        argv.extend(["/P", password])

    argv.extend(["/DumpConfigToFiles", str(output_dir)])
    argv.append("-Format")
    argv.append("Hierarchical")

    if extension_name:
        argv.extend(["-Extension", extension_name])

    argv.extend(["/DisableStartupDialogs", "/DisableStartupMessages"])

    completed = _run_designer(argv, display=effective_display)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Designer DumpConfigToFiles failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )

    file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())
    LOGGER.info("Dumped %d files to %s", file_count, output_dir)
    return {
        "output_dir": str(output_dir),
        "connection_mode": connection_mode,
        "connection_value": connection_value,
        "extension": extension_name,
        "files_count": file_count,
    }


def dump_all_extensions_to_xml(
    platform_dir: Path,
    connection_mode: str,
    connection_value: str,
    output_dir: Path,
    username: str = "",
    password: str = "",
    display: str = "",
) -> dict[str, Any]:
    """Dump all live infobase extensions to XML source subdirectories via Designer.

    1C Designer supports the ``DumpConfigToFiles ... -AllExtensions`` switch.
    Unlike dumping the main configuration, the output directory becomes a
    container with one child directory per extension; each child is a normal
    Designer XML source tree with its own ``Configuration.xml``.
    """
    onec = str(platform_dir / os_support.executable_name("1cv8"))
    if not Path(onec).exists():
        raise FileNotFoundError(f"1cv8 (Designer/thick client) not found: {onec}")

    output_dir.mkdir(parents=True, exist_ok=True)
    effective_display = display or _find_free_display()

    argv = [onec, "DESIGNER"]
    if connection_mode == "server":
        argv.extend(["/S", connection_value])
    elif connection_mode in ("file-direct", "file"):
        argv.extend(["/F", connection_value])
    else:
        raise ValueError(f"Unsupported connection mode for dump: {connection_mode!r}")

    if username:
        argv.extend(["/N", username])
    if password:
        argv.extend(["/P", password])

    argv.extend(["/DumpConfigToFiles", str(output_dir)])
    argv.extend(["-Format", "Hierarchical", "-AllExtensions"])
    argv.extend(["/DisableStartupDialogs", "/DisableStartupMessages"])

    completed = _run_designer(argv, display=effective_display)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Designer DumpConfigToFiles all extensions failed (rc={completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )

    extensions: list[dict[str, Any]] = []
    for child in sorted((item for item in output_dir.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        if not (child / "Configuration.xml").exists():
            continue
        extensions.append({
            "name": child.name,
            "output_dir": str(child),
            "files_count": sum(1 for item in child.rglob("*") if item.is_file()),
        })

    file_count = sum(1 for item in output_dir.rglob("*") if item.is_file())
    LOGGER.info("Dumped %d extension files (%d extensions) to %s", file_count, len(extensions), output_dir)
    return {
        "output_dir": str(output_dir),
        "connection_mode": connection_mode,
        "connection_value": connection_value,
        "all_extensions": True,
        "extensions_count": len(extensions),
        "extensions": extensions,
        "files_count": file_count,
    }


def dump_cf_to_xml(
    platform_dir: Path,
    cf_path: Path,
    output_dir: Path,
    username: str = "",
    password: str = "",
    extension_name: str | None = None,
    display: str = "",
    cleanup_temp_ib: bool = True,
) -> dict[str, Any]:
    """Load a .cf/.cfe file into a temp infobase and dump to XML sources.

    Creates a temporary file infobase, loads the CF/CFE via ibcmd,
    applies the configuration, then dumps to XML via Designer.
    The temporary infobase is removed when cleanup_temp_ib=True.
    """
    ibcmd = str(platform_dir / os_support.executable_name("ibcmd"))
    onec = str(platform_dir / os_support.executable_name("1cv8"))

    if not Path(ibcmd).exists():
        raise FileNotFoundError(f"ibcmd not found: {ibcmd}")
    if not Path(onec).exists():
        raise FileNotFoundError(f"1cv8 (Designer) not found: {onec}")
    if not cf_path.exists():
        raise FileNotFoundError(f"CF/CFE file not found: {cf_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    effective_display = display or _find_free_display()

    temp_dir = Path(tempfile.mkdtemp(prefix="answer42-rag-dump-"))
    db_path = temp_dir / "ib"

    try:
        subprocess.run(
            [ibcmd, "infobase", "create", f"--db-path={db_path}", "--create-database"],
            capture_output=True, text=True, timeout=120, check=True,
        )

        load_argv = [ibcmd, "config", "load", f"--db-path={db_path}", str(cf_path)]
        if extension_name:
            load_argv.extend(["--extension", extension_name])
        subprocess.run(load_argv, capture_output=True, text=True, timeout=300, check=True)

        subprocess.run(
            [ibcmd, "config", "apply", f"--db-path={db_path}", "--force",
             "--dynamic=force", "--session-terminate=force"],
            input="3\n", capture_output=True, text=True, timeout=300, check=True,
        )

        dump_argv = [
            onec, "DESIGNER",
            "/F", str(db_path),
            "/DumpConfigToFiles", str(output_dir),
            "-Format", "Hierarchical",
            "/DisableStartupDialogs", "/DisableStartupMessages",
        ]
        if extension_name:
            dump_argv.extend(["-Extension", extension_name])
        if username:
            dump_argv.extend(["/N", username])
        if password:
            dump_argv.extend(["/P", password])

        completed = _run_designer(dump_argv, display=effective_display)
        if completed.returncode != 0:
            raise RuntimeError(
                f"Designer DumpConfigToFiles from CF failed (rc={completed.returncode}): "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )

        file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())
        LOGGER.info("Dumped %d files from %s to %s", file_count, cf_path, output_dir)
        return {
            "output_dir": str(output_dir),
            "cf_path": str(cf_path),
            "extension": extension_name,
            "files_count": file_count,
        }
    finally:
        if cleanup_temp_ib:
            shutil.rmtree(temp_dir, ignore_errors=True)
            LOGGER.info("Cleaned up temp infobase: %s", temp_dir)
