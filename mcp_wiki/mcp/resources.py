from typing import Any, cast

from mcp.server import FastMCP
from mcp.server.fastmcp import Context
from pydantic import BaseModel
from starlette.requests import Request

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.utils import get_yandex_auth
from mcp_wiki.settings import Settings
from mcp_wiki.yfm import YFM_CHEATSHEET


class YandexWikiMCPConfigurationResponse(BaseModel):
    api_base_url: str
    cloud_org_id: str | None
    org_id: str | None
    read_only: bool
    oauth_enabled: bool


def register_resources(settings: Settings, mcp: FastMCP[Any]) -> None:
    @mcp.resource(
        "wiki-mcp://configuration",
        description="Retrieve configured Yandex Wiki MCP configuration.",
    )
    async def wiki_mcp_configuration() -> YandexWikiMCPConfigurationResponse:
        ctx = cast(Context[Any, AppContext, Request], mcp.get_context())
        auth = get_yandex_auth(ctx)

        return YandexWikiMCPConfigurationResponse(
            api_base_url=settings.wiki_api_base_url,
            cloud_org_id=auth.cloud_org_id or settings.wiki_cloud_org_id,
            org_id=auth.org_id or settings.wiki_org_id,
            read_only=settings.wiki_read_only,
            oauth_enabled=settings.oauth_enabled,
        )

    @mcp.resource(
        "wiki-mcp://yfm-cheatsheet",
        description=(
            "Yandex Wiki markup (YFM) cheat sheet: which Markdown/GFM habits "
            "render as-is, which do not, and the YFM equivalents to use instead."
        ),
        mime_type="text/markdown",
    )
    def yfm_cheatsheet() -> str:
        return YFM_CHEATSHEET
