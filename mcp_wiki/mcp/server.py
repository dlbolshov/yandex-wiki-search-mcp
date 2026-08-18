import base64
import importlib.metadata
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import yarl
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.middleware import log_inbound_middleware
from mcp_wiki.mcp.oauth.provider import YandexOAuthAuthorizationServerProvider
from mcp_wiki.mcp.oauth.store import OAuthStore
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from mcp_wiki.mcp.params import build_instructions
from mcp_wiki.mcp.request_ctx import stash_request_middleware
from mcp_wiki.mcp.resources import register_resources
from mcp_wiki.mcp.tools import register_all_tools
from mcp_wiki.settings import Settings, ToolResultText
from mcp_wiki.wiki.custom.client import WikiClient

Lifespan = Callable[[MCPServer[Any]], AbstractAsyncContextManager[AppContext]]


def server_version() -> str:
    try:
        return importlib.metadata.version("yandex-wiki-search-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def server_description() -> str | None:
    """The one-line summary clients show in a server list.

    Read from package metadata rather than repeated here: the same sentence
    already lives in pyproject.toml, manifest.json and server.json, and a
    fourth copy is a fourth thing to forget. Distinct from `instructions`,
    which is long-form guidance addressed to the model. None rather than ""
    when the package is not installed: the field is optional on the wire,
    and an absent description reads better than a blank one.
    """
    try:
        return importlib.metadata.metadata("yandex-wiki-search-mcp")["Summary"]
    except importlib.metadata.PackageNotFoundError:
        return None


# How long a client may treat a listing as fresh. The tool and resource sets
# are fixed when the server is constructed and never change while it runs, so
# the only staleness this can cause is a redeploy that adds or removes a tool:
# clients keep the old listing for up to this long. Five minutes trades a
# little of that against re-sending 31 tool schemas on every connection.
#
# Deliberately not on `resources/read`: the client caches it per URI, and
# `wiki-mcp://configuration` varies with the `?orgId=`/`?cloudOrgId=` on the
# *endpoint*, which is not part of the URI. A hint there would make it report
# one tenant's organization to another. Nor on `server/discover`, where a
# stale capability set would outlive a redeploy that changed it.
#
# Only 2026-07-28 clients see these; on every earlier revision the hints are
# not sent and traffic is byte-for-byte what it was.
LISTING_CACHE_TTL_MS = 5 * 60 * 1000

STATIC_LISTING_CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=LISTING_CACHE_TTL_MS, scope="private"),
    "resources/list": CacheHint(ttl_ms=LISTING_CACHE_TTL_MS, scope="private"),
}


async def healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class WikiMCPServer(MCPServer[Any]):
    """MCPServer with a configurable text duplicate of structured tool results.

    The MCP spec recommends (SHOULD) mirroring structured_content as a text
    block for backwards compatibility; the SDK renders it with indent=2.
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

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> Any:
        result = await super().call_tool(name, arguments, context)
        if self._tool_result_text == "pretty" or not isinstance(result, CallToolResult):
            return result
        # Nothing to duplicate, so nothing to trim: an error result or a tool
        # with no output schema carries text that is the payload itself.
        if result.structured_content is None:
            return result
        # Only the JSON duplicate is ours to drop or shrink. A tool that
        # returns real content blocks — an image, a downloaded attachment —
        # must keep them, so leave anything non-textual alone.
        if not all(isinstance(block, TextContent) for block in result.content):
            return result
        if self._tool_result_text == "none":
            return result.model_copy(update={"content": []})
        compact_text = json.dumps(
            result.structured_content, ensure_ascii=False, separators=(",", ":")
        )
        return result.model_copy(
            update={"content": [TextContent(type="text", text=compact_text)]}
        )


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
    async def wiki_lifespan(_server: MCPServer[Any]) -> AsyncIterator[AppContext]:
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
) -> MCPServer[Any]:
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

    # Transport settings (host, port, stateless_http, json_response) are no
    # longer constructor arguments in mcp 2.x — they belong to run() and
    # streamable_http_app(), fed by run_options()/http_app_options() below.
    server = WikiMCPServer(
        name="Yandex Wiki Search MCP",
        description=server_description(),
        instructions=build_instructions(
            include_local_uploads=settings.include_local_uploads,
            read_only=settings.wiki_read_only,
        ),
        version=server_version(),
        cache_hints=STATIC_LISTING_CACHE_HINTS,
        log_level=settings.log_level,
        lifespan=lifespan,
        auth_server_provider=auth_server_provider,
        auth=auth_settings,
        tool_result_text=settings.tool_result_text,
        # Order matters only in that the logger wraps the stash, so its
        # timing covers the whole chain rather than part of it.
        middleware=[log_inbound_middleware, stash_request_middleware],
    )

    # custom_route() returns a decorator, so it is applied by call here: the
    # server does not exist at import time, and the OAuth callback is a bound
    # method of a provider built a few lines above.
    server.custom_route("/healthz", methods=["GET"], name="healthz")(healthz)

    if auth_server_provider is not None:
        server.custom_route(
            "/oauth/yandex/callback",
            methods=["GET"],
            name="oauth_yandex_callback",
        )(auth_server_provider.handle_yandex_callback)

    register_resources(settings, server)
    register_all_tools(settings, server)
    return server


def http_app_options(settings: Settings) -> dict[str, Any]:
    """Keywords for streamable_http_app().

    `host` is load-bearing rather than decorative. mcp 2.x arms DNS rebinding
    protection automatically when host is 127.0.0.1/localhost/::1 and no
    transport_security is given — and streamable_http_app() *defaults* host to
    127.0.0.1. Leave it out and a server behind a real hostname answers every
    MCP request with 421 Misdirected Request while /healthz keeps returning
    200, which reads as "up" to every probe that matters.
    """
    return {
        "host": settings.host,
        "json_response": settings.json_response,
        "stateless_http": settings.stateless_http,
    }


def run_options(settings: Settings) -> dict[str, Any]:
    """Keywords for run(transport=settings.transport).

    run() is overloaded per transport and rejects keywords the chosen one does
    not take: stdio accepts none, and only streamable-http knows about
    json_response/stateless_http.
    """
    match settings.transport:
        case "stdio":
            return {}
        case "sse":
            return {"host": settings.host, "port": settings.port}
        case _:
            return {"port": settings.port, **http_app_options(settings)}
