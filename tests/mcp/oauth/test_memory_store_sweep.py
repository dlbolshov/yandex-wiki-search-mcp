"""The in-memory store reclaims what it declared expired.

Redis expires records on its own; here nothing did. The store accepted a
`ttl`, recorded the deadline, and then only ever acted on it when someone
looked up that exact key — so an abandoned login (or a flood of them, since
/authorize is unauthenticated) left records behind for the process lifetime.
"""

import pytest

import mcp_wiki.mcp.oauth.stores.memory as memory_module
from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS
from mcp_wiki.mcp.oauth.stores.memory import SWEEP_MIN_ENTRIES, InMemoryOAuthStore
from tests.mcp.oauth.helpers import make_client, make_state, make_token

STATE_TTL = 600
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
        assert len(store._state_expiry) == len(store._states)

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
