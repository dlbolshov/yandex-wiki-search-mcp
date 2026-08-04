# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added
- `fetch_all` flag on the five cursor-paginated tools (`page_get_descendants`, `page_get_comments`, `page_get_attachments`, `page_get_resources`, `page_get_grids`): the server follows `next_cursor` and returns everything in one call, capped at 500 items and a 25s budget. The response then carries `truncated`: `false` only when the list was drained to its end, `true` when the walk stopped early — on the cap, the budget, a failed page or a cursor the server repeated. `next_cursor` then points at the continuation, except after a repeated cursor, where nothing is safe to continue from and it is cleared. A failed page keeps what was already fetched rather than discarding it. `page_search` deliberately has no such flag — its cursors are dead server-side (always `null`)
- `TOOL_RESULT_TEXT` setting for the text duplicate of structured tool results: `pretty` (indent=2, the FastMCP default and still the spec-friendly choice), `compact` (single line — 10-30% off the text block: most on long lists of small objects, least on a few large strings, since indentation is charged per line) or `none` (structured content only; make sure your client renders `structuredContent` before enabling). Structured content and its schema validation are unaffected
- Living defense against API drift: `scripts/contract_sweep.py` exercises every `WikiClient` method against a live organization (fixtures under a scratch slug, delete/recover cycle included) and reports pydantic mismatches plus undeclared response keys; the `API drift check` workflow re-runs it weekly when the opt-in `DRIFT_WIKI_TOKEN` secret and `DRIFT_*` variables are configured. Born from the search-endpoint incident: the undocumented backend silently swapped its wire contract between 2026-07 and 2026-08 (documented in `docs/api-notes.md`)

### Fixed
- `page_search` was broken against the live API (all fixes verified live on 2026-08-02):
  - any non-empty search failed with a validation error — the API sends `modified_at` as an ISO datetime string, the model expected an integer epoch
  - the snippet arrives in the `content` key, not `body`: the declared field was always empty and the snippet text only reached clients through the extra-fields leak
  - the `page_size` tool parameter was silently ignored by the API (every search returned at most 10 results regardless of the requested size) — the endpoint reads `limit` from the POST body, which the client now sends; values above 50 are a validation error server-side, the existing clamp keeps them at 50

### Changed
- `SearchResponse` now mirrors the live envelope: `results` plus `next_cursor`/`prev_cursor` (currently always `null` server-side). The previously declared `total_documents`, `total_pages`, `page_id`, `search_client` and `uid` fields never arrived from the API and were removed from the schema
- Tool results went on a token diet (all shapes verified against the live API, `scripts/contract_sweep.py`):
  - `None` values are omitted from every tool result — fields the API did not send no longer arrive as `null` noise. `page_append_content` is now typed as a page too (the endpoint answers with the full updated page, not a status stub), so the heaviest write response went from 11 keys to 4
  - unknown API keys are dropped from fixed-shape models instead of leaking into results verbatim; models carrying grid *user data* (`WikiGrid`, `WikiGridColumn`, `WikiGridRow`, `WikiGridSort`, `WikiGridStructure`, `GridUpdateResponse`) still pass unknown keys through, and there a `null` under an unknown key is kept — "this cell is empty" has to stay distinguishable from "no such column". Service envelopes around them (`WikiGridSummary`, `WikiGridPageRef`) stay strict
  - fields the live API actually sends are now declared instead of leaking untyped: `WikiPage.access_policy`/`access_lists`/`owner` (arrive when requested via `fields`), comment `author`/`inline_text`/`is_deleted`/`resolve_status`/`reactions`, attachment `is_downloadable`, recover `slug`/`pages_count`
  - breaking for schema consumers: `grid_update_cells` returns the new `GridCellsResponse` (`revision` + `cells`). It used to share `GridMutationResponse` with the row and column mutations, whose `results` has a list default and so was never dropped as empty — every successful cell update answered with `"results": []`, which reads as "nothing changed" to an agent checking that key. The other seven grid mutations no longer advertise a `cells` field they never fill
  - user references (comment `author`, attachment `user`, page `owner.user`) are trimmed to `id`/`username`/`display_name` — the raw API sends internal identity payloads (`uid`, `cloud_uid`, dismissal flags) on every comment and attachment, and 215 chars of them on every page fetched with `fields=["owner"]` (80 after trimming)
  - `page_get_descendants` items are now honest `{id, slug}` objects (the live API never sends titles there), shrinking both the output schema and each result
  - pydantic's auto-generated `title` keys are stripped from the model JSON schemas — `tools/list` shrinks by ~4.4k characters of pure noise
  - breaking for schema consumers: `PageComment.user` and `PageComment.updated_at` were removed — the live API sends neither (the author arrives in `author`)
