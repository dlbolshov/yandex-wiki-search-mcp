import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS, OAuthStore
from mcp_wiki.mcp.oauth.types import YandexOauthAuthorizationCode, YandexOAuthState

from .crypto import hash_token

# Below this many live entries a sweep is not worth the walk.
SWEEP_MIN_ENTRIES = 128

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Expiring(Generic[T]):
    """A stored record together with the deadline it dies at.

    One entry rather than a value dictionary beside an expiry dictionary:
    whoever finds the record finds its deadline, so there is no pairing for
    a writer to half-maintain and no lookup that can miss its other half.
    """

    value: T
    expires_at: float | None


def _is_expired(expires_at: float | None, now: float) -> bool:
    """Whether a deadline has passed. The one definition in this module.

    A record with no deadline never expires. One reached exactly at its
    deadline counts as expired, so a single-use record cannot be spent on
    the tick it dies.
    """
    return expires_at is not None and expires_at <= now


class InMemoryOAuthStore(OAuthStore):
    """In-memory implementation of OAuthStore interface.

    Uses hashed tokens as dictionary keys for consistency with RedisOAuthStore,
    providing some protection against accidental token exposure in logs/dumps.

    Redis expires records for us; here they have to be reclaimed by hand.
    Every write runs a sweep whose threshold doubles off the surviving size,
    which makes the amortized cost per write constant while keeping the
    footprint proportional to what is actually live.
    """

    def __init__(self) -> None:
        self._dynamic_clients: dict[str, OAuthClientInformationFull] = {}
        self._states: dict[str, _Expiring[YandexOAuthState]] = {}
        self._auth_codes: dict[str, _Expiring[YandexOauthAuthorizationCode]] = {}
        # Keys are hashed tokens, values contain the original token
        self._tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        # Maps hashed refresh token -> hashed access token
        self._refresh2access_tokens: dict[str, str] = {}

        self._sweep_threshold = SWEEP_MIN_ENTRIES

    def _entry_count(self) -> int:
        return (
            len(self._dynamic_clients)
            + len(self._states)
            + len(self._auth_codes)
            + len(self._tokens)
            + len(self._refresh_tokens)
        )

    def _sweep_expired(self) -> None:
        """Drop every record whose lifetime has run out."""
        now = time.time()

        for state_id in [
            key
            for key, entry in self._states.items()
            if _is_expired(entry.expires_at, now)
        ]:
            del self._states[state_id]

        for code_id in [
            key
            for key, entry in self._auth_codes.items()
            if _is_expired(entry.expires_at, now)
        ]:
            del self._auth_codes[code_id]

        for token_hash in [
            key
            for key, token in self._tokens.items()
            if _is_expired(token.expires_at, now)
        ]:
            del self._tokens[token_hash]

        for token_hash in [
            key
            for key, token in self._refresh_tokens.items()
            if _is_expired(token.expires_at, now)
        ]:
            self._forget_refresh_token(token_hash)

        # Registrations expire on the secret the SDK stamps at /register and
        # enforces on every client authentication; dropping them here only
        # reclaims what is already dead. Without client_secret_expiry_seconds
        # configured there is no expiry to act on and they stay.
        for client_id in [
            key
            for key, client in self._dynamic_clients.items()
            if _is_expired(client.client_secret_expires_at, now)
        ]:
            del self._dynamic_clients[client_id]

    def _maybe_sweep(self) -> None:
        """Sweep when the store has grown past its threshold, then re-aim it.

        Re-aiming at twice the surviving size is what keeps this amortized:
        a sweep that frees nothing will not run again until the store has
        doubled, and one that frees a lot pulls the next sweep back in.
        """
        if self._entry_count() < self._sweep_threshold:
            return
        self._sweep_expired()
        self._sweep_threshold = max(SWEEP_MIN_ENTRIES, self._entry_count() * 2)

    def _forget_refresh_token(self, token_hash: str) -> None:
        """Drop a refresh token and the access token it points at."""
        self._refresh_tokens.pop(token_hash, None)
        access_token_hash = self._refresh2access_tokens.pop(token_hash, None)
        if access_token_hash is not None:
            self._tokens.pop(access_token_hash, None)

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        """Save a client to the in-memory store."""
        if client.client_id is None:
            raise ValueError("client_id must be provided")
        self._maybe_sweep()
        self._dynamic_clients[client.client_id] = client

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Retrieve a client from the in-memory store.

        An expired registration is left for the sweep rather than filtered
        here: the SDK rejects it on the authentication path regardless, so
        this stays a plain lookup.
        """
        return self._dynamic_clients.get(client_id)

    async def save_state(
        self, state: YandexOAuthState, *, state_id: str, ttl: int | None = None
    ) -> None:
        """Save an OAuth state with optional TTL."""
        self._maybe_sweep()
        self._states[state_id] = _Expiring(
            state, time.time() + ttl if ttl is not None else None
        )

    async def get_state(self, state_id: str) -> YandexOAuthState | None:
        """Get and remove an OAuth state if it exists and hasn't expired."""
        # States are single-use, so the record goes either way: spent when
        # it is still good, reclaimed when it is not.
        entry = self._states.pop(state_id, None)
        if entry is None or _is_expired(entry.expires_at, time.time()):
            return None
        return entry.value

    async def save_auth_code(
        self, code: YandexOauthAuthorizationCode, *, ttl: int | None = None
    ) -> None:
        """Save an authorization code with optional TTL."""
        self._maybe_sweep()
        self._auth_codes[code.code] = _Expiring(
            code, time.time() + ttl if ttl is not None else None
        )

    async def get_auth_code(self, code_id: str) -> YandexOauthAuthorizationCode | None:
        """Get and remove an authorization code if it exists and hasn't expired."""
        entry = self._auth_codes.pop(code_id, None)
        if entry is None or _is_expired(entry.expires_at, time.time()):
            return None
        return entry.value

    async def save_oauth_token(
        self, token: OAuthToken, client_id: str, scopes: list[str], resource: str | None
    ) -> None:
        """Save an OAuth token and its metadata."""
        if token.expires_in is None:
            raise ValueError("expires_in must be provided")

        self._maybe_sweep()
        access_token_hash = hash_token(token.access_token)

        # Save access token (keyed by hash)
        self._tokens[access_token_hash] = AccessToken(
            token=token.access_token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time() + token.expires_in),
            resource=resource,
        )

        # Save refresh token if provided
        if token.refresh_token is not None:
            refresh_token_hash = hash_token(token.refresh_token)

            # get_refresh_token enforces this deadline and the sweep
            # reclaims on it; without it an in-memory refresh token would
            # live as long as the process.
            self._refresh_tokens[refresh_token_hash] = RefreshToken(
                token=token.refresh_token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
            )

            # Map refresh token hash to access token hash for cleanup
            self._refresh2access_tokens[refresh_token_hash] = access_token_hash

    async def get_access_token(self, token: str) -> AccessToken | None:
        """Get an access token if it exists and hasn't expired."""
        token_hash = hash_token(token)
        access_token = self._tokens.get(token_hash)
        if access_token is None:
            return None

        if _is_expired(access_token.expires_at, time.time()):
            del self._tokens[token_hash]
            return None

        return access_token

    async def get_refresh_token(self, token: str) -> RefreshToken | None:
        """Get a refresh token if it exists and hasn't expired."""
        token_hash = hash_token(token)
        ref_token = self._refresh_tokens.get(token_hash)
        if ref_token is None:
            return None

        if _is_expired(ref_token.expires_at, time.time()):
            self._forget_refresh_token(token_hash)
            return None

        return ref_token

    async def revoke_refresh_token(self, token: str) -> None:
        """Delete a refresh token, its mapping and the access token it issued."""
        self._forget_refresh_token(hash_token(token))

    async def revoke_access_token(self, token: str) -> None:
        """Delete an access token; the refresh token (if any) stays valid."""
        self._tokens.pop(hash_token(token), None)
