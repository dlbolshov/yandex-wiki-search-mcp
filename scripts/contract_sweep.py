"""Live contract sweep for the v0.8.0 work: call every WikiClient method
against the real API and report where our pydantic models disagree with the
live contract (the page_search class of bug). Extras beyond the declared
model fields are reported too — they feed the extra="allow" -> "ignore"
decision.

WARNING: this script WRITES to the wiki configured in .env under the given
base slug — use a scratch spot in your personal section! It ignores
WIKI_READ_ONLY (that flag only gates MCP tool registration, not WikiClient).

Usage:
    uv run python scripts/contract_sweep.py users/<login>/contract-sweep
    uv run python scripts/contract_sweep.py users/<login>/contract-sweep --cleanup
"""

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from mcp_wiki.settings import Settings
from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import PageNotFound, WikiApiError, WikiError
from mcp_wiki.wiki.proto.types import pages as page_models
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    PageFieldEnum,
    WikiGridPageRef,
)

REPORT: list[tuple[str, str, str]] = []


KNOWN_DROPPED = frozenset(
    {
        # Identity payloads on every user reference, trimmed to WikiUser in
        # v0.8.0 on purpose (docs/api-notes.md). They arrive on every comment
        # and attachment, so without this list the sweep would cry drift
        # every single run.
        "identity",
        "uid",
        "cloud_uid",
        "is_dismissed",
        "affiliation",
    }
)


def enable_extras_detection() -> None:
    """Flip every model back to extra="allow" for this process.

    Production models ignore unknown keys (token economy), which would blind
    the sweep to contract drift — rebuild them permissive so new API keys
    show up in the extras report again.

    Flipping `model_config` is not enough: pydantic caches each model's core
    schema, and a parent rebuilt while a child still holds a strict cached
    schema keeps ignoring extras *inside* that child. Since dir() is
    alphabetical, every `results: list[Item]` envelope sorted before its item
    model — which is exactly where API drift shows up. So drop the cached
    schemas first, then rebuild.
    """
    models = [
        obj
        for name in dir(page_models)
        if isinstance(obj := getattr(page_models, name), type)
        and issubclass(obj, page_models.BaseWikiModel)
    ]
    for model in models:
        model.model_config["extra"] = "allow"
        for cached in (
            "__pydantic_core_schema__",
            "__pydantic_validator__",
            "__pydantic_serializer__",
        ):
            if cached in model.__dict__:
                delattr(model, cached)
    for model in models:
        model.model_rebuild(force=True)


def make_client(settings: Settings) -> WikiClient:
    return WikiClient(
        base_url=settings.wiki_api_base_url,
        token=settings.wiki_token.get_secret_value() if settings.wiki_token else None,
        iam_token=settings.wiki_iam_token.get_secret_value()
        if settings.wiki_iam_token
        else None,
        auth_scheme=settings.wiki_auth_scheme,
        cloud_org_id=settings.wiki_cloud_org_id,
        org_id=settings.wiki_org_id,
        max_retries=settings.wiki_max_retries,
    )


def extras_of(obj: Any) -> set[str]:
    """Extra keys the API sent beyond the declared model fields, recursively."""
    found: set[str] = set()
    if isinstance(obj, BaseModel):
        found |= set(obj.model_extra or {})
        for name in type(obj).model_fields:
            found |= extras_of(getattr(obj, name, None))
    elif isinstance(obj, list):
        for item in obj:
            found |= extras_of(item)
    return found


CONFLICT_ATTEMPTS = 4
CONFLICT_DELAY = 1.5


async def _await_settled(fn: Callable[[], Awaitable[Any]]) -> Any:
    """Retry a call while the API reports a conflicting operation.

    Grid mutations are serialized server-side: fire two at a grid back to back
    — or touch one while an async `grid_copy` is still running — and the second
    gets 409 CONFLICTING_OPERATION. That is a lock, not a contract change, and
    a weekly job that cries drift over it teaches everyone to ignore it.
    """
    for attempt in range(1, CONFLICT_ATTEMPTS + 1):
        try:
            return await fn()
        except WikiApiError as exc:
            conflict = exc.status == 409 and exc.error_code == "CONFLICTING_OPERATION"
            if not conflict or attempt == CONFLICT_ATTEMPTS:
                raise
            await asyncio.sleep(CONFLICT_DELAY * attempt)
    raise AssertionError("unreachable")


