from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)


class BaseWikiModel(BaseModel):
    """Fixed-shape API models.

    Unknown keys are dropped (`extra="ignore"`) and `None` values are omitted
    from dumps — both to keep MCP tool results lean for LLM consumers. Fields
    the live API actually sends must be declared explicitly (see
    docs/api-notes.md and scripts/contract_sweep.py).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @model_serializer(mode="wrap")
    def _drop_none(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class DynamicWikiModel(BaseWikiModel):
    """Dynamic payloads (grid values and friends): unknown keys are data."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class PageFieldEnum(StrEnum):
    CONTENT = "content"
    ATTRIBUTES = "attributes"
    BREADCRUMBS = "breadcrumbs"
    REDIRECT = "redirect"
    ACCESS_POLICY = "access_policy"
    ACCESS_LISTS = "access_lists"
    OWNER = "owner"


class GridFieldEnum(StrEnum):
    ATTRIBUTES = "attributes"
    USER_PERMISSIONS = "user_permissions"


class ResourceTypeEnum(StrEnum):
    ATTACHMENT = "attachment"
    GRID = "grid"


UploadLocation = Literal["top", "bottom"]


class WikiUser(BaseWikiModel):
    """Trimmed user reference — the API sends much more (identity, flags…)."""

    id: int | None = None
    username: str | None = None
    display_name: str | None = None


class WikiPage(BaseWikiModel):
    id: int
    slug: str | None = None
    title: str | None = None
    page_type: str | None = None
    content: Any = None
    attributes: dict[str, Any] | None = None
    breadcrumbs: list[dict[str, Any]] | None = None
    redirect: dict[str, Any] | None = None
    access_policy: dict[str, Any] | None = None
    access_lists: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    created_at: str | None = None
    modified_at: str | None = None


class SearchResultItem(BaseWikiModel):
    url: str | None = None
    slug: str | None = None
    title: str | None = None
    content: str | None = None
    type: str | None = None
    modified_at: str | None = None


class SearchResponse(BaseWikiModel):
    results: list[SearchResultItem] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None


class PageComment(BaseWikiModel):
    id: int
    body: str | None = None
    parent_id: int | None = None
    thread_id: int | None = None
    created_at: str | None = None
    author: WikiUser | None = None
    inline_text: str | None = None
    is_deleted: bool | None = None
    resolve_status: str | None = None
    reactions: list[Any] | None = None
    thread_info: Any = None


class WikiAttachment(BaseWikiModel):
    id: int
    name: str | None = None
    download_url: str | None = None
    size: str | None = None
    description: str | None = None
    mimetype: str | None = None
    created_at: str | None = None
    has_preview: bool | None = None
    check_status: str | None = None
    is_downloadable: bool | None = None
    user: WikiUser | None = None


class WikiResource(BaseWikiModel):
    type: str
    item: dict[str, Any]


class DescendantItem(BaseWikiModel):
    """Descendants carry only id and slug live — titles never arrive."""

    id: int
    slug: str | None = None


class DescendantsResponse(BaseWikiModel):
    results: list[DescendantItem] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None


class CommentsResponse(BaseWikiModel):
    results: list[PageComment] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None


class AttachmentListResponse(BaseWikiModel):
    results: list[WikiAttachment] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None


class ResourcesResponse(BaseWikiModel):
    results: list[WikiResource] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None


class WikiGridPageRef(BaseWikiModel):
    id: int | str | None = None
    slug: str | None = None


class WikiGridSort(BaseWikiModel):
    slug: str | None = None
    title: str | None = None
    direction: str | None = None


class WikiGridColumn(DynamicWikiModel):
    id: str | None = None
    slug: str | None = None
    title: str | None = None
    type: str | None = None
    required: bool | None = None
    width: int | None = None
    width_units: str | None = None
    pinned: str | None = None
    color: str | None = None
    multiple: bool | None = None
    format: str | None = None
    ticket_field: str | None = None
    select_options: list[str] | None = None
    mark_rows: bool | None = None
    description: str | None = None


class WikiGridStructure(BaseWikiModel):
    default_sort: list[WikiGridSort] = Field(default_factory=list)
    columns: list[WikiGridColumn] = Field(default_factory=list)


class WikiGridRow(DynamicWikiModel):
    id: str | int | None = None
    row: list[Any] = Field(default_factory=list)
    pinned: bool | None = None
    color: str | None = None


class WikiGridSummary(BaseWikiModel):
    id: str | int
    title: str | None = None
    created_at: str | None = None


class GridsResponse(BaseWikiModel):
    results: list[WikiGridSummary] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None


class WikiGrid(DynamicWikiModel):
    id: str | int
    title: str | None = None
    page: WikiGridPageRef | None = None
    structure: WikiGridStructure | None = None
    rich_text_format: str | None = None
    rows: list[WikiGridRow] = Field(default_factory=list)
    revision: str | None = None
    user_permissions: list[str] | None = None
    attributes: dict[str, Any] | None = None
    template_id: int | None = None
    created_at: str | None = None


class GridCreateRequest(BaseWikiModel):
    title: str
    page: WikiGridPageRef


class GridUpdateRequest(BaseWikiModel):
    revision: str
    title: str | None = None
    default_sort: list[dict[str, Literal["asc", "desc"]]] = Field(default_factory=list)

    @field_validator("default_sort")
    @classmethod
    def validate_default_sort(
        cls, value: list[dict[str, Literal["asc", "desc"]]]
    ) -> list[dict[str, Literal["asc", "desc"]]]:
        for index, item in enumerate(value):
            if len(item) != 1:
                raise ValueError(
                    f"default_sort[{index}] must contain exactly one column slug to direction mapping."
                )
            key = next(iter(item))
            if not key.strip():
                raise ValueError(
                    f"default_sort[{index}] column slug must not be empty."
                )
        return value


class GridMutationResponse(BaseWikiModel):
    revision: str | None = None
    results: list[WikiGridRow] = Field(default_factory=list)
    cells: list[Any] | None = None


class GridUpdateResponse(DynamicWikiModel):
    id: str | int | None = None
    title: str | None = None
    page: WikiGridPageRef | None = None
    structure: WikiGridStructure | None = None
    rich_text_format: str | None = None
    rows: list[WikiGridRow] = Field(default_factory=list)
    revision: str | None = None
    user_permissions: list[str] | None = None
    attributes: dict[str, Any] | None = None
    template_id: int | None = None
    created_at: str | None = None


class GridOperationRef(BaseWikiModel):
    type: str | None = None
    id: str | None = None


class GridOperationResponse(BaseWikiModel):
    operation: GridOperationRef | None = None
    dry_run: bool | None = None
    status_url: str | None = None


class DeletePageResponse(BaseWikiModel):
    recovery_token: str | None = None


class RecoverPageResponse(BaseWikiModel):
    id: int
    slug: str | None = None
    pages_count: int | None = None


class UploadSessionResponse(BaseWikiModel):
    session_id: str


class AttachmentResultsResponse(BaseWikiModel):
    results: list[WikiAttachment] = Field(default_factory=list)


class UploadAttachmentResult(BaseWikiModel):
    page_id: int
    attachments: list[WikiAttachment] = Field(default_factory=list)
    appended_markup: bool = False
    appended_content: str | None = None
