#!/usr/bin/env python3
# ruff: noqa: E402
"""Autonomous smoke E2E test for Answer42 StreamableHTTP transport.

Starts Answer42 in HTTP mode with a static Bearer token, connects via
mcp.client.streamable_http_client, and runs a minimal smoke flow:
- credentials_save / credentials_check scoped to the HTTP account
- start_session
- active_window
- stop_session

Requires the same environment variables as stdio smoke for the 1C target:
  ONEC_MCP_BASE_URL, ONEC_MCP_USERNAME, ONEC_MCP_PASSWORD, ONEC_MCP_VERSION (optional)
Override the HTTP token/account with:
  ONEC_MCP_HTTP_TOKEN (default: generated random token)
  ONEC_MCP_HTTP_ACCOUNT_ID (default: "http-smoke-<random>")
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client._memory import InMemoryTransport
from mcp.client.client import Client


def _parse_tool_result(result):
    sc = result.structured_content
    if isinstance(sc, dict) and "result" in sc:
        return sc["result"]
    if sc not in (None, {}):
        return sc
    texts = [getattr(item, "text", "") for item in result.content if getattr(item, "type", "") == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    parsed = []
    for text in texts:
        try:
            parsed.append(json.loads(text))
        except json.JSONDecodeError:
            parsed.append(text)
    return parsed


async def call_ok(session: ClientSession, name: str, args: dict | None = None):
    result = await session.call_tool(name, args or {})
    if result.is_error:
        raise AssertionError(f"{name} unexpectedly failed: {result.content}")
    value = _parse_tool_result(result)
    print(f"OK {name}", flush=True)
    return value


async def main() -> int:
    base_url = os.environ.get("ONEC_MCP_BASE_URL", "")
    username = os.environ.get("ONEC_MCP_USERNAME", "")
    password = os.environ.get("ONEC_MCP_PASSWORD", "")
    if not base_url or not username or not password:
        print("Set ONEC_MCP_BASE_URL, ONEC_MCP_USERNAME and ONEC_MCP_PASSWORD", flush=True)
        return 2

    token = os.environ.get("ONEC_MCP_HTTP_TOKEN") or f"smoke-{secrets.token_urlsafe(24)}"
    account_id = os.environ.get("ONEC_MCP_HTTP_ACCOUNT_ID") or f"http-smoke-{secrets.token_hex(6)}"
    host = os.environ.get("ONEC_MCP_HTTP_HOST", "127.0.0.1")
    port_env = os.environ.get("ONEC_MCP_HTTP_PORT")
    if port_env:
        port = int(port_env)
    else:
        # Pick a random free port to avoid collisions with OpenClaw gateway/other MCP servers.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = s.getsockname()[1]
    http_url = f"http://{host}:{port}/mcp"

    # Use a temp credentials file so the smoke does not mutate the user's store.
    creds_file = Path(tempfile.gettempdir()) / f"answer42-http-smoke-{secrets.token_hex(6)}.json"
    env = {
        **os.environ,
        "ONEC_MCP_CREDENTIALS_FILE": str(creds_file),
        "ONEC_MCP_HTTP": "1",
        "ONEC_MCP_HTTP_HOST": host,
        "ONEC_MCP_HTTP_PORT": str(port),
        "ONEC_MCP_HTTP_TOKEN": token,
        "ONEC_MCP_ACCOUNT_ID": account_id,
        "ONEC_MCP_LOG_LEVEL": "INFO",
    }

    print(f"=== Answer42 StreamableHTTP smoke ===", flush=True)
    print(f"endpoint={http_url} account_id={account_id}", flush=True)

    server_cmd = [
        sys.executable,
        "-m",
        "mcp_1c.server",
    ]
    proc = subprocess.Popen(
        server_cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    print(f"server pid={proc.pid}", flush=True)

    # Wait for the HTTP endpoint to become ready.
    deadline = time.monotonic() + 30.0
    ready = False
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(10.0, read=120.0),
        follow_redirects=True,
    )
    last_err = ""
    while time.monotonic() < deadline:
        try:
            resp = await http_client.head(http_url)
            if resp.status_code in (200, 202, 404, 405):
                ready = True
                break
        except Exception as exc:
            last_err = str(exc)
            await asyncio.sleep(0.2)
    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"Server did not become ready: {last_err}", flush=True)
        if proc.stdout:
            print(proc.stdout.read(), flush=True)
        return 1

    try:
        async with streamable_http_client(http_url, http_client=http_client, terminate_on_close=False) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                init_result = await session.initialize()
                print("OK initialize", flush=True)
                if init_result:
                    server_info = getattr(init_result, "serverInfo", None)
                    if server_info:
                        print(f"serverInfo.name={getattr(server_info, 'name', None)} version={getattr(server_info, 'version', None)}", flush=True)

                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]
                print(f"OK list_tools ({len(tools_result.tools)} tools): {tool_names}", flush=True)

                await call_ok(
                    session,
                    "credentials_save",
                    {
                        "url": base_url,
                        "username": username,
                        "password": password,
                        "title": "http-smoke-creds",
                    },
                )

                check = await call_ok(session, "credentials_check", {"base_url": base_url})
                assert check.get("found") is True, f"credentials not found: {check}"
                assert check.get("account_id") == account_id, f"wrong account_id: {check}"
                print(f"OK credentials_check account_id={check.get('account_id')}", flush=True)

                session_id = "http-smoke-session"
                start_args = {
                    "session_id": session_id,
                    "base_url": base_url,
                    "idle_timeout_minutes": 10,
                }
                # Let the server auto-resolve credentials by not passing username/password.
                start = await call_ok(session, "start_session", start_args)
                assert start.get("session_id") == session_id, f"unexpected session start result: {start}"

                await call_ok(session, "active_window", {"session_id": session_id})
                await call_ok(session, "stop_session", {"session_id": session_id, "clean_data": True})

        # Verify the credentials file was written in v2 account-bound format.
        data = json.loads(creds_file.read_text(encoding="utf-8"))
        assert data.get("version") == 2, f"expected v2 credentials file: {data}"
        assert account_id in data.get("accounts", {}), f"account {account_id} missing: {data}"
        print("OK credentials file v2 with correct account", flush=True)

        print("=== Answer42 StreamableHTTP smoke PASSED ===", flush=True)
        return 0
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"FAIL: {exc}", flush=True)
        return 1
    finally:
        creds_file.unlink(missing_ok=True)
        if proc.stdout:
            remaining = proc.stdout.read()
            if remaining:
                print(f"--- server stdout ---\n{remaining}\n--- end server stdout ---", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        await http_client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
