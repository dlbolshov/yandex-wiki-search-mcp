from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, model_validator

from mcp_wiki.mcp.params import (
    AttachmentID,
    CloneTargetSlug,
    CommentID,
    GridCellPatch,
    GridColumnSpec,
    GridID,
    GridRevision,
    GridSortEntry,
    OptionalPageID,
    OptionalPageSlug,
    PageID,
    PageSlug,
    RecoveryToken,
)
from mcp_wiki.mcp.tools.common import (
    ToolContext,
    get_wiki,
    resolve_page_id,
    resolve_page_id_and_type,
    resolve_page_slug,
)
from mcp_wiki.mcp.utils import get_yandex_auth, resolve_page_locator
from mcp_wiki.wiki.proto.pages import validate_page_update_args
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentDeleteResponse,
    AttachmentDownloadResult,
    BaseWikiModel,
    ClonedPageRef,
    DeleteCommentResponse,
    DeletePageResponse,
    GridCellsResponse,
    GridCreateRequest,
    GridDeleteResponse,
    GridMutationResponse,
    GridOperationResponse,
    GridUpdateRequest,
    GridUpdateResponse,
    PageComment,
    RecoverPageResponse,
    UploadAttachmentResult,
    UploadLocation,
    WikiGrid,
    WikiGridPageRef,
    WikiPage,
)
from mcp_wiki.yfm import MAX_WARNINGS, validate_yfm

# open_world_hint=False on all of these: the tools talk to exactly one
# configured Wiki organization — a closed domain. Left unset it defaults
# to true.
ADDITIVE = ToolAnnotations(destructive_hint=False, open_world_hint=False)
ADDITIVE_IDEMPOTENT = ToolAnnotations(
    destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True, open_world_hint=False)
# destructive_hint deliberately unset (defaults to true): these overwrite
# existing state, retrying them is safe but running them is not additive.
IDEMPOTENT = ToolAnnotations(idempotent_hint=True, open_world_hint=False)
# Overwrites existing state like IDEMPOTENT, but a repeat is NOT free: for
# page_edit a replacement can match the text it just produced, so the hint
# would invite a client to double-apply an insertion after a lost response.
NON_IDEMPOTENT_WRITE = ToolAnnotations(open_world_hint=False)

YFM_CONTENT_NOTE = (
    "Content is Markdown (YFM): plain Markdown renders as-is, but GitHub-specific "
    "extensions ('[!NOTE]' alerts, raw HTML) do not — see the "
    "wiki-mcp://yfm-cheatsheet resource for YFM equivalents."
)


def _yfm_warnings_field() -> Any:
    """The `yfm_warnings` field, defined once for every response that carries it."""
    return Field(
        default=None,
        description=(
            "Markup warnings for the written content (the write itself "
            "succeeded): parts that will not render as intended on Yandex "
            "Wiki. See the wiki-mcp://yfm-cheatsheet resource for fixes."
        ),
    )


class PageWriteResponse(WikiPage):
    yfm_warnings: list[str] | None = _yfm_warnings_field()


class PageEditReplacement(BaseWikiModel):
    """One exact-text replacement for page_edit.

    A tool-level shape, so it lives here rather than in the wire-model module:
    the API has no partial-edit endpoint (full update and append only), so the
    tool reads the page, applies these in order, and writes the result back.
    """

    old_text: str = Field(
        min_length=1,
        description="Exact text to find in the page content (YFM markup, "
        "as page_get returns it).",
    )
    new_text: str = Field(
        description="Text to replace it with. May be empty to delete old_text."
    )
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence. When false, old_text must "
        "occur exactly once — several occurrences are an error listing "
        "their line numbers.",
    )

    @model_validator(mode="after")
    def _must_change_something(self) -> "PageEditReplacement":
        if self.old_text == self.new_text:
            raise ValueError("old_text and new_text are identical")
        return self


class PageEditResponse(BaseWikiModel):
    """Compact acknowledgment for page_edit — also tool-level, never from the wire.

    Deliberately not the page object: echoing content back would spend the
    tokens the tool exists to save.
    """

    page_id: int
    slug: str | None = None
    title: str | None = None
    occurrences_replaced: int = Field(
        description="Total occurrences replaced across all entries. Every "
        "entry applied — a replacement that did not match fails the whole "
        "call before anything is written."
    )
    yfm_warnings: list[str] | None = _yfm_warnings_field()


