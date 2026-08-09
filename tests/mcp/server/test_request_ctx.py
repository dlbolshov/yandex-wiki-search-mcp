"""End-to-end cover for the middleware that publishes the inbound request.

`Server.middleware` is provisional in the SDK, and the configuration resource
depends on it: a static-URI resource gets no Context, so the per-request
organization can only arrive through the stashed request. These tests go over
real HTTP rather than calling the handler, so a change to the middleware
contract turns into a red run here instead of a resource that quietly reports
the wrong organization.
"""

import json
from typing import Any
from unittest.mock import AsyncMock

from mcp.server import MCPServer
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import create_mcp_server, http_app_options
from mcp_wiki.settings import Settings
from tests.mcp.conftest import (
    MCP_HTTP_HEADERS,
    create_test_settings,
    initialize_request,
    make_test_lifespan,
)


def build_app(settings: Settings) -> Any:
    server: MCPServer[Any] = create_mcp_server(
        settings=settings,
        lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
    )
    return server.streamable_http_app(**http_app_options(settings))


def read_configuration(query: str) -> dict[str, Any]:
    """Read wiki-mcp://configuration over HTTP with `query` on the endpoint."""
    settings = create_test_settings()
    with TestClient(build_app(settings)) as client:
        client.post(
            f"/mcp{query}",
            json=initialize_request(),
            headers=MCP_HTTP_HEADERS,
        )
        response = client.post(
            f"/mcp{query}",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "wiki-mcp://configuration"},
            },
            headers=MCP_HTTP_HEADERS,
        )

    body = response.json()
    assert "error" not in body, body
    return json.loads(body["result"]["contents"][0]["text"])


class TestConfigurationSeesTheRequest:
    def test_org_id_from_the_endpoint_query_is_reported(self) -> None:
        config = read_configuration("?orgId=req-org")

        assert config["org_id"] == "req-org"
        assert config["cloud_org_id"] is None

    def test_cloud_org_id_from_the_endpoint_query_is_reported(self) -> None:
        # The pair moves as a unit: a request cloud org replaces the
        # configured org rather than joining it.
        config = read_configuration("?cloudOrgId=req-cloud")

        assert config["cloud_org_id"] == "req-cloud"
        assert config["org_id"] is None

    def test_without_a_query_the_configured_org_is_reported(self) -> None:
        config = read_configuration("")

        assert config["org_id"] == "test-org"
        assert config["cloud_org_id"] is None
