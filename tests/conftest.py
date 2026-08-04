import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.proto.common import YandexAuth

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
