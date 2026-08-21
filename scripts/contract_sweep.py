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
from mcp_wiki.wiki.custom.errors import (
    GridConflict,
    PageNotFound,
    WikiApiError,
    WikiError,
)
from mcp_wiki.wiki.proto.types import pages as page_models
from mcp_wiki.wiki.proto.types.pages import (
    GridCreateRequest,
    GridUpdateRequest,
    PageFieldEnum,
    SearchAuthor,
    WikiGridPageRef,
)

REPORT: list[tuple[str, str, str]] = []


def broken(name: str, detail: str) -> None:
    """Record a contract that held on the wire but produced the wrong result.

    A plain REPORT.append with the status spelled out every time invited a typo or
    a two-element tuple, which fails at print time — at the end of a live
    sweep that has already mutated the wiki.
    """
    REPORT.append((name, "BROKEN", detail))


# Identity payloads ride along on every user reference, where WikiUser
# deliberately drops them (v0.8.0, docs/api-notes.md) — without this the sweep
# would cry drift on every comment and attachment, every run.
IDENTITY_KEYS = frozenset({"identity", "uid", "cloud_uid"})

KNOWN_DROPPED = IDENTITY_KEYS | frozenset({"is_dismissed", "affiliation"})


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


# The lock clears in about 10s, so back off past that before giving up.
CONFLICT_ATTEMPTS = 5
CONFLICT_DELAY = 1.5


async def _await_settled(fn: Callable[[], Awaitable[Any]]) -> Any:
    """Retry a call while the API reports a conflicting operation.

    Grid mutations are serialized per grid: fire two back to back and the
    second gets 409 CONFLICTING_OPERATION for about ten seconds. That is a
    lock, not a contract change, and a weekly job that cries drift over it
    teaches everyone to ignore it.
    """
    for attempt in range(1, CONFLICT_ATTEMPTS + 1):
        try:
            return await fn()
        except GridConflict:
            if attempt == CONFLICT_ATTEMPTS:
                raise
            await asyncio.sleep(CONFLICT_DELAY * attempt)
    raise AssertionError("unreachable")


