"""Static Bearer-token auth for the Answer42 MCP HTTP transport.

This module provides a minimal auth layer for deployments where the MCP
server is exposed over StreamableHTTP behind a static Bearer token. The
token is checked locally; the account id embedded in the resulting
AccessToken is used to namespace credentials and sessions.

In stdio mode no auth is performed and ``current_account_id()`` falls back to
``ONEC_MCP_ACCOUNT_ID`` or ``"default"``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

LOGGER = logging.getLogger(__name__)

DEFAULT_SCOPE = "answer42"
DEFAULT_ALGORITHM = "sha256"
ENV_TOKEN = "ONEC_MCP_HTTP_TOKEN"
ENV_TOKEN_FILE = "ONEC_MCP_HTTP_TOKEN_FILE"
ENV_ACCOUNT_ID = "ONEC_MCP_ACCOUNT_ID"


def _hash_token(token: str, algorithm: str = DEFAULT_ALGORITHM) -> str:
    digest = hashlib.new(algorithm, token.encode("utf-8")).hexdigest()
    return f"token-{digest[:16]}"


@dataclass(frozen=True, slots=True)
class StaticTokenConfig:
    """Resolved static auth configuration."""

    token: str
    account_id: str
    scope: str
    issuer_url: str
    resource_server_url: str


class StaticTokenVerifier(TokenVerifier):
    """Verify a static Bearer token and expose an account id in the token claims."""

    def __init__(
        self,
        token: str,
        account_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        resource_server_url: str = "",
    ) -> None:
        self._token = token
        self._account_id = account_id or _hash_token(token)
        self._scope = scope
        self._resource_server_url = resource_server_url or "http://answer42.local"

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._token):
            return None
        import time

        now = int(time.time())
        claims: dict[str, Any] = {"account_id": self._account_id}
        return AccessToken(
            token=token,
            client_id=self._account_id,
            scopes=[self._scope],
            expires_at=now + 86400 * 365,  # effectively non-expiring
            resource=self._resource_server_url,
            subject=self._account_id,
            claims=claims,
        )


def _read_file(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def load_static_token_config(
    token: str | None = None,
    token_file: str | None = None,
    account_id: str | None = None,
    scope: str = DEFAULT_SCOPE,
    issuer_url: str = "",
    resource_server_url: str = "",
) -> StaticTokenConfig:
    """Resolve token and account id from explicit args, env, or files.

    Resolution order for the token:
      1. ``token`` argument
      2. ``ONEC_MCP_HTTP_TOKEN`` env variable
      3. ``token_file`` argument
      4. ``ONEC_MCP_HTTP_TOKEN_FILE`` env variable

    Resolution order for the account id:
      1. ``account_id`` argument
      2. ``ONEC_MCP_ACCOUNT_ID`` env variable
      3. stable hash of the token
    """
    resolved_token: str | None = None
    if token:
        resolved_token = token
    elif os.getenv(ENV_TOKEN):
        resolved_token = os.getenv(ENV_TOKEN)
    elif token_file:
        resolved_token = _read_file(token_file)
    elif os.getenv(ENV_TOKEN_FILE):
        resolved_token = _read_file(os.getenv(ENV_TOKEN_FILE))

    if not resolved_token:
        raise ValueError(
            "Static HTTP token is required. Provide it via --http-token, "
            f"{ENV_TOKEN}, --http-token-file, or {ENV_TOKEN_FILE}."
        )

    resolved_account_id = account_id or os.getenv(ENV_ACCOUNT_ID) or _hash_token(resolved_token)

    return StaticTokenConfig(
        token=token,
        account_id=resolved_account_id,
        scope=scope or DEFAULT_SCOPE,
        issuer_url=issuer_url or f"http://{resolved_account_id}.answer42.local",
        resource_server_url=resource_server_url or "http://answer42.local",
    )


def build_auth_settings(config: StaticTokenConfig) -> AuthSettings:
    """Build MCP AuthSettings from a StaticTokenConfig."""
    return AuthSettings(
        issuer_url=config.issuer_url,
        resource_server_url=config.resource_server_url,
        required_scopes=[config.scope],
    )


def build_token_verifier(config: StaticTokenConfig) -> StaticTokenVerifier:
    return StaticTokenVerifier(
        token=token,
        account_id=config.account_id,
        scope=config.scope,
        resource_server_url=config.resource_server_url,
    )


def current_account_id(fallback: str | None = None) -> str:
    """Return the current MCP account id.

    In HTTP mode this reads the account id from the authenticated Bearer token.
    In stdio mode it falls back to ``ONEC_MCP_ACCOUNT_ID`` or ``"default"``.
    """
    from_env = os.getenv(ENV_ACCOUNT_ID)
    if from_env:
        return from_env

    try:
        access_token = get_access_token()
        if access_token is not None:
            account_id = access_token.claims.get("account_id")
            if account_id:
                return str(account_id)
            return access_token.client_id or access_token.subject or _hash_token(access_token.token)
    except Exception as exc:
        LOGGER.debug("Could not read HTTP auth context: %s", exc)

    return fallback or "default"
