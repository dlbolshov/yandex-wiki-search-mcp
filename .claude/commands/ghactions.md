---
allowed-tools: Bash(git push:*), mcp__github__list_workflow_runs
description: validate GitHub Actions workflows for the Yandex Wiki MCP project
---

# Task

Validate that GitHub Actions workflows for the latest push of `yandex-wiki-search-mcp` are passing.

# Instructions

1. Retrieve workflow runs triggered by the latest push.
2. Wait until all relevant workflows finish.
3. Report each workflow status and URL.
4. If a workflow fails, identify which workflow failed and stop instead of claiming success.
