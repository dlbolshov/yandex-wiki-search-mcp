# Changelog

All notable changes to this project are documented in this file.

## [1.4.0] - 2026-08-21

Attachments in both directions: pictures into the conversation, files onto the
disk. Tool surface grows to 33.

### Added
- `page_read_attachment` returns **images as a native MCP image block** — vision-capable clients render them and models see them, where a base64 blob was opaque to both, and the price is right: a vision block costs `ceil(w/28) * ceil(h/28)` visual tokens, capped in the low thousands, against several hundred thousand for the same bytes as base64 text. Whether an attachment *is* an image is decided by its **magic bytes**, never by the wire's `Content-Type`: only PNG/JPEG/GIF/WebP become image blocks, because an `ImageContent` the vision API cannot decode does not degrade to "the model sees nothing" — the host's next model call fails with `Could not process image` and, since hosts retry the same tool call, the session dies (anthropics/claude-code#28279). So an SVG arrives as text, which is what it is anyway, and a mislabelled or empty file cannot masquerade as a picture. Images get a 2 MiB ceiling against the base 128 KiB, sized so a 4K PNG screenshot fits while staying far under the API's hard 10 MB-of-base64 limit; past roughly 2576 px on the long edge the API downscales, so more bytes buy nothing. The header still picks the ceiling — it is the only signal before the body, since the endpoint sends no `Content-Length` — and an uninformative one (`octet-stream`, or none) gets the image budget so the magic-byte check can actually run on files above 128 KiB
- `page_download_attachment` — the counterpart for everything too big or not worth reading: streams an attachment to a local file in 1 MiB chunks (`page_id`/`slug` + `file_id` + `save_to`), uncapped, nothing enters the conversation. The write is atomic: a `.part` file beside the target, `fsync`, then a rename. On POSIX it is also durable — an `fsync` of the directory afterwards makes the rename itself survive a crash; on Windows that step is skipped, because `os.open` cannot open a directory there, so the entry's durability is whatever the filesystem decides. Without `overwrite` the commit prefers `link`+`unlink` over `replace`, which makes the refusal the kernel's at the instant of creation rather than a check that ran minutes earlier; filesystems that cannot hardlink at all (FAT/exFAT, some network mounts) fall back to a fresh existence check plus a rename, which reopens a narrow race but beats failing every such download after the transfer already ran. The file lands with the permissions a plain write would give (`0666 & ~umask` — never executable, and `O_BINARY` so Windows does not translate newlines into the bytes); replacing an existing file keeps that file's own mode on POSIX, where mode means something. The `.part` and the parent directories are created on the first chunk, so a 404 leaves the caller's filesystem untouched. Returns `{page_id, file_id, path, size_bytes, mimetype}`. Annotated destructive (it writes to the caller's disk), registered outside `WIKI_READ_ONLY` and gated with `page_upload_attachment` under OAuth for the same reason in the other direction: `save_to` would name paths on the shared server's filesystem, not the caller's
- `WikiClient.page_read_attachment_bytes` returns `AttachmentContent` (bytes + the wire's mime) and `WikiClient.page_download_attachment` drives the streaming path via a `body_sink` on the raw request layer — a sink is refused on anything retryable, since a retry would append a second copy after the first attempt's chunks. Each client method now carries the name of the tool it backs, which the two did not before: the disk-writing tool and the memory-reading client method were both called `page_download_attachment`

### Changed
- `page_read_attachment`'s result shape changed for images: an `ImageContent` block instead of the `EmbeddedResource` (carrying `uri = wiki-mcp://pages/{id}/attachments/{fid}`) it always returned in 1.3.0. A consumer branching on `isinstance(block, EmbeddedResource)` or keying results by resource URI stops matching for image attachments; text and other binaries are unchanged. The per-call ceiling for images also rises from 128 KiB to 2 MiB
- `page_upload_attachment` now expands `~` in `file_path` and raises `WikiLocalFileError` instead of a builtin `FileNotFoundError`, matching its download counterpart: the two filesystem tools disagreed on what a path meant and on which exception a bad one produced
- `AttachmentDownloadResult`/`AttachmentContent` spell the mime field `mimetype`, matching `WikiAttachment.mimetype` from the attachment listing — one attribute name for one concept across the tool surface

### Internal
- The contract sweep covers the download-to-disk path end-to-end: bytes on disk equal bytes uploaded, no `.part` leftovers, and the two attachment paths must agree on `mimetype` — checked as agreement rather than against a literal, so the row needs no assumption about what the API sends. Each of the two also gets its own SKIP row when the upload produces nothing; the sweep's final gate only inspects rows that exist, so a tool with no row at all left it green having never run
- The streamed download no longer runs under the session's 30-second *total* timeout, which aiohttp applies to the body read as well — so anything larger than roughly thirty seconds of link speed was permanently unfetchable, for a tool whose whole point is large artifacts. It uses a stall detector (`sock_read`) instead, which fires when the peer stops sending and never because the file is big
- Local-filesystem failures raise the new `WikiLocalFileError` instead of escaping as bare `OSError`/`RuntimeError`: a directory as `save_to` (refused up front now, with advice that works, rather than after a full transfer), a parent path component that is a file, ENOSPC mid-write, and a target name long enough that the `.part` suffix overflowed `NAME_MAX`
- `_request()` is now a thin wrapper over `_request_raw()`, which owns the per-response ceiling and the streaming sink; `_WireReply` folded into the public `AttachmentContent`, and the ceiling argument is callable-only rather than int-or-callable. The three loops that pulled bytes off a response collapsed into one `_drain()` over `iter_chunked` — the two older ones each hand-rolled the same "`read(n)` is a partial read" rule and differed by an off-by-one nobody could tell was deliberate
- Image recognition moved to `wiki/custom/mimes.py`, beside `slugs.py` and `anchors.py`, because both attachment paths need it: the inline read decides whether bytes may travel as an image block, and the download-to-disk path now sniffs its first chunk so one file does not report `image/png` when read and `application/octet-stream` when saved. Keeping it in the MCP layer would have meant importing `mcp_wiki.mcp` from `mcp_wiki.wiki` — the one direction the layering forbids — or a second copy of the table
- `test_tilde_expands_to_the_home_directory` sets `USERPROFILE` alongside `HOME`: `ntpath.expanduser` ignores `HOME`, so on Windows CI the test wrote its fixture into the runner's real profile directory and then failed the assert
- The download-to-disk path now runs every filesystem step on one per-call worker thread instead of a `threading.Lock` plus a registry list, shut down with `wait=False` so the waiting never happens on the event loop (a `with` block's implicit `shutdown(wait=True)` froze the whole server for the length of an in-flight write — 951 ms against a one-second stub, unbounded on a hung mount; the `.part` is now cleaned up a moment after the call returns instead): the descriptor is never touched from the event loop, so a cancelled `await` can no longer close it while another thread is still writing (proven before the fix by an orphaned write hitting `EBADF`, and its silent twin — the same race won differently — writing attachment bytes into whatever reused the number). Cleanup is queued behind any in-flight open, so it sees the finished state rather than racing it, and both sides of that race now have tests
- A failure after the rename no longer reports the download as failed: past that point the bytes are in place and correct, so an unflushable directory — or a `.part` that will not unlink, the case missed the first time — is logged rather than raised. It used to tell the caller to retry a file that was already complete, and their retry then found their own new file and demanded `overwrite=true`
- `_drain()` asks for `min(limit + 1, 1 MiB)` rather than a flat chunk: sharing the download's chunk size let a 64 KiB error-body ceiling materialize a whole megabyte's worth before the check could fire, quietly loosening the one guarantee `ResponseTooLarge` exists to make
- A cleanup that cannot be scheduled is logged instead of escaping: `ThreadPoolExecutor.submit` raises `RuntimeError` at interpreter teardown, and letting that out of the `except BaseException` replaced the transfer's own exception — a task swallowing its own cancellation. Not run inline either, because a synchronous discard would close the descriptor from the event loop while the worker may still be writing to it
- The oversize refusal is shaped in the tool for the common case too. `ResponseTooLarge` from the client was only reshaped on one branch, so an image past the image budget reached the agent as a bare transport string quoting a limit the tool description never mentions
- Windows: the `.part` is opened with `O_BINARY` (without it the CRT's text mode rewrote every `\n` in the body, corrupting every image, PDF and archive saved there — a regression from replacing `tempfile`, which sets the flag itself); a parent path component that is a regular file is now detected there too, where Win32 reports it as ENOENT rather than ENOTDIR; the `.part` name is budgeted against `MAX_PATH`, not just `NAME_MAX` — against 259, since MAX_PATH counts the terminating NUL and the first arithmetic landed on exactly 260, and a parent too long to leave room for any stem is refused with a reason instead of an `ERROR_PATH_NOT_FOUND`; and the hardlink fallback recognizes `ENODEV`/`ENOTSUP`, which is what CPython maps `ERROR_NOT_SUPPORTED` to — without them the fallback written for FAT/exFAT and SMB never engaged on the platform that has them
- `os.fchmod` moved inside `_open_part`'s `try`: on the filesystems the hardlink fallback exists for it answers EPERM, and it used to escape as a bare `PermissionError` with the descriptor leaked and the `.part` orphaned
- The 100% coverage gate now runs on ubuntu only, as a separate CI step; every OS still runs every test it can. A few tests — and the code paths only they exercise — are POSIX-only (mode bits, `fchmod`, fsyncing a directory descriptor), so demanding 100% on Windows too could only be met by excluding those paths from measurement on every OS, and a whole-function exclusion on `_fsync_directory` turned out to hide that *no* test asserted the directory fsync happens at all — replacing its body with `return` kept the suite green at 100%. The exclusions are back to genuinely platform-specific branches, and `test_a_finished_download_flushes_the_directory_entry` now pins the durability guarantee the way `test_a_full_disk_at_fsync_is_a_wiki_error` pins the data fsync
- The `.part` name arithmetic is pinned by unit tests that force `os.name` rather than by a download test on the platform: the one long-name test builds a target path past `MAX_PATH` itself, so it only ever passed on a Windows box with long paths enabled — where the budget it appeared to validate does not apply
- `docs/api-notes.md` (EN/RU): the download endpoint's header contract recorded — precise `Content-Type`, no `Content-Length`, no `Content-Disposition`, chunked at any size (probed 2026-08-19)

## [1.3.0] - 2026-08-18

In August 2026 Yandex published a full Wiki API reference (the search endpoint
included, undocumented until then) and its own hosted MCP server; every claim was
checked against the live API before being absorbed — first into the docs, then into
the tools.

### Added
- `page_search` filters moved into the search backend and grew: `slug_prefix` is forwarded as the API's `cluster` filter (deep prefixes like `tech-doc/ml` work, matching on path segments — `a/b` does not leak `a/bc` — and including the cluster page itself; an unknown prefix yields an empty result, and one that normalizes to empty is still rejected before any HTTP), `result_type` as `filters.type`, and the new `created_between`/`modified_between` date intervals filter by creation/modification time. The wire matches `cluster` **literally** — mixed case or a stray slash answers 200 with zero results while `GET /pages?slug=` resolves all three spellings (probed 2026-08-18) — so `WikiClient.page_search` normalizes it like every other slug-shaped argument. Filtering happens before the result limit, so a filtered search no longer loses matches to it — the old "combine filters with limit=50" advice is gone along with the client-side sieve. Both interval bounds are required by the wire (`from` alone is a 400 `SEARCH_BAD_REQUEST`); the schema requires them upfront so the wire error never happens
- `page_search` takes `highlight=true`, which wraps query matches inside `content` excerpts in `<em>…</em>` — the excerpt is otherwise unmarked, and the tool description, output schema and server instructions now say exactly that. The documented-but-dead search knobs stay unexposed: `cursor` is never satisfiable, `order_by` changes nothing (probed 2026-08-11), and `show_obsolete` — probed once the reference described it — turned out to be the third dead one: obsolete pages come back regardless of its value (2026-08-18)
- `page_search` filters by author: `authors` takes a list of `{uid, cloud_uid}` identities matched against the page owner — either field alone works, several entries OR together, and `user_get_current` supplies the caller's own ids, turning "find my pages about X" into one call after another (verified live 2026-08-18; an unknown identity yields an empty result). The schema rejects what the wire would silently ignore: an empty `authors` list, an identity without either id, and blank-string ids — each would search the whole Wiki while reading as an applied filter
- `page_read_attachment` — reads an attachment's content into the conversation as an `EmbeddedResource` (nothing is saved anywhere): text arrives as text — with a NUL-byte check so BOM-less UTF-16, which decodes "cleanly" as UTF-8, travels as a blob instead of mojibake — anything else base64-encoded. Capped at 128 KiB: the cap protects the model's context window, and an embedded resource ships once where a pydantic model would ship twice (`structured_content` plus its spec-recommended text mirror). The ceiling is enforced by the client **before the body is read** — `WikiClient._request` takes `max_bytes`, checks `Content-Length` upfront and otherwise drains the stream to one byte past the cap (aiohttp's `read(n)` returns only what is buffered, so the drain is a loop) — raising the new `ResponseTooLarge` with a pointer to the `download_url` that `page_get_attachments` already returns; error bodies are truncated at a separate ceiling instead, keeping the API's own explanation. The download is never retried (a repeat re-transfers the whole file), and a missing attachment — a 404 with a **placeholder GIF body** instead of the JSON error envelope — maps to the new `AttachmentNotFound`. Registered as a read tool, so it survives `WIKI_READ_ONLY=true`; under OAuth it is safe by construction — the bytes travel in the response, the server's filesystem is never involved (unlike upload, which stays gated)
- `user_get_current` — `GET /users/me` as a tool: `username`, `home_cluster` (the caller's personal-section slug), identity and organization ids. Turns "create it in my section" from a guess into a lookup; available in read-only mode
- `page_delete_comment` — deletes a comment and returns the page's updated `comments_count` on top of a client-built `{page_id, comment_id, deleted}` floor, so an empty or reshaped body cannot read as a successful call with no evidence in it. Comment ids come from `page_get_comments`
- `page_delete_attachment` — deletes an attachment; the API answers a bare 204, so the tool returns a `{page_id, file_id, deleted}` acknowledgment with any response body merged in rather than discarded (the `grid_delete` precedent — a body the API starts sending reaches the model where the contract sweep watches for drift). The description warns that any file macro referencing the attachment stays behind, broken
- `page_update` sets and clears **redirects**: `redirect_to_page_id` points the page at another page, `clear_redirect` removes it, mutually exclusive, each valid alone or alongside `title`/`content`. The redirect state reads back via `page_get` with `fields=["redirect"]` as `{page_id, redirect_target}` (verified live end-to-end: set → read → clear)
- On the two delete endpoints a 404 is deliberately **not** mapped to `PageNotFound`: the address names two resources (page and comment/file), the mapping would blame the page when only the sub-resource is missing, and the API's own envelope already says which one it was ("No Comment matches the given query.")
- `page_edit` — partial page editing by exact-text replacements, the one official-MCP feature this server lacked. Not an API endpoint (the API offers only full update and append): the tool reads the current content, applies `{old_text, new_text, replace_all}` entries sequentially (each sees the content as edited by the preceding ones), and writes the result back with a single update. A missing match, or an ambiguous one without `replace_all`, fails the whole call **before anything is written** — the ambiguity error reports the occurrence count and line numbers (capped at 5) instead of echoing page fragments, following the `str_replace_editor` convention; the schema itself rejects a no-op entry (`old_text == new_text`) and an empty replacement list. The response is a compact acknowledgment (`occurrences_replaced`, `yfm_warnings`) — echoing content back would spend the tokens the tool exists to save. Pages have no revisions, so the read-modify-write is not atomic: the write passes `allow_merge=True` by default (also exposed, with `is_silent`), asking Wiki to three-way-merge a concurrent edit rather than overwrite it — the same flag `page_append_content`'s anchor fallback passes for the same reason — and the description says so plainly. Deliberately **not** annotated idempotent: an insertion-shaped replacement (`"## Setup"` → `"## Setup\n\n…"`) still matches its own output, so a blind retry would insert twice. Verified live end-to-end: two replacements applied and read back verbatim, ambiguity refused with line numbers

### Internal
- `scripts/docs_probe.py` — a new live probe that walks the 2026-08 documentation drop claim by claim on both a full-scope and a `wiki:read`-only token, and captures the hosted MCP server's `tools/list`. Verdicts, in short: search filters (`type`/`cluster`/date intervals/`authors`) and `highlight` are live, search `cursor`, `order_by` and `show_obsolete` are documented but dead, OAuth scopes are documented but still not enforced (the read-only token wrote, HTTP 200), attachment download/deletion, comment deletion and redirect-via-update all work, the comment thread endpoint answers 200 but returned an empty list for a root comment with a live reply
- The weekly contract sweep covers the five new client methods: comment deletion, attachment download (asserting the payload round-trips byte-for-byte, as a `REPORT` row that can actually fail the run) and deletion, the set → read back → clear redirect cycle, `user_get_current` (with the identity keys it declares watched for drift rather than suppressed as ride-alongs), plus a filtered-and-highlighted search for the sweep's own fixtures by their author and a live pin of the `cluster` segment-boundary contract; skipped preconditions surface as `SKIP` rows instead of silent `if` guards — all green on the 2026-08-18 run
- The sweep also probes the live contract `page_edit` leans on: GET content, string-replace, PUT back, GET again — asserting the edited content reads back **verbatim** (no server-side markup normalization). Green on the 2026-08-18 run
- `docs/api-notes.md` (EN/RU) reframed for the era of an official reference: the notes no longer restate what the reference covers — they track where the wire and the docs disagree and what the docs leave out. Stale claims removed ("no server-side filtering", "the only server-side text filter", "undocumented `is_downloadable`"), findings above recorded with dates
- README comparison table (EN/RU) now includes Yandex's official hosted MCP server (`wiki-mcp-server` 1.28.1, tool list captured live 2026-08-11): no search tool, no attachment upload, no read-only mode, no typed output schemas, per-user token pasted into static headers; best-doctor moved to the "Also worth knowing" list to keep the table at five columns
- ROADMAP gains M7 (v1.3.0) — adopt the newly documented API surface: server-side search filters + `highlight`, comment deletion, attachment download/deletion, redirect via `page_update`; per-page access management, the thread endpoint and `actuality` are explicitly left out
- "Undocumented search endpoint" wording adjusted across READMEs, architecture docs and credits — it is "undocumented until August 2026" now
- `validate_page_update_args` lives once, beside `WikiProtocol`, instead of byte-identically in both the `page_update` tool and the client

## [1.2.1] - 2026-08-11

Branding and packaging metadata only — no change to the tool surface or server
behavior, hence the patch bump.

### Added
- The project has a logo: an original mark (one continuous stroke folded four times — the fold from MCP's visual language, the horizontal runs a wiki page's text lines), drawn from scratch so it reproduces neither Yandex Wiki nor MCP branding. Committed assets and a design guide live in `docs/assets/logo/`; the full variant set is regenerable from `build.py` there
- The MCPB bundle declares an `icon` and `server.json` declares registry `icons` (PNG 512 plus scalable SVG, per the 2025-12-11 schema), so MCP catalogs and clients that render icons stop showing a blank tile for this server
- READMEs open with a centered header — logo, title, badges — and carry an explicit "unofficial project" line up top plus a Trademarks section at the bottom: "Yandex" and "Yandex Wiki" are used nominatively, and the logo is an independent mark
- Per-language demo gifs: the English README shows an English session, the Russian one a Russian session, replacing the single shared recording

### Fixed
- README images now use absolute URLs, so PyPI renders them — the demo gif had been invisible there since 0.x, because PyPI does not resolve repository-relative paths

### Internal
- ruff, ty and mypy skip `docs/assets/logo/build.py` — the logo generator is a vendored design artifact with its own compact style, and it broke all four lint steps on `main`. mypy additionally skips the gitignored `local/` scratch directory, the one checker that walks it
- Demo gifs are excluded from the MCPB bundle (`.mcpbignore`): `docs/` ships in the bundle for the icon's sake, and the recordings were adding ~6 MB that no MCP client ever displays

## [1.2.0] - 2026-08-10

### Added
- `page_get_descendants` takes `from_root=true`, which enumerates the **whole Wiki** instead of one page's subtree — top-level pages included. The capability was always in the API and only this server withheld it: `GET /pages/descendants?slug=` (empty) answers `200` and drains to every page in the organization, while `resolve_page_locator` rejected the empty slug before any HTTP happened, so the tool insisted on a starting slug that a client had no way to discover. Live verification against a production organization (2026-08-10) settles that the empty slug is a deliberate contract rather than a quirk of bad input: an unresolvable slug answers `404`, `?slug=tech-doc` returns 1064 items against the root walk's 1065 with that prefix (the extra one being `tech-doc` itself), and of ~200 search hits across four queries not one was missing from the root walk. It is a flag rather than "omit both locators" on purpose — the walk is thousands of pages, and a forgotten argument must not become one. Combining it with `page_id`/`slug` is refused, and omitting everything still errors as before — the error now names `from_root=true` as the way in, since the caller hitting it is exactly the one with no slug to offer. Reaching for the root by slug instead — `slug="/"` or `slug=""`, the obvious guess, both of which normalize to the empty slug — is answered with the same pointer rather than a bare "Slug must not be empty"; passing a page_id alongside still reports the exclusivity violation, which is the real error there. `include_self` is dropped on the root walk rather than forwarded: its description promises it is ignored (the root is not a page), and that promise holds on this side of the wire instead of relying on the API to keep ignoring it. And should a root walk ever answer `404` — the contract rules it out — it surfaces as the API error it is, not as `PageNotFound("")` naming a page that does not exist
- Documented consequence worth stating plainly: **`page_search` is not the only way to find things.** A client with no starting slug can now inventory an organization, then read what it found

### Changed
- `page_search` now describes what `content` on a result actually is, in the tool description, in the output schema for the field itself, and in the server instructions — the same question kept being asked, and the honest answer was nowhere. It is a text excerpt capped at ~510 characters, cut from wherever the match sits (only 9 of 50 sampled excerpts began at the start of the page; others began ~600–3000 characters in), with no highlighting and no guarantee the query terms are inside it at all (29 of 50 contained the term that matched them). The line breaks and tabs in it are the source page's own layout — table cells arrive tab-separated — and not separators between fragments, which is the detail most likely to be misread as "several snippets joined by newlines". Measured over 196 results across four queries and cross-checked against the pages themselves; `docs/api-notes.md` carries the full numbers
- `page_get_descendants` warns in its own description that a root walk is expensive and that `fetch_all` stops at its ~500-item cap with `truncated=true`, so an agent picks a section slug when it has one

### Internal
- `docs/api-notes.md` gains the root-traversal contract (including that there is **no root page** — `homepage` is an ordinary page, and the top level is a set of sibling slugs), the undocumented by-id variant `GET /pages/{id}/descendants`, and the finding that no other enumeration endpoint exists: `GET /pages` without a slug is a `400`, and `/pages/tree`, `/pages/root`, `/pages/list`, `/navigation`, `/clusters` are all `404`
- A client-level test pins that an empty slug survives normalization and reaches aiohttp as `slug=`. The assertion is on what the client hands the session rather than on the captured URL: aioresponses drops empty parameters when it rebuilds a query string, while aiohttp puts them on the wire — the difference matters here, because omitting `slug` entirely is a `400`

## [1.1.0] - 2026-08-09

### Added
- `LOG_LEVEL=DEBUG` now logs every inbound MCP message with the time spent serving it (`tools/call page_get (52 ms)`). The Wiki client already logs its own HTTP calls with durations, so the two subtract: a slow tool call is now attributable to the Wiki API or to us without attaching a profiler. Nothing is emitted at the default `INFO`
- The server now reports a one-line `description` — a short summary for a client's server list, distinct from the long-form `instructions` addressed to the model. Reaches clients on every protocol revision, and is read from package metadata so it cannot drift from `pyproject.toml`
- Cache hints (SEP-2549) on `tools/list` and `resources/list`, whose contents are fixed for the lifetime of the process: a 5-minute TTL saves re-sending 27 tool schemas on every connection. Deliberately **not** on `resources/read` — clients cache it per URI, while `wiki-mcp://configuration` varies with the `?orgId=`/`?cloudOrgId=` on the endpoint, so a hint there would report one tenant's organization to the next. Hints travel only on protocol `2026-07-28`; every earlier revision sees exactly the traffic it saw before

### Changed
- Migrated to MCP Python SDK v2 (`mcp[cli]>=2,<3`). **Nothing changes for clients**: one v2 server answers every protocol revision back to `2024-11-05` alongside the modern `2026-07-28`, the 27 tools and their schemas are identical, and `wiki-mcp://configuration` keeps its URI and its place in `resources/list`. Reinstalling is not required — `uvx` and the Docker tags pick the new version up on their own. If you need the old SDK in a shared environment, pin `pip install "yandex-wiki-search-mcp<1.1"` — `1.0.1` is the last release built on the 1.x SDK and stays on PyPI. Note that `<2` is not that pin: it matches this release too
- The upper bound is now `<3` rather than `<2`. Within 2.x the range stays open on purpose: `uv.lock` is what pins the Docker image, and freezing an exact version in `pyproject.toml` would only strand `uvx`/`pip` users on it
- Dependency footprint moved with the SDK: `httpx`/`httpx-sse` are replaced by `httpx2`, `sse-starlette` jumps to `>=3`, and `opentelemetry-api` and `mcp-types` are new. `httpx2` verifies TLS against the operating system trust store rather than certifi's bundle — irrelevant here, since this server talks to the Wiki API over `aiohttp`, but worth knowing if you build a minimal image with no system CA store

### Fixed
- The documented Docker commands now cap container logs (`--log-opt max-size=10m --log-opt max-file=3`, and the equivalent `logging:` block in Compose). The server writes no log files of its own, but Docker's default `json-file` driver stores stderr without a size limit, so a long-running container grew `/var/lib/docker/containers` until the disk ran out
- `HOST` is now passed to `run()` and `streamable_http_app()`, where mcp 2.x expects it. It was a constructor argument in 1.x; left out, the SDK defaults to `127.0.0.1` and arms DNS rebinding protection, which answers every MCP request behind a real hostname with `421 Misdirected Request` while `/healthz` keeps returning `200` — up to every probe, down to every client. Two tests pin both halves

### Internal
- `wiki-mcp://configuration` no longer reaches for an ambient context. The SDK removed `get_context()` and now refuses to inject a `Context` into a static-URI resource, so a middleware publishes the inbound request through a contextvar (`mcp_wiki/mcp/request_ctx.py`) and the handler reads it back. The alternative — making the URI a template — would have moved the resource out of `resources/list` and given the per-request organization a second source that can disagree with the one tools read
- Custom routes (`/healthz`, the OAuth callback) are registered through the public `custom_route()` instead of the private `_custom_starlette_routes` list
- Tests drive an in-memory `Client`, replacing the removed `create_connected_server_and_client_session`, on the SDK default `mode="auto"` — so the suite exercises the `2026-07-28` path a modern client negotiates

## [1.0.1] - 2026-08-09

### Fixed
- `1.0.0` could not be installed: the `mcp` dependency was declared as `>=1.21` with no upper bound, and `mcp` 2.0.0 (released 2026-07-28) removed `FastMCP` entirely. Every fresh install — `uvx yandex-wiki-search-mcp`, `pip install`, the Glama build — resolved to 2.0.0 and died on `ImportError: cannot import name 'FastMCP' from 'mcp.server'` before the server could answer a single request. The constraint is now `>=1.21,<2`, which states a real compatibility boundary: this server subclasses `FastMCP` and reaches into its internals for custom routes, the low-level server and the auth provider, so a major bump of the SDK is a rewrite, not an upgrade. Migrating to the 2.x API (`MCPServer`) is separate work
- CI never installed the package the way a user does. Both jobs run `uv sync --dev`, which resolves from `uv.lock` and so pins whatever was locked — the version ranges in `pyproject.toml` were exercised nowhere, which is exactly why a broken release shipped. A new `install` job builds the wheel, installs it with a fresh resolve, then constructs the server and counts its tools; it fails on the combination that reached users

## [1.0.0] - 2026-08-09

First stable release. The tool surface is now a compatibility promise: breaking
changes from here on mean a major version bump, which is why this release
bundles every planned rename and removal below.

### Added
- `page_clone` tool — copy a page to a new slug via `POST /pages/{id}/clone`, the API's only relocation primitive. It is a deferred operation: the client polls the returned `status_url` to completion (~1s live) and returns the copy's id and slug instead of an operation handle. An operation that fails, outlives the polling deadline, or comes back without a pollable `status_url` raises the new `WikiOperationError` — every HTTP exchange succeeded there, so a `WikiApiError` reading "failed with status 200" would blame the one layer that worked. The tool description states plainly what live probes proved (2026-08-08, `docs/api-notes.md`): the copy gets a new page id, children/comments/attachments/history stay with the original, and an occupied target slug is refused with `SLUG_OCCUPIED`. A `page_move` tool was briefly on this branch, built on `POST /pages/{id}` with a `slug` field — the contract sweep then proved that call is a silent no-op (200, nothing moves; the documented update body has no `slug`, and no `/move` endpoint exists), so it never shipped. There is no move/rename in the public API; to relocate a page, clone and delete the original. The sweep exercises the clone cycle, including both refusal contracts

### Changed
- Test coverage is now complete and gated at 100% (statements and branches, was 80%). Closing the gap turned up genuinely untested behavior rather than just numbers: three registered tools (`page_add_comment`, `page_delete`, `page_recover`) had no test at the tool layer at all, `make_wiki_lifespan` — the code that turns settings into a live client — ran in no test, so a dropped `wiki_max_retries` or auth token would have been invisible, and neither the Redis OAuth store selection nor `page_upload_attachment`'s `append_markup` path was exercised. The handful of genuinely unreachable lines carry `# pragma: no cover` with the reason: type-narrowing guards after `resolve_page_locator`, which has already refused anything but exactly one locator, and serializer fallbacks for a non-dict that pydantic never produces. `if __name__ == "__main__"` is excluded through a new `[tool.coverage.report]` section
- The codecov badge points at `app.codecov.io` and renders through shields.io. The old `codecov.io/gh/...` link answered 403 in a browser, and the badge image on that host does too — the link now goes where the report actually lives, and the image matches the shields.io style of every other badge in the README
- Tool annotations tightened. Every tool now sets `openWorldHint=false` — they all talk to exactly one configured Wiki organization, and the unset default is `true`, which advertises an open-world tool to clients that gate on it. `grid_update`, `grid_update_cells` and `page_update` keep `destructiveHint` at its `true` default deliberately: they overwrite existing state, so "retry-safe" (`idempotentHint=true`) must not read as "additive". A registration test now asserts the closed world on every tool
- `page_upload_attachment` is no longer registered when `OAUTH_ENABLED=true`. The tool reads `file_path` from the filesystem of the machine running the server; in a multi-user OAuth deployment that is the shared host, not the caller's machine — the tool could not do its job there, but any authenticated user could exfiltrate server-local files (`.env`, secrets) into their own wiki. Single-user stdio and plain-token HTTP setups are unaffected. The server instructions are built from the same setting, so they stop offering local-filesystem uploads when the tool is not registered
- `page_search`'s result-count argument is named `limit` (was `page_size`, breaking for direct schema consumers): since the 0.8.0 contract change the endpoint reads `limit` and ignores `page_size`, so the old name promised pagination that does not exist. Same rename on `WikiClient.page_search`
- `grid_move_rows` and `grid_move_columns` are now `grid_move_row` and `grid_move_column` (breaking for direct callers): each call moves exactly one row or column, and the plural names kept reading as batch operations — the singular is what the tool actually does. Same rename on the `WikiClient` methods
- `grid_delete` now returns a typed `GridDeleteResponse` (`grid_id`, `deleted: true`) instead of an untyped empty dict. The endpoint answers `204 No Content`; the acknowledgment is filled in client-side so agents get a confirmation they can check instead of `{}`
- A grid `409 CONFLICTING_OPERATION` now raises `GridConflict` (a `WikiApiError`) whose message carries the recovery instead of only the API's "Conflicting operation in progress": the write was not applied, so re-read the grid for a fresh revision and retry. The server instructions also tell agents to issue `grid_*` writes one at a time. Measured live: back-to-back mutations on one grid conflict about a third of the time and clear in ~10s, while a 3s gap never conflicted — so this only bites callers that batch grid writes in parallel, which optimistic locking already makes wrong. Deliberately not retried in the client: that would block a tool call for ten seconds and paper over the revision race underneath
- Offline test coverage for the six page endpoints that only the live contract sweep exercised (`page_get_comments`, `page_get_resources`, `page_get_attachments`, `page_add_comment`, `page_delete`, `page_recover`, plus the success path of `page_get_descendants`). Their tool-layer tests run against a mocked protocol, so nothing asserted the URL, query parameters or request body: a typo in a path would have passed the whole suite and shown up only in the weekly, opt-in sweep — which does not run on forks or without secrets
- The `API drift check` workflow now reads every `DRIFT_*` input from repository secrets instead of variables. Only secrets are masked in Actions logs, and on a public repo those logs are public — the sweep prints the slug it works under, so `DRIFT_SWEEP_SLUG` as a variable published the account name and section layout every week. Move the existing variables to secrets under the same names; until then the workflow skips itself, as it does for any incomplete setup

### Security
- An OAuth authorization could be hijacked by anyone who learned the client's `state`. That value was used as the storage key for the pending authorization, so a second `/authorize` call carrying the same `state` overwrote the first: the victim's callback then minted a code bound to the attacker's `client_id`, `redirect_uri` and `code_challenge` and redirected the victim's browser there — PKCE included, since the stored challenge was the attacker's too. `state` is a CSRF nonce that travels in URLs, browser history and proxy logs (RFC 6749 §10.12), not a secret. The key is now always server-generated and unguessable; the client's `state` is kept as data and echoed back on the final redirect. Records written by an older version still validate (the new field defaults to `None`); authorizations already in flight during the upgrade fail and have to be restarted, within the 10-minute state TTL

### Fixed
- The in-memory OAuth store never reclaimed anything it declared expired. It accepted a `ttl` and recorded the deadline, but only acted on it when someone looked up that exact key — so every abandoned login left a record for the lifetime of the process, and `/authorize` is unauthenticated, which made that a way to exhaust memory on purpose (a state costs ~1.6 KB; a gigabyte is about ten minutes of traffic). Writes now sweep expired states, authorization codes, access and refresh tokens, with the sweep threshold re-aimed at twice the surviving size so the amortized cost per write stays constant. Measured on 800k abandoned authorizations against 60k live: the store oscillates between 1.2× and 1.9× the live set instead of growing without bound, at 6.4 µs per write. Redis was never affected — it expires records itself
- Dynamically registered OAuth clients were kept forever, in Redis as well as in memory: `/register` needs no authentication, and nothing ever removed a registration (~2.6 KB each). Registrations now carry the lifetime the MCP SDK already supports — it stamps `client_secret_expires_at` at registration, tells the client the deadline in the registration response, and rejects an expired client on every authentication; both stores now drop what it marks dead. Configurable via the new `OAUTH_CLIENT_SECRET_EXPIRY_SECONDS` (default 30 days, empty disables it and restores the previous forever-behavior). A capacity cap was considered instead and rejected: evicting a live registration breaks a session mid-flight with an unexplainable "invalid client", and refusing new registrations when full hands an attacker the denial of service the cap was meant to prevent
- A stray key in `.env` stopped the server from starting: settings inherited pydantic-settings' `extra="forbid"`, and the env file is a directory-level convention shared with every other tool, not this server's private config — an unrelated `OPENAI_API_KEY` next to `WIKI_TOKEN` was a fatal `Extra inputs are not permitted`. Unknown keys are now ignored. The typo protection that strictness was doing has not been dropped but repaired: it only ever covered the env file, while the same misspelling passed as a real environment variable (`-e WIKI_READ_ONL=true` in Docker, `env` in a client config) was silently discarded and left every write tool registered. A key inside this server's namespaces that is close enough to a real setting to be a slip now stops startup with a "did you mean" in both channels, while unrelated variables like `REDIS_URL` pass untouched
- `import mcp_wiki.mcp.utils` raised `ImportError` as a first import: the HTTP client reached back into the MCP layer for `normalize_slug`, closing a cycle through `mcp_wiki.wiki.__init__`. Nothing in the suite noticed, because by then conftest had already imported the package in an order that happens to work. `normalize_slug` now lives in the Wiki layer where the client can own it, `mcp_wiki.mcp.utils` re-exports it so existing callers are unaffected, and every module is now imported in a subprocess by a test to keep the layering honest
- The `wiki-mcp://configuration` resource reported an organization no request ever carries. It derived `org_id` and `cloud_org_id` independently, so a request with `?cloudOrgId=` against a server defaulting to a plain `WIKI_ORG_ID` was answered with both at once — the very pair the settings validator forbids — while the call itself went out with only the cloud one. This is the 0.8.0 client fix that never reached the resource; both now share a single `select_org`, with a test asserting they agree
- `page_search` silently returned nothing for `slug_prefix="/"` (or whitespace): the prefix normalized to an empty string that matched no slug, and the empty result looked like an empty wiki rather than a bad filter. Such a prefix is now rejected with a message pointing at omitting it instead
- In-memory OAuth refresh tokens never expired. `get_refresh_token` checked `expires_at` all along, but the in-memory store never set it, so those tokens outlived their Redis counterparts by the whole process lifetime. Both stores now share one `REFRESH_TOKEN_TTL_SECONDS`
- `manifest.json` advertised a tool list that had drifted from the registered surface: the retired plural names `grid_move_rows`/`grid_move_columns` were still listed and `page_clone` was missing. The list is synced, and a test now compares it against the actually registered tools so the MCPB listing cannot drift again
- Under `WIKI_READ_ONLY=true` the server instructions still advertised every write capability — creating pages, mutating grids, YFM write guidance — while none of those tools were registered. Instructions are now built from the same settings that gate tool registration: a read-only server says so and lists only what it can actually do
- The contract sweep could only ever run once per slug. A run interrupted between creating its root page and the cleanup step left the slug taken, and every later run died on `page_create` with `SLUG_OCCUPIED` — reported as a problem, so a weekly job would have gone red permanently after one bad night. It now clears its own leftovers and retries, but only when the slug holds a page this sweep created; anything else is someone's real page and it refuses with an explanation instead of deleting it
- The sweep reported grid `409 CONFLICTING_OPERATION` as a contract problem. Grid mutations are serialized per grid, so firing them back to back hits a lock, not drift. Conflicts are now retried with a short backoff (documented in `docs/api-notes.md`)
- `--cleanup` crashed with a traceback when there was nothing to clean up. The workflow runs it with `if: always()`, so a sweep that failed early failed twice

## [0.8.0] - 2026-08-04

### Added
- `fetch_all` flag on the five cursor-paginated tools (`page_get_descendants`, `page_get_comments`, `page_get_attachments`, `page_get_resources`, `page_get_grids`): the server follows `next_cursor` and returns everything in one call, capped at 500 items and a 25s budget. The response then carries `truncated`: `false` only when the list was drained to its end, `true` when the walk stopped early — on the cap, the budget, a failed page or a cursor the server repeated. `next_cursor` then points at the continuation, except after a repeated cursor, where nothing is safe to continue from and it is cleared. A failed page keeps what was already fetched rather than discarding it. `page_search` deliberately has no such flag — its cursors are dead server-side (always `null`)
- `TOOL_RESULT_TEXT` setting for the text duplicate of structured tool results: `pretty` (indent=2, the FastMCP default and still the spec-friendly choice), `compact` (single line — 10-30% off the text block: most on long lists of small objects, least on a few large strings, since indentation is charged per line) or `none` (structured content only; make sure your client renders `structuredContent` before enabling). Structured content and its schema validation are unaffected
- Living defense against API drift: `scripts/contract_sweep.py` exercises every `WikiClient` method against a live organization (fixtures under a scratch slug, delete/recover cycle included) and reports pydantic mismatches plus undeclared response keys; the `API drift check` workflow re-runs it weekly when the opt-in `DRIFT_WIKI_TOKEN` secret and `DRIFT_*` variables are configured. Born from the search-endpoint incident: the undocumented backend silently swapped its wire contract between 2026-07 and 2026-08 (documented in `docs/api-notes.md`)

### Fixed
- `page_search` was broken against the live API (all fixes verified live on 2026-08-02):
  - any non-empty search failed with a validation error — the API sends `modified_at` as an ISO datetime string, the model expected an integer epoch
  - the snippet arrives in the `content` key, not `body`: the declared field was always empty and the snippet text only reached clients through the extra-fields leak
  - the `page_size` tool parameter was silently ignored by the API (every search returned at most 10 results regardless of the requested size) — the endpoint reads `limit` from the POST body, which the client now sends; values above 50 are a validation error server-side, the existing clamp keeps them at 50
- Under OAuth a client could not name a cloud organization on a server whose default is a plain `WIKI_ORG_ID`, or the other way round: the two ids were resolved independently, so a request carrying `?cloudOrgId=` was paired with the server-wide `org_id` and rejected as "only one of org_id or cloud_org_id". Per-request auth now replaces the organization as a unit

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
