from typing import AsyncGenerator

import pytest

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.proto.common import YandexAuth


@pytest.fixture
async def wiki_client() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token="test-token",
        org_id="test-org",
        base_url="https://api.wiki.yandex.net",
    ) as client:
        yield client


@pytest.fixture
async def wiki_client_no_org() -> AsyncGenerator[WikiClient, None]:
    async with WikiClient(
        token="test-token",
        base_url="https://api.wiki.yandex.net",
    ) as client:
        yield client


@pytest.fixture
def yandex_auth() -> YandexAuth:
    return YandexAuth(token="auth-token", org_id="auth-org")


@pytest.fixture
def yandex_auth_cloud() -> YandexAuth:
    return YandexAuth(token="auth-token", cloud_org_id="cloud-org")
