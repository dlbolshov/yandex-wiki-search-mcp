"""Probe the 2026-08 documentation drop against the live API.

Yandex published a full API reference (search included) and a hosted MCP
server (mcp.wiki.yandex.net). This script checks, per documented claim,
whether the wire actually honors it today:

- search: cursor pagination, server-side filters (type/cluster/authors/dates),
  order_by, highlight — everything our 2026-08-02 probes said did NOT exist;
- OAuth scope enforcement (wiki:read token attempting writes);
- comment threads, comment deletion, resolve_status/reactions fields;
- attachment download (by id and by url) and deletion;
- per-page access management (POST/DELETE /pages/{idx}/access);
- redirect set/clear through page update;
- descendants `actuality` query param;
- the official MCP server's tools/list.

WRITES to the wiki under the given base slug — use a scratch spot in your
personal section. Cleans up after itself unless --keep is passed.

Usage:
    set -a; . ./.env; set +a
    uv run python scripts/docs_probe.py users/<login>/docs-probe [--query "..."]
"""

import argparse
import asyncio
import itertools
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_wiki.wiki.custom.client import WikiClient

API = os.environ.get("WIKI_API_BASE_URL", "https://api.wiki.yandex.net")
MCP_URL = os.environ.get("WIKI_OFFICIAL_MCP_URL", "https://mcp.wiki.yandex.net")

REPORT: list[tuple[str, str, str]] = []


def record(probe: str, verdict: str, note: str = "") -> None:
    REPORT.append((probe, verdict, note))
    print(f"  [{verdict}] {probe}" + (f" — {note}" if note else ""))


def headers(token: str) -> dict[str, str]:
    h = {"Authorization": f"OAuth {token}", "Content-Type": "application/json"}
    if os.environ.get("WIKI_ORG_ID"):
        h["X-Org-Id"] = os.environ["WIKI_ORG_ID"]
    if os.environ.get("WIKI_CLOUD_ORG_ID"):
        h["X-Cloud-Org-Id"] = os.environ["WIKI_CLOUD_ORG_ID"]
    return h


async def req(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    token: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, Any]:
    async with session.request(
        method,
        f"{API}{path}",
        headers=headers(token),
        json=body,
        params=params,
    ) as resp:
        if resp.content_type == "application/json":
            return resp.status, await resp.json()
        return resp.status, await resp.read()


async def search(
    session: aiohttp.ClientSession, token: str, body: dict[str, Any]
) -> tuple[int, Any]:
    return await req(session, "POST", "/v1/search", token, body=body)


def slugs(payload: Any) -> list[str]:
    return [r.get("slug", "?") for r in payload.get("results", [])]


# --------------------------------------------------------------------------
# probe groups
# --------------------------------------------------------------------------


