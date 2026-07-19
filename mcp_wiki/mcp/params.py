from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_wiki.wiki.proto.types.pages import (
    GridFieldEnum,
    PageFieldEnum,
    ResourceTypeEnum,
)

PageID = Annotated[int, Field(description="Wiki page numeric ID.", gt=0)]
CommentID = Annotated[int, Field(description="Wiki comment numeric ID.", gt=0)]
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
RecoveryToken = Annotated[
    str,
    Field(description="Recovery token returned by the page_delete tool."),
]
Cursor = Annotated[
    str | None,
    Field(description="Opaque pagination cursor returned by the previous call."),
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
SearchResultPageSize = Annotated[
    int,
    Field(
        description="Number of search results to return (1-50). "
        "Use 50 when combining with the client-side filters (slug_prefix/result_type).",
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


instructions = """Tools for interacting with Yandex Wiki.
Use these tools to:
- Discover pages across the whole Wiki with page_search, then open a result by its slug with page_get.
- Read Wiki pages by slug or ID
- Traverse a page subtree
- Read comments, resources, and attachments
- Read page grids and get dynamic tables
- Create, copy, and update dynamic tables; add and delete grid rows; add grid columns
- Create, update, append to, delete, and recover pages
- Add comments and upload attachments from the local filesystem

In russian Yandex Wiki is called "Яндекс Вики" or "Вики".
If a tool accepts `page_id` and `slug`, provide exactly one of them.
If a tool returns `next_cursor`, continue calling the same tool with that cursor until it becomes empty when you need the full result set.
"""
