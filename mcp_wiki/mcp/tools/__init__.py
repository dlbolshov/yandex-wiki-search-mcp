from typing import Any

from mcp.server import MCPServer

from mcp_wiki.mcp.tools.page_read import register_page_read_tools
from mcp_wiki.mcp.tools.page_write import register_page_write_tools
from mcp_wiki.settings import Settings


def register_all_tools(settings: Settings, mcp: MCPServer[Any]) -> None:
    register_page_read_tools(mcp)
    if not settings.wiki_read_only:
        register_page_write_tools(
            mcp,
            include_local_uploads=settings.include_local_uploads,
        )


__all__ = ["register_all_tools"]
