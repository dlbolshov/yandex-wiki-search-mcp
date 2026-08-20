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


class GridConflict(WikiApiError):
    """409 CONFLICTING_OPERATION — the grid is briefly locked, not broken.

    Grid mutations are serialized per grid: a second one issued before the
    first settles is rejected *without being applied*, and the lock clears in
    about ten seconds (measured 2026-08-05, docs/api-notes.md). The raw API
    text is just "Conflicting operation in progress", which tells a caller
    nothing about whether to retry or give up, so the recovery is appended.
    """

    RECOVERY = (
        "Another operation on this grid is still finishing and this one was "
        "not applied. Wait a few seconds, re-read the grid for its current "
        "revision, then retry. Grid mutations are serialized per grid — issue "
        "them one at a time rather than in parallel."
    )

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.args = (f"{self.args[0]}. {self.RECOVERY}",)


class WikiOperationError(WikiError):
    """A deferred Wiki operation failed or never settled.

    Every HTTP exchange succeeded — the POST started the operation and the
    status polls answered 200 — but the operation itself reported failure,
    returned no usable result, or outlived the polling deadline. Raising
    WikiApiError here would prefix the message with "failed with status
    200", blaming the one layer that worked.
    """


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


class AttachmentNotFound(WikiError):
    """404 from the attachment download endpoint.

    Mapped in the client because that endpoint answers a miss with a
    placeholder GIF body, not the JSON error envelope (probed 2026-08-11) —
    build_api_error would reduce it to a bare status code.
    """

    def __init__(self, page_id: int, file_id: int):
        super().__init__(
            f"Wiki attachment not found: file {file_id} on page {page_id} "
            "(or the page itself is missing)."
        )
        self.page_id = page_id
        self.file_id = file_id


class ResponseTooLarge(WikiError):
    """A response body exceeded the caller's ``max_bytes`` ceiling.

    A transport-level refusal rather than an API error: the request succeeded,
    this process simply declined to hold the answer.

    The two paths are described separately because they differ in what actually
    happened. With a ``Content-Length`` the refusal costs nothing — not one byte
    of the body is read. Without one the ceiling can only be found by reading,
    so the stream is drained to one byte past it and abandoned there; saying
    "the body was not read" in that case would be false.
    """

    def __init__(
        self, method: str, path: str, declared: int | None, max_bytes: int
    ) -> None:
        detail = (
            f"declared {declared} bytes, past the {max_bytes}-byte ceiling for "
            "this request; the body was not read."
            if declared is not None
            else (
                f"sent no Content-Length and ran past the {max_bytes}-byte "
                "ceiling for this request; the read stopped there instead of "
                "fetching the rest."
            )
        )
        super().__init__(f"{method} {path} {detail}")
        self.method = method
        self.path = path
        self.declared = declared
        self.max_bytes = max_bytes


class WikiLocalFileError(WikiError):
    """A local path the caller named cannot be used.

    The attachment tools are the only ones that touch the caller's filesystem,
    and both of them used to signal this differently: upload raised a builtin
    ``FileNotFoundError``, download a ``WikiOperationError`` — a class whose own
    contract is "a deferred Wiki operation failed *after* every HTTP exchange
    succeeded", which a pre-flight check on a local path plainly is not. One
    ``WikiError`` subclass for both keeps `except WikiError` sufficient, which
    is what every layer above the client relies on.

    ``cause`` carries the original OSError when there was one, so the errno is
    not lost when a write fails halfway through.
    """

    def __init__(self, message: str, *, cause: OSError | None = None) -> None:
        detail = f"{message} ({cause.strerror})" if cause is not None else message
        super().__init__(detail)
        self.cause = cause


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
    error_code = details.get("error_code") if details else None
    error_class = (
        GridConflict
        if status == 409 and error_code == "CONFLICTING_OPERATION"
        else WikiApiError
    )
    return error_class(
        status=status,
        error_code=error_code,
        debug_message=details.get("debug_message") if details else None,
        message=details.get("message") if details else None,
    )
