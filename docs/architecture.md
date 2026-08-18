# Architecture and development guide

**Русская версия: [architecture_ru.md](architecture_ru.md)**

This document is for developers reading or changing the code. It explains how the
server is put together, why it is put together that way, and where each kind of
change lands. It complements, not replaces:

| Document | Audience |
|---|---|
| [README.md](../README.md) | Users: installation, configuration, tool reference |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | The verification set to run before every commit |
| [CLAUDE.md](../CLAUDE.md) | Coding agents: condensed working rules |
| [api-notes.md](api-notes.md) | Live-verified Wiki API behavior, probes and their methodology |
| [CHANGELOG.md](../CHANGELOG.md) | What changed, release by release, with reasoning |
| [ROADMAP.md](../ROADMAP.md) | Decision history and milestone log (Russian) |

## The system at a glance

`yandex-wiki-search-mcp` is an MCP server in front of the public Yandex Wiki
HTTP API: 32 tools (10 read, 22 write), 2 resources, three transports (stdio,
streamable HTTP, SSE), one optional OAuth layer for multi-user HTTP
deployments.

A tool call travels like this:

```
MCP client
  │  stdio / streamable-http / sse
  ▼
MCPServer (mcp_wiki/mcp/server.py)
  │  middleware: DEBUG timing log → request stashed in a contextvar
  ▼
tool handler (mcp_wiki/mcp/tools/*)
  │  pydantic-validated params → get_yandex_auth() per-request override
  ▼
WikiClient (mcp_wiki/wiki/custom/client.py)
  │  headers ← select_org(); retries with backoff; error mapping
  ▼
https://api.wiki.yandex.net
```

The response comes back as a typed pydantic model, is serialized without
`None` fields, validated against the tool's output schema, and duplicated as a
text block whose verbosity `TOOL_RESULT_TEXT` controls.

## The two layers

The package is split into a domain layer and a presentation layer, and the
dependency between them points one way only:

- **`mcp_wiki/wiki/`** — the Yandex Wiki domain. The async HTTP client, the
  response models, the error taxonomy, slug handling. Knows nothing about MCP;
  it must be usable from a plain script (and is — `scripts/contract_sweep.py`
  drives it directly).
- **`mcp_wiki/mcp/`** — the MCP presentation. Server assembly, tools,
  resources, parameter models, OAuth. Imports the Wiki layer freely.

**The rule: `mcp_wiki.wiki` never imports from `mcp_wiki.mcp`.** This is not
aesthetic — a violation once closed an import cycle through
`mcp_wiki.wiki.__init__` that made `import mcp_wiki.mcp.utils` raise
`ImportError` as a first import, and the test suite could not see it because
conftest had already imported the package in a working order.
`tests/test_imports.py` imports every module in a subprocess to keep the
layering honest. When a helper is needed on both sides, it lives in the Wiki
layer and `mcp_wiki.mcp.utils` re-exports it (`normalize_slug` is the
precedent).

## Code map

### Entry point and configuration

- **`mcp_wiki/__main__.py`** — builds `Settings`, refuses to start on a
  suspected typo in the environment (see below), configures stderr logging,
  logs the effective configuration, then
  `create_mcp_server(settings).run(transport, **run_options(settings))`.
- **`mcp_wiki/settings.py`** — pydantic-settings over environment variables
  and a `.env` file. Two decisions worth knowing:
  - `extra="ignore"`: the `.env` file is a directory-level convention shared
    with other tools, so an unrelated `OPENAI_API_KEY` must not stop this
    server.
  - `suspicious_env_keys()`: the typo protection that strictness used to
    provide, repaired and extended to real environment variables. A key inside
    this server's namespaces (`WIKI_*`, `OAUTH_*`, `REDIS_*`, `MCP_*`,
    `TOOL_*`) that is difflib-close to a real field (`WIKI_READ_ONL`) stops
    startup with a did-you-mean; unrelated keys (`REDIS_URL`) pass.
  - The model validator enforces the invariants: exactly one of
    `WIKI_ORG_ID`/`WIKI_CLOUD_ORG_ID`, a token unless OAuth is on, the OAuth
    triple when it is. `include_local_uploads` is a property, not a field:
    `page_upload_attachment` reads the *server's* filesystem, so it is only
    offered outside multi-user OAuth deployments.

### MCP layer

