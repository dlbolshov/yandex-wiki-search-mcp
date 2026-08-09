from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context
from starlette.requests import Request

from mcp_wiki.mcp.request_ctx import current_request
from mcp_wiki.wiki.custom.slugs import normalize_slug
from mcp_wiki.wiki.proto.common import YandexAuth

# normalize_slug lives in the Wiki layer (the HTTP client needs it and must
# not import from here); re-exported for callers that reach for it here.
__all__ = ["get_yandex_auth", "normalize_slug", "resolve_page_locator"]


def get_yandex_auth(ctx: Context[Any, Request] | None = None) -> YandexAuth:
    """Per-request Yandex credentials and organization override.

    Handlers the SDK injects a Context into pass it; the configuration
    resource, which the SDK cannot inject into (see request_ctx), passes
    nothing and the middleware-stashed request answers instead. Preferring
    the explicit ctx keeps a middleware regression from reaching the tools.
    """
    access_token = get_access_token()
    token = access_token.token if access_token else None

    auth = YandexAuth(token=token)

    request = (ctx.request_context.request if ctx is not None else None) or (
        current_request()
    )

    if request is not None:
        cloud_org_id = request.query_params.get("cloudOrgId")
        org_id = request.query_params.get("orgId")

        if cloud_org_id:
            auth.cloud_org_id = cloud_org_id.strip() or None

        if org_id:
            auth.org_id = org_id.strip() or None

    return auth


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
