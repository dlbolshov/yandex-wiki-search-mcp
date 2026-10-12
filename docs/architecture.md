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
HTTP API: 33 tools (10 read, 23 write), 2 resources, three transports (stdio,
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
text block whose verbosity `TOOL_RESULT_TEXT` controls. The one exception is
`page_read_attachment`: registered with `structured_output=False`, it has no
output schema and returns a single content block — `ImageContent` for a
recognized image, `EmbeddedResource` otherwise — which ships once and which
`TOOL_RESULT_TEXT` leaves alone.

The same picture as an interactive diagram, every box linked to the source
lines it is drawn from:

[![Runtime architecture: MCP client, transport, middleware, tools, WikiClient and the Yandex Wiki API, with the optional OAuth provider, its store and the gated server-host filesystem](diagrams/en/architecture.svg)](https://dlbolshov.github.io/yandex-wiki-search-mcp/#en/architecture)

And one real call end to end — `page_edit` by slug over streamable HTTP, with
the retry rule that differs between its `GET` and its `POST`:

[![Sequence of one page_edit call: MCP SDK, call_tool, the tool, get_yandex_auth, WikiClient and the Wiki API, including the error branch](diagrams/en/page-edit.svg)](https://dlbolshov.github.io/yandex-wiki-search-mcp/#en/page-edit)

All diagrams, zoomable and with source links, are at **https://dlbolshov.github.io/yandex-wiki-search-mcp/**; see
[Diagrams](#diagrams) for what they are and how they are rebuilt.

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
  - The model validator enforces the invariants: at most one of
    `WIKI_ORG_ID`/`WIKI_CLOUD_ORG_ID`, a token unless OAuth is on, the OAuth
    triple when it is. Exactly one org id is required only with OAuth off;
    under OAuth zero is valid, because the organization can arrive per
    request (`?orgId=`/`?cloudOrgId=`), so `WikiClient._build_headers`
    re-checks it on every call and raises `WikiConfigError` naming both
    remedies. `include_local_uploads` is a property, not a field:
    `page_upload_attachment` and `page_download_attachment` read and write
    the *server's* filesystem, so they are only offered over stdio with
    OAuth off — the one setup where that filesystem is the caller's. Over
    HTTP it is a container or a shared host, and without OAuth the endpoint
    authenticates nobody, so every peer would get the server's disk.

### MCP layer

- **`mcp_wiki/mcp/server.py`** — assembly point. `create_mcp_server()` wires
  the lifespan, the optional OAuth provider and store, instructions, cache
  hints, middleware, custom routes (`/healthz`, the OAuth callback), then
  registers resources and tools. Four details that bite:
  - `WikiMCPServer.call_tool` decides what a failed tool tells the model.
    Since mcp 2.1 (python-sdk#3314) the SDK passes the error text on only
    for a `ToolError`. Anything else counts as a crash: the server logs it
    with its traceback and the model gets a bare `Error executing tool
    <name>`. So the tools refuse bad arguments with `ToolError`; a
    `ValueError` stays only inside pydantic validators, where the SDK turns
    it into an argument error. A `WikiError` comes from a layer that cannot
    import the SDK, so `call_tool` promotes it to a `ToolError` with the text
    mcp 2.0 sent. Every other exception stays masked, which is the half of
    the policy worth keeping. `uv.lock` pins mcp 2.0.0, which passes every
    message on, so only the `test-latest-mcp` job (see [CI](#ci)) sees a
    regression here.
  - It also post-processes results for `TOOL_RESULT_TEXT`
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
  (23 write tools; registered only when `WIKI_READ_ONLY=false`, and the
  local-filesystem pair — `page_upload_attachment`,
  `page_download_attachment` — only over stdio with OAuth off), `common.py` (locator
  resolution against the live API when a slug must become an id or vice
  versa). Write tools attach non-blocking `yfm_warnings` from `mcp_wiki/yfm.py`.
- **`mcp_wiki/yfm.py`** — dependency-free YFM lint (warnings only, never
  blocks a write) and the cheat-sheet text. Rules encode live-verified
  renderer behavior (`scripts/yfm_smoke.py`).

How `page_search` checks its arguments, which checks run before any HTTP, the
two modes `highlight` switches between, and the paging loop the agent runs:

[![Workflow of page_search: argument schema, slug_prefix and cursor checks, POST v1/search, the highlight modes and the agent's paging loop](diagrams/en/page-search.svg)](https://dlbolshov.github.io/yandex-wiki-search-mcp/#en/page-search)

### OAuth subsystem (`mcp_wiki/mcp/oauth/`)

Optional; only assembled when `OAUTH_ENABLED=true`. The server then acts as
an OAuth authorization server for MCP clients and proxies the actual identity
to Yandex OAuth:

```
client → /authorize ──► redirect to oauth.yandex.ru (key: server-generated state_id)
Yandex → /oauth/yandex/callback ──► pop state, mint an mcp_ code wrapping Yandex's code, redirect to client
client → /token ──► our code → Yandex /token exchange → access/refresh pair, stored under hashed keys
```

The Yandex code is not exchanged at the callback: it rides inside our
authorization code until the client redeems that at `/token`, and only then
does `exchange_authorization_code()` call Yandex. Token keys are SHA-256
hashes; the values still hold the raw token — in process memory for the
memory store, Fernet-encrypted for Redis.

- **`provider.py`** — the flow above. The storage key for a pending
  authorization is always server-generated (`secrets.token_hex`); the client's
  `state` is data echoed back on the final redirect, never a key. Breaking
  that rule was CVE-shaped: a predictable key let an attacker overwrite the
  pending record and collect the victim's code, PKCE included. A refusal
  from Yandex's `/token` (any non-200, on the code exchange or a refresh) is
  raised as `TokenError(invalid_grant)` carrying Yandex's status and error,
  and logged as a warning: that is the one exception the SDK's
  `TokenHandler` turns into a 400 OAuth error, and anything else reaches the
  client as a bare 500. On the code exchange `invalid_grant` is accurate
  whatever Yandex's reason, since loading the `mcp_` code already spent it.

  The provider also decides how long a dynamic registration lives, since
  `/register` is unauthenticated and whatever it keeps forever can be
  flooded on purpose. A confidential client goes with its secret: the SDK
  stamps `client_secret_expires_at` from `OAUTH_CLIENT_SECRET_EXPIRY_SECONDS`,
  tells the client and refuses the secret past it (`0`, stamped when the
  expiry is off, is RFC 7591's "never"). A public client
  (`token_endpoint_auth_method: none`) is issued no secret, so the SDK stamps
  nothing and checks nothing; the provider gives it the same setting as an
  idle window, restarted after every successful code exchange or refresh and
  never shorter than the refresh token just issued. A fixed lifetime would
  retire registrations in active use without warning — RFC 7591 has no field
  to announce one — and the SDK's own client answers the resulting
  `invalid_client` by reopening `/authorize` with the dead `client_id`, which
  strands the user on a "Client ID not found" page.
- **`store.py`** — the `OAuthStore` protocol and the shared
  `REFRESH_TOKEN_TTL_SECONDS` (31 days, Yandex's refresh-token lifetime).
  `save_client` takes its TTL from the provider, as `save_state` and
  `save_auth_code` do; no store reads an expiry off the client record, so the
  two cannot read one differently.
- **`stores/memory.py`** — dict store for single-process deployments. Since
  `/authorize` and `/register` are unauthenticated, everything expirable
  carries a deadline and writes run an amortized sweep (threshold re-aimed at
  twice the surviving size), so abandoned logins cannot grow the process
  without bound. Registrations included: `get_client` refuses an expired one
  itself rather than waiting for the sweep, as Redis has by then dropped the
  key. The SDK checks no expiry at all on a public client and a confidential
  one's only at `/token`, so `/authorize` would otherwise send the user
  through the Yandex login just to be refused at the end.
- **`stores/redis.py`** — production store. Only the access, refresh and
  refresh→access mapping keys are SHA-256 hashes; client, state and
  auth-code keys are the raw ids. Field-level encryption (`crypto.py`,
  versioned keys for rotation via `OAUTH_ENCRYPTION_KEYS`, which the Redis
  store requires) covers exactly the `token` and `client_secret` fields
  (`serializers.py`), so the pending state and the auth-code record — the
  wrapped `yandex_auth_code` included — are stored in the clear for their
  600 s / 300 s TTLs. TTLs are delegated to Redis, client registrations'
  included (with `OAUTH_CLIENT_SECRET_EXPIRY_SECONDS` empty a registration
  gets none). Single-use records — state and auth code — are claimed by the
  `DEL`, not the `GET`: two concurrent redemptions may both read one, but
  only the caller whose `DEL` removed the key gets it.
- **`types.py`** — the pydantic shapes for state, callback and code records.

The whole flow, drawn from the code in two parts — registration and login,
then the code exchange, a call with the Bearer and a refresh:

[![OAuth part 1: discovery, dynamic client registration and the browser login up to the mcp_ code](diagrams/en/oauth-login.svg)](https://dlbolshov.github.io/yandex-wiki-search-mcp/#en/oauth-login)

[![OAuth part 2: code exchange with PKCE, a tool call with the Bearer and token refresh](diagrams/en/oauth-tokens.svg)](https://dlbolshov.github.io/yandex-wiki-search-mcp/#en/oauth-tokens)

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
- **`custom/client.py`** — `aiohttp` client. One `_request_raw()` funnel applies
  auth headers (`OAuth`/`Bearer` scheme, IAM, or per-request token),
  organization headers via `select_org()`, retries what is safe to repeat,
  and maps failures to the error taxonomy. Retryable means GET by default,
  plus the two calls flagged `retryable=True` (`POST v1/search`,
  `PUT upload_part`), minus the attachment-download GETs, where a repeat
  re-transfers the whole file; only dropped connections and
  `429`/`502`/`503`/`504` are retried, never timeouts (exponential backoff
  with equal jitter; a `Retry-After` within the cap wins, a longer one fails
  fast). A grid `409 CONFLICTING_OPERATION` is not retried: the rejected
  mutation was not applied, and it surfaces as `GridConflict` with the
  recovery appended to its message. It also carries a per-response
  byte ceiling (resolved from the reply's Content-Type, since the download
  endpoint declares no Content-Length) and a `body_sink` that streams a
  response straight to a caller-supplied consumer instead of memory;
  `_request()` is the thin bytes-only wrapper the other endpoints use.
  Also owns the multi-step flows: upload sessions (create → parts → finish
  → attach), `page_clone`'s deferred-operation polling, and the atomic
  download-to-disk (`.part` file → fsync → link/replace). That last one
  runs every filesystem step on one per-call worker thread: the descriptor
  is never touched from the event loop, so a cancelled `await` cannot close
  it while another thread is still writing, and the cleanup queued behind
  an in-flight open sees the finished state rather than racing it.
- **`custom/errors.py`** — the taxonomy. `WikiError` → `WikiApiError`
  (HTTP-level, `build_api_error()` picks the subclass; `GridConflict` for the
  grid lock) plus `PageNotFound`/`GridNotFound`/`AttachmentNotFound` (the
  last mapped in the client, because the download endpoint answers a miss
  with a placeholder GIF instead of the JSON error envelope),
  `WikiOperationError` (a deferred operation failed *after* every HTTP
  exchange succeeded), `WikiTransportError` (timeouts and connection
  failures, with a non-empty message even for `TimeoutError`),
  `ResponseTooLarge` (a body past the caller's `max_bytes` ceiling — refused
  on `Content-Length` before reading, or one byte past the ceiling on a
  stream that declared none), `WikiLocalFileError` (a local path the
  attachment tools cannot use, keeping the `OSError` as `cause`),
  `WikiConfigError` (bad server setup), `WikiArgumentError` (a request the
  client refuses to send: a search `cursor` without `highlight`, an empty
  clone target, a page update asking for nothing or for opposite things;
  also a `ValueError`). Every failure meant for the caller to read is a
  `WikiError`, and the MCP layer relies on that to show its text.
- **`custom/mimes.py`** — `image_mime()`: the four formats a vision API will
  decode (PNG/JPEG/GIF/WebP), recognized by magic bytes and never by the
  wire's `Content-Type`. Lives in the Wiki layer because both attachment
  paths need it — the inline read to decide whether bytes may travel as an
  image block, the download-to-disk to report a `mimetype` that agrees with
  it — and the MCP layer may not be imported from here.
- **`custom/slugs.py`** — `normalize_slug()`: full Wiki URL or slug → bare
  slug. Lives here because the client needs it on every page call (see the
  layering rule).
- **`custom/anchors.py`** — the fallback behind `page_append_content`'s
  `anchor`: when the API answers `ANCHOR_NOT_FOUND`, the client inserts
  after the anchor in the raw markup and saves the page with `page_update`.
  Matches explicit anchors only — a heading's `{#id}`, an inline
  `#[text](id)`, an `{{anchor href="id"}}` macro; a heading without `{#id}`
  is not one.

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
| Docker image ↔ CI ↔ uv | `tests/test_dockerfile.py`: the Dockerfile's uv pin is exact, every `setup-uv` step pins the same release except the weekly canary's, which must float to `latest`, `required-version` grows no ceiling (the project sets none), no `uv.toml` overrides it, and the image's `uv sync` skips the `dev` group (see [Toolchain](#toolchain)) |
| bundle ↔ host | `tests/test_manifest.py`: `Settings` builds from the env the bundle passes (empty strings for what the user left blank), a platform override in `manifest.json` repeats the whole base `env`, every env keeps Python below 3.15, Windows keeps x86_64 Python, and every platform's `uv run` passes `--no-dev` ahead of the script name (see [Toolchain](#toolchain)) |

Layout mirrors the source tree (`tests/mcp/...`, `tests/wiki/...`). Shared
helpers: `tests/mcp/conftest.py` (the `client` fixture on SDK default
`mode="auto"`, i.e. the modern `2026-07-28` negotiation path;
`create_test_settings`; `make_test_lifespan`) and `tests/aioresponses_utils.py`.
Protocol model fields are snake_case (`structured_content`, `input_schema`);
camelCase works as a constructor kwarg but not for attribute access.

**Coverage is gated at 100%, statements and branches** (`--cov-branch`; CI
enforces the threshold on ubuntu — see [CI](#ci) for why not everywhere).
The policy that keeps this honest rather than performative:

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

`test.yml` runs four jobs on every push and PR:

- **lint** — `scripts/check_versions.py` (version consistency across release
  metadata), ruff check, ruff format check, ty, mypy.
- **install** — builds the wheel and installs it **with a fresh dependency
  resolve**, then constructs the server and counts its tools. This job exists
  because everything else installs from `uv.lock`, so the version ranges in
  `pyproject.toml` were once exercised nowhere — and 1.0.0 shipped
  uninstallable the day its SDK released a major version.
- **test-latest-mcp** — the whole suite with mcp moved to the newest release
  the range allows (`uv sync --dev --upgrade-package mcp`; mcp-types follows,
  everything else stays locked). `uv.lock` pins mcp 2.0.0, the floor of the
  range, so `test` covers that end and this job the end `uvx` and `pip`
  users get. It exists because mcp 2.1.0 changed what a failed tool shows
  the model (see `WikiMCPServer.call_tool` above): 43 tests failed there and
  passed on the lock, and `install` only counts tools.
- **test** — pytest across the OS × Python matrix (3.11–3.13); coverage
  uploaded to Codecov from one matrix entry. The **100% coverage gate is
  enforced on ubuntu only**, as a separate `coverage report --fail-under=100`
  step. Windows and macOS run every test they can, but a few tests — and the
  code paths only they exercise — are POSIX-only (mode bits, `fchmod`,
  fsyncing a directory descriptor). Those lines are unreachable on Windows,
  so a gate there could only be satisfied by excluding them from measurement
  on every OS, hiding real regressions in the process. The gate lives where
  all of the code is reachable, and `# pragma: no cover` stays reserved for
  genuinely platform-specific branches.

`release.yml` triggers on a version tag and on manual `workflow_dispatch`:
validates metadata, builds the wheel and the MCPB bundle, and builds **and
pushes** the Docker image to GHCR; the `publish` job (PyPI, GitHub release,
MCP registry) then runs only on a tag push. The GHCR push is not behind that
condition — it happens in `build-docker` on every run, so a manual dispatch
still pushes an image tagged from the ref it ran on (`:main` from main), and
on a tag the image is out before PyPI is attempted. `api-drift.yml` is the
weekly live sweep.

`uv-canary.yml` runs with the newest uv weekly, by hand, and on a pull request
that changes how the bundle is built or launched (`manifest.json`,
`.mcpbignore`, `pyproject.toml`, `uv.lock` or the workflow itself). It is the
one place CI does not use the pinned release. The `bundle` job runs on
ubuntu, macOS, Windows x64 and Windows ARM64. It packs the MCPB bundle, unpacks it and launches it the way Claude Desktop
does. It runs the unpacked manifest's own `mcp_config` command, args and env,
with `${__dirname}` and `${user_config.*}` substituted (a dummy token and org
id) and the working directory set to the bundle. stdin is at EOF, and
`UV_PYTHON_PREFERENCE=only-managed` with an empty `UV_PYTHON_INSTALL_DIR`
makes it a machine with no Python of its own. `UV_NO_BUILD=1` makes a missing
wheel fail instead of being compiled with the runner's toolchain, which a
Claude Desktop user does not have. The server must log `mcp_wiki starting`
and exit 0. The `suite` job runs `uv sync --locked --dev` and the tests on
ubuntu and macOS, on Python 3.13. The workflow exists because uv 0.13.0 broke the bundle twice
and CI saw neither break. `release.yml` validates and packs the bundle but
never runs it, and Dependabot cannot bump either uv pin (see
[Toolchain](#toolchain)). Both breaks reproduce against this workflow's
launcher. With the `<0.13` ceiling restored, uv refuses to run. Without
`UV_PYTHON`, it fetches Python 3.15 and fails to build `cffi`. A red run
means fixing compatibility first (the manifest, the lock or a range), then
moving every pin in one change.

`pages.yml` deploys `docs/diagrams/` to GitHub Pages on a push to main that
touches it, or by hand. It builds nothing: the HTML is committed, rebuilt
locally (see [Diagrams](#diagrams)).

## Release process

Versions live in **four files plus one tag string**: `pyproject.toml`,
`uv.lock` (regenerate, do not edit), `manifest.json`, `server.json` (two
`version` fields **and** the OCI image tag; `check_versions.py` checks all of
them). The release commit, by convention titled `Release vX.Y.Z`:

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
  tool tables and the "33 tools" claims in both READMEs are not.
- **`page_search.content` semantics**: field description in
  `wiki/proto/types/pages.py`, the tool description in `page_read.py`, and
  `build_instructions()` — three copies, one truth.
- **Attachment byte ceilings**: `MAX_INLINE_ATTACHMENT_BYTES` and
  `MAX_INLINE_IMAGE_BYTES` in `mcp/tools/page_read.py` are the truth; the tool
  description f-strings them, but both READMEs, both `api-notes` and
  `manifest.json` restate them as prose. Lower a constant and five documents
  start advertising a limit the server no longer enforces.
- **Server description**: single-sourced from package metadata at runtime,
  but `manifest.json` and `server.json` carry their own copies.
- **Bilingual pairs**: `README.md` ↔ `README_ru.md`, `api-notes.md` ↔
  `api-notes_ru.md`, this file ↔ `architecture_ru.md`.
- **Env var surface**: `settings.py` ↔ `.env.example` ↔ README configuration
  tables ↔ `manifest.json` user config.
- **Diagrams**: `docs/diagrams/src/{en,ru}/*.json` describe the code at the
  commit in their `meta.repository.revision`, and nothing re-checks them
  against later code. A change to what a diagram shows means updating both
  language sources and rebuilding — see [Diagrams](#diagrams).

## Scripts

| Script | Purpose |
|---|---|
| `check_versions.py` | Version consistency across release metadata; runs in CI |
| `contract_sweep.py` | Live sweep of every client method (writes!); weekly in CI |
| `token_probe.py` | Response-size measurements behind the token-diet decisions |
| `yfm_smoke.py` | Live YFM renderer probes behind the lint rules |
| `docs_probe.py` | The 2026-08 docs drop vs the wire, claim by claim (writes!); includes the hosted MCP tools/list |
| `probe_api*.sh`, `smoke.sh` | Ad-hoc curl probes from past investigations |
| `build_diagrams.py` | Rebuilds `docs/diagrams/` from its Archify sources (checks, HTML, SVG); local only |

## Diagrams

`docs/diagrams/` holds five [Archify](https://github.com/tt-a1i/archify)
diagrams in English and Russian: the runtime architecture, one `page_edit`
call, `page_search`'s checks and paging, and the OAuth flow in two parts.
`pages.yml` publishes the folder to **https://dlbolshov.github.io/yandex-wiki-search-mcp/**, whose index switches between
them and between the languages.

- `src/<lang>/<name>.json` — the source of truth: Archify's typed JSON, with
  every box citing repository line ranges at one pinned commit
  (`meta.repository`).
- `<lang>/<name>.html` — the checked, self-contained interactive diagram:
  zoom, search, upstream/downstream reach, a route probe, light/dark, export,
  and each box's cited lines one click away on GitHub.
- `<lang>/<name>.svg` — the viewer's own "SVG · Auto" export, which follows
  the reader's light/dark preference; this file embeds them.

To change one, edit both language sources — when the code moved, also the
cited ranges and `meta.repository.revision`, which must be a pushed commit
— and run `uv run python scripts/build_diagrams.py [name] [--lang en|ru]`.
For every source it runs Archify's `finalize` (schema, layout, source
evidence verified against that commit, a real-browser check), copies the
HTML in and re-exports the SVG through headless Chrome. It needs Archify
installed once (`npx skills add tt-a1i/archify -g`, Node 18+) and Chrome or
Chromium on `PATH`; neither is a project dependency, and CI never builds
diagrams. Commit the sources, HTML and SVG together.

## Toolchain

`uv` for everything (sync, lock, build, run), `Taskfile.yml` for shortcuts
(`task` = lock + format + check + test; `task test-cov` = the CI gate plus an
HTML report), ruff (lint + format), mypy **and** ty (two type checkers — ty
is the second opinion), pytest + pytest-asyncio. Python 3.11–3.13. The
package ships `py.typed`.

uv's own version is pinned twice and bounded nowhere. The Dockerfile's uv image
and every `setup-uv` step in CI (`version:`) name the same exact release, so
the image is built and CI tests with one known uv. A floating tag is out:
`:latest` broke the image build when it became uv 0.13.0, and only
`release.yml` builds the image, so nothing noticed before a release.
`pyproject.toml` sets no `[tool.uv] required-version`. uv enforces one in
nearly every command run inside a copy of this tree (all but `uvx`/`uv tool`
and `uv self`): a contributor's checkout, the unpacked Claude Desktop bundle
(its `uv run`), and builders that install from source, such as glama.ai. The
`<0.13` ceiling 1.5.0 added broke all of them the day uv 0.13.0 shipped, and
uv releases a breaking minor every three months or so. Its `>=0.11` floor
turned away uv 0.10, which runs the bundle fine, and kept nothing out that
needed keeping out. `tests/test_dockerfile.py` fails when the pins disagree,
when a ceiling comes back, or when a `uv.toml` appears to override
`pyproject.toml`. Moving to a new uv means changing the image tag and every
pinned `version:` input in one change.

Dependabot will not propose that change. The Dockerfile's `FROM` is
ARG-prefixed, and the github-actions ecosystem does not edit `with:` inputs.
The prompt comes from `uv-canary.yml` instead (see [CI](#ci)), the one
sanctioned float. Its `setup-uv` steps carry a `uv-canary` marker in the
comment on their `version:` line and must say `latest`. The test accepts the
marker in that workflow only, and holds every unmarked step to the pin. When
the canary goes red on a new uv, fix compatibility first, then move the pins.

The dev tools above live in `pyproject.toml`'s `dev` dependency group, which
uv installs unless told not to, so both shipped installs tell it: the
Dockerfile's builder runs `uv sync --locked --no-dev`, and `manifest.json`
launches `uv run --no-dev yandex-wiki-search-mcp`. Each gets the 46
distributions the server needs instead of 68. The image's `/venv` drops from
152 MB to 50 MB. The bundle's first launch no longer downloads mypy, ty and
ruff (about 34 MiB of wheels between them), each of which would also need a
wheel for every platform the bundle runs on. `--no-dev` has to come before the
script name, since uv hands anything after it to the server. The `win32`
override sets only `env` and so inherits these args: an override replaces only
the fields it sets. Runtime code must not import from the group. That includes
`httpx`: the SDK moved to `httpx2`, and the group declares `httpx` only for
starlette's test client. `tests/test_dockerfile.py` and `tests/test_manifest.py` fail when
either install drops `--no-dev`. Contributors and CI still get the group from
`uv sync`, `uv sync --dev` or a plain `uv run`.

The bundle's interpreter is the user's uv's choice, so `manifest.json` bounds
it with `UV_PYTHON` (3.11–3.14) in `mcp_config.env`. Since 0.13 uv downloads
Python 3.15 wherever no suitable interpreter exists, and several locked
packages (`cffi`, `pydantic-core`, `aiohttp`, `rpds-py`) have no 3.15 wheels.
Raise that bound once the lock does.

On Windows the bundle stays on x86_64 Python:
`mcp_config.platform_overrides.win32` adds `UV_PYTHON_ARCH=x86_64`. uv 0.13
prefers native ARM64 Python on Windows ARM64, and the locked `aiohttp` 3.13.3
and `cryptography` 46.0.4 have no `win_arm64` wheels (no `cryptography`
release has one), so its first `uv run` would have to build them from source.
uv 0.12 used x86_64 Python there, and on x64 Windows the setting changes
nothing. An override's `env` replaces the base `env` rather than merging into
it (mcpb's `getMcpConfigForManifest`), so the override repeats every
variable; `tests/test_manifest.py` fails when it drops one. The canary's
`windows-11-arm` leg launches the bundle with it.
