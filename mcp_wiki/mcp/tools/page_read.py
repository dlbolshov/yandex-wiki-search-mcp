import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_wiki.mcp.params import (
    Cursor,
    FetchAll,
    GridFields,
    GridID,
    GridPageSize,
    OptionalPageID,
    OptionalPageSlug,
    PageFields,
    PageSize,
    ResourceTypes,
    SearchQuery,
    SearchResultLimit,
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
from mcp_wiki.wiki.custom.errors import WikiError
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentListResponse,
    CommentsResponse,
    CursorEnvelope,
    DescendantsResponse,
    GridsResponse,
    ResourcesResponse,
    SearchResponse,
    WikiGrid,
    WikiPage,
)

logger = logging.getLogger(__name__)

FETCH_ALL_MAX_ITEMS = 500
FETCH_ALL_BUDGET_SECONDS = 25.0
_FETCH_ALL_MAX_REQUESTS = 50

EnvelopeT = TypeVar("EnvelopeT", bound=CursorEnvelope)

# openWorldHint=False: every tool talks to exactly one configured Wiki
# organization — a closed domain, unlike e.g. web search. Left unset it
# defaults to true.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


async def _drain_cursor(
    first: EnvelopeT,
    fetch_page: Callable[[str], Awaitable[EnvelopeT]],
    page_size: int,
    max_items: int = FETCH_ALL_MAX_ITEMS,
    budget_seconds: float = FETCH_ALL_BUDGET_SECONDS,
) -> None:
    """Follow next_cursor in place, extending first.results.

    Stops before a hop that would exceed `max_items`, so the cap is a real
    ceiling rather than an approximate one. Also stops when the request or
    time budget runs out, or when a fetch fails — a partial list plus a
    usable `next_cursor` beats discarding pages already paid for.

    `truncated` is False only when the list was drained to its end. The
    deadline can only be checked between hops, so the wall-clock ceiling is
    the budget plus one in-flight request with its retries.

    When the server echoes the cursor we just sent, continuing is
    impossible: `next_cursor` is cleared so no caller retries it forever.
    """
    results: list[Any] = first.results
    deadline = asyncio.get_running_loop().time() + budget_seconds
    complete = False

    for _ in range(_FETCH_ALL_MAX_REQUESTS):
        cursor = first.next_cursor
        if cursor is None:
            complete = True
            break
        if len(results) + page_size > max_items:
            logger.debug("fetch_all stopped: %d items, cap %d", len(results), max_items)
            break
        if asyncio.get_running_loop().time() >= deadline:
            logger.debug("fetch_all stopped: %.0fs budget spent", budget_seconds)
            break
        try:
            page = await fetch_page(cursor)
        except WikiError as exc:
            logger.warning("fetch_all stopped after a failed page: %s", exc)
            break
        results.extend(page.results)
        if page.next_cursor == cursor:
            logger.warning("fetch_all stopped: the server repeated cursor %r", cursor)
            first.next_cursor = None
            break
        first.next_cursor = page.next_cursor

    first.truncated = not complete


async def _paginate(
    fetch_page: Callable[[str | None], Awaitable[EnvelopeT]],
    cursor: str | None,
    fetch_all: bool,
    page_size: int,
) -> EnvelopeT:
    """Fetch one page, or drain the whole cursor when fetch_all is set."""
    response = await fetch_page(cursor)
    if fetch_all:
        await _drain_cursor(response, fetch_page, page_size=page_size)
    return response


