"""Shared builders for OAuth layer tests."""

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from mcp_wiki.mcp.oauth.types import YandexOauthAuthorizationCode, YandexOAuthState

CLIENT_REDIRECT_URI = "http://localhost:3000/callback"
# Shorter than REFRESH_TOKEN_TTL_SECONDS on purpose, so a test can tell the
# window a registration starts with from the one a token grant renews it to.
PUBLIC_CLIENT_IDLE_TTL = 24 * 60 * 60


def make_state(client_state: str | None = "state-1") -> YandexOAuthState:
    return YandexOAuthState(
        redirect_uri=AnyUrl(CLIENT_REDIRECT_URI),
        code_challenge="challenge-1",
        redirect_uri_provided_explicitly=True,
        client_id="client-1",
        client_state=client_state,
    )


def make_auth_code(
    *,
    code: str = "mcp_code-1",
    expires_at: float = 2_000_000.0,
) -> YandexOauthAuthorizationCode:
    return YandexOauthAuthorizationCode(
        code=code,
        yandex_auth_code="ya-code-1",
        client_id="client-1",
        redirect_uri=AnyUrl(CLIENT_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        expires_at=expires_at,
        scopes=["wiki:read"],
        code_challenge="challenge-1",
    )


def make_token(
    access: str = "ya-access-1",
    refresh: str | None = "ya-refresh-1",
    expires_in: int | None = 3600,
) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_in=expires_in,
        refresh_token=refresh,
    )


def make_client(
    client_id: str = "client-1",
    client_secret_expires_at: int | None = None,
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret-1",
        client_secret_expires_at=client_secret_expires_at,
        redirect_uris=[AnyUrl(CLIENT_REDIRECT_URI)],
        scope="wiki:read wiki:write",
    )


def make_public_client(client_id: str = "client-1") -> OAuthClientInformationFull:
    """What the SDK's /register builds for token_endpoint_auth_method "none".

    No secret, and so no client_secret_expires_at either: RFC 7591 ties
    that field to an issued secret.
    """
    return OAuthClientInformationFull(
        client_id=client_id,
        token_endpoint_auth_method="none",
        redirect_uris=[AnyUrl(CLIENT_REDIRECT_URI)],
        scope="wiki:read wiki:write",
    )
