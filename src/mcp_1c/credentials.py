"""Secure credential store for 1C infobase URLs.

Reads a JSON file mapping base URLs (exact or wildcard) to
{username, password}.  The file is never committed to git;
a sample ``credentials.example.json`` is provided for reference.

Environment variable ``ONEC_MCP_CREDENTIALS_FILE`` overrides the
default path (``~/.answer42-credentials.json``).
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


class CredentialsStore:
    """Look up 1C credentials by base URL.

    File format (JSON)::

        {
          "entries": [
            {
              "url": "https://example.invalid/infobase",
              "username": "user",
              "password": "secret"
            },
            {
              "url": "https://*.example.invalid/*",
              "username": "wildcard-user",
              "password": "wildcard-secret"
            }
          ]
        }

    Matching rules (first match wins):
      * exact ``url`` match
      * ``fnmatch`` wildcard match (``*``, ``?``, ``[seq]``)
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path or DEFAULT_CREDENTIALS_PATH)
        self._entries: list[dict[str, str]] = []
        self._load()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def lookup(self, base_url: str) -> dict[str, str] | None:
        """Return {username, password} for *base_url*, title or alias, or None."""
        resolved = self.resolve(base_url)
        if resolved is None:
            return None
        return {"username": resolved["username"], "password": resolved["password"]}

    def resolve(self, base_url_or_title: str) -> dict[str, str] | None:
        """Return full credential entry for URL, title or alias, without mutating it."""
        value = str(base_url_or_title or "").strip()
        url = value.rstrip("/")
        normalized_title = value.lower()
        for entry in self._entries:
            title = str(entry.get("title", "")).strip().lower()
            aliases = [str(a).strip().lower() for a in entry.get("aliases", [])]
            if title and title == normalized_title or normalized_title in aliases:
                return dict(entry)
        for entry in self._entries:
            pattern = entry["url"].rstrip("/")
            if url == pattern:
                return dict(entry)
        for entry in self._entries:
            pattern = entry["url"].rstrip("/")
            if fnmatch.fnmatch(url, pattern):
                return dict(entry)
        return None

    def list_entries(self) -> list[dict[str, object]]:
        """Return credential titles and URL patterns without usernames or passwords."""
        result: list[dict[str, str]] = []
        for entry in self._entries:
            public = {"url": entry["url"]}
            if entry.get("title"):
                public["title"] = entry["title"]
            if entry.get("aliases"):
                public["aliases"] = list(entry["aliases"])
            result.append(public)
        return result

    def titles(self) -> list[dict[str, object]]:
        """Return named credential entries only, without secrets."""
        return [e for e in self.list_entries() if e.get("title")]

    def upsert(self, url: str, username: str, password: str, title: str = "", aliases: list[str] | None = None) -> bool:
        """Add or update a credential entry for *url*.

        Returns True when an existing entry was updated, False when a new entry
        was created.  Usernames and passwords are intentionally not returned.
        """
        url = url.rstrip("/")
        title = str(title or "").strip()
        aliases = [str(alias).strip() for alias in (aliases or []) if str(alias).strip()]
        updated = False
        for entry in self._entries:
            if entry["url"].rstrip("/") == url or (title and str(entry.get("title", "")).strip() == title):
                entry["url"] = url
                entry["username"] = username
                entry["password"] = password
                if title:
                    entry["title"] = title
                else:
                    entry.pop("title", None)
                if aliases:
                    entry["aliases"] = aliases
                else:
                    entry.pop("aliases", None)
                updated = True
                break
        else:
            entry = {"url": url, "username": username, "password": password}
            if title:
                entry["title"] = title
            if aliases:
                entry["aliases"] = aliases
            self._entries.append(entry)
        self._save()
        return updated

    def remove(self, url: str) -> bool:
        """Remove the credential entry for *url* (exact match)."""
        url = url.rstrip("/")
        for i, entry in enumerate(self._entries):
            if entry["url"].rstrip("/") == url:
                del self._entries[i]
                self._save()
                return True
        return False

    def reload(self) -> None:
        """Re-read the credentials file (useful after external edits)."""
        self._load()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Persist entries to the JSON file atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"entries": self._entries}, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self._path)
        LOGGER.info("Saved %d credential entries to %s", len(self._entries), self._path)

    def _load(self) -> None:
        if not self._path.exists():
            LOGGER.debug("Credentials file not found: %s", self._path)
            self._entries = []
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = data.get("entries", [])
            LOGGER.info("Loaded %d credential entries from %s",
                        len(self._entries), self._path)
        except Exception as exc:
            LOGGER.warning("Failed to load credentials from %s: %s",
                           self._path, exc)
            self._entries = []


# Singleton for the MCP server process
_store: CredentialsStore | None = None


def get_store(path: Path | str | None = None) -> CredentialsStore:
    global _store
    if _store is None:
        _store = CredentialsStore(path)
    return _store
