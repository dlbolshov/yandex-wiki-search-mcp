from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
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
        return data  # pragma: no cover - a model always serializes to a dict

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        """Strip pydantic's auto-generated titles — pure schema-token noise."""
        json_schema = dict(handler(core_schema))
        json_schema.pop("title", None)
        for prop in json_schema.get("properties", {}).values():
            if isinstance(prop, dict):  # pragma: no branch - always a dict
                prop.pop("title", None)
        return json_schema


class DynamicWikiModel(BaseWikiModel):
    """Dynamic payloads (grid values and friends): unknown keys are data.

    Declared fields are still API form, so a `None` there means "not sent"
    and is dropped like everywhere else. Unknown keys are the user's own
    columns, where `null` is a value — "this cell is empty" has to stay
    distinguishable from "this column does not exist".
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_serializer(mode="wrap")
    def _drop_none(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if not isinstance(data, dict):  # pragma: no cover - always a dict
            return data
        extras = self.model_extra or {}
        return {
            key: value
            for key, value in data.items()
            if value is not None or key in extras
        }


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


class WikiOwner(BaseWikiModel):
    """Page owner: the API nests the full identity payload under `user`."""

    user: WikiUser | None = None
    group: dict[str, Any] | None = None


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
    owner: WikiOwner | None = None
    created_at: str | None = None
    modified_at: str | None = None


class SearchDateInterval(BaseWikiModel):
    """Closed date-time interval for search filters.

    The API requires both bounds: `from` alone is a 400 SEARCH_BAD_REQUEST
    (verified live 2026-08-11), so both fields are required here and the
    schema says so instead of the wire error.
    """

    from_: str = Field(
        alias="from",
        description="Interval start, ISO 8601 date-time, e.g. '2026-01-01T00:00:00Z'.",
    )
    to: str = Field(
        description="Interval end, ISO 8601 date-time. The API rejects an "
        "open-ended interval, so both bounds are required."
    )


class SearchAuthor(BaseWikiModel):
    """Search author filter entry: a user identity, matched against page owner.

    The wire shape is `{uid, cloud_uid}` and either field alone filters
    (verified live 2026-08-18); when both are present the backend matches on
    `uid`. Requiring at least one here turns a silently-ignored empty object
    into a schema error.
    """

    uid: str | None = Field(
        default=None,
        description="User id, e.g. from user_get_current's identity.uid or a "
        "page owner.",
    )
    cloud_uid: str | None = Field(
        default=None,
        description="Cloud user id — the alternative identifier for Yandex "
        "Cloud organizations.",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "SearchAuthor":
        if self.uid is None and self.cloud_uid is None:
            raise ValueError("provide uid or cloud_uid")
        return self


class SearchResultItem(BaseWikiModel):
    url: str | None = None
    slug: str | None = None
    title: str | None = None
    content: str | None = Field(
        default=None,
        description=(
            "Rendered text excerpt from the page, capped at ~510 characters. "
            "It is NOT the page's content and NOT a summary of it: the excerpt "
            "is a window taken from wherever the match is, which on a long page "
            "can start thousands of characters in. The query terms need not "
            "appear in the excerpt at all, and matches are marked with "
            "<em>…</em> only when the search was called with highlight=true — "
            "so never answer from this field: call page_get with the result's "
            "slug to read the page. Line breaks and tabs inside it are the "
            "source page's own layout (table cells arrive tab-separated), not "
            "separators between excerpts. Empty for type='file' results."
        ),
    )
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


class CursorEnvelope(BaseWikiModel):
    """Cursor-paginated list envelope; subclasses narrow `results`.

    `truncated` is set only by the tool-layer `fetch_all` loop: False when the
    whole list was drained, True when it stopped early — on the item cap, the
    time budget, a failed page, or a cursor the server repeated. `next_cursor`
    then points at the continuation, except after a repeated cursor, where
    there is nothing safe to continue from.
    """

    results: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None
    prev_cursor: str | None = None
    truncated: bool | None = None


class DescendantsResponse(CursorEnvelope):
    results: list[DescendantItem] = Field(default_factory=list)


class CommentsResponse(CursorEnvelope):
    results: list[PageComment] = Field(default_factory=list)


class AttachmentListResponse(CursorEnvelope):
    results: list[WikiAttachment] = Field(default_factory=list)


class ResourcesResponse(CursorEnvelope):
    results: list[WikiResource] = Field(default_factory=list)


class WikiGridPageRef(BaseWikiModel):
    """Service reference, not user data — stays strict."""

    id: int | str | None = None
    slug: str | None = None


class WikiGridSort(DynamicWikiModel):
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


class WikiGridStructure(DynamicWikiModel):
    default_sort: list[WikiGridSort] = Field(default_factory=list)
    columns: list[WikiGridColumn] = Field(default_factory=list)


class WikiGridRow(DynamicWikiModel):
    id: str | int | None = None
    row: list[Any] = Field(default_factory=list)
    pinned: bool | None = None
    color: str | None = None


class WikiGridSummary(BaseWikiModel):
    """Listing envelope, not user data — stays strict."""

    id: str | int
    title: str | None = None
    created_at: str | None = None


class GridsResponse(CursorEnvelope):
    results: list[WikiGridSummary] = Field(default_factory=list)


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
    """Row and column mutations answer with `results` (+ `revision`)."""

    revision: str | None = None
    results: list[WikiGridRow] = Field(default_factory=list)


class GridCellsResponse(BaseWikiModel):
    """`POST /grids/{id}/cells` answers with `cells`, not `results`.

    Its own model rather than a shared one: `results` has a list default, so
    it is never dropped as empty, and a mutation reply carrying
    `"results": []` reads as "nothing changed" to an agent checking it.
    """

    revision: str | None = None
    cells: list[Any] | None = None


class GridDeleteResponse(BaseWikiModel):
    """Acknowledgment for `DELETE /grids/{id}`.

    The endpoint answers 204 No Content (documented and verified live), so
    both fields are filled in client-side: they confirm which grid the
    deletion was applied to. Any JSON object the API starts sending in the
    future still passes through validation, where the contract sweep will
    see it; a non-object body would be dropped in the client instead.
    """

    grid_id: str
    deleted: bool


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


class ClonedPageRef(BaseWikiModel):
    """The clone operation's result: where the copy landed.

    `POST /pages/{id}/clone` is a deferred operation; the client polls it to
    completion and returns this instead of the operation envelope, so callers
    get the useful fact (the new page's id and slug) rather than a status URL.
    """

    id: int
    slug: str


class PageCloneStatusResult(BaseWikiModel):
    page: ClonedPageRef | None = None


class PageCloneStatus(BaseWikiModel):
    """`GET /operations/clone/{id}` envelope, polled until terminal."""

    status: str | None = None
    result: PageCloneStatusResult | None = None


class DeletePageResponse(BaseWikiModel):
    recovery_token: str | None = None


class DeleteCommentResponse(BaseWikiModel):
    """`DELETE /pages/{id}/comments/{cid}` answers 200 with the page's
    updated comment tally (probed 2026-08-11)."""

    comments_count: int | None = None


class AttachmentDeleteResponse(BaseWikiModel):
    """Acknowledgment for `DELETE /pages/{id}/attachments/{fid}`.

    The endpoint answers 204 No Content (documented and verified live
    2026-08-11), so the fields are filled in client-side — same pattern as
    GridDeleteResponse: they confirm which attachment the deletion was
    applied to.
    """

    page_id: int
    file_id: int
    deleted: bool


class PageEditReplacement(BaseWikiModel):
    """One exact-text replacement for page_edit.

    Tool-level model: the API has no partial-edit endpoint (full update and
    append only), so the tool reads the page, applies these in order, and
    writes the result back.
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
    """Compact acknowledgment for page_edit.

    Deliberately not the page object: echoing content back would spend the
    tokens the tool exists to save.
    """

    page_id: int
    slug: str | None = None
    title: str | None = None
    edits_applied: int = Field(description="Number of replacement entries applied.")
    occurrences_replaced: int = Field(
        description="Total occurrences replaced across all entries."
    )
    yfm_warnings: list[str] | None = Field(
        default=None,
        description=(
            "Markup warnings for the resulting content (the write itself "
            "succeeded): parts that will not render as intended on Yandex "
            "Wiki. See the wiki-mcp://yfm-cheatsheet resource for fixes."
        ),
    )


class AttachmentDownloadResult(BaseWikiModel):
    """Tool-level envelope for an attachment downloaded inline.

    Never arrives from the wire — the API streams raw bytes; the tool layer
    picks the representation.
    """

    page_id: int
    file_id: int
    size_bytes: int
    encoding: Literal["utf-8", "base64"]
    content: str = Field(
        description=(
            "The attachment's content: the text itself when encoding is "
            "'utf-8', otherwise the raw bytes base64-encoded."
        )
    )


class UserIdentity(BaseWikiModel):
    uid: str | None = None
    cloud_uid: str | None = None


class UserOrg(BaseWikiModel):
    dir_id: str | None = None
    collab_id: str | None = None


class WikiCurrentUser(BaseWikiModel):
    username: str | None = None
    home_cluster: str | None = Field(
        default=None,
        description=(
            "Slug of the caller's personal section, e.g. 'users/<login>' — "
            "the parent for pages that belong in 'my' space."
        ),
    )
    identity: UserIdentity | None = None
    org: UserOrg | None = None


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