async def check(
    name: str,
    fn: Callable[[], Awaitable[Any]],
    note_from_result: Callable[[Any], str] | None = None,
) -> Any:
    """Run one live call and record what the contract looked like.

    Undeclared keys are a failure, not a footnote — silently noting them is
    how drift reaches production. Keys we drop on purpose live in
    KNOWN_DROPPED.
    """
    try:
        result = await _await_settled(fn)
    except ValidationError as exc:
        first = "; ".join(str(exc).splitlines()[1:3])
        REPORT.append((name, "MODEL MISMATCH", first[:160]))
        print(f"  !! {name}: MODEL MISMATCH — {first[:160]}")
        return None
    except WikiError as exc:
        REPORT.append((name, f"API {type(exc).__name__}", str(exc)[:160]))
        print(f"  !! {name}: {type(exc).__name__}: {str(exc)[:160]}")
        return None

    notes = []
    if note_from_result is not None:
        notes.append(note_from_result(result))
    status = "OK"
    if isinstance(result, BaseModel | list):
        undeclared = extras_of(result) - KNOWN_DROPPED
        if undeclared:
            status = "UNDECLARED EXTRAS"
            notes.append(", ".join(sorted(undeclared)))
    elif isinstance(result, dict):
        notes.append("keys: " + ", ".join(sorted(result)) if result else "empty body")

    note = "; ".join(n for n in notes if n)
    REPORT.append((name, status, note))
    marker = "ok" if status == "OK" else "!!"
    print(f"  {marker} {name}" + (f"  [{note}]" if note else ""))
    return result


class CursorWalk:
    """Follows next_cursor to the end, merging every page into one envelope.

    Merging matters: `check` inspects the object it is handed, so returning
    only the last page would hide undeclared keys that appeared on any
    earlier one.
    """

    MAX_PAGES = 20

    def __init__(self, fetch: Callable[[str | None], Awaitable[Any]]):
        self._fetch = fetch
        self.pages = 0
        self.stopped_early = False

    async def run(self) -> Any:
        first = await self._fetch(None)
        self.pages = 1
        cursor = first.next_cursor
        while cursor:
            if self.pages >= self.MAX_PAGES:
                self.stopped_early = True
                break
            page = await self._fetch(cursor)
            first.results.extend(page.results)
            self.pages += 1
            if page.next_cursor == cursor:
                self.stopped_early = True
                break
            cursor = page.next_cursor
        first.next_cursor = None
        return first

    def note(self, response: Any) -> str:
        suffix = " (stopped early!)" if self.stopped_early else ""
        return f"{len(response.results)} items in {self.pages} page(s){suffix}"


ROOT_TITLE = "Contract sweep"


async def _clear_own_leftovers(wiki: WikiClient, base: str) -> bool:
    """Remove a previous run's fixtures so a rerun can proceed.

    A run cancelled between creating the root and the cleanup step leaves the
    slug taken, and every later run would then fail on page_create — a weekly
    job that breaks permanently after one bad night is worse than useless.

    Only fixtures this script made are removed: the base slug must hold a page
    this script titled. Anything else is someone's real page — the operator
    pointed the sweep at the wrong slug, and deleting it would be the worst
    possible response.
    """
    try:
        existing = await wiki.page_get_by_slug(base)
    except WikiError:
        return False

    title = existing.title or ""
    if not title.startswith(ROOT_TITLE):
        print(f"  !! {base!r} holds a page titled {title!r}, which this sweep did")
        print("     not create. Point SWEEP_SLUG at a scratch slug instead — the")
        print("     sweep creates and deletes pages under it.")
        return False

    print(f"  leftovers from an earlier run at {base!r}, clearing them first")
    await cleanup(wiki, base)
    return True


