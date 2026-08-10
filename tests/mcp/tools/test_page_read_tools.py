import itertools
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from mcp_wiki.mcp.tools.page_read import _FETCH_ALL_MAX_REQUESTS, _drain_cursor
from mcp_wiki.wiki.custom.errors import WikiTransportError
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentListResponse,
    CommentsResponse,
    CursorEnvelope,
    DescendantItem,
    DescendantsResponse,
    GridsResponse,
    PageComment,
    ResourcesResponse,
    SearchResponse,
    SearchResultItem,
    WikiAttachment,
    WikiGrid,
    WikiGridRow,
    WikiGridSummary,
    WikiPage,
    WikiResource,
)
from tests.mcp.conftest import get_tool_result_content, get_tool_result_text


def _tree_page(start: int, count: int, next_cursor: str | None) -> DescendantsResponse:
    return DescendantsResponse(
        results=[
            DescendantItem(id=i, slug=f"s/{i}") for i in range(start, start + count)
        ],
        next_cursor=next_cursor,
    )


def _cursor_page(
    envelope: type[CursorEnvelope],
    item: Any,
    start: int,
    count: int,
    next_cursor: str | None,
) -> CursorEnvelope:
    return envelope(
        results=[item(i) for i in range(start, start + count)],
        next_cursor=next_cursor,
    )


# (tool name, protocol method, envelope type, item builder)
CURSOR_TOOLS = [
    (
        "page_get_descendants",
        "page_get_descendants",
        DescendantsResponse,
        lambda i: DescendantItem(id=i, slug=f"s/{i}"),
    ),
    (
        "page_get_comments",
        "page_get_comments",
        CommentsResponse,
        lambda i: PageComment(id=i, body=f"c{i}"),
    ),
    (
        "page_get_attachments",
        "page_get_attachments",
        AttachmentListResponse,
        lambda i: WikiAttachment(id=i, name=f"f{i}.txt"),
    ),
    (
        "page_get_resources",
        "page_get_resources",
        ResourcesResponse,
        lambda i: WikiResource(type="attachment", item={"id": i}),
    ),
    (
        "page_get_grids",
        "page_get_grids",
        GridsResponse,
        lambda i: WikiGridSummary(id=str(i), title=f"g{i}"),
    ),
]