def _with_yfm_warnings(page: WikiPage, warnings: list[str]) -> PageWriteResponse:
    return PageWriteResponse.model_validate(
        {**page.model_dump(), "yfm_warnings": warnings or None}
    )


def _page_type_warnings(page_type: str | None) -> list[str]:
    if page_type is None or page_type == "wysiwyg":
        return []
    if page_type == "grid":
        return [
            "this page is a grid (dynamic table), not a Markdown page — "
            "page content writes may not apply; use the grid_* tools to "
            "edit its rows and columns"
        ]
    return [
        f"this page has page_type={page_type!r} (not the modern 'wysiwyg' "
        "format) — YFM directives may not render on legacy pages"
    ]


def _content_warnings(page_type: str | None, content: str) -> list[str]:
    """Combine page-type and markup warnings within the shared MAX_WARNINGS cap."""
    warnings = _page_type_warnings(page_type)
    return warnings + validate_yfm(content, max_warnings=MAX_WARNINGS - len(warnings))


_OCCURRENCE_LINES_CAP = 5


def _occurrence_lines(content: str, needle: str) -> list[int]:
    """1-based line numbers where needle occurs, capped.

    Line numbers instead of surrounding text: they are enough to point the
    agent at the right occurrence, and echoing page fragments into every
    ambiguity error would spend the tokens page_edit exists to save (the
    agent can always page_get for context).
    """
    lines: list[int] = []
    start = 0
    while len(lines) < _OCCURRENCE_LINES_CAP:
        index = content.find(needle, start)
        if index == -1:
            break
        lines.append(content.count("\n", 0, index) + 1)
        # Step past the whole match, not one character: str.count — which
        # produces the occurrence tally these lines are reported alongside —
        # is non-overlapping, and a scan that stepped by 1 would list more
        # positions than the count it annotates ("--" inside a "----" rule,
        # "| |" in a table).
        start = index + len(needle)
    return lines


def _apply_replacements(
    content: str, replacements: list[PageEditReplacement]
) -> tuple[str, int]:
    """Apply replacements in order; refuse loudly instead of guessing.

    Each old_text is matched against the content as already edited by the
    preceding entries — same sequential semantics as a multi-edit in an
    IDE agent.
    """
    occurrences_replaced = 0
    for index, replacement in enumerate(replacements, start=1):
        count = content.count(replacement.old_text)
        if count == 0:
            raise ValueError(
                f"Replacement {index}/{len(replacements)}: old_text not found "
                "in the page content. Nothing was written. Read the page with "
                "page_get and copy the text exactly, including whitespace."
            )
        if count > 1 and not replacement.replace_all:
            lines = ", ".join(
                str(n) for n in _occurrence_lines(content, replacement.old_text)
            )
            suffix = " and more" if count > _OCCURRENCE_LINES_CAP else ""
            raise ValueError(
                f"Replacement {index}/{len(replacements)}: old_text occurs "
                f"{count} times (lines {lines}{suffix}). Nothing was written. "
                "Extend old_text to make it unique, or set replace_all=true."
            )
        content = content.replace(replacement.old_text, replacement.new_text)
        occurrences_replaced += count
    return content, occurrences_replaced


def _require_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    return normalized


def _validate_row_ids(row_ids: list[str | int]) -> list[str]:
    if not row_ids:
        raise ValueError("row_ids must not be empty.")

    normalized: list[str] = []
    for index, row_id in enumerate(row_ids):
        if isinstance(row_id, str):
            normalized.append(
                _require_non_empty_text(row_id, field_name=f"row_ids[{index}]")
            )
        else:
            normalized.append(str(row_id))
    return normalized


def _validate_column_slugs(column_slugs: list[str]) -> list[str]:
    if not column_slugs:
        raise ValueError("column_slugs must not be empty.")

    return [
        _require_non_empty_text(column_slug, field_name=f"column_slugs[{index}]")
        for index, column_slug in enumerate(column_slugs)
    ]


