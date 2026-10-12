import time
from typing import Any, cast

import aiohttp
import pytest
import yarl
from aioresponses import aioresponses
from mcp.server.auth.provider import AuthorizationParams, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request

import mcp_wiki.mcp.oauth.provider as provider_module
from mcp_wiki.mcp.oauth.provider import (
    YandexOAuthAuthorizationServerProvider,
    YandexOAuthUnavailable,
)
from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS, OAuthStore
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from tests.aioresponses_utils import RequestCapture
from tests.mcp.oauth.helpers import (
    CLIENT_REDIRECT_URI,
    REGISTRATION_IDLE_TTL,
    make_auth_code,
    make_client,
    make_public_client,
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
    assert url.query["scope"] == "wiki:read"

    # The state we hand Yandex is our own unguessable key, never the
    # client's — which is kept as data and echoed back at the end.
    state_id = url.query["state"]
    assert state_id != "state-1"

    state = await memory_store.get_state(state_id)
    assert state is not None
    assert state.client_id == "client-1"
    assert state.code_challenge == "challenge-1"
    assert state.client_state == "state-1"
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
        registration_idle_ttl=None,
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


async def test_a_second_authorization_cannot_hijack_the_first(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    """Reusing another client's `state` must not overwrite its pending record.

    Were the store keyed by the client-supplied `state`, anyone who learned
    it could authorize under the same key with their own redirect_uri and
    code_challenge; the victim's callback would then mint a code bound to
    the attacker and redirect the victim's browser there — PKCE included,
    since the stored challenge would be the attacker's too.
    """
    victim_url = yarl.URL(await provider.authorize(client_info, make_params()))
    attacker = make_client(client_id="client-2")
    await provider.authorize(attacker, make_params())

    victim_state_id = victim_url.query["state"]
    victim_state = await memory_store.get_state(victim_state_id)

    assert victim_state is not None, "the victim's pending record was overwritten"
    assert victim_state.client_id == "client-1"


async def test_client_state_is_echoed_back_not_the_lookup_key(
    provider: YandexOAuthAuthorizationServerProvider,
    client_info: OAuthClientInformationFull,
) -> None:
    url = yarl.URL(await provider.authorize(client_info, make_params()))
    state_id = url.query["state"]

    response = await provider.handle_yandex_callback(
        make_callback_request(f"code=ya-code-1&state={state_id}")
    )

    location = yarl.URL(response.headers["location"])
    assert location.query["state"] == "state-1"


async def test_no_state_is_returned_when_the_client_sent_none(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_state(
        make_state(client_state=None), state_id="key-1", ttl=600
    )

    response = await provider.handle_yandex_callback(
        make_callback_request("code=ya-code-1&state=key-1")
    )

    location = yarl.URL(response.headers["location"])
    assert "state" not in location.query


async def test_load_refresh_token_reads_the_store(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    loaded = await provider.load_refresh_token(client_info, "ya-refresh-1")

    assert loaded is not None
    assert loaded.token == "ya-refresh-1"
    assert await provider.load_refresh_token(client_info, "missing") is None


class TestClientWithoutAnId:
    """Every path that stores by `client_id` checks it is there.

    mcp 2.x made the field required on `OAuthClientInformationFull`, so the
    type system now forbids what these tests build with `model_construct`.
    The checks stay: validation is bypassable, and a client that reached the
    store without an id would key its tokens under `None` and be
    unrecoverable.
    """

    @staticmethod
    def _anonymous() -> OAuthClientInformationFull:
        # Valid in every respect the earlier checks look at, so the failure
        # is the missing id and nothing else.
        return OAuthClientInformationFull.model_construct(
            client_id=cast(str, None),
            redirect_uris=[AnyUrl(CLIENT_REDIRECT_URI)],
            scope="wiki:read wiki:write",
        )

    async def test_authorize_refuses(
        self, provider: YandexOAuthAuthorizationServerProvider
    ) -> None:
        with pytest.raises(ValueError, match="Client ID not provided"):
            await provider.authorize(self._anonymous(), make_params())

    async def test_exchanging_an_authorization_code_refuses(
        self, provider: YandexOAuthAuthorizationServerProvider
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            with pytest.raises(ValueError, match="client_id must be provided"):
                await provider.exchange_authorization_code(
                    self._anonymous(), make_auth_code()
                )

    async def test_refreshing_refuses(
        self, provider: YandexOAuthAuthorizationServerProvider
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            with pytest.raises(ValueError, match="client_id must be provided"):
                await provider.exchange_refresh_token(
                    self._anonymous(),
                    RefreshToken(
                        token="ya-refresh-1", client_id="client-1", scopes=["wiki:read"]
                    ),
                    ["wiki:read"],
                )


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
    # TokenError is the one exception the SDK's /token handler turns into an
    # OAuth error response; anything else reaches the client as a bare 500.
    with aioresponses() as mocked:
        mocked.post(
            TOKEN_URL,
            status=400,
            payload={"error": "invalid_grant", "error_description": "Code has expired"},
        )
        with pytest.raises(TokenError) as raised:
            await provider.exchange_authorization_code(client_info, make_auth_code())

    assert raised.value.error == "invalid_grant"
    assert raised.value.error_description == (
        "Yandex OAuth refused the authorization code: "
        "HTTP 400 invalid_grant: Code has expired"
    )
    assert await memory_store.get_access_token("ya-access-1") is None


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # A proxy in front of Yandex answering with its own page.
        (502, "<html>Bad Gateway</html>"),
        (400, ""),
        (400, '{"message": "not an OAuth error"}'),
        # Transient, unlike on a refresh, still invalid_grant: our mcp_ code
        # is spent either way, so there is nothing left to retry.
        (503, ""),
    ],
    ids=["html", "empty", "json-without-oauth-fields", "unavailable"],
)
async def test_exchange_failure_without_an_oauth_error_body_is_still_one(
    provider: YandexOAuthAuthorizationServerProvider,
    client_info: OAuthClientInformationFull,
    status: int,
    body: str,
) -> None:
    with aioresponses() as mocked:
        mocked.post(TOKEN_URL, status=status, body=body)
        with pytest.raises(TokenError) as raised:
            await provider.exchange_authorization_code(client_info, make_auth_code())

    assert raised.value.error == "invalid_grant"
    assert raised.value.error_description == (
        f"Yandex OAuth refused the authorization code: HTTP {status}"
    )


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
        mocked.post(TOKEN_URL, status=400, payload={"error": "invalid_grant"})
        with pytest.raises(TokenError) as raised:
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

    assert raised.value.error == "invalid_grant"
    assert raised.value.error_description == (
        "Yandex OAuth refused the refresh token: HTTP 400 invalid_grant"
    )
    assert await memory_store.get_refresh_token("old-refresh") is not None
    assert await memory_store.get_access_token("old-access") is not None


class TestRefreshOrdering:
    """A refresh stores the new pair before it lets go of the old token."""

    @pytest.fixture
    async def refresh(self, memory_store: InMemoryOAuthStore) -> RefreshToken:
        await memory_store.save_oauth_token(
            make_token(access="old-access", refresh="old-refresh"),
            "client-1",
            ["wiki:read"],
            None,
        )
        refresh = await memory_store.get_refresh_token("old-refresh")
        assert refresh is not None
        return refresh

    async def test_a_failed_save_leaves_the_old_refresh_token_usable(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        client_info: OAuthClientInformationFull,
        refresh: RefreshToken,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Revoked first, the old token was gone with nothing in its place:
        # the client got a 500 and could not even retry.
        async def unavailable(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(memory_store, "save_oauth_token", unavailable)
        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            with pytest.raises(ConnectionError):
                await provider.exchange_refresh_token(client_info, refresh, scopes=[])

        assert await memory_store.get_refresh_token("old-refresh") is not None

    async def test_a_refresh_token_handed_back_is_not_revoked(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        client_info: OAuthClientInformationFull,
        refresh: RefreshToken,
    ) -> None:
        # The record just saved is the old token renewed; revoking "the old
        # one" would delete it.
        with aioresponses() as mocked:
            mocked.post(
                TOKEN_URL, payload={**TOKEN_PAYLOAD, "refresh_token": "old-refresh"}
            )
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

        assert await memory_store.get_refresh_token("old-refresh") is not None
        assert await memory_store.get_access_token("ya-access-1") is not None

    async def test_an_access_token_handed_back_survives_the_cleanup(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        client_info: OAuthClientInformationFull,
        refresh: RefreshToken,
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(
                TOKEN_URL, payload={**TOKEN_PAYLOAD, "access_token": "old-access"}
            )
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

        assert await memory_store.get_refresh_token("old-refresh") is None
        assert await memory_store.get_access_token("old-access") is not None

    async def test_no_new_refresh_token_keeps_the_old_one(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        client_info: OAuthClientInformationFull,
        refresh: RefreshToken,
    ) -> None:
        # RFC 6749 §6: without a new one the client keeps using the old one.
        payload = {k: v for k, v in TOKEN_PAYLOAD.items() if k != "refresh_token"}
        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=payload)
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

        assert await memory_store.get_refresh_token("old-refresh") is not None
        assert await memory_store.get_access_token("ya-access-1") is not None

    @pytest.mark.parametrize("store_kind", ["memory", "redis"])
    @pytest.mark.parametrize(
        ("new_refresh", "new_access"),
        [
            ("ya-refresh-1", "ya-access-1"),
            ("old-refresh", "ya-access-1"),
            (None, "ya-access-1"),
            ("ya-refresh-1", "old-access"),
        ],
        ids=["both-new", "refresh-handed-back", "no-refresh", "access-handed-back"],
    )
    async def test_every_shape_of_answer_leaves_one_live_pair(
        self,
        memory_store: InMemoryOAuthStore,
        redis_store: RedisOAuthStore,
        client_info: OAuthClientInformationFull,
        store_kind: str,
        new_refresh: str | None,
        new_access: str,
    ) -> None:
        # Whatever Yandex hands back, the replaced access token stops working
        # and the refresh token the client now holds leads to the new one —
        # so revoking it later takes the new access token with it.
        store: OAuthStore = memory_store if store_kind == "memory" else redis_store
        provider = make_provider(store, registration_idle_ttl=REGISTRATION_IDLE_TTL)
        await store.save_oauth_token(
            make_token(access="old-access", refresh="old-refresh"),
            "client-1",
            ["wiki:read"],
            None,
        )
        refresh = await store.get_refresh_token("old-refresh")
        assert refresh is not None
        payload: dict[str, Any] = {**TOKEN_PAYLOAD, "access_token": new_access}
        if new_refresh is None:
            del payload["refresh_token"]
        else:
            payload["refresh_token"] = new_refresh

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=payload)
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

        held = new_refresh or "old-refresh"
        assert await store.get_access_token(new_access) is not None
        assert await store.get_refresh_token(held) is not None
        if new_access != "old-access":
            assert await store.get_access_token("old-access") is None
        if held != "old-refresh":
            assert await store.get_refresh_token("old-refresh") is None

        await store.revoke_refresh_token(held)
        assert await store.get_access_token(new_access) is None

    async def test_a_failed_revoke_does_not_fail_the_grant(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        client_info: OAuthClientInformationFull,
        refresh: RefreshToken,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def unavailable(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(memory_store, "revoke_refresh_token", unavailable)
        with aioresponses() as mocked, caplog.at_level("WARNING"):
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            token = await provider.exchange_refresh_token(
                client_info, refresh, scopes=[]
            )

        assert token.access_token == "ya-access-1"
        assert await memory_store.get_access_token("ya-access-1") is not None
        assert "Could not revoke a refresh token after a refresh" in caplog.text


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, '{"error": "temporarily_unavailable"}'),
        (429, ""),
        # A proxy in front of Yandex answering with its own page.
        (502, "<html>Bad Gateway</html>"),
    ],
    ids=["unavailable", "rate-limited", "proxy-page"],
)
async def test_a_refresh_yandex_could_not_take_is_not_invalid_grant(
    provider: YandexOAuthAuthorizationServerProvider,
    memory_store: InMemoryOAuthStore,
    client_info: OAuthClientInformationFull,
    status: int,
    body: str,
) -> None:
    # invalid_grant would tell the client its refresh token is dead, and a
    # client following RFC 6749 would throw it away. It is not: it is still
    # here, still valid at Yandex, and the next attempt can succeed.
    await memory_store.save_oauth_token(
        make_token(access="old-access", refresh="old-refresh"),
        "client-1",
        ["wiki:read"],
        None,
    )
    refresh = await memory_store.get_refresh_token("old-refresh")
    assert refresh is not None

    with aioresponses() as mocked:
        mocked.post(TOKEN_URL, status=status, body=body)
        with pytest.raises(YandexOAuthUnavailable, match=f"HTTP {status}"):
            await provider.exchange_refresh_token(client_info, refresh, scopes=[])

    assert await memory_store.get_refresh_token("old-refresh") is not None
    assert await memory_store.get_access_token("old-access") is not None


class _FakeRefusal:
    """Just enough of an aiohttp response for _describe_refusal."""

    status = 400

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def json(self, content_type: str | None = None) -> Any:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientPayloadError("Response payload is not completed"),
        aiohttp.ClientConnectionError("Connection reset by peer"),
        TimeoutError(),
        # aiohttp decodes with the charset the response names.
        LookupError("'rot-13' is not a text encoding"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ],
    ids=["truncated-body", "connection-lost", "timeout", "charset", "not-json"],
)
async def test_a_refusal_whose_body_cannot_be_read_is_described_by_its_status(
    error: Exception,
) -> None:
    # Escaping here would turn the refusal into the SDK's bare 500.
    response = cast(aiohttp.ClientResponse, _FakeRefusal(error))

    assert await provider_module._describe_refusal(response) == "HTTP 400"


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


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    # provider_module.time is the time module itself, so the store reads
    # this clock as well.
    fake = FakeClock()
    monkeypatch.setattr(provider_module.time, "time", fake)
    return fake


def make_provider(
    store: OAuthStore, registration_idle_ttl: int | None
) -> YandexOAuthAuthorizationServerProvider:
    return YandexOAuthAuthorizationServerProvider(
        client_id="yandex-app-id",
        client_secret="yandex-app-secret",
        server_url=yarl.URL("https://mcp.example.test"),
        yandex_oauth_issuer=yarl.URL("https://oauth.yandex.test"),
        store=store,
        registration_idle_ttl=registration_idle_ttl,
        scopes=["wiki:read"],
    )


def deadline(store: InMemoryOAuthStore, client_id: str = "client-1") -> float | None:
    return store._dynamic_clients[client_id].expires_at


class TestRegistrationLifetime:
    """How long a registration is kept. /register is unauthenticated, so
    whatever it keeps forever is a way to fill the store on purpose."""

    @pytest.mark.parametrize(
        "client",
        [make_public_client(), make_client(client_secret_expires_at=0)],
        ids=["public", "confidential"],
    )
    async def test_every_registration_gets_the_idle_window(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
        client: OAuthClientInformationFull,
    ) -> None:
        # One rule, whether the SDK issued a secret or not: without it the
        # record would be kept forever, and a secret's own deadline cut off
        # clients in daily use.
        await provider.register_client(client)

        assert deadline(memory_store) == clock.now + REGISTRATION_IDLE_TTL

    async def test_nothing_expires_with_the_expiry_switched_off(
        self, memory_store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        provider = make_provider(memory_store, registration_idle_ttl=None)

        await provider.register_client(make_public_client())

        assert deadline(memory_store) is None


class TestRegistrationRenewal:
    """A token grant restarts a registration's idle window.

    A fixed lifetime would retire a registration in active use: the next
    refresh gets invalid_client, and the SDK's own client then reopens
    /authorize with the dead client_id instead of registering again.
    """

    async def test_a_code_exchange_restarts_the_window(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
    ) -> None:
        await provider.register_client(make_public_client())
        clock.advance(REGISTRATION_IDLE_TTL - 1)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(
                make_public_client(), make_auth_code()
            )

        # A whole window more, counted from the end of the refresh token,
        # the longer-lived of the two.
        assert deadline(memory_store) == (
            clock.now + REFRESH_TOKEN_TTL_SECONDS + REGISTRATION_IDLE_TTL
        )

    async def test_a_refresh_restarts_the_window(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
    ) -> None:
        await provider.register_client(make_public_client())
        await memory_store.save_oauth_token(
            make_token(access="old-access", refresh="old-refresh"),
            "client-1",
            ["wiki:read"],
            None,
        )
        refresh = await memory_store.get_refresh_token("old-refresh")
        assert refresh is not None
        clock.advance(REGISTRATION_IDLE_TTL - 1)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_refresh_token(
                make_public_client(), refresh, scopes=[]
            )

        assert deadline(memory_store) == (
            clock.now + REFRESH_TOKEN_TTL_SECONDS + REGISTRATION_IDLE_TTL
        )

    async def test_the_registration_outlives_the_refresh_token_it_was_issued(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
    ) -> None:
        # Had the registration gone first, a refresh that could succeed would
        # get invalid_client; had it gone with the token, a client coming
        # back after it would find no registration to sign in again with.
        await provider.register_client(make_public_client())

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(
                make_public_client(), make_auth_code()
            )

        refresh = await memory_store.get_refresh_token("ya-refresh-1")
        assert refresh is not None
        assert refresh.expires_at is not None
        registration_deadline = deadline(memory_store)
        assert registration_deadline is not None
        assert registration_deadline == refresh.expires_at + REGISTRATION_IDLE_TTL

        # Back the day after the refresh token died: told invalid_grant by
        # the SDK (no such token), it signs in again with this registration.
        clock.advance(REFRESH_TOKEN_TTL_SECONDS + 24 * 60 * 60 - 1)
        assert await memory_store.get_refresh_token("ya-refresh-1") is None
        assert await provider.get_client("client-1") is not None

    async def test_a_window_longer_than_the_refresh_token_is_kept_whole(
        self, memory_store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        idle = 2 * REFRESH_TOKEN_TTL_SECONDS
        provider = make_provider(memory_store, registration_idle_ttl=idle)
        await provider.register_client(make_public_client())

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(
                make_public_client(), make_auth_code()
            )

        assert deadline(memory_store) == clock.now + REFRESH_TOKEN_TTL_SECONDS + idle

    async def test_a_refused_grant_renews_nothing(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
    ) -> None:
        await provider.register_client(make_public_client())
        registered_until = deadline(memory_store)
        clock.advance(60)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, status=400, payload={"error": "invalid_grant"})
            with pytest.raises(TokenError):
                await provider.exchange_authorization_code(
                    make_public_client(), make_auth_code()
                )

        assert deadline(memory_store) == registered_until

    async def test_a_confidential_registration_renews_like_a_public_one(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
    ) -> None:
        client = make_client(client_secret_expires_at=0)
        await provider.register_client(client)
        clock.advance(REGISTRATION_IDLE_TTL - 1)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(client, make_auth_code())

        assert deadline(memory_store) == (
            clock.now + REFRESH_TOKEN_TTL_SECONDS + REGISTRATION_IDLE_TTL
        )

    @pytest.mark.parametrize("store_kind", ["memory", "redis"])
    async def test_a_secret_deadline_from_before_is_lifted_at_the_next_grant(
        self,
        memory_store: InMemoryOAuthStore,
        redis_store: RedisOAuthStore,
        store_kind: str,
    ) -> None:
        # Registered while secrets still expired: the record carries the
        # deadline it was stamped with, which the SDK enforces whatever the
        # store keeps, and its TTL in the store is that deadline. A client
        # still in use moves to the idle rule at its first grant.
        store: OAuthStore = memory_store if store_kind == "memory" else redis_store
        provider = make_provider(store, registration_idle_ttl=REGISTRATION_IDLE_TTL)
        stamped = make_client(client_secret_expires_at=int(time.time()) + 600)
        await store.save_client(stamped, ttl=600)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(stamped, make_auth_code())

        saved = await provider.get_client("client-1")
        assert saved is not None
        assert saved.client_secret_expires_at == 0
        assert saved.client_secret == stamped.client_secret

    @pytest.mark.parametrize("grant", ["code", "refresh"])
    async def test_the_registration_outlives_a_long_lived_access_token(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
        grant: str,
    ) -> None:
        # Yandex ID tokens commonly last a year. A client spending one sends
        # only bearer requests, which renew nothing, and comes back to /token
        # only after the token has expired — by its own clock, which started
        # when the reply arrived. A window ending with the token would be
        # gone by then, so it runs one idle window past the token's expiry.
        year = 365 * 24 * 60 * 60
        await provider.register_client(make_public_client())
        await memory_store.save_oauth_token(
            make_token(access="old-access", refresh="old-refresh"),
            "client-1",
            ["wiki:read"],
            None,
        )

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload={**TOKEN_PAYLOAD, "expires_in": year})
            if grant == "code":
                await provider.exchange_authorization_code(
                    make_public_client(), make_auth_code()
                )
            else:
                refresh = await memory_store.get_refresh_token("old-refresh")
                assert refresh is not None
                await provider.exchange_refresh_token(
                    make_public_client(), refresh, scopes=[]
                )

        access = await memory_store.get_access_token("ya-access-1")
        assert access is not None
        assert access.expires_at == clock.now + year
        assert deadline(memory_store) == clock.now + year + REGISTRATION_IDLE_TTL

        # The day the client first notices its token expired: the refresh
        # token is long gone, so it is told invalid_grant and signs in again
        # — with a registration that is still there.
        clock.advance(year + 60)
        assert await provider.get_client("client-1") is not None
        assert await memory_store.get_refresh_token("ya-refresh-1") is None

    async def test_switching_the_expiry_off_clears_an_earlier_window(
        self, memory_store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        # Registered while the expiry was on, renewed after it was switched
        # off: "registrations then live indefinitely" has to reach this one.
        await memory_store.save_client(make_public_client(), ttl=REGISTRATION_IDLE_TTL)
        provider = make_provider(memory_store, registration_idle_ttl=None)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(
                make_public_client(), make_auth_code()
            )

        assert deadline(memory_store) is None

    async def test_switching_the_expiry_off_clears_a_redis_ttl(
        self, redis_store: RedisOAuthStore
    ) -> None:
        # The case that matters: Redis keeps a TTL across restarts, and only
        # a save without one (a plain SET) takes it off the key.
        await redis_store.save_client(make_public_client(), ttl=REGISTRATION_IDLE_TTL)
        provider = make_provider(redis_store, registration_idle_ttl=None)

        with aioresponses() as mocked:
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            await provider.exchange_authorization_code(
                make_public_client(), make_auth_code()
            )

        redis = redis_store._cache.client  # ty: ignore[unresolved-attribute]
        assert await redis.ttl("oauth:client:client-1") == -1
        assert await provider.get_client("client-1") is not None

    async def test_a_failed_renewal_does_not_fail_the_grant(
        self,
        provider: YandexOAuthAuthorizationServerProvider,
        memory_store: InMemoryOAuthStore,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # By the time the renewal runs, the old refresh token is revoked and
        # the new pair saved. Failing the grant then would hand the client a
        # 500 instead of tokens it can no longer get any other way.
        await provider.register_client(make_public_client())
        await memory_store.save_oauth_token(
            make_token(access="old-access", refresh="old-refresh"),
            "client-1",
            ["wiki:read"],
            None,
        )
        refresh = await memory_store.get_refresh_token("old-refresh")
        assert refresh is not None

        async def unavailable(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(memory_store, "save_client", unavailable)
        with aioresponses() as mocked, caplog.at_level("WARNING"):
            mocked.post(TOKEN_URL, payload=TOKEN_PAYLOAD)
            token = await provider.exchange_refresh_token(
                make_public_client(), refresh, scopes=[]
            )

        assert token.access_token == "ya-access-1"
        assert await memory_store.get_access_token("ya-access-1") is not None
        assert "Could not renew the registration of client client-1" in caplog.text
