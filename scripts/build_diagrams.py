"""Rebuild the Archify diagrams in docs/diagrams/ from their sources.

Each source in docs/diagrams/src/<lang>/<name>.json is an Archify candidate
pinned to a commit of this repository. For every one this script:

1. runs Archify's `finalize` (schema, layout, source-evidence and real-browser
   checks) into a scratch folder under `.archify/build/`, so the receipts it
   writes stay out of the tree;
2. copies the checked HTML to docs/diagrams/<lang>/<name>.html;
3. drives that HTML in headless Chrome and clicks the viewer's own
   "SVG · Auto" export, saving docs/diagrams/<lang>/<name>.svg — the static,
   light/dark-adaptive image docs/architecture*.md embed.

Archify is not a project dependency. Install it once with
`npx skills add tt-a1i/archify -g` (it needs Node 18+), and have Chrome or
Chromium on PATH. Then:

    uv run python scripts/build_diagrams.py            # everything
    uv run python scripts/build_diagrams.py architecture --lang en
    uv run python scripts/build_diagrams.py --skip-svg

A diagram cites line ranges at the commit in its `meta.repository.revision`.
When the code it describes changes, update the source (text and ranges, then
the revision) and rebuild; docs/architecture.md → "Diagrams" has the details.
"""

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import subprocess  # noqa: S404
import tempfile
from typing import Any

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIAGRAMS = ROOT / "docs" / "diagrams"
BUILD = ROOT / ".archify" / "build"
LANGS = ("en", "ru")
ARCHIFY_CANDIDATES = (
    pathlib.Path.home() / ".claude" / "skills" / "archify",
    pathlib.Path.home() / ".agents" / "skills" / "archify",
)
CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def find_archify(explicit: str | None) -> pathlib.Path:
    options = [pathlib.Path(explicit)] if explicit else []
    if os.environ.get("ARCHIFY_HOME"):
        options.append(pathlib.Path(os.environ["ARCHIFY_HOME"]))
    options.extend(ARCHIFY_CANDIDATES)
    for option in options:
        if (option / "bin" / "archify.mjs").is_file():
            return option
    raise SystemExit(
        "Archify not found. Install it with `npx skills add tt-a1i/archify -g`, "
        "or pass --archify / set ARCHIFY_HOME to the skill folder."
    )


def find_chrome(explicit: str | None) -> str:
    for option in [explicit, os.environ.get("ARCHIFY_CHROME"), *CHROME_CANDIDATES]:
        if option and shutil.which(option):
            return option
    raise SystemExit(
        "Chrome or Chromium not found; pass --chrome or set ARCHIFY_CHROME."
    )


