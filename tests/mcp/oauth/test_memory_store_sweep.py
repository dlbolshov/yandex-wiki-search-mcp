"""The in-memory store reclaims what it declared expired.

Redis expires records on its own; here they have to be swept. Acting on a
deadline only when someone looks up that exact key is not enough: nobody
ever looks up an abandoned login, and /authorize is unauthenticated, so
those records are also the cheapest way to fill the store on purpose.
"""

import pytest

import mcp_wiki.mcp.oauth.stores.memory as memory_module
from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS
from mcp_wiki.mcp.oauth.stores.memory import SWEEP_MIN_ENTRIES, InMemoryOAuthStore
from tests.mcp.oauth.helpers import (
    make_auth_code,
    make_client,
    make_state,
    make_token,
)

# The lifetimes the provider hands these records.
STATE_TTL = 600
AUTH_CODE_TTL = 300
# Enough writes to take the store past its first sweep threshold.
ENOUGH_TO_SWEEP = SWEEP_MIN_ENTRIES * 2 + 1


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


@pytest.fixture
def store() -> InMemoryOAuthStore:
    return InMemoryOAuthStore()


async def save_states(store: InMemoryOAuthStore, count: int, prefix: str) -> None:
    for index in range(count):
        await store.save_state(
            make_state(), state_id=f"{prefix}-{index}", ttl=STATE_TTL
        )


class TestAbandonedRecordsAreReclaimed:
    async def test_expired_states_do_not_accumulate(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        # Nobody ever comes back for these: the callback that would consume
        # them never arrives.
        await save_states(store, ENOUGH_TO_SWEEP, "abandoned")
        assert len(store._states) == ENOUGH_TO_SWEEP

        clock.advance(STATE_TTL + 1)
        await save_states(store, ENOUGH_TO_SWEEP, "later")

        # Only the second batch is still alive.
        assert len(store._states) == ENOUGH_TO_SWEEP
        assert not any(key.startswith("abandoned-") for key in store._states)

    async def test_expired_auth_codes_do_not_accumulate(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        # A token exchange that never happens leaks the same way an
        # abandoned /authorize does: the code outlives its deadline with
        # nobody left to consume it.
        async def save_codes(prefix: str) -> None:
            for index in range(ENOUGH_TO_SWEEP):
                await store.save_auth_code(
                    make_auth_code(code=f"{prefix}-{index}"), ttl=AUTH_CODE_TTL
                )

        await save_codes("abandoned")
        clock.advance(AUTH_CODE_TTL + 1)
        await save_codes("later")

        assert len(store._auth_codes) == ENOUGH_TO_SWEEP
        assert not any(key.startswith("abandoned-") for key in store._auth_codes)

    async def test_live_records_are_never_dropped(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        await save_states(store, ENOUGH_TO_SWEEP, "live")

        clock.advance(STATE_TTL - 1)
        await save_states(store, ENOUGH_TO_SWEEP, "newer")

        assert len(store._states) == ENOUGH_TO_SWEEP * 2
        assert await store.get_state("live-0") is not None

    async def test_expired_tokens_are_reclaimed_with_their_mapping(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        async def save_tokens(prefix: str) -> None:
            for index in range(ENOUGH_TO_SWEEP):
                await store.save_oauth_token(
                    make_token(
                        access=f"{prefix}-access-{index}",
                        refresh=f"{prefix}-refresh-{index}",
                    ),
                    "client-1",
                    ["wiki:read"],
                    None,
                )

        await save_tokens("old")
        clock.advance(REFRESH_TOKEN_TTL_SECONDS + 1)
        await save_tokens("new")

        assert await store.get_access_token("old-access-0") is None
        assert await store.get_refresh_token("new-refresh-0") is not None
        assert len(store._tokens) <= ENOUGH_TO_SWEEP
        assert len(store._refresh_tokens) <= ENOUGH_TO_SWEEP
        # The refresh->access mapping must not outlive what it points at.
        assert len(store._refresh2access_tokens) == len(store._refresh_tokens)

    async def test_expired_registrations_are_reclaimed(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        expires_at = int(clock.now) + 60
        for index in range(ENOUGH_TO_SWEEP):
            await store.save_client(
                make_client(f"old-{index}", client_secret_expires_at=expires_at)
            )

        clock.advance(61)
        for index in range(ENOUGH_TO_SWEEP):
            await store.save_client(make_client(f"new-{index}"))

        assert not any(key.startswith("old-") for key in store._dynamic_clients)
        assert len(store._dynamic_clients) == ENOUGH_TO_SWEEP

    async def test_registrations_without_an_expiry_are_kept(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        # client_secret_expiry_seconds unset on the server: no expiry to act
        # on, so nothing may be dropped.
        for index in range(ENOUGH_TO_SWEEP):
            await store.save_client(make_client(f"client-{index}"))

        clock.advance(365 * 24 * 60 * 60)
        await store.save_client(make_client("client-fresh"))

        assert len(store._dynamic_clients) == ENOUGH_TO_SWEEP + 1


class TestRecordsCarryTheirDeadline:
    """A record and its deadline live in one entry, so neither can go missing."""

    async def test_a_state_stores_its_own_deadline(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        await store.save_state(make_state(), state_id="k", ttl=STATE_TTL)

        entry = store._states["k"]
        assert entry.value == make_state()
        assert entry.expires_at == clock.now + STATE_TTL

    async def test_a_state_saved_without_a_ttl_never_expires(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        await store.save_state(make_state(), state_id="k")

        assert store._states["k"].expires_at is None
        clock.advance(10 * 365 * 24 * 60 * 60)
        assert await store.get_state("k") is not None

    async def test_a_state_is_spent_at_its_deadline_not_after(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        await store.save_state(make_state(), state_id="k", ttl=STATE_TTL)

        clock.advance(STATE_TTL)
        assert await store.get_state("k") is None


class TestSweepCost:
    async def test_a_small_store_is_left_alone(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        # Below the threshold the walk costs more than the memory it frees.
        await save_states(store, 3, "small")
        clock.advance(STATE_TTL + 1)
        await save_states(store, 3, "later")

        assert len(store._states) == 6

    async def test_threshold_tracks_what_survived(
        self, store: InMemoryOAuthStore, clock: FakeClock
    ) -> None:
        await save_states(store, ENOUGH_TO_SWEEP, "live")
        grown = store._sweep_threshold
        assert grown >= ENOUGH_TO_SWEEP, "a sweep that frees nothing must back off"

        clock.advance(STATE_TTL + 1)
        await save_states(store, ENOUGH_TO_SWEEP, "later")

        # Freeing a lot pulls the next sweep back in rather than letting the
        # threshold ratchet up forever.
        assert store._sweep_threshold < grown * 2
