"""TOOL_RESULT_TEXT modes: pretty (FastMCP default) | compact | none."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import WikiFastMCP, create_mcp_server
from mcp_wiki.settings import ToolResultText
from mcp_wiki.wiki.proto.types.pages import WikiPage
from tests.mcp.conftest import (
    create_test_settings,
    make_test_lifespan,
    safe_client_session,
)

EXPECTED_STRUCTURED = {"id": 1, "slug": "users/x", "title": "T"}


async def call_page_get(mode: ToolResultText) -> CallToolResult:
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


class TestNonTextContentSurvives:
    """Only the JSON duplicate is ours to drop — real content blocks are not."""

    @staticmethod
    def _server(mode: ToolResultText) -> WikiFastMCP:
        # An annotated ContentBlock return does carry an output schema, so
        # convert_result hands back the same (blocks, structured) pair a
        # model-returning tool does — the tuple check alone would not save it.
        server = WikiFastMCP(name="test", tool_result_text=mode)

        @server.tool()
        def make_image() -> ImageContent:
            return ImageContent(
                type="image",
                data=base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
                mimeType="image/png",
            )

        return server

    @pytest.mark.parametrize("mode", ["none", "compact"])
    async def test_image_block_is_not_replaced(self, mode: ToolResultText) -> None:
        blocks, structured = await self._server(mode).call_tool("make_image", {})

        assert [type(block).__name__ for block in blocks] == ["ImageContent"]
        assert structured["mimeType"] == "image/png"

    async def test_none_still_drops_a_plain_json_duplicate(self) -> None:
        server = WikiFastMCP(name="test", tool_result_text="none")

        @server.tool()
        def make_page() -> WikiPage:
            return WikiPage(id=1, slug="users/x")

        blocks, structured = await server.call_tool("make_page", {})
        assert blocks == []
        assert structured == {"id": 1, "slug": "users/x"}
