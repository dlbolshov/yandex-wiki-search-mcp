import importlib.metadata
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.client.session import ClientSession
from mcp.server import FastMCP
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import create_mcp_server
from tests.mcp.conftest import create_test_settings, make_test_lifespan

READ_ONLY_TOOL_NAMES = [
    "page_search",
    "page_get",
    "page_get_descendants",
    "page_get_comments",
    "page_get_resources",
    "page_get_grids",
    "grid_get",
    "page_get_attachments",
]

NON_READ_TOOL_NAMES = [
    "grid_create",
    "grid_update",
    "grid_add_rows",
    "grid_delete",
    "grid_copy",
    "grid_update_cells",
    "grid_delete_rows",
    "grid_add_columns",
    "grid_delete_columns",
    "grid_move_rows",
    "grid_move_columns",
    "page_create",
    "page_update",
    "page_append_content",
    "page_add_comment",
    "page_delete",
    "page_recover",
    "page_upload_attachment",
]

EXPECTED_TOOL_NAMES = READ_ONLY_TOOL_NAMES + NON_READ_TOOL_NAMES


class TestToolRegistration:
    @pytest.mark.parametrize("tool_name", EXPECTED_TOOL_NAMES)
    async def test_tool_is_registered(
        self,
        client_session: ClientSession,
        tool_name: str,
    ) -> None:
        result = await client_session.list_tools()
        tool_names = [tool.name for tool in result.tools]
        assert tool_name in tool_names


class TestReadOnlyModeToolRegistration:
    @pytest.mark.parametrize("tool_name", READ_ONLY_TOOL_NAMES)
    async def test_read_only_tools_are_registered(
        self,
        client_session_read_only: ClientSession,
        tool_name: str,
    ) -> None:
        result = await client_session_read_only.list_tools()
        tool_names = [tool.name for tool in result.tools]
        assert tool_name in tool_names

    @pytest.mark.parametrize("tool_name", NON_READ_TOOL_NAMES)
    async def test_non_read_tools_are_not_registered(
        self,
        client_session_read_only: ClientSession,
        tool_name: str,
    ) -> None:
        result = await client_session_read_only.list_tools()
        tool_names = [tool.name for tool in result.tools]
        assert tool_name not in tool_names


class TestResourceRegistration:
    async def test_configuration_resource_is_registered(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.list_resources()
        resource_uris = [str(resource.uri) for resource in result.resources]
        assert "wiki-mcp://configuration" in resource_uris


class TestServerConfiguration:
    async def test_server_has_correct_name(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.initialize()
        assert result.serverInfo.name == "Yandex Wiki Search MCP"

    async def test_server_has_instructions(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.initialize()
        assert result.instructions

    async def test_initialize_reports_package_version(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.initialize()
        expected = importlib.metadata.version("yandex-wiki-search-mcp")
        assert result.serverInfo.version == expected


class TestHealthz:
    def test_healthz_returns_200(self, mcp_server: FastMCP[Any]) -> None:
        app = mcp_server.streamable_http_app()
        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.text == "ok"


class TestHttpTransportSettings:
    @pytest.mark.parametrize("flag", [True, False])
    def test_stateless_http_and_json_response_follow_settings(self, flag: bool) -> None:
        settings = create_test_settings()
        settings.stateless_http = flag
        settings.json_response = flag

        server = create_mcp_server(
            settings=settings,
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert server.settings.stateless_http is flag
        assert server.settings.json_response is flag
