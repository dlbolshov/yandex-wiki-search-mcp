import contextlib
import json
from typing import Any


class WikiError(Exception):
    """Base class for Wiki API errors."""


class WikiApiError(WikiError):
    def __init__(
        self,
        *,
        status: int,
        error_code: str | None = None,
        debug_message: str | None = None,
        message: str | list[str] | None = None,
    ):
        parts = [f"Wiki API request failed with status {status}"]
        if error_code:
            parts.append(f"error_code={error_code}")
        message_text = (
            ", ".join(str(item) for item in message)
            if isinstance(message, list)
            else message
        )
        if debug_message:
            parts.append(f"debug_message={debug_message}")
        elif message_text:
            parts.append(f"message={message_text}")
        super().__init__(", ".join(parts))
        self.status = status
        self.error_code = error_code
        self.debug_message = debug_message
        self.message = message


class WikiConfigError(WikiError):
    """The request could not be built from the current configuration.

    A WikiError rather than a ValueError so callers can handle it alongside
    every other Wiki failure, and so MCP clients get a message that names the
    remedy instead of a bare exception repr.
    """


class WikiTransportError(WikiError):
    """The request never produced an HTTP response.

    Wraps the transport library's own exceptions (dropped connections, broken
    payloads, timeouts) so that callers above the client — tools, scripts —
    can handle every Wiki failure with a single ``except WikiError`` instead
    of importing aiohttp and tracking its exception hierarchy. Timeouts in
    particular carry an empty ``str()``, which would otherwise reach MCP
    clients as a blank error message.
    """

    def __init__(self, method: str, path: str, cause: BaseException):
        detail = str(cause) or type(cause).__name__
        super().__init__(f"{method} {path} failed before a response: {detail}")
        self.method = method
        self.path = path
        self.cause = cause


class PageNotFound(WikiError):
    def __init__(self, page_identifier: int | str):
        super().__init__(f"Wiki page not found: {page_identifier}")
        self.page_identifier = page_identifier


class GridNotFound(WikiError):
    def __init__(self, grid_id: str):
        super().__init__(f"Wiki grid not found: {grid_id}")
        self.grid_id = grid_id


def build_api_error(status: int, payload: bytes) -> WikiApiError:
    """Build a WikiApiError from an HTTP status and a raw response body.

    Understands both Wiki API error envelope shapes: string ``message`` with
    ``details`` and list ``message`` with ``level``.
    """
    details: dict[str, Any] | None = None
    if payload:
        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                details = decoded
    return WikiApiError(
        status=status,
        error_code=details.get("error_code") if details else None,
        debug_message=details.get("debug_message") if details else None,
        message=details.get("message") if details else None,
    )