def register_page_read_tools(mcp: FastMCP[Any]) -> None:
    @mcp.tool(
        title="Search Wiki",
        description=(
            "Full-text search across the entire Yandex Wiki. Returns up to 50 results "
            "(pages and files) ranked by relevance, each with a title, slug, url, and a "
            "short text snippet in `content` (there is no deeper pagination). Use this "
            "to DISCOVER pages, then call page_get with a result's "
            "slug to read full content. Wrap multi-word exact phrases in double quotes. "
            "Search is global: there is no server-side section filter. slug_prefix and "
            "result_type are applied client-side AFTER fetching, so combine them with "
            "limit=50 to avoid missing matches."
        ),
        annotations=READ_ONLY,
    )
    async def page_search(
        ctx: ToolContext,
        query: SearchQuery,
        limit: SearchResultLimit = 10,
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
            limit=limit,
            auth=get_yandex_auth(ctx),
        )
        if slug_prefix is not None:
            prefix = normalize_slug(slug_prefix).lower()
            if not prefix:
                # '/' and whitespace normalize to '', which matches no slug
                # at all — an empty result reads as an empty wiki rather
                # than as a filter that threw everything away.
                raise ValueError(
                    "slug_prefix must not be empty. Omit it to search the whole Wiki."
                )
            response.results = [
                r
                for r in response.results
                if (slug := (r.slug or "").lower()) == prefix
                or slug.startswith(prefix + "/")
            ]
        if result_type:
            response.results = [r for r in response.results if r.type == result_type]
        web_base_url = app_context.web_base_url.rstrip("/")
        for r in response.results:
            if r.url and r.url.startswith("/"):
                r.url = web_base_url + r.url
        return response

    @mcp.tool(
        title="Get Wiki Page",
        description="Get a Yandex Wiki page by page_id or slug.",
        annotations=READ_ONLY,
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
        if slug is None:  # pragma: no cover - narrowing for the type checker;
            # resolve_page_locator raises unless exactly one is set.
            raise ValueError("Either page_id or slug must be provided.")

        return await get_wiki(ctx).page_get_by_slug(
            slug,
            fields=field_names,
            auth=auth,
        )

    @mcp.tool(
        title="Get Page Descendants",
        description=(
            "Get the subtree of Yandex Wiki pages under a parent page. Returns "
            "descendants from ALL nesting levels as one flat list of {id, slug} "
            "items — slugs encode the hierarchy ('<parent>/x/y' is nested under "
            "'<parent>/x'), so the tree can be reconstructed without further "
            "calls. Combine with fetch_all=true to map a whole section at once; "
            "if the result comes back truncated=true, continue via next_cursor "
            "or narrow down by calling this tool on a subsection's slug."
        ),
        annotations=READ_ONLY,
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
        fetch_all: FetchAll = False,
    ) -> DescendantsResponse:
        resolved_slug = await resolve_page_slug(ctx, page_id=page_id, slug=slug)
        auth = get_yandex_auth(ctx)

        async def fetch(next_cursor: str | None) -> DescendantsResponse:
            return await get_wiki(ctx).page_get_descendants(
                resolved_slug,
                include_self=include_self,
                page_size=page_size,
                cursor=next_cursor,
                auth=auth,
            )

        return await _paginate(fetch, cursor, fetch_all, page_size)

    @mcp.tool(
        title="Get Page Comments",
        description="Get comments for a Yandex Wiki page.",
        annotations=READ_ONLY,
    )
    async def page_get_comments(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        page_size: PageSize = 100,
        cursor: Cursor = None,
        fetch_all: FetchAll = False,
    ) -> CommentsResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        auth = get_yandex_auth(ctx)

        async def fetch(next_cursor: str | None) -> CommentsResponse:
            return await get_wiki(ctx).page_get_comments(
                resolved_page_id,
                page_size=page_size,
                cursor=next_cursor,
                auth=auth,
            )

        return await _paginate(fetch, cursor, fetch_all, page_size)

    @mcp.tool(
        title="Get Page Resources",
        description="Get resources linked to a Yandex Wiki page, including attachments and grids.",
        annotations=READ_ONLY,
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
        fetch_all: FetchAll = False,
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
        auth = get_yandex_auth(ctx)
        type_values = (
            [resource_type.value for resource_type in resource_types]
            if resource_types
            else None
        )

        async def fetch(next_cursor: str | None) -> ResourcesResponse:
            return await get_wiki(ctx).page_get_resources(
                resolved_page_id,
                resource_types=type_values,
                q=search,
                page_size=page_size,
                cursor=next_cursor,
                order_by=order_by,
                order_direction=order_direction,
                auth=auth,
            )

        return await _paginate(fetch, cursor, fetch_all, page_size)

    @mcp.tool(
        title="Get Page Grids",
        description="Get dynamic tables attached to a Yandex Wiki page.",
        annotations=READ_ONLY,
    )
    async def page_get_grids(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        page_size: GridPageSize = 50,
        cursor: Cursor = None,
        fetch_all: FetchAll = False,
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
        auth = get_yandex_auth(ctx)

        async def fetch(next_cursor: str | None) -> GridsResponse:
            return await get_wiki(ctx).page_get_grids(
                resolved_page_id,
                page_size=page_size,
                cursor=next_cursor,
                order_by=order_by,
                order_direction=order_direction,
                auth=auth,
            )

        return await _paginate(fetch, cursor, fetch_all, page_size)

    @mcp.tool(
        title="Get Wiki Grid",
        description="Get a Yandex Wiki dynamic table by grid ID.",
        annotations=READ_ONLY,
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
        annotations=READ_ONLY,
    )
    async def page_get_attachments(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        page_size: PageSize = 100,
        cursor: Cursor = None,
        fetch_all: FetchAll = False,
    ) -> AttachmentListResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        auth = get_yandex_auth(ctx)

        async def fetch(next_cursor: str | None) -> AttachmentListResponse:
            return await get_wiki(ctx).page_get_attachments(
                resolved_page_id,
                page_size=page_size,
                cursor=next_cursor,
                auth=auth,
            )

        return await _paginate(fetch, cursor, fetch_all, page_size)
