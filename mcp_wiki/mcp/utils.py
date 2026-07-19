from typing import Any
from urllib.parse import unquote, urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context
from starlette.requests import Request

from mcp_wiki.wiki.proto.common import YandexAuth


def get_yandex_auth(ctx: Context[Any, Any, Request]) -> YandexAuth:
    access_token = get_access_token()
    token = access_token.token if access_token else None

    auth = YandexAuth(token=token)

    if ctx.request_context.request is not None:
        cloud_org_id = ctx.request_context.request.query_params.get("cloudOrgId")
        org_id = ctx.request_context.request.query_params.get("orgId")

        if cloud_org_id:
            auth.cloud_org_id = cloud_org_id.strip() or None

        if org_id:
            auth.org_id = org_id.strip() or None

    return auth


def normalize_slug(slug_or_url: str) -> str:
    candidate = slug_or_url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        candidate = unquote(parsed.path)
    return candidate.strip("/")


def resolve_page_locator(
    *,
    page_id: int | None,
    slug: str | None,
) -> tuple[int | None, str | None]:
    if (page_id is None) == (slug is None):
        raise ValueError("Provide exactly one of page_id or slug.")

    if slug is not None:
        slug = normalize_slug(slug)
        if not slug:
            raise ValueError("Slug must not be empty.")

    return page_id, slug
