from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_wiki.wiki.proto.types.pages import (
    GridFieldEnum,
    PageFieldEnum,
    ResourceTypeEnum,
)

PageID = Annotated[int, Field(description="Wiki page numeric ID.", gt=0)]
CommentID = Annotated[int, Field(description="Wiki comment numeric ID.", gt=0)]
AttachmentID = Annotated[
    int,
    Field(
        description=(
            "Attachment (file) numeric ID, as listed by page_get_attachments."
        ),
        gt=0,
    ),
]
OptionalPageID = Annotated[
    int | None,
    Field(description="Wiki page numeric ID. Provide either page_id or slug.", gt=0),
]
OptionalPageSlug = Annotated[
    str | None,
    Field(
        description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
    ),
]
PageSlug = Annotated[
    str,
    Field(
        description=(
            "Wiki page slug like 'users/login/project/page'. "
            "A full Wiki page URL is also accepted."
        )
    ),
]
CloneTargetSlug = Annotated[
    str,
    Field(
        description=(
            "Slug for the copy, like 'users/login/project/copy-name'. "
            "A full Wiki page URL is also accepted. Must not be occupied "
            "by an existing page."
        )
    ),
]
RecoveryToken = Annotated[
    str,
    Field(description="Recovery token returned by the page_delete tool."),
]
Cursor = Annotated[
    str | None,
    Field(description="Opaque pagination cursor returned by the previous call."),
]
FetchAll = Annotated[
    bool,
    Field(
        description=(
            "Follow pagination automatically and return all items in one call "
            "(up to ~500). The response then carries truncated=false when the "
            "list is complete, or truncated=true with next_cursor to continue."
        )
    ),
]
PageSize = Annotated[
    int,
    Field(description="Page size for cursor-based endpoints.", ge=1, le=100),
]
GridID = Annotated[str, Field(description="Wiki dynamic table ID.")]
GridRevision = Annotated[
    str,
    Field(
        description=(
            "Current grid revision for optimistic locking. "
            "Fetch the grid first and pass its latest revision."
        )
    ),
]
GridPageSize = Annotated[
    int,
    Field(description="Page size for page grid list endpoints.", ge=1, le=50),
]
PageFields = Annotated[
    list[PageFieldEnum] | None,
    Field(
        description=(
            "Additional page fields to fetch. Supported values: "
            "content, attributes, breadcrumbs, redirect, "
            "access_policy, access_lists, owner. "
            "Pass them as an array, for example ['content', 'breadcrumbs']."
        )
    ),
]
GridFields = Annotated[
    list[GridFieldEnum] | None,
    Field(
        description=(
            "Additional grid fields to fetch. Supported values: "
            "attributes, user_permissions. "
            "Pass them as an array, for example ['attributes']."
        )
    ),
]
SearchQuery = Annotated[
    str,
    Field(
        description="Full-text search query over the whole Wiki. "
        "Wrap a multi-word exact phrase in double quotes.",
        min_length=1,
    ),
]
SearchResultLimit = Annotated[
    int,
    Field(
        description="Number of search results to return (1-50). Filters are "
        "applied by the search backend before this limit, so filtered "
        "searches do not need a larger limit to compensate. With "
        "highlight=true every page is hard-capped at 10 results, and this "
        "value only trims below that cap.",
        ge=1,
        le=50,
    ),
]
ResourceTypes = Annotated[
    list[ResourceTypeEnum] | None,
    Field(
        description=(
            "Optional resource types filter. Supported values: "
            "attachment, grid. Pass them as an array, for example ['attachment']."
        )
    ),
]


def _require_non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty")
    return stripped


class GridCellPatch(BaseModel):
    """A single cell update for grid_update_cells."""

    model_config = ConfigDict(extra="forbid")

    row_id: str | int = Field(description="Row ID that contains the cell.")
    value: Any = Field(description="New typed cell value. Pass null to clear the cell.")
    column_id: str | None = Field(
        default=None,
        description="Column ID. Provide exactly one of column_id or column_slug.",
    )
    column_slug: str | None = Field(
        default=None,
        description="Column slug. Provide exactly one of column_id or column_slug.",
    )

    @field_validator("row_id")
    @classmethod
    def _validate_row_id(cls, value: str | int) -> str | int:
        if isinstance(value, int):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("column_id", "column_slug")
    @classmethod
    def _validate_column_ref_text(cls, value: str | None) -> str | None:
        return _require_non_empty(value)

    @model_validator(mode="after")
    def _validate_column_ref(self) -> "GridCellPatch":
        if (self.column_id is None) == (self.column_slug is None):
            raise ValueError("Provide exactly one of column_id or column_slug.")
        return self

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"row_id": self.row_id, "value": self.value}
        if self.column_id is not None:
            payload["column_id"] = self.column_id
        if self.column_slug is not None:
            payload["column_slug"] = self.column_slug
        return payload


