from typing import Any, Protocol

from mcp_wiki.wiki.proto.common import YandexAuth
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentListResponse,
    AttachmentResultsResponse,
    CommentsResponse,
    DeletePageResponse,
    DescendantsResponse,
    GridCreateRequest,
    GridMutationResponse,
    GridOperationResponse,
    GridsResponse,
    GridUpdateRequest,
    GridUpdateResponse,
    PageComment,
    RecoverPageResponse,
    ResourcesResponse,
    SearchResponse,
    UploadAttachmentResult,
    UploadLocation,
    UploadSessionResponse,
    WikiGrid,
    WikiPage,
)


class WikiProtocol(Protocol):
    async def prepare(self) -> None: ...
    async def close(self) -> None: ...

    async def page_get_by_slug(
        self,
        slug: str,
        *,
        fields: list[str] | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage: ...

    async def page_get(
        self,
        page_id: int,
        *,
        fields: list[str] | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage: ...

    async def page_search(
        self,
        query: str,
        *,
        page_size: int = 10,
        auth: YandexAuth | None = None,
    ) -> SearchResponse: ...

    async def page_get_descendants(
        self,
        slug: str,
        *,
        include_self: bool = False,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> DescendantsResponse: ...

    async def page_get_comments(
        self,
        page_id: int,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> CommentsResponse: ...

    async def page_get_resources(
        self,
        page_id: int,
        *,
        resource_types: list[str] | None = None,
        q: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
        order_by: str | None = None,
        order_direction: str | None = None,
        auth: YandexAuth | None = None,
    ) -> ResourcesResponse: ...

    async def page_get_grids(
        self,
        page_id: int,
        *,
        page_size: int = 50,
        cursor: str | None = None,
        order_by: str | None = None,
        order_direction: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridsResponse: ...

    async def grid_get(
        self,
        grid_id: str,
        *,
        fields: list[str] | None = None,
        filter: str | None = None,
        only_cols: str | None = None,
        only_rows: str | None = None,
        revision: str | None = None,
        sort: str | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiGrid: ...

    async def grid_create(
        self,
        *,
        request: GridCreateRequest,
        auth: YandexAuth | None = None,
    ) -> WikiGrid: ...

    async def grid_update(
        self,
        grid_id: str,
        *,
        request: GridUpdateRequest,
        auth: YandexAuth | None = None,
    ) -> GridUpdateResponse: ...

    async def grid_add_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        rows: list[dict[str, Any]],
        position: int | None = None,
        after_row_id: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def grid_delete(
        self,
        grid_id: str,
        *,
        auth: YandexAuth | None = None,
    ) -> dict[str, Any]: ...

    async def grid_copy(
        self,
        grid_id: str,
        *,
        target: str,
        title: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridOperationResponse: ...

    async def grid_update_cells(
        self,
        grid_id: str,
        *,
        cells: list[dict[str, Any]],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def grid_delete_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        row_ids: list[str],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def grid_add_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        columns: list[dict[str, Any]],
        position: int | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def grid_delete_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        column_slugs: list[str],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def grid_move_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        row_id: str,
        position: int | None = None,
        after_row_id: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def grid_move_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        column_slug: str,
        position: int,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse: ...

    async def page_get_attachments(
        self,
        page_id: int,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> AttachmentListResponse: ...

    async def page_create(
        self,
        *,
        slug: str,
        title: str,
        content: str,
        page_type: str = "wysiwyg",
        auth: YandexAuth | None = None,
    ) -> WikiPage: ...

    async def page_update(
        self,
        page_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        allow_merge: bool = False,
        is_silent: bool = False,
        auth: YandexAuth | None = None,
    ) -> WikiPage: ...

    async def page_append_content(
        self,
        page_id: int,
        *,
        content: str,
        location: UploadLocation = "bottom",
        anchor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> dict[str, Any]: ...

    async def page_add_comment(
        self,
        page_id: int,
        *,
        body: str,
        parent_id: int | None = None,
        thread_id: int | None = None,
        auth: YandexAuth | None = None,
    ) -> PageComment: ...

    async def page_delete(
        self,
        page_id: int,
        *,
        auth: YandexAuth | None = None,
    ) -> DeletePageResponse: ...

    async def page_recover(
        self,
        recovery_token: str,
        *,
        auth: YandexAuth | None = None,
    ) -> RecoverPageResponse: ...

    async def upload_session_create(
        self,
        *,
        file_name: str,
        file_size: int,
        auth: YandexAuth | None = None,
    ) -> UploadSessionResponse: ...

    async def page_attach_upload_sessions(
        self,
        page_id: int,
        *,
        session_ids: list[str],
        auth: YandexAuth | None = None,
    ) -> AttachmentResultsResponse: ...

    async def page_upload_attachment(
        self,
        page_id: int,
        *,
        file_path: str,
        append_markup: bool = False,
        append_location: UploadLocation = "bottom",
        auth: YandexAuth | None = None,
    ) -> UploadAttachmentResult: ...
