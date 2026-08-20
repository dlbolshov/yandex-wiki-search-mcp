# CLAUDE.md

This file provides guidance for working on the `yandex-wiki-search-mcp` package.

## Project Overview

`yandex-wiki-search-mcp` is an MCP server for the public Yandex Wiki API with full-text search.
It exposes Wiki-oriented tools through the MCP Python SDK's `MCPServer` and keeps the code organized around a dedicated Wiki domain model.

The SDK is pinned to `mcp[cli]>=2,<3`. One v2 server answers both protocol eras — every handshake revision back to `2024-11-05` plus the modern `2026-07-28` — so nothing here needs a per-era branch.

Main capabilities:
- full-text search across the whole Wiki, with server-side filters (section, type, author, date interval) and optional `<em>` highlighting
- read pages by `page_id` or `slug`
- fetch descendants for page trees
- read comments, resources, and attachments; read an attachment into the conversation (`page_read_attachment` — images as a native image block, everything else as text or a base64 blob) or stream it to a local file (`page_download_attachment`)
- look up the calling user (`/users/me`): username, personal-section slug, identity ids
- create and update pages, including setting and clearing redirects
- edit page content by exact-text replacements, without resending the whole page
- append content to pages
- delete and recover pages; delete comments and attachments
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

The full developer guide — layers, code map, design decisions, testing seams, CI and the release process — lives in `docs/architecture.md` (EN) and `docs/architecture_ru.md` (RU). **When a change alters anything described there (structure, tool surface, contracts, CI, release flow), update both files in the same change.**

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
  `MCPServer` creation, lifespan wiring, optional OAuth provider registration, and resource/tool registration.
  Also `run_options()` / `http_app_options()`, which assemble the transport keywords `run()` and `streamable_http_app()` take — these are no longer constructor arguments.
  **Always pass `host`**: the SDK defaults it to `127.0.0.1` and auto-arms DNS rebinding protection on loopback, so an app built without it answers every MCP request behind a real hostname with `421` while `/healthz` still returns `200`.

- `mcp_wiki/mcp/middleware.py`
  DEBUG log of every inbound message with the time spent serving it, so it subtracts against the Wiki client's own per-request timings.
  Silent at the default `LOG_LEVEL=INFO`; a per-request line at INFO would stall a stdio server whose client does not drain stderr.

- `mcp_wiki/mcp/request_ctx.py`
  Contextvar holding the transport request of the message being handled, published by a `Server.middleware` entry.
  Exists because the SDK injects no `Context` into a static-URI resource, which `wiki-mcp://configuration` is.
  `Server.middleware` is provisional upstream, so this module is deliberately the only place that touches it.

- `mcp_wiki/mcp/resources.py`
  Configuration resource exposed as `wiki-mcp://configuration`, plus the YFM cheat sheet.
  Neither handler takes a `Context` — a static URI paired with one raises at registration.

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
- `page_read_attachment`
- `user_get_current`
- `grid_get`

Write tools:
- `page_create`
- `page_update`
- `page_edit`
- `page_append_content`
- `page_clone`
- `page_add_comment`
- `page_delete_comment`
- `page_delete`
- `page_recover`
- `page_upload_attachment`
- `page_download_attachment`
- `page_delete_attachment`
- `grid_create`
- `grid_update`
- `grid_delete`
- `grid_copy`
- `grid_add_rows`
- `grid_delete_rows`
- `grid_update_cells`
- `grid_add_columns`
- `grid_delete_columns`
- `grid_move_row`
- `grid_move_column`

`manifest.json` lists the same set; a registration test asserts the two stay in sync.

## Testing

### General rules

- Use `pytest` with async tests.
- Use `aioresponses` for Wiki HTTP client tests.
- Use `AsyncMock` for MCP tool tests through a real `MCPServer`.
- Keep imports at module top level.
- Prefer explicit fixtures and narrow assertions.
- Protocol model fields are snake_case: `is_error`, `structured_content`, `input_schema`, `server_info`, and the `ToolAnnotations` hints. camelCase still works as a constructor kwarg but not for attribute access.

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

- `tests/mcp/server/test_request_ctx.py`
  The middleware that publishes the inbound request, covered over real HTTP.
  This is the guard on a provisional SDK API — keep it end-to-end rather than calling the handler directly.

### MCP tool tests