def register_page_write_tools(
    mcp: MCPServer[Any],
    *,
    include_local_uploads: bool = True,
) -> None:
    @mcp.tool(
        title="Create Wiki Grid",
        description=(
            "Create a Yandex Wiki dynamic table resource on a page. "
            "This changes structured data."
        ),
        annotations=ADDITIVE,
    )
    async def grid_create(
        ctx: ToolContext,
        title: Annotated[
            str,
            Field(description="Grid title. Must be between 1 and 255 characters."),
        ],
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
    ) -> WikiGrid:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).grid_create(
            request=GridCreateRequest(
                title=_require_non_empty_text(title, field_name="title"),
                page=WikiGridPageRef(id=resolved_page_id),
            ),
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Update Wiki Grid",
        description=(
            "Update a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=IDEMPOTENT,
    )
    async def grid_update(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        title: Annotated[
            str | None,
            Field(description="New grid title."),
        ] = None,
        default_sort: Annotated[
            list[GridSortEntry] | None,
            Field(
                description=(
                    "Optional default sort order, for example "
                    "[{'column': 'status', 'direction': 'asc'}]."
                )
            ),
        ] = None,
    ) -> GridUpdateResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        normalized_title = (
            _require_non_empty_text(title, field_name="title")
            if title is not None
            else None
        )
        if default_sort is not None and not default_sort:
            raise ValueError("default_sort must not be empty.")
        normalized_default_sort = (
            [entry.to_mapping() for entry in default_sort] if default_sort else []
        )
        if normalized_title is None and not normalized_default_sort:
            raise ValueError("Provide at least one of title or default_sort.")

        return await get_wiki(ctx).grid_update(
            normalized_grid_id,
            request=GridUpdateRequest(
                revision=normalized_revision,
                title=normalized_title,
                default_sort=normalized_default_sort,
            ),
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Add Wiki Grid Rows",
        description=(
            "Add rows to a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=ADDITIVE,
    )
    async def grid_add_rows(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        rows: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Rows to add. Each row is a mapping of column slug or column ID "
                    "to a typed cell value."
                )
            ),
        ],
        position: Annotated[
            int | None,
            Field(
                description="Optional zero-based insertion position. Mutually exclusive with after_row_id."
            ),
        ] = None,
        after_row_id: Annotated[
            str | int | None,
            Field(
                description="Optional row ID after which to insert new rows. Mutually exclusive with position."
            ),
        ] = None,
    ) -> GridMutationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        if not rows:
            raise ValueError("rows must not be empty.")
        if position is not None and after_row_id is not None:
            raise ValueError("Provide either position or after_row_id, not both.")
        normalized_after_row_id = (
            _require_non_empty_text(str(after_row_id), field_name="after_row_id")
            if after_row_id is not None
            else None
        )

        return await get_wiki(ctx).grid_add_rows(
            normalized_grid_id,
            revision=normalized_revision,
            rows=rows,
            position=position,
            after_row_id=normalized_after_row_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Grid",
        description=(
            "Delete a Yandex Wiki dynamic table. This changes structured data and is destructive."
        ),
        annotations=DESTRUCTIVE,
    )
    async def grid_delete(
        ctx: ToolContext,
        grid_id: GridID,
    ) -> GridDeleteResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        return await get_wiki(ctx).grid_delete(
            normalized_grid_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Copy Wiki Grid",
        description=(
            "Copy a Yandex Wiki dynamic table to an existing target page. "
            "This starts an asynchronous operation and returns operation metadata."
        ),
        annotations=ADDITIVE,
    )
    async def grid_copy(
        ctx: ToolContext,
        grid_id: GridID,
        page_id: Annotated[
            PageID | None,
            Field(
                description="Target Wiki page numeric ID. Provide either page_id or slug."
            ),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Target Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
        title: Annotated[
            str | None,
            Field(description="Optional title for the copied grid."),
        ] = None,
    ) -> GridOperationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        target_slug = await resolve_page_slug(ctx, page_id=page_id, slug=slug)
        normalized_title = (
            _require_non_empty_text(title, field_name="title")
            if title is not None
            else None
        )
        return await get_wiki(ctx).grid_copy(
            normalized_grid_id,
            target=target_slug,
            title=normalized_title,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Update Wiki Grid Cells",
        description=(
            "Update cells in a Yandex Wiki dynamic table. This changes structured data. "
            "Each cell patch must include row_id, value, and exactly one of column_id or column_slug."
        ),
        annotations=IDEMPOTENT,
    )
    async def grid_update_cells(
        ctx: ToolContext,
        grid_id: GridID,
        cells: Annotated[
            list[GridCellPatch],
            Field(
                description=(
                    "Cell patches. Each object must include row_id, value, and exactly one "
                    "of column_id or column_slug."
                )
            ),
        ],
    ) -> GridCellsResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        if not cells:
            raise ValueError("cells must not be empty.")
        return await get_wiki(ctx).grid_update_cells(
            normalized_grid_id,
            cells=[cell.to_payload() for cell in cells],
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Grid Rows",
        description=(
            "Delete rows from a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=DESTRUCTIVE,
    )
    async def grid_delete_rows(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        row_ids: Annotated[
            list[str | int],
            Field(description="Row IDs to delete from the grid."),
        ],
    ) -> GridMutationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        return await get_wiki(ctx).grid_delete_rows(
            normalized_grid_id,
            revision=normalized_revision,
            row_ids=_validate_row_ids(row_ids),
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Add Wiki Grid Columns",
        description=(
            "Add columns to a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=ADDITIVE,
    )
    async def grid_add_columns(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        columns: Annotated[
            list[GridColumnSpec],
            Field(
                description=(
                    "Columns to add. Each object must include title, slug, type, and required."
                )
            ),
        ],
        position: Annotated[
            int | None,
            Field(
                description="Optional zero-based insertion position for new columns."
            ),
        ] = None,
    ) -> GridMutationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        if not columns:
            raise ValueError("columns must not be empty.")

        return await get_wiki(ctx).grid_add_columns(
            normalized_grid_id,
            revision=normalized_revision,
            columns=[column.to_payload() for column in columns],
            position=position,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Grid Columns",
        description=(
            "Delete columns from a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=DESTRUCTIVE,
    )
    async def grid_delete_columns(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        column_slugs: Annotated[
            list[str],
            Field(description="Column slugs to delete from the grid."),
        ],
    ) -> GridMutationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        return await get_wiki(ctx).grid_delete_columns(
            normalized_grid_id,
            revision=normalized_revision,
            column_slugs=_validate_column_slugs(column_slugs),
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Move Wiki Grid Row",
        description=(
            "Move a row inside a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def grid_move_row(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        row_id: Annotated[
            str | int,
            Field(description="Row ID to move."),
        ],
        position: Annotated[
            int | None,
            Field(
                description="Optional zero-based target position. Mutually exclusive with after_row_id."
            ),
        ] = None,
        after_row_id: Annotated[
            str | int | None,
            Field(
                description="Optional row ID after which the row should be placed. Mutually exclusive with position."
            ),
        ] = None,
    ) -> GridMutationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        normalized_row_id = _require_non_empty_text(str(row_id), field_name="row_id")
        if position is None and after_row_id is None:
            raise ValueError("Provide either position or after_row_id.")
        if position is not None and after_row_id is not None:
            raise ValueError("Provide either position or after_row_id, not both.")
        normalized_after_row_id = (
            _require_non_empty_text(str(after_row_id), field_name="after_row_id")
            if after_row_id is not None
            else None
        )
        return await get_wiki(ctx).grid_move_row(
            normalized_grid_id,
            revision=normalized_revision,
            row_id=normalized_row_id,
            position=position,
            after_row_id=normalized_after_row_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Move Wiki Grid Column",
        description=(
            "Move a column inside a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def grid_move_column(
        ctx: ToolContext,
        grid_id: GridID,
        revision: GridRevision,
        column_slug: Annotated[
            str,
            Field(description="Column slug to move."),
        ],
        position: Annotated[
            int,
            Field(description="Zero-based target position for the column."),
        ],
    ) -> GridMutationResponse:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        normalized_column_slug = _require_non_empty_text(
            column_slug, field_name="column_slug"
        )
        return await get_wiki(ctx).grid_move_column(
            normalized_grid_id,
            revision=normalized_revision,
            column_slug=normalized_column_slug,
            position=position,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Create Wiki Page",
        description=f"Create a Yandex Wiki page. {YFM_CONTENT_NOTE}",
        annotations=ADDITIVE,
    )
    async def page_create(
        ctx: ToolContext,
        slug: PageSlug,
        title: Annotated[str, Field(description="Wiki page title.")],
        content: Annotated[str, Field(description="Full page content.")],
    ) -> PageWriteResponse:
        page = await get_wiki(ctx).page_create(
            slug=slug,
            title=title,
            content=content,
            auth=get_yandex_auth(ctx),
        )
        return _with_yfm_warnings(page, validate_yfm(content))

    @mcp.tool(
        title="Update Wiki Page",
        description=(
            "Update an existing Yandex Wiki page: title, content, or a "
            "redirect to another page. Content replacement is full-page "
            f"when content is provided. {YFM_CONTENT_NOTE}"
        ),
        annotations=IDEMPOTENT,
    )
    async def page_update(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        title: Annotated[str | None, Field(description="New page title.")] = None,
        content: Annotated[
            str | None,
            Field(description="New full page content. Replaces the existing body."),
        ] = None,
        redirect_to_page_id: Annotated[
            int | None,
            Field(
                description=(
                    "Make this page redirect to the page with this id. The "
                    "page keeps its own content; the redirect state reads "
                    "back via page_get with fields=['redirect']."
                ),
                gt=0,
            ),
        ] = None,
        clear_redirect: Annotated[
            bool,
            Field(description="Remove this page's existing redirect."),
        ] = False,
        allow_merge: Annotated[
            bool,
            Field(
                description="Whether to allow Yandex Wiki three-way merge on concurrent edits."
            ),
        ] = False,
        is_silent: Annotated[
            bool,
            Field(
                description="Whether to suppress notifications when supported by the API."
            ),
        ] = False,
    ) -> PageWriteResponse:
        # Same validator the client runs, called here so a malformed request
        # is refused before slug resolution spends a GET.
        validate_page_update_args(
            title=title,
            content=content,
            redirect_to_page_id=redirect_to_page_id,
            clear_redirect=clear_redirect,
        )
        resolved_page_id, resolved_page_type = await resolve_page_id_and_type(
            ctx, page_id=page_id, slug=slug
        )
        page = await get_wiki(ctx).page_update(
            resolved_page_id,
            title=title,
            content=content,
            redirect_to_page_id=redirect_to_page_id,
            clear_redirect=clear_redirect,
            allow_merge=allow_merge,
            is_silent=is_silent,
            auth=get_yandex_auth(ctx),
        )
        warnings: list[str] = []
        if content is not None:
            warnings = _content_warnings(resolved_page_type, content)
        return _with_yfm_warnings(page, warnings)

    @mcp.tool(
        title="Edit Wiki Page Content",
        description=(
            "Edit a Yandex Wiki page by exact-text replacements, without "
            "resending the whole page: reads the current content, applies "
            "the replacements in order, and writes the result back with a "
            "single update. Each old_text must match the stored YFM markup "
            "exactly (copy it from page_get, whitespace included) and occur "
            "exactly once unless replace_all is set — a missing or ambiguous "
            "match fails the whole call before anything is written. NOTE: "
            "the Wiki API has no page revisions, so the read-modify-write is "
            "not atomic; allow_merge (on by default) asks Wiki to merge a "
            "concurrent edit that landed in between rather than overwrite it. "
            "Do NOT blindly retry a call whose result you did not see: a "
            "replacement whose new_text contains its own old_text applies "
            f"again on a repeat. {YFM_CONTENT_NOTE}"
        ),
        # destructive_hint defaults to true (this overwrites existing state),
        # and idempotent_hint is deliberately NOT set: an insertion-shaped
        # replacement — old_text "## Setup" -> new_text "## Setup\n\n…" —
        # still matches its own output, so a client retrying on the strength
        # of the hint would insert twice. page_append_content is annotated
        # without the hint for exactly the same reason.
        annotations=NON_IDEMPOTENT_WRITE,
    )
    async def page_edit(
        ctx: ToolContext,
        replacements: Annotated[
            list[PageEditReplacement],
            Field(
                min_length=1,
                description="Replacements to apply sequentially: each "
                "old_text is matched against the content as already edited "
                "by the preceding entries.",
            ),
        ],
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        allow_merge: Annotated[
            bool,
            Field(
                description="Let Yandex Wiki three-way merge an edit that "
                "landed between this tool's read and its write. On by "
                "default: the read-modify-write has no revision to lock "
                "against, so without it a concurrent edit is overwritten."
            ),
        ] = True,
        is_silent: Annotated[
            bool,
            Field(
                description="Whether to suppress notifications when supported by the API."
            ),
        ] = False,
    ) -> PageEditResponse:
        resolved_page_id, resolved_slug = resolve_page_locator(
            page_id=page_id, slug=slug
        )
        auth = get_yandex_auth(ctx)
        wiki = get_wiki(ctx)
        if resolved_page_id is not None:
            page = await wiki.page_get(resolved_page_id, fields=["content"], auth=auth)
        elif resolved_slug is not None:
            page = await wiki.page_get_by_slug(
                resolved_slug, fields=["content"], auth=auth
            )
        else:  # pragma: no cover - narrowing; resolve_page_locator raised
            # already unless exactly one of the two is set.
            raise ValueError("Either page_id or slug must be provided.")
        content = page.content if isinstance(page.content, str) else None
        if content is None:
            raise ValueError(
                f"Page {page.slug or page.id} has no editable text content "
                f"(page_type={page.page_type!r})."
            )

        new_content, occurrences_replaced = _apply_replacements(content, replacements)
        # allow_merge defaults to True here, unlike on page_update: this is a
        # read-modify-write, the same shape as page_append_content's anchor
        # fallback, which passes it for the same reason. page_update's caller
        # supplies the whole body and can mean the overwrite; here the body was
        # derived from a read that may already be stale.
        updated = await wiki.page_update(
            page.id,
            content=new_content,
            allow_merge=allow_merge,
            is_silent=is_silent,
            auth=auth,
        )
        return PageEditResponse(
            page_id=updated.id,
            slug=updated.slug or page.slug,
            title=updated.title or page.title,
            occurrences_replaced=occurrences_replaced,
            yfm_warnings=_content_warnings(page.page_type, new_content) or None,
        )

    @mcp.tool(
        title="Append Wiki Content",
        description=(
            "Append content to the top, bottom, or anchor of a Yandex Wiki page. "
            f"{YFM_CONTENT_NOTE}"
        ),
        annotations=ADDITIVE,
    )
    async def page_append_content(
        ctx: ToolContext,
        content: Annotated[str, Field(description="Content block to append.")],
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        location: Annotated[
            UploadLocation,
            Field(
                description="Target location in the page body when anchor is not provided."
            ),
        ] = "bottom",
        anchor: Annotated[
            str | None,
            Field(
                description="Anchor name like '#release-notes'. Overrides location when provided."
            ),
        ] = None,
    ) -> PageWriteResponse:
        resolved_page_id, resolved_page_type = await resolve_page_id_and_type(
            ctx, page_id=page_id, slug=slug
        )
        page = await get_wiki(ctx).page_append_content(
            resolved_page_id,
            content=content,
            location=location,
            anchor=anchor,
            auth=get_yandex_auth(ctx),
        )
        return _with_yfm_warnings(page, _content_warnings(resolved_page_type, content))

    @mcp.tool(
        title="Clone Wiki Page",
        description=(
            "Copy a Yandex Wiki page to a new slug and return the copy's id "
            "and slug once the operation completes. Copies title and content "
            "only: child pages, comments, attachments, and edit history stay "
            "with the original, and the copy gets a new page id. Fails when "
            "the target slug is already occupied. The Wiki API has no true "
            "move/rename; to relocate a page, clone it and delete the "
            "original — re-uploading attachments and re-creating grids on "
            "the copy if they must follow."
        ),
        annotations=ADDITIVE,
    )
    async def page_clone(
        ctx: ToolContext,
        target: CloneTargetSlug,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        title: Annotated[
            str | None,
            Field(description="Optional title for the copied page."),
        ] = None,
    ) -> ClonedPageRef:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        normalized_title = (
            _require_non_empty_text(title, field_name="title")
            if title is not None
            else None
        )
        return await get_wiki(ctx).page_clone(
            resolved_page_id,
            target=target,
            title=normalized_title,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Add Page Comment",
        description="Add a comment to a Yandex Wiki page.",
        annotations=ADDITIVE,
    )
    async def page_add_comment(
        ctx: ToolContext,
        body: Annotated[str, Field(description="Comment body.")],
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        parent_id: Annotated[
            CommentID | None,
            Field(description="Optional parent comment ID for a reply."),
        ] = None,
        thread_id: Annotated[
            CommentID | None,
            Field(
                description="Optional thread ID when replying in an existing thread."
            ),
        ] = None,
    ) -> PageComment:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_add_comment(
            resolved_page_id,
            body=body,
            parent_id=parent_id,
            thread_id=thread_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Page Comment",
        description=(
            "Delete a comment from a Yandex Wiki page and return the page's "
            "updated comment count. Comment ids come from page_get_comments."
        ),
        annotations=DESTRUCTIVE,
    )
    async def page_delete_comment(
        ctx: ToolContext,
        comment_id: CommentID,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
    ) -> DeleteCommentResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_delete_comment(
            resolved_page_id,
            comment_id=comment_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Page Attachment",
        description=(
            "Delete an attachment from a Yandex Wiki page. File ids come "
            "from page_get_attachments. Does not touch page content — any "
            "file macro referencing the attachment stays behind, broken."
        ),
        annotations=DESTRUCTIVE,
    )
    async def page_delete_attachment(
        ctx: ToolContext,
        file_id: AttachmentID,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
    ) -> AttachmentDeleteResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_delete_attachment(
            resolved_page_id,
            file_id=file_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Page",
        description="Delete a Yandex Wiki page and return a recovery token.",
        annotations=DESTRUCTIVE,
    )
    async def page_delete(
        ctx: ToolContext,
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
    ) -> DeletePageResponse:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_delete(
            resolved_page_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Recover Wiki Page",
        description="Recover a deleted Yandex Wiki page using a recovery token.",
        annotations=ADDITIVE,
    )
    async def page_recover(
        ctx: ToolContext,
        recovery_token: RecoveryToken,
    ) -> RecoverPageResponse:
        return await get_wiki(ctx).page_recover(
            recovery_token,
            auth=get_yandex_auth(ctx),
        )

    if not include_local_uploads:
        # file_path/save_to name paths on the filesystem of the machine
        # running THIS server. In a multi-user OAuth deployment that is the
        # shared server host, not the caller's machine: the tools would be
        # useless for their purpose, upload would let any authenticated user
        # exfiltrate server-local files (.env, secrets) into their own wiki,
        # and download would let them write onto the server's disk.
        return

    @mcp.tool(
        title="Upload Page Attachment",
        description="Upload a local file to Yandex Wiki and attach it to a page.",
        annotations=ADDITIVE,
    )
    async def page_upload_attachment(
        ctx: ToolContext,
        file_path: Annotated[
            str,
            Field(
                description="Local filesystem path to the file that should be uploaded."
            ),
        ],
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        append_markup: Annotated[
            bool,
            Field(
                description="Whether to append Wiki file macro markup to the page after uploading the attachment."
            ),
        ] = False,
        append_location: Annotated[
            UploadLocation,
            Field(
                description="Where to append the generated file macro when append_markup is true."
            ),
        ] = "bottom",
    ) -> UploadAttachmentResult:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_upload_attachment(
            resolved_page_id,
            file_path=file_path,
            append_markup=append_markup,
            append_location=append_location,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Download Page Attachment",
        description=(
            "Download a Yandex Wiki page attachment to a local file: the "
            "bytes stream to disk without a size cap and never enter the "
            "conversation — the counterpart to page_read_attachment, for "
            "getting the artifact itself (a PDF, an archive, a large "
            "export). Writes atomically; refuses to replace an existing "
            "file unless overwrite is true. File ids come from "
            "page_get_attachments."
        ),
        # NON_IDEMPOTENT_WRITE, not ADDITIVE: with overwrite=true this
        # replaces a local file, and destructive_hint=False would promise
        # it cannot. The write is to the local disk, not the Wiki — which
        # is exactly why it sits behind the same gate as upload.
        annotations=NON_IDEMPOTENT_WRITE,
    )
    async def page_download_attachment(
        ctx: ToolContext,
        file_id: AttachmentID,
        save_to: Annotated[
            str,
            Field(
                description=(
                    "Local filesystem path (a file, not a directory) to save "
                    "the attachment to. Missing parent directories are "
                    "created; '~' expands to the home directory."
                )
            ),
        ],
        page_id: OptionalPageID = None,
        slug: OptionalPageSlug = None,
        overwrite: Annotated[
            bool,
            Field(
                description=(
                    "Whether an existing file at save_to may be replaced. "
                    "When false (default), the call fails instead of "
                    "overwriting."
                )
            ),
        ] = False,
    ) -> AttachmentDownloadResult:
        resolved_page_id = await resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await get_wiki(ctx).page_download_attachment_to_path(
            resolved_page_id,
            file_id=file_id,
            save_to=save_to,
            overwrite=overwrite,
            auth=get_yandex_auth(ctx),
        )