- `TOOL_RESULT_TEXT=compact` and `none` now touch only the JSON duplicate: a tool returning real content blocks keeps them. Nothing does yet, but the setting used to replace an image with its own base64 or drop it outright
- Transport failures now raise `WikiTransportError` (a `WikiError`) instead of leaking aiohttp's own exceptions: callers no longer need to import aiohttp and track its hierarchy to handle a dropped connection. This also fixes a bare request timeout reaching MCP clients as `Error executing tool page_get: ` with nothing after the colon — `str(TimeoutError())` is empty. Retry behavior is unchanged
- Running without OAuth now requires `WIKI_ORG_ID` or `WIKI_CLOUD_ORG_ID` at startup instead of failing on the first API call. Under `OAUTH_ENABLED=true` the organization still arrives per request, so nothing is required there — via `?orgId=` / `?cloudOrgId=` on the MCP server URL, which the READMEs now document. Configuration failures raise `WikiConfigError` (a `WikiError`) instead of a bare `ValueError`, and the missing-organization message names both ways to supply one

### Removed
- The `page_type` parameter of `page_create` (breaking): a live smoke on 2026-07-27 proved the API ignores it entirely — any value, even garbage, yields a `wysiwyg` page with no error, so the parameter only misled agents into believing they could create other page types. Also removed from `WikiClient.page_create` and the protocol

## [0.7.0] - 2026-07-27

### Added
- YFM (Yandex Flavored Markdown) helpers, rules verified against a live wiki (`scripts/yfm_smoke.py`):
  - `wiki-mcp://yfm-cheatsheet` resource — which Markdown/GFM habits render as-is (pipe tables and task lists do!), which break (raw HTML, `> [!NOTE]` alerts), and the YFM equivalents
  - warnings-only markup check (`mcp_wiki/yfm.py`, fence-aware, dependency-free): unclosed `{% note/cut/list %}` blocks, `#|` tables and code fences; GFM alerts and raw HTML that render as literal text. Writes are never blocked
  - `yfm_warnings` field in `page_create`/`page_update` responses (schema-additive) and key in `page_append_content` responses; warnings are capped at 10 per call
  - page-type guard: writing content by slug to a non-`wysiwyg` page warns — grid pages get a dedicated message pointing to the `grid_*` tools, other types get a legacy-format warning; title-only updates stay silent, no extra GET on the id path
  - YFM note in the three write tool descriptions and server instructions
- `/healthz` liveness endpoint for HTTP deployments — always answers `200 ok` without calling the Wiki API, so an upstream outage cannot fail container health checks
- The server now reports its real package version to MCP clients in `initialize` (previously the version of the `mcp` library was reported)
- `STATELESS_HTTP` and `JSON_RESPONSE` settings for the `streamable-http` transport (previously hardcoded to `true`/`true`, which stay the defaults)
- CI guard for release metadata (`scripts/check_versions.py`): version consistency across `pyproject.toml`, `uv.lock`, `manifest.json` and `server.json` is now checked on every PR, not only by the release workflow at tag time

### Changed
- `page_update` now requires at least one of `title`/`content` — the Wiki API bumps `modified_at` even on an empty POST, so accidental no-op calls used to mutate the page

## [0.6.0] - 2026-07-27

### Added
- OAuth token revocation: `revoke_token` is implemented (the revocation endpoint returned 500 before); revoking a refresh token also revokes the paired access token, revoking an access token keeps the refresh token valid
- Test suite for the OAuth layer (provider flow, in-memory and Redis stores via fakeredis, Fernet crypto with key rotation, serializers) and for `Settings` validators — line coverage 73% → 88% (branch 83%), gated in CI at 80% branch coverage
- Codecov upload + coverage badge; `dependabot.yml` for uv, GitHub Actions and Docker
- Retries with jittered backoff in `WikiClient`: dropped connections (a long-lived stdio server loses its keep-alive connection while idle) and `429`/`502`/`503`/`504` responses are retried twice, adding at most ~0.9s. Only requests that are safe to repeat are retried — all reads, `page_search` and upload parts; write requests still fail fast, because a 5xx can arrive after the write was applied. Timeouts are never retried. `Retry-After` is honored when it asks for 3s or less, otherwise the error is raised right away; the HTTP-date form of the header falls back to the regular backoff
- `WIKI_MAX_RETRIES` setting (default `2`, `0` disables retries)