async def probe_search(session: aiohttp.ClientSession, token: str, query: str) -> None:
    print("\n== search: documented contract vs wire ==")
    status, base = await search(session, token, {"query": query, "limit": 3})
    if status != 200 or not base.get("results"):
        record("search baseline", "SKIP", f"HTTP {status}, no results for {query!r}")
        return
    base_slugs = slugs(base)
    record(
        "search baseline",
        "OK",
        f"{len(base_slugs)} results, next_cursor={base.get('next_cursor')!r}",
    )

    # cursor pagination: page 2 must differ from page 1 if live
    status, page2 = await search(
        session, token, {"query": query, "limit": 3, "cursor": 2}
    )
    if status == 200:
        moved = slugs(page2) != base_slugs
        live = moved or base.get("next_cursor") is not None
        record(
            "search cursor pagination (default mode)",
            "LIVE" if live else "DEAD",
            f"page2 differs: {moved}, next_cursor: {base.get('next_cursor')!r}",
        )
    else:
        record(
            "search cursor pagination (default mode)", "ERR", f"HTTP {status}: {page2}"
        )

    # the same walk with highlight=true: a different backend answers there,
    # and it is the one that actually paginates (mode split found 2026-08-25)
    status, hl1 = await search(
        session, token, {"query": query, "limit": 3, "highlight": True}
    )
    if status == 200 and hl1.get("results"):
        hl1_slugs = slugs(hl1)
        status2, hl2 = await search(
            session,
            token,
            {"query": query, "limit": 3, "highlight": True, "cursor": 2},
        )
        if status2 == 200:
            moved = slugs(hl2) != hl1_slugs
            live = moved or hl1.get("next_cursor") is not None
            record(
                "search cursor pagination (highlight mode)",
                "LIVE" if live else "DEAD",
                f"page2 differs: {moved}, next_cursor: {hl1.get('next_cursor')!r}, "
                f"page cap: asked limit=3, got {len(hl1_slugs)}",
            )
        else:
            record(
                "search cursor pagination (highlight mode)",
                "ERR",
                f"HTTP {status2}: {hl2}",
            )
    else:
        record(
            "search cursor pagination (highlight mode)",
            "SKIP" if status == 200 else "ERR",
            f"HTTP {status}, {len(hl1.get('results', []))} results",
        )

    # page_size must still be ignored (10 default results, not 3)
    status, ps = await search(session, token, {"query": query, "page_size": 3})
    if status == 200:
        n = len(ps.get("results", []))
        record(
            "search page_size still ignored",
            "YES" if n != 3 else "NO",
            f"asked page_size=3, got {n}",
        )

    # server-side type filter
    status, files = await search(
        session, token, {"query": query, "limit": 10, "filters": {"type": "file"}}
    )
    if status == 200:
        types = {r.get("type") for r in files.get("results", [])}
        n = len(files.get("results", []))
        verdict = (
            "LIVE" if types <= {"file"} and n > 0 else ("EMPTY" if n == 0 else "DEAD")
        )
        record(
            "search filters.type=file", verdict, f"{n} results, types={sorted(types)}"
        )
    else:
        record("search filters.type=file", "ERR", f"HTTP {status}: {files}")

    # server-side cluster filter: take the first segment of a real result
    cluster = base_slugs[0].split("/")[0]
    status, cl = await search(
        session, token, {"query": query, "limit": 10, "filters": {"cluster": cluster}}
    )
    if status == 200:
        got = slugs(cl)
        inside = all(s.startswith(cluster) for s in got)
        verdict = "LIVE" if got and inside else ("EMPTY" if not got else "DEAD")
        record(
            f"search filters.cluster={cluster!r}",
            verdict,
            f"{len(got)} results, all under prefix: {inside}",
        )
    else:
        record("search filters.cluster", "ERR", f"HTTP {status}: {cl}")

    # date filter falsification: created strictly in the future must yield 0
    status, future = await search(
        session,
        token,
        {
            "query": query,
            "limit": 10,
            "filters": {
                "created_at": {
                    "from": "2030-01-01T00:00:00Z",
                    "to": "2031-01-01T00:00:00Z",
                }
            },
        },
    )
    if status == 200:
        n = len(future.get("results", []))
        record(
            "search filters.created_at (future ⇒ empty)",
            "LIVE" if n == 0 else "DEAD",
            f"{n} results for created_at.from=2030",
        )
    else:
        record("search filters.created_at", "ERR", f"HTTP {status}: {future}")

    # order_by: compare both documented values against the relevancy baseline
    status, plain = await search(session, token, {"query": query, "limit": 10})
    plain_slugs = slugs(plain) if status == 200 else []
    for order in ("modified_date", "creation_date"):
        status, ordered = await search(
            session, token, {"query": query, "limit": 10, "order_by": order}
        )
        if status != 200:
            record(f"search order_by={order}", "ERR", f"HTTP {status}")
            continue
        dates = [
            r.get("modified_at")
            for r in ordered.get("results", [])
            if r.get("modified_at")
        ]
        desc = all(a >= b for a, b in itertools.pairwise(dates))
        asc = all(a <= b for a, b in itertools.pairwise(dates))
        reordered = slugs(ordered) != plain_slugs
        record(
            f"search order_by={order}",
            "LIVE" if (desc or asc) and len(dates) > 2 else "UNCLEAR",
            f"monotone desc={desc}/asc={asc}, differs from relevancy order: {reordered}",
        )

    # highlight: diff against the same query without it, count real markup only
    status, hl = await search(
        session, token, {"query": query, "limit": 10, "highlight": True}
    )
    if status == 200 and plain_slugs:
        plain_content = {
            r["slug"]: r.get("content") or "" for r in plain.get("results", [])
        }
        changed = [
            r
            for r in hl.get("results", [])
            if r.get("content")
            and r["content"] != plain_content.get(r["slug"], r["content"])
        ]
        tags = [
            m
            for m in ("<b>", "<em>", "<mark>", "<hlword", "<span")
            if any(m in (r.get("content") or "") for r in hl.get("results", []))
        ]
        sample = changed[0]["content"][:160] if changed else ""
        record(
            "search highlight=true",
            "LIVE" if changed or tags else "DEAD",
            f"{len(changed)}/10 snippets differ from highlight=false, tags seen: {tags}; sample: {sample!r}",
        )


