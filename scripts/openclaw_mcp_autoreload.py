#!/usr/bin/env python3
"""Auto-reload OpenClaw MCP runtimes when Answer42 code changes.

This is intentionally outside the MCP stdio process: stdio JSON-RPC sessions cannot
be hot-reloaded safely. On file changes we ask OpenClaw to dispose cached MCP
runtimes; the next tool call starts a fresh Answer42 process.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PATTERNS = ("src/mcp_1c/**/*.py", "src/cf/**/*.bsl", "pyproject.toml")


def snapshot(root: Path, patterns: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for pattern in patterns:
        files.extend(root.glob(pattern))
    for path in sorted({p for p in files if p.is_file()}):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_size).encode("ascii"))
    return digest.hexdigest()


def _active_answer42_pids() -> list[int]:
    """Return active Answer42 MCP stdio process PIDs, excluding this watcher."""
    current_pid = os.getpid()
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, cmd = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if "openclaw_mcp_autoreload.py" in cmd:
            continue
        if "--dev openclaw" not in cmd:
            continue
        if "/answer42" in cmd or "mcp_1c.server" in cmd:
            pids.append(pid)
    return pids


def kill_active_answer42(timeout: float = 5.0) -> list[int]:
    """Terminate active Answer42 MCP processes so next tool call starts fresh code/env."""
    pids = _active_answer42_pids()
    if not pids:
        return []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
    for pid in sorted(remaining):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return pids


def run_reload(server: str | None) -> int:
    cmd = ["openclaw", "mcp", "reload"]
    # Current OpenClaw reload command is global; keep --server support ready if added later.
    if server:
        probe = subprocess.run(["openclaw", "mcp", "reload", "--help"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if "--server" in probe.stdout:
            cmd.extend(["--server", server])
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sys.stdout.write(result.stdout)
    sys.stdout.flush()
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--debounce", type=float, default=1.5)
    parser.add_argument(
        "--dev-restart-timeout",
        type=float,
        default=30.0,
        help="seconds to wait after the last detected change before reloading OpenClaw MCP runtimes",
    )
    parser.add_argument("--server", default="Answer42")
    parser.add_argument(
        "--no-kill-active",
        action="store_true",
        help="do not terminate active Answer42 stdio processes after OpenClaw cache reload",
    )
    parser.add_argument("--once", action="store_true", help="wait for one change, reload once, exit")
    parser.add_argument("--pid-file", default="", help="write watcher pid to this file and remove it on exit")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if args.pid_file:
        pid_file = Path(args.pid_file)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        def cleanup_pid_file() -> None:
            try:
                if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_file.unlink()
            except FileNotFoundError:
                pass

        atexit.register(cleanup_pid_file)

    patterns = DEFAULT_PATTERNS
    current = snapshot(root, patterns)
    print(f"[mcp-autoreload] watching {root} ({', '.join(patterns)})", flush=True)

    while True:
        time.sleep(args.interval)
        new = snapshot(root, patterns)
        if new == current:
            continue
        # Debounce bursts of editor writes.  For OpenClaw development this must
        # be long enough to avoid reloading the MCP process on every small file
        # write while an agent is applying a multi-file patch.
        deadline = time.monotonic() + max(args.debounce, args.dev_restart_timeout)
        stable = new
        while True:
            time.sleep(args.interval)
            newer = snapshot(root, patterns)
            if newer != stable:
                stable = newer
                deadline = time.monotonic() + max(args.debounce, args.dev_restart_timeout)
                continue
            if time.monotonic() >= deadline:
                break
        if stable == current:
            continue
        current = stable
        print("[mcp-autoreload] change detected; running openclaw mcp reload", flush=True)
        code = run_reload(args.server)
        print(f"[mcp-autoreload] reload exit code {code}", flush=True)
        if code == 0 and not args.no_kill_active:
            killed = kill_active_answer42()
            if killed:
                print(f"[mcp-autoreload] terminated active Answer42 MCP processes: {killed}", flush=True)
        if args.once:
            return code


if __name__ == "__main__":
    raise SystemExit(main())
