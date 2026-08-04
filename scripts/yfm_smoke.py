"""Live smoke for the YFM v0.7.0 work (ROADMAP M6, run once before coding).

Creates probe pages under the given base slug (use a scratch spot in your
personal section!), reads them back and prints a report. Rendering cannot be
checked via the API — open the printed URLs and eyeball them.

WARNING: this script WRITES to the wiki configured in .env and ignores
WIKI_READ_ONLY (that flag only gates MCP tool registration, not WikiClient).

Usage:
    uv run python scripts/yfm_smoke.py users/<login>/yfm-smoke
    uv run python scripts/yfm_smoke.py users/<login>/yfm-smoke --legacy-slug some/old/page
    uv run python scripts/yfm_smoke.py users/<login>/yfm-smoke --cleanup
"""

import argparse
import asyncio
import sys

from mcp_wiki.settings import Settings
from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import WikiError

FENCE = "```"

BATTERY_SECTIONS = [
    ("note", '{% note info "Заметка" %}\n\nТекст заметки.\n\n{% endnote %}'),
    ("cut", '{% cut "Раскрыть" %}\n\nСкрытый текст.\n\n{% endcut %}'),
    (
        "tabs",
        "{% list tabs %}\n\n- Таб 1\n\n  Контент таба 1.\n\n"
        "- Таб 2\n\n  Контент таба 2.\n\n{% endlist %}",
    ),
    (
        "yfm-table",
        "#|\n|| Ячейка 1 | Ячейка 2 ||\n|| Ячейка 3 | Ячейка 4 ||\n|#",
    ),
    ("gfm-pipe-table", "| A | B |\n|---|---|\n| 1 | 2 |"),
    ("gfm-alert", "> [!NOTE]\n> Это GFM-алерт (в YFM не поддерживается?)."),
    (
        "html-details",
        "<details><summary>Спойлер</summary>HTML внутри markdown</details>",
    ),
    ("task-list", "- [ ] не сделано\n- [x] сделано"),
    (
        "fence-with-directive",
        f"{FENCE}\n{{% note %}} внутри код-фенса — не директива\n{FENCE}",
    ),
]

BATTERY_CONTENT = "# YFM battery\n\n" + "\n\n".join(
    f"## {name}\n\n{body}" for name, body in BATTERY_SECTIONS
)

APPEND_FRAGMENT = "\n\n## appended\n\nДописано через append-content (bottom)."


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


async def probe_battery(wiki: WikiClient, base_slug: str, web_base_url: str) -> None:
    print("\n=== 1. YFM battery page ===")
    slug = f"{base_slug}/battery"
    page = await wiki.page_create(
        slug=slug, title="YFM battery", content=BATTERY_CONTENT
    )
    got = await wiki.page_get(page.id, fields=["content", "attributes"])
    stored = got.content if isinstance(got.content, str) else repr(got.content)
    if stored == BATTERY_CONTENT:
        print("  stored content is byte-identical to what was sent")
    else:
        print("  stored content DIFFERS from what was sent:")
        sent_lines = BATTERY_CONTENT.splitlines()
        got_lines = stored.splitlines()
        for i, (a, b) in enumerate(zip(sent_lines, got_lines, strict=False)):
            if a != b:
                print(f"    first diff at line {i}: sent={a!r} got={b!r}")
                break
        if len(sent_lines) != len(got_lines):
            print(f"    line count: sent={len(sent_lines)} got={len(got_lines)}")

    print("\n=== 2. append-content into the battery page ===")
    try:
        await wiki.page_append_content(page.id, content=APPEND_FRAGMENT)
        print("  append (bottom) -> OK")
    except WikiError as exc:
        print(f"  append (bottom) -> FAIL {type(exc).__name__}: {exc}")

    print(f"\n  RENDER CHECK -> {web_base_url}/{slug}")


async def probe_legacy(wiki: WikiClient, legacy_slug: str) -> None:
    print("\n=== 3. legacy page probe ===")
    try:
        page = await wiki.page_get_by_slug(legacy_slug, fields=["attributes"])
        print(f"  {legacy_slug}: page_type={page.page_type!r} id={page.id}")
    except WikiError as exc:
        print(f"  {legacy_slug}: FAIL {type(exc).__name__}: {exc}")


async def cleanup(wiki: WikiClient, base_slug: str) -> None:
    print(f"\n=== cleanup: deleting descendants of {base_slug} ===")
    await _delete_silent(wiki, f"{base_slug}/battery")


async def _delete_silent(wiki: WikiClient, slug: str) -> None:
    try:
        page = await wiki.page_get_by_slug(slug)
        await wiki.page_delete(page.id)
        print(f"  deleted {slug} (id={page.id})")
    except WikiError:
        print(f"  skip {slug} (not found)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_slug", help="scratch slug, e.g. users/<login>/yfm-smoke")
    parser.add_argument("--legacy-slug", help="slug of an old-editor page to inspect")
    parser.add_argument(
        "--cleanup", action="store_true", help="delete previously created probe pages"
    )
    args = parser.parse_args()

    settings = Settings()
    wiki = make_client(settings)
    await wiki.prepare()
    try:
        if args.cleanup:
            await cleanup(wiki, args.base_slug)
            return 0
        await probe_battery(wiki, args.base_slug, settings.wiki_web_base_url)
        if args.legacy_slug:
            await probe_legacy(wiki, args.legacy_slug)
        print("\nDone. Re-run with --cleanup to remove the probe pages.")
    finally:
        await wiki.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
