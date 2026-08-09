"""Debug logging for inbound MCP messages.

The Wiki client already logs every outbound HTTP call with its duration. This
logs the other half — which MCP method arrived and how long serving it took —
so the two subtract: a `page_search` that took 8 seconds of which the Wiki API
took 7.9 is their latency, not ours, and the log says so without a profiler.

Kept at DEBUG deliberately. `LOG_LEVEL` defaults to INFO, so this costs
nothing and emits nothing until someone turns it on to answer a question; on
stdio, a client that does not drain the server's stderr applies back-pressure,
so a per-request line at INFO would be a way to stall the server on a sloppy
client.
"""

import logging
import time
from collections.abc import Mapping
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

logger = logging.getLogger(__name__)

# Where the interesting name sits in each method's params. Anything else is
# logged by method alone — a bare `tools/list` needs no qualifier.
_TARGET_KEYS: Mapping[str, str] = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}


def _target(ctx: ServerRequestContext[Any, Any]) -> str:
    """The tool name or resource URI this message is about, if it has one."""
    key = _TARGET_KEYS.get(ctx.method)
    if key is None or not isinstance(ctx.params, Mapping):
        return ""
    value = ctx.params.get(key)
    return f" {value}" if isinstance(value, str) else ""


async def log_inbound_middleware(
    ctx: ServerRequestContext[Any, Any],
    call_next: CallNext,
) -> HandlerResult:
    """Log every inbound message with the time spent serving it."""
    if not logger.isEnabledFor(logging.DEBUG):
        return await call_next(ctx)

    started = time.perf_counter()
    try:
        return await call_next(ctx)
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.debug("%s%s (%.0f ms)", ctx.method, _target(ctx), elapsed_ms)
