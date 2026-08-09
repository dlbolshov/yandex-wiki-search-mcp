import json
from unittest.mock import AsyncMock, patch

from mcp import Client
from mcp.types import TextResourceContents

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import create_mcp_server
from mcp_wiki.wiki.proto.common import YandexAuth
from tests.mcp.conftest import (
    create_test_settings,
    make_test_lifespan,
    safe_client,
)


async def read_configuration(auth: YandexAuth) -> dict[str, str | bool | None]:
    """Read the resource as if the request carried `auth`."""
    server = create_mcp_server(
        settings=create_test_settings(),
        lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
    )
    with patch("mcp_wiki.mcp.resources.get_yandex_auth", return_value=auth):
        async with safe_client(server) as session:
            result = await session.read_resource("wiki-mcp://configuration")

    content = result.contents[0]
    assert isinstance(content, TextResourceContents)
    return json.loads(content.text)


class TestReportedOrganization:
    """The resource must report the organization calls actually go to.

    The pair moves as a unit. Derived independently, the answer would pair
    the server-wide org_id with the request's cloud_org_id — a combination
    the settings validator forbids and no request ever carries.
    """

    async def test_defaults_are_reported_without_per_request_auth(self) -> None:
        config = await read_configuration(YandexAuth(token="t"))

        assert config["org_id"] == "test-org"
        assert config["cloud_org_id"] is None

    async def test_request_cloud_org_replaces_the_server_org(self) -> None:
        config = await read_configuration(
            YandexAuth(token="t", cloud_org_id="req-cloud")
        )

        assert config["cloud_org_id"] == "req-cloud"
        assert config["org_id"] is None

    async def test_request_org_replaces_the_server_org(self) -> None:
        config = await read_configuration(YandexAuth(token="t", org_id="req-org"))

        assert config["org_id"] == "req-org"
        assert config["cloud_org_id"] is None


class TestConfigurationResource:
    async def test_read_returns_configuration(
        self,
        client: Client,
    ) -> None:
        result = await client.read_resource("wiki-mcp://configuration")

        assert len(result.contents) > 0
        content = result.contents[0]
        assert isinstance(content, TextResourceContents)
        assert content.text is not None

    async def test_contains_expected_fields(
        self,
        client: Client,
    ) -> None:
        result = await client.read_resource("wiki-mcp://configuration")

        content = result.contents[0]
        assert isinstance(content, TextResourceContents)
        assert "api_base_url" in content.text
        assert "read_only" in content.text