class TestDrainCursor:
    async def test_walks_until_exhausted(self) -> None:
        first = _tree_page(0, 2, "c1")
        pages = {"c1": _tree_page(2, 2, "c2"), "c2": _tree_page(4, 1, None)}
        fetch = AsyncMock(side_effect=lambda cursor: pages[cursor])

        await _drain_cursor(first, fetch, page_size=2)

        assert [item.id for item in first.results] == [0, 1, 2, 3, 4]
        assert first.truncated is False
        assert first.next_cursor is None
        assert fetch.await_count == 2

    async def test_stops_before_a_hop_that_would_exceed_the_cap(self) -> None:
        first = _tree_page(0, 2, "c1")
        pages = {"c1": _tree_page(2, 2, "c2"), "c2": _tree_page(4, 2, "c3")}
        fetch = AsyncMock(side_effect=lambda cursor: pages[cursor])

        await _drain_cursor(first, fetch, page_size=2, max_items=5)

        # 2 + 2 = 4 fits under 5, a third hop would reach 6 — so it is not made
        assert [item.id for item in first.results] == [0, 1, 2, 3]
        assert first.truncated is True
        assert first.next_cursor == "c2"
        assert fetch.await_count == 1

    async def test_cap_is_never_overshot(self) -> None:
        first = _tree_page(0, 100, "c1")
        fetch = AsyncMock(side_effect=lambda cursor: _tree_page(100, 100, "c2"))

        await _drain_cursor(first, fetch, page_size=100, max_items=150)

        assert len(first.results) <= 150
        fetch.assert_not_awaited()
        assert first.truncated is True

    async def test_repeated_cursor_clears_the_continuation(self) -> None:
        first = _tree_page(0, 1, "c1")
        fetch = AsyncMock(return_value=_tree_page(1, 1, "c1"))

        await _drain_cursor(first, fetch, page_size=1)

        # the page was merged, but continuing from "c1" would re-fetch it forever
        assert [item.id for item in first.results] == [0, 1]
        assert fetch.await_count == 1
        assert first.truncated is True
        assert first.next_cursor is None

    async def test_failed_page_keeps_what_was_already_fetched(self) -> None:
        first = _tree_page(0, 2, "c1")
        fetch = AsyncMock(side_effect=WikiTransportError("GET", "v1/x", TimeoutError()))

        await _drain_cursor(first, fetch, page_size=2)

        assert [item.id for item in first.results] == [0, 1]
        assert first.truncated is True
        assert first.next_cursor == "c1"

    async def test_time_budget_stops_the_walk(self) -> None:
        first = _tree_page(0, 1, "c1")
        fetch = AsyncMock(side_effect=lambda cursor: _tree_page(1, 1, "c2"))

        await _drain_cursor(first, fetch, page_size=1, budget_seconds=-1.0)

        fetch.assert_not_awaited()
        assert first.truncated is True
        assert first.next_cursor == "c1"

    async def test_request_ceiling_stops_a_cursor_that_never_ends(self) -> None:
        # A server handing out a fresh cursor forever must not keep the tool
        # call running: the hop ceiling bounds it even under the item cap.
        counter = itertools.count(1)
        first = _tree_page(0, 1, "c0")
        fetch = AsyncMock(
            side_effect=lambda cursor: _tree_page(next(counter), 1, f"c{cursor}")
        )

        await _drain_cursor(first, fetch, page_size=1, max_items=10_000)

        assert fetch.await_count == _FETCH_ALL_MAX_REQUESTS
        assert first.truncated is True
        assert first.next_cursor is not None

    async def test_no_cursor_means_nothing_to_do(self) -> None:
        first = _tree_page(0, 1, None)
        fetch = AsyncMock()

        await _drain_cursor(first, fetch, page_size=1)

        fetch.assert_not_awaited()
        assert first.truncated is False