async def sweep(wiki: WikiClient, base: str, n_pages: int) -> None:
    print(f"\n=== fixtures under {base!r} ===")

    def create_root() -> Any:
        return wiki.page_create(slug=base, title=ROOT_TITLE, content="root page")

    root = await check("page_create (root)", create_root)
    if root is None and await _clear_own_leftovers(wiki, base):
        REPORT.pop()
        root = await check("page_create (root, after clearing leftovers)", create_root)
    if root is None:
        print("cannot continue without the root page")
        return

    children = []
    for i in range(n_pages):
        page = await wiki.page_create(
            slug=f"{base}/p-{i:02d}", title=f"Sweep page {i}", content=f"page {i}"
        )
        children.append(page)
    REPORT.append((f"page_create x{n_pages} (children)", "OK", ""))
    print(f"  ok page_create x{n_pages} (children)")

    await check(
        "page_update", lambda: wiki.page_update(root.id, title="Contract sweep *")
    )
    await check(
        "page_append_content",
        lambda: wiki.page_append_content(
            root.id, content="appended", location="bottom"
        ),
    )

    comment = await check(
        "page_add_comment",
        lambda: wiki.page_add_comment(root.id, body="sweep comment 1"),
    )
    await wiki.page_add_comment(root.id, body="sweep comment 2")
    await wiki.page_add_comment(root.id, body="sweep comment 3")
    if comment is not None:
        await check(
            "page_add_comment (reply)",
            lambda: wiki.page_add_comment(
                root.id, body="sweep reply", parent_id=comment.id
            ),
        )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="sweep-", delete=False
    ) as handle:
        handle.write("attachment payload for the contract sweep\n")
        tmp_path = Path(handle.name)
    try:
        await check(
            "page_upload_attachment",
            lambda: wiki.page_upload_attachment(root.id, file_path=str(tmp_path)),
        )
    finally:
        await asyncio.to_thread(tmp_path.unlink, True)

    print(f"\n=== grids (host page {base}/grid-host) ===")
    grid_host = await check(
        "page_create (grid host)",
        lambda: wiki.page_create(
            slug=f"{base}/grid-host", title="Grid host", content="hosts a grid"
        ),
    )
    grid = None
    if grid_host is not None:
        grid = await check(
            "grid_create",
            lambda: wiki.grid_create(
                request=GridCreateRequest(
                    title="Sweep grid", page=WikiGridPageRef(id=grid_host.id)
                )
            ),
        )
    if grid is not None:
        grid_id = str(grid.id)
        revision = grid.revision or ""
        columns = [
            {"title": "Name", "slug": "name", "type": "string", "required": False},
            {"title": "Count", "slug": "count", "type": "number", "required": False},
            {"title": "Done", "slug": "done", "type": "checkbox", "required": False},
        ]
        mutation = await check(
            "grid_add_columns",
            lambda: wiki.grid_add_columns(grid_id, revision=revision, columns=columns),
        )
        if mutation is not None and mutation.revision:
            revision = mutation.revision
        rows = [
            {"name": "alpha", "count": 1, "done": False},
            {"name": "beta", "count": 2, "done": True},
            {"name": "gamma", "count": 3, "done": False},
        ]
        mutation = await check(
            "grid_add_rows",
            lambda: wiki.grid_add_rows(grid_id, revision=revision, rows=rows),
        )
        row_ids = []
        if mutation is not None:
            row_ids = [row.id for row in mutation.results if row.id is not None]
            if mutation.revision:
                revision = mutation.revision
        if row_ids:
            await check(
                "grid_update_cells",
                lambda: wiki.grid_update_cells(
                    grid_id,
                    cells=[{"row_id": row_ids[0], "column_slug": "count", "value": 42}],
                ),
            )
            await check(
                "grid_move_rows",
                lambda: wiki.grid_move_rows(
                    grid_id, revision=revision, row_id=str(row_ids[0]), position=1
                ),
            )
        fetched = await check("grid_get", lambda: wiki.grid_get(grid_id))
        if fetched is not None and fetched.revision:
            revision = fetched.revision
        await check(
            "grid_update (title)",
            lambda: wiki.grid_update(
                grid_id,
                request=GridUpdateRequest(revision=revision, title="Sweep grid *"),
            ),
        )
        await check("page_get_grids", lambda: wiki.page_get_grids(grid_host.id))
        await check(
            "grid_copy",
            lambda: wiki.grid_copy(
                grid_id, target=f"{base}/grid-copy", title="Sweep grid copy"
            ),
        )
        fetched = await wiki.grid_get(grid_id)
        revision = fetched.revision or revision
        if row_ids:
            await check(
                "grid_delete_rows",
                lambda: wiki.grid_delete_rows(
                    grid_id, revision=revision, row_ids=[str(row_ids[-1])]
                ),
            )
        fetched = await wiki.grid_get(grid_id)
        revision = fetched.revision or revision
        await check(
            "grid_move_columns",
            lambda: wiki.grid_move_columns(
                grid_id, revision=revision, column_slug="count", position=0
            ),
        )
        fetched = await wiki.grid_get(grid_id)
        revision = fetched.revision or revision
        await check(
            "grid_delete_columns",
            lambda: wiki.grid_delete_columns(
                grid_id, revision=revision, column_slugs=["done"]
            ),
        )
        # grid_delete last: it takes the fixture away, and its response shape
        # is one of the two the models do not cover yet.
        await check("grid_delete", lambda: wiki.grid_delete(grid_id))

    print("\n=== reads ===")
    all_fields = [field.value for field in PageFieldEnum]
    await check(
        "page_get (all fields)",
        lambda: wiki.page_get(root.id, fields=all_fields),
    )
    await check("page_get_by_slug", lambda: wiki.page_get_by_slug(base))
    tree_walk = CursorWalk(
        lambda cur: wiki.page_get_descendants(
            base, include_self=True, page_size=5, cursor=cur
        )
    )
    await check(
        "page_get_descendants (cursor walk, page_size=5)",
        tree_walk.run,
        note_from_result=tree_walk.note,
    )
    comment_walk = CursorWalk(
        lambda cur: wiki.page_get_comments(root.id, page_size=2, cursor=cur)
    )
    await check(
        "page_get_comments (cursor walk, page_size=2)",
        comment_walk.run,
        note_from_result=comment_walk.note,
    )
    await check("page_get_attachments", lambda: wiki.page_get_attachments(root.id))
    await check("page_get_resources (root)", lambda: wiki.page_get_resources(root.id))
    if grid is not None:
        await check(
            "page_get_resources (grid host)",
            lambda: wiki.page_get_resources(grid_host.id),
        )
    await check(
        "page_search (existing corpus)",
        lambda: wiki.page_search("документация", page_size=50),
    )

    print("\n=== delete / recover cycle ===")
    if not children:
        REPORT.append(("page_delete / page_recover", "SKIP", "no child pages created"))
        return
    victim = children[-1]
    deleted = await check("page_delete", lambda: wiki.page_delete(victim.id))
    if deleted is not None and deleted.recovery_token:
        await check(
            "page_recover",
            lambda: wiki.page_recover(deleted.recovery_token),
            note_from_result=lambda r: (
                "recovered with same id"
                if r.id == victim.id
                else "recovered with NEW id!"
            ),
        )
    elif deleted is not None:
        REPORT.append(("page_recover", "SKIP", "no recovery_token in response"))


