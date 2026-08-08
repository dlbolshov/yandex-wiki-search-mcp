import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import (
    GridConflict,
    GridNotFound,
    PageNotFound,
    WikiApiError,
    WikiOperationError,
)
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    WikiGridPageRef,
)
from tests.aioresponses_utils import RequestCapture
from tests.conftest import load_fixture


class TestWikiClient:
    async def test_build_headers_with_token_and_org(
        self,
        wiki_client: WikiClient,
    ) -> None:
        headers = wiki_client._build_headers()
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
        capture = RequestCapture(payload=load_fixture("search_results.json"))
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            result = await wiki_client.page_search("query text", limit=50)

        assert result.results[0].slug == "tech-doc/example/ml/pipeline-overview"
        assert result.results[0].content == (
            "The pipeline trains models and evaluates convergence.\n\n"
            "See the testing module for details.\tHow to prepare inputs."
        )
        assert result.results[0].modified_at == "2026-05-12T22:14:54"
        assert result.results[2].type == "file"
        capture.assert_called_once()
        # the live endpoint honors "limit" only; "page_size" is silently ignored
        capture.last_request.assert_json_body({"query": "query text", "limit": 50})

    async def test_page_search_empty(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload=load_fixture("search_empty.json"))
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            result = await wiki_client.page_search("nothing matches this")

        assert result.results == []
        assert result.next_cursor is None

    async def test_page_search_clamps_limit(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(
            payload={"results": [], "next_cursor": None, "prev_cursor": None}
        )
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/search",
                callback=capture.callback,
            )
            await wiki_client.page_search("q", limit=1000)
        capture.last_request.assert_json_field("limit", 50)

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

    async def test_grid_mutation_conflict_surfaces_as_grid_conflict(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            status=409,
            body=(
                '{"error_code":"CONFLICTING_OPERATION",'
                '"debug_message":"Conflicting operation in progress"}'
            ),
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/rows",
                callback=capture.callback,
            )
            with pytest.raises(GridConflict) as excinfo:
                await wiki_client.grid_add_rows(
                    "grid-1", revision="7", rows=[{"status": "todo"}]
                )

        # Not retried here: writes never are, and the caller has to re-read the
        # grid for a fresh revision anyway, so recovery belongs above the client.
        capture.assert_called_once()
        assert "re-read the grid" in str(excinfo.value)

    async def test_grid_404_reports_the_grid_id(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/grids/missing.*"),
                status=404,
                body="{}",
            )
            with pytest.raises(GridNotFound) as excinfo:
                await wiki_client.grid_get("missing")

        assert excinfo.value.grid_id == "missing"
        assert "missing" in str(excinfo.value)

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

        # 204 No Content → the acknowledgment is filled in client-side.
        assert result.grid_id == "grid-1"
        assert result.deleted is True
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

    async def test_grid_move_row(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "10"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/rows/move",
                callback=capture.callback,
            )
            result = await wiki_client.grid_move_row(
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

    async def test_grid_move_column(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"revision": "11"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/grid-1/columns/move",
                callback=capture.callback,
            )
            result = await wiki_client.grid_move_column(
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

        assert result.id == 10
        append_capture.assert_called_once()
        get_capture.assert_called_once()
        update_capture.assert_called_once()
        update_capture.last_request.assert_json_field(
            "content",
            "# Root\n\n## Section {#release-notes}\n\nAppended under anchor.\n\nBody",
        )
        update_capture.last_request.assert_param("allow_merge", "true")

    async def test_page_update_requires_title_or_content(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with pytest.raises(ValueError, match="at least one of title or content"):
            await wiki_client.page_update(10)

    async def test_page_clone_polls_the_operation_and_returns_the_copy(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={
                "operation": {"type": "clone", "id": "op-1"},
                "dry_run": False,
                "status_url": "/v1/operations/clone/op-1",
            }
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/clone",
                callback=capture.callback,
            )
            mocked.get(
                "https://api.wiki.yandex.net/v1/operations/clone/op-1",
                payload={
                    "status": "success",
                    "result": {"page": {"id": 77, "slug": "users/test/copy"}},
                },
            )
            copy = await wiki_client.page_clone(
                10,
                target="https://wiki.yandex.ru/users/test/copy/",
                title="Copy title",
            )

        assert copy.id == 77
        assert copy.slug == "users/test/copy"
        capture.last_request.assert_json_body(
            {"target": "users/test/copy", "title": "Copy title"}
        )

    async def test_page_clone_keeps_polling_until_the_operation_settles(
        self,
        wiki_client: WikiClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("mcp_wiki.wiki.custom.client.CLONE_POLL_INTERVAL", 0)

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/clone",
                payload={
                    "operation": {"type": "clone", "id": "op-2"},
                    "status_url": "/v1/operations/clone/op-2",
                },
            )
            mocked.get(
                "https://api.wiki.yandex.net/v1/operations/clone/op-2",
                payload={"status": "in_progress"},
            )
            mocked.get(
                "https://api.wiki.yandex.net/v1/operations/clone/op-2",
                payload={
                    "status": "success",
                    "result": {"page": {"id": 78, "slug": "users/test/copy-2"}},
                },
            )
            copy = await wiki_client.page_clone(10, target="users/test/copy-2")

        assert copy.id == 78

    async def test_page_clone_rejects_an_empty_target(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with pytest.raises(ValueError, match="target must not be empty"):
            await wiki_client.page_clone(10, target="  / ")

    async def test_page_clone_not_found_raises_page_not_found(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/404/clone",
                status=404,
                payload={"error_code": "NOT_FOUND"},
            )
            with pytest.raises(PageNotFound):
                await wiki_client.page_clone(404, target="users/test/anywhere")

    async def test_page_clone_raises_when_the_operation_fails(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/clone",
                payload={
                    "operation": {"type": "clone", "id": "op-3"},
                    "status_url": "/v1/operations/clone/op-3",
                },
            )
            mocked.get(
                "https://api.wiki.yandex.net/v1/operations/clone/op-3",
                payload={"status": "failed"},
            )
            with pytest.raises(WikiOperationError, match="ended with status='failed'"):
                await wiki_client.page_clone(10, target="users/test/copy-3")

    async def test_page_clone_raises_when_the_operation_never_settles(
        self,
        wiki_client: WikiClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("mcp_wiki.wiki.custom.client.CLONE_POLL_TIMEOUT", 0.0)

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/clone",
                payload={
                    "operation": {"type": "clone", "id": "op-4"},
                    "status_url": "/v1/operations/clone/op-4",
                },
            )
            mocked.get(
                "https://api.wiki.yandex.net/v1/operations/clone/op-4",
                payload={"status": "in_progress"},
            )
            with pytest.raises(WikiOperationError, match="did not finish within"):
                await wiki_client.page_clone(10, target="users/test/copy-4")

    async def test_page_clone_raises_when_no_status_url_is_returned(
        self,
        wiki_client: WikiClient,
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/clone",
                payload={"operation": {"type": "clone", "id": "op-5"}},
            )
            with pytest.raises(WikiOperationError, match="did not return a status_url"):
                await wiki_client.page_clone(10, target="users/test/copy-5")

    async def test_page_clone_raises_when_success_reports_no_page(
        self,
        wiki_client: WikiClient,
    ) -> None:
        # Drift guard: a "success" without result.page must not be reported
        # as a completed clone — there is no id or slug to hand back.
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/clone",
                payload={
                    "operation": {"type": "clone", "id": "op-6"},
                    "status_url": "/v1/operations/clone/op-6",
                },
            )
            mocked.get(
                "https://api.wiki.yandex.net/v1/operations/clone/op-6",
                payload={"status": "success"},
            )
            with pytest.raises(WikiOperationError, match="reported no page"):
                await wiki_client.page_clone(10, target="users/test/copy-6")

    async def test_page_create_normalizes_the_slug_and_parses_the_page(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(
            payload={"id": 42, "slug": "users/test/new", "title": "New"}
        )

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages",
                callback=capture.callback,
            )
            page = await wiki_client.page_create(
                slug="https://wiki.yandex.ru/users/test/new/",
                title="New",
                content="body",
            )

        assert page.id == 42
        assert page.slug == "users/test/new"
        capture.last_request.assert_json_body(
            {"slug": "users/test/new", "title": "New", "content": "body"}
        )

    async def test_page_append_content_without_anchor_sends_a_location(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(payload={"id": 10, "slug": "users/test/page"})

        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/10/append-content",
                callback=capture.callback,
            )
            page = await wiki_client.page_append_content(
                10, content="Tail block", location="top"
            )

        assert page.id == 10
        capture.last_request.assert_json_body(
            {"content": "Tail block", "body": {"location": "top"}}
        )

    async def test_page_get_by_slug_not_found_reports_normalized_slug(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(status=404, payload={})
        with aioresponses() as mocked:
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages.*"),
                callback=capture.callback,
            )
            with pytest.raises(PageNotFound) as exc_info:
                await wiki_client.page_get_by_slug("/users/test/page/")

        assert exc_info.value.page_identifier == "users/test/page"

    async def test_page_get_descendants_not_found_reports_normalized_slug(
        self,
        wiki_client: WikiClient,
    ) -> None:
        capture = RequestCapture(status=404, payload={})
        with aioresponses() as mocked:
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/descendants.*"),
                callback=capture.callback,
            )
            with pytest.raises(PageNotFound) as exc_info:
                await wiki_client.page_get_descendants("/users/test/page/")

        assert exc_info.value.page_identifier == "users/test/page"
