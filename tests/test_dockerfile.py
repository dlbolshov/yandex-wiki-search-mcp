"""The Dockerfile's uv image must stay inside pyproject.toml's `required-version`.

uv refuses to run outside that range, so an image past it fails the builder's
`uv sync --locked`. Only release.yml builds the image, on a tag, so without
this check a pin and a range that drift apart surface at release time.
"""

import re
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
UV_IMAGE = re.compile(r"^FROM\s+\S*astral-sh/uv:(?P<tag>[^\s@]+)", re.MULTILINE)


def test_dockerfile_uv_image_is_inside_required_version() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = SpecifierSet(pyproject["tool"]["uv"]["required-version"])

    tags = UV_IMAGE.findall((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert len(tags) == 1, tags
    tag = tags[0]

    # An exact release: a floating tag changes the image's uv without a
    # commit, which is how `latest` left the range when uv 0.13.0 shipped.
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag), tag
    assert Version(tag) in required, f"uv {tag} is outside {required}"
