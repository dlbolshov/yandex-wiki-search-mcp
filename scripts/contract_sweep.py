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
from mcp_wiki.wiki.custom.errors import WikiError
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    PageFieldEnum,
    WikiGridPageRef,
)

REPORT: list[tuple[str, str, str]] = []


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


async def check(name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    try:
        result = await fn()
    except ValidationError as exc:
        first = "; ".join(str(exc).splitlines()[1:3])
        REPORT.append((name, "MODEL MISMATCH", first[:160]))
        print(f"  !! {name}: MODEL MISMATCH — {first[:160]}")
        return None
    except WikiError as exc:
        REPORT.append((name, f"API {type(exc).__name__}", str(exc)[:160]))
        print(f"  !! {name}: {type(exc).__name__}: {str(exc)[:160]}")
        return None
    note = ""
    if isinstance(result, BaseModel | list):
        extra = extras_of(result)
        if extra:
            note = "extras: " + ", ".join(sorted(extra))
    elif isinstance(result, dict):
        note = "keys: " + ", ".join(sorted(result)) if result else "empty body"
    REPORT.append((name, "OK", note))
    print(f"  ok {name}" + (f"  [{note}]" if note else ""))
    return result


async def walk_cursor(fetch: Callable[[str | None], Awaitable[Any]]) -> tuple[Any, str]:
    """Follow next_cursor to the end; returns (last response, note)."""
    total = 0
    hops = 0
    cursor: str | None = None
    response = None
    while True:
        response = await fetch(cursor)
        total += len(response.results)
        cursor = response.next_cursor
        if not cursor:
            break
        hops += 1
        if hops > 20:
            raise RuntimeError("cursor did not terminate after 20 hops")
    return response, f"{total} items in {hops + 1} page(s)"


async def sweep(wiki: WikiClient, base: str, n_pages: int) -> None:
    print(f"\n=== fixtures under {base!r} ===")
    root = await check(
        "page_create (root)",
        lambda: wiki.page_create(
            slug=base, title="Contract sweep", content="root page"
        ),
    )
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
            "grid_delete_columns",
            lambda: wiki.grid_delete_columns(
                grid_id, revision=revision, column_slugs=["done"]
            ),
        )

    print("\n=== reads ===")
    all_fields = [field.value for field in PageFieldEnum]
    await check(
        "page_get (all fields)",
        lambda: wiki.page_get(root.id, fields=all_fields),
    )
    await check("page_get_by_slug", lambda: wiki.page_get_by_slug(base))
    result = await check(
        "page_get_descendants (cursor walk, page_size=5)",
        lambda: walk_cursor(
            lambda cur: wiki.page_get_descendants(
                base, include_self=True, page_size=5, cursor=cur
            )
        ),
    )
    if result is not None:
        REPORT[-1] = (REPORT[-1][0], "OK", result[1])
        print(f"     -> {result[1]}")
    result = await check(
        "page_get_comments (cursor walk, page_size=2)",
        lambda: walk_cursor(
            lambda cur: wiki.page_get_comments(root.id, page_size=2, cursor=cur)
        ),
    )
    if result is not None:
        REPORT[-1] = (REPORT[-1][0], "OK", result[1])
        print(f"     -> {result[1]}")
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
    victim = children[-1]
    deleted = await check("page_delete", lambda: wiki.page_delete(victim.id))
    if deleted is not None and deleted.recovery_token:
        recovered = await check(
            "page_recover", lambda: wiki.page_recover(deleted.recovery_token)
        )
        if recovered is not None:
            same = "same id" if recovered.id == victim.id else "NEW id!"
            REPORT[-1] = (REPORT[-1][0], "OK", f"recovered with {same}")
            print(f"     -> recovered with {same}")
    elif deleted is not None:
        REPORT.append(("page_recover", "SKIP", "no recovery_token in response"))


async def cleanup(wiki: WikiClient, base: str) -> None:
    response = await wiki.page_get_descendants(base, include_self=True, page_size=100)
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
