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


def test_login_treats_invalid_password_proof_crc_as_authentication_failure(monkeypatch):
    bootstrap = probe._Bootstrap("https://host/base/", "ru_RU", "8.5")
    responses = [
        (FakeResponse(status_code=402, payload={"exception": {"seanceID": "sid"}}), {"exception": {"seanceID": "sid"}}),
        (FakeResponse(payload={"cred": "challenge"}), {"cred": "challenge"}),
        (FakeResponse(payload={"cred": "proof"}), {"cred": "proof"}),
    ]

    monkeypatch.setattr(probe, "_post_json", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(probe, "_outer_decrypt", lambda _value: b"challenge")
    monkeypatch.setattr(probe, "_verify_crc", lambda value, message: (_ for _ in ()).throw(probe.WebRuntimeProbeError(message)) if value == b"proof" else b"\0" * 20)
    monkeypatch.setattr(probe.base64, "b64decode", lambda _value: b"proof")

    with pytest.raises(probe.WebRuntimeAuthenticationError, match="incorrect user or password"):
        probe._login(FakeSession(), bootstrap, "user", "password", 5.0)


def test_json_body_accepts_1c_start_info_with_javascript_undefined():
    class Response:
        content = b'{"response":{"presentation":undefined,"confName":"MS"}}'

    assert probe._json_body(Response()) == {
        "response": {"presentation": None, "confName": "MS"}
    }


def test_moscow_tz_falls_back_to_fixed_offset_without_tz_database(monkeypatch):
    def missing_zone(key: str):
        raise probe.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(probe, "ZoneInfo", missing_zone)

    tz = probe._moscow_tz()

    assert probe.datetime(2026, 1, 1, tzinfo=tz).utcoffset() == probe.timedelta(hours=3)
