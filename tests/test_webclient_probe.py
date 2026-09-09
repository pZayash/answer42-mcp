import json
from dataclasses import dataclass
from typing import Any

import pytest

from mcp_1c import webclient_probe as probe


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict[str, Any] | None = None
    headers: dict[str, str] | None = None

    @property
    def content(self):
        return json.dumps(self.payload or {}).encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def close(self):
        self.closed = True


def test_logout_uses_browser_compatible_seance_request(monkeypatch):
    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

    session = Session()

    assert probe._logout(session, "https://host/base/", "temporary-session", 5.0) is True
    assert session.calls == [
        (
            "https://host/base/e1cib/logout",
            {
                "params": {"seanceId": "temporary-session"},
                "headers": {"Content-Type": "text/plain;charset=UTF-8"},
                "data": '{"root":{}}',
                "timeout": 5.0,
            },
        )
    ]


def test_probe_closes_successful_web_session(monkeypatch):
    monkeypatch.setattr(probe, "_bootstrap", lambda *_: probe._Bootstrap("https://host/base/", "ru_RU", "8.5"))
    monkeypatch.setattr(probe, "_login", lambda *_: ("client-id", "temporary-session"))
    calls = []

    def fake_post_json(_session, url, data, timeout, headers=None):
        calls.append((url, data, headers))
        return FakeResponse(payload={"response": {"confName": "Demo", "configVersion": "1.0", "extName": ["Ext (2.0)"]}}), {"response": {"confName": "Demo", "configVersion": "1.0", "extName": ["Ext (2.0)"]}}

    def fake_logout(_session, base, token, timeout):
        assert base == "https://host/base/"
        assert token == "temporary-session"
        return True

    monkeypatch.setattr(probe, "_post_json", fake_post_json)
    monkeypatch.setattr(probe, "_logout", fake_logout)
    monkeypatch.setattr(probe.requests, "Session", FakeSession)

    result = probe.probe_web_runtime("https://host/base", "user", "password")

    assert result["configuration_name"] == "Demo"
    assert result["extensions"] == [{"name": "Ext", "version": "2.0"}]
    assert result["session_closed"] is True
    assert calls[0][2] == {"VRS-Session": "temporary-session", "Pragma": "sync=1"}


def test_probe_does_not_attempt_logout_when_login_fails(monkeypatch):
    monkeypatch.setattr(probe, "_bootstrap", lambda *_: probe._Bootstrap("https://host/base/", "ru_RU", "8.5"))
    monkeypatch.setattr(probe, "_login", lambda *_: (_ for _ in ()).throw(probe.WebRuntimeAuthenticationError("denied")))
    monkeypatch.setattr(probe, "_logout", lambda *_: pytest.fail("logout must not run without a session"))
    monkeypatch.setattr(probe.requests, "Session", FakeSession)

    with pytest.raises(probe.WebRuntimeAuthenticationError, match="denied"):
        probe.probe_web_runtime("https://host/base", "user", "password")
