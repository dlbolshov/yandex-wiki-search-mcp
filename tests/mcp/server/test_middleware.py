"""The debug log for inbound MCP messages.

Two properties matter: it names what was called (a bare method is useless when
27 tools share `tools/call`), and it stays silent unless DEBUG is on, because
a per-request line at the default level is how you stall a stdio server whose
client does not drain stderr.
"""

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server import MCPServer

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import create_mcp_server
from mcp_wiki.wiki.proto.types.pages import WikiPage
from tests.mcp.conftest import (
    create_test_settings,
    make_test_lifespan,
    safe_client,
)

LOGGER_NAME = "mcp_wiki.mcp.middleware"


def build_server() -> MCPServer[Any]:
    wiki = AsyncMock()
    wiki.page_get.return_value = WikiPage(id=7, slug="users/x", title="T")
    return create_mcp_server(
        settings=create_test_settings(),
        lifespan=make_test_lifespan(AppContext(wiki=wiki)),
    )


class TestDebugLogging:
    async def test_a_tool_call_is_logged_with_its_name_and_duration(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            async with safe_client(build_server()) as client:
                await client.call_tool("page_get", {"page_id": 7})

        lines = [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME]
        tool_calls = [line for line in lines if line.startswith("tools/call")]
        assert tool_calls, lines
        assert "page_get" in tool_calls[0]
        assert "ms)" in tool_calls[0]

    async def test_a_resource_read_is_logged_with_its_uri(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            async with safe_client(build_server()) as client:
                await client.read_resource("wiki-mcp://configuration")

        lines = [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME]
        assert any("wiki-mcp://configuration" in line for line in lines), lines

    async def test_a_method_without_a_target_is_logged_bare(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            async with safe_client(build_server()) as client:
                await client.list_tools()

        lines = [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME]
        listings = [line for line in lines if line.startswith("tools/list")]
        assert listings, lines
        # "tools/list (3 ms)" — no qualifier wedged in before the duration
        assert listings[0].startswith("tools/list (")


class TestSilentUnlessDebug:
    async def test_nothing_is_logged_at_the_default_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # LOG_LEVEL defaults to INFO; at that level this must cost nothing and
        # add no bytes to whatever collects the server's stderr.
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            async with safe_client(build_server()) as client:
                await client.call_tool("page_get", {"page_id": 7})

        assert [r for r in caplog.records if r.name == LOGGER_NAME] == []


class TestTheChainStillWorks:
    async def test_the_stashed_request_survives_the_added_middleware(
        self,
    ) -> None:
        # The logger wraps the stash; a mistake in ordering or in the finally
        # block would break the configuration resource rather than the log.
        async with safe_client(build_server()) as client:
            result = await client.read_resource("wiki-mcp://configuration")

        assert result.contents
