# CLAUDE.md

This file provides guidance for working on the `yandex-wiki-search-mcp` package.

## Project Overview

`yandex-wiki-search-mcp` is an MCP server for the public Yandex Wiki API with full-text search.
It exposes Wiki-oriented tools through FastMCP and keeps the code organized around a dedicated Wiki domain model.

Main capabilities:
- full-text search across the whole Wiki
- read pages by `page_id` or `slug`
- fetch descendants for page trees
- read comments, resources, and attachments
- create and update pages
- append content to pages
- delete and recover pages
- upload local files through Wiki upload sessions and attach them to pages

## Commands

```bash
task              # Run lock, format, checks, and tests
task format       # Format code with Ruff and fix imports
task check        # Run Ruff, format check, mypy, and ty
task test         # Run pytest
task test-cov     # Run tests with HTML coverage report
uv sync --dev     # Install dependencies
uv run yandex-wiki-search-mcp  # Run the server locally
```

Before finalizing substantial code changes, run at least:

```bash
task format
task test
```

## Architecture

- `mcp_wiki/settings.py`
  Pydantic settings sourced from environment variables.
  Main runtime env vars are `WIKI_TOKEN` or `WIKI_IAM_TOKEN`, plus exactly one of `WIKI_ORG_ID` or `WIKI_CLOUD_ORG_ID`.

- `mcp_wiki/wiki/custom/client.py`
  Async HTTP client for Yandex Wiki API.
  Implements the domain operations and handles auth headers, page resolution, upload sessions, and attachment flow.

- `mcp_wiki/wiki/proto/`
  Protocol and Pydantic response models for the Wiki domain.
  `pages.py` defines the `WikiProtocol`.

- `mcp_wiki/mcp/server.py`
  FastMCP server creation, lifespan wiring, optional OAuth provider registration, and resource/tool registration.

- `mcp_wiki/mcp/resources.py`
  Configuration resource exposed as `wiki-mcp://configuration`.

- `mcp_wiki/mcp/tools/page_read.py`
  Read-only Wiki tools.

- `mcp_wiki/mcp/tools/page_write.py`
  Write tools. These are registered only when `settings.wiki_read_only == False`.

- `mcp_wiki/mcp/oauth/`
  Optional OAuth provider/store implementation reused for MCP auth flows.

## Tool Inventory

Read-only tools:
- `page_search`
- `page_get`
- `page_get_descendants`
- `page_get_comments`
- `page_get_resources`
- `page_get_attachments`
- `page_get_grids`
- `grid_get`

Write tools:
- `page_create`
- `page_update`
- `page_append_content`
- `page_add_comment`
- `page_delete`
- `page_recover`
- `page_upload_attachment`
- `grid_create`
- `grid_update`
- `grid_delete`
- `grid_copy`
- `grid_add_rows`
- `grid_delete_rows`
- `grid_update_cells`
- `grid_add_columns`
- `grid_delete_columns`
- `grid_move_rows`
- `grid_move_columns`

## Testing

### General rules

- Use `pytest` with async tests.
- Use `aioresponses` for Wiki HTTP client tests.
- Use `AsyncMock` for MCP tool tests through a real `FastMCP` server.
- Keep imports at module top level.
- Prefer explicit fixtures and narrow assertions.

### Test layout

- `tests/wiki/custom/test_client.py`
  HTTP-level tests for `WikiClient`.

- `tests/mcp/server/test_server_creation.py`
  Tool/resource registration and server metadata checks.

- `tests/mcp/resources/test_configuration.py`
  Configuration resource checks.

- `tests/mcp/tools/test_page_read_tools.py`
  Read-tool behavior against mocked Wiki protocol.

- `tests/mcp/tools/test_page_write_tools.py`
  Write-tool behavior against mocked Wiki protocol.

### MCP tool tests

Use `client_session.call_tool(...)` and extract output with `get_tool_result_content(...)` from `tests/mcp/conftest.py`.

When a tool can accept both `page_id` and `slug`, test at least one of:
- direct `page_id` path
- slug resolution path through `page_get_by_slug`

### Wiki client tests

Use `aioresponses` and validate:
- auth headers
- organization headers
- query params
- body payloads
- response parsing

For upload-related tests, mock the whole sequence:
1. create upload session
2. upload part(s)
3. finish upload session
4. attach upload session to page
5. optional append of file macro markup

## Adding Or Changing Tools

When adding a new MCP tool:

1. Extend `WikiProtocol` in `mcp_wiki/wiki/proto/pages.py` if needed.
2. Add or update response models in `mcp_wiki/wiki/proto/types/pages.py` if needed.
3. Implement the HTTP method in `mcp_wiki/wiki/custom/client.py`.
4. Register the tool in:
   - `mcp_wiki/mcp/tools/page_read.py` for read-only operations
   - `mcp_wiki/mcp/tools/page_write.py` for write operations
5. Update:
   - `README.md`
   - `README_ru.md`
   - `manifest.json`
   - `CHANGELOG.md`
6. Add tests in the matching `tests/mcp/...` or `tests/wiki/...` location.

## Configuration Notes

Authentication:
- `WIKI_TOKEN` for OAuth token auth
- `WIKI_IAM_TOKEN` for IAM token auth

Organization routing:
- `WIKI_ORG_ID`
- `WIKI_CLOUD_ORG_ID`

Optional:
- `WIKI_AUTH_SCHEME` with `OAuth` default
- `WIKI_API_BASE_URL` with `https://api.wiki.yandex.net` default
- `WIKI_READ_ONLY=true` to disable write tool registration
- `OAUTH_ENABLED=true` to run the MCP OAuth provider flow

Constraints:
- exactly one of `WIKI_ORG_ID` and `WIKI_CLOUD_ORG_ID`
- if `WIKI_READ_ONLY=true`, write tools must not be registered
- `page_update` replaces full content when `content` is provided
- file upload uses Yandex Wiki multipart upload sessions
