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

from mcp_wiki.mcp.oauth.store import OAuthStore
from mcp_wiki.mcp.oauth.types import (
    YandexCallbackRequest,
    YandexOauthAuthorizationCode,
    YandexOAuthState,
)

logger = logging.getLogger(__name__)


async def _describe_refusal(response: aiohttp.ClientResponse) -> str:
    """The status of a Yandex /token refusal plus its own error, if it sent one."""
    reason = f"HTTP {response.status}"
    try:
        body = await response.json(content_type=None)
    except ValueError:
        # An HTML error page from a proxy, or no JSON at all.
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
        scopes: list[str] | None = None,
        use_scopes: bool = True,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._server_url = server_url
        self._yandex_oauth_issuer = yandex_oauth_issuer
        self._store = store
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
        await self._store.save_client(client_info)

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
        self, form: aiohttp.FormData, *, grant: str
    ) -> OAuthToken:
        """POST a grant to Yandex's /token; a refusal becomes an OAuth error.

        The SDK's TokenHandler turns a TokenError into the 400 OAuth error
        JSON a client knows how to act on. Anything else escapes it as a bare
        500, which tells the client nothing — not even that this grant is
        dead and it has to authorize again.
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
        token = await self._request_yandex_token(form, grant="authorization code")

        if client.client_id is None:
            raise ValueError("client_id must be provided")
        await self._store.save_oauth_token(
            token=token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )
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

        token = await self._request_yandex_token(form, grant="refresh token")

        await self._store.revoke_refresh_token(refresh_token.token)
        if client.client_id is None:
            raise ValueError("client_id must be provided")
        await self._store.save_oauth_token(
            token=token,
            client_id=client.client_id,
            scopes=refresh_token.scopes,
            resource=None,
        )
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await self._store.get_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            await self._store.revoke_refresh_token(token.token)
        else:
            await self._store.revoke_access_token(token.token)
