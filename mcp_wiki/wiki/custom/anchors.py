import re


def append_content_to_anchor_source(
    page_content: str,
    *,
    appended_content: str,
    anchor: str,
) -> str | None:
    """Insert content right after an anchor found in raw page markup.

    Supports heading anchors (``{#id}``), inline anchor links
    (``#[text](id)``) and anchor macros (``{{anchor href="id"}}``).
    Returns the updated markup or None when the anchor is not present.
    """
    anchor_id = anchor.lstrip("#")
    escaped_anchor_id = re.escape(anchor_id)
    patterns = [
        re.compile(rf"(?m)^.*\{{#{escaped_anchor_id}\}}[ \t]*$"),
        re.compile(rf'(?m)^.*#\[[^\]]*\]\({escaped_anchor_id}(?:\s+"[^"]*")?\)[ \t]*$'),
        re.compile(
            rf'(?m)^.*\{{\{{(?:anchor|a)\s+href="{escaped_anchor_id}"[^}}]*\}}\}}[ \t]*$'
        ),
    ]
    for pattern in patterns:
        match = pattern.search(page_content)
        if match is None:
            continue
        anchor_end = match.end()
        insertion_point = anchor_end
        while insertion_point < len(page_content) and page_content[insertion_point] in (
            "\r",
            "\n",
        ):
            insertion_point += 1
        separator = page_content[anchor_end:insertion_point]
        suffix = page_content[insertion_point:]
        if separator:
            normalized_content = appended_content.strip("\r\n")
            trailing_separator = separator if suffix else ""
            return (
                f"{page_content[:anchor_end]}{separator}{normalized_content}"
                f"{trailing_separator}{suffix}"
            )
        return (
            f"{page_content[:anchor_end]}{appended_content}"
            f"{page_content[insertion_point:]}"
        )
    return None