- **`mcp_wiki/mcp/server.py`** — assembly point. `create_mcp_server()` wires
  the lifespan, the optional OAuth provider and store, instructions, cache
  hints, middleware, custom routes (`/healthz`, the OAuth callback), then
  registers resources and tools. Three details that bite:
  - `WikiMCPServer.call_tool` post-processes results for `TOOL_RESULT_TEXT`
    (`pretty`/`compact`/`none`) without touching structured content.
  - Transport keywords live in `run_options()`/`http_app_options()`, not the
    constructor — and **`host` must reach them**: the SDK defaults it to
    loopback and auto-arms DNS rebinding protection, which answers every MCP
    request behind a real hostname with `421` while `/healthz` still says
    `200`.
  - `server_version()`/`server_description()` read package metadata, so
    `pyproject.toml` stays the single source for both.
- **`mcp_wiki/mcp/context.py`** — `AppContext`, the lifespan payload: the
  `WikiProtocol` instance plus the web base URL.
- **`mcp_wiki/mcp/middleware.py`** — DEBUG-level log of every inbound message
  with serve time. Deliberately silent at INFO: a per-request line would stall
  a stdio server whose client does not drain stderr.
- **`mcp_wiki/mcp/request_ctx.py`** — a contextvar carrying the transport
  request of the message being handled. Exists because the SDK will not inject
  a `Context` into a static-URI resource, and `wiki-mcp://configuration` needs
  the query string. `Server.middleware` is provisional upstream, so this
  module and `middleware.py` are deliberately the only two places touching it.
- **`mcp_wiki/mcp/params.py`** — shared `Annotated` parameter types (IDs,
  slugs, cursors, `fetch_all`), request-body models for grid operations, and
  `build_instructions()`, which derives the server instructions from the same
  settings that gate tool registration — a read-only server describes only
  what it registered.
- **`mcp_wiki/mcp/utils.py`** — `get_yandex_auth()` (per-request token and
  organization override; prefers the handler's `Context`, falls back to the
  middleware-stashed request) and `resolve_page_locator()` (the
  exactly-one-of-`page_id`/`slug` contract every page tool shares).
- **`mcp_wiki/mcp/resources.py`** — two resources: `wiki-mcp://configuration`
  (reports the org pair through the same `select_org()` the client uses, so
  it can never report a combination no request carries) and
  `wiki-mcp://yfm-cheatsheet`.
- **`mcp_wiki/mcp/tools/`** — `page_read.py` (10 read tools), `page_write.py`
  (21 write tools; registered only when `WIKI_READ_ONLY=false`, and
  `page_upload_attachment` only when OAuth is off), `common.py` (locator
  resolution against the live API when a slug must become an id or vice
  versa). Write tools attach non-blocking `yfm_warnings` from `mcp_wiki/yfm.py`.
- **`mcp_wiki/yfm.py`** — dependency-free YFM lint (warnings only, never
  blocks a write) and the cheat-sheet text. Rules encode live-verified
  renderer behavior (`scripts/yfm_smoke.py`).

### OAuth subsystem (`mcp_wiki/mcp/oauth/`)

Optional; only assembled when `OAUTH_ENABLED=true`. The server then acts as
an OAuth authorization server for MCP clients and proxies the actual identity
to Yandex OAuth:

```
client → /authorize ──► redirect to oauth.yandex.ru (key: server-generated state_id)
Yandex → /oauth/yandex/callback ──► exchange code, mint our code, redirect to client
client → /token ──► our code → Yandex access/refresh token pair, stored hashed
```

- **`provider.py`** — the flow above. The storage key for a pending
  authorization is always server-generated (`secrets.token_hex`); the client's
  `state` is data echoed back on the final redirect, never a key. Breaking
  that rule was CVE-shaped: a predictable key let an attacker overwrite the
  pending record and collect the victim's code, PKCE included.
