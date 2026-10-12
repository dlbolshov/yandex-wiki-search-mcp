"""What a failing tool shows the model.

Since mcp 2.1 (python-sdk#3314) the SDK passes a failed tool's message on
only for a `ToolError` and reduces anything else to `Error executing tool
<name>`. The tools raise `ToolError` for their own refusals; a `WikiError`
comes from a layer that cannot import the SDK, so `WikiMCPServer.call_tool`
promotes it. uv.lock pins mcp 2.0, which passes every message on, so most of
these hold there with or without the promotion. The `test-latest-mcp` CI job
runs them against the newest 2.x, where they are what keeps a Wiki failure
from reaching the model as a bare error.
"""

import logging
from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_wiki.mcp.server import WikiMCPServer
from mcp_wiki.wiki.custom.errors import (
    GridConflict,
    PageNotFound,
    WikiArgumentError,
    WikiError,
    WikiRateLimited,
    WikiResponseError,
    WikiTransportError,
)
from tests.mcp.conftest import get_tool_result_text


def _server_raising(error: Exception) -> WikiMCPServer:
    server = WikiMCPServer(name="test")

    @server.tool()
    def fails() -> str:
        raise error

    return server


async def _raised(call: Awaitable[Any]) -> ToolError:
    with pytest.raises(ToolError) as caught:
        await call
    return caught.value


class TestWikiErrorsReachTheModel:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (PageNotFound(42), "Wiki page not found: 42"),
            # The retry advice is the whole point of this class.
            (
                GridConflict(
                    status=409,
                    error_code="CONFLICTING_OPERATION",
                    message="Conflicting operation in progress",
                ),
                GridConflict.RECOVERY,
            ),
            (
                WikiTransportError("GET", "v1/pages/42", TimeoutError()),
                "failed before a response: TimeoutError",
            ),
            (WikiArgumentError("target must not be empty."), "target must not be"),
            # Whether it was applied is what an agent needs to know.
            (WikiRateLimited(status=429), "refused this one without applying it"),
            # For a write, the warning not to retry blind is the point.
            (
                WikiResponseError("POST", "v1/pages", ValueError("not JSON")),
                "The change may have been applied",
            ),
        ],
        ids=[
            "not-found",
            "grid-conflict",
            "transport",
            "argument",
            "rate-limited",
            "unreadable",
        ],
    )
    async def test_its_message_is_the_tool_result(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        error: WikiError,
        expected: str,
    ) -> None:
        mock_wiki_protocol.page_get.side_effect = error

        result = await client.call_tool("page_get", {"page_id": 42})

        assert result.is_error is True
        assert expected in get_tool_result_text(result)

    async def test_it_is_not_logged_as_a_crash(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # mcp 2.1+ logs a crash at ERROR with its traceback; a 404 is the
        # Wiki answering, not this server failing.
        mock_wiki_protocol.page_get.side_effect = PageNotFound(42)

        with caplog.at_level(logging.INFO):
            await client.call_tool("page_get", {"page_id": 42})

        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    async def test_a_slug_that_is_not_a_url_says_so(
        self, client: Client, mock_wiki_protocol: AsyncMock
    ) -> None:
        # Refused by normalize_slug, which the tool layer shares with the
        # client: urlparse's ValueError reached the model as a bare crash.
        result = await client.call_tool(
            "page_get", {"slug": "https://[wiki.yandex.ru/team/page"}
        )

        assert result.is_error is True
        assert "Not a valid page slug or Wiki URL" in get_tool_result_text(result)
        mock_wiki_protocol.page_get_by_slug.assert_not_awaited()


class TestCallTool:
    async def test_a_wiki_error_becomes_a_plain_tool_error(self) -> None:
        raised = await _raised(_server_raising(PageNotFound(42)).call_tool("fails", {}))

        # Exactly ToolError: the SDK's UnexpectedToolError subclass is what it
        # masks and logs as a crash.
        assert type(raised) is ToolError
        assert isinstance(raised.__cause__, PageNotFound)
        assert str(raised) == "Error executing tool fails: Wiki page not found: 42"

    async def test_anything_else_is_left_as_the_sdk_reports_it(self) -> None:
        # A crash's text stays on the server from 2.1 on, which is the half of
        # the SDK's policy worth keeping: compared with the SDK's own
        # call_tool rather than a fixed string, so this holds on every 2.x.
        server = _server_raising(RuntimeError("redis://:hunter2@cache:6379"))

        ours = await _raised(server.call_tool("fails", {}))
        sdk = await _raised(MCPServer.call_tool(server, "fails", {}))

        assert (type(ours), str(ours)) == (type(sdk), str(sdk))
        assert isinstance(ours.__cause__, RuntimeError)
