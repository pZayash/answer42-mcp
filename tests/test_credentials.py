import json

from mcp_1c.credentials import CredentialsStore


def test_credentials_exact_match_wins_over_wildcard(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "entries": [
            {"url": "https://example.com/infobase", "username": "exact", "password": "exact-pass"},
            {"url": "https://*.com/*", "username": "wild", "password": "wild-pass"},
        ]
    }), encoding="utf-8")

    store = CredentialsStore(path)

    assert store.lookup("https://example.com/infobase") == {
        "username": "exact",
        "password": "exact-pass",
    }


def test_credentials_wildcard_match(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "entries": [
            {"url": "https://*.example.invalid/*", "username": "u", "password": "p"},
        ]
    }), encoding="utf-8")

    store = CredentialsStore(path)

    assert store.lookup("https://dev.example.invalid/infobase") == {
        "username": "u",
        "password": "p",
    }


def test_credentials_list_redacts_password(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "entries": [
            {"url": "https://example.com/infobase", "username": "u", "password": "secret"},
        ]
    }), encoding="utf-8")

    store = CredentialsStore(path)

    assert store.list_entries() == [
        {"url": "https://example.com/infobase", "username": "u", "password": "***"},
    ]