class GridColumnSpec(BaseModel):
    """A column definition for grid_add_columns.

    Extra properties (width, select_options, description, ...) are passed
    through to the Wiki API as-is.
    """

    model_config = ConfigDict(extra="allow")

    title: str = Field(description="Column title.")
    slug: str = Field(description="Column slug.")
    type: str = Field(
        description=(
            "Column type, for example string, number, checkbox, date, select, staff."
        )
    )
    required: bool = Field(
        description=(
            "Whether cells in the column must be filled. "
            "The Wiki API requires this field explicitly."
        )
    )

    @field_validator("title", "slug", "type")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump()


class GridSortEntry(BaseModel):
    """A default sort entry for grid_update."""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(description="Column slug to sort by.")
    direction: Literal["asc", "desc"] = Field(
        default="asc", description="Sort direction."
    )

    @field_validator("column")
    @classmethod
    def _validate_column(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    def to_mapping(self) -> dict[str, Literal["asc", "desc"]]:
        return {self.column: self.direction}


def build_instructions(*, include_local_uploads: bool, read_only: bool) -> str:
    """Server instructions matching the registered tool surface.

    Static instructions would send agents to tools that do not exist: the
    write capabilities and their usage notes survive only outside
    WIKI_READ_ONLY, and the comments line offers local-filesystem transfers
    only when page_upload_attachment and page_download_attachment are
    actually registered (they are not under OAuth, see
    Settings.include_local_uploads).
    """
    capabilities = [
        "- Discover pages across the whole Wiki with page_search, then open a result by its slug with page_get.",
        "- Read Wiki pages by slug or ID",
        "- Traverse a page subtree, or the whole Wiki with page_get_descendants(from_root=true)",
        "- Read comments, resources, and attachments — and read an attachment's content into the conversation with page_read_attachment",
        "- Read page grids and get dynamic tables",
        "- Look up the calling user's username and personal-section slug with user_get_current",
    ]
    if not read_only:
        capabilities += [
            "- Create, update, copy, and delete dynamic tables; add, move, and delete grid rows and columns; update cells",
            "- Create, update, append to, clone, delete, and recover pages; point a page at another with a redirect; edit page content by exact-text replacements with page_edit instead of resending the whole page",
            "- Add and delete comments, delete attachments, and upload attachments from the local filesystem or download them to it with page_download_attachment"
            if include_local_uploads
            else "- Add and delete comments, and delete attachments",
        ]

    notes = [
        'In russian Yandex Wiki is called "Яндекс Вики" or "Вики".',
        "If a tool accepts `page_id` and `slug`, provide exactly one of them.",
        "The `content` on a `page_search` result is an excerpt of at most ~510 characters taken from wherever the match sits in the page — not the page and not a summary, sometimes without the query terms in it, highlighted with <em> tags only when highlight=true was passed. Use it to pick a result, then read the page with `page_get` before answering from it.",
        "`page_search` paginates only with highlight=true: that mode caps every page at 10 results and unlocks `cursor` (~100 results reachable). Without it a single call returns up to 50 results — the top of the ranking — and that is the whole reachable set.",
        "When you need a full list from a cursor-paginated tool, pass `fetch_all=true` — the server follows the cursors for you and returns everything in one call. Only if the reply carries `truncated: true` is the list incomplete: continue from `next_cursor`, or narrow the request. Walk cursors by hand only when a tool has no `fetch_all`.",
    ]
    if read_only:
        notes.insert(
            0,
            "This server runs in read-only mode: tools that modify the Wiki are not registered.",
        )
    else:
        notes += [
            "Grid mutations are serialized per grid and need the grid's current `revision`: call the `grid_*` write tools one at a time, never several against the same grid in one batch. A 409 means the previous one is still finishing and yours was not applied — re-read the grid and retry.",
            "Page content is Markdown (YFM): plain Markdown renders as-is, but GitHub-specific extensions ('[!NOTE]' alerts, raw HTML) do not — the wiki-mcp://yfm-cheatsheet resource maps them to YFM equivalents. Write tools return `yfm_warnings` when submitted content will not render as intended.",
        ]

    lines = [
        "Tools for interacting with Yandex Wiki.",
        "Use these tools to:",
        *capabilities,
        "",
        *notes,
    ]
    return "\n".join(lines) + "\n"
