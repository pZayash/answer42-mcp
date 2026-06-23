#!/usr/bin/env python3
"""Auto-reload OpenClaw MCP runtimes when testclient-bridge code changes.

This is intentionally outside the MCP stdio process: stdio JSON-RPC sessions cannot
be hot-reloaded safely. On file changes we ask OpenClaw to dispose cached MCP
runtimes; the next tool call starts a fresh testclient-bridge process.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PATTERNS = ("src/mcp_1c/**/*.py", "pyproject.toml")


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


def run_reload(server: str | None) -> int:
    cmd = ["openclaw", "mcp", "reload"]
    # Current OpenClaw reload command is global; keep --server support ready if added later.
    if server:
        probe = subprocess.run(["openclaw", "mcp", "reload", "--help"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if f"--server" in probe.stdout:
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
    parser.add_argument("--server", default="testclient-bridge")
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
        # debounce bursts of editor writes
        time.sleep(args.debounce)
        new = snapshot(root, patterns)
        if new == current:
            continue
        current = new
        print("[mcp-autoreload] change detected; running openclaw mcp reload", flush=True)
        code = run_reload(args.server)
        print(f"[mcp-autoreload] reload exit code {code}", flush=True)
        if args.once:
            return code


if __name__ == "__main__":
    raise SystemExit(main())
