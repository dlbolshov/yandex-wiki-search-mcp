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
3. Decide the next version in `@pyproject.toml`. Since 1.0.0 the tool
   surface is the compatibility promise (`docs/architecture.md` → "Release
   process"):
   - fixes and documentation-only changes bump patch, including a security
     fix that closes a hole by taking something away (1.5.2)
   - a new tool, argument or setting, or a deprecated one, bumps minor
     (1.6.0)
   - a breaking change to the tool surface bumps major

   When the choice is not obvious, the `CHANGELOG.md` entry's lead
   paragraph says why, and what an upgrading deployment has to do.
4. Keep versions synchronized across:
   - `@pyproject.toml`
   - `@manifest.json`
   - `@server.json`
   - OCI image tag in `@server.json`
5. Promote `## [Unreleased]` in `@CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`
   and append the dated entry to ROADMAP's execution log.
   Never rewrite older released entries.
6. Run `uv lock` so `uv.lock` matches the new package metadata and dependencies.
7. Create a git commit for the release preparation.
   First line: `release: X.Y.Z`, the title every release since 1.3.0 has
   used (CLAUDE.md and `docs/architecture*.md` name the same convention).
   Then bullet points with the important release notes.
8. Other requirements: $ARGUMENTS
