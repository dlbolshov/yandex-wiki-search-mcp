"""Every module must import cleanly on its own.

The suite cannot catch this in-process: by the time any test runs, conftest
has already imported the package in an order that happens to work. A cycle
only shows up when a module is the *first* thing imported, which is exactly
what a library consumer does — `mcp_wiki.mcp.utils` used to raise ImportError
there, because the HTTP client reached back into it for normalize_slug.
"""

import subprocess  # noqa: S404
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "mcp_wiki"


def module_names() -> list[str]:
    names = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
        parts = relative.parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts[-1] == "__main__":
            continue  # importing it would run the server
        names.append(".".join(parts))
    return names


@pytest.mark.parametrize("module", module_names())
def test_module_imports_first(module: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"`import {module}` fails as a first import:\n{result.stderr}"
    )
