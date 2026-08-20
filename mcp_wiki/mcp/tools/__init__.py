from typing import Any

from mcp.server import MCPServer

from mcp_wiki.mcp.tools.page_read import register_page_read_tools
from mcp_wiki.mcp.tools.page_write import register_page_write_tools
from mcp_wiki.settings import Settings


def register_all_tools(settings: Settings, mcp: MCPServer[Any]) -> None:
    # page_read_attachment points oversized reads at page_download_attachment,
    # which exists only when the write tools are registered AND local file
    # access is on. Passing the same condition keeps the read tool from
    # advertising a tool this server does not offer.
    register_page_read_tools(
        mcp,
        include_local_downloads=(
            not settings.wiki_read_only and settings.include_local_uploads
        ),
    )
    if not settings.wiki_read_only:
        register_page_write_tools(
            mcp,
            include_local_uploads=settings.include_local_uploads,
        )


__all__ = ["register_all_tools"]
