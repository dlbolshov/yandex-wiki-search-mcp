import logging
import secrets
import time

import aiohttp
import yarl
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS, OAuthStore
from mcp_wiki.mcp.oauth.types import (
    YandexCallbackRequest,
    YandexOauthAuthorizationCode,
    YandexOAuthState,
)

logger = logging.getLogger(__name__)


class YandexOAuthUnavailable(Exception):
    """Yandex's /token could not take a refresh just now: a 5xx or a 429.

    Deliberately not a TokenError. Every TokenError code tells the client
    something about its grant, and the only one that fits a refusal,
    invalid_grant, says the refresh token is dead, so a client following
    RFC 6749 discards it. Yandex being down or rate limiting says nothing
    about the token: it is still valid, here and at Yandex. The SDK's token
    handler lets anything that is not a TokenError escape as a 500, which
    blames the server and says nothing about the grant, so a client keeps
    its refresh token and can use it once Yandex is back. (The MCP SDK
    clients still start a sign-in at once; that much no status changes.)
    """


def _is_transient(status: int) -> bool:
    """Whether a Yandex /token status says it is busy or down, not "no"."""
    return status == 429 or status >= 500


async def _describe_refusal(response: aiohttp.ClientResponse) -> str:
    """The status of a Yandex /token refusal plus its own error, if it sent one."""
    reason = f"HTTP {response.status}"
    try:
        body = await response.json(content_type=None)
    except (ValueError, LookupError, aiohttp.ClientError, TimeoutError):
        # An HTML error page from a proxy, no JSON at all, a charset that
        # does not decode, or a body cut off mid-read. The status alone has
        # to do: describing a refusal must not fail on its own, or the SDK
        # answers the client with a bare 500 instead of the refusal.
        return reason
    if isinstance(body, dict):
        details = [
            str(body[key]) for key in ("error", "error_description") if body.get(key)
        ]
        if details:
            reason = f"{reason} {': '.join(details)}"
    return reason


