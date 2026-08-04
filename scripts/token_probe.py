"""Live READ-ONLY probe for the v0.8.0 token-economy work (ROADMAP).

For each read endpoint this prints, against the real Wiki API:
  * raw response size,
  * extra keys the API sends beyond our declared model fields (the
    extra="allow" leak) and their weight,
  * weight of declared-but-null fields (the exclude_none candidate),
  * what actually goes over the MCP wire today (structuredContent +
    pretty-printed text duplicate) vs a slim projection.

Makes no writes. Usage:

    uv run python scripts/token_probe.py
    uv run python scripts/token_probe.py --descendants-slug tech-doc \\
        --search-query "деплой" --grid-slug some/grid/page
"""

import argparse
import asyncio
import json
import statistics
import sys
from typing import Any

import pydantic_core
from pydantic import BaseModel

from mcp_wiki.settings import Settings
from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.proto.types.pages import (
    DescendantItem,
    DescendantsResponse,
    PageComment,
    SearchResponse,
    SearchResultItem,
    WikiPage,
)


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


def size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def wire_now(model: BaseModel) -> tuple[int, int]:
    """(text half, structured half) exactly as FastMCP serializes them today."""
    text = len(pydantic_core.to_json(model, fallback=str, indent=2).decode())
    structured = size(model.model_dump(mode="json", by_alias=True))
    return text, structured


def analyze_items(
    items: list[dict[str, Any]], model_cls: type[BaseModel], label: str
) -> None:
    declared = set(model_cls.model_fields)
    extra_weight = 0
    extra_keys: dict[str, int] = {}
    null_weight = 0
    null_keys: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            entry = len(json.dumps(key)) + 1 + size(value) + 1
            if key not in declared:
                extra_weight += entry
                extra_keys[key] = extra_keys.get(key, 0) + entry
            elif value is None:
                null_weight += entry
                null_keys[key] = null_keys.get(key, 0) + entry
    print(f"  [{label}] n={len(items)}")
    if extra_keys:
        ranked = sorted(extra_keys.items(), key=lambda kv: -kv[1])
        listing = ", ".join(f"{k}={v}" for k, v in ranked)
        print(f"    extras beyond model: {extra_weight} chars ({listing})")
    else:
        print("    extras beyond model: none")
    if null_keys:
        listing = ", ".join(f"{k}={v}" for k, v in sorted(null_keys.items()))
        print(f"    declared-but-null:   {null_weight} chars ({listing})")
    else:
        print("    declared-but-null:   none")


def report_wire(model: BaseModel, slim: Any) -> None:
    text, structured = wire_now(model)
    slim_size = len(json.dumps(slim, ensure_ascii=False, separators=(",", ":")))
    total = text + structured
    print(f"    MCP wire today: {total} chars (text {text} + structured {structured})")
    print(
        f"    slim projection: {slim_size} chars single, {slim_size * 2} doubled "
        f"(vs {total}: -{100 - 100 * slim_size * 2 // total}% doubled, "
        f"-{100 - 100 * slim_size // total}% single)"
    )


async def probe_search(wiki: WikiClient, query: str) -> None:
    print(f"\n=== page_search (query={query!r}, limit=50) ===")
    # The endpoint reads "limit" and silently ignores "page_size" — sending
    # the latter would measure 10 results while claiming 50 (api-notes.md).
    payload = await wiki._request(
        "POST", "v1/search", json_body={"query": query, "limit": 50}
    )
    raw = json.loads(payload)
    print(f"  raw API response: {size(raw)} chars")
    results = raw.get("results", [])
    envelope = {k: v for k, v in raw.items() if k != "results"}
    print(f"  envelope keys: {sorted(envelope)}")
    if results:
        analyze_items(results, SearchResultItem, "result item")
        bodies = [len(r.get("content") or "") for r in results]
        print(
            f"    snippets (content): min={min(bodies)} "
            f"med={int(statistics.median(bodies))} "
            f"max={max(bodies)} total={sum(bodies)}"
        )
        over = sum(1 for b in bodies if b > 200)
        print(f"    snippets longer than 200 chars: {over}/{len(bodies)}")
    slim = {
        "results": [
            {
                "url": r.get("url"),
                "slug": r.get("slug"),
                "title": r.get("title"),
                "type": r.get("type"),
                "snippet": (r.get("content") or "")[:200],
            }
            for r in results
        ],
        "next_cursor": raw.get("next_cursor"),
    }
    try:
        model: BaseModel = SearchResponse.model_validate(raw)
    except Exception as exc:
        print(f"    !! MODEL DOES NOT FIT LIVE API: {type(exc).__name__}")
        for line in str(exc).splitlines()[:4]:
            print(f"       {line}")
        return
    report_wire(model, slim)


