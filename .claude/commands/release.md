---
allowed-tools: Bash(mkdir:*), Bash(grep:*), Bash(task:*), Bash(find:*), Bash(sed:*), Bash(git add:*), Bash(git commit:*), Bash(git tag:*), Bash(git describe:*), Bash(git branch:*), Bash(git log:*), Bash(git status:*), Bash(git diff:*)
description: make a new release of the Yandex Wiki MCP project
---

## Context

- Current git status: !`git status`
- Current git diff (staged and unstaged changes): !`git diff HEAD`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -10`
- Linters status: !`task`

# Task

Prepare a new release of `yandex-wiki-search-mcp`.

# Instructions

1. Make sure current code passes `task`.
2. Verify `@README.md`, `@README_ru.md`, and `@CLAUDE.md` match the current Wiki project, toolset, and configuration.
3. Decide the next version in `@pyproject.toml`.
   While major version is `0.x`:
   - small fixes and documentation-only changes usually bump patch
   - substantial API/tool additions usually bump minor
4. Keep versions synchronized across:
   - `@pyproject.toml`
   - `@manifest.json`
   - `@server.json`
   - OCI image tag in `@server.json`
5. Add a new top entry to `@CHANGELOG.md`.
   Never rewrite older released entries.
6. Run `uv lock` so `uv.lock` matches the new package metadata and dependencies.
7. Create a git commit for the release preparation.
   First line: short summary of the actual change, not just "bump version".
   Then bullet points with the important release notes.
8. Other requirements: $ARGUMENTS
