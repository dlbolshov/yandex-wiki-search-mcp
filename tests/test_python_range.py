"""One Python range for every uv that runs from this tree.

uv.lock has wheels for CPython 3.11–3.14 and none for 3.15, which uv 0.13
made its default download wherever no suitable interpreter is installed.
`.python-versions` keeps a checkout — a contributor's `uv sync`, a source
build such as glama.ai's — inside the range; uv takes it as the project's
default request, ahead of `requires-python`. The Claude Desktop bundle
carries the same range as `UV_PYTHON` in manifest.json, which beats the
file and so leaves the bundle exactly as it was.

The plural name on purpose: pyenv and asdf read `.python-version` and
cannot parse a range there, so a singular file would break their shims
inside the repo, while uv reads both. And no upper bound on
`requires-python`: that ships in the package metadata, where pip and uv on
3.15 would fall back to old releases instead of installing this one.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
MINORS = range(8, 30)
# A wheel built for one CPython minor (`-cp314-`), or for the stable ABI from
# one minor on (`-cp311-abi3-`).
CPYTHON_TAG = re.compile(r"-cp3(?P<minor>\d+)-(?P<abi>abi3|cp3\d+t?)-")


def _range() -> SpecifierSet:
    text = (ROOT / ".python-versions").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1, lines
    return SpecifierSet(lines[0])


def _admitted(specifier: SpecifierSet) -> list[int]:
    return [minor for minor in MINORS if Version(f"3.{minor}.0") in specifier]


def _requires_python() -> SpecifierSet:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return SpecifierSet(pyproject["project"]["requires-python"])


def _has_wheel_for(wheels: list[str], minor: int) -> bool:
    for wheel in wheels:
        tag = CPYTHON_TAG.search(wheel)
        if tag is None:
            continue
        built_for = int(tag["minor"])
        if built_for == minor or (tag["abi"] == "abi3" and built_for <= minor):
            return True
    return False


def test_the_range_starts_where_the_project_does() -> None:
    # Narrower would turn away interpreters the package supports; wider would
    # offer ones it does not.
    assert _admitted(_range())[0] == _admitted(_requires_python())[0]


def test_the_range_stops_before_3_15() -> None:
    assert Version("3.14.0") in _range()
    assert Version("3.15.0") not in _range()


@pytest.mark.parametrize("minor", _admitted(_range()))
def test_the_lock_has_wheels_for_every_minor_in_the_range(minor: int) -> None:
    # What a raised bound has to be backed by: a wheel of every compiled
    # package in the lock for that minor, or uv builds it from source — or,
    # where the bundle sets UV_NO_BUILD, fails. Pure and platform-only wheels
    # (`-none-`) fit any minor.
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    missing = []
    for package in lock["package"]:
        wheels = [
            wheel["url"].rsplit("/", 1)[-1] for wheel in package.get("wheels", [])
        ]
        if not wheels or any("-none-" in wheel for wheel in wheels):
            continue
        if not _has_wheel_for(wheels, minor):
            missing.append(f"{package['name']}=={package['version']}")
    assert missing == [], f"no cp3{minor} wheel for {missing}"


def test_the_bundle_carries_the_same_range() -> None:
    # UV_PYTHON beats the file, and the bundle sets it on every platform.
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["server"]["mcp_config"]
    envs = [config["env"]] + [
        override["env"]
        for override in config.get("platform_overrides", {}).values()
        if "env" in override
    ]
    assert [SpecifierSet(env["UV_PYTHON"]) for env in envs] == [_range()] * len(envs)


def test_there_is_no_singular_pin() -> None:
    # pyenv and asdf would read it, and choke on a range.
    assert not (ROOT / ".python-version").exists()
