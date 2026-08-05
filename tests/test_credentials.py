import json
from pathlib import Path

from mcp_1c.credentials import DEFAULT_CREDENTIALS_PATH, CredentialsStore


def test_default_credentials_path_uses_answer42_name():
    assert DEFAULT_CREDENTIALS_PATH == Path.home() / ".answer42-credentials.json"


def test_credentials_v2_lookup_by_url_and_title(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": {
                    "default": {
                        "entries": [
                            {
                                "url": "https://example.com/infobase",
                                "username": "u",
                                "password": "p",
                                "title": "bp27932",
                                "aliases": ["test-base"],
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = CredentialsStore(path)

    assert store.lookup("https://example.com/infobase") == {"username": "u", "password": "p"}
    assert store.lookup("bp27932") == {"username": "u", "password": "p"}
    assert store.lookup("test-base") == {"username": "u", "password": "p"}
    assert store.resolve("bp27932")["url"] == "https://example.com/infobase"
    assert store.list_entries() == [
        {
            "url": "https://example.com/infobase",
            "verified": False,
            "verification_status": "unverified",
            "title": "bp27932",
            "aliases": ["test-base"],
        }
    ]
    assert store.titles() == [
        {
            "url": "https://example.com/infobase",
            "verified": False,
            "verification_status": "unverified",
            "title": "bp27932",
            "aliases": ["test-base"],
        }
    ]


def test_credentials_v2_account_isolation(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)

    store.upsert("https://a.example/ib", "u1", "p1", title="a", account_id="account-a")
    store.upsert("https://b.example/ib", "u2", "p2", title="b", account_id="account-b")

    assert store.lookup("a", account_id="account-a") == {"username": "u1", "password": "p1"}
    assert store.lookup("b", account_id="account-b") == {"username": "u2", "password": "p2"}
    assert store.lookup("a", account_id="account-b") is None
    assert store.lookup("b", account_id="account-a") is None
    assert sorted(store.list_accounts()) == ["account-a", "account-b", "default"]


def test_credentials_default_account_for_missing_account_id(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)

    store.upsert("https://example.com/ib", "u", "p")

    assert store.lookup("https://example.com/ib") == {"username": "u", "password": "p"}
    assert store.lookup("https://example.com/ib", account_id="default") == {"username": "u", "password": "p"}


def test_credentials_exact_match_wins_over_wildcard(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": {
                    "default": {
                        "entries": [
                            {"url": "https://example.com/infobase", "username": "exact", "password": "exact-pass"},
                            {"url": "https://*.com/*", "username": "wild", "password": "wild-pass"},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = CredentialsStore(path)

    assert store.lookup("https://example.com/infobase") == {
        "username": "exact",
        "password": "exact-pass",
    }


def test_credentials_wildcard_match(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": {
                    "default": {
                        "entries": [
                            {"url": "https://*.example.invalid/*", "username": "u", "password": "p"},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = CredentialsStore(path)

    assert store.lookup("https://dev.example.invalid/infobase") == {
        "username": "u",
        "password": "p",
    }


def test_credentials_upsert_creates_file_and_returns_bool(tmp_path):
    path = tmp_path / "nested" / "credentials.json"
    store = CredentialsStore(path)

    updated = store.upsert("https://example.com/infobase/", "u1", "p1")

    assert updated is False  # new entry
    assert store.lookup("https://example.com/infobase") == {"username": "u1", "password": "p1"}

    updated = store.upsert("https://example.com/infobase", "u2", "p2")

    assert updated is True  # existing entry updated
    assert store.lookup("https://example.com/infobase") == {"username": "u2", "password": "p2"}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["accounts"]["default"]["entries"] == [
        {"url": "https://example.com/infobase", "username": "u2", "password": "p2", "verified": False}
    ]


def test_credentials_upsert_updates_by_title(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)
    store.upsert("https://old.example/ib", "u1", "p1", title="main")

    updated = store.upsert("https://new.example/ib", "u2", "p2", title="main")

    assert updated is True
    assert store.resolve("main")["url"] == "https://new.example/ib"
    assert store.lookup("main") == {"username": "u2", "password": "p2"}


def test_credentials_upsert_reuses_wildcard_with_same_login_password(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)

    assert store.upsert("https://*.example.invalid/*", "u", "p", title="wild") is False
    assert store.upsert("https://dev.example.invalid/ib", "u", "p", title="dev", aliases=["dev-ib"]) is True

    entries = json.loads(path.read_text(encoding="utf-8"))["accounts"]["default"]["entries"]
    assert entries == [
        {
            "url": "https://*.example.invalid/*",
            "username": "u",
            "password": "p",
            "verified": False,
            "title": "dev",
            "aliases": ["dev-ib"],
        }
    ]


def test_credentials_upsert_reuses_same_url_with_same_login_password(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)

    assert store.upsert("https://example.invalid/ib", "u", "p") is False
    assert store.upsert("https://example.invalid/ib/", "u", "p", title="same") is True

    assert len(json.loads(path.read_text(encoding="utf-8"))["accounts"]["default"]["entries"]) == 1
    assert store.resolve("same")["url"] == "https://example.invalid/ib"


def test_credentials_upsert_same_url_new_password_becomes_unverified(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)

    store.upsert("https://example.invalid/ib", "u", "p1")
    assert store.mark_verified("https://example.invalid/ib", "u", "p1") is True
    assert store.list_entries()[0]["verified"] is True

    assert store.upsert("https://example.invalid/ib", "u", "p2") is True

    assert store.lookup("https://example.invalid/ib") == {"username": "u", "password": "p2"}
    assert store.list_entries()[0]["verified"] is False
    assert store.list_entries()[0]["verification_status"] == "unverified"


def test_credentials_mark_verified_and_remove_matching_wildcard(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialsStore(path)

    store.upsert("https://*.example.invalid/*", "u", "p")
    assert store.mark_verified("https://dev.example.invalid/ib", "u", "p") is True
    assert store.list_entries() == [
        {
            "url": "https://*.example.invalid/*",
            "verified": True,
            "verification_status": "verified",
        }
    ]

    assert store.remove_matching("https://dev.example.invalid/ib", "u", "bad") is False
    assert store.remove_matching("https://dev.example.invalid/ib", "u", "p") is True
    assert store.list_entries() == []


def test_credentials_remove_exact_match(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": {
                    "default": {
                        "entries": [
                            {"url": "https://example.com/infobase", "username": "u", "password": "secret"},
                            {"url": "https://other.example.com/infobase", "username": "o", "password": "other"},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = CredentialsStore(path)

    removed = store.remove("https://example.com/infobase/")

    assert removed is True
    assert store.lookup("https://example.com/infobase") is None
    assert store.lookup("https://other.example.com/infobase") == {"username": "o", "password": "other"}


def test_credentials_remove_nonexistent(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": {
                    "default": {
                        "entries": [
                            {"url": "https://example.com/infobase", "username": "u", "password": "secret"},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = CredentialsStore(path)

    removed = store.remove("https://nonexistent.example.com/infobase")

    assert removed is False
    assert store.lookup("https://example.com/infobase") == {"username": "u", "password": "secret"}


def test_credentials_legacy_v1_loaded_into_legacy_account(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {"url": "https://legacy.example/ib", "username": "old", "password": "old-pass"},
                ]
            }
        ),
        encoding="utf-8",
    )

    store = CredentialsStore(path)

    assert store.lookup("https://legacy.example/ib") is None
    assert store.lookup("https://legacy.example/ib", account_id="legacy") == {
        "username": "old",
        "password": "old-pass",
    }
    assert "legacy" in store.list_accounts()


def test_credentials_save_migrates_legacy_to_v2(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {"url": "https://legacy.example/ib", "username": "old", "password": "old-pass"},
                ]
            }
        ),
        encoding="utf-8",
    )

    store = CredentialsStore(path)
    store.upsert("https://new.example/ib", "new", "new-pass")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert "legacy" in saved["accounts"]
    assert saved["accounts"]["default"]["entries"] == [
        {"url": "https://new.example/ib", "username": "new", "password": "new-pass", "verified": False}
    ]
