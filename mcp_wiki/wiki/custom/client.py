import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Literal

from aiohttp import (
    ClientSession,
    ClientTimeout,
    TraceConfig,
    TraceRequestEndParams,
    TraceRequestExceptionParams,
    TraceRequestStartParams,
)

from mcp_wiki.mcp.utils import normalize_slug
from mcp_wiki.wiki.custom.anchors import append_content_to_anchor_source
from mcp_wiki.wiki.custom.errors import (
    GridNotFound,
    PageNotFound,
    WikiApiError,
    WikiError,
    build_api_error,
)
from mcp_wiki.wiki.proto.common import YandexAuth
from mcp_wiki.wiki.proto.pages import WikiProtocol
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

SEARCH_PAGE_SIZE_MAX = 50

logger = logging.getLogger(__name__)


def _open_binary(path: Path) -> BinaryIO:
    return path.open("rb")


def _build_trace_config() -> TraceConfig:
    async def on_request_start(
        _session: ClientSession,
        ctx: SimpleNamespace,
        _params: TraceRequestStartParams,
    ) -> None:
        ctx.start_time = asyncio.get_running_loop().time()

    async def on_request_end(
        _session: ClientSession,
        ctx: SimpleNamespace,
        params: TraceRequestEndParams,
    ) -> None:
        elapsed_ms = (asyncio.get_running_loop().time() - ctx.start_time) * 1000
        logger.debug(
            "%s %s -> %s (%.0f ms)",
            params.method,
            params.url.path,
            params.response.status,
            elapsed_ms,
        )

    async def on_request_exception(
        _session: ClientSession,
        ctx: SimpleNamespace,
        params: TraceRequestExceptionParams,
    ) -> None:
        elapsed_ms = (asyncio.get_running_loop().time() - ctx.start_time) * 1000
        logger.debug(
            "%s %s -> %r (%.0f ms)",
            params.method,
            params.url.path,
            params.exception,
            elapsed_ms,
        )

    trace_config = TraceConfig()
    trace_config.on_request_start.append(on_request_start)
    trace_config.on_request_end.append(on_request_end)
    trace_config.on_request_exception.append(on_request_exception)
    return trace_config


