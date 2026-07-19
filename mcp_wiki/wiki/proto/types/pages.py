from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BaseWikiModel(BaseModel):
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


class WikiPage(BaseWikiModel):
    id: int
    slug: str | None = None
    title: str | None = None
    page_type: str | None = None
    content: Any = None
    attributes: dict[str, Any] | None = None
    breadcrumbs: list[dict[str, Any]] | None = None
    redirect: dict[str, Any] | None = None
    created_at: str | None = None
    modified_at: str | None = None


class SearchResultItem(BaseWikiModel):
    url: str | None = None
    slug: str | None = None
    title: str | None = None
    body: str | None = None
    type: str | None = None
    modified_at: int | None = None


class SearchResponse(BaseWikiModel):
    results: list[SearchResultItem] = Field(default_factory=list)
    total_documents: int | None = None
    total_pages: int | None = None
    page_id: int | None = None
    search_client: str | None = None
    uid: str | None = None


class PageComment(BaseWikiModel):
    id: int
    body: str | None = None
    parent_id: int | None = None
    thread_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    user: dict[str, Any] | None = None


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
    user: dict[str, Any] | None = None


class WikiResource(BaseWikiModel):
    type: str
    item: dict[str, Any]


class DescendantsResponse(BaseWikiModel):
    results: list[WikiPage] = Field(default_factory=list)
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


class WikiGridColumn(BaseWikiModel):
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


class WikiGridRow(BaseWikiModel):
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


class WikiGrid(BaseWikiModel):
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


class GridUpdateResponse(BaseWikiModel):
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


class UploadSessionResponse(BaseWikiModel):
    session_id: str


class AttachmentResultsResponse(BaseWikiModel):
    results: list[WikiAttachment] = Field(default_factory=list)


class UploadAttachmentResult(BaseWikiModel):
    page_id: int
    attachments: list[WikiAttachment] = Field(default_factory=list)
    appended_markup: bool = False
    appended_content: str | None = None