- **`store.py`** — the `OAuthStore` protocol and the shared
  `REFRESH_TOKEN_TTL_SECONDS` (31 days, Yandex's refresh-token lifetime).
- **`stores/memory.py`** — dict store for single-process deployments. Since
  `/authorize` and `/register` are unauthenticated, everything expirable
  carries a deadline and writes run an amortized sweep (threshold re-aimed at
  twice the surviving size), so abandoned logins cannot grow the process
  without bound.
- **`stores/redis.py`** — production store: hashed keys, field-level
  encryption (`crypto.py`, versioned keys for rotation via
  `OAUTH_ENCRYPTION_KEYS`), TTLs delegated to Redis. Client registrations
  expire with `client_secret_expires_at`, which the SDK stamps at `/register`
  (`OAUTH_CLIENT_SECRET_EXPIRY_SECONDS`).
- **`types.py`** — the pydantic shapes for state, callback and code records.

### Wiki layer (`mcp_wiki/wiki/`)

- **`proto/pages.py`** — `WikiProtocol`, the interface tools program against.
  Tools never see `WikiClient` directly; tests substitute an `AsyncMock` with
  this protocol's surface.
- **`proto/common.py`** — `YandexAuth` (per-request credentials) and
  `select_org()`: per-request auth replaces the organization **as a unit**,
  never mixing a request's `cloud_org_id` with the server-wide `org_id`. The
  client builds headers from it and the configuration resource reports from
  it, so they cannot drift apart.
- **`proto/types/pages.py`** — response models. `BaseWikiModel` drops `None`
  fields at serialization (token diet for LLM output) and strips `title` noise
  from generated JSON schemas; `DynamicWikiModel` additionally keeps unknown
  keys through round-trips. Grid cell/row/column shapes live here too.
- **`custom/client.py`** — `aiohttp` client. One `_request()` funnel applies
  auth headers (`OAuth`/`Bearer` scheme, IAM, or per-request token),
  organization headers via `select_org()`, retries idempotent failures
  (exponential backoff with equal jitter, `Retry-After` honored up to a cap,
  grid `409 CONFLICTING_OPERATION` retried as a lock rather than an error),
  and maps failures to the error taxonomy. Also owns the multi-step flows:
  upload sessions (create → parts → finish → attach) and `page_clone`'s
  deferred-operation polling.
- **`custom/errors.py`** — the taxonomy. `WikiError` → `WikiApiError`
  (HTTP-level, `build_api_error()` picks the subclass; `GridConflict` for the
  grid lock) plus `PageNotFound`/`GridNotFound`, `WikiOperationError` (a
  deferred operation failed *after* every HTTP exchange succeeded),
  `WikiTransportError` (timeouts and connection failures, with a non-empty
  message even for `TimeoutError`), `WikiConfigError` (bad server setup).
- **`custom/slugs.py`** — `normalize_slug()`: full Wiki URL or slug → bare
  slug. Lives here because the client needs it on every page call (see the
  layering rule).
- **`custom/anchors.py`** — anchor-targeted insertion for
  `page_append_content` (heading `{#id}`, inline anchors, plain headings).

## One wire contract worth memorizing

`GET /pages/descendants?slug=` with an **empty** slug means "the whole
organization" — a deliberate API contract, not lax input handling (an
unresolvable slug 404s; omitting the parameter is a 400). Verified live
2026-08-10, see [api-notes.md](api-notes.md). `page_get_descendants(from_root=true)`
is the only caller that sends it. Do not "fix" the empty slug away in
`WikiClient.page_get_descendants`; `resolve_page_locator` still rejects it for
every other tool, where the API genuinely needs a page.

## Testing

The suite runs against mocks at every seam the architecture defines:

| Seam | Technique |
|---|---|
| MCP protocol ↔ tools | in-memory `Client` from the SDK against a real `MCPServer`; tools see an `AsyncMock` `WikiProtocol` |
| tools ↔ Wiki client | `WikiProtocol` is the contract; write tools are tested for exact call arguments |
| Wiki client ↔ HTTP | `aioresponses`; assertions on headers, query params and bodies as aiohttp would send them |
| OAuth ↔ Redis | `fakeredis` |
| package ↔ interpreter | `tests/test_imports.py` imports every module in a subprocess |

Layout mirrors the source tree (`tests/mcp/...`, `tests/wiki/...`). Shared
helpers: `tests/mcp/conftest.py` (the `client` fixture on SDK default
`mode="auto"`, i.e. the modern `2026-07-28` negotiation path;
`create_test_settings`; `make_test_lifespan`) and `tests/aioresponses_utils.py`.
Protocol model fields are snake_case (`structured_content`, `input_schema`);
camelCase works as a constructor kwarg but not for attribute access.

**Coverage is gated at 100%, statements and branches** (`--cov-branch
--cov-fail-under=100`, enforced in CI). The policy that keeps this honest
rather than performative:

