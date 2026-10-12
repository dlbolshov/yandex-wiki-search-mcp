"""One exact uv for the Dockerfile and CI, and no range for everyone else.

uv refuses nearly every command in a copy of this tree that falls outside
pyproject.toml's `required-version`, so a ceiling there breaks the Claude
Desktop bundle, glama.ai's source build and contributors on the next uv
minor. The project sets none; the uv it is built and tested with is pinned
instead, once in the Dockerfile's uv image and once in every setup-uv
step. Only release.yml builds the image, on a tag, so without this check a
pin that drifts surfaces at release time.

The one exemption is the weekly canary, uv-canary.yml, whose steps install
the newest uv on purpose: Dependabot cannot see either pin, so the canary is
what notices the next uv release, by running the bundle and the suite on it.
"""

import re
import tomllib
from pathlib import Path
from typing import NamedTuple

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
# The uv image, whether a stage is built FROM it or a file is copied from it.
UV_IMAGE = re.compile(
    r"^(?:FROM\s+(?:--\S+\s+)*|COPY\s+(?:--\S+\s+)*--from=)\S*astral-sh/uv:(?P<tag>[^\s@]+)",
    re.MULTILINE | re.IGNORECASE,
)
SETUP_UV = re.compile(r"""^(?P<lead>\s*(?:-\s+)?)uses:\s*["']?astral-sh/setup-uv@""")
KEY = re.compile(r"^\s*(?:-\s+)?(?P<key>[\w-]+):")
VERSION = re.compile(
    r"""^\s+version:\s*(?:"(?P<dq>[^"]+)"|'(?P<sq>[^']+)'|(?P<bare>[^\s"'#]+))\s*(?:#(?P<comment>.*))?$"""
)
# A step is the canary when this word is in the comment on its `version:` line.
# Only the canary workflow may carry it, and there the version must float.
CANARY_MARKER = re.compile(r"\buv-canary\b")
CANARY_WORKFLOW = "uv-canary.yml"


class SetupUv(NamedTuple):
    version: str | None
    canary: bool


def _dockerfile_uv() -> str:
    tags = UV_IMAGE.findall((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert len(tags) == 1, tags
    return tags[0]


def _setup_uv_steps() -> dict[str, SetupUv]:
    """Map every setup-uv step (file:line) to its `with: version`, if any."""
    steps: dict[str, SetupUv] = {}
    workflows = (ROOT / ".github" / "workflows").glob("*.y*ml")
    for workflow in sorted(workflows):
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            match = SETUP_UV.match(line)
            if not match:
                continue
            # The step's keys sit at the column of `uses:`; the step runs
            # until a line indented less than that.
            column = len(match.group("lead"))
            version = None
            canary = False
            in_with = False
            for following in lines[number + 1 :]:
                stripped = following.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(following) - len(stripped)
                if indent < column:
                    break
                if indent == column:
                    key = KEY.match(following)
                    in_with = bool(key and key.group("key") == "with")
                elif in_with and (found := VERSION.match(following)):
                    version = (
                        found.group("dq") or found.group("sq") or found.group("bare")
                    )
                    canary = bool(CANARY_MARKER.search(found.group("comment") or ""))
            steps[f"{workflow.name}:{number + 1}"] = SetupUv(version, canary)
    return steps


def test_required_version_has_no_ceiling() -> None:
    # uv.toml would take over required-version from pyproject.toml unseen.
    assert not (ROOT / "uv.toml").exists()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = SpecifierSet(pyproject["tool"]["uv"].get("required-version", ""))
    # None today. A floor would be tolerable; anything else is a ceiling.
    assert {spec.operator for spec in required} <= {">="}, str(required)
    assert Version(_dockerfile_uv()) in required


def test_dockerfile_uv_image_is_an_exact_release() -> None:
    # A floating tag changes the image's uv without a commit, which is how
    # `latest` broke the image build when uv 0.13.0 shipped.
    tag = _dockerfile_uv()
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag), tag


def test_every_setup_uv_step_pins_the_dockerfile_uv() -> None:
    steps = {
        step: found.version
        for step, found in _setup_uv_steps().items()
        if not found.canary
    }
    assert steps
    tag = _dockerfile_uv()
    assert steps == dict.fromkeys(steps, tag), f"Dockerfile has uv {tag}"


def test_only_the_canary_floats() -> None:
    canary = {
        step: found.version for step, found in _setup_uv_steps().items() if found.canary
    }
    # Without it nothing installs a newer uv: Dependabot sees neither pin.
    assert canary
    assert {step.partition(":")[0] for step in canary} == {CANARY_WORKFLOW}, canary
    assert canary == dict.fromkeys(canary, "latest")