### Changed
- CI split into a single `lint` job (ruff, format, ty, mypy on Python 3.11) and the test matrix (3 OS × 3 Python, pytest + coverage); concurrent runs on the same ref are cancelled
- Ruff rule sets expanded (`UP`, `SIM`, `RUF`, `PTH`, `ASYNC`, `PERF`, `TRY`, `S`); mypy config tightened (`disallow_untyped_defs`, targeted overrides instead of blanket `ignore_missing_imports`); production asserts converted to explicit raises
- Docs: README/README_ru rebuilt (install buttons, tool tables, comparison with alternatives); deep API notes moved to `docs/api-notes.md` (+ Russian mirror)

### Fixed
- `RedisOAuthStore`: the refresh→access token mapping key is now saved with a TTL — previously it never expired and leaked one Redis key per login

## [0.5.0] - 2026-07-19

### Added
- All 26 tools now declare `outputSchema` (typed returns) and emit structured content
- `ToolAnnotations` on write tools: `destructiveHint` for deletes, `idempotentHint` for updates/moves, additive hints for creates/appends
- `WikiClient` supports `async with`; `GridNotFound` error for grid 404s
- Separate (larger, default 300s) timeout for upload requests
- Own test suite for the anchor fallback module

### Changed
- **Breaking (tool schema)**: `grid_update.default_sort` now takes `[{"column": ..., "direction": ...}]` objects instead of single-entry mappings
- `grid_update_cells.cells` and `grid_add_columns.columns` are typed Pydantic models (`GridCellPatch`, `GridColumnSpec`) with real JSON schemas instead of free-form objects
- Every Wiki API error is now raised as `WikiApiError` with the API's own message (both error envelope shapes parsed) instead of a raw `aiohttp.ClientResponseError`; 404s map to `PageNotFound`/`GridNotFound` consistently
- `ClientSession` is created in `prepare()` instead of `__init__`
- File reads in `page_upload_attachment` no longer block the event loop (`asyncio.to_thread`)
- Anchor fallback logic extracted from the client into `wiki/custom/anchors.py`
- Tool layer deduplicated: shared `page_id`/`slug` params, `get_wiki()`/`resolve_page_id()`/`resolve_page_slug()` in `mcp/tools/common.py`

### Fixed
- Error responses with non-UTF-8 bodies (e.g. proxy HTML error pages) no longer crash with `UnicodeDecodeError` and produce a proper `WikiApiError`
- `grid_update_cells` rejects empty/whitespace `row_id` instead of sending it to the API
- `grid_add_rows.after_row_id` accepts numeric row IDs, consistent with `grid_move_rows`
- `PageNotFound` for slug-based lookups reports the normalized slug instead of the raw input (URL, leading/trailing slashes)

## [0.4.0] - 2026-07-19

### Added
- `LOG_LEVEL` setting (default `INFO`): logging goes to stderr (stdio-safe); startup logs a secret-free config summary; `DEBUG` additionally logs every Wiki API request (method, path, status, duration — no headers or bodies)
- `py.typed` marker so the package ships type information
- `.env.example` documenting all supported environment variables

### Changed
- Secrets in `Settings` (`wiki_token`, `wiki_iam_token`, `oauth_client_secret`, `oauth_encryption_keys`, `redis_password`) are `SecretStr` — masked in `repr`/logs, unwrapped only at usage points
- `YandexAuth.token` is excluded from the dataclass `repr`
- `page_add_comment` validates `parent_id`/`thread_id` as positive integers (shared `CommentID` type)
- `page_append_content.location` and `page_upload_attachment.append_location` are typed as `Literal["top", "bottom"]` end-to-end via the shared `UploadLocation` type
- `WikiClient._build_headers` is synchronous (it never awaited anything)
- Importing `mcp_wiki.__main__` no longer instantiates settings and the MCP server; they are created inside `main()`

### Removed
- Dead code: `WikiMCPError` (`mcp_wiki/mcp/errors.py`) and the unused `set_non_needed_fields_null` helper

## [0.3.0] - 2026-07-19