- A genuinely unreachable line carries `# pragma: no cover` **with the reason
  in the comment** (type-narrowing after `resolve_page_locator`, serializer
  fallbacks pydantic never triggers, the unreachable tail of the retry loop).
- Never delete or weaken a test to keep the gate green; close the gap or
  justify the pragma.
- `if __name__ == "__main__":` and `TYPE_CHECKING` blocks are excluded
  globally in `pyproject.toml`.

Beyond the unit suite there is a **live layer**: `scripts/contract_sweep.py`
calls every `WikiClient` method against a real organization and reports
validation mismatches and undeclared keys. It exists because the search
endpoint silently changed contract once (back when it was undocumented), and it caught `page_move`
being a silent no-op before that tool could ship. It **writes** to the wiki
under the given base slug — use a scratch spot in your personal section. The
`api-drift.yml` workflow runs it weekly when `DRIFT_*` secrets are configured;
findings belong in [api-notes.md](api-notes.md).

## CI

`test.yml` runs three jobs on every push and PR:

- **lint** — `scripts/check_versions.py` (version consistency across release
  metadata), ruff check, ruff format check, ty, mypy.
- **install** — builds the wheel and installs it **with a fresh dependency
  resolve**, then constructs the server and counts its tools. This job exists
  because everything else installs from `uv.lock`, so the version ranges in
  `pyproject.toml` were once exercised nowhere — and 1.0.0 shipped
  uninstallable the day its SDK released a major version.
- **test** — pytest with the 100% gate across the OS × Python matrix
  (3.11–3.13); coverage uploaded to Codecov from one cell.

`release.yml` triggers on a version tag: validates metadata, builds the wheel,
the MCPB bundle and the Docker image, then publishes (PyPI, GitHub release,
GHCR, MCP registry). `api-drift.yml` is the weekly live sweep.

## Release process

Versions live in **four files plus one tag string**: `pyproject.toml`,
`uv.lock` (regenerate, do not edit), `manifest.json`, `server.json` (two
`version` fields **and** the OCI image tag — the one `check_versions.py` does
not cover). The release commit, by convention titled `Release vX.Y.Z`:

1. Bump all of the above; run `uv lock`.
2. Promote `## [Unreleased]` to `## [X.Y.Z] - date` in `CHANGELOG.md`.
3. Append the dated entry to ROADMAP's execution log.
4. Run the full verification set, including the install smoke
   (`uv build` → install the wheel into a fresh venv → construct the server).
5. Commit, push, wait for green CI, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

Since 1.0.0 the tool surface is a compatibility promise: breaking changes
mean a major bump. PyPI publishing is irreversible — tag only on green.

## Cross-file sync points

Things that are asserted nowhere (or only partially) and must be kept in sync
by hand:

- **Tool list**: `manifest.json` ↔ registered surface — test-pinned; README
  tool tables and the "32 tools" claims in both READMEs are not.
- **`page_search.content` semantics**: field description in
  `wiki/proto/types/pages.py`, the tool description in `page_read.py`, and
  `build_instructions()` — three copies, one truth.
- **Server description**: single-sourced from package metadata at runtime,
  but `manifest.json` and `server.json` carry their own copies.
- **Bilingual pairs**: `README.md` ↔ `README_ru.md`, `api-notes.md` ↔
  `api-notes_ru.md`, this file ↔ `architecture_ru.md`.
- **Env var surface**: `settings.py` ↔ `.env.example` ↔ README configuration
  tables ↔ `manifest.json` user config.

## Scripts

| Script | Purpose |
|---|---|
| `check_versions.py` | Version consistency across release metadata; runs in CI |
| `contract_sweep.py` | Live sweep of every client method (writes!); weekly in CI |
| `token_probe.py` | Response-size measurements behind the token-diet decisions |
| `yfm_smoke.py` | Live YFM renderer probes behind the lint rules |
| `docs_probe.py` | The 2026-08 docs drop vs the wire, claim by claim (writes!); includes the hosted MCP tools/list |
| `probe_api*.sh`, `smoke.sh` | Ad-hoc curl probes from past investigations |

## Toolchain

`uv` for everything (sync, lock, build, run), `Taskfile.yml` for shortcuts
(`task` = lock + format + check + test; `task test-cov` = the CI gate plus an
HTML report), ruff (lint + format), mypy **and** ty (two type checkers — ty
is the second opinion), pytest + pytest-asyncio. Python 3.11–3.13. The
package ships `py.typed`.
