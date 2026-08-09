import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import mcp_wiki.mcp.oauth.stores.memory as memory_module
from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from tests.mcp.oauth.helpers import (
    CLIENT_REDIRECT_URI,
    make_auth_code,
    make_client,
    make_state,
    make_token,
)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(memory_module.time, "time", fake)
    return fake


async def test_client_roundtrip(memory_store: InMemoryOAuthStore) -> None:
    client = make_client()
    await memory_store.save_client(client)
    assert await memory_store.get_client("client-1") == client
    assert await memory_store.get_client("missing") is None


async def test_save_client_requires_id(memory_store: InMemoryOAuthStore) -> None:
    client = OAuthClientInformationFull.model_construct(
        client_id=None, redirect_uris=[AnyUrl(CLIENT_REDIRECT_URI)]
    )
    with pytest.raises(ValueError, match="client_id"):
        await memory_store.save_client(client)


async def test_state_is_single_use(memory_store: InMemoryOAuthStore) -> None:
    await memory_store.save_state(make_state(), state_id="state-1", ttl=600)
    assert await memory_store.get_state("state-1") == make_state()
    assert await memory_store.get_state("state-1") is None


async def test_state_expires(
    memory_store: InMemoryOAuthStore, clock: FakeClock
) -> None:
    await memory_store.save_state(make_state(), state_id="state-1", ttl=600)
    clock.advance(601)
    assert await memory_store.get_state("state-1") is None
    # Second lookup after expiry cleanup must not raise
    assert await memory_store.get_state("state-1") is None


async def test_state_fresh_within_ttl(
    memory_store: InMemoryOAuthStore, clock: FakeClock
) -> None:
    await memory_store.save_state(make_state(), state_id="state-1", ttl=600)
    clock.advance(599)
    assert await memory_store.get_state("state-1") == make_state()


async def test_auth_code_is_single_use(memory_store: InMemoryOAuthStore) -> None:
    await memory_store.save_auth_code(make_auth_code(), ttl=300)
    assert await memory_store.get_auth_code("mcp_code-1") == make_auth_code()
    assert await memory_store.get_auth_code("mcp_code-1") is None


async def test_auth_code_expires(
    memory_store: InMemoryOAuthStore, clock: FakeClock
) -> None:
    await memory_store.save_auth_code(make_auth_code(), ttl=300)
    clock.advance(301)
    assert await memory_store.get_auth_code("mcp_code-1") is None
    assert await memory_store.get_auth_code("mcp_code-1") is None


async def test_save_token_requires_expires_in(
    memory_store: InMemoryOAuthStore,
) -> None:
    with pytest.raises(ValueError, match="expires_in"):
        await memory_store.save_oauth_token(
            make_token(expires_in=None), "client-1", ["wiki:read"], None
        )


async def test_access_token_roundtrip(
    memory_store: InMemoryOAuthStore, clock: FakeClock
) -> None:
    await memory_store.save_oauth_token(
        make_token(), "client-1", ["wiki:read"], "resource-1"
    )

    access = await memory_store.get_access_token("ya-access-1")
    assert access is not None
    assert access.token == "ya-access-1"
    assert access.client_id == "client-1"
    assert access.scopes == ["wiki:read"]
    assert access.resource == "resource-1"
    assert access.expires_at == int(clock.now + 3600)


async def test_access_token_expires(
    memory_store: InMemoryOAuthStore, clock: FakeClock
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)
    clock.advance(3601)
    assert await memory_store.get_access_token("ya-access-1") is None
    assert await memory_store.get_access_token("ya-access-1") is None


async def test_refresh_token_roundtrip(memory_store: InMemoryOAuthStore) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    refresh = await memory_store.get_refresh_token("ya-refresh-1")
    assert refresh is not None
    assert refresh.token == "ya-refresh-1"
    assert refresh.client_id == "client-1"
    assert refresh.scopes == ["wiki:read"]


async def test_refresh_token_expires_like_the_redis_one(
    memory_store: InMemoryOAuthStore, clock: FakeClock
) -> None:
    # Both stores hand out refresh tokens with the same lifetime; without
    # expires_at set here an in-memory one would live as long as the process.
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    refresh = await memory_store.get_refresh_token("ya-refresh-1")
    assert refresh is not None
    assert refresh.expires_at == int(clock.now) + REFRESH_TOKEN_TTL_SECONDS

    clock.advance(REFRESH_TOKEN_TTL_SECONDS + 1)
    assert await memory_store.get_refresh_token("ya-refresh-1") is None


async def test_token_without_refresh(memory_store: InMemoryOAuthStore) -> None:
    await memory_store.save_oauth_token(
        make_token(refresh=None), "client-1", ["wiki:read"], None
    )
    assert await memory_store.get_access_token("ya-access-1") is not None
    assert await memory_store.get_refresh_token("ya-refresh-1") is None


async def test_revoke_refresh_token_cascades_to_access(
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    await memory_store.revoke_refresh_token("ya-refresh-1")

    assert await memory_store.get_refresh_token("ya-refresh-1") is None
    assert await memory_store.get_access_token("ya-access-1") is None


async def test_revoke_access_token_keeps_refresh(
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.save_oauth_token(make_token(), "client-1", ["wiki:read"], None)

    await memory_store.revoke_access_token("ya-access-1")

    assert await memory_store.get_access_token("ya-access-1") is None
    assert await memory_store.get_refresh_token("ya-refresh-1") is not None


async def test_revoke_unknown_tokens_is_noop(
    memory_store: InMemoryOAuthStore,
) -> None:
    await memory_store.revoke_refresh_token("unknown")
    await memory_store.revoke_access_token("unknown")
