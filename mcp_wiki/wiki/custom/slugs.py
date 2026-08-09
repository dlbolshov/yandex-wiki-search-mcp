from urllib.parse import unquote, urlparse


def normalize_slug(slug_or_url: str) -> str:
    """Reduce a slug or a full Wiki page URL to a bare slug.

    Lives in the Wiki layer rather than next to the MCP helpers: the HTTP
    client needs it on every page call, and owning it here keeps the
    transport free of any dependency on the presentation layer.
    ``mcp_wiki.mcp.utils`` re-exports it for callers that reach for it there.
    """
    candidate = slug_or_url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        candidate = unquote(parsed.path)
    return candidate.strip("/")
