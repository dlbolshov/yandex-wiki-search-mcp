import logging
import sys

from pydantic import ValidationError

from mcp_wiki.mcp.server import create_mcp_server
from mcp_wiki.settings import Settings, suspicious_env_keys

logger = logging.getLogger("mcp_wiki")


def main() -> None:
    """Main entry point for the Yandex Wiki Search MCP Server command."""
    try:
        settings = Settings()
    except ValidationError as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)

    # Refuse to start on a misspelled setting rather than run with a default
    # the operator did not choose: WIKI_READ_ONL=true would otherwise leave
    # every write tool registered, silently.
    if suspects := suspicious_env_keys():
        listed = "\n".join(
            f"  {key.upper()} — did you mean {field.upper()}?"
            for key, field in suspects.items()
        )
        sys.stderr.write(
            f"Unrecognized setting(s) that look like a typo:\n{listed}\n"
            "Fix or remove them; unrelated variables outside this server's "
            "namespaces are ignored.\n"
        )
        sys.exit(1)

    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    token_mode: str | None = None
    if settings.wiki_token:
        token_mode = "token"  # noqa: S105
    elif settings.wiki_iam_token:
        token_mode = "iam_token"  # noqa: S105

    if settings.oauth_enabled:
        auth_mode = f"oauth+{token_mode}" if token_mode else "oauth"
    else:
        auth_mode = token_mode or "none"
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
