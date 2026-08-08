from typing import Any

from mcp.server import FastMCP

from mcp_wiki.mcp.tools.page_read import register_page_read_tools
from mcp_wiki.mcp.tools.page_write import register_page_write_tools
from mcp_wiki.settings import Settings


def register_all_tools(settings: Settings, mcp: FastMCP[Any]) -> None:
    register_page_read_tools(mcp)
    if not settings.wiki_read_only:
        register_page_write_tools(
            mcp,
            # page_upload_attachment reads the server's local filesystem,
            # which only matches the caller's files outside multi-user
            # OAuth deployments.
            include_local_uploads=not settings.oauth_enabled,
        )


__all__ = ["register_all_tools"]
