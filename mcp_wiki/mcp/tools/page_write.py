from typing import Annotated, Any, Literal, TypeAlias

from mcp.server import FastMCP
from mcp.server.fastmcp import Context
from pydantic import Field
from starlette.requests import Request

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.params import (
    CommentID,
    GridID,
    GridRevision,
    PageID,
    PageSlug,
    RecoveryToken,
)
from mcp_wiki.mcp.tools.page_read import _resolve_page_id, _resolve_page_slug
from mcp_wiki.mcp.utils import get_yandex_auth
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    UploadLocation,
    WikiGridPageRef,
)

GridSortMapping: TypeAlias = dict[str, Literal["asc", "desc"]]


def _require_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    return normalized


def _validate_grid_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not cells:
        raise ValueError("cells must not be empty.")

    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ValueError(f"cells[{index}] must be an object.")
        if "row_id" not in cell:
            raise ValueError(f"cells[{index}] must include row_id.")
        if "value" not in cell:
            raise ValueError(f"cells[{index}] must include value.")

        has_column_id = "column_id" in cell
        has_column_slug = "column_slug" in cell
        if has_column_id == has_column_slug:
            raise ValueError(
                f"cells[{index}] must include exactly one of column_id or column_slug."
            )
        if has_column_id and isinstance(cell["column_id"], str):
            _require_non_empty_text(
                cell["column_id"], field_name=f"cells[{index}].column_id"
            )
        if has_column_slug and isinstance(cell["column_slug"], str):
            _require_non_empty_text(
                cell["column_slug"], field_name=f"cells[{index}].column_slug"
            )

    return cells


def _validate_grid_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not columns:
        raise ValueError("columns must not be empty.")

    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            raise ValueError(f"columns[{index}] must be an object.")
        if "title" not in column:
            raise ValueError(f"columns[{index}] must include title.")
        if "slug" not in column:
            raise ValueError(f"columns[{index}] must include slug.")
        if "type" not in column:
            raise ValueError(f"columns[{index}] must include type.")
        if "required" not in column:
            raise ValueError(f"columns[{index}] must include required.")
        if isinstance(column["title"], str):
            _require_non_empty_text(
                column["title"], field_name=f"columns[{index}].title"
            )
        if isinstance(column["slug"], str):
            _require_non_empty_text(column["slug"], field_name=f"columns[{index}].slug")
        if isinstance(column["type"], str):
            _require_non_empty_text(column["type"], field_name=f"columns[{index}].type")

    return columns


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


def _validate_default_sort(
    default_sort: list[GridSortMapping],
) -> list[GridSortMapping]:
    if not default_sort:
        raise ValueError("default_sort must not be empty.")

    normalized: list[GridSortMapping] = []
    for index, item in enumerate(default_sort):
        if not isinstance(item, dict):
            raise ValueError(f"default_sort[{index}] must be an object.")
        if len(item) != 1:
            raise ValueError(
                f"default_sort[{index}] must contain exactly one column slug to direction mapping."
            )
        slug, direction = next(iter(item.items()))
        normalized_slug = _require_non_empty_text(
            str(slug), field_name=f"default_sort[{index}] column slug"
        )
        if direction not in {"asc", "desc"}:
            raise ValueError(
                f"default_sort[{index}] direction must be 'asc' or 'desc'."
            )
        normalized.append({normalized_slug: direction})

    return normalized


