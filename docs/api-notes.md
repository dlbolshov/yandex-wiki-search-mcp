# Yandex Wiki API field notes

Findings verified live against a production Yandex 360 organization. The org-neutral
probe scripts in [`scripts/`](../scripts/) (`probe_api*.sh`, `smoke.sh`) are living
documentation of this behavior and can be re-run against your own organization
(credentials via env vars or a `$SECRETS` file; probe output goes to `raw/`, which is
gitignored because it contains real org data).

Official references:

- API overview: <https://yandex.ru/support/wiki/en/api-ref/about>
- API examples: <https://yandex.ru/support/wiki/ru/api-ref/examples>
- Access and tokens: <https://yandex.ru/support/wiki/ru/api-ref/access>
- Page resources: <https://yandex.ru/support/wiki/ru/api-ref/pagesresources/pagesresources__resources>
- Grids API index: <https://yandex.ru/support/wiki/ru/api-ref/grids/>

## Search endpoint (`POST /v1/search`)

The endpoint is undocumented but public — it is the same backend that powers the Wiki
web search bar. It was first discovered and published by
[slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki), which directly
inspired `page_search`; this project independently re-verified and extended those
findings (e.g. `page_size` accepts up to 50, not 10).

- `page_size` max is **50** (0, negative, or >50 → HTTP 400). The tool clamps it to 1–50 client-side.
- There is **no server-side pagination or filtering** — `page`/`offset`/`limit` and any
  section/type body params are ignored, and `total_pages` is always 1 (or 0 when empty).
  The tool's `slug_prefix` and `result_type` arguments are applied client-side after fetching.
- `total_documents` always equals the number of returned results — it is **not** a global hit count.
- Results come in two types: **`page`** (relative url, normalized by the tool to an
  absolute link based on `WIKI_WEB_BASE_URL`) and **`file`** (absolute `...?download=1` download link).
- Quoted `"exact phrase"` queries work and produce phrase-matched results;
  `-minus` and boolean operators are ignored.

## Auth, scopes, and permissions

- **OAuth scopes are not enforced** by the Wiki API — a token with only `wiki:read` can
  still write. Read-only is guaranteed only by not registering write tools
  (`WIKI_READ_ONLY=true`). *Credit: first reported publicly by
  [slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki) and independently
  confirmed here.*
- **HTTP 403 is about user permissions**, not token scopes — e.g. readonly system pages
  owned by `yandex360-wiki` (per slartus, see above).
- Two organization header sources exist: `X-Org-Id` (Yandex 360) and `X-Cloud-Org-Id`
  (Yandex Cloud); the server sets one based on `WIKI_ORG_ID`/`WIKI_CLOUD_ORG_ID`.

## Pages

- **Any `POST /pages/{id}` bumps `modified_at`**, even with an empty body — the page is
  marked as modified (per slartus, see above).
- There is **no revisions/history/backlinks API** — "who links here" workflows are not possible.
- `created_at`/`modified_at`/`comments_count`/`is_readonly` are not top-level page
  fields; fetch them via `page_get` with `fields=["attributes"]`.
- `GET /pages/{id}/resources?q=` is the only server-side *text* filter in the whole API
  (title search within one page's attachments/grids) — exposed via `page_get_resources`.

## Grids

- Grid mutation endpoints use optimistic locking: they require the current `revision`
  and reject the request when it is stale.
- The wire format of `default_sort` is a list of single-entry mappings, for example
  `[{"status": "asc"}]`. The `grid_update` tool accepts the friendlier
  `[{"column": "status", "direction": "asc"}]` shape and converts it.
- `grid_add_columns` requires `required` on every column — the API validates it.
- `grid_copy` is asynchronous: the API returns operation metadata, not a ready copied grid.

## Errors and limits

- Error responses come in **two envelope shapes** (`message` as string-or-null plus
  `details`, or as a list plus `level`); the client parses both and surfaces the API's
  own message in `WikiApiError`.
- No rate-limit headers are exposed (`X-RateLimit-*`/`Retry-After` absent).
