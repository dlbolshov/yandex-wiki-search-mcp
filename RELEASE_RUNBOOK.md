# Release Runbook — yandex-wiki-search-mcp v0.3.0

Human-only steps to finish the v0.3.0 release. Automated release logic lives in
`.github/workflows/release.yml` and triggers on a `v*` tag push.

## 0. Repo description & topics — NEEDS HUMAN

`gh repo edit` from the automation account returned
`HTTP 403: Resource not accessible by integration`, so set these manually at
`https://github.com/dlbolshov/yandex-wiki-search-mcp` → ⚙ (About, top-right of the repo page):

- **Description:** `MCP server for Yandex Wiki with full-text search (read-only friendly, Docker-ready). Fork of APonkratov/yandex-wiki-mcp.`
- **Topics:** `mcp`, `model-context-protocol`, `yandex-wiki`, `yandex-360`, `search`, `llm`, `ai`

Or from your own terminal:

```bash
gh repo edit dlbolshov/yandex-wiki-search-mcp \
  --description "MCP server for Yandex Wiki with full-text search (read-only friendly, Docker-ready). Fork of APonkratov/yandex-wiki-mcp." \
  --add-topic mcp --add-topic model-context-protocol --add-topic yandex-wiki \
  --add-topic yandex-360 --add-topic search --add-topic llm --add-topic ai
```

Other repo settings verified as done: fork renamed, Issues enabled, Actions enabled.

## 1. GitHub Actions on the fork — VERIFY

Actions were pre-enabled. Confirm the `Test` workflow actually ran on the v0.3.0 PR
(check the PR's Checks tab). If it did not: repo → **Actions** tab → *"I understand my
workflows, go ahead and enable them"*.

## 2. PyPI Trusted Publishing — NEEDS HUMAN (before the first tag push)

At `https://pypi.org/manage/account/publishing/`, add a **pending publisher**:

- PyPI project name: `yandex-wiki-search-mcp`
- Owner: `dlbolshov`
- Repository: `yandex-wiki-search-mcp`
- Workflow name: `release.yml`
- Environment name: `pypi`

No API token is needed; the workflow publishes via OIDC. This must exist **before**
the first tag push, otherwise the `publish` job fails.

## 3. Live smoke test — NEEDS HUMAN (after merging the PR)

Run `scripts/smoke.sh` against the live API with your own credentials
(env vars or a `$SECRETS` file; see the script header). Verify `page_search`
returns results and read tools work.

## 4. Merge PR, tag, and push — NEEDS HUMAN

After review, green CI, and the live smoke test:

```bash
git checkout main && git pull
git tag v0.3.0
git push origin v0.3.0
```

`release.yml` then automatically: validates metadata versions, builds sdist/wheel,
builds the `.mcpb`, builds and pushes the multi-arch Docker image to ghcr, publishes
to PyPI (trusted publishing), creates a GitHub Release, and publishes to the MCP Registry.

## 5. GHCR visibility — NEEDS HUMAN (after the first image push)

The ghcr package is **private** by default; `docker pull` gives 403 until you flip it:
GitHub → your profile → **Packages** → `yandex-wiki-search-mcp` → Package settings →
Change visibility → **Public**.

## 6. MCP Registry — VERIFY (automated)

Publish runs via `mcp-publisher login github-oidc` in the release workflow. It works
because `server.json.name` is `io.github.dlbolshov/yandex-wiki-search-mcp`, the repo
is public, and the Docker image carries the matching
`io.modelcontextprotocol.server.name` OCI label. After the release, verify the listing
at `https://registry.modelcontextprotocol.io`.

## 7. Glama — OPTIONAL

Glama self-indexes public GitHub MCP servers. Optionally claim the listing and fill
metadata at `https://glama.ai/mcp/servers`.

## 8. awesome-mcp-servers — OPTIONAL

One PR to `https://github.com/punkpeye/awesome-mcp-servers` adding
`dlbolshov/yandex-wiki-search-mcp` for steady discovery.
