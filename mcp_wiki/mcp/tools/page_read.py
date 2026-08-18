import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    TextResourceContents,
    ToolAnnotations,
)
from pydantic import Field

from mcp_wiki.mcp.params import (
    AttachmentID,
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
    SearchAuthor,
    SearchDateInterval,
    SearchResponse,
    WikiCurrentUser,
    WikiGrid,
    WikiPage,
)

logger = logging.getLogger(__name__)

FETCH_ALL_MAX_ITEMS = 500
FETCH_ALL_BUDGET_SECONDS = 25.0
_FETCH_ALL_MAX_REQUESTS = 50

# Inline ceiling for page_download_attachment, enforced by the client BEFORE
# the body is read (Content-Length, else a capped stream read), so an oversized
# attachment never lands in this process at all.
#
# 128 KiB, not the megabyte it started as: this guards the conversation, and a
# megabyte of base64 is ~1.4M characters — several hundred thousand tokens,
# past any context window, i.e. a ceiling that admits exactly what it exists to
# refuse. At 128 KiB the base64 worst case is ~175k characters, which is large
# but survivable. Bigger files stay reachable via the attachment's download_url.
MAX_INLINE_ATTACHMENT_BYTES = 131_072

EnvelopeT = TypeVar("EnvelopeT", bound=CursorEnvelope)


