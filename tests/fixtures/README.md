# Test fixtures

**Fully synthetic** JSON responses for `yandex-wiki-search-mcp` unit tests. They
reproduce the *exact shape/keys/types* observed live against the Yandex Wiki API
(see [`docs/api-notes.md`](../../docs/api-notes.md) and the
probe scripts in `scripts/`), but every value — slugs, titles, uids, usernames,
org ids — is fake. Safe to keep in a public repo.

| file | purpose |
|---|---|
| `search_results.json` | `POST /v1/search` 200 — mixed `page` + `file` results. Note: `page` url is relative, `file` url is an absolute `?download=1` link; the snippet is in `content` and `modified_at` is an ISO string. |
| `search_empty.json` | `POST /v1/search` 200 for a query with no hits (`results: []` plus the two always-null cursors). |
| `page_full.json` | `GET /v1/pages/{id}?fields=content,attributes,breadcrumbs,access_policy,access_lists,owner` — all optional fields populated. |
| `resources.json` | `GET /v1/pages/{id}/resources` — combined attachment + grid resource items. |
| `errors.json` | Keyed collection of real error envelopes (400/401/403/404). Two envelope shapes (A: `message` string\|null + `details`; B: `message` array + `level`) — the client must parse both. |

## How they map to tests

- **client layer** (`tests/wiki/custom/test_client.py`): mock the HTTP response with
  `aioresponses` using these payloads (`load_fixture(...)` from `tests/conftest.py`);
  assert the parsed pydantic model and the outgoing request (search body is
  `{query, limit}` — the endpoint ignores `page_size`, headers).

Load every fixture from at least one test. An unloaded fixture cannot fail, so
it drifts away from the contract it claims to document — which is exactly what
happened to the search pair before v0.8.0.
- **MCP layer** (`tests/mcp/tools/test_page_read_tools.py`): set
  `mock_wiki_protocol.page_search.return_value` to the parsed model / dict and
  assert the tool output.