async def cleanup(wiki: WikiClient, base: str) -> None:
    # Walk the cursor: a single page would leave a tail behind for any
    # --pages value past the page size, and the leftovers would then collide
    # with the next run's root page and read as API drift.
    walk = CursorWalk(
        lambda cur: wiki.page_get_descendants(
            base, include_self=True, page_size=100, cursor=cur
        )
    )
    try:
        response = await walk.run()
    except PageNotFound:
        # The workflow runs cleanup with if: always(), so it also runs after a
        # sweep that died before creating anything. Nothing to remove is the
        # goal state, not an error to fail the job with.
        print(f"nothing to clean up under {base!r}")
        return
    pages = sorted(
        response.results,
        key=lambda p: (p.slug or "").count("/"),
        reverse=True,
    )
    for page in pages:
        try:
            await wiki.page_delete(page.id)
            print(f"  deleted {page.slug} (id={page.id})")
        except WikiError as exc:
            print(f"  FAILED to delete {page.slug}: {exc}")
    print(f"cleanup done, {len(pages)} page(s)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_slug", help="Scratch slug, e.g. users/me/contract-sweep")
    parser.add_argument("--pages", type=int, default=12)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()

    enable_extras_detection()
    settings = Settings()
    async with make_client(settings) as wiki:
        if args.cleanup:
            await cleanup(wiki, args.base_slug)
            return 0
        await sweep(wiki, args.base_slug, args.pages)

    print("\n=== summary ===")
    broken = 0
    for name, status, note in REPORT:
        marker = "ok" if status == "OK" else "!!"
        if status not in ("OK", "SKIP"):
            broken += 1
        print(f"  {marker} {name:44} {status:16} {note}")
    print(f"\n{broken} problem(s) found" if broken else "\nall contracts hold")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
