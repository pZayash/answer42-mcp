"""Cross-platform OS helpers for Answer42 runtime.

Keep platform-specific process, lock and filesystem defaults here so the
higher-level MCP tools do not import POSIX-only modules on Windows.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"


def default_data_dir() -> Path:
    """Return the default Answer42 runtime data directory for this OS."""
    override = os.getenv("ONEC_MCP_DATA_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "answer42"


def runtime_path(env_name: str, *parts: str) -> Path:
    """Return an env-overridable path under the runtime data directory."""
    override = os.getenv(env_name)
    if override:
        return Path(override)
    return default_data_dir().joinpath(*parts)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive file lock on POSIX and Windows.

    The implementation imports fcntl/msvcrt lazily so importing Answer42 on
    Windows does not fail on the POSIX-only ``fcntl`` module.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if IS_WINDOWS:
            import msvcrt  # type: ignore

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def subprocess_creation_kwargs(*, detached: bool = True) -> dict[str, object]:
    """Return Popen kwargs for a separately controllable child process."""
    if IS_WINDOWS:
        flags = 0
        if detached:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True} if detached else {}


def kill_process_tree(proc: subprocess.Popen | None, label: str, logger: logging.Logger | None = None) -> None:
    """Terminate a subprocess tree in a cross-platform way."""
    log = logger or logging.getLogger(__name__)
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            log.info("Stopped %s (pid=%s) via taskkill", label, proc.pid)
            return
        except Exception as exc:
            log.debug("taskkill failed for %s (pid=%s): %s", label, proc.pid, exc)
        try:
            proc.terminate()
            proc.wait(timeout=5)
            log.info("Stopped %s (pid=%s) via terminate", label, proc.pid)
            return
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=5)
            log.warning("Killed %s (pid=%s)", label, proc.pid)
        except Exception as exc:
            log.error("Failed to stop %s (pid=%s): %s", label, proc.pid, exc)
        return

    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
        log.info("Stopped %s (pid=%s) via SIGTERM", label, proc.pid)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
        log.warning("Killed %s (pid=%s) via SIGKILL", label, proc.pid)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        log.error("Failed to stop %s (pid=%s)", label, proc.pid)


def kill_processes_by_command_substrings(
    substrings: list[str],
    label: str,
    logger: logging.Logger | None = None,
) -> None:
    """Best-effort cleanup of stale child processes matching command-line substrings."""
    patterns = [s for s in substrings if s]
    if not patterns:
        return
    log = logger or logging.getLogger(__name__)
    if IS_WINDOWS:
        patterns_json = json.dumps(patterns, ensure_ascii=False)
        script = f"""
$patterns = ConvertFrom-Json @'
{patterns_json}
'@
$own = {os.getpid()}
Get-CimInstance Win32_Process | Where-Object {{
  if ($_.ProcessId -eq $own -or -not $_.CommandLine) {{ return $false }}
  $cmd = $_.CommandLine
  foreach ($p in $patterns) {{
    if ($p -and $cmd -like ('*' + $p + '*')) {{ return $true }}
  }}
  return $false
}} | ForEach-Object {{
  try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }} catch {{}}
}}
"""
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
            log.info("Requested cleanup of stale %s processes by command substrings", label)
        except Exception as exc:
            log.debug("Failed to cleanup stale %s processes: %s", label, exc)
        return

    try:
        completed = subprocess.run(
            ["pgrep", "-f", "|".join(patterns)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:
        log.debug("pgrep failed for stale %s cleanup: %s", label, exc)
        return
    own = {os.getpid(), os.getppid()}
    for raw_pid in completed.stdout.split():
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        if pid in own:
            continue
        try:
            os.kill(pid, 9)
            log.info("Killed stale %s process pid=%s", label, pid)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log.debug("Failed to kill stale %s process pid=%s: %s", label, pid, exc)


def executable_name(base: str) -> str:
    """Return executable name with .exe suffix on Windows."""
    return f"{base}.exe" if IS_WINDOWS and not base.lower().endswith(".exe") else base
