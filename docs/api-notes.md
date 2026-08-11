**English** | [Русский](api-notes_ru.md)

# Yandex Wiki API field notes

Findings verified live against a production Yandex 360 organization. The org-neutral
probe scripts in [`scripts/`](../scripts/) are living documentation of this behavior and
can be re-run against your own organization: `probe_api*.sh`/`smoke.sh` (curl-based;
credentials via env vars or a `$SECRETS` file, output goes to `raw/`, which is gitignored
because it contains real org data), plus the Python probes `contract_sweep.py` (every
client method against the live API), `token_probe.py` (payload sizes and extra fields),
`yfm_smoke.py` (YFM rendering rules) and `docs_probe.py` (the 2026-08 documentation
drop and the hosted MCP server, claim by claim).

**Warning: this API drifts, and the docs trail the wire in both directions.** The search
endpoint silently changed its wire contract between 2026-07-19 and 2026-08-02 (see below)
— no versioning, no deprecation — and the full reference Yandex published in August 2026
documents search pagination and ordering that the wire ignores, while OAuth scopes are
documented but not enforced. These notes deliberately do not restate what the reference
already covers; they track where the wire and the docs disagree, and what the docs leave
out. When something looks off, re-run the probes before trusting either.
The [API drift check](../.github/workflows/api-drift.yml) workflow re-runs the contract
sweep weekly against a live organization when its `DRIFT_*` secrets are configured.

Official references:

- API overview: <https://yandex.ru/support/wiki/en/api-ref/about>
- Full API reference (published 2026-08 — pages, attachments, access, comments, grids,
  search, operations, recovery): <https://yandex.ru/support/wiki/en/api-ref/>
- Search endpoint reference: <https://yandex.ru/support/wiki/ru/api-ref/search/search__search>
- Access and tokens: <https://yandex.ru/support/wiki/ru/api-ref/access>
- Yandex's hosted MCP server: <https://yandex.ru/support/wiki/ru/mcp>

## Search endpoint (`POST /v1/search`)

