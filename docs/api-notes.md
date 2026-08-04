**English** | [Русский](api-notes_ru.md)

# Yandex Wiki API field notes

Findings verified live against a production Yandex 360 organization. The org-neutral
probe scripts in [`scripts/`](../scripts/) are living documentation of this behavior and
can be re-run against your own organization: `probe_api*.sh`/`smoke.sh` (curl-based;
credentials via env vars or a `$SECRETS` file, output goes to `raw/`, which is gitignored
because it contains real org data), plus the Python probes `contract_sweep.py` (every
client method against the live API), `token_probe.py` (payload sizes and extra fields)
and `yfm_smoke.py` (YFM rendering rules).

**Warning: the undocumented parts of this API drift.** The search endpoint silently
changed its wire contract between 2026-07-19 and 2026-08-02 (see below) — no versioning,
no deprecation. When something looks off, re-run the probes before trusting these notes.
The [API drift check](../.github/workflows/api-drift.yml) workflow re-runs the contract
sweep weekly against a live organization when its `DRIFT_*` secrets are configured.

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
inspired `page_search`; this project independently re-verified and extended those findings.

**The wire contract silently changed between 2026-07-19 and 2026-08-02.** As probed in
July: `page_size` controlled the result count (ceiling 50, out of range → 400), the
envelope carried `total_documents`/`total_pages`, `modified_at` was an epoch integer and
the snippet key was `body`. None of that is true anymore. Current behavior, verified
2026-08-02:

- The result-count knob is **`limit` in the POST body**: 1–50, anything else → HTTP 400.
  `page_size`, `page` and `offset` are accepted but **ignored** (you get the default 10
  results). The tool keeps its `page_size` argument and sends it as `limit`, clamped to 1–50.
- The envelope is `results` + `next_cursor`/`prev_cursor`. The cursors are **always
  `null`**, and a request `cursor` is validated (garbage → 400) but never satisfiable —
  the pagination machinery exists in the schema only. You get the top ≤50 hits, full stop.
  `total_documents`/`total_pages` are gone.
- Per result: the snippet is in **`content`** (plain text), `modified_at` is an **ISO
  datetime string**. Two result types: **`page`** (relative `url`, normalized by the tool
  to an absolute link based on `WIKI_WEB_BASE_URL`) and **`file`** (absolute
  `...?download=1` download link).
- Size, measured with `scripts/token_probe.py` on 2026-08-04 at `limit=50`: a 48-hit
  response is ~28k chars, of which ~14k are snippets — 33 of 48 snippets exceed 200
  chars. Worth knowing before raising `page_size`: the endpoint honoring `limit`
  (fixed in 0.8.0) made a full-size search roughly 5x heavier than the 10-result
  replies it used to return.
- There is still **no server-side filtering** — section/type body params are ignored.
  The tool's `slug_prefix` and `result_type` arguments are applied client-side after fetching.
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
- `page_type` in `POST /pages` is **ignored** — any value, even garbage, yields a
  `wysiwyg` page with no error (verified 2026-07-27).
- `POST /pages/{id}/append-content` responds with the **full updated page object**
  (id, content, breadcrumbs, access data, owner…), not a status stub.
- Descendants items carry **only `id` and `slug`** — no titles; a `fields` query param is
  accepted but has no effect. Cursor pagination works (verified with `page_size=5` walks).
- `GET /pages/descendants` returns the **full subtree, all nesting levels**, as one flat
  list — not just direct children (verified 2026-08-03: a 3-level tree arrives in a single
  call). There is no depth parameter; slugs encode the hierarchy, so depth is
  `slug.count("/")` relative to the root.
- Delete → recover (`DELETE /pages/{id}` → `POST /recovery_tokens/{token}/recover`)
  restores the page with the **same id**; the recover response also carries `slug` and
  `pages_count` (subtree size).
- Attachment objects include an undocumented `is_downloadable` flag.

## Comments

- Comment objects carry `author` (id, login, display name), `inline_text`, `is_deleted`,
  `reactions` and `resolve_status`. There is **no `user` key** (as of 2026-08-02 —
  early versions of this project modeled one).
- Cursor pagination on `GET /pages/{id}/comments` works (verified with `page_size=2` walks).

## Grids

- Grid mutation endpoints use optimistic locking: they require the current `revision`
  and reject the request when it is stale.
- The wire format of `default_sort` is a list of single-entry mappings, for example
  `[{"status": "asc"}]`. The `grid_update` tool accepts the friendlier
  `[{"column": "status", "direction": "asc"}]` shape and converts it.
- `grid_add_columns` requires `required` on every column — the API validates it.
- `grid_copy` is asynchronous: the API returns operation metadata, not a ready copied grid.
- `POST /grids/{id}/cells` responds with a **`cells`** key — unlike the row/column
  mutations, which answer with `results` (+ `revision`).

## Errors and limits

- Error responses come in **two envelope shapes** (`message` as string-or-null plus
  `details`, or as a list plus `level`); the client parses both and surfaces the API's
  own message in `WikiApiError`.
- No rate-limit headers are exposed (`X-RateLimit-*`/`Retry-After` absent).
