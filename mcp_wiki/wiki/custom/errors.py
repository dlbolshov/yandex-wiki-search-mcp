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
        message_text = ", ".join(message) if isinstance(message, list) else message
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
