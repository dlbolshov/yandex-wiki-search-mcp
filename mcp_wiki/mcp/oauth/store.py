from typing import Protocol

from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_wiki.mcp.oauth.types import YandexOauthAuthorizationCode, YandexOAuthState

# Yandex refresh token lifetime, shared so the stores cannot disagree:
# https://yandex.cloud/en-ru/docs/iam/concepts/authorization/refresh-token#token-lifetime
REFRESH_TOKEN_TTL_SECONDS = 31 * 24 * 60 * 60


def client_deadline(client: OAuthClientInformationFull) -> int | None:
    """When a registration dies, or None if it never does.

    RFC 7591 §3.2.1 spells "never expires" as `client_secret_expires_at=0`,
    and that is what the SDK stamps at /register when
    `client_secret_expiry_seconds` is unset; a public client, issued no
    secret, carries None instead. Both mean no deadline. Shared so the
    stores cannot read the field differently: one that took 0 at face value
    would treat every such registration as dead since 1970.
    """
    return client.client_secret_expires_at or None


class OAuthStore(Protocol):
    async def save_client(self, client: OAuthClientInformationFull) -> None: ...
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None: ...

    async def save_state(
        self, state: YandexOAuthState, *, state_id: str, ttl: int | None = None
    ) -> None: ...
    async def get_state(self, state_id: str) -> YandexOAuthState | None: ...

    async def save_auth_code(
        self, code: YandexOauthAuthorizationCode, *, ttl: int | None = None
    ) -> None: ...
    async def get_auth_code(
        self, code_id: str
    ) -> YandexOauthAuthorizationCode | None: ...

    async def save_oauth_token(
        self, token: OAuthToken, client_id: str, scopes: list[str], resource: str | None
    ) -> None: ...
    async def get_access_token(self, token: str) -> AccessToken | None: ...
    async def get_refresh_token(self, token: str) -> RefreshToken | None: ...

    async def revoke_refresh_token(self, token: str) -> None: ...
    async def revoke_access_token(self, token: str) -> None: ...
