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


def test_credentials_upsert_creates_file_and_redacts_previous(tmp_path):
    path = tmp_path / "nested" / "credentials.json"
    store = CredentialsStore(path)

    previous = store.upsert("https://example.com/infobase/", "u1", "p1")

    assert previous == {"url": "https://example.com/infobase", "username": "", "password": "***"}
    assert store.lookup("https://example.com/infobase") == {"username": "u1", "password": "p1"}

    previous = store.upsert("https://example.com/infobase", "u2", "p2")

    assert previous == {"url": "https://example.com/infobase", "username": "u1", "password": "***"}
    assert store.lookup("https://example.com/infobase") == {"username": "u2", "password": "p2"}
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "entries": [{"url": "https://example.com/infobase", "username": "u2", "password": "p2"}]
    }


def test_credentials_remove_exact_match(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "entries": [
            {"url": "https://example.com/infobase", "username": "u", "password": "secret"},
            {"url": "https://other.example.com/infobase", "username": "o", "password": "other"},
        ]
    }), encoding="utf-8")
    store = CredentialsStore(path)

    removed = store.remove("https://example.com/infobase/")

    assert removed == {"url": "https://example.com/infobase", "username": "u", "password": "***"}
    assert store.lookup("https://example.com/infobase") is None
    assert store.lookup("https://other.example.com/infobase") == {"username": "o", "password": "other"}
