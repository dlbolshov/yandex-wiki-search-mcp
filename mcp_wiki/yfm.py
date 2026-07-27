"""Lightweight, dependency-free YFM (Yandex Flavored Markdown) checks.

Warnings-only: writes are never blocked. The rules are based on live-verified
Yandex Wiki behavior (scripts/yfm_smoke.py, 2026-07-27):

- GFM pipe tables and task lists render fine — never flagged.
- Raw HTML is escaped and shows up as literal text.
- GFM alerts (``> [!NOTE]``) render as a blockquote with a literal ``[!NOTE]``.
- ``{% ... %}`` directives inside code fences are not parsed, so fenced
  content is skipped entirely.
"""

import re

MAX_WARNINGS = 10

YFM_CHEATSHEET = """\
# Yandex Wiki markup (YFM) cheat sheet

Yandex Wiki renders YFM (Yandex Flavored Markdown), a CommonMark superset.
Plain Markdown works as-is: headings, emphasis, lists, blockquotes, links,
images, code spans/fences, GFM pipe tables and task lists all render fine.

## GFM habits that DO NOT render (verified live)

- Raw HTML is escaped and shows as literal text (`<details>`, `<br>`, `<img>`, ...).
- GFM alerts (`> [!NOTE]`) render as a plain blockquote with a literal `[!NOTE]`.

## YFM equivalents

Callout / alert (types: `info`, `tip`, `warning`, `alert`):

    {% note info "Optional title" %}

    Body text.

    {% endnote %}

Collapsible block (instead of `<details>`):

    {% cut "Click to expand" %}

    Hidden content.

    {% endcut %}

Tabs (indent tab content by two spaces):

    {% list tabs %}

    - Tab one

      Tab one content.

    - Tab two

      Tab two content.

    {% endlist %}

Table with multiline cells (regular pipe tables also work):

    #|
    || Cell 1 | Cell 2 ||
    || Multi
    line | Cell 4 ||
    |#

## Notes

- `{% ... %}` inside code fences is treated as plain code — safe to write.
- Do not leave `{% note %}`/`{% cut %}`/`{% list tabs %}` unclosed: Yandex Wiki
  renders unclosed blocks as literal text. Page write tools return
  `yfm_warnings` when the submitted content looks off.
"""

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(.*)$")
_DIRECTIVE_OPEN_RE = re.compile(r"\{%\s*(note|cut|list)\b[^}]*%\}")
_DIRECTIVE_END_RE = re.compile(r"\{%\s*end(note|cut|list)\s*%\}")
_GFM_ALERT_RE = re.compile(
    r"^ {0,3}>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(
    r"</?(details|summary|br|hr|div|span|p|a|b|i|u|s|em|strong|sup|sub|kbd|mark"
    r"|table|thead|tbody|tr|td|th|img|video|iframe|ul|ol|li|h[1-6]|pre|center|font)"
    r"(\s+[a-zA-Z-]+(=\"[^\"]*\"|='[^']*'|=[^\s>]+)?)*\s*/?>",
    re.IGNORECASE,
)

_SHORT_HTML_TAGS = frozenset("abipsu")

_HTML_HINTS = {
    "details": "use '{% cut \"Title\" %}...{% endcut %}' instead",
    "summary": "use '{% cut \"Title\" %}...{% endcut %}' instead",
    "br": "use a blank line or a trailing backslash instead",
    "img": "use Markdown image syntax '![alt](url)' instead",
    "a": "use Markdown link syntax '[text](url)' instead",
    "table": "use a Markdown pipe table or a '#|' multiline table instead",
}


def _is_prose_not_html(line: str, match: re.Match[str]) -> bool:
    """Tell a single-letter HTML tag apart from its homonyms in prose.

    ``<a>``, ``<b>``, ``<i>``, ``<p>``, ``<s>`` and ``<u>`` are also how generic
    type parameters (``Vec<a>``) and comparisons (``a<b and b>c``) look. A
    closing tag or an ``attr=value`` pair is decisive markup evidence — prose
    never produces either. Everything else that hangs off a word is prose.
    """
    tag = match.group(0)
    if tag.startswith("</") or "=" in tag:
        return False
    if match.group(1).lower() not in _SHORT_HTML_TAGS:
        return False
    return match.start() > 0 and line[match.start() - 1].isalnum()


