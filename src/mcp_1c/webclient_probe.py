"""Read runtime metadata from a 1C web publication without starting a 1C client.

This module implements the standard three-message 1C 8.5 web-client login
exchange used before ``e1cib/misc?cmd=startInfo``.  It is deliberately limited
to startup metadata.  Authentication material and ``VRS-Session`` stay local
to :func:`probe_web_runtime` and are never included in its result or errors.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# These constants are the public web-client envelope constants extracted from
# the platform's shipped JavaScript. They are not credentials for an infobase.
_OUTER_KEY = bytes([
    163, 174, 179, 144, 172, 188, 21, 128, 174, 191, 17, 141, 213, 98, 155,
    228, 46, 14, 7, 173, 177, 52, 53, 106, 131, 16, 83, 197, 113, 129, 9, 205,
])
_OUTER_IV = bytes([18, 107, 175, 234, 32, 119, 46, 66, 17, 204, 33, 151, 248, 94, 243, 47])
_INNER_IV = bytes([157, 123, 154, 32, 105, 101, 187, 40, 6, 122, 72, 61, 178, 108, 113, 142])
_BOOTSTRAP_RE = re.compile(
    r'var BASE = "([^"]+)";.*?var LANG = "([^"]+)";.*?var VERSION = "([^"]+)";',
    re.DOTALL,
)
# Moscow has used a fixed UTC+3 offset without DST since 2014.
_MOSCOW_FIXED_TZ = timezone(timedelta(hours=3), "MSK")


def _moscow_tz() -> tzinfo:
    """Return Europe/Moscow, falling back to UTC+3 when no tz database exists.

    Windows has no system IANA database, so ``zoneinfo`` depends on the
    ``tzdata`` package there; a missing package must not break the probe.
    """
    try:
        return ZoneInfo("Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return _MOSCOW_FIXED_TZ


class WebRuntimeProbeError(RuntimeError):
    """A non-authentication web-runtime probe failure."""


class WebRuntimeAuthenticationError(WebRuntimeProbeError):
    """The web publication rejected the supplied 1C credentials."""


@dataclass(frozen=True)
class _Bootstrap:
    base: str
    language: str
    platform_version: str


def _json_body(response: requests.Response) -> dict[str, Any]:
    try:
        value = json.loads(response.content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebRuntimeProbeError("1C web publication returned an invalid JSON response") from exc
    if not isinstance(value, dict):
        raise WebRuntimeProbeError("1C web publication returned an unexpected JSON response")
    return value


def _crc(data: bytes) -> bytes:
    return data + (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")


def _encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    padding_size = 16 - len(data) % 16
    padded = data + os.urandom(padding_size - 1) + bytes([padding_size])
    return Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(padded)


def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    value = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(data)
    if not value or not 1 <= value[-1] <= 16:
        raise WebRuntimeProbeError("1C web authentication returned invalid credential padding")
    return value[: -value[-1]]


def _outer_encrypt(data: bytes) -> bytes:
    return _encrypt(data, _OUTER_KEY, _OUTER_IV)


def _outer_decrypt(value: str) -> bytes:
    try:
        return _decrypt(base64.b64decode(value), _OUTER_KEY, _OUTER_IV)
    except (ValueError, TypeError) as exc:
        raise WebRuntimeProbeError("1C web authentication returned an invalid challenge") from exc


def _packed_outer(data: bytes) -> str:
    return base64.b64encode(_outer_encrypt(_crc(data))).decode("ascii")


def _verify_crc(data: bytes, message: str) -> bytes:
    if len(data) < 4 or zlib.crc32(data[:-4]) & 0xFFFFFFFF != int.from_bytes(data[-4:], "little"):
        raise WebRuntimeProbeError(message)
    return data[:-4]


def _post_json(
    session: requests.Session,
    url: str,
    data: dict[str, Any],
    timeout: float,
    headers: dict[str, str] | None = None,
) -> tuple[requests.Response, dict[str, Any]]:
    request_headers = {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)
    response = session.post(url, headers=request_headers, json=data, timeout=timeout)
    return response, _json_body(response)


def _bootstrap(session: requests.Session, base_url: str, timeout: float) -> _Bootstrap:
    response = session.get(base_url.rstrip("/") + "/", timeout=timeout)
    response.raise_for_status()
    match = _BOOTSTRAP_RE.search(response.text)
    if not match:
        raise WebRuntimeProbeError("web publication bootstrap variables were not found")
    path, language, platform_version = match.groups()
    publication = urlparse(response.url)
    base = f"{publication.scheme}://{publication.netloc}{path.rstrip('/')}/"
    return _Bootstrap(base=base, language=language, platform_version=platform_version)


def _login(
    session: requests.Session,
    bootstrap: _Bootstrap,
    username: str,
    password: str,
    timeout: float,
) -> tuple[str, str]:
    """Return ``(client_id, VRS-Session)``. Never log either value."""
    login_url = urljoin(bootstrap.base, "e1cib/login")
    client_id = str(uuid.uuid4())
    params: dict[str, str] = {
        "version": bootstrap.platform_version,
        "nooida": "",
        "vl": bootstrap.language,
        "clnId": client_id,
    }
    initial_url = requests.Request("POST", login_url, params=params).prepare().url
    response, initial = _post_json(session, initial_url, {}, timeout)
    if response.status_code != 402:
        raise WebRuntimeProbeError(f"unexpected initial 1C web login response: HTTP {response.status_code}")
    try:
        sid = str(initial["exception"]["seanceID"])
    except (KeyError, TypeError) as exc:
        raise WebRuntimeProbeError("1C web login did not return an authentication challenge") from exc
    params["sid"] = sid
    login_url = requests.Request("POST", login_url, params=params).prepare().url

    encoded_username = username.encode("utf-8")
    username_credential = _packed_outer(
        bytes([1, 16]) + os.urandom(16) + len(encoded_username).to_bytes(4, "little") + encoded_username
    )
    response, challenge_response = _post_json(session, login_url, {"cred": username_credential}, timeout)
    if response.status_code in {401, 402, 403}:
        raise WebRuntimeAuthenticationError("1C web authentication failed: user was not accepted")
    response.raise_for_status()
    try:
        challenge = _verify_crc(_outer_decrypt(str(challenge_response["cred"])), "1C web authentication returned an invalid challenge CRC")
        case_sensitive = challenge[17] == 0 and challenge[18] == 0 and challenge[19] == 1
    except (IndexError, KeyError) as exc:
        raise WebRuntimeProbeError("1C web authentication returned an unsupported challenge") from exc

    password_for_hash = password if case_sensitive else password.upper()
    proof = hashlib.sha256(base64.b64encode(hashlib.sha1(password_for_hash.encode("utf-8")).digest())).digest()
    proof_key = os.urandom(32)
    encrypted_proof = _encrypt(proof_key, proof, _INNER_IV)
    proof_credential = base64.b64encode(_crc(bytes([3, len(encrypted_proof)]) + encrypted_proof)).decode("ascii")
    response, proof_response = _post_json(session, login_url, {"cred": proof_credential}, timeout)
    if response.status_code in {401, 402, 403}:
        raise WebRuntimeAuthenticationError("1C web authentication failed: incorrect user or password")
    response.raise_for_status()
    try:
        reply = _verify_crc(base64.b64decode(str(proof_response["cred"])), "1C web authentication returned an invalid password-proof CRC")
        key_size = reply[0]
        encrypted_login_key = reply[1 : 1 + key_size]
        login_key = _decrypt(encrypted_login_key, proof_key, _INNER_IV)
    except (IndexError, KeyError, ValueError, TypeError) as exc:
        raise WebRuntimeProbeError("1C web authentication returned an unsupported password-proof response") from exc

    encoded_password = password.encode("utf-8")
    random_prefix = os.urandom(32)
    password_payload = bytes([len(random_prefix)]) + random_prefix + len(encoded_password).to_bytes(4, "little") + encoded_password
    final_credential = base64.b64encode(_encrypt(_crc(password_payload), login_key, _INNER_IV)).decode("ascii")
    response, _ = _post_json(session, login_url, {"cred": final_credential}, timeout)
    if response.status_code in {401, 402, 403}:
        raise WebRuntimeAuthenticationError("1C web authentication failed: incorrect user or password")
    response.raise_for_status()
    token = response.headers.get("VRS-Session")
    if not token:
        raise WebRuntimeProbeError("1C web login succeeded without a session header")
    return client_id, token


def _logout(session: requests.Session, base: str, session_token: str, timeout: float) -> bool:
    """End the temporary 1C web session; logout failures never hide probe data."""
    try:
        response = session.post(
            urljoin(base, "e1cib/logout"),
            # The browser uses sendBeacon here. It serializes the empty
            # request root as JSON and supplies the web-session token in the
            # ``seanceId`` query parameter; VRS-Session alone is rejected by
            # the platform after startInfo has initialized the session.
            params={"seanceId": session_token},
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            data=json.dumps({"root": {}}, separators=(",", ":")),
            timeout=timeout,
        )
        return response.ok
    except requests.RequestException:
        return False


def _extensions(values: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(values, list):
        return result
    for value in values:
        match = re.fullmatch(r"\s*(.*?)\s*\(([^()]*)\)\s*", value) if isinstance(value, str) else None
        if match:
            result.append({"name": match.group(1), "version": match.group(2)})
    return result


def probe_web_runtime(base_url: str, username: str, password: str, timeout: float = 15.0) -> dict[str, Any]:
    """Authenticate temporarily, read ``startInfo``, and always attempt logout.

    The result is suitable for persistent configuration/RAG caches. It contains
    no credential, cookie, challenge, session identifier, or session header.
    """
    session = requests.Session()
    bootstrap: _Bootstrap | None = None
    session_token = ""
    result: dict[str, Any] | None = None
    try:
        bootstrap = _bootstrap(session, base_url, timeout)
        client_id, session_token = _login(session, bootstrap, username, password, timeout)
        start_url = requests.Request(
            "POST",
            urljoin(bootstrap.base, "e1cib/misc"),
            params={
                "cmd": "startInfo",
                "clnId": client_id,
                "connSpeed": "normal",
                "clnOS": "Linux",
                "clientTime": datetime.now(_moscow_tz()).strftime("%Y%m%d%H%M%S"),
                "defaultScale": "compact",
            },
        ).prepare().url
        payload = {
            "#request": "4500381B-DB30-4A10-9DB4-990038032ACF",
            "request": {
                "#Value": ["686A0E1D-8946-4583-B31C-0B39AA16A6AC"],
                "Value": [{"x": 1280, "y": 720, "dpi": 0}],
            },
        }
        response, start_info = _post_json(
            session, start_url, payload, timeout, headers={"VRS-Session": session_token, "Pragma": "sync=1"}
        )
        response.raise_for_status()
        info = start_info.get("response")
        if not isinstance(info, dict):
            raise WebRuntimeProbeError("1C web startInfo response has no runtime data")
        result = {
            "found": True,
            "source": "web_start_info",
            "platform_version": bootstrap.platform_version,
            "configuration_name": str(info.get("confName") or ""),
            "configuration_version": str(info.get("configVersion") or ""),
            "configuration_full_name": str(info.get("fullConfName") or ""),
            "has_configuration_extensions": bool(info.get("hasConfigExt")),
            "extensions": _extensions(info.get("extName")),
        }
    finally:
        if bootstrap is not None and session_token:
            # A successful login allocates a server-side 1C web session even
            # though this probe never opens the desktop. Explicitly close it
            # rather than waiting for the publication's idle-session timeout.
            session_closed = _logout(session, bootstrap.base, session_token, timeout)
            if result is not None:
                result["session_closed"] = session_closed
        session.close()
    if result is None:  # pragma: no cover - defensive; exceptions exit above.
        raise WebRuntimeProbeError("1C web runtime probe did not return a result")
    return result