def _as_text(raw: bytes) -> str | None:
    """Decode an attachment as text, or None when it should travel as a blob.

    Decodability alone is not the test. UTF-16LE without a BOM — the ordinary
    shape of a Windows-exported .txt or .csv — decodes cleanly as UTF-8 because
    every byte is a valid code point (NUL is U+0000), and so does any binary
    whose bytes all fall below 0x80. Both would arrive as NUL-riddled mojibake
    labelled "text". A NUL is the cheap, reliable discriminator: no real
    plain-text file contains one.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


# open_world_hint=False: every tool talks to exactly one configured Wiki
# organization — a closed domain, unlike e.g. web search. Left unset it
# defaults to true.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


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


def register_page_read_tools(mcp: MCPServer[Any]) -> None:
    @mcp.tool(
        title="Search Wiki",
        description=(
            "Full-text search across the entire Yandex Wiki. Returns up to 50 results "
            "(pages and files) ranked by relevance, each with a title, slug, url, and a "
            "text excerpt in `content` (there is no deeper pagination). Use this "
            "to DISCOVER pages, then call page_get with a result's "
            "slug to read full content. `content` is an excerpt of at most ~510 "
            "characters cut from wherever the match sits in the page — not the page "
            "and not a summary of it, with no guarantee the query terms are even "
            "inside it — so treat it as a relevance signal and read the page before "
            "answering from it; highlight=true marks the matches it does contain "
            "with <em> tags. Wrap multi-word exact phrases in double quotes. All "
            "filters (slug_prefix, result_type, authors, dates) run in the search "
            "backend itself, before the result limit, so a filtered search does not "
            "lose matches to it. To enumerate a section (or the whole Wiki) rather "
            "than search it, use page_get_descendants."
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
                description="Optional server-side section filter: only results whose "
                "slug equals this prefix or lies under it, e.g. 'tech-doc/ml'. Deep "
                "prefixes are fine. An unknown prefix simply yields no results."
            ),
        ] = None,
        result_type: Annotated[
            Literal["page", "file"] | None,
            Field(description="Optional server-side filter by result type."),
        ] = None,
        authors: Annotated[
            list[SearchAuthor] | None,
            Field(
                # min_length=1: an empty list would be dropped on the wire and
                # silently search the whole Wiki — the opposite of what a caller
                # who asked for an author filter wants, and the same trap
                # slug_prefix refuses below. Omit the argument to search
                # everything; never pass an empty list to mean that.
                min_length=1,
                description="Optional server-side filter by page owner: a list "
                "of user identities (each with uid or cloud_uid), ORed together. "
                "Take your own from user_get_current's identity. Omit it to "
                "search every author — an empty list is rejected, because it "
                "would silently mean the same thing. An unknown identity "
                "simply yields no results.",
            ),
        ] = None,
        created_between: Annotated[
            SearchDateInterval | None,
            Field(
                description="Optional server-side filter by creation time. "
                "Both bounds are required — the API rejects open intervals."
            ),
        ] = None,
        modified_between: Annotated[
            SearchDateInterval | None,
            Field(
                description="Optional server-side filter by last-modification time. "
                "Both bounds are required — the API rejects open intervals."
            ),
        ] = None,
        highlight: Annotated[
            bool,
            Field(
                description="Wrap query matches inside `content` excerpts in "
                "<em>…</em> tags."
            ),
        ] = False,
    ) -> SearchResponse:
        app_context = ctx.request_context.lifespan_context
        if slug_prefix is not None and not normalize_slug(slug_prefix):
            # '/' and whitespace normalize to '', which matches no slug at all
            # — an empty result reads as an empty wiki rather than as a filter
            # that threw everything away. Only the emptiness check lives here;
            # the client normalizes the value it sends, like every other slug.
            raise ValueError(
                "slug_prefix must not be empty. Omit it to search the whole Wiki."
            )
        response = await app_context.wiki.page_search(
            query,
            limit=limit,
            cluster=slug_prefix,
            result_type=result_type,
            authors=authors,
            created_at=created_between,
            modified_at=modified_between,
            highlight=highlight,
            auth=get_yandex_auth(ctx),
        )
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
            "or narrow down by calling this tool on a subsection's slug. "
            "Pass from_root=true instead of page_id/slug to enumerate the WHOLE "
            "Wiki, top-level pages included — the way to inventory an "
            "organization when no starting slug is known. Prefer a section slug "
            "when you have one: a full wiki is routinely thousands of pages, so "
            "a root walk costs many requests and a large reply, and fetch_all "
            "stops at its ~500-item cap with truncated=true."
        ),
        annotations=READ_ONLY,
    )
    async def page_get_descendants(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        from_root: Annotated[
            bool,
            Field(
                description=(
                    "Traverse the whole Wiki instead of one page's subtree. "
                    "Mutually exclusive with page_id and slug."
                )
            ),
        ] = False,
        include_self: Annotated[
            bool,
            Field(
                description=(
                    "Whether to include the parent page itself in the subtree. "
                    "Ignored with from_root=true — the root is not a page."
                )
            ),
        ] = False,
        page_size: PageSize = 100,
        cursor: Cursor = None,
        fetch_all: FetchAll = False,
    ) -> DescendantsResponse:
        # The Wiki API reads an empty ?slug= as "the whole organization" — a
        # deliberate contract, not a fallback: an unresolvable slug 404s
        # instead (verified live 2026-08-10, docs/api-notes.md). It stays
        # behind an explicit flag because reaching it by leaving both
        # locators out would turn every forgotten argument into a
        # thousands-of-pages walk.
        if from_root:
            if page_id is not None or slug is not None:
                raise ValueError(
                    "from_root traverses the whole Wiki and cannot be combined "
                    "with page_id or slug."
                )
            resolved_slug = ""
            # Dropped rather than forwarded: the parameter's description
            # promises it is ignored here, and the root is no page to
            # include — so the promise holds on this side of the wire
            # instead of relying on the API to keep ignoring it.
            include_self = False
        else:
            if page_id is None and slug is None:
                raise ValueError(
                    "Provide exactly one of page_id or slug, or pass "
                    "from_root=true to traverse the whole Wiki."
                )
            # '/' and '' are how a caller reaches for the root before
            # finding from_root, and normalize_slug turns both into ''.
            # resolve_page_locator would answer "Slug must not be empty",
            # which is a dead end for the one caller who most needs the
            # flag. Skipped when page_id is also set: there the exclusivity
            # violation is the real error, and that caller has a page.
            if page_id is None and slug is not None and not normalize_slug(slug):
                raise ValueError(
                    "Slug must not be empty. Pass from_root=true to traverse "
                    "the whole Wiki."
                )
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

    @mcp.tool(
        title="Download Page Attachment",
        description=(
            "Download a Yandex Wiki page attachment and return its content "
            "inline as an embedded resource: text files arrive as text, "
            "anything else base64-encoded with its mime type. Refuses files "
            "over 128 KiB without transferring them — fetch those yourself "
            "via the attachment's download_url from page_get_attachments. "
            "That listing is also where file ids come from."
        ),
        annotations=READ_ONLY,
        # An embedded resource is a real content block, so the SDK ships it
        # once. Returning a pydantic model instead would put the payload in
        # structured_content AND in the spec-recommended text mirror of it —
        # the same bytes twice on every call (see WikiMCPServer.call_tool,
        # which deliberately leaves non-text blocks alone).
        structured_output=False,
    )
    async def page_download_attachment(
        ctx: ToolContext,
        file_id: AttachmentID,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
    ) -> EmbeddedResource:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        raw = await get_wiki(ctx).page_download_attachment(
            resolved_page_id,
            file_id=file_id,
            max_bytes=MAX_INLINE_ATTACHMENT_BYTES,
            auth=get_yandex_auth(ctx),
        )
        uri = f"wiki-mcp://pages/{resolved_page_id}/attachments/{file_id}"
        text = _as_text(raw)
        if text is not None:
            return EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=uri, mime_type="text/plain; charset=utf-8", text=text
                ),
            )
        return EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=uri,
                mime_type="application/octet-stream",
                blob=base64.b64encode(raw).decode("ascii"),
            ),
        )

    @mcp.tool(
        title="Get Current User",
        description=(
            "Get the calling Yandex Wiki user: username, home_cluster (the "
            "caller's personal-section slug, e.g. 'users/<login>' — where "
            "'create it in my section' requests belong), and identity/org "
            "ids."
        ),
        annotations=READ_ONLY,
    )
    async def user_get_current(ctx: ToolContext) -> WikiCurrentUser:
        return await get_wiki(ctx).user_get_current(auth=get_yandex_auth(ctx))
