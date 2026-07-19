from typing import Annotated, Any, Literal

from mcp.server import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_wiki.mcp.params import (
    Cursor,
    GridFields,
    GridID,
    GridPageSize,
    OptionalPageID,
    OptionalPageSlug,
    PageFields,
    PageSize,
    ResourceTypes,
    SearchQuery,
    SearchResultPageSize,
)
from mcp_wiki.mcp.tools.common import (
    ToolContext,
    get_wiki,
    resolve_page_id,
    resolve_page_slug,
)
from mcp_wiki.mcp.utils import (
    get_yandex_auth,
    normalize_slug,
    resolve_page_locator,
)
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentListResponse,
    CommentsResponse,
    DescendantsResponse,
    GridsResponse,
    ResourcesResponse,
    SearchResponse,
    WikiGrid,
    WikiPage,
)


def register_page_read_tools(mcp: FastMCP[Any]) -> None:
    @mcp.tool(
        title="Search Wiki",
        description=(
            "Full-text search across the entire Yandex Wiki. Returns up to 50 results "
            "(pages and files) ranked by relevance, each with a title, slug, url, and a "
            "text snippet. Use this to DISCOVER pages, then call page_get with a result's "
            "slug to read full content. Wrap multi-word exact phrases in double quotes. "
            "Search is global: there is no server-side section filter. slug_prefix and "
            "result_type are applied client-side AFTER fetching, so combine them with "
            "page_size=50 to avoid missing matches."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_search(
        ctx: ToolContext,
        query: SearchQuery,
        page_size: SearchResultPageSize = 10,
        slug_prefix: Annotated[
            str | None,
            Field(
                description="Optional client-side filter: keep only results whose slug "
                "equals this prefix or lies under it as a path segment, "
                "e.g. 'tech-doc/ml'."
            ),
        ] = None,
        result_type: Annotated[
            Literal["page", "file"] | None,
            Field(description="Optional client-side filter by result type."),
        ] = None,
    ) -> SearchResponse:
        app_context = ctx.request_context.lifespan_context
        response = await app_context.wiki.page_search(
            query,
            page_size=page_size,
            auth=get_yandex_auth(ctx),
        )
        if slug_prefix:
            prefix = normalize_slug(slug_prefix).lower()
            response.results = [
                r
                for r in response.results
                if (slug := (r.slug or "").lower()) == prefix
                or slug.startswith(prefix + "/")
            ]
        if result_type:
            response.results = [r for r in response.results if r.type == result_type]
        if slug_prefix or result_type:
            # keep the fields' semantics: total_documents always equals the number
            # of returned results and total_pages is 0 only when there are none
            response.total_documents = len(response.results)
            response.total_pages = 1 if response.results else 0
        web_base_url = app_context.web_base_url.rstrip("/")
        for r in response.results:
            if r.url and r.url.startswith("/"):
                r.url = web_base_url + r.url
        return response

    @mcp.tool(
        title="Get Wiki Page",
        description="Get a Yandex Wiki page by page_id or slug.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_get(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        fields: PageFields = None,
    ) -> WikiPage:
        page_id, slug = resolve_page_locator(page_id=page_id, slug=slug)
        auth = get_yandex_auth(ctx)
        field_names = [field.value for field in fields] if fields else None

        if page_id is not None:
            return await get_wiki(ctx).page_get(
                page_id,
                fields=field_names,
                auth=auth,
            )
        if slug is None:
            raise ValueError("Either page_id or slug must be provided.")

        return await get_wiki(ctx).page_get_by_slug(
            slug,
            fields=field_names,
            auth=auth,
        )

    @mcp.tool(
        title="Get Page Descendants",
        description="Get a subtree of Yandex Wiki pages under a parent page.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_get_descendants(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        include_self: Annotated[
            bool,
            Field(
                description="Whether to include the root page itself in the subtree."
            ),
        ] = False,
        page_size: PageSize = 100,
        cursor: Cursor = None,
    ) -> DescendantsResponse:
        resolved_slug = await resolve_page_slug(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_get_descendants(
            resolved_slug,
            include_self=include_self,
            page_size=page_size,
            cursor=cursor,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Get Page Comments",
        description="Get comments for a Yandex Wiki page.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_get_comments(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        page_size: PageSize = 100,
        cursor: Cursor = None,
    ) -> CommentsResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_get_comments(
            resolved_page_id,
            page_size=page_size,
            cursor=cursor,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Get Page Resources",
        description="Get resources linked to a Yandex Wiki page, including attachments and grids.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_get_resources(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        resource_types: ResourceTypes = None,
        search: Annotated[
            str | None,
            Field(description="Optional title search query for resources."),
        ] = None,
        page_size: PageSize = 50,
        cursor: Cursor = None,
        order_by: Annotated[
            Literal["name_title", "created_at"] | None,
            Field(description="Optional resource sorting field."),
        ] = None,
        order_direction: Annotated[
            Literal["asc", "desc"] | None,
            Field(description="Optional resource sorting direction."),
        ] = None,
    ) -> ResourcesResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_get_resources(
            resolved_page_id,
            resource_types=[resource_type.value for resource_type in resource_types]
            if resource_types
            else None,
            q=search,
            page_size=page_size,
            cursor=cursor,
            order_by=order_by,
            order_direction=order_direction,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Get Page Grids",
        description="Get dynamic tables attached to a Yandex Wiki page.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_get_grids(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        page_size: GridPageSize = 50,
        cursor: Cursor = None,
        order_by: Annotated[
            Literal["title", "created_at"] | None,
            Field(description="Optional grid sorting field."),
        ] = None,
        order_direction: Annotated[
            Literal["asc", "desc"] | None,
            Field(description="Optional grid sorting direction."),
        ] = None,
    ) -> GridsResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_get_grids(
            resolved_page_id,
            page_size=page_size,
            cursor=cursor,
            order_by=order_by,
            order_direction=order_direction,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Get Wiki Grid",
        description="Get a Yandex Wiki dynamic table by grid ID.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def grid_get(
        ctx: ToolContext,
        grid_id: GridID,
        fields: GridFields = None,
        filter: Annotated[
            str | None,
            Field(description="Optional row filter expression for the grid."),
        ] = None,
        only_cols: Annotated[
            str | None,
            Field(
                description="Optional comma-separated list of column slugs to return."
            ),
        ] = None,
        only_rows: Annotated[
            str | None,
            Field(description="Optional comma-separated list of row IDs to return."),
        ] = None,
        revision: Annotated[
            str | None,
            Field(description="Optional grid revision for historical reads."),
        ] = None,
        sort: Annotated[
            str | None,
            Field(description="Optional sort expression for grid rows."),
        ] = None,
    ) -> WikiGrid:
        grid_id = grid_id.strip()
        if not grid_id:
            raise ValueError("grid_id must not be empty.")

        return await get_wiki(ctx).grid_get(
            grid_id,
            fields=[field.value for field in fields] if fields else None,
            filter=filter,
            only_cols=only_cols,
            only_rows=only_rows,
            revision=revision,
            sort=sort,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Get Page Attachments",
        description="Get attachments for a Yandex Wiki page.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def page_get_attachments(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        page_size: PageSize = 100,
        cursor: Cursor = None,
    ) -> AttachmentListResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_get_attachments(
            resolved_page_id,
            page_size=page_size,
            cursor=cursor,
            auth=get_yandex_auth(ctx),
        )
