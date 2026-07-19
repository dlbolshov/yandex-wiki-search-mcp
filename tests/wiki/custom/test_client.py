import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import WikiApiError
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    WikiGridPageRef,
)
from tests.aioresponses_utils import RequestCapture


class TestWikiClient:
    async def test_build_headers_with_token_and_org(
        self,
        wiki_client: WikiClient,
    ) -> None:
        headers = await wiki_client._build_headers()
        assert headers["Authorization"] == "OAuth test-token"
        assert headers["X-Org-Id"] == "test-org"

    async def test_page_get_by_slug(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={"id": 10, "slug": "users/test/page", "title": "Page title"}
        )
        with aioresponses() as mocked:
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages.*"),
                callback=capture.callback,
            )
            page = await wiki_client.page_get_by_slug("users/test/page")

        assert page.id == 10
        capture.assert_called_once()
        capture.last_request.assert_headers(
            {
                "Authorization": "OAuth test-token",
                "X-Org-Id": "test-org",
            }
        )
        capture.last_request.assert_params({"slug": "users/test/page"})

    async def test_page_search(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(
            payload={
                "results": [
                    {
                        "url": "/a/b",
                        "slug": "a/b",
                        "title": "T",
                        "body": "snip",
                        "type": "page",
                        "modified_at": 1778104120,
                    },
                    {
                        "url": "https://wiki.yandex.ru/a/b/.files/f.xlsx?download=1",
                        "slug": "a/b",
                        "title": "f.xlsx",
                        "body": "",
                        "type": "file",
                        "modified_at": 1769154990,
                    },
                ],
                "total_documents": 2,
                "total_pages": 1,
                "page_id": 1,
                "search_client": "mailsearch",
                "uid": "1",
            }
        )
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            result = await wiki_client.page_search("query text", page_size=50)

        assert result.results[0].slug == "a/b"
        assert result.results[1].type == "file"
        capture.assert_called_once()
        capture.last_request.assert_json_body({"query": "query text", "page_size": 50})

    async def test_page_search_clamps_page_size(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(
            payload={"results": [], "total_documents": 0, "total_pages": 0}
        )
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            await wiki_client.page_search("q", page_size=1000)
        capture.last_request.assert_json_field("page_size", 50)

    async def test_page_search_raises_api_error_with_list_message(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(
            status=404,
            payload={
                "debug_message": "",
                "error_code": "NOT_FOUND",
                "level": "ERROR",
                "message": ["Страница не найдена"],
            },
        )
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            with pytest.raises(WikiApiError) as exc_info:
                await wiki_client.page_search("q")

        assert exc_info.value.error_code == "NOT_FOUND"
        assert exc_info.value.message == ["Страница не найдена"]
        assert "message=Страница не найдена" in str(exc_info.value)

    async def test_page_search_raises_api_error_on_non_dict_json_body(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(status=502, payload=["upstream error"])
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            with pytest.raises(WikiApiError) as exc_info:
                await wiki_client.page_search("q")

        assert exc_info.value.status == 502
        assert exc_info.value.error_code is None
        assert exc_info.value.message is None

    async def test_page_get_grids(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "results": [{"id": "grid-1", "title": "Roadmap"}],
                "next_cursor": "next-cursor",
                "prev_cursor": None,
            }
        )

        with aioresponses() as mocked:
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/10/grids.*"),
                callback=capture.callback,
            )
            grids = await wiki_client.page_get_grids(
                10,
                page_size=25,
                cursor="cursor-1",
                order_by="title",
                order_direction="asc",
            )

        assert grids.results[0].id == "grid-1"
        assert grids.next_cursor == "next-cursor"
        capture.assert_called_once()
        assert str(capture.last_request.params["page_size"]) == "25"
        assert capture.last_request.params["cursor"] == "cursor-1"
        assert capture.last_request.params["order_by"] == "title"
        assert capture.last_request.params["order_direction"] == "asc"

    async def test_grid_get(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "id": "grid-1",
                "title": "Roadmap",
                "revision": "7",
                "structure": {
                    "columns": [{"id": "col-1", "slug": "status", "title": "Status"}]
                },
                "rows": [{"id": "row-1", "row": ["done"]}],
            }
        )

        with aioresponses() as mocked:
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/grids/grid-1.*"),
                callback=capture.callback,
            )
            grid = await wiki_client.grid_get(
                "grid-1",
                fields=["attributes", "user_permissions"],
                filter="[status] = done",
                only_cols="status",
                only_rows="row-1",
                revision="7",
                sort="status",
            )

        assert grid.id == "grid-1"
        assert grid.revision == "7"
        assert grid.structure is not None
        assert grid.structure.columns[0].slug == "status"
        assert grid.rows[0].row == ["done"]
        capture.assert_called_once()
        capture.last_request.assert_params(
            {
                "fields": "attributes,user_permissions",
                "filter": "[status] = done",
                "only_cols": "status",
                "only_rows": "row-1",
                "revision": "7",
                "sort": "status",
            }
        )

    async def test_grid_create(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "id": "grid-1",
                "title": "Roadmap",
                "page": {"id": 10, "slug": "users/test/page"},
                "revision": "1",
            }
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids",
                callback=capture.callback,
            )
            grid = await wiki_client.grid_create(
                request=GridCreateRequest(title="Roadmap", page=WikiGridPageRef(id=10))
            )

        assert grid.id == "grid-1"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "title": "Roadmap",
                "page": {"id": 10},
            }
        )

    async def test_grid_update(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "id": "grid-1",
                "title": "Updated roadmap",
                "revision": "8",
            }
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1",
                callback=capture.callback,
            )
            grid = await wiki_client.grid_update(
                "grid-1",
                request=GridUpdateRequest(
                    revision="7",
                    title="Updated roadmap",
                    default_sort=[{"status": "asc"}],
                ),
            )

        assert grid.revision == "8"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "revision": "7",
                "title": "Updated roadmap",
                "default_sort": [{"status": "asc"}],
            }
        )

    async def test_grid_add_rows(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "revision": "8",
                "results": [{"id": "row-1", "row": ["todo"], "pinned": False}],
            }
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/rows",
                callback=capture.callback,
            )
            result = await wiki_client.grid_add_rows(
                "grid-1",
                revision="7",
                rows=[{"status": "todo"}],
                after_row_id="row-0",
            )

        assert result.revision == "8"
        assert result.results[0].id == "row-1"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "revision": "7",
                "rows": [{"status": "todo"}],
                "after_row_id": "row-0",
            }
        )

    async def test_grid_delete(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(status=204)

        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/grids/grid-1",
                callback=capture.callback,
            )
            result = await wiki_client.grid_delete("grid-1")

        assert result == {}
        capture.assert_called_once()

    async def test_grid_copy(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "operation": {"type": "clone_inline_grid", "id": "op-1"},
                "dry_run": False,
                "status_url": "/v1/operations/clone_inline_grid/op-1",
            }
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/clone",
                callback=capture.callback,
            )
            result = await wiki_client.grid_copy(
                "grid-1",
                target="users/test/target-page",
                title="Copied grid",
            )

        assert result.operation is not None
        assert result.operation.id == "op-1"
        assert result.status_url == "/v1/operations/clone_inline_grid/op-1"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "target": "users/test/target-page",
                "title": "Copied grid",
            }
        )

    async def test_grid_update_cells(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "8"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/cells",
                callback=capture.callback,
            )
            result = await wiki_client.grid_update_cells(
                "grid-1",
                cells=[
                    {"row_id": 2, "column_slug": "id", "value": 22},
                    {"row_id": 2, "column_slug": "name", "value": "Done"},
                ],
            )

        assert result.revision == "8"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "cells": [
                    {"row_id": 2, "column_slug": "id", "value": 22},
                    {"row_id": 2, "column_slug": "name", "value": "Done"},
                ]
            }
        )

    async def test_grid_delete_rows(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "3"})

        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/grids/grid-1/rows",
                callback=capture.callback,
            )
            result = await wiki_client.grid_delete_rows(
                "grid-1",
                revision="2",
                row_ids=["1", "2"],
            )

        assert result.revision == "3"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "revision": "2",
                "row_ids": ["1", "2"],
            }
        )

    async def test_grid_add_columns(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "8"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/columns",
                callback=capture.callback,
            )
            result = await wiki_client.grid_add_columns(
                "grid-1",
                revision="7",
                columns=[
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
                position=1,
            )

        assert result.revision == "8"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
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
            }
        )

    async def test_grid_delete_columns(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "9"})

        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/grids/grid-1/columns",
                callback=capture.callback,
            )
            result = await wiki_client.grid_delete_columns(
                "grid-1",
                revision="8",
                column_slugs=["obsolete"],
            )

        assert result.revision == "9"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "revision": "8",
                "column_slugs": ["obsolete"],
            }
        )

    async def test_grid_move_rows(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "10"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/rows/move",
                callback=capture.callback,
            )
            result = await wiki_client.grid_move_rows(
                "grid-1",
                revision="9",
                row_id="3",
                position=0,
            )

        assert result.revision == "10"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "revision": "9",
                "row_id": "3",
                "position": 0,
            }
        )

    async def test_grid_move_columns(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "11"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/columns/move",
                callback=capture.callback,
            )
            result = await wiki_client.grid_move_columns(
                "grid-1",
                revision="10",
                column_slug="status",
                position=0,
            )

        assert result.revision == "11"
        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "revision": "10",
                "column_slug": "status",
                "position": 0,
            }
        )

    async def test_page_upload_attachment(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "example.txt"
            file_path.write_text("hello wiki", encoding="utf-8")

            upload_capture = RequestCapture(payload={"session_id": "session-1"})
            upload_part_capture = RequestCapture()
            finish_capture = RequestCapture()
            attach_capture = RequestCapture(
                payload={
                    "results": [
                        {
                            "id": 1,
                            "name": "example.txt",
                            "download_url": "https://wiki.yandex.net/file/example.txt",
                        }
                    ]
                }
            )

            with aioresponses() as mocked:
                mocked.post(
                    "https://api.wiki.yandex.net/v1/upload_sessions",
                    callback=upload_capture.callback,
                )
                mocked.put(
                    re.compile(
                        r"https://api\.wiki\.yandex\.net/v1/upload_sessions/session-1/upload_part.*"
                    ),
                    callback=upload_part_capture.callback,
                )
                mocked.post(
                    "https://api.wiki.yandex.net/v1/upload_sessions/session-1/finish",
                    callback=finish_capture.callback,
                )
                mocked.post(
                    "https://api.wiki.yandex.net/v1/pages/10/attachments",
                    callback=attach_capture.callback,
                )

                result = await wiki_client.page_upload_attachment(
                    10,
                    file_path=str(file_path),
                )

        assert result.page_id == 10
        assert result.attachments[0].name == "example.txt"
        upload_capture.assert_called_once()
        upload_part_capture.assert_called_once()
        finish_capture.assert_called_once()
        attach_capture.assert_called_once()

    async def test_page_append_content_with_anchor(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"id": 10, "slug": "users/test/page"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/append-content",
                callback=capture.callback,
            )
            await wiki_client.page_append_content(
                10,
                content="Anchored block",
                anchor="#release-notes",
            )

        capture.assert_called_once()
        capture.last_request.assert_json_body(
            {
                "content": "Anchored block",
                "anchor": {"name": "#release-notes"},
            }
        )

    async def test_page_append_content_anchor_not_found_raises_wiki_api_error(
        self,
        wiki_client: WikiClient,
    ) -> None:
        append_capture = RequestCapture(
            status=400,
            body=(
                '{"error_code":"ANCHOR_NOT_FOUND","debug_message":"Anchor not found","message":null}'
            ),
        )
        get_capture = RequestCapture(
            payload={
                "id": 10,
                "slug": "users/test/page",
                "content": "# Root\n\nNo explicit anchors here.\n\nBody",
            }
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/append-content",
                callback=append_capture.callback,
            )
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/10.*"),
                callback=get_capture.callback,
            )
            try:
                await wiki_client.page_append_content(
                    10,
                    content="Anchored block",
                    anchor="#release-notes",
                )
            except WikiApiError as exc:
                assert exc.status == 400
                assert exc.error_code == "ANCHOR_NOT_FOUND"
                assert exc.debug_message == "Anchor not found"
            else:  # pragma: no cover
                raise AssertionError("Expected WikiApiError to be raised")
        append_capture.assert_called_once()
        get_capture.assert_called_once()

    async def test_page_append_content_falls_back_to_source_anchor_replace(
        self,
        wiki_client: WikiClient,
    ) -> None:
        append_capture = RequestCapture(
            status=400,
            body=(
                '{"error_code":"ANCHOR_NOT_FOUND","debug_message":"Anchor not found","message":null}'
            ),
        )
        get_capture = RequestCapture(
            payload={
                "id": 10,
                "slug": "users/test/page",
                "content": "# Root\n\n## Section {#release-notes}\n\nBody",
            }
        )
        update_capture = RequestCapture(
            payload={"id": 10, "slug": "users/test/page", "title": "Updated"}
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/append-content",
                callback=append_capture.callback,
            )
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/10.*"),
                callback=get_capture.callback,
            )
            mocked.post(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/10.*"),
                callback=update_capture.callback,
            )
            result = await wiki_client.page_append_content(
                10,
                content="\n\nAppended under anchor.",
                anchor="#release-notes",
            )

        assert result["id"] == 10
        append_capture.assert_called_once()
        get_capture.assert_called_once()
        update_capture.assert_called_once()
        update_capture.last_request.assert_json_field(
            "content",
            "# Root\n\n## Section {#release-notes}\n\nAppended under anchor.\n\nBody",
        )
        update_capture.last_request.assert_param("allow_merge", "true")