async def probe_scopes(
    session: aiohttp.ClientSession, ro_token: str, page_id: int, base: str
) -> int | None:
    print("\n== OAuth scope enforcement (wiki:read token) ==")
    status, _ = await req(session, "GET", f"/v1/pages/{page_id}", ro_token)
    record(
        "read with wiki:read token", "OK" if status == 200 else "ERR", f"HTTP {status}"
    )

    status, body = await req(
        session, "POST", f"/v1/pages/{page_id}", ro_token, body={"title": "scope probe"}
    )
    enforced_update = status in (401, 403)
    record(
        "update with wiki:read token",
        "ENFORCED" if enforced_update else "NOT ENFORCED",
        f"HTTP {status}, error_code={body.get('error_code') if isinstance(body, dict) else '-'}",
    )

    status, body = await req(
        session,
        "POST",
        "/v1/pages",
        ro_token,
        body={"slug": f"{base}/scope-probe", "title": "scope probe", "content": "x"},
    )
    enforced_create = status in (401, 403)
    created_id = body.get("id") if isinstance(body, dict) and status == 200 else None
    record(
        "create with wiki:read token",
        "ENFORCED" if enforced_create else "NOT ENFORCED",
        f"HTTP {status}, error_code={body.get('error_code') if isinstance(body, dict) else '-'}",
    )
    return created_id


async def probe_comments(
    session: aiohttp.ClientSession, token: str, page_id: int
) -> None:
    print("\n== comments: threads, deletion, new fields ==")
    status, c1 = await req(
        session,
        "POST",
        f"/v1/pages/{page_id}/comments",
        token,
        body={"body": "probe root"},
    )
    if status != 200:
        record("comment create", "ERR", f"HTTP {status}: {c1}")
        return
    c1_id = c1["id"]
    status, c2 = await req(
        session,
        "POST",
        f"/v1/pages/{page_id}/comments",
        token,
        body={"body": "probe reply", "parent_id": c1_id},
    )
    c2_id = c2.get("id") if status == 200 else None
    record("comment + reply create", "OK", f"ids {c1_id}, {c2_id}")

    status, listing = await req(session, "GET", f"/v1/pages/{page_id}/comments", token)
    thread_ids: set[int] = set()
    if status == 200 and listing.get("results"):
        keys = set(listing["results"][0])
        linkage = [
            (c["id"], c.get("parent_id"), c.get("thread_id"))
            for c in listing["results"]
        ]
        thread_ids = {
            c.get("thread_id") for c in listing["results"] if c.get("thread_id")
        }
        record(
            "comment fields",
            "OK",
            f"resolve_status: {'resolve_status' in keys}, reactions: {'reactions' in keys}, "
            f"(id, parent_id, thread_id): {linkage}",
        )

    for label, probe_id in (
        ("root comment id", c1_id),
        *[("thread_id", t) for t in sorted(thread_ids) if t != c1_id],
    ):
        status, thread = await req(
            session, "GET", f"/v1/pages/{page_id}/comments/{probe_id}/thread", token
        )
        if status == 200:
            got = [c["id"] for c in thread.get("results", [])]
            record(
                f"GET .../comments/{{{label}}}/thread",
                "LIVE",
                f"comments in thread: {got}",
            )
        else:
            record(
                f"GET .../comments/{{{label}}}/thread",
                "ERR",
                f"HTTP {status}: {thread}",
            )

    if c2_id is not None:
        status, deleted = await req(
            session, "DELETE", f"/v1/pages/{page_id}/comments/{c2_id}", token
        )
        record(
            "DELETE comment",
            "LIVE" if status == 200 else "ERR",
            f"HTTP {status}, comments_count={deleted.get('comments_count') if isinstance(deleted, dict) else '-'}",
        )


