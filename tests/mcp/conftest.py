import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import AnyHttpUrl, SecretStr

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import Lifespan, create_mcp_server
from mcp_wiki.settings import Settings


@asynccontextmanager
async def safe_client(mcp_server: MCPServer[Any]) -> AsyncIterator[Client]:
    """In-memory client: the mcp 2.x replacement for the removed
    create_connected_server_and_client_session helper.

    Left on the default mode="auto", so the suite exercises the 2026-07-28
    path a modern client actually negotiates. Nothing here needs a
    back-channel — no tool elicits, samples, or lists roots — so the era
    costs us nothing.
    """
    ctx_mgr = Client(mcp_server, raise_exceptions=True)
    client = await ctx_mgr.__aenter__()
    try:
        yield client
    finally:
        with suppress(RuntimeError, ExceptionGroup):
            await ctx_mgr.__aexit__(None, None, None)


def get_tool_result_content(result: CallToolResult) -> Any:
    structured = result.structured_content
    if structured is not None:
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured

    assert result.content, "Tool result has neither structured_content nor content"
    text = getattr(result.content[0], "text", None)
    assert text is not None, "Tool result content item does not expose text"
    return json.loads(text)


# Streamable HTTP plumbing for the tests that drive the ASGI app directly
# instead of going through an in-memory Client.
MCP_HTTP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def initialize_request(request_id: int = 1) -> dict[str, Any]:
    """A 2025-era `initialize`, the handshake a legacy client still sends.

    mcp 2.x answers it alongside 2026-07-28 on the same endpoint, so these
    tests double as cover for the both-eras promise.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        },
    }


def get_tool_result_text(result: CallToolResult) -> str:
    assert result.content, "Tool result has no content"
    text = getattr(result.content[0], "text", None)
    assert text is not None, "Tool result content item does not expose text"
    return text


def create_test_settings(read_only: bool = False) -> Settings:
    return Settings.model_construct(
        wiki_token=SecretStr("test-token"),
        wiki_org_id="test-org",
        wiki_cloud_org_id=None,
        wiki_read_only=read_only,
        host="0.0.0.0",
        port=8000,
        transport="stdio",
        stateless_http=True,
        json_response=True,
        wiki_api_base_url="https://api.wiki.yandex.net",
        wiki_iam_token=None,
        wiki_auth_scheme="OAuth",
        oauth_enabled=False,
        oauth_store="memory",
        oauth_server_url=AnyHttpUrl("https://oauth.yandex.ru"),
        oauth_use_scopes=True,
        oauth_client_id=None,
        oauth_client_secret=None,
        mcp_server_public_url=None,
        oauth_encryption_keys=None,
        redis_endpoint="localhost",
        redis_port=6379,
        redis_db=0,
        redis_password=None,
        redis_pool_max_size=10,
    )


@pytest.fixture
def test_settings() -> Settings:
    return create_test_settings()


@pytest.fixture
def test_settings_read_only() -> Settings:
    return create_test_settings(read_only=True)


@pytest.fixture
def mock_wiki_protocol() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_app_context(mock_wiki_protocol: AsyncMock) -> AppContext:
    return AppContext(wiki=mock_wiki_protocol)


def make_test_lifespan(app_context: AppContext) -> Lifespan:
    @asynccontextmanager
    async def test_lifespan(_server: MCPServer[Any]) -> AsyncIterator[AppContext]:
        yield app_context

    return test_lifespan


@pytest.fixture
def mcp_server(test_settings: Settings, mock_app_context: AppContext) -> MCPServer[Any]:
    return create_mcp_server(
        settings=test_settings,
        lifespan=make_test_lifespan(mock_app_context),
    )


@pytest.fixture
def mcp_server_read_only(
    test_settings_read_only: Settings,
    mock_app_context: AppContext,
) -> MCPServer[Any]:
    return create_mcp_server(
        settings=test_settings_read_only,
        lifespan=make_test_lifespan(mock_app_context),
    )


@pytest_asyncio.fixture(loop_scope="function")
async def client(mcp_server: MCPServer[Any]) -> AsyncIterator[Client]:
    async with safe_client(mcp_server) as connected:
        yield connected


@pytest_asyncio.fixture(loop_scope="function")
async def client_read_only(
    mcp_server_read_only: MCPServer[Any],
) -> AsyncIterator[Client]:
    async with safe_client(mcp_server_read_only) as connected:
        yield connected