The endpoint is the same backend that powers the Wiki web search bar. It spent its first
years undocumented: it was discovered and published by
[slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki), which directly
inspired `page_search`; this project independently re-verified and extended those
findings. **In August 2026 Yandex documented it**
([reference](https://yandex.ru/support/wiki/ru/api-ref/search/search__search)) — but the
published contract and the wire still disagree, so the notes below track what actually
answers (docs-vs-wire probed 2026-08-11 with `docs_probe.py`).

**The wire contract silently changed between 2026-07-19 and 2026-08-02.** As probed in
July: `page_size` controlled the result count (ceiling 50, out of range → 400), the
envelope carried `total_documents`/`total_pages`, `modified_at` was an epoch integer and
the snippet key was `body`. None of that is true anymore. Current behavior, verified
2026-08-02:

- The result-count knob is **`limit` in the POST body**: 1–50, anything else → HTTP 400.
  `page_size`, `page` and `offset` are accepted but **ignored** (you get the default 10
  results). The tool exposes the same `limit` argument end-to-end (renamed from
  `page_size` in 1.0.0), clamped to 1–50.
- The envelope is `results` + `next_cursor`/`prev_cursor`. The cursors are **always
  `null`**, and a request `cursor` is validated (garbage → 400) but never satisfiable —
  the pagination machinery exists in the schema only. You get the top ≤50 hits, full stop.
  `total_documents`/`total_pages` are gone. The 2026-08 reference documents `cursor`
  (integer, 1–500, default 1) and string response cursors — the wire still honors
  neither (re-probed 2026-08-11: `next_cursor` stays `null`, `cursor: 2` returns page 1
  again).
- Per result: the snippet is in **`content`** (plain text), `modified_at` is an **ISO
  datetime string**. Two result types: **`page`** (relative `url`, normalized by the tool
  to an absolute link based on `WIKI_WEB_BASE_URL`) and **`file`** (absolute
  `...?download=1` download link).
- **What `content` actually is** (measured 2026-08-10 over 196 page results across four
  queries, each cross-checked against the page fetched by slug). It reads like a digest
  and is not one:
  - **A window of the page's rendered text, hard-capped at ~510 characters.** Observed
    lengths run 12–530, clustering at 505–511; medians per query 312–427. Pages of 10k
    characters get the same ~500-character budget.
  - **Positioned at the match, not at the head.** Only 9 of 50 sampled excerpts started
    at offset 0; others began 604, 2215, 2298, 2915, 3078 characters in. So it is neither
    a lede nor a summary.
  - **One contiguous window in the normal case** — 9 of 11 locatable samples; the rest
    genuinely spanned the page (one covered offsets 2298→4252 of 5504). The
    `\n`/`\t` inside are **the source page's own layout**, not fragment separators:
    table cells arrive tab-separated, which is why 109/196 excerpts contain tabs.
  - **No highlighting unless you opt in** — 0/196 carried `<b>`/`<em>`/`<mark>`, `**` or
    `==` (measured without `highlight`; requesting it wraps matches in `<em>`, see
    above) — and the query term is not guaranteed to be present at all (29/50).
  - Headings often arrive **doubled** at the start (`Таблица ПлощадокТаблица Площадок`).
  - Empty for `type: "file"` results.
  - Comparing `content` against `page_get`'s `content` needs care: the excerpt is
    rendered text while the page field is YFM markup, so a literal diff understates the
    overlap.
- Size, measured with `scripts/token_probe.py` on 2026-08-04 at `limit=50`: a 48-hit
  response is ~28k chars, of which ~14k are snippets — 33 of 48 snippets exceed 200
  chars. Worth knowing before raising `limit`: the endpoint honoring `limit`
  (fixed in 0.8.0) made a full-size search roughly 5x heavier than the 10-result
  replies it used to return.
- **Server-side filters arrived with the 2026-08 drop and work** (probed 2026-08-11):
  `filters.type` (`page`/`file`) returns only that type; `filters.cluster` restricts
  results to a section and **takes deep prefixes** (`a/b` returns only slugs under
  `a/b`; an unknown cluster is 200 with 0 results, not an error) — filtering happens
  before `limit`, so hits are not lost to it; `filters.created_at`/`modified_at` take
  a `{from, to}` interval
  and **require both bounds** — `from` alone is a 400 `SEARCH_BAD_REQUEST`.
  `filters.authors` and `show_obsolete` are documented but not probed yet. `order_by`
  (`relevancy`/`creation_date`/`modified_date`) is documented but **ignored** — neither
  value changes the order. As of 1.2.x the tool still applies `slug_prefix`/`result_type`
  client-side; moving onto the server filters is planned (ROADMAP M7).
- **`highlight: true` works**: matches inside `content` arrive wrapped in `<em>`
  (9/10 snippets changed against the same query without it). Off by default and not
  exposed by the tool yet.
- Quoted `"exact phrase"` queries work and produce phrase-matched results;
  `-minus` and boolean operators are ignored.

## Auth, scopes, and permissions

- **OAuth scopes are not enforced** by the Wiki API — a token with only `wiki:read` can
  still write. The 2026-08 token guide puts the `wiki:read` / `wiki:write` choice front
  and center, but enforcement did not follow: re-verified 2026-08-11 — a `wiki:read`-only
  token updated an existing page and created a new one, both HTTP 200. Read-only is
  guaranteed only by not registering write tools (`WIKI_READ_ONLY=true`). *Credit: first
  reported publicly by [slartus/mcp-yandex-wiki](https://github.com/slartus/mcp-yandex-wiki)
  and independently confirmed here.*
- **HTTP 403 is about user permissions**, not token scopes — e.g. readonly system pages
  owned by `yandex360-wiki` (per slartus, see above).
- Two organization header sources exist: `X-Org-Id` (Yandex 360) and `X-Cloud-Org-Id`
  (Yandex Cloud); the server sets one based on `WIKI_ORG_ID`/`WIKI_CLOUD_ORG_ID`.
- **`GET /users/me` is documented and live** (probed 2026-08-11): `username`,
  `home_cluster` (the caller's personal-section slug, e.g. `users/<login>`),
  `identity` (`uid`, `cloud_uid`) and `org`. Before the 2026-08 reference this project
  did not know the endpoint existed; scheduled as `user_get_current` (ROADMAP M7) —
  it turns "create it in my section" from a guess into a lookup.

## Pages

- **Any `POST /pages/{id}` bumps `modified_at`**, even with an empty body — the page is
  marked as modified (per slartus, see above).
- There is **no revisions/history/backlinks API** — "who links here" workflows are not possible.
- `created_at`/`modified_at`/`comments_count`/`is_readonly` are not top-level page
  fields; fetch them via `page_get` with `fields=["attributes"]`.
- `GET /pages/{id}/resources?q=` is a server-side title search within one page's
  attachments/grids — exposed via `page_get_resources`. (Until the 2026-08 search
  filters it was the only server-side text filter in the whole API.)
- `page_type` in `POST /pages` is **ignored** — any value, even garbage, yields a
  `wysiwyg` page with no error (verified 2026-07-27).
- **There is no move/rename** (probed live 2026-08-08). `POST /pages/{id}` with a
  `slug` field answers 200 and **silently ignores it** — the documented update body is
  `title`/`content`/`redirect`/`access_policy`/`owner`, no slug. No `/move` endpoint
  exists in v1 or v2 (404). Moving a page is a web-UI-only capability. Beware of MCP
  servers advertising "move" over this API: implemented via `POST /pages/{id}` it is a
  silent no-op that reports success.
- **`POST /pages/{id}/clone` is the only relocation primitive** — a deferred operation
  (`operation.id` + `status_url`; polled `GET /operations/clone/{id}` reaches
  `status: "success"` in about a second). The copy gets a **new page id**, copies title
  and content only, and **children do not follow** (verified 2026-08-08: `src/kid` did
  not appear under `dst/`). Comments, attachments, and history stay with the original.
  Slug collisions are refused on the initial POST with `SLUG_OCCUPIED` (400), before
  the operation starts. Exposed as `page_clone`, which polls the operation and returns
  the copy's id and slug.
- **Redirects work through the regular update** (probed 2026-08-11): `POST /pages/{id}`
  with `redirect: {"page": {"id": N}}` sets one, `redirect: {"page": null}` clears it,
  and the state reads back via `page_get` with `fields=["redirect"]`. Not exposed as a
  tool yet (ROADMAP M7).
- **Per-page access management is documented and live** (`POST`/`DELETE
  /pages/{idx}/access`): granting yourself a role you already hold is refused with
  `PAGE_ACCESS_ALREADY_GRANTED`, so the endpoint validates for real; a full grant/revoke
  round-trip needs a second user and was not probed. Not exposed as tools — an admin
  feature, out of scope for now.
- `POST /pages/{id}/append-content` responds with the **full updated page object**
  (id, content, breadcrumbs, access data, owner…), not a status stub.
- Descendants items carry **only `id` and `slug`** — no titles; a `fields` query param is
  accepted but has no effect. Cursor pagination works (verified with `page_size=5` walks).
  The documented `actuality` filter answers 200 (probed 2026-08-11 with
  `actuality=actual`); not exposed via the tool.
- `GET /pages/descendants` returns the **full subtree, all nesting levels**, as one flat
  list — not just direct children (verified 2026-08-03: a 3-level tree arrives in a single
  call). There is no depth parameter; slugs encode the hierarchy, so depth is
  `slug.count("/")` relative to the root.
- **An empty `?slug=` means the whole organization** (verified 2026-08-10). It answers
  `200` and drains to every page in the wiki, top-level pages included — 2039 items over
  21 requests at `page_size=100`, 16 top-level segments, depth 0–9, in the organization
  probed. Three checks that this is a deliberate contract and not a quirk:
  - it is **not** a fallback for bad input — `?slug=zzz-no-such-page-000` answers `404`;
  - the numbers reconcile — `?slug=tech-doc` yields 1064 items while the root walk holds
    1065 with that prefix, the extra one being `tech-doc` itself (`include_self=false`);
  - nothing is hidden from it — of ~200 search hits across four queries, **0** were
    missing from the root walk.

  `include_self=true` changes nothing there, the root being no page. Omitting `slug`
  altogether is a `400`, so the empty value is load-bearing: anything that strips an
  empty query parameter turns a wiki-wide walk into a validation error. Exposed as
  `page_get_descendants(from_root=true)`, deliberately behind an explicit flag so a
  forgotten argument cannot become a thousands-of-pages walk.
- There is **no root page**. `homepage` exists and is an ordinary page — its subtree held
  a single child — while the real top level is a set of sibling slugs (`tech-doc`,
  `users`, `common`, …). The organization root is reachable only as the empty slug above.
- `GET /pages/{id}/descendants` is a by-id variant of the same endpoint and works
  (`404` for an unknown id); undocumented when first probed, it appears in the 2026-08
  reference. The client uses the `?slug=` form only.
- **No other enumeration endpoint exists.** `GET /pages` without a slug is a `400`;
  `/pages/tree`, `/pages/root`, `/pages/list`, `/navigation` and `/clusters` are all
  `404` (probed 2026-08-10).
- Delete → recover (`DELETE /pages/{id}` → `POST /recovery_tokens/{token}/recover`)
  restores the page with the **same id**; the recover response also carries `slug` and
  `pages_count` (subtree size).
- Attachment objects include an `is_downloadable` flag. **Download and deletion are
  documented and live** (probed 2026-08-11): `GET /pages/{id}/attachments/{fid}/download`
  streams the bytes, `GET /pages/attachments/download_by_url?url=<slug>/.files/<name>`
  works too, `DELETE /pages/{id}/attachments/{fid}` answers 204. Not exposed as tools
  yet (ROADMAP M7).
- Deleting a page **frees its slug immediately**: the page stops resolving and the
  same slug can be created again, even though the delete is recoverable by token
  (verified 2026-08-05).

## Comments

- Comment objects carry `author` (id, login, display name), `inline_text`, `is_deleted`,
  `reactions` and `resolve_status`. There is **no `user` key** (as of 2026-08-02 —
  early versions of this project modeled one).
- Cursor pagination on `GET /pages/{id}/comments` works (verified with `page_size=2` walks).
- **Comment deletion is live**: `DELETE /pages/{id}/comments/{cid}` answers 200 with the
  page's updated `comments_count` (probed 2026-08-11). Not exposed as a tool yet
  (ROADMAP M7).
- The documented thread endpoint (`GET /pages/{id}/comments/{cid}/thread`) answers 200
  but returned an **empty list for a root comment with a live reply** (the reply carried
  `parent_id`, `thread_id` was null on both) — semantics unclear, left alone.

## Grids

- Grid mutation endpoints use optimistic locking: they require the current `revision`
  and reject the request when it is stale.
- The wire format of `default_sort` is a list of single-entry mappings, for example
  `[{"status": "asc"}]`. The `grid_update` tool accepts the friendlier
  `[{"column": "status", "direction": "asc"}]` shape and converts it.
- `grid_add_columns` requires `required` on every column — the API validates it.
- `grid_copy` is asynchronous: the API returns operation metadata, not a ready copied grid.
- Grid mutations are **serialized per grid**: a second one issued while the first is
  still settling answers `409` `CONFLICTING_OPERATION` ("Conflicting operation in
  progress") and is **not applied**, so retrying after a pause is safe. Measured
  2026-08-05 over 8 pairs at each spacing: back to back it fires about a third of the
  time and clears in ~10s; at a 3s or 10s gap it never fired. An async `grid_copy`
  does **not** lock its source grid — a mutation right after one goes through.
  `scripts/contract_sweep.py` retries conflicts rather than reporting the lock as drift.
- `POST /grids/{id}/cells` responds with a **`cells`** key — unlike the row/column
  mutations, which answer with `results` (+ `revision`).

## Errors and limits

- Error responses come in **two envelope shapes** (`message` as string-or-null plus
  `details`, or as a list plus `level`); the client parses both and surfaces the API's
  own message in `WikiApiError`.
- No rate-limit headers are exposed (`X-RateLimit-*`/`Retry-After` absent).
