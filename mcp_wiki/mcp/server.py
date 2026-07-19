import base64
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import yarl
from mcp.server import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from starlette.routing import Route

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.oauth.provider import YandexOAuthAuthorizationServerProvider
from mcp_wiki.mcp.oauth.store import OAuthStore
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from mcp_wiki.mcp.params import instructions
from mcp_wiki.mcp.resources import register_resources
from mcp_wiki.mcp.tools import register_all_tools
from mcp_wiki.settings import Settings
from mcp_wiki.wiki.custom.client import WikiClient

Lifespan = Callable[[FastMCP[Any]], AbstractAsyncContextManager[AppContext]]


def _parse_encryption_keys(keys_str: str | None) -> list[bytes] | None:
    if not keys_str:
        return None

    keys: list[bytes] = []
    for i, key_b64 in enumerate(keys_str.split(","), start=1):
        if not (key_b64 := key_b64.strip()):
            continue
        try:
            key_bytes = base64.b64decode(key_b64)
        except Exception as exc:
            raise ValueError(f"Encryption key {i} is not valid base64: {exc}") from exc
        if len(key_bytes) != 32:
            raise ValueError(
                f"Encryption key {i} must be 32 bytes, got {len(key_bytes)}"
            )
        keys.append(key_bytes)

    return keys if keys else None


def make_wiki_lifespan(settings: Settings) -> Lifespan:
    @asynccontextmanager
    async def wiki_lifespan(_server: FastMCP[Any]) -> AsyncIterator[AppContext]:
        wiki = WikiClient(
            base_url=settings.wiki_api_base_url,
            token=settings.wiki_token,
            iam_token=settings.wiki_iam_token,
            auth_scheme=settings.wiki_auth_scheme,
            cloud_org_id=settings.wiki_cloud_org_id,
            org_id=settings.wiki_org_id,
        )
        try:
            await wiki.prepare()
            yield AppContext(wiki=wiki)
        finally:
            await wiki.close()

    return wiki_lifespan


def create_mcp_server(
    settings: Settings,
    lifespan: Lifespan | None = None,
) -> FastMCP[Any]:
    if lifespan is None:
        lifespan = make_wiki_lifespan(settings)

    auth_server_provider: YandexOAuthAuthorizationServerProvider | None = None
    auth_settings: AuthSettings | None = None

    if settings.oauth_enabled:
        assert settings.oauth_client_id, "OAuth client ID must be set."
        assert settings.oauth_client_secret, "OAuth client secret must be set."
        assert settings.mcp_server_public_url, "MCP server public url must be set."

        oauth_store: OAuthStore
        if settings.oauth_store == "memory":
            oauth_store = InMemoryOAuthStore()
        elif settings.oauth_store == "redis":
            encryption_keys = _parse_encryption_keys(settings.oauth_encryption_keys)
            if not encryption_keys:
                raise ValueError(
                    "OAUTH_ENCRYPTION_KEYS must be set when using Redis OAuth store."
                )
            oauth_store = RedisOAuthStore(
                endpoint=settings.redis_endpoint,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                pool_max_size=settings.redis_pool_max_size,
                encryption_keys=encryption_keys,
            )
        else:
            raise ValueError(
                f"Unsupported OAuth store: {settings.oauth_store}. "
                "Supported values are 'memory' and 'redis'."
            )

        scopes: list[str] | None = None
        if settings.oauth_use_scopes:
            scopes = (
                ["wiki:read"]
                if settings.wiki_read_only
                else [
                    "wiki:read",
                    "wiki:write",
                ]
            )

        auth_server_provider = YandexOAuthAuthorizationServerProvider(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            server_url=yarl.URL(str(settings.mcp_server_public_url)),
            yandex_oauth_issuer=yarl.URL(str(settings.oauth_server_url)),
            store=oauth_store,
            scopes=scopes,
            use_scopes=settings.oauth_use_scopes,
        )

        auth_settings = AuthSettings(
            issuer_url=settings.mcp_server_public_url,
            required_scopes=scopes,
            resource_server_url=settings.mcp_server_public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=scopes,
                default_scopes=scopes,
            ),
        )

    server = FastMCP(
        name="Yandex Wiki Search MCP",
        instructions=instructions,
        host=settings.host,
        port=settings.port,
        lifespan=lifespan,
        auth_server_provider=auth_server_provider,
        stateless_http=True,
        json_response=True,
        auth=auth_settings,
    )

    if auth_server_provider is not None:
        server._custom_starlette_routes.append(
            Route(
                path="/oauth/yandex/callback",
                endpoint=auth_server_provider.handle_yandex_callback,
                methods=["GET"],
                name="oauth_yandex_callback",
            )
        )

    register_resources(settings, server)
    register_all_tools(settings, server)
    return server
