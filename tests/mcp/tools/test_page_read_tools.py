import base64
import itertools
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    TextResourceContents,
)

from mcp_wiki.mcp.tools.page_read import (
    _FETCH_ALL_MAX_REQUESTS,
    MAX_INLINE_ATTACHMENT_BYTES,
    MAX_INLINE_IMAGE_BYTES,
    _drain_cursor,
)
from mcp_wiki.wiki.custom.errors import ResponseTooLarge, WikiTransportError
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentContent,
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
        # no filters requested → none forwarded, highlight stays off
        kwargs = mock_wiki_protocol.page_search.await_args.kwargs
        assert kwargs["cluster"] is None
        assert kwargs["result_type"] is None
        assert kwargs["authors"] is None
        assert kwargs["created_at"] is None
        assert kwargs["modified_at"] is None
        assert kwargs["highlight"] is False
        assert kwargs["cursor"] is None

    async def test_page_search_forwards_filters_server_side(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # The backend does the filtering (verified live 2026-08-11); the tool
        # must forward the arguments instead of sieving results itself.
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[SearchResultItem.model_construct(slug="tech-doc/ml/a")],
        )

        result = await client.call_tool(
            "page_search",
            {
                "query": "x",
                "slug_prefix": "/Tech-Doc/ML/",
                "result_type": "page",
                "authors": [{"uid": "1130000067296925"}],
                "created_between": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-06-01T00:00:00Z",
                },
                "modified_between": {
                    "from": "2026-06-01T00:00:00Z",
                    "to": "2026-08-01T00:00:00Z",
                },
                "highlight": True,
            },
        )

        assert result.is_error is False
        kwargs = mock_wiki_protocol.page_search.await_args.kwargs
        # slug_prefix arrives as the API's cluster filter, verbatim: the client
        # normalizes it, like every other slug-shaped client argument, so a
        # direct client caller gets the same treatment as a tool caller.
        assert kwargs["cluster"] == "/Tech-Doc/ML/"
        assert kwargs["result_type"] == "page"
        assert [a.uid for a in kwargs["authors"]] == ["1130000067296925"]
        assert kwargs["created_at"].from_ == "2026-01-01T00:00:00Z"
        assert kwargs["created_at"].to == "2026-06-01T00:00:00Z"
        assert kwargs["modified_at"].from_ == "2026-06-01T00:00:00Z"
        assert kwargs["highlight"] is True

    async def test_page_search_forwards_the_cursor(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # Pagination lives in the backend's highlight mode; the tool's job is
        # to hand the page number over, not to walk pages itself.
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[], next_cursor="3", prev_cursor="1"
        )

        result = await client.call_tool(
            "page_search", {"query": "x", "highlight": True, "cursor": 2}
        )

        assert result.is_error is False
        kwargs = mock_wiki_protocol.page_search.await_args.kwargs
        assert kwargs["highlight"] is True
        assert kwargs["cursor"] == 2
        content = get_tool_result_content(result)
        assert content["next_cursor"] == "3"

    @pytest.mark.parametrize("cursor", [0, 501])
    async def test_page_search_rejects_a_cursor_out_of_range(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        cursor: int,
    ) -> None:
        # The wire 400s outside 1-500; the schema says so upfront instead.
        result = await client.call_tool(
            "page_search",
            {"query": "x", "highlight": True, "cursor": cursor},
        )

        assert result.is_error is True
        mock_wiki_protocol.page_search.assert_not_awaited()

    async def test_page_search_cursor_without_highlight_errors_loudly(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # The refusal itself lives in the client (it guards direct callers
        # too); here it must surface to the agent as a tool error, not vanish.
        mock_wiki_protocol.page_search.side_effect = ValueError(
            "cursor requires highlight=true: the search endpoint paginates "
            "only in highlight mode and silently ignores the cursor "
            "otherwise, returning page 1 again."
        )

        result = await client.call_tool(
            "page_search",
            {"query": "x", "cursor": 2},
        )

        assert result.is_error is True
        assert "cursor requires highlight=true" in get_tool_result_text(result)

    async def test_page_search_rejects_an_open_date_interval(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # Both bounds are required — the schema says so, the wire is never hit.
        result = await client.call_tool(
            "page_search",
            {"query": "x", "created_between": {"from": "2026-01-01T00:00:00Z"}},
        )

        assert result.is_error is True
        mock_wiki_protocol.page_search.assert_not_awaited()

    async def test_page_search_rejects_an_empty_authors_list(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # An empty list would be dropped on the wire and silently search the
        # whole Wiki — the opposite of what a caller asking for an author
        # filter wants, and worse than the empty slug_prefix this tool already
        # refuses, because it returns everything rather than nothing.
        result = await client.call_tool(
            "page_search",
            {"query": "x", "authors": []},
        )

        assert result.is_error is True
        mock_wiki_protocol.page_search.assert_not_awaited()

    async def test_page_search_rejects_a_blank_author_id(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # "" is accepted by the API and answers 200 with zero results, which
        # reads as "this user wrote nothing" rather than "you sent junk".
        result = await client.call_tool(
            "page_search",
            {"query": "x", "authors": [{"uid": ""}]},
        )

        assert result.is_error is True
        mock_wiki_protocol.page_search.assert_not_awaited()

    async def test_page_search_rejects_an_empty_author(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # {} would be silently ignored by the backend — the schema refuses it.
        result = await client.call_tool(
            "page_search",
            {"query": "x", "authors": [{}]},
        )

        assert result.is_error is True
        mock_wiki_protocol.page_search.assert_not_awaited()

    async def test_page_search_url_normalization(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[
                SearchResultItem.model_construct(
                    slug="tech-doc/ml/page", url="/tech-doc/ml/page", type="page"
                ),
            ],
        )

        result = await client.call_tool("page_search", {"query": "x"})

        content = get_tool_result_content(result)
        assert content["results"][0]["url"] == "https://wiki.yandex.ru/tech-doc/ml/page"

    @pytest.mark.parametrize("slug_prefix", ["/", "   ", "///"])
    async def test_page_search_rejects_a_prefix_that_normalizes_to_empty(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        slug_prefix: str,
    ) -> None:
        # These match no slug at all, and an empty result set gives no hint
        # that the filter, rather than the wiki, is the reason. Rejected
        # before any HTTP happens.
        result = await client.call_tool(
            "page_search", {"query": "x", "slug_prefix": slug_prefix}
        )

        assert result.is_error is True
        assert "slug_prefix must not be empty" in get_tool_result_text(result)
        mock_wiki_protocol.page_search.assert_not_awaited()

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


class TestPageReadAttachment:
    async def test_utf8_content_arrives_as_an_embedded_text_resource(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            b"col1;col2\na;b\n", "text/csv"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        # A real content block, not a model: the payload ships once instead of
        # being mirrored into a text duplicate of structured_content.
        assert result.structured_content is None
        assert len(result.content) == 1
        block = result.content[0]
        assert isinstance(block, EmbeddedResource)
        assert isinstance(block.resource, TextResourceContents)
        assert block.resource.text == "col1;col2\na;b\n"
        assert str(block.resource.uri) == "wiki-mcp://pages/10/attachments/5"
        args = mock_wiki_protocol.page_read_attachment_bytes.await_args
        assert args.args[0] == 10
        assert args.kwargs["file_id"] == 5
        # The ceiling reaches the client as a mime-keyed callable, enforced
        # there before the body is read: text gets the conversation budget,
        # images the vision budget.
        ceiling = args.kwargs["max_bytes"]
        assert ceiling("text/csv") == MAX_INLINE_ATTACHMENT_BYTES
        assert ceiling("image/png") == MAX_INLINE_IMAGE_BYTES
        # An unrenderable image subtype is not an image for our purposes, so
        # it keeps the small budget and is refused before transfer.
        assert ceiling("image/svg+xml") == MAX_INLINE_ATTACHMENT_BYTES
        # "the server said nothing" and octet-stream both get the image budget:
        # only bytes that were read can reach the magic-byte fallback.
        assert ceiling(None) == MAX_INLINE_IMAGE_BYTES
        assert ceiling("application/octet-stream") == MAX_INLINE_IMAGE_BYTES

    async def test_binary_content_arrives_as_a_base64_blob(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # A ZIP header: binary, but not an image — those get their own block.
        blob = b"PK\x03\x04\x00\xff\xfe"
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10
        )
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            blob, "application/zip"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"slug": "users/test/page", "file_id": 5},
        )

        block = result.content[0]
        assert isinstance(block, EmbeddedResource)
        assert isinstance(block.resource, BlobResourceContents)
        assert base64.b64decode(block.resource.blob) == blob

    async def test_utf8_decodable_binary_still_travels_as_a_blob(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # UTF-16LE without a BOM: every byte is a valid UTF-8 code point, so a
        # bare decode() succeeds and would hand the model NUL-riddled mojibake
        # labelled "text".
        blob = "hello".encode("utf-16-le")
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            blob, "application/octet-stream"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        block = result.content[0]
        assert isinstance(block, EmbeddedResource)
        assert isinstance(block.resource, BlobResourceContents)
        assert base64.b64decode(block.resource.blob) == blob

    async def test_oversized_attachment_is_refused_by_the_client(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # The client refuses on the stream and can only speak in bytes; the
        # tool reshapes that into something the agent can act on. Without the
        # reshaping this was the COMMON path — any image past the image budget
        # — and it reached the agent as a bare transport string quoting a limit
        # the tool description never mentions.
        mock_wiki_protocol.page_read_attachment_bytes.side_effect = ResponseTooLarge(
            "GET",
            "v1/pages/10/attachments/5/download",
            9_000_000,
            MAX_INLINE_IMAGE_BYTES,
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        assert result.is_error is True
        text = get_tool_result_text(result)
        assert str(MAX_INLINE_IMAGE_BYTES) in text
        assert str(MAX_INLINE_ATTACHMENT_BYTES) in text
        assert "page_download_attachment" in text
        assert "download_url" in text

    async def test_an_image_arrives_as_an_image_block(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            png, "image/png"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        # A native image block: vision-capable clients render it, models see
        # it — an embedded blob would be opaque base64 to both.
        assert result.structured_content is None
        assert len(result.content) == 1
        block = result.content[0]
        assert isinstance(block, ImageContent)
        assert block.mime_type == "image/png"
        assert base64.b64decode(block.data) == png

    @pytest.mark.parametrize(
        ("magic_body", "expected_mime"),
        [
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
            (b"GIF87a" + b"\x00" * 8, "image/gif"),
            (b"GIF89a" + b"\x00" * 8, "image/gif"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        ],
    )
    async def test_an_image_without_a_mime_claim_is_recognized_by_magic(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        magic_body: bytes,
        expected_mime: str,
    ) -> None:
        # The wire says octet-stream; the bytes say image. The bytes win —
        # otherwise the picture ships as an opaque blob for a header's lie.
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            magic_body, "application/octet-stream"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        block = result.content[0]
        assert isinstance(block, ImageContent)
        assert block.mime_type == expected_mime
        assert base64.b64decode(block.data) == magic_body

    async def test_an_unrenderable_image_mime_travels_as_text(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # An ImageContent block the vision API cannot decode does not degrade
        # to "the model sees nothing" — the host's next call fails with
        # `Could not process image` and the retry loop kills the session
        # (anthropics/claude-code#28279). SVG is XML, so text is both safe and
        # more useful.
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            svg, "image/svg+xml"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        block = result.content[0]
        assert isinstance(block, EmbeddedResource)
        assert isinstance(block.resource, TextResourceContents)
        assert block.resource.text == svg.decode()

    @pytest.mark.parametrize(
        "unrenderable", ["image/bmp", "image/tiff", "image/x-icon", "image/heic"]
    )
    async def test_other_unrenderable_image_subtypes_do_not_become_blocks(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
        unrenderable: str,
    ) -> None:
        body = b"\x00\x01\x02 not a renderable image"
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            body, unrenderable
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        assert not isinstance(result.content[0], ImageContent)

    async def test_an_octet_stream_image_above_the_text_ceiling_still_arrives(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # The whole point of the magic-byte fallback: a real PNG served as
        # octet-stream, larger than the text ceiling. Picking the ceiling from
        # the header alone would have refused it before the bytes could speak.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_INLINE_ATTACHMENT_BYTES + 1)
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            png, "application/octet-stream"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        block = result.content[0]
        assert isinstance(block, ImageContent)
        assert block.mime_type == "image/png"

    async def test_oversized_non_image_under_the_image_budget_is_refused(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        # Read under the image budget because the mime was uninformative, then
        # found not to be an image: refused here, with a usable remedy.
        mock_wiki_protocol.page_read_attachment_bytes.return_value = AttachmentContent(
            b"t" * (MAX_INLINE_ATTACHMENT_BYTES + 1), "application/octet-stream"
        )

        result = await client.call_tool(
            "page_read_attachment",
            {"page_id": 10, "file_id": 5},
        )

        assert result.is_error is True
        text = get_tool_result_text(result)
        assert "page_download_attachment" in text
        assert "download_url" in text


class TestUserGetCurrent:
    async def test_returns_the_identity(
        self,
        client: Client,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.user_get_current.return_value = {
            "username": "david",
            "home_cluster": "users/david",
            "identity": {"uid": "113000"},
            "org": {"dir_id": "752289"},
        }

        result = await client.call_tool("user_get_current", {})

        content = get_tool_result_content(result)
        assert content["username"] == "david"
        assert content["home_cluster"] == "users/david"
        mock_wiki_protocol.user_get_current.assert_awaited_once()
