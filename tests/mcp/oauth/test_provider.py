from typing import Any

import aiohttp
import pytest
import yarl
from aioresponses import aioresponses
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request

from mcp_wiki.mcp.oauth.provider import YandexOAuthAuthorizationServerProvider
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from tests.aioresponses_utils import RequestCapture
from tests.mcp.oauth.helpers import (
    CLIENT_REDIRECT_URI,
    make_auth_code,
    make_state,
    make_token,
)

TOKEN_URL = "https://oauth.yandex.test/token"
CALLBACK_URL = "https://mcp.example.test/oauth/yandex/callback"

TOKEN_PAYLOAD = {
    "access_token": "ya-access-1",
    "token_type": "bearer",
    "expires_in": 3600,
    "refresh_token": "ya-refresh-1",
}


def make_params(state: str | None = "state-1") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["wiki:read"],
        code_challenge="challenge-1",
        redirect_uri=AnyUrl(CLIENT_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def make_callback_request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/oauth/yandex/callback",
            "query_string": query.encode(),
            "headers": [],
        }
    )


def form_fields(data: Any) -> dict[str, str]:
    assert isinstance(data, aiohttp.FormData)
    return {opts["name"]: str(value) for opts, _headers, value in data._fields}


async def test_authorize_redirects_to_yandex_and_saves_state(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    url = yarl.URL(await provider.authorize(client_info, make_params()))

    assert str(url.with_query(None)) == "https://oauth.yandex.test/authorize"
    assert url.query["response_type"] == "code"
    assert url.query["client_id"] == "yandex-app-id"
    assert url.query["redirect_uri"] == CALLBACK_URL
    assert url.query["state"] == "state-1"
    assert url.query["scope"] == "wiki:read"

    state = await memory_store.get_state("state-1")
    assert state is not None
    assert state.client_id == "client-1"
    assert state.code_challenge == "challenge-1"
    assert str(state.redirect_uri) == CLIENT_REDIRECT_URI


async def test_authorize_generates_state_id_when_client_sends_none(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    url = yarl.URL(await provider.authorize(client_info, make_params(state=None)))

    state_id = url.query["state"]
    assert state_id
    assert await memory_store.get_state(state_id) is not None


async def test_authorize_without_scopes(
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    provider = YandexOAuthAuthorizationServerProvider(
        client_id="yandex-app-id",
        client_secret="yandex-app-secret",
        server_url=yarl.URL("https://mcp.example.test"),
        yandex_oauth_issuer=yarl.URL("https://oauth.yandex.test"),
        store=memory_store,
        use_scopes=False,
    )

    url = yarl.URL(await provider.authorize(client_info, make_params()))
    assert "scope" not in url.query


async def test_callback_mints_code_and_redirects_to_client(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_state(make_state(), state_id="state-1", ttl=600)

    response = await provider.handle_yandex_callback(
        make_callback_request("code=ya-code-1&state=state-1")
    )

    assert response.status_code == 302
    assert response.headers["Cache-Control"] == "no-store"

    location = yarl.URL(response.headers["location"])
    assert str(location.with_query(None)) == CLIENT_REDIRECT_URI
    assert location.query["state"] == "state-1"
    mcp_code = location.query["code"]
    assert mcp_code.startswith("mcp_")

    auth_code = await memory_store.get_auth_code(mcp_code)
    assert auth_code is not None
    assert auth_code.yandex_auth_code == "ya-code-1"
    assert auth_code.client_id == "client-1"
    assert auth_code.code_challenge == "challenge-1"
    assert auth_code.scopes == ["wiki:read"]

    # State is single-use and must be consumed by the callback
    assert await memory_store.get_state("state-1") is None


async def test_callback_rejects_unknown_state(
    provider: YandexOAuthAuthorizationServerProvider,
) -> None:
    response = await provider.handle_yandex_callback(
        make_callback_request("code=ya-code-1&state=missing")
    )
    assert response.status_code == 400


async def test_callback_rejects_malformed_query(
    provider: YandexOAuthAuthorizationServerProvider,
) -> None:
    response = await provider.handle_yandex_callback(
        make_callback_request("state=state-without-code")
    )
    assert response.status_code == 400


async def test_load_authorization_code(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    code = make_auth_code()
    await memory_store.save_auth_code(code, ttl=300)

    assert await provider.load_authorization_code(client_info, code.code) == code
    assert await provider.load_authorization_code(client_info, "missing") is None


async def test_exchange_authorization_code(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    capture = RequestCapture(payload=TOKEN_PAYLOAD)
    with aioresponses() as mocked:
        mocked.post(TOKEN_URL, callback=capture.callback)
        token = await provider.exchange_authorization_code(
            client_info, make_auth_code()
        )

    assert token.access_token == "ya-access-1"

    fields = form_fields(capture.last_request.data)
    assert fields["grant_type"] == "authorization_code"
    assert fields["code"] == "ya-code-1"
    assert fields["client_id"] == "yandex-app-id"
    assert fields["client_secret"] == "yandex-app-secret"
    assert fields["redirect_uri"] == CALLBACK_URL

    access = await memory_store.get_access_token("ya-access-1")
    assert access is not None
    assert access.client_id == "client-1"
    assert access.scopes == ["wiki:read"]
    assert await memory_store.get_refresh_token("ya-refresh-1") is not None


async def test_exchange_authorization_code_failure(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    with aioresponses() as mocked:
        mocked.post(TOKEN_URL, status=400)
        with pytest.raises(ValueError, match="Failed to exchange"):
            await provider.exchange_authorization_code(client_info, make_auth_code())

    assert await memory_store.get_access_token("ya-access-1") is None


async def test_exchange_refresh_token_rotates_tokens(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    await memory_store.save_oauth_token(
        make_token(access="old-access", refresh="old-refresh"),
        "client-1",
        ["wiki:read"],
        None,
    )
    refresh = await memory_store.get_refresh_token("old-refresh")
    assert refresh is not None

    payload = {
        "access_token": "new-access",
        "token_type": "bearer",
        "expires_in": 3600,
        "refresh_token": "new-refresh",
    }
    capture = RequestCapture(payload=payload)
    with aioresponses() as mocked:
        mocked.post(TOKEN_URL, callback=capture.callback)
        token = await provider.exchange_refresh_token(client_info, refresh, scopes=[])

    assert token.access_token == "new-access"

    fields = form_fields(capture.last_request.data)
    assert fields["grant_type"] == "refresh_token"
    assert fields["refresh_token"] == "old-refresh"

    assert await memory_store.get_refresh_token("old-refresh") is None
    assert await memory_store.get_access_token("old-access") is None
    assert await memory_store.get_access_token("new-access") is not None
    assert await memory_store.get_refresh_token("new-refresh") is not None


async def test_exchange_refresh_token_failure_keeps_old_tokens(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    await memory_store.save_oauth_token(
        make_token(access="old-access", refresh="old-refresh"),
        "client-1",
        ["wiki:read"],
        None,
    )
    refresh = await memory_store.get_refresh_token("old-refresh")
    assert refresh is not None

    with aioresponses() as mocked:
        mocked.post(TOKEN_URL, status=400)
        with pytest.raises(ValueError, match="Failed to refresh"):
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

    assert await memory_store.get_refresh_token("old-refresh") is not None
    assert await memory_store.get_access_token("old-access") is not None


async def test_load_access_token(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    access = await provider.load_access_token("ya-access-1")
    assert access is not None
    assert access.client_id == "client-1"
    assert await provider.load_access_token("missing") is None


async def test_revoke_access_token_keeps_refresh(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)
    access = await memory_store.get_access_token("ya-access-1")
    assert access is not None

    await provider.revoke_token(access)

    assert await memory_store.get_access_token("ya-access-1") is None
    assert await memory_store.get_refresh_token("ya-refresh-1") is not None


async def test_revoke_refresh_token_cascades(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)
    refresh = await memory_store.get_refresh_token("ya-refresh-1")
    assert refresh is not None

    await provider.revoke_token(refresh)

    assert await memory_store.get_refresh_token("ya-refresh-1") is None
    assert await memory_store.get_access_token("ya-access-1") is None


async def test_client_registration_roundtrip(
    provider: YandexOAuthAuthorizationServerProvider,
    client_info: OAuthClientInformationFull,
) -> None:
    await provider.register_client(client_info)
    assert await provider.get_client("client-1") == client_info
    assert await provider.get_client("missing") is None