### Added
- `page_search`: full-text Wiki search (`POST /v1/search`), read-only, `page_size` 1-50 (clamped client-side), client-side `slug_prefix` (case-insensitive, path-segment boundary) and `result_type` filters, page urls normalized to absolute links; `total_documents`/`total_pages` recalculated after filtering
- `WIKI_WEB_BASE_URL` setting (default `https://wiki.yandex.ru`) — base for absolute page links in `page_search` results
- New `page_get` fields: `access_policy`, `access_lists`, `owner`
- Org-neutral live API probe scripts (`scripts/probe_api*.sh`, `scripts/smoke.sh`) documenting verified Yandex Wiki API behavior
- Synthetic test fixtures in `tests/fixtures/`

### Changed
- Rebranded to `yandex-wiki-search-mcp` (fork of [APonkratov/yandex-wiki-mcp](https://github.com/APonkratov/yandex-wiki-mcp)); added `NOTICE` and README credits
- `WikiApiError.message` now accepts list payloads (both API error envelope shapes) and renders them joined
- API error parsing tolerates non-object JSON error bodies (e.g. from proxies) instead of raising `AttributeError`

## [0.2.0] - 2026-04-06

### Added
- Added first-class grid read tools:
  - `page_get_grids`
  - `grid_get`
- Added grid write tools:
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
- Added grid protocol models for:
  - grid summaries and full grid reads
  - mutation responses
  - async grid copy operation metadata
- Added `CONTRIBUTING.md` with the required full local verification checklist before commit and merge request updates

### Changed
- Clarified `WIKI_READ_ONLY` semantics: it disables all non-read MCP tools, not only grid mutations
- Aligned `grid_update.default_sort` with the real Yandex Wiki API contract: the request now uses a list of single-entry `{column_slug: "asc"|"desc"}` mappings
- Exposed `WIKI_READ_ONLY` in `manifest.json` user config and runtime environment mapping

### Fixed
- Relaxed `grid_update` response parsing to accept revision-only bodies returned by the real API
- Fixed typing and formatting issues uncovered by the full CI-equivalent local verification set

## [0.1.2] - 2026-04-06

### Changed
- Run the test workflow on pull requests and only on pushes to `main` and `master`
- Run the release workflow only on version tags and manual dispatches
- Clarified MCP parameter descriptions for list-valued `fields` and `resource_types`

### Fixed
- Added a fallback for `page_append_content(anchor=...)` that updates page source when the Wiki API returns `ANCHOR_NOT_FOUND` for explicit source anchors
- Surfaced structured `WikiApiError` details from Wiki API 400 responses
- Fixed formatting in `tests/mcp/tools/test_page_read_tools.py`

## [0.1.1] - 2026-04-06

### Changed
- Aligned package, manifest, runtime, and registry metadata around `Yet Another Yandex Wiki MCP Server`
- Added `mcp-name: io.github.APonkratov/ya-yandex-wiki-mcp` to the package README for MCP Registry ownership validation
- Updated GitHub Actions workflows to Node 24-compatible action versions
- Improved release workflow with metadata validation, verbose PyPI publishing logs, `skip-existing`, and autogenerated GitHub release notes
- Fixed MCP Registry publishing namespace case for `io.github.APonkratov/ya-yandex-wiki-mcp`

### Fixed
- Fixed release workflow metadata validation to read `GITHUB_REF` from `os.environ`
- Fixed `ty` type narrowing in read tools for `page_id` and `slug` resolution
- Fixed `mypy` settings fixture typing for `oauth_server_url`

## [0.1.0] - 2026-04-06

### Added
- Initial release of `ya-yandex-wiki-mcp`
- Yet Another Yandex Wiki MCP Server with `stdio`, `sse`, and `streamable-http` transports
- Wiki HTTP client with support for:
  - page read by `page_id` or `slug`
  - subtree traversal via descendants endpoint
  - comments, resources, and attachments retrieval
  - page create, update, append, delete, and recover flows
  - multipart file upload and attachment linking
- MCP tools:
  - `page_get`
  - `page_get_descendants`
  - `page_get_comments`
  - `page_get_resources`
  - `page_get_attachments`
  - `page_create`
  - `page_update`
  - `page_append_content`
  - `page_add_comment`
  - `page_delete`
  - `page_recover`
  - `page_upload_attachment`
- Configuration resource `wiki-mcp://configuration`
- Project packaging files for PyPI/MCP registry/OCI metadata
- Basic repository documentation in English and Russian
- Test suite for:
  - MCP server registration
  - configuration resource
  - read/write MCP tools
  - core Wiki client requests

### Changed
- Adopted a modular MCP layout with separate settings, Wiki client, protocol models, MCP tools, and tests

### Verified
- `uv run ruff format .`
- `uv run ruff check . --fix`
- `uv run pytest`