async def probe_descendants(wiki: WikiClient, slug: str) -> None:
    print(f"\n=== page_get_descendants (slug={slug!r}, page_size=100) ===")
    payload = await wiki._request(
        "GET",
        "v1/pages/descendants",
        params={"slug": slug, "include_self": "true", "page_size": 100},
    )
    raw = json.loads(payload)
    print(f"  raw API response: {size(raw)} chars")
    results = raw.get("results", [])
    print(f"  envelope keys: {sorted(k for k in raw if k != 'results')}")
    if results:
        analyze_items(results, DescendantItem, "tree item")
        present: dict[str, int] = {}
        for item in results:
            for key, value in item.items():
                if value is not None:
                    present[key] = present.get(key, 0) + 1
        print(f"    non-null key coverage: {dict(sorted(present.items()))}")
    slim = {
        "results": [{"id": r.get("id"), "slug": r.get("slug")} for r in results],
        "next_cursor": raw.get("next_cursor"),
    }
    try:
        model: BaseModel = DescendantsResponse.model_validate(raw)
    except Exception as exc:
        print(f"    !! MODEL DOES NOT FIT LIVE API: {type(exc).__name__}")
        for line in str(exc).splitlines()[:4]:
            print(f"       {line}")
        return
    report_wire(model, slim)


async def probe_page(wiki: WikiClient, slug: str) -> int | None:
    print(f"\n=== page_get (slug={slug!r}, all fields) ===")
    fields = "content,attributes,breadcrumbs,redirect,access_policy,access_lists,owner"
    payload = await wiki._request(
        "GET", "v1/pages", params={"slug": slug, "fields": fields}
    )
    raw = json.loads(payload)
    print(f"  raw API response: {size(raw)} chars")
    weights = sorted(((size(v), k) for k, v in raw.items()), reverse=True)
    for weight, key in weights[:10]:
        print(f"    {weight:>7} chars  {key}")
    analyze_items([raw], WikiPage, "page object")
    return raw.get("id")


async def probe_comments(wiki: WikiClient, page_id: int) -> None:
    print(f"\n=== page_get_comments (page_id={page_id}) ===")
    payload = await wiki._request(
        "GET", f"v1/pages/{page_id}/comments", params={"page_size": 25}
    )
    raw = json.loads(payload)
    print(f"  raw API response: {size(raw)} chars")
    results = raw.get("results", [])
    print(f"  envelope keys: {sorted(k for k in raw if k != 'results')}")
    if results:
        analyze_items(results, PageComment, "comment")
    else:
        print("  no comments on this page (envelope measured anyway)")


async def probe_grid(wiki: WikiClient, slug: str) -> None:
    print(f"\n=== grid_get (slug={slug!r}) ===")
    payload = await wiki._request("GET", "v1/pages", params={"slug": slug})
    page_id = json.loads(payload).get("id")
    payload = await wiki._request("GET", f"v1/grids/{page_id}")
    raw = json.loads(payload)
    print(f"  raw API response: {size(raw)} chars")
    weights = sorted(((size(v), k) for k, v in raw.items()), reverse=True)
    for weight, key in weights[:8]:
        print(f"    {weight:>7} chars  {key}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-query", default="документация")
    parser.add_argument("--descendants-slug", default="users/david")
    parser.add_argument("--page-slug", default="users/david")
    parser.add_argument("--grid-slug", default=None)
    args = parser.parse_args()

    settings = Settings()
    async with make_client(settings) as wiki:
        for coro in (
            probe_search(wiki, args.search_query),
            probe_descendants(wiki, args.descendants_slug),
        ):
            try:
                await coro
            except Exception as exc:
                print(f"    !! PROBE FAILED: {type(exc).__name__}: {exc}")
        page_id = await probe_page(wiki, args.page_slug)
        if page_id is not None:
            await probe_comments(wiki, page_id)
        if args.grid_slug:
            await probe_grid(wiki, args.grid_slug)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
