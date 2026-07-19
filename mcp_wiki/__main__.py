import sys

from pydantic import ValidationError

from mcp_wiki.mcp.server import create_mcp_server
from mcp_wiki.settings import Settings


def main() -> None:
    """Main entry point for the Yandex Wiki Search MCP Server command."""
    try:
        settings = Settings()
    except ValidationError as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)

    create_mcp_server(settings).run(transport=settings.transport)


if __name__ == "__main__":
    main()
