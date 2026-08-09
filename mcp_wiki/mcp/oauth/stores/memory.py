import time

from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS, OAuthStore
from mcp_wiki.mcp.oauth.types import YandexOauthAuthorizationCode, YandexOAuthState

from .crypto import hash_token

# Below this many live entries a sweep is not worth the walk.
SWEEP_MIN_ENTRIES = 128


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
        self._states: dict[str, YandexOAuthState] = {}
        self._auth_codes: dict[str, YandexOauthAuthorizationCode] = {}
        # Keys are hashed tokens, values contain the original token
        self._tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        # Maps hashed refresh token -> hashed access token
        self._refresh2access_tokens: dict[str, str] = {}

        # TTL tracking for temporary data
        self._state_expiry: dict[str, float] = {}
        self._auth_code_expiry: dict[str, float] = {}

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

        for state_id in [k for k, exp in self._state_expiry.items() if exp <= now]:
            self._states.pop(state_id, None)
            self._state_expiry.pop(state_id, None)

        for code_id in [k for k, exp in self._auth_code_expiry.items() if exp <= now]:
            self._auth_codes.pop(code_id, None)
            self._auth_code_expiry.pop(code_id, None)

        for token_hash in [
            k
            for k, tok in self._tokens.items()
            if tok.expires_at and tok.expires_at < now
        ]:
            del self._tokens[token_hash]

        for token_hash in [
            k
            for k, tok in self._refresh_tokens.items()
            if tok.expires_at and tok.expires_at < now
        ]:
            del self._refresh_tokens[token_hash]
            self._refresh2access_tokens.pop(token_hash, None)

        # Registrations expire on the secret the SDK stamped at /register and
        # enforces on every client authentication; dropping them here just
        # reclaims what is already dead. Without client_secret_expiry_seconds
        # configured there is no expiry and they stay, as before.
        for client_id in [
            cid
            for cid, client in self._dynamic_clients.items()
            if client.client_secret_expires_at and client.client_secret_expires_at < now
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
        self._states[state_id] = state
        if ttl is not None:
            self._state_expiry[state_id] = time.time() + ttl

    async def get_state(self, state_id: str) -> YandexOAuthState | None:
        """Get and remove an OAuth state if it exists and hasn't expired."""
        # Check expiry
        if (
            state_id in self._state_expiry
            and time.time() > self._state_expiry[state_id]
        ):
            # Expired - clean up
            del self._states[state_id]
            del self._state_expiry[state_id]
            return None

        # Return and remove state (states are single-use)
        state = self._states.get(state_id)
        if state is not None:
            del self._states[state_id]
            if state_id in self._state_expiry:
                del self._state_expiry[state_id]
        return state

    async def save_auth_code(
        self, code: YandexOauthAuthorizationCode, *, ttl: int | None = None
    ) -> None:
        """Save an authorization code with optional TTL."""
        self._maybe_sweep()
        self._auth_codes[code.code] = code
        if ttl is not None:
            self._auth_code_expiry[code.code] = time.time() + ttl

    async def get_auth_code(self, code_id: str) -> YandexOauthAuthorizationCode | None:
        """Get and remove an authorization code if it exists and hasn't expired."""
        # Check expiry
        if (
            code_id in self._auth_code_expiry
            and time.time() > self._auth_code_expiry[code_id]
        ):
            # Expired - clean up
            del self._auth_codes[code_id]
            del self._auth_code_expiry[code_id]
            return None

        # Return and remove auth code (auth codes are single-use)
        auth_code = self._auth_codes.get(code_id)
        if auth_code is not None:
            del self._auth_codes[code_id]
            if code_id in self._auth_code_expiry:
                del self._auth_code_expiry[code_id]
        return auth_code

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

            # expires_at matters: get_refresh_token already checks it, but
            # without a value set here the check never fired and in-memory
            # refresh tokens outlived the Redis ones by the process lifetime.
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
        if not access_token:
            return None

        # Check if expired
        if access_token.expires_at and access_token.expires_at < time.time():
            del self._tokens[token_hash]
            return None

        return access_token

    async def get_refresh_token(self, token: str) -> RefreshToken | None:
        """Get a refresh token if it exists and hasn't expired."""
        token_hash = hash_token(token)
        ref_token = self._refresh_tokens.get(token_hash)
        if ref_token is None:
            return None

        # Check if expired (if expiry is set)
        if ref_token.expires_at and ref_token.expires_at < time.time():
            # Token is expired, remove it
            del self._refresh_tokens[token_hash]
            if token_hash in self._refresh2access_tokens:
                del self._refresh2access_tokens[token_hash]
            return None

        return ref_token

    async def revoke_refresh_token(self, token: str) -> None:
        """Delete a refresh token and its associated mappings."""
        token_hash = hash_token(token)
        if token_hash in self._refresh_tokens:
            # Get associated access token hash
            access_token_hash = self._refresh2access_tokens.get(token_hash)

            # Delete refresh token
            del self._refresh_tokens[token_hash]

            # Delete mapping
            if token_hash in self._refresh2access_tokens:
                del self._refresh2access_tokens[token_hash]

            # Delete associated access token
            if access_token_hash and access_token_hash in self._tokens:
                del self._tokens[access_token_hash]

    async def revoke_access_token(self, token: str) -> None:
        """Delete an access token; the refresh token (if any) stays valid."""
        self._tokens.pop(hash_token(token), None)
