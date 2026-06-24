"""Secure credential store for 1C infobase URLs.

Reads a JSON file mapping base URLs (exact or wildcard) to
{username, password}.  The file is never committed to git;
a sample ``credentials.example.json`` is provided for reference.

Environment variable ``ONEC_MCP_CREDENTIALS_FILE`` overrides the
default path (``~/.onec-mcp-credentials.json``).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = Path(
    os.getenv("ONEC_MCP_CREDENTIALS_FILE",
              str(Path.home() / ".onec-mcp-credentials.json"))
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
        """Return {username, password} for *base_url*, or None."""
        url = base_url.rstrip("/")
        for entry in self._entries:
            pattern = entry["url"].rstrip("/")
            if url == pattern:
                return {"username": entry["username"], "password": entry["password"]}
        for entry in self._entries:
            pattern = entry["url"].rstrip("/")
            if fnmatch.fnmatch(url, pattern):
                return {"username": entry["username"], "password": entry["password"]}
        return None

    def list_entries(self) -> list[dict[str, str]]:
        """Return entries with passwords redacted (safe for logging)."""
        return [
            {"url": e["url"], "username": e["username"], "password": "***"}
            for e in self._entries
        ]

    def reload(self) -> None:
        """Re-read the credentials file (useful after external edits)."""
        self._load()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

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
