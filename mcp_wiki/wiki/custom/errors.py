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
        with contextlib.suppress(json.JSONDecodeError):
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                details = decoded
    return WikiApiError(
        status=status,
        error_code=details.get("error_code") if details else None,
        debug_message=details.get("debug_message") if details else None,
        message=details.get("message") if details else None,
    )
