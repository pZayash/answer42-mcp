import asyncio
import base64
import hashlib
import os
import subprocess
import sys
import types

import pytest

from mcp_1c import runtime, server


def test_external_open_env_installs_a_recording_xdg_open(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.os_support, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = runtime.external_open_env(tmp_path, open_targets=False)
    wrapper = tmp_path / "bin" / "xdg-open"
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert env["ANSWER42_REAL_PATH"] == "/usr/bin:/bin" and env["ANSWER42_EXTERNAL_OPEN"] == "record"
    assert wrapper.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert "setsid xdg-open" in wrapper.read_text(encoding="utf-8")
    assert runtime.external_open_env(tmp_path, open_targets=True)["ANSWER42_EXTERNAL_OPEN"] == "open"


@pytest.mark.skipif(sys.platform == "win32", reason="runs the POSIX shell wrapper")
def test_xdg_open_wrapper_records_and_returns_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.os_support, "IS_WINDOWS", False)
    env = {**os.environ, **runtime.external_open_env(tmp_path, open_targets=False)}
    (tmp_path / "logs-client").mkdir()
    subprocess.run(["sh", "-c", 'xdg-open "http://example.invalid/files/a b.pdf"'], env=env, check=True, timeout=10)
    opens = runtime.external_opens(tmp_path)
    assert [o["target"] for o in opens] == ["http://example.invalid/files/a b.pdf"]


def test_external_opens_reads_the_log_since_a_moment(tmp_path):
    log = tmp_path / "logs-client" / runtime.EXTERNAL_OPEN_LOG_NAME
    log.parent.mkdir()
    log.write_text("100\thttp://old\n200\thttp://new\nbroken line\n", encoding="utf-8")
    assert [o["target"] for o in runtime.external_opens(tmp_path)] == ["http://old", "http://new"]
    assert [o["target"] for o in runtime.external_opens(tmp_path, since=150)] == ["http://new"]
    assert runtime.external_opens(tmp_path / "missing") == []


def test_test_client_env_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(server.os_support, "IS_WINDOWS", False)
    monkeypatch.setattr(runtime.os_support, "IS_WINDOWS", False)
    sess = types.SimpleNamespace(html_agent=None, data_dir=tmp_path, client_xvfb_process=object())
    monkeypatch.delenv("ONEC_MCP_EXTERNAL_OPEN", raising=False)
    assert server._test_client_env(sess)["ANSWER42_EXTERNAL_OPEN"] == "record"  # Answer42's own Xvfb
    sess.client_xvfb_process = None
    assert server._test_client_env(sess)["ANSWER42_EXTERNAL_OPEN"] == "open"  # a real display
    monkeypatch.setenv("ONEC_MCP_EXTERNAL_OPEN", "off")
    assert server._test_client_env(sess) is None


def test_download_file_name():
    name = server._download_file_name
    assert name("attachment; filename*=UTF-8''%D0%9E%D0%A1%D0%92.pdf; filename=x.pdf", "http://h/f") == "ОСВ.pdf"
    assert name('attachment; filename="report a.pdf"', "http://h/f") == "report a.pdf"
    assert name("", "http://h/neuro1c/files/oVLTXQ") == "oVLTXQ"
    assert name("", "http://h/a/..%2F..%2Fetc%2Fpasswd") == "_.._etc_passwd"
    assert name("", "http://h/") == "download"


def test_html_download_saves_the_file_and_rejects_http_errors(tmp_path, monkeypatch):
    payload = b"%PDF-1.4 \x00\xff binary"
    replies = [
        {"url": "http://h/neuro1c/files/tok", "status": 200, "content_type": "application/pdf",
         "disposition": 'attachment; filename="ОСВ.pdf"', "size": len(payload), "base64": base64.b64encode(payload).decode()},
        {"url": "http://h/neuro1c/files/tok", "status": 200, "content_type": "application/pdf",
         "disposition": 'attachment; filename="ОСВ.pdf"', "size": len(payload), "base64": base64.b64encode(payload).decode()},
        {"url": "http://h/gone", "status": 404, "content_type": "text/plain", "disposition": "", "size": 9,
         "base64": base64.b64encode(b"not found").decode()},
    ]
    calls = []

    async def fake_command(session_id, page, op, args, timeout=30.0):
        calls.append((op, args))
        return replies.pop(0)

    monkeypatch.setattr(server, "_html_command", fake_command)
    monkeypatch.setitem(server._SESSIONS, "dl", types.SimpleNamespace(data_dir=tmp_path))
    first = asyncio.run(server.html_download(selector="a.dl", session_id="dl"))
    second = asyncio.run(server.html_download(selector="a.dl", session_id="dl"))
    assert first["filename"] == "ОСВ.pdf" and second["filename"] == "ОСВ (2).pdf"
    assert (tmp_path / "downloads" / "ОСВ.pdf").read_bytes() == payload
    assert first["sha256"] == hashlib.sha256(payload).hexdigest() and first["size"] == len(payload)
    assert calls[0] == ("fetch", {"selector": "a.dl", "text": "", "index": 0, "url": "", "max_bytes": 50 * 1024 * 1024})
    with pytest.raises(server.ToolError, match="HTTP 404"):
        asyncio.run(server.html_download(url="/gone", session_id="dl"))
    with pytest.raises(server.ToolError, match="selector, text or url"):
        asyncio.run(server.html_download(session_id="dl"))


def test_html_mode_disabled_starts_no_bridge(tmp_path):
    sess = types.SimpleNamespace(onec_thin="", data_dir=tmp_path, session_id="off", html_agent=None, html_disabled=False)
    asyncio.run(server._start_html_agent(sess, "disabled"))
    assert sess.html_agent is None and sess.html_disabled is True
    with pytest.raises(server.ToolError, match="Unknown html_mode"):
        asyncio.run(server._start_html_agent(sess, "off"))
