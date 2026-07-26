from typing import Any

import fakeredis.aioredis
import pytest

from mcp_wiki.mcp.oauth.stores.crypto import hash_token
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from tests.mcp.oauth.helpers import (
    make_auth_code,
    make_client,
    make_state,
    make_token,
)

REFRESH_TTL = 31 * 24 * 60 * 60


def raw_client(store: RedisOAuthStore) -> Any:
    return store._cache.client  # ty: ignore[unresolved-attribute]


async def test_client_roundtrip_and_secret_encrypted_at_rest(
    redis_store: RedisOAuthStore,
) -> None:
    client = make_client()
    await redis_store.save_client(client)

    assert await redis_store.get_client("client-1") == client

    raw = await raw_client(redis_store).get("oauth:client:client-1")
    assert raw is not None
    assert b"client-secret-1" not in raw


async def test_get_client_missing(redis_store: RedisOAuthStore) -> None:
    assert await redis_store.get_client("missing") is None


async def test_state_single_use_with_ttl(redis_store: RedisOAuthStore) -> None:
    await redis_store.save_state(make_state(), state_id="state-1", ttl=600)

    ttl = await raw_client(redis_store).ttl("oauth:state:state-1")
    assert 0 < ttl <= 600

    assert await redis_store.get_state("state-1") == make_state()
    assert await redis_store.get_state("state-1") is None
    assert await raw_client(redis_store).exists("oauth:state:state-1") == 0


async def test_auth_code_single_use(redis_store: RedisOAuthStore) -> None:
    await redis_store.save_auth_code(make_auth_code(), ttl=300)

    assert await redis_store.get_auth_code("mcp_code-1") == make_auth_code()
    assert await redis_store.get_auth_code("mcp_code-1") is None


async def test_save_token_requires_expires_in(redis_store: RedisOAuthStore) -> None:
    with pytest.raises(ValueError, match="expires_in"):
        await redis_store.save_oauth_token(
            make_token(expires_in=None), "client-1", ["wiki:read"], None
        )


async def test_token_saved_encrypted_with_ttls(redis_store: RedisOAuthStore) -> None:
    await redis_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)
    client = raw_client(redis_store)

    access_key = f"oauth:access:{hash_token('ya-access-1')}"
    raw = await client.get(access_key)
    assert raw is not None
    assert b"ya-access-1" not in raw
    assert 0 < await client.ttl(access_key) <= 3600

    refresh_key = f"oauth:refresh:{hash_token('ya-refresh-1')}"
    assert 0 < await client.ttl(refresh_key) <= REFRESH_TTL

    # Regression: the refresh->access mapping must expire together with the
    # refresh token instead of living in Redis forever.
    mapping_key = f"oauth:mapping:{hash_token('ya-refresh-1')}"
    assert 0 < await client.ttl(mapping_key) <= REFRESH_TTL

    access = await redis_store.get_access_token("ya-access-1")
    assert access is not None
    assert access.token == "ya-access-1"
    assert access.client_id == "client-1"
    assert access.scopes == ["wiki:read"]

    refresh = await redis_store.get_refresh_token("ya-refresh-1")
    assert refresh is not None
    assert refresh.token == "ya-refresh-1"
    assert refresh.client_id == "client-1"


async def test_token_without_refresh(redis_store: RedisOAuthStore) -> None:
    await redis_store.save_oauth_token(
        make_token(refresh=None), "client-1", ["wiki:read"], None
    )

    assert await redis_store.get_access_token("ya-access-1") is not None
    assert await redis_store.get_refresh_token("ya-refresh-1") is None


async def test_get_missing_tokens(redis_store: RedisOAuthStore) -> None:
    assert await redis_store.get_access_token("missing") is None
    assert await redis_store.get_refresh_token("missing") is None


async def test_revoke_refresh_token_cascades_to_access(
    redis_store: RedisOAuthStore,
) -> None:
    await redis_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    await redis_store.revoke_refresh_token("ya-refresh-1")

    assert await redis_store.get_refresh_token("ya-refresh-1") is None
    assert await redis_store.get_access_token("ya-access-1") is None
    mapping_key = f"oauth:mapping:{hash_token('ya-refresh-1')}"
    assert await raw_client(redis_store).exists(mapping_key) == 0


async def test_revoke_access_token_keeps_refresh(
    redis_store: RedisOAuthStore,
) -> None:
    await redis_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    await redis_store.revoke_access_token("ya-access-1")

    assert await redis_store.get_access_token("ya-access-1") is None
    assert await redis_store.get_refresh_token("ya-refresh-1") is not None


async def test_revoke_unknown_tokens_is_noop(redis_store: RedisOAuthStore) -> None:
    await redis_store.revoke_refresh_token("unknown")
    await redis_store.revoke_access_token("unknown")


async def test_plaintext_without_encryption_keys() -> None:
    store = RedisOAuthStore()
    store._cache.client = fakeredis.aioredis.FakeRedis()  # ty: ignore[unresolved-attribute]

    await store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    raw = await raw_client(store).get(f"oauth:access:{hash_token('ya-access-1')}")
    assert raw is not None
    assert b"ya-access-1" in raw

    access = await store.get_access_token("ya-access-1")
    assert access is not None
    assert access.token == "ya-access-1"
