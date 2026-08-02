"""TOOL_RESULT_TEXT modes: pretty (FastMCP default) | compact | none."""

import json
from unittest.mock import AsyncMock

from mcp.types import CallToolResult

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import create_mcp_server
from mcp_wiki.wiki.proto.types.pages import WikiPage
from tests.mcp.conftest import (
    create_test_settings,
    make_test_lifespan,
    safe_client_session,
)

EXPECTED_STRUCTURED = {"id": 1, "slug": "users/x", "title": "T"}


async def call_page_get(mode: str) -> CallToolResult:
    wiki = AsyncMock()
    wiki.page_get.return_value = WikiPage(id=1, slug="users/x", title="T")
    settings = create_test_settings().model_copy(update={"tool_result_text": mode})
    server = create_mcp_server(
        settings=settings,
        lifespan=make_test_lifespan(AppContext(wiki=wiki)),
    )
    async with safe_client_session(server) as session:
        return await session.call_tool("page_get", {"page_id": 1})


class TestToolResultText:
    async def test_pretty_keeps_the_indented_duplicate(self) -> None:
        result = await call_page_get("pretty")

        assert not result.isError
        assert result.structuredContent == EXPECTED_STRUCTURED
        text = getattr(result.content[0], "text", None)
        assert text is not None
        assert "\n" in text
        assert json.loads(text) == EXPECTED_STRUCTURED

    async def test_compact_collapses_the_duplicate_to_one_line(self) -> None:
        result = await call_page_get("compact")

        assert not result.isError
        assert result.structuredContent == EXPECTED_STRUCTURED
        text = getattr(result.content[0], "text", None)
        assert text is not None
        assert "\n" not in text
        assert json.loads(text) == EXPECTED_STRUCTURED

    async def test_none_omits_the_text_block(self) -> None:
        result = await call_page_get("none")

        assert not result.isError
        assert result.content == []
        assert result.structuredContent == EXPECTED_STRUCTURED
