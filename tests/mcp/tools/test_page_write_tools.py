from unittest.mock import AsyncMock

from mcp.client.session import ClientSession

from mcp_wiki.wiki.proto.types.pages import WikiPage
from tests.mcp.conftest import get_tool_result_content, get_tool_result_text


class TestPageWriteTools:
    async def test_grid_create_by_slug(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )
        mock_wiki_protocol.grid_create.return_value = {
            "id": "grid-1",
            "title": "Roadmap",
            "page": {"id": 10},
        }

        result = await client_session.call_tool(
            "grid_create",
            {"slug": "users/test/page", "title": "Roadmap"},
        )

        assert get_tool_result_content(result)["title"] == "Roadmap"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.grid_create.assert_awaited_once()
        request = mock_wiki_protocol.grid_create.await_args.kwargs["request"]
        assert request.title == "Roadmap"
        assert request.page.id == 10

    async def test_grid_update(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_update.return_value = {
            "id": "grid-1",
            "title": "Updated roadmap",
            "revision": "8",
        }

        result = await client_session.call_tool(
            "grid_update",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "title": "Updated roadmap",
                "default_sort": [{"column": "status", "direction": "asc"}],
            },
        )

        assert get_tool_result_content(result)["revision"] == "8"
        mock_wiki_protocol.grid_update.assert_awaited_once()
        args = mock_wiki_protocol.grid_update.await_args
        assert args.args[0] == "grid-1"
        request = args.kwargs["request"]
        assert request.revision == "7"
        assert request.title == "Updated roadmap"
        assert request.default_sort == [{"status": "asc"}]

    async def test_grid_update_rejects_invalid_default_sort_shape(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool(
            "grid_update",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "default_sort": [{"status": "asc", "priority": "desc"}],
            },
        )

        assert result.isError is True
        assert "Extra inputs are not permitted" in get_tool_result_text(result)

    async def test_grid_add_rows(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_add_rows.return_value = {
            "revision": "8",
            "results": [{"id": "row-1", "row": ["todo"]}],
        }

        result = await client_session.call_tool(
            "grid_add_rows",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "rows": [{"status": "todo"}],
                "after_row_id": "row-0",
            },
        )

        assert get_tool_result_content(result)["revision"] == "8"
        mock_wiki_protocol.grid_add_rows.assert_awaited_once()
        args = mock_wiki_protocol.grid_add_rows.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["revision"] == "7"
        assert args.kwargs["rows"] == [{"status": "todo"}]
        assert args.kwargs["position"] is None
        assert args.kwargs["after_row_id"] == "row-0"

    async def test_grid_add_rows_rejects_conflicting_position_inputs(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool(
            "grid_add_rows",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "rows": [{"status": "todo"}],
                "position": 0,
                "after_row_id": "row-0",
            },
        )

        assert result.isError is True
        assert "either position or after_row_id" in get_tool_result_text(result)

    async def test_grid_delete(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_delete.return_value = {}

        result = await client_session.call_tool(
            "grid_delete",
            {"grid_id": "grid-1"},
        )

        assert get_tool_result_content(result) == {}
        mock_wiki_protocol.grid_delete.assert_awaited_once()
        args = mock_wiki_protocol.grid_delete.await_args
        assert args.args[0] == "grid-1"

    async def test_grid_copy_by_page_id(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=20,
            slug="users/test/target-page",
        )
        mock_wiki_protocol.grid_copy.return_value = {
            "operation": {"type": "clone_inline_grid", "id": "op-1"},
            "dry_run": False,
            "status_url": "/v1/operations/clone_inline_grid/op-1",
        }

        result = await client_session.call_tool(
            "grid_copy",
            {
                "grid_id": "grid-1",
                "page_id": 20,
                "title": "Copied grid",
            },
        )

        assert get_tool_result_content(result)["operation"]["id"] == "op-1"
        mock_wiki_protocol.page_get.assert_awaited_once()
        mock_wiki_protocol.grid_copy.assert_awaited_once()
        args = mock_wiki_protocol.grid_copy.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["target"] == "users/test/target-page"
        assert args.kwargs["title"] == "Copied grid"

    async def test_grid_update_cells(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_update_cells.return_value = {
            "revision": "8",
            "results": [],
        }

        result = await client_session.call_tool(
            "grid_update_cells",
            {
                "grid_id": "grid-1",
                "cells": [
                    {"row_id": 2, "column_slug": "status", "value": "done"},
                    {"row_id": 2, "column_id": "col-2", "value": 100},
                ],
            },
        )

        assert get_tool_result_content(result)["revision"] == "8"
        mock_wiki_protocol.grid_update_cells.assert_awaited_once()
        args = mock_wiki_protocol.grid_update_cells.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["cells"][0]["column_slug"] == "status"
        assert args.kwargs["cells"][1]["column_id"] == "col-2"

    async def test_grid_update_cells_rejects_invalid_patch(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool(
            "grid_update_cells",
            {
                "grid_id": "grid-1",
                "cells": [{"row_id": 2, "value": "done"}],
            },
        )

        assert result.isError is True
        assert "exactly one of column_id or column_slug" in get_tool_result_text(result)

    async def test_grid_delete_rows(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_delete_rows.return_value = {"revision": "3"}

        result = await client_session.call_tool(
            "grid_delete_rows",
            {
                "grid_id": "grid-1",
                "revision": "2",
                "row_ids": ["1", 2],
            },
        )

        assert get_tool_result_content(result)["revision"] == "3"
        mock_wiki_protocol.grid_delete_rows.assert_awaited_once()
        args = mock_wiki_protocol.grid_delete_rows.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["revision"] == "2"
        assert args.kwargs["row_ids"] == ["1", "2"]

    async def test_grid_delete_rows_rejects_empty_row_ids(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool(
            "grid_delete_rows",
            {
                "grid_id": "grid-1",
                "revision": "2",
                "row_ids": [],
            },
        )

        assert result.isError is True
        assert "row_ids must not be empty" in get_tool_result_text(result)

    async def test_grid_add_columns(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_add_columns.return_value = {"revision": "8"}

        result = await client_session.call_tool(
            "grid_add_columns",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "columns": [
                    {
                        "slug": "status",
                        "title": "Status",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "slug": "done",
                        "title": "Done",
                        "type": "checkbox",
                        "required": False,
                    },
                ],
                "position": 1,
            },
        )

        assert get_tool_result_content(result)["revision"] == "8"
        mock_wiki_protocol.grid_add_columns.assert_awaited_once()
        args = mock_wiki_protocol.grid_add_columns.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["revision"] == "7"
        assert args.kwargs["position"] == 1
        assert args.kwargs["columns"][0]["slug"] == "status"

    async def test_grid_add_columns_rejects_empty_columns(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool(
            "grid_add_columns",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "columns": [],
            },
        )

        assert result.isError is True
        assert "columns must not be empty" in get_tool_result_text(result)

    async def test_grid_delete_columns(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_delete_columns.return_value = {"revision": "9"}

        result = await client_session.call_tool(
            "grid_delete_columns",
            {
                "grid_id": "grid-1",
                "revision": "8",
                "column_slugs": ["obsolete"],
            },
        )

        assert get_tool_result_content(result)["revision"] == "9"
        mock_wiki_protocol.grid_delete_columns.assert_awaited_once()
        args = mock_wiki_protocol.grid_delete_columns.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["column_slugs"] == ["obsolete"]

    async def test_grid_move_rows_by_position(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_move_rows.return_value = {"revision": "10"}

        result = await client_session.call_tool(
            "grid_move_rows",
            {
                "grid_id": "grid-1",
                "revision": "9",
                "row_id": "3",
                "position": 0,
            },
        )

        assert get_tool_result_content(result)["revision"] == "10"
        mock_wiki_protocol.grid_move_rows.assert_awaited_once()
        args = mock_wiki_protocol.grid_move_rows.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["row_id"] == "3"
        assert args.kwargs["position"] == 0
        assert args.kwargs["after_row_id"] is None

    async def test_grid_move_rows_rejects_missing_target(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool(
            "grid_move_rows",
            {
                "grid_id": "grid-1",
                "revision": "9",
                "row_id": "3",
            },
        )

        assert result.isError is True
        assert "either position or after_row_id" in get_tool_result_text(result)

    async def test_grid_move_columns(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_move_columns.return_value = {"revision": "11"}

        result = await client_session.call_tool(
            "grid_move_columns",
            {
                "grid_id": "grid-1",
                "revision": "10",
                "column_slug": "status",
                "position": 0,
            },
        )

        assert get_tool_result_content(result)["revision"] == "11"
        mock_wiki_protocol.grid_move_columns.assert_awaited_once()
        args = mock_wiki_protocol.grid_move_columns.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["column_slug"] == "status"
        assert args.kwargs["position"] == 0

    async def test_page_create(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_create.return_value = {
            "id": 10,
            "slug": "users/test/page",
            "title": "Created page",
        }

        result = await client_session.call_tool(
            "page_create",
            {
                "slug": "users/test/page",
                "title": "Created page",
                "content": "content",
            },
        )

        assert get_tool_result_content(result)["title"] == "Created page"
        mock_wiki_protocol.page_create.assert_awaited_once()

    async def test_page_update_by_slug(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )
        mock_wiki_protocol.page_update.return_value = {
            "id": 10,
            "title": "Updated",
        }

        result = await client_session.call_tool(
            "page_update",
            {"slug": "users/test/page", "content": "new content"},
        )

        assert get_tool_result_content(result)["title"] == "Updated"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.page_update.assert_awaited_once()

    async def test_page_upload_attachment(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_upload_attachment.return_value = {
            "page_id": 10,
            "attachments": [{"id": 1, "name": "file.zip"}],
            "appended_markup": False,
            "appended_content": None,
        }

        result = await client_session.call_tool(
            "page_upload_attachment",
            {"page_id": 10, "file_path": "C:\\temp\\file.zip"},
        )

        assert get_tool_result_content(result)["attachments"][0]["name"] == "file.zip"
        mock_wiki_protocol.page_upload_attachment.assert_awaited_once()
