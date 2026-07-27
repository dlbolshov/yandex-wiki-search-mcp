"""Check that the package version matches across all release metadata files.

Sources: pyproject.toml, uv.lock, manifest.json and server.json (top-level
version, pypi package version, OCI image tag). CHANGELOG.md must contain a
'## [X.Y.Z]' section for the package version. Optionally validates a git tag
ref (refs/tags/vX.Y.Z) against the package version; non-tag refs are ignored.

Runs in CI on every pull request (test.yml) and again on release tags
(release.yml). Locally:

    uv run python scripts/check_versions.py
"""

import argparse
import json
import pathlib
import sys
import tomllib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_NAME = "yandex-wiki-search-mcp"


def _package_field(server: dict[str, Any], registry_type: str, field: str) -> str:
    for package in server.get("packages", []):
        if package.get("registryType") == registry_type:
            return str(package[field])
    raise SystemExit(f"server.json: no package with registryType={registry_type!r}")


def collect_versions() -> dict[str, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    locked = [
        str(package["version"])
        for package in uv_lock.get("package", [])
        if package.get("name") == PACKAGE_NAME
    ]
    if not locked:
        raise SystemExit(f"uv.lock: package {PACKAGE_NAME!r} not found")

    oci_identifier = _package_field(server, "oci", "identifier")
    _, _, oci_tag = oci_identifier.rpartition(":")

    return {
        "pyproject.toml": str(pyproject["project"]["version"]),
        "uv.lock": locked[0],
        "manifest.json": str(manifest["version"]),
        "server.json": str(server["version"]),
        "server.json[pypi package]": _package_field(server, "pypi", "version"),
        "server.json[oci tag]": oci_tag,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        default=None,
        help="git ref to validate, e.g. refs/tags/v0.7.0 (non-tag refs are ignored)",
    )
    args = parser.parse_args()

    versions = collect_versions()
    if len(set(versions.values())) != 1:
        for source, version in versions.items():
            print(f"{source}: {version}")
        raise SystemExit("Version mismatch across package metadata files.")

    project_version = versions["pyproject.toml"]

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{project_version}]" not in changelog:
        raise SystemExit(
            f"CHANGELOG.md: no '## [{project_version}]' section for the package version"
        )

    if args.tag and args.tag.startswith("refs/tags/v"):
        tag_version = args.tag.removeprefix("refs/tags/v")
        if tag_version != project_version:
            raise SystemExit(
                f"Git tag version {tag_version} does not match "
                f"package version {project_version}"
            )

    print(f"Validated package version: {project_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