def finalize(archify: pathlib.Path, source: pathlib.Path, lang: str) -> pathlib.Path:
    """Run Archify's finalize on a scratch copy and return the checked HTML."""
    candidate = json.loads(source.read_text(encoding="utf-8"))
    work = BUILD / lang / source.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    html = work / f"{source.stem}.html"
    # meta.output must name the file finalize writes; the committed source
    # names its published path instead, so the scratch copy is retargeted.
    candidate["meta"]["output"] = html.relative_to(ROOT).as_posix()
    scratch = work / "candidate.json"
    scratch.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    command = [
        "node",
        str(archify / "bin" / "archify.mjs"),
        "finalize",
        candidate["diagram_type"],
        str(scratch.relative_to(ROOT)),
        str(html.relative_to(ROOT)),
        "--repo-root",
        ".",
        "--quality",
        "showcase",
        "--json",
    ]
    # Built here from fixed arguments and repository paths, never user input.
    result = subprocess.run(  # noqa: S603
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(
            f"{lang}/{source.stem}: finalize printed no receipt\n{result.stderr}"
        ) from None
    if receipt.get("status") != "pass":
        diagnostics = json.dumps(
            receipt.get("diagnostics"), ensure_ascii=False, indent=1
        )
        raise SystemExit(f"{lang}/{source.stem}: finalize failed\n{diagnostics}")
    return html


def _svgs(folder: pathlib.Path) -> list[pathlib.Path]:
    return [f for f in folder.iterdir() if f.suffix == ".svg"]


async def export_svg(chrome: str, html: pathlib.Path, target: pathlib.Path) -> None:
    """Click the viewer's "SVG · Auto" export in headless Chrome and keep the file."""
    # Chrome's helper processes can still be flushing the profile when the
    # browser exits, so its cleanup must not fail the build.
    with (
        tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile,
        tempfile.TemporaryDirectory() as downloads,
    ):
        process = await asyncio.create_subprocess_exec(
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1600,1100",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            port_file = pathlib.Path(profile) / "DevToolsActivePort"
            for _ in range(200):
                if port_file.exists() and port_file.read_text().strip():
                    break
                await asyncio.sleep(0.05)
            port = port_file.read_text().splitlines()[0]
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/json") as response:
                    targets: list[dict[str, Any]] = await response.json()
                page = next(t for t in targets if t["type"] == "page")
                async with session.ws_connect(
                    page["webSocketDebuggerUrl"], max_msg_size=0
                ) as ws:
                    await _drive_export(ws, html, pathlib.Path(downloads))
            files = await asyncio.to_thread(_svgs, pathlib.Path(downloads))
            if len(files) != 1:
                raise SystemExit(f"{html.name}: expected one exported SVG, got {files}")
            shutil.move(files[0], target)
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()


async def _drive_export(
    ws: aiohttp.ClientWebSocketResponse, html: pathlib.Path, downloads: pathlib.Path
) -> None:
    counter = 0

    async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        await ws.send_json({"id": counter, "method": method, "params": params or {}})
        while True:
            message: dict[str, Any] = await ws.receive_json()
            if message.get("id") == counter:
                return message

    await send(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(downloads)},
    )
    await send("Page.navigate", {"url": html.as_uri()})
    # The export button exists once the viewer has booted.
    for _ in range(100):
        ready = await send(
            "Runtime.evaluate",
            {
                "expression": "!!document.querySelector('[data-format=\"svg\"]')",
                "returnByValue": True,
            },
        )
        if ready["result"]["result"].get("value"):
            break
        await asyncio.sleep(0.1)
    else:
        raise SystemExit(f"{html.name}: the viewer's SVG export never appeared")
    await send(
        "Runtime.evaluate",
        {"expression": "document.querySelector('[data-format=\"svg\"]').click()"},
    )
    for _ in range(100):
        if await asyncio.to_thread(_svgs, downloads):
            await ws.send_json({"id": counter + 1, "method": "Browser.close"})
            return
        await asyncio.sleep(0.1)
    raise SystemExit(f"{html.name}: the SVG export produced no file")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "Rebuild the Archify diagrams.").splitlines()[0]
    )
    parser.add_argument("names", nargs="*", help="diagram names (default: all)")
    parser.add_argument("--lang", choices=LANGS, action="append", help="default: all")
    parser.add_argument("--archify", help="path to the Archify skill folder")
    parser.add_argument("--chrome", help="Chrome or Chromium executable")
    parser.add_argument("--skip-svg", action="store_true", help="do not re-export SVGs")
    args = parser.parse_args()

    archify = find_archify(args.archify)
    chrome = None if args.skip_svg else find_chrome(args.chrome)
    for lang in args.lang or LANGS:
        sources = sorted((DIAGRAMS / "src" / lang).glob("*.json"))
        if args.names:
            sources = [s for s in sources if s.stem in args.names]
        for source in sources:
            html = finalize(archify, source, lang)
            published = DIAGRAMS / lang / f"{source.stem}.html"
            published.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(html, published)
            print(f"{lang}/{source.stem}: checked, {published.relative_to(ROOT)}")
            if chrome:
                svg = published.with_suffix(".svg")
                asyncio.run(export_svg(chrome, published, svg))
                print(f"{lang}/{source.stem}: exported {svg.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
