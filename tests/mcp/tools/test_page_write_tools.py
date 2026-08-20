from unittest.mock import AsyncMock

from mcp import Client

from mcp_wiki.wiki.proto.types.pages import (
    ClonedPageRef,
    DeleteCommentResponse,
    GridDeleteResponse,
    WikiPage,
)
from mcp_wiki.yfm import MAX_WARNINGS
from tests.mcp.conftest import get_tool_result_content, get_tool_result_text


class TestPageWriteTools:
    async def test_grid_create_by_slug(
        self,
        client: Client,
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

        result = await client.call_tool(
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
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_update.return_value = {
            "id": "grid-1",
            "title": "Updated roadmap",
            "revision": "8",
        }

        result = await client.call_tool(
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
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_update",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "default_sort": [{"status": "asc", "priority": "desc"}],
            },
        )

        assert result.is_error is True
        assert "Extra inputs are not permitted" in get_tool_result_text(result)

    async def test_grid_add_rows(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_add_rows.return_value = {
            "revision": "8",
            "results": [{"id": "row-1", "row": ["todo"]}],
        }

        result = await client.call_tool(
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
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_add_rows",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "rows": [{"status": "todo"}],
                "position": 0,
                "after_row_id": "row-0",
            },
        )

        assert result.is_error is True
        assert "either position or after_row_id" in get_tool_result_text(result)

    async def test_grid_add_rows_accepts_numeric_after_row_id(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_add_rows.return_value = {
            "revision": "8",
            "results": [],
        }

        result = await client.call_tool(
            "grid_add_rows",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "rows": [{"status": "todo"}],
                "after_row_id": 5,
            },
        )

        assert result.is_error is False
        args = mock_wiki_protocol.grid_add_rows.await_args
        assert args.kwargs["after_row_id"] == "5"

    async def test_grid_delete(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_delete.return_value = GridDeleteResponse(
            grid_id="grid-1", deleted=True
        )

        result = await client.call_tool(
            "grid_delete",
            {"grid_id": "grid-1"},
        )

        content = get_tool_result_content(result)
        assert content["grid_id"] == "grid-1"
        assert content["deleted"] is True
        mock_wiki_protocol.grid_delete.assert_awaited_once()
        args = mock_wiki_protocol.grid_delete.await_args
        assert args.args[0] == "grid-1"

    async def test_grid_copy_by_page_id(
        self,
        client: Client,
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

        result = await client.call_tool(
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
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_update_cells.return_value = {
            "revision": "8",
            "cells": [{"row_id": 2, "column_slug": "status", "value": "done"}],
        }

        result = await client.call_tool(
            "grid_update_cells",
            {
                "grid_id": "grid-1",
                "cells": [
                    {"row_id": 2, "column_slug": "status", "value": "done"},
                    {"row_id": 2, "column_id": "col-2", "value": 100},
                ],
            },
        )

        content = get_tool_result_content(result)
        assert content["revision"] == "8"
        assert len(content["cells"]) == 1
        assert "results" not in content
        mock_wiki_protocol.grid_update_cells.assert_awaited_once()
        args = mock_wiki_protocol.grid_update_cells.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["cells"][0]["column_slug"] == "status"
        assert args.kwargs["cells"][1]["column_id"] == "col-2"

    async def test_grid_update_cells_rejects_an_empty_cell_list(
        self,
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_update_cells",
            {"grid_id": "grid-1", "cells": []},
        )

        assert result.is_error is True
        assert "cells must not be empty" in get_tool_result_text(result)

    async def test_grid_update_cells_rejects_invalid_patch(
        self,
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_update_cells",
            {
                "grid_id": "grid-1",
                "cells": [{"row_id": 2, "value": "done"}],
            },
        )

        assert result.is_error is True
        assert "exactly one of column_id or column_slug" in get_tool_result_text(result)

    async def test_grid_update_cells_rejects_empty_row_id(
        self,
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_update_cells",
            {
                "grid_id": "grid-1",
                "cells": [{"row_id": "  ", "column_slug": "status", "value": "done"}],
            },
        )

        assert result.is_error is True
        assert "must not be empty" in get_tool_result_text(result)

    async def test_grid_delete_rows(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_delete_rows.return_value = {"revision": "3"}

        result = await client.call_tool(
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
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_delete_rows",
            {
                "grid_id": "grid-1",
                "revision": "2",
                "row_ids": [],
            },
        )

        assert result.is_error is True
        assert "row_ids must not be empty" in get_tool_result_text(result)

    async def test_grid_add_columns(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_add_columns.return_value = {"revision": "8"}

        result = await client.call_tool(
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
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_add_columns",
            {
                "grid_id": "grid-1",
                "revision": "7",
                "columns": [],
            },
        )

        assert result.is_error is True
        assert "columns must not be empty" in get_tool_result_text(result)

    async def test_grid_delete_columns(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_delete_columns.return_value = {"revision": "9"}

        result = await client.call_tool(
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

    async def test_grid_move_row_by_position(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_move_row.return_value = {"revision": "10"}

        result = await client.call_tool(
            "grid_move_row",
            {
                "grid_id": "grid-1",
                "revision": "9",
                "row_id": "3",
                "position": 0,
            },
        )

        assert get_tool_result_content(result)["revision"] == "10"
        mock_wiki_protocol.grid_move_row.assert_awaited_once()
        args = mock_wiki_protocol.grid_move_row.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["row_id"] == "3"
        assert args.kwargs["position"] == 0
        assert args.kwargs["after_row_id"] is None

    async def test_grid_move_row_rejects_missing_target(
        self,
        client: Client,
    ) -> None:
        result = await client.call_tool(
            "grid_move_row",
            {
                "grid_id": "grid-1",
                "revision": "9",
                "row_id": "3",
            },
        )

        assert result.is_error is True
        assert "either position or after_row_id" in get_tool_result_text(result)

    async def test_grid_move_column(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_move_column.return_value = {"revision": "11"}

        result = await client.call_tool(
            "grid_move_column",
            {
                "grid_id": "grid-1",
                "revision": "10",
                "column_slug": "status",
                "position": 0,
            },
        )

        assert get_tool_result_content(result)["revision"] == "11"
        mock_wiki_protocol.grid_move_column.assert_awaited_once()
        args = mock_wiki_protocol.grid_move_column.await_args
        assert args.args[0] == "grid-1"
        assert args.kwargs["column_slug"] == "status"
        assert args.kwargs["position"] == 0

    async def test_page_create(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_create.return_value = WikiPage.model_validate(
            {
                "id": 10,
                "slug": "users/test/page",
                "title": "Created page",
            }
        )

        result = await client.call_tool(
            "page_create",
            {
                "slug": "users/test/page",
                "title": "Created page",
                "content": "content",
            },
        )

        content = get_tool_result_content(result)
        assert content["title"] == "Created page"
        assert not content.get("yfm_warnings")
        mock_wiki_protocol.page_create.assert_awaited_once()

    async def test_page_create_returns_yfm_warnings(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_create.return_value = WikiPage.model_validate(
            {"id": 10, "slug": "users/test/page", "title": "Created page"}
        )

        result = await client.call_tool(
            "page_create",
            {
                "slug": "users/test/page",
                "title": "Created page",
                "content": "> [!NOTE]\n> GFM alert\n\n{% note %}\nunclosed",
            },
        )

        warnings = get_tool_result_content(result)["yfm_warnings"]
        assert len(warnings) == 2
        assert "[!NOTE]" in warnings[0]
        assert "{% endnote %}" in warnings[1]

    async def test_page_update_by_slug(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "title": "Updated"}
        )

        result = await client.call_tool(
            "page_update",
            {"slug": "users/test/page", "content": "new content"},
        )

        content = get_tool_result_content(result)
        assert content["title"] == "Updated"
        assert not content.get("yfm_warnings")
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.page_update.assert_awaited_once()

    async def test_page_update_by_slug_warns_on_legacy_page_type(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_validate(
            {"id": 10, "page_type": "wiki"}
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "title": "Updated"}
        )

        result = await client.call_tool(
            "page_update",
            {"slug": "users/test/page", "content": "new content"},
        )

        warnings = get_tool_result_content(result)["yfm_warnings"]
        assert len(warnings) == 1
        assert "page_type='wiki'" in warnings[0]

    async def test_page_update_by_slug_warns_on_grid_page(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_validate(
            {"id": 10, "page_type": "grid"}
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "title": "Updated"}
        )

        result = await client.call_tool(
            "page_update",
            {"slug": "users/test/grid-page", "content": "new content"},
        )

        warnings = get_tool_result_content(result)["yfm_warnings"]
        assert len(warnings) == 1
        assert "grid" in warnings[0]
        assert "grid_* tools" in warnings[0]
        assert "legacy" not in warnings[0]

    async def test_page_clone_by_slug(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )
        mock_wiki_protocol.page_clone.return_value = ClonedPageRef.model_validate(
            {"id": 77, "slug": "users/test/copy"}
        )

        result = await client.call_tool(
            "page_clone",
            {"slug": "users/test/page", "target": "users/test/copy"},
        )

        assert get_tool_result_content(result)["slug"] == "users/test/copy"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.page_clone.assert_awaited_once()
        args = mock_wiki_protocol.page_clone.await_args
        assert args.args[0] == 10
        assert args.kwargs["target"] == "users/test/copy"
        assert args.kwargs["title"] is None

    async def test_page_clone_by_page_id_passes_the_title(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_clone.return_value = ClonedPageRef.model_validate(
            {"id": 78, "slug": "users/test/copy"}
        )

        result = await client.call_tool(
            "page_clone",
            {"page_id": 7, "target": "users/test/copy", "title": "Copy title"},
        )

        assert result.is_error is False
        mock_wiki_protocol.page_get_by_slug.assert_not_awaited()
        mock_wiki_protocol.page_get.assert_not_awaited()
        args = mock_wiki_protocol.page_clone.await_args
        assert args.args[0] == 7
        assert args.kwargs["title"] == "Copy title"

    async def test_page_clone_rejects_both_locators(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        result = await client.call_tool(
            "page_clone",
            {
                "page_id": 7,
                "slug": "users/test/page",
                "target": "users/test/copy",
            },
        )

        assert result.is_error is True
        assert "exactly one of page_id or slug" in get_tool_result_text(result)
        mock_wiki_protocol.page_clone.assert_not_awaited()

    async def test_page_update_rejects_missing_changes(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        result = await client.call_tool(
            "page_update",
            {"slug": "users/test/page", "is_silent": True},
        )

        assert result.is_error is True
        assert "at least one of title, content" in get_tool_result_text(result)
        mock_wiki_protocol.page_get_by_slug.assert_not_awaited()
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_update_sets_a_redirect_without_content(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "redirect": {"page_id": 77}}
        )

        result = await client.call_tool(
            "page_update",
            {"page_id": 10, "redirect_to_page_id": 77},
        )

        assert not get_tool_result_content(result).get("yfm_warnings")
        args = mock_wiki_protocol.page_update.await_args
        assert args.kwargs["redirect_to_page_id"] == 77
        assert args.kwargs["clear_redirect"] is False

    async def test_page_update_rejects_set_and_clear_redirect_together(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        result = await client.call_tool(
            "page_update",
            {"page_id": 10, "redirect_to_page_id": 77, "clear_redirect": True},
        )

        assert result.is_error is True
        assert "mutually exclusive" in get_tool_result_text(result)
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_update_warnings_capped_including_page_type(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_validate(
            {"id": 10, "page_type": "grid"}
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "title": "Updated"}
        )
        noisy_content = "\n\n".join("> [!NOTE]" for _ in range(30))

        result = await client.call_tool(
            "page_update",
            {"slug": "users/test/grid-page", "content": noisy_content},
        )

        warnings = get_tool_result_content(result)["yfm_warnings"]
        assert "grid_* tools" in warnings[0]
        assert "suppressed" in warnings[-1]
        assert len(warnings) == MAX_WARNINGS + 1

    async def test_page_update_title_only_skips_page_type_warning(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_validate(
            {"id": 10, "page_type": "grid"}
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "title": "Renamed"}
        )

        result = await client.call_tool(
            "page_update",
            {"slug": "users/test/grid-page", "title": "Renamed"},
        )

        assert not get_tool_result_content(result).get("yfm_warnings")

    async def test_page_append_content_warns_on_grid_page(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_validate(
            {"id": 10, "page_type": "grid"}
        )
        mock_wiki_protocol.page_append_content.return_value = WikiPage.model_validate(
            {"id": 10, "slug": "users/test/page", "title": "T"}
        )

        result = await client.call_tool(
            "page_append_content",
            {"slug": "users/test/grid-page", "content": "plain text"},
        )

        warnings = get_tool_result_content(result)["yfm_warnings"]
        assert len(warnings) == 1
        assert "grid_* tools" in warnings[0]

    async def test_page_update_by_id_skips_legacy_check(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_update.return_value = WikiPage.model_validate(
            {"id": 10, "title": "Updated"}
        )

        result = await client.call_tool(
            "page_update",
            {"page_id": 10, "title": "Updated"},
        )

        assert not get_tool_result_content(result).get("yfm_warnings")
        mock_wiki_protocol.page_get_by_slug.assert_not_awaited()

    async def test_page_append_content_adds_yfm_warnings_key(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_append_content.return_value = WikiPage.model_validate(
            {"id": 10, "slug": "users/test/page", "title": "T"}
        )

        result = await client.call_tool(
            "page_append_content",
            {
                "page_id": 10,
                "content": "<details><summary>x</summary>y</details>",
            },
        )

        content = get_tool_result_content(result)
        assert content["id"] == 10
        assert len(content["yfm_warnings"]) == 1
        assert "{% cut" in content["yfm_warnings"][0]

    async def test_page_append_content_clean_has_no_warnings_key(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_append_content.return_value = WikiPage.model_validate(
            {"id": 10, "slug": "users/test/page", "title": "T"}
        )

        result = await client.call_tool(
            "page_append_content",
            {"page_id": 10, "content": "## New section\n\nplain text"},
        )

        content = get_tool_result_content(result)
        assert content["id"] == 10
        assert "yfm_warnings" not in content

    async def test_page_delete_comment(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_delete_comment.return_value = (
            DeleteCommentResponse.model_construct(
                page_id=10, comment_id=11, deleted=True, comments_count=2
            )
        )

        result = await client.call_tool(
            "page_delete_comment",
            {"page_id": 10, "comment_id": 11},
        )

        content = get_tool_result_content(result)
        assert content["comments_count"] == 2
        # The id pair and `deleted` are the floor: an empty body must not
        # dump to `{}` and read as a successful call with no evidence in it.
        assert content["page_id"] == 10
        assert content["comment_id"] == 11
        assert content["deleted"] is True
        args = mock_wiki_protocol.page_delete_comment.await_args
        assert args.args[0] == 10
        assert args.kwargs["comment_id"] == 11

    async def test_page_delete_attachment_by_slug(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )
        mock_wiki_protocol.page_delete_attachment.return_value = {
            "page_id": 10,
            "file_id": 5,
            "deleted": True,
        }

        result = await client.call_tool(
            "page_delete_attachment",
            {"slug": "users/test/page", "file_id": 5},
        )

        assert get_tool_result_content(result)["deleted"] is True
        args = mock_wiki_protocol.page_delete_attachment.await_args
        assert args.args[0] == 10
        assert args.kwargs["file_id"] == 5

    async def test_page_upload_attachment(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_upload_attachment.return_value = {
            "page_id": 10,
            "attachments": [{"id": 1, "name": "file.zip"}],
            "appended_markup": False,
            "appended_content": None,
        }

        result = await client.call_tool(
            "page_upload_attachment",
            {"page_id": 10, "file_path": "C:\\temp\\file.zip"},
        )

        assert get_tool_result_content(result)["attachments"][0]["name"] == "file.zip"
        mock_wiki_protocol.page_upload_attachment.assert_awaited_once()

    async def test_page_download_attachment(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_download_attachment.return_value = {
            "page_id": 10,
            "file_id": 5,
            "path": "/home/user/report.pdf",
            "size_bytes": 2048,
            "mime_type": "application/pdf",
        }

        result = await client.call_tool(
            "page_download_attachment",
            {"page_id": 10, "file_id": 5, "save_to": "/home/user/report.pdf"},
        )

        content = get_tool_result_content(result)
        assert content["path"] == "/home/user/report.pdf"
        assert content["size_bytes"] == 2048
        args = mock_wiki_protocol.page_download_attachment.await_args
        assert args.args[0] == 10
        assert args.kwargs["file_id"] == 5
        assert args.kwargs["save_to"] == "/home/user/report.pdf"
        # Not passed explicitly, so the safe default must reach the client.
        assert args.kwargs["overwrite"] is False

    async def test_page_download_attachment_forwards_overwrite(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_download_attachment.return_value = {
            "page_id": 10,
            "file_id": 5,
            "path": "/home/user/report.pdf",
            "size_bytes": 1,
        }

        await client.call_tool(
            "page_download_attachment",
            {
                "page_id": 10,
                "file_id": 5,
                "save_to": "/home/user/report.pdf",
                "overwrite": True,
            },
        )

        args = mock_wiki_protocol.page_download_attachment.await_args
        assert args.kwargs["overwrite"] is True

    async def test_page_edit_by_id(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, slug="users/test/page", content="intro\nold line\noutro"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(
            id=10, slug="users/test/page", title="T"
        )

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "old line", "new_text": "new line"}],
            },
        )

        content = get_tool_result_content(result)
        assert content["page_id"] == 10
        assert content["occurrences_replaced"] == 1
        # the read asks only for content — the tool must not pull extra fields
        get_kwargs = mock_wiki_protocol.page_get.await_args.kwargs
        assert get_kwargs["fields"] == ["content"]
        update_args = mock_wiki_protocol.page_update.await_args
        assert update_args.args[0] == 10
        assert update_args.kwargs["content"] == "intro\nnew line\noutro"

    async def test_page_edit_by_slug(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10, slug="users/test/page", content="hello world"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(
            id=10, slug="users/test/page"
        )

        result = await client.call_tool(
            "page_edit",
            {
                "slug": "users/test/page",
                "replacements": [{"old_text": "world", "new_text": "wiki"}],
            },
        )

        assert get_tool_result_content(result)["slug"] == "users/test/page"
        get_kwargs = mock_wiki_protocol.page_get_by_slug.await_args.kwargs
        assert get_kwargs["fields"] == ["content"]
        assert mock_wiki_protocol.page_update.await_args.kwargs["content"] == (
            "hello wiki"
        )

    async def test_page_edit_replace_all_counts_occurrences(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="x a x b x"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(id=10)

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [
                    {"old_text": "x", "new_text": "y", "replace_all": True}
                ],
            },
        )

        content = get_tool_result_content(result)
        assert content["occurrences_replaced"] == 3
        assert mock_wiki_protocol.page_update.await_args.kwargs["content"] == (
            "y a y b y"
        )

    async def test_page_edit_applies_replacements_sequentially(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # The second old_text exists only in the output of the first —
        # multi-edit semantics, each entry sees the already-edited content.
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="alpha"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(id=10)

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [
                    {"old_text": "alpha", "new_text": "beta"},
                    {"old_text": "beta", "new_text": "gamma"},
                ],
            },
        )

        # Sequential: the second old_text matches what the first produced, so
        # both entries counted even though the page held one occurrence.
        assert get_tool_result_content(result)["occurrences_replaced"] == 2
        assert mock_wiki_protocol.page_update.await_args.kwargs["content"] == "gamma"

    async def test_page_edit_missing_text_writes_nothing(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="some page text"
        )

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "absent", "new_text": "x"}],
            },
        )

        assert result.is_error is True
        assert "not found" in get_tool_result_text(result)
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_edit_ambiguous_text_reports_lines_and_writes_nothing(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="intro\nfoo\nmiddle\nfoo\n"
        )

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "foo", "new_text": "bar"}],
            },
        )

        assert result.is_error is True
        text = get_tool_result_text(result)
        assert "occurs 2 times" in text
        assert "lines 2, 4" in text
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_edit_ambiguity_line_list_is_capped(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # Six occurrences, cap five: the line scan must stop at the cap (not
        # walk the whole page) and the error must say the list is incomplete.
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="foo\n" * 6
        )

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "foo", "new_text": "bar"}],
            },
        )

        assert result.is_error is True
        text = get_tool_result_text(result)
        assert "occurs 6 times" in text
        assert "lines 1, 2, 3, 4, 5 and more" in text
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_edit_writes_with_allow_merge_by_default(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # The read-modify-write has no revision to lock against, so without
        # allow_merge a concurrent edit landing between the read and the write
        # is overwritten. page_append_content's anchor fallback — the only
        # other read-modify-write here — passes it for the same reason.
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="alpha"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(id=10)

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "alpha", "new_text": "beta"}],
            },
        )

        assert result.is_error is False
        assert mock_wiki_protocol.page_update.await_args.kwargs["allow_merge"] is True
        assert mock_wiki_protocol.page_update.await_args.kwargs["is_silent"] is False

    async def test_page_edit_forwards_the_write_flags(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="alpha"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(id=10)

        await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "alpha", "new_text": "beta"}],
                "allow_merge": False,
                "is_silent": True,
            },
        )

        kwargs = mock_wiki_protocol.page_update.await_args.kwargs
        assert kwargs["allow_merge"] is False
        assert kwargs["is_silent"] is True

    async def test_page_edit_ambiguity_lines_match_the_reported_count(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # "--" inside a "----" rule: str.count is non-overlapping, so the line
        # list must be too, or the error names more positions than occurrences.
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, content="----\ntail"
        )

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "--", "new_text": "=="}],
            },
        )

        assert result.is_error is True
        text = get_tool_result_text(result)
        assert "occurs 2 times" in text
        assert "lines 1, 1)" in text
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_edit_is_not_advertised_as_idempotent(
        self,
        client: Client,
    ) -> None:
        # A replacement whose new_text contains its own old_text applies again
        # on a repeat, so a client must not retry it on the strength of a hint.
        listing = await client.list_tools()
        tool = next(t for t in listing.tools if t.name == "page_edit")
        assert tool.annotations is not None
        assert tool.annotations.idempotent_hint is not True

    async def test_page_edit_rejects_identical_old_and_new(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # A no-op replacement is an agent mistake — the schema refuses it
        # before any HTTP.
        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "same", "new_text": "same"}],
            },
        )

        assert result.is_error is True
        mock_wiki_protocol.page_get.assert_not_awaited()
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_edit_rejects_empty_replacements(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        result = await client.call_tool(
            "page_edit",
            {"page_id": 10, "replacements": []},
        )

        assert result.is_error is True
        mock_wiki_protocol.page_get.assert_not_awaited()

    async def test_page_edit_refuses_a_page_without_text_content(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, page_type="grid", content=None
        )

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "a", "new_text": "b"}],
            },
        )

        assert result.is_error is True
        assert "no editable text content" in get_tool_result_text(result)
        mock_wiki_protocol.page_update.assert_not_awaited()

    async def test_page_edit_warns_on_legacy_page_type(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10, page_type="wiki", content="old text here"
        )
        mock_wiki_protocol.page_update.return_value = WikiPage.model_construct(id=10)

        result = await client.call_tool(
            "page_edit",
            {
                "page_id": 10,
                "replacements": [{"old_text": "old text", "new_text": "new text"}],
            },
        )

        warnings = get_tool_result_content(result)["yfm_warnings"]
        assert len(warnings) == 1
        assert "page_type='wiki'" in warnings[0]
