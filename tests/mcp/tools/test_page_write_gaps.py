"""Write tools whose bodies and refusals nothing else exercises.

Three registered tools (`page_add_comment`, `page_delete`, `page_recover`)
had no test at this layer at all: slug resolution, argument forwarding and
auth passing went unchecked. The rest here are the refusals — the arguments
an LLM plausibly produces, where the tool must name the field instead of
letting the Wiki API answer with something obscure.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.client.session import ClientSession

from mcp_wiki.wiki.proto.types.pages import (
    DeletePageResponse,
    PageComment,
    RecoverPageResponse,
    WikiPage,
)
from tests.mcp.conftest import get_tool_result_content, get_tool_result_text


class TestPageAddComment:
    async def test_by_page_id(
        self, client_session: ClientSession, mock_wiki_protocol: AsyncMock
    ) -> None:
        mock_wiki_protocol.page_add_comment.return_value = PageComment.model_construct(
            id=7, body="looks good"
        )

        result = await client_session.call_tool(
            "page_add_comment", {"page_id": 42, "body": "looks good"}
        )

        assert get_tool_result_content(result)["id"] == 7
        call = mock_wiki_protocol.page_add_comment.await_args
        assert call.args[0] == 42
        assert call.kwargs["body"] == "looks good"
        assert call.kwargs["parent_id"] is None
        assert "auth" in call.kwargs

    async def test_by_slug_resolves_the_page_first(
        self, client_session: ClientSession, mock_wiki_protocol: AsyncMock
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=99
        )
        mock_wiki_protocol.page_add_comment.return_value = PageComment.model_construct(
            id=8
        )

        await client_session.call_tool(
            "page_add_comment",
            {"slug": "users/test/page", "body": "re", "parent_id": 7, "thread_id": 3},
        )

        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        call = mock_wiki_protocol.page_add_comment.await_args
        assert call.args[0] == 99
        assert call.kwargs["parent_id"] == 7
        assert call.kwargs["thread_id"] == 3


class TestPageDeleteAndRecover:
    async def test_delete_returns_the_recovery_token(
        self, client_session: ClientSession, mock_wiki_protocol: AsyncMock
    ) -> None:
        mock_wiki_protocol.page_delete.return_value = (
            DeletePageResponse.model_construct(recovery_token="rt-1")
        )

        result = await client_session.call_tool("page_delete", {"page_id": 42})

        assert get_tool_result_content(result)["recovery_token"] == "rt-1"
        assert mock_wiki_protocol.page_delete.await_args.args[0] == 42

    async def test_delete_by_slug_resolves_the_page_first(
        self, client_session: ClientSession, mock_wiki_protocol: AsyncMock
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=77
        )
        mock_wiki_protocol.page_delete.return_value = (
            DeletePageResponse.model_construct(recovery_token="rt-2")
        )

        await client_session.call_tool("page_delete", {"slug": "users/test/page"})

        assert mock_wiki_protocol.page_delete.await_args.args[0] == 77

    async def test_recover_forwards_the_token(
        self, client_session: ClientSession, mock_wiki_protocol: AsyncMock
    ) -> None:
        mock_wiki_protocol.page_recover.return_value = (
            RecoverPageResponse.model_construct(id=42, slug="users/test/page")
        )

        result = await client_session.call_tool(
            "page_recover", {"recovery_token": "rt-1"}
        )

        assert get_tool_result_content(result)["id"] == 42
        call = mock_wiki_protocol.page_recover.await_args
        assert call.args[0] == "rt-1"
        assert "auth" in call.kwargs


class TestRefusals:
    """Each names the offending field rather than deferring to the API."""

    @pytest.mark.parametrize(
        ("tool", "arguments", "expected"),
        [
            (
                "grid_create",
                {"page_id": 1, "title": "   "},
                "title must not be empty",
            ),
            (
                "grid_delete",
                {"grid_id": "   "},
                "grid_id must not be empty",
            ),
            (
                "grid_delete_columns",
                {"grid_id": "g-1", "revision": "r1", "column_slugs": []},
                "column_slugs must not be empty",
            ),
            (
                "grid_delete_columns",
                {"grid_id": "g-1", "revision": "r1", "column_slugs": ["  "]},
                "column_slugs[0] must not be empty",
            ),
            (
                "grid_delete_rows",
                {"grid_id": "g-1", "revision": "r1", "row_ids": []},
                "row_ids must not be empty",
            ),
            (
                "grid_add_rows",
                {"grid_id": "g-1", "revision": "r1", "rows": []},
                "rows must not be empty",
            ),
            (
                "grid_update",
                {"grid_id": "g-1", "revision": "r1", "default_sort": []},
                "default_sort must not be empty",
            ),
            (
                "grid_update",
                {"grid_id": "g-1", "revision": "r1"},
                "Provide at least one of title or default_sort",
            ),
            (
                "grid_add_columns",
                {"grid_id": "g-1", "revision": "r1", "columns": []},
                "columns must not be empty",
            ),
            (
                "grid_update_cells",
                {"grid_id": "g-1", "cells": []},
                "cells must not be empty",
            ),
            (
                "grid_move_row",
                {
                    "grid_id": "g-1",
                    "revision": "r1",
                    "row_id": "r",
                    "position": 1,
                    "after_row_id": "r2",
                },
                "Provide either position or after_row_id, not both",
            ),
            (
                "grid_move_row",
                {"grid_id": "g-1", "revision": "r1", "row_id": "r"},
                "Provide either position or after_row_id",
            ),
            (
                "grid_add_rows",
                {
                    "grid_id": "g-1",
                    "revision": "r1",
                    "rows": [{"a": 1}],
                    "position": 1,
                    "after_row_id": "r2",
                },
                "Provide either position or after_row_id, not both",
            ),
        ],
    )
    async def test_refusal_names_the_field(
        self,
        client_session: ClientSession,
        tool: str,
        arguments: dict[str, Any],
        expected: str,
    ) -> None:
        result = await client_session.call_tool(tool, arguments)

        assert result.isError is True
        assert expected in get_tool_result_text(result)


class TestSlugResolutionFailure:
    async def test_a_page_without_a_slug_is_reported(
        self, client_session: ClientSession, mock_wiki_protocol: AsyncMock
    ) -> None:
        # Tools that need a slug resolve it from the id; the API answering
        # without one leaves nothing to send.
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(id=42)

        result = await client_session.call_tool("page_get_descendants", {"page_id": 42})

        assert result.isError is True
        assert "does not have a slug" in get_tool_result_text(result)
