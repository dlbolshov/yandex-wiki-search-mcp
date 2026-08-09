"""The transport request of the message being handled, as a contextvar.

The SDK injects a `Context` into tools, prompts and *templated* resources, but
not into a resource with a static URI — registering one that asks for a
`Context` raises at decoration time. `wiki-mcp://configuration` is exactly
that shape: a fixed URI whose whole job is to report the organization requests
actually go to, which lives in the HTTP query string.

Rather than turn the URI into a template (which would move it out of
`resources/list` and give the organization a second, divergent source), a
middleware stashes the inbound request here and the handler reads it back.

`Server.middleware` is marked provisional in the SDK, so its surface is kept
to two small modules: this one and mcp_wiki.mcp.middleware (the debug log).
If the signature moves, those two files are the whole repair. The read side
degrades rather than breaks — with nothing stashed, `current_request()` is
None and callers fall back to the configured organization, which is already
what happens on stdio.
"""

import contextvars
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from starlette.requests import Request

_current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "wiki_mcp_current_request",
    default=None,
)


def current_request() -> Request | None:
    """The transport request being served, or None outside HTTP (e.g. stdio)."""
    return _current_request.get()


async def stash_request_middleware(
    ctx: ServerRequestContext[Any, Any],
    call_next: CallNext,
) -> HandlerResult:
    """Publish the inbound request for the duration of one message.

    getattr rather than attribute access: a field rename upstream then costs
    the per-request organization override, not every request.
    """
    token = _current_request.set(getattr(ctx, "request", None))
    try:
        return await call_next(ctx)
    finally:
        _current_request.reset(token)
