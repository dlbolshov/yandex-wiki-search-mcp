"""Optional arguments reach the wire, and omitted ones stay off it.

Every one of these is a documented tool parameter, so a branch that drops
one silently changes what the caller asked for: a `fields` that never
arrives, a `cursor` that restarts pagination from the top, an `is_silent`
that notifies the whole page anyway.
"""

import re

from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    WikiGridPageRef,
)
from tests.aioresponses_utils import RequestCapture

PAGES_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages(\?.*)?$")
PAGE_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/42(\?.*)?$")
DESCENDANTS_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/descendants.*")
ATTACHMENTS_URL = re.compile(
    r"https://api\.wiki\.yandex\.net/v1/pages/42/attachments.*"
)
GRIDS_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/42/grids.*")


class TestReadOptions:
    async def test_page_get_by_slug_joins_requested_fields(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"id": 42})
        with aioresponses() as mocked:
            mocked.get(PAGES_URL, callback=capture.callback)
            await wiki_client.page_get_by_slug(
                "users/test/page", fields=["content", "owner"]
            )

        capture.last_request.assert_params({"fields": "content,owner"})

    async def test_descendants_forward_the_cursor(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(DESCENDANTS_URL, callback=capture.callback)
            await wiki_client.page_get_descendants("users/test", cursor="cur-7")

        capture.last_request.assert_params({"cursor": "cur-7"})

    async def test_attachments_forward_the_cursor(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(ATTACHMENTS_URL, callback=capture.callback)
            await wiki_client.page_get_attachments(42, cursor="cur-8")

        capture.last_request.assert_params({"cursor": "cur-8"})

    async def test_page_grids_send_every_optional_filter(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(GRIDS_URL, callback=capture.callback)
            await wiki_client.page_get_grids(
                42,
                page_size=25,
                cursor="cur-9",
                order_by="title",
                order_direction="asc",
            )

        capture.last_request.assert_params(
            {
                "page_size": 25,
                "cursor": "cur-9",
                "order_by": "title",
                "order_direction": "asc",
            }
        )

    async def test_page_grids_omit_what_was_not_asked_for(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(GRIDS_URL, callback=capture.callback)
            await wiki_client.page_get_grids(42)

        assert set(capture.last_request.params) == {"page_size"}


class TestPageUpdateOptions:
    async def test_title_only_update_sends_just_the_title(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"id": 42})
        with aioresponses() as mocked:
            mocked.post(PAGE_URL, callback=capture.callback)
            await wiki_client.page_update(42, title="New title")

        capture.last_request.assert_json_body({"title": "New title"})
        assert not capture.last_request.params

    async def test_flags_travel_as_query_strings(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload={"id": 42})
        with aioresponses() as mocked:
            mocked.post(PAGE_URL, callback=capture.callback)
            await wiki_client.page_update(
                42, content="body", allow_merge=True, is_silent=True
            )

        capture.last_request.assert_params({"allow_merge": "true", "is_silent": "true"})


class TestGridOptions:
    async def test_update_without_a_sort_drops_the_key(
        self, wiki_client: WikiClient
    ) -> None:
        # An empty default_sort must not reach the API as an instruction to
        # clear the grid's sorting.
        capture = RequestCapture(payload={"id": "g-1"})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/g-1", callback=capture.callback
            )
            await wiki_client.grid_update(
                "g-1", request=GridUpdateRequest(revision="r1", title="T")
            )

        assert "default_sort" not in capture.last_request.get_json_body()

    async def test_add_rows_at_a_position(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/g-1/rows",
                callback=capture.callback,
            )
            await wiki_client.grid_add_rows(
                "g-1", revision="r1", rows=[{"a": 1}], position=2
            )

        capture.last_request.assert_json_field("position", 2)

    async def test_move_a_row_after_another(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/g-1/rows/move",
                callback=capture.callback,
            )
            await wiki_client.grid_move_row(
                "g-1", revision="r1", row_id="row-1", after_row_id="row-2"
            )

        capture.last_request.assert_json_field("after_row_id", "row-2")

    async def test_add_columns_at_a_position(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/g-1/columns",
                callback=capture.callback,
            )
            await wiki_client.grid_add_columns(
                "g-1",
                revision="r1",
                columns=[{"title": "C", "slug": "c", "type": "string"}],
                position=1,
            )

        capture.last_request.assert_json_field("position", 1)

    async def test_add_columns_without_a_position_appends(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/g-1/columns",
                callback=capture.callback,
            )
            await wiki_client.grid_add_columns(
                "g-1",
                revision="r1",
                columns=[{"title": "C", "slug": "c", "type": "string"}],
            )

        assert "position" not in capture.last_request.get_json_body()

    async def test_copy_without_a_title_omits_it(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload={"status_url": "/v1/op/1"})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids/g-1/clone",
                callback=capture.callback,
            )
            await wiki_client.grid_copy("g-1", target="users/test/target")

        capture.last_request.assert_json_body({"target": "users/test/target"})

    async def test_create_sends_the_page_reference(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"id": "g-1"})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/grids", callback=capture.callback
            )
            await wiki_client.grid_create(
                request=GridCreateRequest(title="T", page=WikiGridPageRef(id=42))
            )

        capture.last_request.assert_json_body({"title": "T", "page": {"id": 42}})
