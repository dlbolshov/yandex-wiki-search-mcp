from urllib.parse import unquote, urlparse


def normalize_slug(slug_or_url: str) -> str:
    """Reduce a slug or a full Wiki page URL to a bare slug.

    Lives in the Wiki layer rather than next to the MCP helpers: the HTTP
    client needs it on every page call, and importing it from
    ``mcp_wiki.mcp.utils`` made the transport depend on the presentation
    layer — a cycle that broke ``import mcp_wiki.mcp.utils`` as a first
    import. ``mcp_wiki.mcp.utils`` re-exports it for callers that already
    reach for it there.
    """
    candidate = slug_or_url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        candidate = unquote(parsed.path)
    return candidate.strip("/")
