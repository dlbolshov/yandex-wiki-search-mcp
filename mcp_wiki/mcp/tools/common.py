from typing import Any, TypeAlias

from mcp.server.fastmcp import Context
from starlette.requests import Request

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.utils import get_yandex_auth, resolve_page_locator
from mcp_wiki.wiki.proto.pages import WikiProtocol

ToolContext: TypeAlias = Context[Any, AppContext, Request]


def get_wiki(ctx: ToolContext) -> WikiProtocol:
    return ctx.request_context.lifespan_context.wiki


async def resolve_page_id(
    ctx: ToolContext,
    *,
    page_id: int | None,
    slug: str | None,
) -> int:
    resolved_page_id, _ = await resolve_page_id_and_type(
        ctx, page_id=page_id, slug=slug
    )
    return resolved_page_id


async def resolve_page_id_and_type(
    ctx: ToolContext,
    *,
    page_id: int | None,
    slug: str | None,
) -> tuple[int, str | None]:
    """Resolve a page locator to its numeric ID.

    page_type is returned only when the page was fetched during slug
    resolution; the ID path stays free of extra GET requests and yields None.
    """
    page_id, slug = resolve_page_locator(page_id=page_id, slug=slug)
    if page_id is not None:
        return page_id, None
    if slug is None:
        raise ValueError("Either page_id or slug must be provided.")

    page = await get_wiki(ctx).page_get_by_slug(
        slug,
        auth=get_yandex_auth(ctx),
    )
    return page.id, page.page_type


async def resolve_page_slug(
    ctx: ToolContext,
    *,
    page_id: int | None,
    slug: str | None,
) -> str:
    page_id, slug = resolve_page_locator(page_id=page_id, slug=slug)
    if slug is not None:
        return slug
    if page_id is None:
        raise ValueError("Either page_id or slug must be provided.")

    page = await get_wiki(ctx).page_get(
        page_id,
        auth=get_yandex_auth(ctx),
    )
    if not page.slug:
        raise ValueError(f"Page {page_id} does not have a slug in the API response.")
    return page.slug
