import logging
import sys

from pydantic import ValidationError

from mcp_wiki.mcp.server import create_mcp_server
from mcp_wiki.settings import Settings

logger = logging.getLogger("mcp_wiki")


def main() -> None:
    """Main entry point for the Yandex Wiki Search MCP Server command."""
    try:
        settings = Settings()
    except ValidationError as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)

    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    auth_mode = (
        "oauth"
        if settings.oauth_enabled
        else ("token" if settings.wiki_token else "iam_token")
    )
    logger.info(
        "starting: transport=%s api=%s web=%s org_id=%s cloud_org_id=%s read_only=%s auth=%s oauth_store=%s log_level=%s",
        settings.transport,
        settings.wiki_api_base_url,
        settings.wiki_web_base_url,
        settings.wiki_org_id,
        settings.wiki_cloud_org_id,
        settings.wiki_read_only,
        auth_mode,
        settings.oauth_store if settings.oauth_enabled else "-",
        settings.log_level,
    )

    create_mcp_server(settings).run(transport=settings.transport)


if __name__ == "__main__":
    main()