def register_page_write_tools(mcp: FastMCP[Any]) -> None:
    @mcp.tool(
        title="Create Wiki Grid",
        description=(
            "Create a Yandex Wiki dynamic table resource on a page. "
            "This changes structured data."
        ),
    )
    async def grid_create(
        ctx: Context[Any, AppContext, Request],
        title: Annotated[
            str,
            Field(description="Grid title. Must be between 1 and 255 characters."),
        ],
        page_id: Annotated[
            PageID | None,
            Field(description="Wiki page numeric ID. Provide either page_id or slug."),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
    ) -> Any:
        resolved_page_id = await _resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await ctx.request_context.lifespan_context.wiki.grid_create(
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
    )
    async def grid_update(
        ctx: Context[Any, AppContext, Request],
        grid_id: GridID,
        revision: GridRevision,
        title: Annotated[
            str | None,
            Field(description="New grid title."),
        ] = None,
        default_sort: Annotated[
            list[GridSortMapping] | None,
            Field(
                description=(
                    "Optional default sort as a list of single-entry mappings from "
                    "column slug to direction, for example [{'status': 'asc'}]."
                )
            ),
        ] = None,
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        normalized_title = (
            _require_non_empty_text(title, field_name="title")
            if title is not None
            else None
        )
        normalized_default_sort = (
            _validate_default_sort(default_sort) if default_sort is not None else []
        )
        if normalized_title is None and not normalized_default_sort:
            raise ValueError("Provide at least one of title or default_sort.")

        return await ctx.request_context.lifespan_context.wiki.grid_update(
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
    )
    async def grid_add_rows(
        ctx: Context[Any, AppContext, Request],
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
            str | None,
            Field(
                description="Optional row ID after which to insert new rows. Mutually exclusive with position."
            ),
        ] = None,
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        if not rows:
            raise ValueError("rows must not be empty.")
        if position is not None and after_row_id is not None:
            raise ValueError("Provide either position or after_row_id, not both.")
        normalized_after_row_id = (
            _require_non_empty_text(after_row_id, field_name="after_row_id")
            if after_row_id is not None
            else None
        )

        return await ctx.request_context.lifespan_context.wiki.grid_add_rows(
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
    )
    async def grid_delete(
        ctx: Context[Any, AppContext, Request],
        grid_id: GridID,
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        return await ctx.request_context.lifespan_context.wiki.grid_delete(
            normalized_grid_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Copy Wiki Grid",
        description=(
            "Copy a Yandex Wiki dynamic table to an existing target page. "
            "This starts an asynchronous operation and returns operation metadata."
        ),
    )
    async def grid_copy(
        ctx: Context[Any, AppContext, Request],
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
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        target_slug = await _resolve_page_slug(ctx, page_id=page_id, slug=slug)
        normalized_title = (
            _require_non_empty_text(title, field_name="title")
            if title is not None
            else None
        )
        return await ctx.request_context.lifespan_context.wiki.grid_copy(
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
    )
    async def grid_update_cells(
        ctx: Context[Any, AppContext, Request],
        grid_id: GridID,
        cells: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Cell patches. Each object must include row_id, value, and exactly one "
                    "of column_id or column_slug."
                )
            ),
        ],
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        return await ctx.request_context.lifespan_context.wiki.grid_update_cells(
            normalized_grid_id,
            cells=_validate_grid_cells(cells),
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Grid Rows",
        description=(
            "Delete rows from a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
    )
    async def grid_delete_rows(
        ctx: Context[Any, AppContext, Request],
        grid_id: GridID,
        revision: GridRevision,
        row_ids: Annotated[
            list[str | int],
            Field(description="Row IDs to delete from the grid."),
        ],
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        return await ctx.request_context.lifespan_context.wiki.grid_delete_rows(
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
    )
    async def grid_add_columns(
        ctx: Context[Any, AppContext, Request],
        grid_id: GridID,
        revision: GridRevision,
        columns: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Columns to add. Each object must include at least title, slug, and type."
                )
            ),
        ],
        position: Annotated[
            int | None,
            Field(
                description="Optional zero-based insertion position for new columns."
            ),
        ] = None,
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")

        return await ctx.request_context.lifespan_context.wiki.grid_add_columns(
            normalized_grid_id,
            revision=normalized_revision,
            columns=_validate_grid_columns(columns),
            position=position,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Grid Columns",
        description=(
            "Delete columns from a Yandex Wiki dynamic table. Fetch the grid first and pass the latest revision. "
            "This changes structured data."
        ),
    )
    async def grid_delete_columns(
        ctx: Context[Any, AppContext, Request],
        grid_id: GridID,
        revision: GridRevision,
        column_slugs: Annotated[
            list[str],
            Field(description="Column slugs to delete from the grid."),
        ],
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        return await ctx.request_context.lifespan_context.wiki.grid_delete_columns(
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
    )
    async def grid_move_rows(
        ctx: Context[Any, AppContext, Request],
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
    ) -> Any:
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
        return await ctx.request_context.lifespan_context.wiki.grid_move_rows(
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
    )
    async def grid_move_columns(
        ctx: Context[Any, AppContext, Request],
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
    ) -> Any:
        normalized_grid_id = _require_non_empty_text(grid_id, field_name="grid_id")
        normalized_revision = _require_non_empty_text(revision, field_name="revision")
        normalized_column_slug = _require_non_empty_text(
            column_slug, field_name="column_slug"
        )
        return await ctx.request_context.lifespan_context.wiki.grid_move_columns(
            normalized_grid_id,
            revision=normalized_revision,
            column_slug=normalized_column_slug,
            position=position,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Create Wiki Page",
        description="Create a Yandex Wiki page.",
    )
    async def page_create(
        ctx: Context[Any, AppContext, Request],
        slug: PageSlug,
        title: Annotated[str, Field(description="Wiki page title.")],
        content: Annotated[str, Field(description="Full page content.")],
        page_type: Annotated[
            str,
            Field(
                description=(
                    "Wiki page type. Prefer 'wysiwyg' unless a different editor type is required."
                )
            ),
        ] = "wysiwyg",
    ) -> Any:
        return await ctx.request_context.lifespan_context.wiki.page_create(
            slug=slug,
            title=title,
            content=content,
            page_type=page_type,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Update Wiki Page",
        description="Update an existing Yandex Wiki page. Content replacement is full-page when content is provided.",
    )
    async def page_update(
        ctx: Context[Any, AppContext, Request],
        page_id: Annotated[
            PageID | None,
            Field(description="Wiki page numeric ID. Provide either page_id or slug."),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
        title: Annotated[str | None, Field(description="New page title.")] = None,
        content: Annotated[
            str | None,
            Field(description="New full page content. Replaces the existing body."),
        ] = None,
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
    ) -> Any:
        resolved_page_id = await _resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await ctx.request_context.lifespan_context.wiki.page_update(
            resolved_page_id,
            title=title,
            content=content,
            allow_merge=allow_merge,
            is_silent=is_silent,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Append Wiki Content",
        description="Append content to the top, bottom, or anchor of a Yandex Wiki page.",
    )
    async def page_append_content(
        ctx: Context[Any, AppContext, Request],
        content: Annotated[str, Field(description="Content block to append.")],
        page_id: Annotated[
            PageID | None,
            Field(description="Wiki page numeric ID. Provide either page_id or slug."),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
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
    ) -> Any:
        resolved_page_id = await _resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await ctx.request_context.lifespan_context.wiki.page_append_content(
            resolved_page_id,
            content=content,
            location=location,
            anchor=anchor,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Add Page Comment",
        description="Add a comment to a Yandex Wiki page.",
    )
    async def page_add_comment(
        ctx: Context[Any, AppContext, Request],
        body: Annotated[str, Field(description="Comment body.")],
        page_id: Annotated[
            PageID | None,
            Field(description="Wiki page numeric ID. Provide either page_id or slug."),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
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
    ) -> Any:
        resolved_page_id = await _resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await ctx.request_context.lifespan_context.wiki.page_add_comment(
            resolved_page_id,
            body=body,
            parent_id=parent_id,
            thread_id=thread_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Delete Wiki Page",
        description="Delete a Yandex Wiki page and return a recovery token.",
    )
    async def page_delete(
        ctx: Context[Any, AppContext, Request],
        page_id: Annotated[
            PageID | None,
            Field(description="Wiki page numeric ID. Provide either page_id or slug."),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
    ) -> Any:
        resolved_page_id = await _resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await ctx.request_context.lifespan_context.wiki.page_delete(
            resolved_page_id,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Recover Wiki Page",
        description="Recover a deleted Yandex Wiki page using a recovery token.",
    )
    async def page_recover(
        ctx: Context[Any, AppContext, Request],
        recovery_token: RecoveryToken,
    ) -> Any:
        return await ctx.request_context.lifespan_context.wiki.page_recover(
            recovery_token,
            auth=get_yandex_auth(ctx),
        )

    @mcp.tool(
        title="Upload Page Attachment",
        description="Upload a local file to Yandex Wiki and attach it to a page.",
    )
    async def page_upload_attachment(
        ctx: Context[Any, AppContext, Request],
        file_path: Annotated[
            str,
            Field(
                description="Local filesystem path to the file that should be uploaded."
            ),
        ],
        page_id: Annotated[
            PageID | None,
            Field(description="Wiki page numeric ID. Provide either page_id or slug."),
        ] = None,
        slug: Annotated[
            PageSlug | None,
            Field(
                description="Wiki page slug or full Wiki URL. Provide either page_id or slug."
            ),
        ] = None,
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
    ) -> Any:
        resolved_page_id = await _resolve_page_id(ctx, page_id=page_id, slug=slug)
        return await ctx.request_context.lifespan_context.wiki.page_upload_attachment(
            resolved_page_id,
            file_path=file_path,
            append_markup=append_markup,
            append_location=append_location,
            auth=get_yandex_auth(ctx),
        )
