"""The OAuth flow over real HTTP, through the SDK's own handlers.

What a client receives from /token, and what /register stamps on the record
a store keeps, are the SDK's decisions; only a request through it can pin
them. This builds the real app with create_mcp_server() and walks the flow
the way a client does.
"""

import base64
import hashlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, patch

import fakeredis
import fakeredis.aioredis
import httpx
import yarl
from pydantic import AnyHttpUrl, SecretStr
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from mcp_wiki.mcp.server import create_mcp_server, http_app_options
from tests.mcp.conftest import create_test_settings, make_test_lifespan
from tests.mcp.oauth.helpers import CLIENT_REDIRECT_URI

# The server's default.
EXPIRY = 30 * 24 * 60 * 60
YANDEX_TOKEN_URL = "https://oauth.yandex.test/token"
CODE_VERIFIER = "verifier-" + "x" * 50
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)
YANDEX_TOKENS = {
    "access_token": "ya-access-1",
    "token_type": "bearer",
    "expires_in": 3600,
    "refresh_token": "ya-refresh-1",
}

StoreKind = Literal["memory", "redis"]


@dataclass
class OAuthApp:
    http: TestClient
    remaining: Callable[[str], float | None]
    """Seconds a registration has left in the store, or None for never."""


@contextmanager
def oauth_app(
    store: StoreKind = "memory", expiry: int | None = EXPIRY
) -> Iterator[OAuthApp]:
    settings = create_test_settings()
    settings.oauth_enabled = True
    settings.oauth_client_id = "yandex-app-id"
    settings.oauth_client_secret = SecretStr("yandex-app-secret")
    settings.oauth_server_url = AnyHttpUrl("https://oauth.yandex.test")
    settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.test")
    settings.oauth_client_secret_expiry_seconds = expiry
    settings.oauth_store = store
    settings.oauth_encryption_keys = SecretStr(
        base64.b64encode(os.urandom(32)).decode()
    )

    # The stores are built inside create_mcp_server(); these subclasses are
    # how a test gets hold of the one the app ended up with.
    built: list[Any] = []
    redis_server = fakeredis.FakeServer()

    class SeenMemoryStore(InMemoryOAuthStore):
        def __init__(self) -> None:
            super().__init__()
            built.append(self)

    class FakeRedisStore(RedisOAuthStore):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._cache.client = fakeredis.aioredis.FakeRedis(server=redis_server)  # ty: ignore[unresolved-attribute]

    def memory_remaining(client_id: str) -> float | None:
        expires_at = built[0]._dynamic_clients[client_id].expires_at
        return None if expires_at is None else expires_at - time.time()

    def redis_remaining(client_id: str) -> float | None:
        # redis-py types every reply as maybe-awaitable; the sync client's is not.
        ttl = cast(
            int,
            fakeredis.FakeRedis(server=redis_server).ttl(f"oauth:client:{client_id}"),
        )
        assert ttl != -2, "the registration is not in Redis at all"
        return None if ttl == -1 else ttl

    with (
        patch("mcp_wiki.mcp.server.InMemoryOAuthStore", SeenMemoryStore),
        patch("mcp_wiki.mcp.server.RedisOAuthStore", FakeRedisStore),
    ):
        server = create_mcp_server(
            settings=settings,
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

    # A provider exception the SDK does not handle must surface as the 500
    # the client would see, not be re-raised into the test.
    app = server.streamable_http_app(**http_app_options(settings))
    with TestClient(app, raise_server_exceptions=False) as http:
        yield OAuthApp(
            http=http,
            remaining=memory_remaining if store == "memory" else redis_remaining,
        )


def register(
    http: TestClient, auth_method: str = "client_secret_post"
) -> dict[str, Any]:
    response = http.post(
        "/register",
        json={
            "redirect_uris": [CLIENT_REDIRECT_URI],
            "token_endpoint_auth_method": auth_method,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def authorize(http: TestClient, registration: dict[str, Any]) -> str:
    """Walk /authorize and the Yandex callback; return the client's mcp_ code."""
    to_yandex = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": CLIENT_REDIRECT_URI,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state",
        },
        follow_redirects=False,
    )
    assert to_yandex.status_code == 302, to_yandex.text
    state_id = yarl.URL(to_yandex.headers["location"]).query["state"]

    to_client = http.get(
        "/oauth/yandex/callback",
        params={"code": "ya-code-1", "state": state_id},
        follow_redirects=False,
    )
    assert to_client.status_code == 302, to_client.text
    return yarl.URL(to_client.headers["location"]).query["code"]


def client_credentials(registration: dict[str, Any]) -> dict[str, str]:
    """What a client authenticates /token with: a public one sends no secret."""
    credentials = {"client_id": registration["client_id"]}
    if registration.get("client_secret"):
        credentials["client_secret"] = registration["client_secret"]
    return credentials


def redeem_code(
    http: TestClient, registration: dict[str, Any], code: str
) -> httpx.Response:
    return http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT_URI,
            "code_verifier": CODE_VERIFIER,
            **client_credentials(registration),
        },
    )
