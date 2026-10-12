"""How the Claude Desktop bundle launches: manifest.json's `mcp_config`."""

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _mcp_config() -> dict[str, Any]:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    config: dict[str, Any] = manifest["server"]["mcp_config"]
    return config


def test_platform_overrides_repeat_the_base_env() -> None:
    # An override's env replaces the base env instead of merging into it
    # (mcpb's getMcpConfigForManifest), so a variable it leaves out is gone
    # on that platform.
    config = _mcp_config()
    base = config["env"]
    for platform, override in config.get("platform_overrides", {}).items():
        env = override.get("env", base)
        assert {name: env.get(name) for name in base} == base, platform


def test_windows_keeps_x86_64_python() -> None:
    # uv 0.13 prefers native Python on Windows ARM64, where the locked aiohttp
    # and cryptography have no wheels; uv 0.12 used x86_64 there.
    env = _mcp_config()["platform_overrides"]["win32"]["env"]
    assert env["UV_PYTHON_ARCH"] == "x86_64"