def _strip_code_spans(line: str) -> str:
    """Remove inline code spans the way CommonMark parses them.

    A span opens with a backtick run and closes with the next run of exactly
    the same length (a double-backtick span may contain single backticks).
    Runs without a matching closer stay literal text. Each span is replaced
    with a single space so that removal cannot glue the surrounding text into
    new markup.
    """
    runs: list[tuple[int, int]] = []
    position = 0
    while (start := line.find("`", position)) != -1:
        end = start
        while end < len(line) and line[end] == "`":
            end += 1
        runs.append((start, end - start))
        position = end
    if not runs:
        return line

    parts: list[str] = []
    copied_up_to = 0
    index = 0
    while index < len(runs):
        start, length = runs[index]
        closer = next(
            (i for i in range(index + 1, len(runs)) if runs[i][1] == length),
            None,
        )
        if closer is None:
            index += 1
            continue
        parts.append(line[copied_up_to:start])
        parts.append(" ")
        copied_up_to = runs[closer][0] + runs[closer][1]
        index = closer + 1
    parts.append(line[copied_up_to:])
    return "".join(parts)


def _end_of_content_warnings(
    directive_opens: dict[str, list[int]],
    table_opens: list[int],
    fence_open_line: int | None,
) -> list[tuple[int, str]]:
    warnings: list[tuple[int, str]] = []
    for name, lines in directive_opens.items():
        warnings.extend(
            (
                line,
                f"line {line}: '{{% {name} %}}' is never closed with "
                f"'{{% end{name} %}}' — Yandex Wiki will render the block "
                "as literal text",
            )
            for line in lines
        )
    warnings.extend(
        (
            line,
            f"line {line}: multiline table '#|' is never closed with '|#' — "
            "Yandex Wiki will render it as literal text",
        )
        for line in table_opens
    )
    if fence_open_line is not None:
        warnings.append(
            (
                fence_open_line,
                f"line {fence_open_line}: code fence is never closed — "
                "everything after it will render as code",
            )
        )
    return warnings


def validate_yfm(content: str, *, max_warnings: int = MAX_WARNINGS) -> list[str]:
    """Check Markdown/YFM content against Yandex Wiki rendering quirks.

    Returns human-readable warnings sorted by line number, capped at
    max_warnings (callers that prepend their own warnings pass a reduced
    budget). An empty list means nothing suspicious was found.
    """
    warnings: list[tuple[int, str]] = []
    directive_opens: dict[str, list[int]] = {"note": [], "cut": [], "list": []}
    table_opens: list[int] = []
    fence: tuple[str, int] | None = None
    fence_open_line: int | None = None

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        fence_match = _FENCE_RE.match(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
                fence_open_line = line_number
                continue
            if (
                marker[0] == fence[0]
                and len(marker) >= fence[1]
                and not fence_match.group(2)
            ):
                fence = None
                fence_open_line = None
                continue
        if fence is not None:
            continue

        line = _strip_code_spans(raw_line)

        for match in _DIRECTIVE_OPEN_RE.finditer(line):
            directive_opens[match.group(1)].append(line_number)
        for match in _DIRECTIVE_END_RE.finditer(line):
            name = match.group(1)
            if directive_opens[name]:
                directive_opens[name].pop()
            else:
                warnings.append(
                    (
                        line_number,
                        f"line {line_number}: '{{% end{name} %}}' has no matching "
                        f"'{{% {name} %}}' — Yandex Wiki will render it as "
                        "literal text",
                    )
                )

        stripped = line.strip()
        if stripped == "#|":
            table_opens.append(line_number)
            continue
        if stripped == "|#":
            if table_opens:
                table_opens.pop()
            else:
                warnings.append(
                    (
                        line_number,
                        f"line {line_number}: '|#' closes a multiline table that "
                        "was never opened with '#|'",
                    )
                )
            continue

        alert_match = _GFM_ALERT_RE.match(line)
        if alert_match:
            alert = alert_match.group(1).upper()
            warnings.append(
                (
                    line_number,
                    f"line {line_number}: GFM alert '[!{alert}]' is not supported "
                    "by Yandex Wiki — it renders as a plain blockquote with a "
                    f"literal '[!{alert}]'. Use "
                    "'{% note info %}...{% endnote %}' instead "
                    "(types: info, tip, warning, alert)",
                )
            )

        for html_match in _HTML_TAG_RE.finditer(line):
            if _is_prose_not_html(line, html_match):
                continue
            tag_name = html_match.group(1).lower()
            hint = _HTML_HINTS.get(tag_name, "use Markdown/YFM syntax instead")
            warnings.append(
                (
                    line_number,
                    f"line {line_number}: raw HTML '{html_match.group(0)}' is not "
                    f"rendered by Yandex Wiki and shows as literal text — {hint}",
                )
            )
            break

    warnings.extend(
        _end_of_content_warnings(directive_opens, table_opens, fence_open_line)
    )
    warnings.sort(key=lambda item: item[0])

    messages = [message for _, message in warnings]
    if len(messages) > max_warnings:
        suppressed = len(messages) - max_warnings
        messages = messages[:max_warnings]
        messages.append(f"... {suppressed} more warning(s) suppressed")
    return messages