class YandexOAuthAuthorizationServerProvider(
    OAuthAuthorizationServerProvider[
        YandexOauthAuthorizationCode, RefreshToken, AccessToken
    ]
):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        server_url: yarl.URL,
        yandex_oauth_issuer: yarl.URL,
        store: OAuthStore,
        registration_idle_ttl: int | None,
        scopes: list[str] | None = None,
        use_scopes: bool = True,
    ):
        """`registration_idle_ttl` has no default on purpose.

        It is how long a registration lives without a token grant, None for
        never — and never is exactly the unbounded growth a flood of
        unauthenticated /register calls would exploit, so every caller has
        to choose it.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._server_url = server_url
        self._yandex_oauth_issuer = yandex_oauth_issuer
        self._store = store
        self._registration_idle_ttl = registration_idle_ttl
        self._scopes = scopes
        self._use_scopes = use_scopes

    async def handle_yandex_callback(self, request: Request) -> Response:
        try:
            yandex_cb_data = YandexCallbackRequest.model_validate(request.query_params)
        except ValidationError:
            return JSONResponse(content="invalid callback data", status_code=400)

        state = await self._store.get_state(yandex_cb_data.state)
        if state is None:
            return JSONResponse(content="invalid state", status_code=400)

        new_code = f"mcp_{secrets.token_hex(16)}"
        auth_code = YandexOauthAuthorizationCode(
            code=new_code,
            yandex_auth_code=yandex_cb_data.code,
            client_id=state.client_id,
            redirect_uri=state.redirect_uri,
            redirect_uri_provided_explicitly=state.redirect_uri_provided_explicitly,
            expires_at=time.time() + 300,
            scopes=state.scopes or self._scopes or [],
            code_challenge=state.code_challenge,
            resource=state.resource,
        )
        await self._store.save_auth_code(auth_code, ttl=300)

        return RedirectResponse(
            url=construct_redirect_uri(
                str(state.redirect_uri),
                code=new_code,
                # The client's own state, not the lookup key we sent Yandex.
                # None when the client sent none — construct_redirect_uri
                # drops parameters that are None.
                state=state.client_state,
            ),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # One rule for every registration, public or confidential: kept for
        # the idle window, then slid by every token grant. A secret gets no
        # deadline of its own (the SDK stamps 0, RFC 7591's "never"): one
        # cut off clients in daily use, and the MCP Python SDK client never
        # recovers from that — it keeps the dead client_id and reopens
        # /authorize with it. RFC 7591 §5 lets a server retire unused
        # registrations instead, which is what the idle window does.
        await self._store.save_client(client_info, ttl=self._registration_idle_ttl)

    def _renewal_ttl(self, token: OAuthToken) -> int | None:
        """How long a registration lives after a grant issued `token`."""
        idle = self._registration_idle_ttl
        if idle is None:
            return None
        # The idle window counts from the moment the last of the grant's
        # tokens stops working. Both stores refuse a token without
        # expires_in before this runs.
        return max(REFRESH_TOKEN_TTL_SECONDS, token.expires_in or 0) + idle

    async def _renew_registration(
        self, client: OAuthClientInformationFull, token: OAuthToken
    ) -> None:
        """Restart a registration's idle window after a token grant.

        A fixed lifetime would retire a registration in active use, unannounced:
        its next refresh gets invalid_client, and the SDK's own client answers
        that by reopening /authorize with the dead client_id — a "Client ID
        not found" page in the user's browser rather than a re-registration.
        So the window slides, and it counts from the moment the last token
        the grant just issued stops working: the refresh token after 31 days,
        or the access token if Yandex made that one longer, as it commonly
        does (a year). Bearer requests touch no registration, so a client
        comes back to /token only once its tokens have run out — by its own
        clock, which starts when the reply reaches it, later than this. A
        window that ended with the tokens would be gone by that visit, which
        then strands the client; counted from their end, the client finds its
        registration, is told invalid_grant and simply signs in again.

        With the expiry off the record is saved with no TTL at all, which in
        Redis also clears one an earlier configuration set: switching the
        expiry off has to reach the registrations made while it was on.

        Best effort. It runs after the grant is committed — the new tokens
        saved, the old refresh token retired if Yandex replaced it — so a
        store failure here
        must not turn that grant into a 500 whose tokens the client never
        receives. A miss leaves the previous deadline until the next grant.

        A confidential registration made before secrets stopped expiring
        still carries the deadline it was stamped with, and the SDK refuses
        the secret past it whatever the store keeps. Saving it with
        client_secret_expires_at 0 lifts that at its first grant. One whose
        client sends no grant before that deadline — a year-long access
        token needs none — still expires on it, as it always would have.
        """
        if client.client_secret_expires_at:
            client = client.model_copy(update={"client_secret_expires_at": 0})
        try:
            await self._store.save_client(client, ttl=self._renewal_ttl(token))
        except Exception:
            logger.warning(
                "Could not renew the registration of client %s",
                client.client_id,
                exc_info=True,
            )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # Always server-generated, never the client's `state`. The key has
        # to be unguessable: anyone able to predict it could authorize under
        # the same key, overwrite the pending record, and collect the
        # victim's code bound to their own client_id, redirect_uri and
        # code_challenge. PKCE would not stand in the way — the stored
        # challenge would be theirs as well.
        state_id = secrets.token_hex(16)
        redirect_uri = client.validate_redirect_uri(params.redirect_uri)
        scopes = None
        if self._use_scopes:
            scopes = client.validate_scope(
                " ".join(params.scopes) if params.scopes else None
            )

        if client.client_id is None:
            raise ValueError("Client ID not provided.")
        await self._store.save_state(
            YandexOAuthState(
                redirect_uri=redirect_uri,
                code_challenge=params.code_challenge,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                client_id=client.client_id,
                resource=params.resource,
                scopes=scopes,
                client_state=params.state,
            ),
            state_id=state_id,
            ttl=600,
        )

        return construct_redirect_uri(
            str(self._yandex_oauth_issuer / "authorize"),
            response_type="code",
            client_id=self._client_id,
            redirect_uri=str(self._server_url / "oauth/yandex/callback"),
            state=state_id,
            scope=" ".join(scopes) if scopes else None,
        )

    async def _request_yandex_token(
        self, form: aiohttp.FormData, *, grant: str, grant_spent: bool
    ) -> OAuthToken:
        """POST a grant to Yandex's /token; a refusal becomes an OAuth error.

        The SDK's TokenHandler turns a TokenError into the 400 OAuth error
        JSON a client knows how to act on. Anything else escapes it as a bare
        500, which tells the client nothing — not even that this grant is
        dead and it has to authorize again. So a refusal is invalid_grant,
        but for one case where that would be plainly false: a refresh token
        Yandex could not take because it was down or rate limiting (5xx,
        429) is still valid, and told invalid_grant a client following RFC
        6749 would discard it. That one stays a 500 via YandexOAuthUnavailable.
        A refusal of our own app (invalid_client, a wrong OAUTH_CLIENT_SECRET)
        is still invalid_grant: no answer helps the client there, and the
        warning below is what tells the operator.

        `grant_spent` is whether our own grant is gone whatever Yandex says.
        An authorization code is: load_authorization_code() already took our
        mcp_ code, so even a transient refusal leaves nothing to retry, and
        invalid_grant is accurate whatever Yandex's reason.
        """
        async with (
            aiohttp.ClientSession() as session,
            session.post(self._yandex_oauth_issuer / "token", data=form) as response,
        ):
            if response.status != 200:
                reason = await _describe_refusal(response)
                # The client gets a clean 400 and the server no traceback, so
                # without this line a misconfigured OAUTH_CLIENT_SECRET would
                # show up in the log only as a run of 400s on /token.
                logger.warning("Yandex OAuth refused the %s: %s", grant, reason)
                if not grant_spent and _is_transient(response.status):
                    raise YandexOAuthUnavailable(
                        f"Yandex OAuth could not take the {grant} now: {reason}"
                    )
                raise TokenError(
                    error="invalid_grant",
                    error_description=f"Yandex OAuth refused the {grant}: {reason}",
                )
            return OAuthToken.model_validate_json(await response.read())

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> YandexOauthAuthorizationCode | None:
        return await self._store.get_auth_code(authorization_code)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: YandexOauthAuthorizationCode,
    ) -> OAuthToken:
        form = aiohttp.FormData()
        form.add_field("grant_type", "authorization_code")
        form.add_field("code", authorization_code.yandex_auth_code)
        form.add_field("client_id", self._client_id)
        form.add_field("client_secret", self._client_secret)
        form.add_field("redirect_uri", str(self._server_url / "oauth/yandex/callback"))

        # load_authorization_code() has already spent our mcp_ code, so
        # invalid_grant is accurate whatever Yandex's reason: there is
        # nothing left to retry, and the client has to authorize again.
        token = await self._request_yandex_token(
            form, grant="authorization code", grant_spent=True
        )

        if client.client_id is None:
            raise ValueError("client_id must be provided")
        await self._store.save_oauth_token(
            token=token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )
        await self._renew_registration(client, token)
        return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await self._store.get_refresh_token(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        form = aiohttp.FormData()
        form.add_field("grant_type", "refresh_token")
        form.add_field("refresh_token", refresh_token.token)
        form.add_field("client_id", self._client_id)
        form.add_field("client_secret", self._client_secret)

        # Not spent: the old refresh token is revoked only after Yandex
        # issues a new one, so a refusal leaves it as valid as it was.
        token = await self._request_yandex_token(
            form, grant="refresh token", grant_spent=False
        )

        if client.client_id is None:
            raise ValueError("client_id must be provided")
        # No new refresh token means the client keeps using the old one
        # (RFC 6749 §6), so it is stored as that token's pair: the old access
        # token it pointed at is dropped, and revoking it later reaches the
        # new one. The client still gets Yandex's answer as it came.
        stored = (
            token
            if token.refresh_token is not None
            else token.model_copy(update={"refresh_token": refresh_token.token})
        )
        # The new pair first. Until it is stored the old refresh token is the
        # client's only way back in: revoked first, a failed save here left
        # the client with a 500, no new tokens and an old one that no longer
        # worked either.
        await self._store.save_oauth_token(
            token=stored,
            client_id=client.client_id,
            scopes=refresh_token.scopes,
            resource=None,
        )
        await self._retire_refresh_token(refresh_token.token, replaced_by=stored)
        await self._renew_registration(client, token)
        return token

    async def _retire_refresh_token(self, old: str, *, replaced_by: OAuthToken) -> None:
        """Revoke the refresh token a successful refresh traded in.

        Only when Yandex issued a different one. The same token handed back
        (or none, which the caller stores as the same) is the record just
        saved, and revoking it would delete it; saving it again already
        dropped the access token it used to point at. The access token the
        new pair carries is spared too, in case Yandex handed that one back
        unchanged.

        Best effort, like the renewal: the grant is already stored, and a
        failed cleanup only leaves the old refresh token to run out its TTL.
        """
        if replaced_by.refresh_token == old:
            return
        try:
            await self._store.revoke_refresh_token(
                old, keep_access_token=replaced_by.access_token
            )
        except Exception:
            logger.warning(
                "Could not revoke a refresh token after a refresh", exc_info=True
            )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await self._store.get_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            await self._store.revoke_refresh_token(token.token)
        else:
            await self._store.revoke_access_token(token.token)
