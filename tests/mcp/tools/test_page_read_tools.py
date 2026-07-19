from unittest.mock import AsyncMock

from mcp.client.session import ClientSession

from mcp_wiki.wiki.proto.types.pages import (
    SearchResponse,
    SearchResultItem,
    WikiGrid,
    WikiGridRow,
    WikiPage,
)
from tests.mcp.conftest import get_tool_result_content, get_tool_result_text


class TestPageReadTools:
    async def test_page_search(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[
                SearchResultItem.model_construct(slug="a/b", title="T", type="page"),
                SearchResultItem.model_construct(slug="c/d", title="U", type="file"),
            ],
            total_documents=2,
        )

        result = await client_session.call_tool("page_search", {"query": "hello"})

        content = get_tool_result_content(result)
        assert content["results"][0]["slug"] == "a/b"
        mock_wiki_protocol.page_search.assert_awaited_once()

    async def test_page_search_result_type_filter(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_search.return_value = SearchResponse.model_construct(
            results=[
                SearchResultItem.model_construct(slug="a/b", type="page"),
                SearchResultItem.model_construct(slug="c/d", type="file"),
            ],
            total_documents=2,
        )

        result = await client_session.call_tool(
            "page_search", {"query": "x", "result_type": "page"}
        )

        content = get_tool_result_content(result)
        assert len(content["results"]) == 1
        assert content["results"][0]["type"] == "page"
        assert content["total_documents"] == 1

    async def test_page_search_slug_prefix_filter_and_url_normalization(
        self,
        client_session: ClientSession,
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
            total_documents=2,
        )

        result = await client_session.call_tool(
            "page_search", {"query": "x", "slug_prefix": "/Tech-Doc/ML/"}
        )

        content = get_tool_result_content(result)
        # segment-boundary match: 'tech-doc/mlops' must NOT pass; prefix got normalized
        assert [r["slug"] for r in content["results"]] == ["tech-doc/ml/page"]
        assert content["total_documents"] == 1
        assert content["total_pages"] == 1
        assert content["results"][0]["url"] == "https://wiki.yandex.ru/tech-doc/ml/page"

    async def test_page_get_by_slug(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_by_slug.return_value = WikiPage.model_construct(
            id=10,
            slug="users/test/page",
            title="Page title",
        )

        result = await client_session.call_tool(
            "page_get",
            {"slug": "users/test/page"},
        )

        assert get_tool_result_content(result)["slug"] == "users/test/page"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()

    async def test_page_get_descendants(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_descendants.return_value = {
            "results": [{"id": 10, "slug": "users/test/page"}],
            "next_cursor": None,
            "prev_cursor": None,
        }

        result = await client_session.call_tool(
            "page_get_descendants",
            {"slug": "users/test/page", "include_self": True},
        )

        assert (
            get_tool_result_content(result)["results"][0]["slug"] == "users/test/page"
        )
        mock_wiki_protocol.page_get_descendants.assert_awaited_once()

    async def test_page_get_with_fields(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get.return_value = WikiPage.model_construct(
            id=10,
            slug="users/test/page",
            content="Page content",
        )

        result = await client_session.call_tool(
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
        client_session: ClientSession,
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

        result = await client_session.call_tool(
            "page_get_resources",
            {"slug": "users/test/page", "resource_types": ["attachment"]},
        )

        assert get_tool_result_content(result)["results"][0]["type"] == "attachment"
        mock_wiki_protocol.page_get_by_slug.assert_awaited_once()
        mock_wiki_protocol.page_get_resources.assert_awaited_once()

    async def test_page_get_resources_with_attachment_filter(
        self,
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.page_get_resources.return_value = {
            "results": [{"type": "attachment", "item": {"name": "file.zip"}}],
            "next_cursor": None,
            "prev_cursor": None,
        }

        result = await client_session.call_tool(
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
        client_session: ClientSession,
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

        result = await client_session.call_tool(
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
        client_session: ClientSession,
        mock_wiki_protocol: AsyncMock,
    ) -> None:
        mock_wiki_protocol.grid_get.return_value = WikiGrid.model_construct(
            id="grid-1",
            title="Roadmap",
            revision="7",
            rows=[WikiGridRow.model_construct(id="row-1", row=["In progress", 3])],
        )

        result = await client_session.call_tool(
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
        client_session: ClientSession,
    ) -> None:
        result = await client_session.call_tool("grid_get", {"grid_id": "   "})

        assert result.isError is True
        assert "grid_id must not be empty" in get_tool_result_text(result)
