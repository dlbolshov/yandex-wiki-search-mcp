import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from mcp.client.session import ClientSession
from mcp.server import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult
from pydantic import AnyHttpUrl, SecretStr

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import Lifespan, create_mcp_server
from mcp_wiki.settings import Settings


@asynccontextmanager
async def safe_client_session(
    mcp_server: FastMCP[Any],
) -> AsyncIterator[ClientSession]:
    ctx_mgr = create_connected_server_and_client_session(
        mcp_server,
        raise_exceptions=True,
    )
    session = await ctx_mgr.__aenter__()
    try:
        yield session
    finally:
        with suppress(RuntimeError, ExceptionGroup):
            await ctx_mgr.__aexit__(None, None, None)


def get_tool_result_content(result: CallToolResult) -> Any:
    structured = result.structuredContent
    if structured is not None:
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured

    assert result.content, "Tool result has neither structuredContent nor content"
    text = getattr(result.content[0], "text", None)
    assert text is not None, "Tool result content item does not expose text"
    return json.loads(text)


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
    async def test_lifespan(_server: FastMCP[Any]) -> AsyncIterator[AppContext]:
        yield app_context

    return test_lifespan


@pytest.fixture
def mcp_server(test_settings: Settings, mock_app_context: AppContext) -> FastMCP[Any]:
    return create_mcp_server(
        settings=test_settings,
        lifespan=make_test_lifespan(mock_app_context),
    )


@pytest.fixture
def mcp_server_read_only(
    test_settings_read_only: Settings,
    mock_app_context: AppContext,
) -> FastMCP[Any]:
    return create_mcp_server(
        settings=test_settings_read_only,
        lifespan=make_test_lifespan(mock_app_context),
    )


@pytest_asyncio.fixture(loop_scope="function")
async def client_session(mcp_server: FastMCP[Any]) -> AsyncIterator[ClientSession]:
    async with safe_client_session(mcp_server) as session:
        yield session


@pytest_asyncio.fixture(loop_scope="function")
async def client_session_read_only(
    mcp_server_read_only: FastMCP[Any],
) -> AsyncIterator[ClientSession]:
    async with safe_client_session(mcp_server_read_only) as session:
        yield session
