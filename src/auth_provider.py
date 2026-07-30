"""OAuth 2.1 + PKCE provider for Claude.ai's custom-connector flow.

Mirrors srj-mcp's provider: in-memory client registry, auto-approved authorize,
and a static admin token honoured as a permanent access token so curl probes work
without the OAuth dance. Single-worker only — the stores are process-local, which
is why the Dockerfile pins --workers 1.
"""

import os
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ADMIN_TOKEN = os.environ.get("WIKIDATA_MCP_TOKEN", "")


class WikidataOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str):
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull):
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            client_id=client.client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + 300,
            scopes=params.scopes or ["mcp"],
            code_challenge=params.code_challenge,
        )
        sep = "&" if "?" in str(params.redirect_uri) else "?"
        state = f"&state={params.state}" if params.state else ""
        return f"{params.redirect_uri}{sep}code={code}{state}"

    async def load_authorization_code(self, client, authorization_code: str):
        return self._codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        self._codes.pop(authorization_code.code, None)
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        self._tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + 3600,
        )
        self._refresh[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(self, client, refresh_token: str):
        return self._refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        access = secrets.token_urlsafe(48)
        self._tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + 3600,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=refresh_token.token,
            scope=" ".join(scopes or refresh_token.scopes),
        )

    async def load_access_token(self, token: str):
        if ADMIN_TOKEN and token == ADMIN_TOKEN:
            return AccessToken(
                token=token, client_id="admin", scopes=["mcp"], expires_at=None
            )
        tok = self._tokens.get(token)
        if tok and tok.expires_at and tok.expires_at < time.time():
            self._tokens.pop(token, None)
            return None
        return tok

    async def revoke_token(self, token) -> None:
        self._tokens.pop(getattr(token, "token", ""), None)
        self._refresh.pop(getattr(token, "token", ""), None)
