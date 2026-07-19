# Yandex Wiki Search MCP

`mcp-name: io.github.dlbolshov/yandex-wiki-search-mcp`

MCP server for the Yandex Wiki API with **full-text search**, focused on Wiki pages, comments, resources, attachments, and recovery workflows.
It is the only Yandex Wiki MCP server that combines full-text content discovery, a server-side read-only mode, and a ready-to-use Docker image.
The tool surface also includes first-class support for Yandex Wiki dynamic tables ("grids").

Fork of [APonkratov/yandex-wiki-mcp](https://github.com/APonkratov/yandex-wiki-mcp) (`ya-yandex-wiki-mcp`), see [Credits](#credits).

## Supported tools

- `page_search`: full-text search across the entire Wiki (the headline feature)
- `page_get_grids`: list grids attached to a page
- `grid_get`: get a grid by `grid_id`
- `page_get`: get a page by `page_id` or `slug`
- `page_get_descendants`: get a page subtree
- `page_get_comments`: get page comments
- `page_get_resources`: get page resources, including attachments and grids
- `page_get_attachments`: get page attachments
- `grid_create`: create a grid on a page
- `grid_update`: update grid title and/or default sort
- `grid_delete`: delete a grid
- `grid_copy`: copy a grid to an existing target page
- `grid_add_rows`: add rows to a grid
- `grid_delete_rows`: delete rows from a grid
- `grid_update_cells`: update individual grid cells
- `grid_add_columns`: add columns to a grid
- `grid_delete_columns`: delete columns from a grid
- `grid_move_rows`: move a row inside a grid
- `grid_move_columns`: move a column inside a grid
- `page_create`: create a page
- `page_update`: update page title and/or full content
- `page_append_content`: append content to top, bottom, or anchor
- `page_add_comment`: add a page comment or reply
- `page_delete`: delete a page and receive recovery token
- `page_recover`: recover a page by recovery token
- `page_upload_attachment`: upload a local file in chunks and attach it to a page

## Full-text search

`page_search` wraps the undocumented-but-public `POST /v1/search` endpoint — the same
backend that powers the Wiki web search bar. It is the content **discovery** entry point:
search first, then open a result with `page_get` by its `slug`. The endpoint was first
discovered and published by [slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki),
which directly inspired this tool; this project independently re-verified and extended
those findings (e.g. `page_size` accepts up to 50, not 10).

- Returns up to **50** results per call (`page_size` is clamped to 1–50 client-side; the API rejects anything else with HTTP 400).
- Search is **global only** — there is no server-side section or type filter. The optional `slug_prefix` and `result_type` arguments are applied **client-side after fetching**, so combine them with `page_size=50` to avoid missing matches. `slug_prefix` matches on path-segment boundaries (`tech-doc/ml` does not match `tech-doc/mlops`).
- Results come in two types: **`page`** (relative url, normalized by the tool to an absolute `https://wiki.yandex.ru/...` link) and **`file`** (absolute `...?download=1` download link).
- Quoted `"exact phrase"` queries work and produce phrase-matched results.
- `total_documents` always equals the number of returned results — it is **not** a global hit count.

## Notes on the Yandex Wiki API

Findings verified live against a production Yandex 360 organization (see the probe
scripts in [`scripts/`](scripts/)):

- `POST /v1/search` is undocumented; `page_size` max is 50 (0, negative, or >50 → HTTP 400); there is no server-side pagination or filtering — `page`/`offset`/`limit` and any section/type body params are ignored, and `total_pages` is always 1 (or 0 when empty).
- **OAuth scopes are not enforced** by the Wiki API — a token with only `wiki:read` can still write. Read-only is guaranteed only by not registering write tools (`WIKI_READ_ONLY=true`). *Credit: first reported publicly by [slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki) and independently confirmed here.*
- **HTTP 403 is about user permissions**, not token scopes — e.g. readonly system pages owned by `yandex360-wiki` (per slartus, see above).
- **Any `POST /pages/{id}` bumps `modified_at`**, even with an empty body — the page is marked as modified (per slartus, see above).
- Quoted `"exact phrase"` search works; `-minus` and boolean operators are ignored.
- There is **no revisions/history/backlinks API** — "who links here" workflows are not possible.
- `created_at`/`modified_at`/`comments_count`/`is_readonly` are not top-level page fields; fetch them via `page_get` with `fields=["attributes"]`.
- Error responses come in **two envelope shapes** (`message` as string-or-null plus `details`, or as a list plus `level`); the client parses both.
- No rate-limit headers are exposed (`X-RateLimit-*`/`Retry-After` absent).
- `GET /pages/{id}/resources?q=` is the only server-side *text* filter in the whole API (title search within one page's attachments/grids) — exposed via `page_get_resources`.

The org-neutral scripts in [`scripts/`](scripts/) (`probe_api*.sh`, `smoke.sh`) are living
documentation of this behavior and can be re-run against your own organization
(credentials via env vars or a `$SECRETS` file; probe output goes to `raw/`, which is
gitignored because it contains real org data).

## Quickstart (Claude Desktop / Cursor / Windsurf)

Docker, read-only (recommended for agents):

```json
{
  "mcpServers": {
    "yandex-wiki-search": {
      "command": "docker",
      "args": ["run","--rm","-i",
        "-e","WIKI_TOKEN","-e","WIKI_ORG_ID","-e","WIKI_READ_ONLY=true",
        "ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest"],
      "env": {"WIKI_TOKEN":"...","WIKI_ORG_ID":"..."}
    }
  }
}
```

`uvx` (PyPI):

```json
{
  "mcpServers": {
    "yandex-wiki-search": {
      "command": "uvx",
      "args": ["yandex-wiki-search-mcp"],
      "env": {
        "WIKI_TOKEN": "...",
        "WIKI_ORG_ID": "...",
        "WIKI_READ_ONLY": "true"
      }
    }
  }
}
```

## Why these tools

The toolset is based on the public Yandex Wiki API areas that are most useful in an MCP workflow:

- full-text discovery of pages and files
- page read/write operations
- grid read/write operations
- subtree traversal for documentation sections
- comments for review and collaboration flows
- resources and attachments for document management
- recovery tokens for safe automation
- upload sessions for large local files

## Grid Notes

- All non-read tools are disabled when `WIKI_READ_ONLY=true`.
- Grid mutation tools use optimistic locking where the Wiki API requires `revision`.
- `grid_copy` returns operation metadata from the Wiki API, not a ready copied grid object.
- `grid_add_columns` requires `required` on every column because the real Wiki API validates it.
- `grid_update.default_sort` takes a list of `{"column": ..., "direction": ...}` entries, for example `[{"column": "status", "direction": "asc"}]`; the server converts them to the single-entry mappings the real API expects.

These areas are documented in the official Yandex Wiki API references and examples:

- API overview: `https://yandex.ru/support/wiki/en/api-ref/about`
- API examples: `https://yandex.ru/support/wiki/ru/api-ref/examples`
- Page resources: `https://yandex.ru/support/wiki/ru/api-ref/pagesresources/pagesresources__resources`
- Grids API index: `https://yandex.ru/support/wiki/ru/api-ref/grids/`

## Authentication

Set one of these:

- `WIKI_TOKEN`
- `WIKI_IAM_TOKEN`

And exactly one organization header source:

- `WIKI_ORG_ID`
- `WIKI_CLOUD_ORG_ID`

Optional:

- `TRANSPORT=stdio|sse|streamable-http`
- `WIKI_API_BASE_URL=https://api.wiki.yandex.net`
- `WIKI_WEB_BASE_URL=https://wiki.yandex.ru` (base for absolute page links in `page_search` results)
- `WIKI_READ_ONLY=true|false`
- `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR|CRITICAL` (stderr; `DEBUG` additionally logs Wiki API requests, default `INFO`)

## Run locally

```bash
uv sync --dev
uv run yandex-wiki-search-mcp
```

## Docker deployment

The Docker image requires the same core environment variables as the local launch:

- one of `WIKI_TOKEN` or `WIKI_IAM_TOKEN`
- exactly one of `WIKI_ORG_ID` or `WIKI_CLOUD_ORG_ID`
- `TRANSPORT=streamable-http` for HTTP deployment

Optional:

- `HOST=0.0.0.0`
- `PORT=8000`
- `WIKI_API_BASE_URL=https://api.wiki.yandex.net`
- `WIKI_READ_ONLY=true|false`

## Using Pre-built Image (Recommended)

```bash
# Using environment file
docker run --env-file .env -p 8000:8000 ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest

# With inline environment variables
docker run -e WIKI_TOKEN=your_token \
           -e WIKI_ORG_ID=your_org_id \
           -e TRANSPORT=streamable-http \
           -p 8000:8000 \
           ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest
```

The MCP endpoint is available at `http://localhost:8000/mcp`.

## Building the Image Locally

```bash
docker build -t yandex-wiki-search-mcp .
```

## Docker Compose

**Using pre-built image:**

```yaml
version: '3.8'
services:
  mcp-wiki:
    image: ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest
    ports:
      - "8000:8000"
    environment:
      - WIKI_TOKEN=${WIKI_TOKEN}
      - WIKI_ORG_ID=${WIKI_ORG_ID}
      - TRANSPORT=streamable-http
```

**Building locally:**

```yaml
version: '3.8'
services:
  mcp-wiki:
    build: .
    ports:
      - "8000:8000"
    environment:
      - WIKI_TOKEN=${WIKI_TOKEN}
      - WIKI_ORG_ID=${WIKI_ORG_ID}
      - TRANSPORT=streamable-http
```

If you enable Redis-backed OAuth storage later, the existing [`compose.yaml`](compose.yaml) can be used as the Redis service baseline.

## Contributing

Before creating a commit or opening a merge request, run the full local verification set from [CONTRIBUTING.md](CONTRIBUTING.md).

## Tests

```bash
uv run pytest
```

## Credits

This project is a fork of [APonkratov/yandex-wiki-mcp](https://github.com/APonkratov/yandex-wiki-mcp)
(`ya-yandex-wiki-mcp`) by Aleksandr Ponkratov, an excellent, well-tested Python MCP server
for the Yandex Wiki API, licensed under Apache-2.0. This fork adds full-text search
(`page_search`) and rebranding; the original copyright and license are preserved
(see [LICENSE](LICENSE) and [NOTICE](NOTICE)).

The idea and key API findings behind full-text search come from
[slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki) (JavaScript, MIT):
it was the first to discover the undocumented `POST /v1/search` endpoint and to report
that OAuth scopes are not enforced. No code was taken from it — only findings and ideas,
independently re-verified against a live organization and extended here.