async def probe_attachments(
    session: aiohttp.ClientSession,
    wiki: WikiClient,
    token: str,
    page_id: int,
    slug: str,
) -> None:
    print("\n== attachments: download and delete ==")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="docs-probe-", delete=False
    ) as handle:
        handle.write("docs-probe attachment payload\n")
        tmp = Path(handle.name)
    try:
        await wiki.page_upload_attachment(page_id, file_path=str(tmp))
    except Exception as exc:  # probe records, never raises
        record("attachment upload (WikiClient)", "ERR", repr(exc)[:120])
        return
    finally:
        await asyncio.to_thread(tmp.unlink, True)

    status, listing = await req(
        session, "GET", f"/v1/pages/{page_id}/attachments", token
    )
    results = listing.get("results", []) if isinstance(listing, dict) else []
    if not results:
        record("attachment listing", "ERR", f"HTTP {status}, empty")
        return
    att = results[0]
    file_id, name = att.get("id"), att.get("name")
    record(
        "attachment listing", "OK", f"id={file_id}, name={name!r}, keys={sorted(att)}"
    )

    status, blob = await req(
        session, "GET", f"/v1/pages/{page_id}/attachments/{file_id}/download", token
    )
    record(
        "GET .../attachments/{id}/download",
        "LIVE" if status == 200 else "ERR",
        f"HTTP {status}, {len(blob) if isinstance(blob, (bytes, bytearray)) else '?'} bytes",
    )

    status, blob = await req(
        session,
        "GET",
        "/v1/pages/attachments/download_by_url",
        token,
        params={"url": f"{slug}/.files/{name}", "download": "true"},
    )
    record(
        "GET .../attachments/download_by_url",
        "LIVE" if status == 200 else "ERR",
        f"HTTP {status}",
    )

    status, _ = await req(
        session, "DELETE", f"/v1/pages/{page_id}/attachments/{file_id}", token
    )
    record(
        "DELETE attachment", "LIVE" if status in (200, 204) else "ERR", f"HTTP {status}"
    )


async def probe_access(
    session: aiohttp.ClientSession, token: str, page_id: int, my_uid: str | None
) -> None:
    print("\n== per-page access management ==")
    status, created = await req(
        session,
        "POST",
        f"/v1/pages/{page_id}/access",
        token,
        body={
            "user": {"uid": my_uid},
            "role": "reader",
            "inheritance": "not_inherited",
        },
    )
    note = (
        f"HTTP {status}, "
        f"{created.get('error_code') if isinstance(created, dict) and status != 200 else created}"
    )
    record(
        "POST /pages/{idx}/access (grant self reader)",
        "LIVE" if status == 200 else "INFO",
        str(note)[:160],
    )
    access_id = (
        created.get("id") if isinstance(created, dict) and status == 200 else None
    )
    if access_id:
        status, _ = await req(
            session,
            "DELETE",
            f"/v1/pages/{page_id}/access/{access_id}",
            token,
            params={"prevent_selflock": "true"},
        )
        record(
            "DELETE /pages/{idx}/access/{access_id}",
            "LIVE" if status in (200, 204) else "ERR",
            f"HTTP {status}",
        )


async def probe_redirect(
    session: aiohttp.ClientSession, token: str, from_id: int, to_id: int
) -> None:
    print("\n== redirect via page update ==")
    status, _ = await req(
        session,
        "POST",
        f"/v1/pages/{from_id}",
        token,
        body={"redirect": {"page": {"id": to_id}}},
    )
    ok = status == 200
    record("set redirect", "LIVE" if ok else "ERR", f"HTTP {status}")
    if ok:
        status, page = await req(
            session, "GET", f"/v1/pages/{from_id}", token, params={"fields": "redirect"}
        )
        record(
            "redirect visible on page",
            "OK" if isinstance(page, dict) and page.get("redirect") else "NO",
            f"redirect={page.get('redirect') if isinstance(page, dict) else '-'}",
        )
        status, _ = await req(
            session,
            "POST",
            f"/v1/pages/{from_id}",
            token,
            body={"redirect": {"page": None}},
        )
        record("clear redirect", "LIVE" if status == 200 else "ERR", f"HTTP {status}")


async def probe_descendants(
    session: aiohttp.ClientSession, token: str, page_id: int
) -> None:
    print("\n== descendants by id + actuality param ==")
    status, body = await req(
        session,
        "GET",
        f"/v1/pages/{page_id}/descendants",
        token,
        params={"actuality": "actual", "include_self": "true"},
    )
    n = len(body.get("results", [])) if isinstance(body, dict) else "?"
    record(
        "GET /pages/{idx}/descendants?actuality=actual",
        "LIVE" if status == 200 else "ERR",
        f"HTTP {status}, {n} results",
    )


