"""Secure, account-bound credential store for 1C infobase URLs.

Reads a JSON file mapping MCP accounts to credential entries.  Each entry
contains a base URL (exact or wildcard), username, password and optional
title/aliases.  The file is never committed to git; a sample
``credentials.example.json`` is provided for reference.

Environment variable ``ONEC_MCP_CREDENTIALS_FILE`` overrides the default path
(``~/.answer42-credentials.json``).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = Path(
    os.getenv("ONEC_MCP_CREDENTIALS_FILE",
              str(Path.home() / ".answer42-credentials.json"))
)


def _normalize_account(account_id: str | None) -> str:
    return str(account_id or "default").strip() or "default"


class CredentialsStore:
    """Look up 1C credentials by base URL, scoped to an MCP account.

    File format (JSON) v2::

        {
          "version": 2,
          "accounts": {
            "default": {
              "entries": [
                {
                  "url": "https://example.invalid/infobase",
                  "username": "user",
                  "password": "secret",
                  "title": "dev-01 BP",
                  "aliases": ["dev01"]
                }
              ]
            }
          }
        }

    Legacy v1 format (flat ``entries`` list) is loaded on first read and
    assigned to the ``legacy`` account; the store is migrated to v2 on save.

    Matching rules (first match wins):
      * exact ``url`` match
      * ``fnmatch`` wildcard match (``*``, ``?``, ``[seq]``)
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path or DEFAULT_CREDENTIALS_PATH)
        self._version = 2
        self._accounts: dict[str, dict[str, list[dict[str, object]]]] = {}
        self._file_signature: tuple[int, int] | None = None
        self._load()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def lookup(
        self,
        base_url: str,
        account_id: str | None = None,
    ) -> dict[str, str] | None:
        """Return {username, password} for *base_url* under *account_id*."""
        resolved = self.resolve(base_url, account_id)
        if resolved is None:
            return None
        return {"username": resolved["username"], "password": resolved["password"]}

    def resolve(
        self,
        base_url_or_title: str,
        account_id: str | None = None,
    ) -> dict[str, str] | None:
        """Return full credential entry for URL/title under *account_id*."""
        entries = self._account_entries(account_id)
        value = str(base_url_or_title or "").strip()
        url = value.rstrip("/")
        normalized_title = value.lower()

        # Match by title or alias first.
        for entry in entries:
            title = str(entry.get("title", "")).strip().lower()
            aliases = [str(a).strip().lower() for a in entry.get("aliases", [])]
            if (title and title == normalized_title) or (normalized_title in aliases):
                return dict(entry)

        # Match by exact or wildcard URL.
        for entry in entries:
            pattern = str(entry.get("url", "")).rstrip("/")
            if url == pattern:
                return dict(entry)
        for entry in entries:
            pattern = str(entry.get("url", "")).rstrip("/")
            if pattern and fnmatch.fnmatch(url, pattern):
                return dict(entry)

        return None

    def matching_entries(
        self,
        base_url: str,
        account_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Return every exact and wildcard credential candidate for a URL.

        Exact URL entries precede wildcard entries so callers can try the most
        specific mapping first without discarding other matching wildcards.
        """
        url = str(base_url or "").strip().rstrip("/")
        if not url:
            return []
        entries = self._account_entries(account_id)
        exact = [
            dict(entry) for entry in entries
            if str(entry.get("url", "")).rstrip("/") == url
        ]
        wildcard = [
            dict(entry) for entry in entries
            if self._has_wildcards(str(entry.get("url", "")))
            and fnmatch.fnmatch(url, str(entry.get("url", "")).rstrip("/"))
        ]
        return exact + wildcard

    def list_entries(
        self,
        account_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Return credential titles, URL patterns and verification status without secrets."""
        result: list[dict[str, object]] = []
        for entry in self._account_entries(account_id):
            public: dict[str, object] = {
                "url": entry.get("url", ""),
                "verified": bool(entry.get("verified", False)),
                "verification_status": "verified" if entry.get("verified", False) else "unverified",
            }
            if entry.get("title"):
                public["title"] = entry["title"]
            if entry.get("aliases"):
                public["aliases"] = list(entry["aliases"])
            result.append(public)
        return result

    def titles(
        self,
        account_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Return named credential entries only, without secrets."""
        return [e for e in self.list_entries(account_id) if e.get("title")]

    def upsert(
        self,
        url: str,
        username: str,
        password: str,
        title: str = "",
        aliases: list[str] | None = None,
        account_id: str | None = None,
    ) -> bool:
        """Add or update a credential entry for *url* under *account_id*.

        Returns True when an existing entry was updated, False when a new
        entry was created.  Usernames and passwords are intentionally not
        returned.
        """
        account_id = _normalize_account(account_id)
        entries = self._ensure_account(account_id)

        url = url.rstrip("/")
        title = str(title or "").strip()
        aliases = [str(alias).strip() for alias in (aliases or []) if str(alias).strip()]

        updated = False
        for entry in entries:
            entry_url = str(entry.get("url", "")).rstrip("/")
            entry_title = str(entry.get("title", "")).strip()
            same_url_or_title = entry_url == url or (title and entry_title == title)
            same_credentials = entry.get("username") == username and entry.get("password") == password
            duplicate_match = same_credentials and self._credential_url_matches(entry_url, url)
            if same_url_or_title or duplicate_match:
                # Do not create a second exact URL when an existing wildcard already
                # covers it (or vice versa) for the same username/password.  Merge
                # user-facing labels into the existing entry instead.
                if not duplicate_match or not entry_url:
                    entry["url"] = url
                entry["username"] = username
                entry["password"] = password
                entry["verified"] = False
                if title:
                    entry["title"] = title
                elif not duplicate_match:
                    entry.pop("title", None)
                if aliases:
                    existing_aliases = [str(a).strip() for a in entry.get("aliases", []) if str(a).strip()]
                    merged_aliases = list(dict.fromkeys(existing_aliases + aliases)) if duplicate_match else aliases
                    entry["aliases"] = merged_aliases
                elif not duplicate_match:
                    entry.pop("aliases", None)
                updated = True
                break
        else:
            entry = {"url": url, "username": username, "password": password, "verified": False}
            if title:
                entry["title"] = title
            if aliases:
                entry["aliases"] = aliases
            entries.append(entry)

        self._save()
        return updated

    def remove(
        self,
        url: str,
        account_id: str | None = None,
    ) -> bool:
        """Remove the credential entry for *url* (exact match) under *account_id*."""
        account_id = _normalize_account(account_id)
        entries = self._account_entries(account_id)
        url = url.rstrip("/")
        for i, entry in enumerate(entries):
            if str(entry.get("url", "")).rstrip("/") == url:
                del entries[i]
                self._save()
                return True
        return False

    def mark_verified(
        self,
        url: str,
        username: str,
        password: str,
        account_id: str | None = None,
    ) -> bool:
        """Mark a matching credential as successfully verified."""
        entry = self._find_entry(url, username, password, account_id)
        if entry is None:
            return False
        if entry.get("verified") is True:
            return True
        entry["verified"] = True
        self._save()
        return True

    def remove_matching(
        self,
        url: str,
        username: str,
        password: str,
        account_id: str | None = None,
    ) -> bool:
        """Remove a credential entry matching URL/pattern and username/password."""
        entries = self._account_entries(account_id)
        url = str(url or "").rstrip("/")
        for i, entry in enumerate(entries):
            entry_url = str(entry.get("url", "")).rstrip("/")
            if (
                entry.get("username") == username
                and entry.get("password") == password
                and self._credential_url_matches(entry_url, url)
            ):
                del entries[i]
                self._save()
                return True
        return False

    def reload(self) -> None:
        """Re-read the credentials file (useful after external edits)."""
        self._load()

    def list_accounts(self) -> list[str]:
        """Return all account ids present in the store."""
        self._reload_if_changed()
        return sorted(self._accounts.keys())

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _current_file_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload_if_changed(self) -> None:
        """Pick up edits made outside this process (CLI, editor) before using the data."""
        if self._current_file_signature() != self._file_signature:
            self._load()

    def _ensure_account(self, account_id: str) -> list[dict[str, object]]:
        self._reload_if_changed()
        account_id = _normalize_account(account_id)
        if account_id not in self._accounts:
            self._accounts[account_id] = {"entries": []}
        return self._accounts[account_id]["entries"]

    def _account_entries(self, account_id: str | None) -> list[dict[str, object]]:
        self._reload_if_changed()
        account_id = _normalize_account(account_id)
        return self._accounts.get(account_id, {}).get("entries", [])

    @staticmethod
    def _has_wildcards(value: str) -> bool:
        return any(ch in value for ch in "*?[")

    @classmethod
    def _credential_url_matches(cls, stored_url: str, candidate_url: str) -> bool:
        stored_url = str(stored_url or "").rstrip("/")
        candidate_url = str(candidate_url or "").rstrip("/")
        if not stored_url or not candidate_url:
            return False
        if stored_url == candidate_url:
            return True
        if cls._has_wildcards(stored_url) and fnmatch.fnmatch(candidate_url, stored_url):
            return True
        return cls._has_wildcards(candidate_url) and fnmatch.fnmatch(stored_url, candidate_url)

    def _find_entry(
        self,
        url: str,
        username: str,
        password: str,
        account_id: str | None = None,
    ) -> dict[str, object] | None:
        url = str(url or "").rstrip("/")
        for entry in self._account_entries(account_id):
            entry_url = str(entry.get("url", "")).rstrip("/")
            if (
                entry.get("username") == username
                and entry.get("password") == password
                and self._credential_url_matches(entry_url, url)
            ):
                return entry
        return None

    def _save(self) -> None:
        """Persist entries to the JSON file atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": self._version, "accounts": self._accounts}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self._path)
        self._file_signature = self._current_file_signature()
        total = sum(len(a.get("entries", [])) for a in self._accounts.values())
        LOGGER.info("Saved %d credential entries across %d accounts to %s",
                    total, len(self._accounts), self._path)

    def _load(self) -> None:
        self._file_signature = self._current_file_signature()
        if not self._path.exists():
            LOGGER.debug("Credentials file not found: %s", self._path)
            self._accounts = {"default": {"entries": []}}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            version = data.get("version", 1)
            if version == 2:
                self._accounts = data.get("accounts", {})
                if "default" not in self._accounts:
                    self._accounts["default"] = {"entries": []}
            else:
                legacy_entries = data.get("entries", [])
                self._accounts = {
                    "legacy": {"entries": legacy_entries},
                    "default": {"entries": []},
                }
                LOGGER.warning(
                    "Loaded legacy v1 credential store from %s; "
                    "legacy entries assigned to account 'legacy'. "
                    "The file will be migrated to v2 on next save.",
                    self._path,
                )
            LOGGER.info("Loaded credential store v%d from %s with %d account(s)",
                        version, self._path, len(self._accounts))
        except Exception as exc:
            LOGGER.warning("Failed to load credentials from %s: %s",
                           self._path, exc)
            self._accounts = {"default": {"entries": []}}


# Singleton for the MCP server process
_store: CredentialsStore | None = None


def get_store(path: Path | str | None = None) -> CredentialsStore:
    global _store
    if _store is None:
        _store = CredentialsStore(path)
    return _store
