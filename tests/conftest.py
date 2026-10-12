import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from mcp_wiki.settings import SETTINGS_PREFIXES, Settings
from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.proto.common import YandexAuth

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Every name Settings reads from the environment: the namespaces it answers
# to and its unprefixed fields (HOST, LOG_LEVEL, STATELESS_HTTP...). Taken
# from Settings itself, so a field added later is covered without anyone
# remembering this list. Upper-cased because pydantic-settings matches
# environment names case-insensitively.
_SETTINGS_ENV_PREFIXES = tuple(prefix.upper() for prefix in SETTINGS_PREFIXES)
_SETTINGS_ENV_NAMES = frozenset(name.upper() for name in Settings.model_fields)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every variable Settings would read, so the shell cannot leak in.

    A developer's `LOG_LEVEL=info` (lowercase) or `PORT=abc` otherwise fails
    any test that builds Settings from the environment.
    """
    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith(_SETTINGS_ENV_PREFIXES) or upper in _SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key, raising=False)


def load_fixture(name: str) -> Any:
    """Load a recorded API payload from tests/fixtures.

    Tests that use these are what keeps them honest: a fixture nobody loads
    quietly drifts away from the contract it claims to document.
    """
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
async def wiki_client() -> AsyncGenerator[WikiClient, None]:
    # Retries off by default so that error-path tests stay deterministic and fast;
    # use wiki_client_retrying to exercise them.
    async with WikiClient(
        token="test-token",
        org_id="test-org",
        base_url="https://api.wiki.yandex.net",
        max_retries=0,
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_retrying() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token="test-token",
        org_id="test-org",
        base_url="https://api.wiki.yandex.net",
        max_retries=2,
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_iam() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token=None,
        iam_token="test-iam-token",
        org_id="test-org",
        base_url="https://api.wiki.yandex.net",
        max_retries=0,
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_cloud_org() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token="test-token",
        cloud_org_id="test-cloud-org",
        base_url="https://api.wiki.yandex.net",
        max_retries=0,
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_no_auth() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token=None,
        org_id="test-org",
        base_url="https://api.wiki.yandex.net",
        max_retries=0,
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_both_orgs() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token="test-token",
        org_id="test-org",
        cloud_org_id="test-cloud-org",
        base_url="https://api.wiki.yandex.net",
        max_retries=0,
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_no_org() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token="test-token",
        base_url="https://api.wiki.yandex.net",
        max_retries=0,
    ) as client:
        yield client


@pytest.fixture
def yandex_auth() -> YandexAuth:
    return YandexAuth(token="auth-token", org_id="auth-org")


@pytest.fixture
def yandex_auth_cloud() -> YandexAuth:
    return YandexAuth(token="auth-token", cloud_org_id="cloud-org")
