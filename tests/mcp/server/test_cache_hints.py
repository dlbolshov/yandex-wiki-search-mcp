"""Cache hints (SEP-2549) for the listings that never change while we run.

Two things need pinning: that the hints reach a modern client at all, and
that they stay off `resources/read`, where they would make
`wiki-mcp://configuration` answer one tenant with another's organization.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import TextResourceContents

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import (
    LISTING_CACHE_TTL_MS,
    STATIC_LISTING_CACHE_HINTS,
    create_mcp_server,
)
from mcp_wiki.wiki.proto.common import YandexAuth
from tests.mcp.conftest import (
    create_test_settings,
    make_test_lifespan,
    safe_client,
)


def build_server() -> MCPServer[Any]:
    return create_mcp_server(
        settings=create_test_settings(),
        lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
    )


def org_of(contents: Any) -> str | None:
    assert isinstance(contents, TextResourceContents)
    org: str | None = json.loads(contents.text)["org_id"]
    return org


class TestHintsReachModernClients:
    async def test_listings_carry_the_ttl(self) -> None:
        async with safe_client(build_server()) as client:
            assert client.protocol_version == "2026-07-28"
            tools = await client.list_tools()
            resources = await client.list_resources()

        assert tools.ttl_ms == LISTING_CACHE_TTL_MS
        assert tools.cache_scope == "private"
        assert resources.ttl_ms == LISTING_CACHE_TTL_MS

    async def test_reads_are_not_hinted(self) -> None:
        # The exclusion that keeps the configuration resource honest.
        assert "resources/read" not in STATIC_LISTING_CACHE_HINTS

        async with safe_client(build_server()) as client:
            result = await client.read_resource("wiki-mcp://configuration")

        assert result.ttl_ms == 0


class TestLegacyTrafficIsUnchanged:
    @pytest.mark.parametrize("method", ["tools/list", "resources/list"])
    async def test_no_hint_is_sent_before_2026(self, method: str) -> None:
        # Hints are a 2026-07-28 feature; a 2025-era client must see exactly
        # the bytes it saw before this was configured.
        async with Client(build_server(), mode="legacy") as client:
            assert client.protocol_version == "2025-11-25"
            result = (
                await client.list_tools()
                if method == "tools/list"
                else await client.list_resources()
            )

        assert result.ttl_ms == 0


class TestConfigurationIsNeverServedFromCache:
    async def test_a_second_read_sees_the_new_organization(self) -> None:
        """The failure a `resources/read` hint would have bought.

        The client caches reads by URI, and the organization arrives on the
        endpoint query rather than in the URI — so a hint here would pin the
        first caller's organization for every later one.
        """
        server = build_server()
        auths = [
            YandexAuth(token="t", org_id="first-org"),
            YandexAuth(token="t", org_id="second-org"),
        ]

        with patch("mcp_wiki.mcp.resources.get_yandex_auth", side_effect=auths):
            async with safe_client(server) as client:
                first = await client.read_resource("wiki-mcp://configuration")
                second = await client.read_resource("wiki-mcp://configuration")

        assert org_of(first.contents[0]) == "first-org"
        assert org_of(second.contents[0]) == "second-org"
