---
allowed-tools: WebFetch(domain:yandex.ru), Bash(mkdir:*), Bash(grep:*), Bash(task:*), Bash(find:*), Bash(sed:*)
description: add new MCP tools to the Yandex Wiki MCP server
---

# Task

Add a new MCP tool to `yandex-wiki-search-mcp`.

Always follow `@CLAUDE.md`.

## Instructions

1. Keep the implementation aligned with the current Wiki architecture:
   - protocol in `mcp_wiki/wiki/proto/pages.py`
   - models in `mcp_wiki/wiki/proto/types/pages.py`
   - HTTP implementation in `mcp_wiki/wiki/custom/client.py`
   - MCP registration in `mcp_wiki/mcp/tools/page_read.py` or `mcp_wiki/mcp/tools/page_write.py`
2. If the tool is read-only, register it only in `page_read.py`.
3. If the tool mutates Wiki state, register it only in `page_write.py` and make sure it is hidden in read-only mode.
4. Add or update tests:
   - HTTP-level tests in `tests/wiki/custom/` when client behavior changes
   - MCP-level tests in `tests/mcp/tools/`
   - registration checks in `tests/mcp/server/test_server_creation.py` when a new tool is added
5. Update `@README.md`, `@README_ru.md`, `@manifest.json`, and `@CHANGELOG.md`.

# Tool description

$ARGUMENTS