Use the `client` fixture (an in-memory `Client`, the mcp 2.x replacement for the removed `create_connected_server_and_client_session`): `client.call_tool(...)`, then extract output with `get_tool_result_content(...)` from `tests/mcp/conftest.py`.

The fixture is left on the SDK default `mode="auto"`, so the suite runs against the `2026-07-28` path a modern client negotiates. Nothing here needs a back-channel; a tool that elicits, samples, or lists roots would need `mode="legacy"`.

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
   - `docs/architecture.md` + `docs/architecture_ru.md` (if the change touches anything they describe)
6. Add tests in the matching `tests/mcp/...` or `tests/wiki/...` location.

## Versioning And Release Flow

**Never bump the version in a feature branch.** Feature work lands under
`## [Unreleased]` in `CHANGELOG.md`; version numbers are assigned by a
dedicated release commit on `main` (`Release vX.Y.Z`) that updates everything
at once: `pyproject.toml`, `uv.lock` (via `uv lock`, never by hand),
`manifest.json`, `server.json` (both `version` fields **and** the OCI image
tag), promotes `[Unreleased]` to `[X.Y.Z] - date`, and appends the ROADMAP
log entry. `scripts/check_versions.py` verifies the sync (except the OCI
tag). Full procedure: `docs/architecture.md` → "Release process". Do not
reference the future version number from docs, ROADMAP entries, or code
comments before that commit exists — the number is not decided until release.

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
- `HOST` must reach `run()` / `streamable_http_app()`, or HTTP transports answer `421` behind any non-loopback hostname
- `GET /pages/descendants?slug=` **empty means the whole organization** — a real contract, not bad input (an unresolvable slug 404s). `page_get_descendants(from_root=true)` is the only caller that sends it; do not add an "empty slug" guard to `WikiClient.page_get_descendants` or to the params model, and note that omitting the parameter entirely is a `400`. `resolve_page_locator` still rejects the empty slug for every other tool, where the API genuinely needs a page
- `page_search` results carry a ~510-character excerpt in `content`, cut from wherever the match sits — not the page and not a summary. Matches inside it are wrapped in `<em>` tags only when the call passed `highlight=true`; otherwise the excerpt is unmarked. Its `\n`/`\t` are the source page's layout, not fragment separators. This is documented in four places that must stay in sync: the field description in `wiki/proto/types/pages.py`, the tool description, `build_instructions()`, and this bullet
- `page_search`'s `slug_prefix` reaches the API as `filters.cluster`, which is the strictest slug on the wire: it matches the stored slug **literally**, so mixed case, a leading slash or a trailing slash each answer 200 with zero results — indistinguishable from an empty section. `GET /pages?slug=` resolves all three spellings happily, so the two endpoints disagree. `WikiClient.page_search` therefore normalizes and lowercases `cluster` itself, like every other slug-shaped client argument; the tool layer only rejects a prefix that normalizes to empty. Verified live 2026-08-18, along with the two guarantees the tool description promises: the filter matches on path segments (`a/b` does not pull in `a/bc`) and includes the cluster page itself

- `page_read_attachment` decides "is this an image?" by **magic bytes only** — PNG/JPEG/GIF/WebP. Never by the response's `Content-Type`: an `ImageContent` block the vision API cannot decode fails the host's next model call with `Could not process image`, and hosts retry the same tool call, so the session dies rather than degrading (anthropics/claude-code#28279). SVG therefore travels as text. The header is still what picks the read ceiling — it is the only signal before the body, since the download endpoint sends no `Content-Length` — and an uninformative header (`octet-stream`, or none) gets the image budget so the magic check can run on files above the text ceiling
- `page_download_attachment` writes to the caller's disk, so it is gated with `page_upload_attachment` behind `include_local_uploads`, and `page_read_attachment`'s description only points at it when it is actually registered. The write is `.part` → fsync → rename (`link`+`unlink` without `overwrite`, so the refusal is the kernel's), and permissions are `0666 & ~umask` for a new file or the replaced file's own mode — never `tempfile`'s 0600, which `os.replace` would carry onto the delivered file

Per-request organization override:
- a client may append `?orgId=` or `?cloudOrgId=` to the server endpoint, and those replace the configured organization for that request
- handlers with a `Context` read it from `ctx.request_context.request`; the configuration resource has no `Context` and reads the middleware-stashed request instead
- both go through `get_yandex_auth()`, which prefers an explicit `ctx` so a middleware regression cannot reach the tools
