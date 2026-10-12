from urllib.parse import unquote, urlparse

from mcp_wiki.wiki.custom.errors import WikiArgumentError


def normalize_slug(slug_or_url: str) -> str:
    """Reduce a slug or a full Wiki page URL to a bare slug.

    Lives in the Wiki layer rather than next to the MCP helpers: the HTTP
    client needs it on every page call, and owning it here keeps the
    transport free of any dependency on the presentation layer.
    ``mcp_wiki.mcp.utils`` re-exports it for callers that reach for it there.

    A URL ``urlparse`` cannot take apart — an unbalanced bracket in the host
    (``https://[wiki.yandex.ru/x``: "Invalid IPv6 URL"), a host that changes
    under NFKC normalization — is refused as a WikiArgumentError. Its own
    ValueError is no WikiError, so the MCP layer would report it to the model
    as an anonymous crash instead of saying what was wrong with the argument.
    """
    candidate = slug_or_url.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise WikiArgumentError(
            f"Not a valid page slug or Wiki URL: {slug_or_url!r} ({exc})."
        ) from exc
    if parsed.scheme and parsed.netloc:
        candidate = unquote(parsed.path)
    return candidate.strip("/")