async def check(
    name: str,
    fn: Callable[[], Awaitable[Any]],
    note_from_result: Callable[[Any], str] | None = None,
    *,
    watch_identity: bool = False,
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
        ignored = KNOWN_DROPPED - IDENTITY_KEYS if watch_identity else KNOWN_DROPPED
        undeclared = extras_of(result) - ignored
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


async def check_expected_error(
    name: str,
    fn: Callable[[], Awaitable[Any]],
    expected: type[WikiError],
    *,
    error_code: str | None = None,
) -> None:
    """Record a contract that is only visible in a refusal.

    Slug collisions and single-page clone semantics answer with an error by
    design, so this check is inverted: the call succeeding means the API
    changed under us. When error_code is given, the refusal must carry
    exactly that code — a 403 or a 500 is an outage wearing the same
    exception class, not the contract.
    """
    try:
        await _await_settled(fn)
    except expected as exc:
        actual_code = getattr(exc, "error_code", None)
        if error_code is not None and actual_code != error_code:
            note = f"{actual_code!r} (expected {error_code!r}): {str(exc)[:120]}"
            REPORT.append((name, "UNEXPECTED ERROR_CODE", note))
            print(f"  !! {name}: error_code={actual_code!r}, expected {error_code!r}")
            return
        note = f"{type(exc).__name__}: {str(exc)[:120]}"
        REPORT.append((name, "OK", note))
        print(f"  ok {name}  [{note}]")
        return
    except WikiError as exc:
        REPORT.append((name, f"UNEXPECTED {type(exc).__name__}", str(exc)[:160]))
        print(f"  !! {name}: {type(exc).__name__}: {str(exc)[:160]}")
        return
    REPORT.append((name, "NO ERROR", f"expected {expected.__name__}"))
    print(f"  !! {name}: expected {expected.__name__}, but the call succeeded")


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
ATTACHMENT_PAYLOAD = "attachment payload for the contract sweep\n"


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
    doomed = await wiki.page_add_comment(root.id, body="sweep comment 3")
    if comment is not None:
        await check(
            "page_add_comment (reply)",
            lambda: wiki.page_add_comment(
                root.id, body="sweep reply", parent_id=comment.id
            ),
        )
    await check(
        "page_delete_comment",
        lambda: wiki.page_delete_comment(root.id, comment_id=doomed.id),
        note_from_result=lambda r: f"comments_count={r.comments_count}",
    )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="sweep-", delete=False
    ) as handle:
        handle.write(ATTACHMENT_PAYLOAD)
        tmp_path = Path(handle.name)
    try:
        uploaded = await check(
            "page_upload_attachment",
            lambda: wiki.page_upload_attachment(root.id, file_path=str(tmp_path)),
        )
    finally:
        await asyncio.to_thread(tmp_path.unlink, True)
    attachment_id = None
    if uploaded is None or not uploaded.attachments:
        # A SKIP row each, not one row for both: the final gate only looks at
        # rows that exist, so a tool with no row at all leaves the sweep green
        # having never touched it.
        REPORT.extend(
            (name, "SKIP", "upload produced no attachment")
            for name in ("page_read_attachment_bytes", "page_download_attachment")
        )
    else:
        attachment_id = uploaded.attachments[0].id
        downloaded = await check(
            "page_read_attachment_bytes",
            lambda: wiki.page_read_attachment_bytes(root.id, file_id=attachment_id),
            note_from_result=lambda r: f"{len(r.content)} bytes, mime={r.mimetype}",
        )
        # The round-trip verdict has to be a REPORT row, not a note: check()
        # only escalates status for BaseModel/list results, so bytes are always
        # "OK" and an assertion smuggled into note_from_result could never fail
        # the sweep. Comparing raw bytes also keeps a non-UTF-8 body from
        # raising inside the note, which check() evaluates outside its
        # try/except and would turn into a traceback instead of a verdict.
        if downloaded is not None and downloaded.content != ATTACHMENT_PAYLOAD.encode():
            broken(
                "page_read_attachment_bytes round-trip",
                f"wrote {ATTACHMENT_PAYLOAD!r}, read back {downloaded.content[:120]!r}",
            )
        with tempfile.TemporaryDirectory(prefix="sweep-dl-") as download_dir:
            save_to = Path(download_dir) / "sweep-attachment.txt"
            saved = await check(
                "page_download_attachment",
                lambda: wiki.page_download_attachment(
                    root.id, file_id=attachment_id, save_to=str(save_to)
                ),
                note_from_result=lambda r: f"{r.size_bytes} bytes -> {r.path}",
            )
            if saved is not None:
                if downloaded is not None and saved.mimetype != downloaded.mimetype:
                    # The invariant this branch introduced: one file must not
                    # report one type when read into the conversation and
                    # another when saved to disk. Checked as agreement rather
                    # than against a literal, so it needs no assumption about
                    # what the API sends — and as a REPORT row, because a note
                    # could never fail the sweep (see above).
                    broken(
                        "page_download_attachment mimetype",
                        f"read says {downloaded.mimetype}, "
                        f"download says {saved.mimetype}",
                    )
                on_disk = save_to.read_bytes()
                if on_disk != ATTACHMENT_PAYLOAD.encode():
                    broken(
                        "page_download_attachment round-trip",
                        f"wrote {ATTACHMENT_PAYLOAD!r}, file holds {on_disk[:120]!r}",
                    )
                leftovers = [p.name for p in save_to.parent.iterdir() if p != save_to]
                if leftovers:
                    broken(
                        "page_download_attachment round-trip",
                        f"leftover files beside the target: {leftovers}",
                    )

    print(f"\n=== redirect cycle (on {base}/p-00) ===")
    if children:
        target = children[0]
        await check(
            "page_update (set redirect)",
            lambda: wiki.page_update(target.id, redirect_to_page_id=root.id),
        )
        await check(
            "page_get (redirect reads back)",
            lambda: wiki.page_get(target.id, fields=["redirect"]),
            note_from_result=lambda r: f"redirect={r.redirect}",
        )
        await check(
            "page_update (clear redirect)",
            lambda: wiki.page_update(target.id, clear_redirect=True),
        )

    print(f"\n=== page_edit cycle (on {base}/p-01) ===")
    # The page_edit tool is a client-side read-modify-write; the live
    # contract it leans on is that GET content, string-replaced and PUT
    # back, reads back verbatim (no server-side markup normalization).
    if len(children) > 1:
        editee = children[1]
        fetched = await check(
            "page_get (content for edit)",
            lambda: wiki.page_get(editee.id, fields=["content"]),
            note_from_result=lambda r: f"content={r.content!r}",
        )
        if fetched is not None and isinstance(fetched.content, str):
            edited = fetched.content.replace("page 1", "page 1 (edited)")
            await check(
                "page_update (edited content)",
                lambda: wiki.page_update(editee.id, content=edited),
            )
            reread = await check(
                "page_get (edit round-trips)",
                lambda: wiki.page_get(editee.id, fields=["content"]),
                note_from_result=lambda r: f"round-trips: {r.content == edited}",
            )
            if reread is not None and reread.content != edited:
                broken(
                    "page_edit round-trip",
                    f"wrote {edited!r}, read back {reread.content!r}",
                )

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
                "grid_move_row",
                lambda: wiki.grid_move_row(
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
            "grid_move_column",
            lambda: wiki.grid_move_column(
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
        # grid_delete last: it takes the fixture away. The endpoint answers
        # 204 No Content; the model fields are client-side acknowledgment,
        # so any body the API starts sending shows up here as extras.
        await check("grid_delete", lambda: wiki.grid_delete(grid_id))

    print("\n=== reads ===")
    # watch_identity: `/users/me` DECLARES identity/uid/cloud_uid — they are what
    # the authors search filter is built from — so the global suppression that
    # exists for user references must not apply here, or drift on the one
    # endpoint that owns those keys would be subtracted before it is reported.
    me = await check(
        "user_get_current",
        lambda: wiki.user_get_current(),
        note_from_result=lambda r: (
            f"username={r.username!r}, home_cluster={r.home_cluster!r}, "
            f"uid={(r.identity.uid if r.identity else None)!r}"
        ),
        watch_identity=True,
    )
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
        lambda: wiki.page_search("документация", limit=50),
    )
    own_uid = me.identity.uid if me is not None and me.identity is not None else None
    if own_uid is None:
        # A SKIP row, not a silent `if`: this check and user_get_current are the
        # two contracts the authors filter stands on, and a skip that prints
        # nothing lets both break under a green summary.
        REPORT.append(
            (
                "page_search (filters + highlight)",
                "SKIP",
                "user_get_current returned no identity.uid to filter by",
            )
        )
    else:
        # The sweep's own fixtures are owned by the token's user, so a search
        # filtered to that author with highlighting exercises the whole
        # filters+highlight wire shape against pages that must match.
        await check(
            "page_search (filters + highlight)",
            lambda: wiki.page_search(
                "sweep",
                limit=50,
                cluster=base,
                result_type="page",
                authors=[SearchAuthor(uid=own_uid)],
                highlight=True,
            ),
            note_from_result=lambda r: f"{len(r.results)} hits under {base!r}",
        )

    # The tool promises "slug equals this prefix or lies under it", which is now
    # the backend's job (probed 2026-08-18). Both halves are pinned here because
    # nothing in the unit suite can see them any more: the client-side sieve
    # that used to enforce them is gone.
    cluster_hits = await check(
        "page_search (cluster = segment boundary + self)",
        lambda: wiki.page_search("sweep", limit=50, cluster=f"{base}/p-00"),
        note_from_result=lambda r: f"{len(r.results)} hits",
    )
    if cluster_hits is not None:
        prefix = f"{base}/p-00"
        leaked = [
            item.slug
            for item in cluster_hits.results
            if item.slug
            and item.slug != prefix
            and not item.slug.startswith(prefix + "/")
        ]
        if leaked:
            broken(
                "page_search (cluster boundary)",
                f"cluster={prefix!r} returned slugs outside the subtree: {leaked[:5]}",
            )

    print("\n=== attachment deletion ===")
    if attachment_id is None:
        REPORT.append(
            ("page_delete_attachment", "SKIP", "no attachment id from the upload")
        )
    else:
        await check(
            "page_delete_attachment",
            lambda: wiki.page_delete_attachment(root.id, file_id=attachment_id),
        )

    print("\n=== page_clone ===")
    # One fixture, four contracts, matching the page_clone tool description:
    # the copy lands at the target with a NEW id, children do NOT follow
    # (clone is single-page — probed 2026-08-08, docs/api-notes.md), the
    # original stays, and cloning onto an occupied slug is refused.
    original = await check(
        "page_create (clone fixture)",
        lambda: wiki.page_create(
            slug=f"{base}/clone-src", title="Sweep clone fixture", content="original"
        ),
    )
    if original is not None:
        kid = await check(
            "page_create (clone kid)",
            lambda: wiki.page_create(
                slug=f"{base}/clone-src/kid", title="Sweep clone kid", content="kid"
            ),
        )
        copy = await check(
            "page_clone",
            lambda: wiki.page_clone(original.id, target=f"{base}/clone-dst"),
            note_from_result=lambda r: f"copy landed at {r.slug} (id={r.id})",
        )
        if copy is not None and copy.id == original.id:
            broken("page_clone (copy has a new id)", "copy kept the same id")
        await check(
            "page_get_by_slug (original stays)",
            lambda: wiki.page_get_by_slug(f"{base}/clone-src"),
        )
        if kid is not None:
            # Meaningful only when the kid exists: without it, PageNotFound
            # on clone-dst/kid proves nothing about clone semantics.
            await check_expected_error(
                "page_get_by_slug (children do not follow the clone)",
                lambda: wiki.page_get_by_slug(f"{base}/clone-dst/kid"),
                PageNotFound,
            )
        await check_expected_error(
            "page_clone (onto an occupied slug is refused)",
            lambda: wiki.page_clone(original.id, target=f"{base}/clone-src"),
            WikiApiError,
            error_code="SLUG_OCCUPIED",
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