class WikiClient(WikiProtocol):
    CHUNK_SIZE = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        token: str | None,
        iam_token: str | None = None,
        auth_scheme: Literal["OAuth", "Bearer"] = "OAuth",
        org_id: str | None = None,
        cloud_org_id: str | None = None,
        base_url: str = "https://api.wiki.yandex.net",
        timeout: float = 30,
        upload_timeout: float = 300,
    ):
        self._token = token
        self._iam_token = iam_token
        self._auth_scheme = auth_scheme
        self._org_id = org_id
        self._cloud_org_id = cloud_org_id
        self._base_url = base_url
        self._timeout = ClientTimeout(total=timeout)
        self._upload_timeout = ClientTimeout(total=upload_timeout)
        self._session: ClientSession | None = None

    async def prepare(self) -> None:
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                base_url=self._base_url,
                timeout=self._timeout,
                trace_configs=[_build_trace_config()],
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "WikiClient":
        await self.prepare()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def _http(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError(
                "WikiClient is not prepared. "
                "Call prepare() or use 'async with WikiClient(...)'."
            )
        return self._session

    def _build_headers(self, auth: YandexAuth | None = None) -> dict[str, str]:
        if auth and auth.token:
            auth_header = f"{self._auth_scheme} {auth.token}"
        elif self._token:
            auth_header = f"{self._auth_scheme} {self._token}"
        elif self._iam_token:
            auth_header = f"Bearer {self._iam_token}"
        else:
            raise ValueError(
                "No authentication method provided. Configure wiki_token, wiki_iam_token, or OAuth."
            )

        org_id = auth.org_id if auth and auth.org_id else self._org_id
        cloud_org_id = (
            auth.cloud_org_id if auth and auth.cloud_org_id else self._cloud_org_id
        )

        if org_id and cloud_org_id:
            raise ValueError("Only one of org_id or cloud_org_id should be provided.")
        if not org_id and not cloud_org_id:
            raise ValueError("Either org_id or cloud_org_id must be provided.")

        headers = {"Authorization": auth_header}
        if org_id:
            headers["X-Org-Id"] = org_id
        if cloud_org_id:
            headers["X-Cloud-Org-Id"] = cloud_org_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: YandexAuth | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        content_type: str | None = None,
        not_found: Callable[[], WikiError] | None = None,
        timeout: ClientTimeout | None = None,
    ) -> bytes:
        headers = self._build_headers(auth)
        if content_type:
            headers["Content-Type"] = content_type

        kwargs: dict[str, Any] = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if timeout is not None:
            kwargs["timeout"] = timeout

        async with self._http.request(method, path, **kwargs) as response:
            payload = await response.read()
            if response.status == 404 and not_found is not None:
                raise not_found()
            if response.status >= 400:
                raise build_api_error(response.status, payload)
            return payload

    @staticmethod
    def _json_or_empty(payload: bytes) -> Any:
        if not payload:
            return {}
        return json.loads(payload)

    async def page_get_by_slug(
        self,
        slug: str,
        *,
        fields: list[str] | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        normalized_slug = normalize_slug(slug)
        params: dict[str, Any] = {"slug": normalized_slug}
        if fields:
            params["fields"] = ",".join(fields)

        payload = await self._request(
            "GET",
            "v1/pages",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(normalized_slug),
        )
        return WikiPage.model_validate_json(payload)

    async def page_get(
        self,
        page_id: int,
        *,
        fields: list[str] | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}",
            params=params if params else None,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return WikiPage.model_validate_json(payload)

    async def page_search(
        self,
        query: str,
        *,
        page_size: int = 10,
        auth: YandexAuth | None = None,
    ) -> SearchResponse:
        body = {
            "query": query,
            "page_size": max(1, min(page_size, SEARCH_PAGE_SIZE_MAX)),
        }
        payload = await self._request("POST", "v1/search", json_body=body, auth=auth)
        return SearchResponse.model_validate_json(payload)

    async def page_get_descendants(
        self,
        slug: str,
        *,
        include_self: bool = False,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> DescendantsResponse:
        normalized_slug = normalize_slug(slug)
        params: dict[str, Any] = {
            "slug": normalized_slug,
            "include_self": str(include_self).lower(),
            "page_size": page_size,
        }
        if cursor:
            params["cursor"] = cursor

        payload = await self._request(
            "GET",
            "v1/pages/descendants",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(normalized_slug),
        )
        return DescendantsResponse.model_validate_json(payload)

    async def page_get_comments(
        self,
        page_id: int,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> CommentsResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/comments",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return CommentsResponse.model_validate_json(payload)

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
    ) -> ResourcesResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if resource_types:
            params["types"] = ",".join(resource_types)
        if q:
            params["q"] = q
        if cursor:
            params["cursor"] = cursor
        if order_by:
            params["order_by"] = order_by
        if order_direction:
            params["order_direction"] = order_direction

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/resources",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return ResourcesResponse.model_validate_json(payload)

    async def page_get_grids(
        self,
        page_id: int,
        *,
        page_size: int = 50,
        cursor: str | None = None,
        order_by: str | None = None,
        order_direction: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridsResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        if order_by:
            params["order_by"] = order_by
        if order_direction:
            params["order_direction"] = order_direction

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/grids",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return GridsResponse.model_validate_json(payload)

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
    ) -> WikiGrid:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if filter:
            params["filter"] = filter
        if only_cols:
            params["only_cols"] = only_cols
        if only_rows:
            params["only_rows"] = only_rows
        if revision:
            params["revision"] = revision
        if sort:
            params["sort"] = sort

        payload = await self._request(
            "GET",
            f"v1/grids/{grid_id}",
            params=params if params else None,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return WikiGrid.model_validate_json(payload)

    async def grid_create(
        self,
        *,
        request: GridCreateRequest,
        auth: YandexAuth | None = None,
    ) -> WikiGrid:
        payload = await self._request(
            "POST",
            "v1/grids",
            json_body=request.model_dump(exclude_none=True),
            auth=auth,
        )
        return WikiGrid.model_validate_json(payload)

    async def grid_update(
        self,
        grid_id: str,
        *,
        request: GridUpdateRequest,
        auth: YandexAuth | None = None,
    ) -> GridUpdateResponse:
        body = request.model_dump(exclude_none=True)
        if not request.default_sort:
            body.pop("default_sort", None)

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridUpdateResponse.model_validate_json(payload)

    async def grid_add_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        rows: list[dict[str, Any]],
        position: int | None = None,
        after_row_id: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        body: dict[str, Any] = {
            "revision": revision,
            "rows": rows,
        }
        if position is not None:
            body["position"] = position
        if after_row_id is not None:
            body["after_row_id"] = after_row_id

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/rows",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate_json(payload)

    async def grid_delete(
        self,
        grid_id: str,
        *,
        auth: YandexAuth | None = None,
    ) -> dict[str, Any]:
        payload = await self._request(
            "DELETE",
            f"v1/grids/{grid_id}",
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return self._json_or_empty(payload)

    async def grid_copy(
        self,
        grid_id: str,
        *,
        target: str,
        title: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridOperationResponse:
        body: dict[str, Any] = {"target": target}
        if title is not None:
            body["title"] = title

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/clone",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridOperationResponse.model_validate(self._json_or_empty(payload))

    async def grid_update_cells(
        self,
        grid_id: str,
        *,
        cells: list[dict[str, Any]],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/cells",
            json_body={"cells": cells},
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_delete_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        row_ids: list[str],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "DELETE",
            f"v1/grids/{grid_id}/rows",
            json_body={"revision": revision, "row_ids": row_ids},
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_add_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        columns: list[dict[str, Any]],
        position: int | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        body: dict[str, Any] = {
            "revision": revision,
            "columns": columns,
        }
        if position is not None:
            body["position"] = position

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/columns",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_delete_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        column_slugs: list[str],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "DELETE",
            f"v1/grids/{grid_id}/columns",
            json_body={"revision": revision, "column_slugs": column_slugs},
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_move_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        row_id: str,
        position: int | None = None,
        after_row_id: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        body: dict[str, Any] = {
            "revision": revision,
            "row_id": row_id,
        }
        if position is not None:
            body["position"] = position
        if after_row_id is not None:
            body["after_row_id"] = after_row_id

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/rows/move",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_move_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        column_slug: str,
        position: int,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/columns/move",
            json_body={
                "revision": revision,
                "column_slug": column_slug,
                "position": position,
            },
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def page_get_attachments(
        self,
        page_id: int,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> AttachmentListResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/attachments",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return AttachmentListResponse.model_validate_json(payload)

    async def page_create(
        self,
        *,
        slug: str,
        title: str,
        content: str,
        page_type: str = "wysiwyg",
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        body = {
            "slug": normalize_slug(slug),
            "title": title,
            "content": content,
            "page_type": page_type,
        }
        payload = await self._request("POST", "v1/pages", json_body=body, auth=auth)
        return WikiPage.model_validate_json(payload)

    async def page_update(
        self,
        page_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        allow_merge: bool = False,
        is_silent: bool = False,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        if title is None and content is None:
            raise ValueError("Provide at least one of title or content.")

        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if content is not None:
            body["content"] = content

        params: dict[str, Any] = {}
        if allow_merge:
            params["allow_merge"] = "true"
        if is_silent:
            params["is_silent"] = "true"

        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}",
            params=params if params else None,
            json_body=body,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return WikiPage.model_validate_json(payload)

    async def page_append_content(
        self,
        page_id: int,
        *,
        content: str,
        location: UploadLocation = "bottom",
        anchor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if anchor:
            body["anchor"] = {"name": anchor}
        else:
            body["body"] = {"location": location}

        try:
            payload = await self._request(
                "POST",
                f"v1/pages/{page_id}/append-content",
                json_body=body,
                auth=auth,
                not_found=lambda: PageNotFound(page_id),
            )
        except WikiApiError as exc:
            if not (
                anchor and exc.status == 400 and exc.error_code == "ANCHOR_NOT_FOUND"
            ):
                raise
            page = await self.page_get(page_id, fields=["content"], auth=auth)
            if isinstance(page.content, str):
                updated_content = append_content_to_anchor_source(
                    page.content,
                    appended_content=content,
                    anchor=anchor,
                )
                if updated_content is not None:
                    updated_page = await self.page_update(
                        page_id,
                        content=updated_content,
                        allow_merge=True,
                        auth=auth,
                    )
                    return json.loads(updated_page.model_dump_json())
            raise
        return self._json_or_empty(payload)

    async def page_add_comment(
        self,
        page_id: int,
        *,
        body: str,
        parent_id: int | None = None,
        thread_id: int | None = None,
        auth: YandexAuth | None = None,
    ) -> PageComment:
        request_body: dict[str, Any] = {"body": body}
        if parent_id is not None:
            request_body["parent_id"] = parent_id
        if thread_id is not None:
            request_body["thread_id"] = thread_id

        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}/comments",
            json_body=request_body,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return PageComment.model_validate_json(payload)

    async def page_delete(
        self,
        page_id: int,
        *,
        auth: YandexAuth | None = None,
    ) -> DeletePageResponse:
        payload = await self._request(
            "DELETE",
            f"v1/pages/{page_id}",
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return DeletePageResponse.model_validate_json(payload)

    async def page_recover(
        self,
        recovery_token: str,
        *,
        auth: YandexAuth | None = None,
    ) -> RecoverPageResponse:
        payload = await self._request(
            "POST",
            f"v1/recovery_tokens/{recovery_token}/recover",
            auth=auth,
        )
        return RecoverPageResponse.model_validate_json(payload)

    async def upload_session_create(
        self,
        *,
        file_name: str,
        file_size: int,
        auth: YandexAuth | None = None,
    ) -> UploadSessionResponse:
        payload = await self._request(
            "POST",
            "v1/upload_sessions",
            json_body={"file_name": file_name, "file_size": file_size},
            auth=auth,
        )
        return UploadSessionResponse.model_validate_json(payload)

    async def _upload_part(
        self,
        session_id: str,
        *,
        part_number: int,
        data: bytes,
        auth: YandexAuth | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"v1/upload_sessions/{session_id}/upload_part",
            params={"part_number": part_number},
            data=data,
            content_type="application/octet-stream",
            auth=auth,
            timeout=self._upload_timeout,
        )

    async def _finish_upload_session(
        self,
        session_id: str,
        *,
        auth: YandexAuth | None = None,
    ) -> None:
        await self._request(
            "POST",
            f"v1/upload_sessions/{session_id}/finish",
            auth=auth,
            timeout=self._upload_timeout,
        )

    async def page_attach_upload_sessions(
        self,
        page_id: int,
        *,
        session_ids: list[str],
        auth: YandexAuth | None = None,
    ) -> AttachmentResultsResponse:
        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}/attachments",
            json_body={"upload_sessions": session_ids},
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return AttachmentResultsResponse.model_validate_json(payload)

    async def page_upload_attachment(
        self,
        page_id: int,
        *,
        file_path: str,
        append_markup: bool = False,
        append_location: UploadLocation = "bottom",
        auth: YandexAuth | None = None,
    ) -> UploadAttachmentResult:
        path = Path(file_path)
        if not await asyncio.to_thread(path.is_file):
            raise FileNotFoundError(f"File not found: {file_path}")

        stat_result = await asyncio.to_thread(path.stat)
        upload_session = await self.upload_session_create(
            file_name=path.name,
            file_size=stat_result.st_size,
            auth=auth,
        )

        handle = await asyncio.to_thread(_open_binary, path)
        try:
            part_number = 1
            while True:
                chunk = await asyncio.to_thread(handle.read, self.CHUNK_SIZE)
                if not chunk:
                    break
                await self._upload_part(
                    upload_session.session_id,
                    part_number=part_number,
                    data=chunk,
                    auth=auth,
                )
                part_number += 1
        finally:
            await asyncio.to_thread(handle.close)

        await self._finish_upload_session(upload_session.session_id, auth=auth)
        attachment_result = await self.page_attach_upload_sessions(
            page_id,
            session_ids=[upload_session.session_id],
            auth=auth,
        )

        appended_content: str | None = None
        if append_markup and attachment_result.results:
            first_attachment = attachment_result.results[0]
            appended_content = f'{{% file src="{first_attachment.download_url}" name="{first_attachment.name}" %}}'
            await self.page_append_content(
                page_id,
                content=appended_content,
                location=append_location,
                auth=auth,
            )

        return UploadAttachmentResult(
            page_id=page_id,
            attachments=attachment_result.results,
            appended_markup=append_markup,
            appended_content=appended_content,
        )
