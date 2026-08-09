import base64
import importlib.metadata
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import yarl
from mcp.server import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.types import TextContent
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.oauth.provider import YandexOAuthAuthorizationServerProvider
from mcp_wiki.mcp.oauth.store import OAuthStore
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from mcp_wiki.mcp.params import build_instructions
from mcp_wiki.mcp.resources import register_resources
from mcp_wiki.mcp.tools import register_all_tools
from mcp_wiki.settings import Settings, ToolResultText
from mcp_wiki.wiki.custom.client import WikiClient

Lifespan = Callable[[FastMCP[Any]], AbstractAsyncContextManager[AppContext]]


def server_version() -> str:
    try:
        return importlib.metadata.version("yandex-wiki-search-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


async def healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class WikiFastMCP(FastMCP):
    """FastMCP with a configurable text duplicate of structured tool results.

    The MCP spec recommends (SHOULD) mirroring structuredContent as a text
    block for backwards compatibility; FastMCP renders it with indent=2.
    `tool_result_text` keeps that default ("pretty"), shrinks it to one line
    ("compact"), or omits the duplicate entirely ("none") — structured
    content and its schema validation are untouched.
    """

    def __init__(
        self,
        *args: Any,
        tool_result_text: ToolResultText = "pretty",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tool_result_text = tool_result_text

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await super().call_tool(name, arguments)
        if self._tool_result_text == "pretty" or not (
            isinstance(result, tuple) and len(result) == 2
        ):
            return result
        unstructured, structured = result
        # Only the JSON duplicate is ours to drop or shrink. A tool that
        # returns real content blocks — an image, a downloaded attachment —
        # must keep them, so leave anything non-textual alone.
        if not all(isinstance(block, TextContent) for block in unstructured):
            return result
        if self._tool_result_text == "none":
            return [], structured
        compact_text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
        return [TextContent(type="text", text=compact_text)], structured


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
            token=settings.wiki_token.get_secret_value()
            if settings.wiki_token
            else None,
            iam_token=settings.wiki_iam_token.get_secret_value()
            if settings.wiki_iam_token
            else None,
            auth_scheme=settings.wiki_auth_scheme,
            cloud_org_id=settings.wiki_cloud_org_id,
            org_id=settings.wiki_org_id,
            max_retries=settings.wiki_max_retries,
        )
        try:
            await wiki.prepare()
            yield AppContext(wiki=wiki, web_base_url=settings.wiki_web_base_url)
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
        if not settings.oauth_client_id:
            raise ValueError("OAuth client ID must be set.")
        if not settings.oauth_client_secret:
            raise ValueError("OAuth client secret must be set.")
        if not settings.mcp_server_public_url:
            raise ValueError("MCP server public url must be set.")

        oauth_store: OAuthStore
        if settings.oauth_store == "memory":
            oauth_store = InMemoryOAuthStore()
        elif settings.oauth_store == "redis":
            encryption_keys = _parse_encryption_keys(
                settings.oauth_encryption_keys.get_secret_value()
                if settings.oauth_encryption_keys
                else None
            )
            if not encryption_keys:
                raise ValueError(
                    "OAUTH_ENCRYPTION_KEYS must be set when using Redis OAuth store."
                )
            oauth_store = RedisOAuthStore(
                endpoint=settings.redis_endpoint,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password.get_secret_value()
                if settings.redis_password
                else None,
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
            client_secret=settings.oauth_client_secret.get_secret_value(),
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
                client_secret_expiry_seconds=settings.oauth_client_secret_expiry_seconds,
            ),
        )

    server = WikiFastMCP(
        name="Yandex Wiki Search MCP",
        instructions=build_instructions(
            include_local_uploads=settings.include_local_uploads,
            read_only=settings.wiki_read_only,
        ),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        lifespan=lifespan,
        auth_server_provider=auth_server_provider,
        stateless_http=settings.stateless_http,
        json_response=settings.json_response,
        auth=auth_settings,
        tool_result_text=settings.tool_result_text,
    )
    server._mcp_server.version = server_version()

    server._custom_starlette_routes.append(
        Route(
            path="/healthz",
            endpoint=healthz,
            methods=["GET"],
            name="healthz",
        )
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