class TestPageReadTools:
    async def test_page_search(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[
                SearchResultItem.model_construct(slug="a/b", title="T", type="page"),
                SearchResultItem.model_construct(slug="c/d", title="U", type="file"),
            ],
        )

        result = await client.call_tool("page_search", {"query": "hello"})

        content = get_tool_result_content(result)
        assert content["results"][0]["slug"] == "a/b"
        mock_wiki_protocol.page_search.assert_awaited_once()

    async def test_page_search_result_type_filter(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[
                SearchResultItem.model_construct(slug="a/b", type="page"),
                SearchResultItem.model_construct(slug="c/d", type="file"),
            ],
        )

        result = await client.call_tool(
            "page_search", {"query": "x", "result_type": "page"}
        )

        content = get_tool_result_content(result)
        assert len(content["results"]) == 1
        assert content["results"][0]["type"] == "page"

    async def test_page_search_slug_prefix_filter_and_url_normalization(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[
                SearchResultItem.model_construct(
                    slug="tech-doc/ml/page", url="/tech-doc/ml/page", type="page"
                ),
                SearchResultItem.model_construct(
                    slug="tech-doc/mlops/page", url="/tech-doc/mlops/page", type="page"
                ),
            ],
        )

        result = await client.call_tool(
            "page_search", {"query": "x", "slug_prefix": "/Tech-Doc/ML/"}
        )

        content = get_tool_result_content(result)
        # segment-boundary match: 'tech-doc/mlops' must NOT pass; prefix got normalized
        assert [r["slug"] for r in content["results"]] == ["tech-doc/ml/page"]
        assert content["results"][0]["url"] == "https://wiki.yandex.ru/tech-doc/ml/page"

    @pytest.mark.parametrize("slug_prefix", ["/", "   ", "///"])
    async def test_page_search_rejects_a_prefix_that_normalizes_to_empty(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        slug_prefix: str,
    ) -> None:
        # These match no slug at all, and an empty result set gives no hint
        # that the filter, rather than the wiki, is the reason.
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[SearchResultItem.model_construct(slug="tech-doc/ml", type="page")],
        )

        result = await client.call_tool(
            "page_search", {"query": "x", "slug_prefix": slug_prefix}
        )

        assert result.is_error is True
        assert "slug_prefix must not be empty" in get_tool_result_text(result)

    async def test_page_get_by_slug(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10,
            slug="users/test/page",
            title="Page title",
        )

        result = await client.call_tool(
            "page_get",
            {"slug": "users/test/page"},
        )

        assert get_tool_result_content(result)["slug"] == "users/test/page"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()

    async def test_page_get_descendants(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_descendants.return_value = {
            "results": [{"id": 10, "slug": "users/test/page"}],
            "next_cursor": None,
            "prev_cursor": None,
        }

        result = await client.call_tool(
            "page_get_descendants",
            {"slug": "users/test/page", "include_self": True},
        )

        assert (
            get_tool_result_content(result)["results"][0]["slug"] == "users/test/page"
        )
        mock_wiki_protocol.page_get_descendants.assert_awaited_once()

    async def test_page_get_descendants_from_root(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        """from_root reaches the API's whole-organization traversal.

        The empty slug is the payload under test: the Wiki API reads ?slug=
        as the root, so anything that "sanitizes" it away silently turns a
        wiki-wide walk into a 400. include_self is dropped rather than
        forwarded — its description promises it is ignored, and that must
        hold on this side of the wire, not by the API's grace.
        """
        mock_wiki_protocol.page_get_descendants.return_value = {
            "results": [{"id": 10, "slug": "tech-doc"}, {"id": 11, "slug": "users"}],
            "next_cursor": None,
            "prev_cursor": None,
        }

        result = await client.call_tool(
            "page_get_descendants",
            {"from_root": True, "include_self": True},
        )

        assert [i["slug"] for i in get_tool_result_content(result)["results"]] == [
            "tech-doc",
            "users",
        ]
        call = mock_wiki_protocol.page_get_descendants.await_args
        assert call.args[0] == ""
        assert call.kwargs["include_self"] is False
        # no page lookup: the root is not a page and must not be resolved
        mock_wiki_protocol.page_get.assert_not_awaited()
        mock_wiki_protocol.page_get_by_slug.assert_not_awaited()

    @pytest.mark.parametrize(
        "locator",
        [{"slug": "users/test/page"}, {"page_id": 42}],
        ids=["slug", "page_id"],
    )
    async def test_page_get_descendants_from_root_rejects_locator(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        locator: dict[str, Any],
    ) -> None:
        result = await client.call_tool(
            "page_get_descendants",
            {"from_root": True, **locator},
        )

        assert result.is_error is True
        assert "cannot be combined" in get_tool_result_text(result)
        mock_wiki_protocol.page_get_descendants.assert_not_awaited()

    async def test_page_get_descendants_requires_a_locator(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        """Omitting everything stays an error rather than meaning the root.

        A forgotten argument must not silently become a thousands-of-pages
        walk; reaching the root is opt-in through from_root — and the error
        must say so, because the agent hitting it is exactly the one with
        no slug to offer.
        """
        result = await client.call_tool("page_get_descendants", {})

        assert result.is_error
        assert "from_root=true" in get_tool_result_text(result)
        mock_wiki_protocol.page_get_descendants.assert_not_awaited()

    @pytest.mark.parametrize("slug", ["/", "", "   "], ids=["slash", "empty", "blank"])
    async def test_page_get_descendants_root_slug_points_at_from_root(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        slug: str,
    ) -> None:
        """Reaching for the root by slug is answered with the flag that works.

        '/' is the obvious guess for "the whole Wiki", and it normalizes to
        the empty slug that resolve_page_locator refuses. Left alone the
        caller gets "Slug must not be empty" — a dead end for exactly the
        one who needs from_root.
        """
        result = await client.call_tool("page_get_descendants", {"slug": slug})

        assert result.is_error
        assert "from_root=true" in get_tool_result_text(result)
        mock_wiki_protocol.page_get_descendants.assert_not_awaited()

    async def test_page_get_descendants_root_slug_with_page_id_reports_exclusivity(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        """Both locators set: the exclusivity violation is the real error.

        The from_root hint would misdirect here — this caller has a page.
        """
        result = await client.call_tool(
            "page_get_descendants", {"slug": "/", "page_id": 42}
        )

        assert result.is_error
        assert "exactly one" in get_tool_result_text(result)
        assert "from_root=true" not in get_tool_result_text(result)
        mock_wiki_protocol.page_get_descendants.assert_not_awaited()

    async def test_page_get_descendants_fetch_all(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_descendants.side_effect = [
            _tree_page(0, 2, "c1"),
            _tree_page(2, 2, "c2"),
            _tree_page(4, 1, None),
        ]

        result = await client.call_tool(
            "page_get_descendants",
            {"slug": "users/test/page", "fetch_all": True},
        )

        content = get_tool_result_content(result)
        assert [item["id"] for item in content["results"]] == [0, 1, 2, 3, 4]
        assert content["truncated"] is False
        assert "next_cursor" not in content
        assert mock_wiki_protocol.page_get_descendants.await_count == 3
        last_call = mock_wiki_protocol.page_get_descendants.await_args_list[-1]
        assert last_call.kwargs["cursor"] == "c2"

    @pytest.mark.parametrize(
        ("tool_name", "method", "envelope", "item"),
        CURSOR_TOOLS,
        ids=[tool for tool, *_ in CURSOR_TOOLS],
    )
    async def test_every_cursor_tool_drains_with_fetch_all(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        tool_name: str,
        method: str,
        envelope: type[CursorEnvelope],
        item: Any,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10, slug="users/test/page"
        )
        getattr(mock_wiki_protocol, method).side_effect = [
            _cursor_page(envelope, item, 0, 2, "c1"),
            _cursor_page(envelope, item, 2, 1, None),
        ]

        result = await client.call_tool(
            tool_name,
            {"slug": "users/test/page", "fetch_all": True},
        )

        content = get_tool_result_content(result)
        assert len(content["results"]) == 3
        assert content["truncated"] is False
        assert "next_cursor" not in content
        assert getattr(mock_wiki_protocol, method).await_count == 2

    @pytest.mark.parametrize(
        ("tool_name", "method", "envelope", "item"),
        CURSOR_TOOLS,
        ids=[tool for tool, *_ in CURSOR_TOOLS],
    )
    async def test_every_cursor_tool_stays_on_one_page_by_default(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        tool_name: str,
        method: str,
        envelope: type[CursorEnvelope],
        item: Any,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10, slug="users/test/page"
        )
        getattr(mock_wiki_protocol, method).return_value = _cursor_page(
            envelope, item, 0, 2, "c1"
        )

        result = await client.call_tool(tool_name, {"slug": "users/test/page"})

        content = get_tool_result_content(result)
        assert len(content["results"]) == 2
        assert content["next_cursor"] == "c1"
        assert "truncated" not in content
        assert getattr(mock_wiki_protocol, method).await_count == 1

    async def test_page_get_descendants_single_page_has_no_truncated_flag(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_descendants.return_value = _tree_page(0, 1, None)

        result = await client.call_tool(
            "page_get_descendants",
            {"slug": "users/test/page"},
        )

        content = get_tool_result_content(result)
        assert "truncated" not in content
        mock_wiki_protocol.page_get_descendants.assert_awaited_once()

    async def test_page_get_with_fields(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10,
            slug="users/test/page",
            content="Page content",
        )

        result = await client.call_tool(
            "page_get",
            {"page_id": 10, "fields": ["content", "breadcrumbs"]},
        )

        assert get_tool_result_content(result)["content"] == "Page content"
        mock_wiki_protocol.page_get.assert_awaited_once()
        assert mock_wiki_protocol.page_get.await_args.args == (10,)
        assert mock_wiki_protocol.page_get.await_args.kwargs["fields"] == [
            "content",
            "breadcrumbs",
        ]
        assert "auth" in mock_wiki_protocol.page_get.await_args.kwargs

    async def test_page_get_resources(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_resources.return_value = {
            "results": [{"type": "attachment", "item": {"name": "file.zip"}}],
            "next_cursor": None,
            "prev_cursor": None,
        }
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )

        result = await client.call_tool(
            "page_get_resources",
            {"slug": "users/test/page", "resource_types": ["attachment"]},
        )

        assert get_tool_result_content(result)["results"][0]["type"] == "attachment"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.page_get_resources.assert_awaited_once()

    async def test_page_get_resources_with_attachment_filter(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_resources.return_value = {
            "results": [{"type": "attachment", "item": {"name": "file.zip"}}],
            "next_cursor": None,
            "prev_cursor": None,
        }

        result = await client.call_tool(
            "page_get_resources",
            {"page_id": 10, "resource_types": ["attachment"]},
        )

        assert get_tool_result_content(result)["results"][0]["type"] == "attachment"
        mock_wiki_protocol.page_get_resources.assert_awaited_once()
        assert mock_wiki_protocol.page_get_resources.await_args.args == (10,)
        assert mock_wiki_protocol.page_get_resources.await_args.kwargs[
            "resource_types"
        ] == ["attachment"]
        assert mock_wiki_protocol.page_get_resources.await_args.kwargs["q"] is None
        assert (
            mock_wiki_protocol.page_get_resources.await_args.kwargs["page_size"] == 50
        )
        assert mock_wiki_protocol.page_get_resources.await_args.kwargs["cursor"] is None
        assert (
            mock_wiki_protocol.page_get_resources.await_args.kwargs["order_by"] is None
        )
        assert (
            mock_wiki_protocol.page_get_resources.await_args.kwargs["order_direction"]
            is None
        )
        assert "auth" in mock_wiki_protocol.page_get_resources.await_args.kwargs

    async def test_page_get_grids(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_grids.return_value = {
            "results": [{"id": "grid-1", "title": "Roadmap"}],
            "next_cursor": None,
            "prev_cursor": None,
        }
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )

        result = await client.call_tool(
            "page_get_grids",
            {"slug": "users/test/page", "order_by": "title", "order_direction": "asc"},
        )

        assert get_tool_result_content(result)["results"][0]["id"] == "grid-1"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.page_get_grids.assert_awaited_once()
        assert mock_wiki_protocol.page_get_grids.await_args.args == (10,)
        assert (
            mock_wiki_protocol.page_get_grids.await_args.kwargs["order_by"] == "title"
        )
        assert (
            mock_wiki_protocol.page_get_grids.await_args.kwargs["order_direction"]
            == "asc"
        )
        assert mock_wiki_protocol.page_get_grids.await_args.kwargs["page_size"] == 50
        assert "auth" in mock_wiki_protocol.page_get_grids.await_args.kwargs

    async def test_grid_get(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_get.return_value = WikiGrid.model_construct(
            id="grid-1",
            title="Roadmap",
            revision="7",
            rows=[WikiGridRow.model_construct(id="row-1", row=["In progress", 3])],
        )

        result = await client.call_tool(
            "grid_get",
            {
                "grid_id": "grid-1",
                "fields": ["attributes", "user_permissions"],
                "filter": "[status] = done",
                "only_cols": "status,eta",
                "only_rows": "row-1,row-2",
                "revision": "7",
                "sort": "eta",
            },
        )

        assert get_tool_result_content(result)["id"] == "grid-1"
        mock_wiki_protocol.grid_get.assert_awaited_once()
        assert mock_wiki_protocol.grid_get.await_args.args == ("grid-1",)
        assert mock_wiki_protocol.grid_get.await_args.kwargs["fields"] == [
            "attributes",
            "user_permissions",
        ]
        assert (
            mock_wiki_protocol.grid_get.await_args.kwargs["filter"] == "[status] = done"
        )
        assert (
            mock_wiki_protocol.grid_get.await_args.kwargs["only_cols"] == "status,eta"
        )
        assert (
            mock_wiki_protocol.grid_get.await_args.kwargs["only_rows"] == "row-1,row-2"
        )
        assert mock_wiki_protocol.grid_get.await_args.kwargs["revision"] == "7"
        assert mock_wiki_protocol.grid_get.await_args.kwargs["sort"] == "eta"
        assert "auth" in mock_wiki_protocol.grid_get.await_args.kwargs

    async def test_grid_get_rejects_empty_grid_id(
        self,
        client: Client,
    ) -> None:
        result = await client.call_tool("grid_get", {"grid_id": "   "})

        assert result.is_error is True
        assert "grid_id must not be empty" in get_tool_result_text(result)