async def probe_official_mcp(token: str) -> None:
    print("\n== official MCP server (mcp.wiki.yandex.net) ==")
    h = headers(token)
    h["Accept"] = "application/json, text/event-stream"

    async def rpc(
        session: aiohttp.ClientSession, payload: dict[str, Any], extra: dict[str, str]
    ) -> tuple[int, dict[str, str], Any]:
        async with session.post(MCP_URL, headers={**h, **extra}, json=payload) as resp:
            raw = await resp.text()
            data: Any = None
            if resp.content_type == "text/event-stream":
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        data = json.loads(line[5:].strip())
            elif raw:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = raw[:200]
            return resp.status, dict(resp.headers), data

    async with aiohttp.ClientSession() as session:
        status, resp_headers, init = await rpc(
            session,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "docs-probe", "version": "0"},
                },
            },
            {},
        )
        if status != 200 or not isinstance(init, dict):
            record(
                "official MCP initialize", "ERR", f"HTTP {status}: {str(init)[:160]}"
            )
            return
        server_info = init.get("result", {}).get("serverInfo", {})
        record(
            "official MCP initialize",
            "OK",
            f"{server_info.get('name')} {server_info.get('version')}, "
            f"protocol {init.get('result', {}).get('protocolVersion')}",
        )
        sid = {k: v for k, v in resp_headers.items() if k.lower() == "mcp-session-id"}
        session_header = {"Mcp-Session-Id": next(iter(sid.values()))} if sid else {}
        await rpc(
            session,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_header,
        )
        status, _, tools = await rpc(
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            session_header,
        )
        if status == 200 and isinstance(tools, dict) and "result" in tools:
            names = sorted(t["name"] for t in tools["result"].get("tools", []))
            record("official MCP tools/list", "OK", f"{len(names)} tools")
            for name in names:
                print(f"      - {name}")
        else:
            record(
                "official MCP tools/list", "ERR", f"HTTP {status}: {str(tools)[:160]}"
            )


# --------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_slug", help="scratch slug, e.g. users/<login>/docs-probe")
    parser.add_argument(
        "--query", default="вики", help="search query with hits in your org"
    )
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    parser.add_argument(
        "--skip-mcp", action="store_true", help="skip the official MCP probe"
    )
    args = parser.parse_args()

    token = os.environ.get("WIKI_TOKEN")
    if not token:
        raise SystemExit("WIKI_TOKEN is not set")
    ro_token = os.environ.get("WIKI_TOKEN_READ_ONLY")
    base = args.base_slug.strip("/")

    wiki = WikiClient(
        base_url=API,
        token=token,
        iam_token=None,
        auth_scheme="OAuth",
        cloud_org_id=os.environ.get("WIKI_CLOUD_ORG_ID"),
        org_id=os.environ.get("WIKI_ORG_ID"),
        max_retries=1,
    )

    created: list[int] = []
    async with aiohttp.ClientSession() as session, wiki:
        status, me = await req(session, "GET", "/v1/users/me", token)
        my_uid = None
        if status == 200 and isinstance(me, dict):
            my_uid = (me.get("identity") or {}).get("uid") or me.get("uid")
        record(
            "GET /users/me",
            "OK" if status == 200 else "ERR",
            f"HTTP {status}, uid found: {my_uid is not None}",
        )

        # sandbox: root + two children
        pages: dict[str, int] = {}
        for name, title in (("", "docs probe"), ("/a", "probe A"), ("/b", "probe B")):
            slug = f"{base}{name}"
            status, page = await req(
                session,
                "POST",
                "/v1/pages",
                token,
                body={"slug": slug, "title": title, "content": f"docs probe {title}"},
            )
            if status == 200:
                pages[slug] = page["id"]
                created.append(page["id"])
            else:
                record(f"create {slug}", "ERR", f"HTTP {status}: {page}")
        if len(pages) < 3:
            print("sandbox creation failed, aborting")
            return 1
        root_id = pages[base]
        a_id = pages[f"{base}/a"]
        b_id = pages[f"{base}/b"]
        record("sandbox pages", "OK", f"{len(pages)} created under {base}")

        await probe_search(session, token, args.query)

        if ro_token:
            ro_created = await probe_scopes(session, ro_token, a_id, base)
            if ro_created:
                created.append(ro_created)
        else:
            record("scope probes", "SKIP", "WIKI_TOKEN_READ_ONLY not set")

        await probe_comments(session, token, a_id)
        await probe_attachments(session, wiki, token, a_id, f"{base}/a")
        await probe_access(session, token, a_id, my_uid)
        await probe_redirect(session, token, b_id, a_id)
        await probe_descendants(session, token, root_id)

        if not args.keep:
            print("\n== cleanup ==")
            for page_id in sorted(created, reverse=True):
                status, _ = await req(session, "DELETE", f"/v1/pages/{page_id}", token)
                print(f"  DELETE page {page_id}: HTTP {status}")

    if not args.skip_mcp:
        await probe_official_mcp(token)

    print("\n=== verdicts ===")
    for probe, verdict, note in REPORT:
        print(f"  {verdict:14} {probe:48} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
