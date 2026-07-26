import os

import fakeredis.aioredis
import pytest
import yarl
from mcp.shared.auth import OAuthClientInformationFull

from mcp_wiki.mcp.oauth.provider import YandexOAuthAuthorizationServerProvider
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from tests.mcp.oauth.helpers import make_client


@pytest.fixture
def memory_store() -> InMemoryOAuthStore:
    return InMemoryOAuthStore()


@pytest.fixture
def redis_store() -> RedisOAuthStore:
    store = RedisOAuthStore(encryption_keys=[os.urandom(32)])
    # Swap the real redis client for an in-process fake; the aiocache layer
    # (serializer included) still runs the full code path.
    store._cache.client = fakeredis.aioredis.FakeRedis()  # ty: ignore[unresolved-attribute]
    return store


@pytest.fixture
def client_info() -> OAuthClientInformationFull:
    return make_client()


@pytest.fixture
def provider(
    memory_store: InMemoryOAuthStore,
) -> YandexOAuthAuthorizationServerProvider:
    return YandexOAuthAuthorizationServerProvider(
        client_id="yandex-app-id",
        client_secret="yandex-app-secret",
        server_url=yarl.URL("https://mcp.example.test"),
        yandex_oauth_issuer=yarl.URL("https://oauth.yandex.test"),
        store=memory_store,
        scopes=["wiki:read"],
    )
