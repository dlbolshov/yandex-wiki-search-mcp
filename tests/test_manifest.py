"""How the Claude Desktop bundle launches: manifest.json's `mcp_config`."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from mcp_wiki.settings import Settings, suspicious_env_keys

ROOT = Path(__file__).resolve().parents[1]
_SETTINGS_PREFIXES = ("WIKI_", "OAUTH_", "REDIS_", "MCP_", "TOOL_")


def _manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    return manifest


def _mcp_config() -> dict[str, Any]:
    config: dict[str, Any] = _manifest()["server"]["mcp_config"]
    return config


def _envs() -> dict[str, dict[str, str]]:
    """The env each platform gets: the base one, or an override's instead."""
    config = _mcp_config()
    overrides = config.get("platform_overrides", {})
    return {"base": config["env"]} | {
        platform: override.get("env", config["env"])
        for platform, override in overrides.items()
    }


def _args() -> dict[str, list[str]]:
    """The args each platform gets: the base ones, or an override's instead."""
    config = _mcp_config()
    overrides = config.get("platform_overrides", {})
    return {"base": config["args"]} | {
        platform: override.get("args", config["args"])
        for platform, override in overrides.items()
    }


def _resolved(env: dict[str, str], **user: str) -> dict[str, str]:
    """Substitute placeholders the way mcpb does: defaults, then the user's."""
    values: dict[str, str] = {"__dirname": "/bundle"}
    for key, option in _manifest()["user_config"].items():
        value = user.get(key, option.get("default", ""))
        values[f"user_config.{key}"] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
    return {
        name: re.sub(r"\$\{([^}]+)\}", lambda m: values[m.group(1)], value)
        for name, value in env.items()
    }


def test_platform_overrides_repeat_the_base_env() -> None:
    # An override's env replaces the base env instead of merging into it
    # (mcpb's getMcpConfigForManifest), so a variable it leaves out is gone
    # on that platform.
    envs = _envs()
    base = envs.pop("base")
    for platform, env in envs.items():
        assert {name: env.get(name) for name in base} == base, platform


def test_windows_keeps_x86_64_python() -> None:
    # uv 0.13 prefers native Python on Windows ARM64, where the locked aiohttp
    # and cryptography have no wheels; uv 0.12 used x86_64 there.
    env = _mcp_config()["platform_overrides"]["win32"]["env"]
    assert env["UV_PYTHON_ARCH"] == "x86_64"


@pytest.mark.parametrize("platform", ["base", "win32"])
def test_every_env_keeps_python_below_3_15(platform: str) -> None:
    # uv 0.13 downloads 3.15 where no interpreter fits, and several locked
    # packages have no 3.15 wheels. Raise the bound once the lock has them.
    allowed = SpecifierSet(_envs()[platform]["UV_PYTHON"])
    assert Version("3.11.0") in allowed
    assert Version("3.15.0") not in allowed


@pytest.mark.parametrize("platform", ["base", "win32"])
def test_every_launch_skips_the_dev_group(platform: str) -> None:
    # uv installs the `dev` group by default, so a first launch would also
    # download mypy, ruff, ty and the test stack. An option after the script
    # name goes to the server, not to uv.
    args = _args()[platform]
    assert args[0] == "run"
    assert "--no-dev" in args[: args.index("yandex-wiki-search-mcp")]


@pytest.mark.parametrize("platform", ["base", "win32"])
def test_the_bundle_env_starts_the_server(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude Desktop passes every variable the manifest names, the ones the
    # user left empty as "" — a shape no other test builds Settings from.
    monkeypatch.chdir(tmp_path)  # no stray .env
    for key in list(os.environ):
        if key.upper().startswith(_SETTINGS_PREFIXES):
            monkeypatch.delenv(key)
    env = _resolved(_envs()[platform], wiki_token="token", wiki_org_id="123")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    settings = Settings()

    assert settings.wiki_org_id == "123"
    assert not settings.wiki_cloud_org_id
    assert settings.wiki_read_only is False
    assert suspicious_env_keys() == {}
