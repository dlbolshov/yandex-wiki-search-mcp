from typing import Annotated

from pydantic import Field

from mcp_wiki.wiki.proto.types.pages import (
    GridFieldEnum,
    PageFieldEnum,
    ResourceTypeEnum,
)

PageID = Annotated[int, Field(description="Wiki page numeric ID.", gt=0)]
CommentID = Annotated[int, Field(description="Wiki comment numeric ID.", gt=0)]
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
