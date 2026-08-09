from typing import Any

from mcp.server import MCPServer
from pydantic import BaseModel

from mcp_wiki.mcp.utils import get_yandex_auth
from mcp_wiki.settings import Settings
from mcp_wiki.wiki.proto.common import select_org
from mcp_wiki.yfm import YFM_CHEATSHEET


class YandexWikiMCPConfigurationResponse(BaseModel):
    api_base_url: str
    cloud_org_id: str | None
    org_id: str | None
    read_only: bool
    oauth_enabled: bool


def register_resources(settings: Settings, mcp: MCPServer[Any]) -> None:
    @mcp.resource(
        "wiki-mcp://configuration",
        description="Retrieve configured Yandex Wiki MCP configuration.",
    )
    async def wiki_mcp_configuration() -> YandexWikiMCPConfigurationResponse:
        # No `ctx` parameter: the SDK refuses to inject a Context into a
        # static-URI resource, and get_context() is gone in mcp 2.x. The
        # per-request organization comes from the middleware-stashed request
        # instead — see mcp_wiki.mcp.request_ctx.
        #
        # Same selection the client applies to the request headers, so the
        # reported organization is the one calls actually go to. The pair
        # moves as a unit: naming an org_id alongside a cloud_org_id would
        # report a combination the settings validator forbids and no
        # request ever carries.
        org_id, cloud_org_id = select_org(
            get_yandex_auth(),
            default_org_id=settings.wiki_org_id,
            default_cloud_org_id=settings.wiki_cloud_org_id,
        )

        return YandexWikiMCPConfigurationResponse(
            api_base_url=settings.wiki_api_base_url,
            cloud_org_id=cloud_org_id,
            org_id=org_id,
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
