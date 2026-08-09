import base64
import importlib.metadata
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client.session import ClientSession
from mcp.server import FastMCP
from pydantic import AnyHttpUrl, SecretStr
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import (
    _parse_encryption_keys,
    create_mcp_server,
    server_version,
)
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
    "grid_move_row",
    "grid_move_column",
    "page_create",
    "page_update",
    "page_clone",
    "page_append_content",
    "page_add_comment",
    "page_delete",
    "page_recover",
    "page_upload_attachment",
]

EXPECTED_TOOL_NAMES = READ_ONLY_TOOL_NAMES + NON_READ_TOOL_NAMES

MANIFEST_PATH = Path(__file__).resolve().parents[3] / "manifest.json"


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


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

    async def test_read_only_instructions_do_not_advertise_writes(self) -> None:
        server = create_mcp_server(
            settings=create_test_settings(read_only=True),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert server.instructions is not None
        assert "read-only mode" in server.instructions
        assert "Create, update" not in server.instructions
        assert "Add comments" not in server.instructions
        assert "Grid mutations" not in server.instructions
        assert "yfm_warnings" not in server.instructions
        # read guidance stays
        assert "page_search" in server.instructions
        assert "fetch_all" in server.instructions


class TestToolAnnotations:
    async def test_every_tool_declares_a_closed_world(
        self,
        client_session: ClientSession,
    ) -> None:
        # openWorldHint left unset defaults to true; every tool here talks
        # to one configured Wiki organization, so all must say otherwise.
        result = await client_session.list_tools()
        for tool in result.tools:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.openWorldHint is False, tool.name


class TestOAuthUploadGating:
    async def test_local_upload_tool_is_hidden_under_oauth(self) -> None:
        settings = create_test_settings()
        settings.oauth_enabled = True
        settings.oauth_client_id = "client-id"
        settings.oauth_client_secret = SecretStr("client-secret")
        settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.com")

        server = create_mcp_server(
            settings=settings,
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        tool_names = [tool.name for tool in await server.list_tools()]
        assert "page_upload_attachment" not in tool_names
        # only the local-filesystem tool is gated; other writes stay
        assert "page_clone" in tool_names
        assert "page_create" in tool_names
        # the instructions must not advertise the tool that is not there
        assert server.instructions is not None
        assert "upload attachments" not in server.instructions
        assert "- Add comments" in server.instructions

    async def test_local_upload_tool_is_offered_without_oauth(self) -> None:
        server = create_mcp_server(
            settings=create_test_settings(),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        tool_names = [tool.name for tool in await server.list_tools()]
        assert "page_upload_attachment" in tool_names
        assert server.instructions is not None
        assert "upload attachments from the local filesystem" in server.instructions


class TestManifestSync:
    async def test_manifest_tools_match_the_registered_surface(
        self,
        mcp_server: FastMCP[Any],
    ) -> None:
        # manifest.json is the MCPB bundle metadata: its tools list is what
        # clients show before installing. Nothing generates it from the code,
        # so renames and additions silently drift apart without this check —
        # the v1.0.0 branch still listed grid_move_row under its old plural
        # name and had no page_clone at all until this test caught it.
        manifest = load_manifest()

        manifest_names = [tool["name"] for tool in manifest["tools"]]
        registered_names = [tool.name for tool in await mcp_server.list_tools()]

        assert sorted(manifest_names) == sorted(registered_names)

        for tool in manifest["tools"]:
            assert tool.get("description", "").strip(), (
                f"manifest tool {tool['name']!r} has no description"
            )


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


class TestServerVersion:
    def test_falls_back_to_dev_when_package_not_installed(self) -> None:
        with patch(
            "importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            assert server_version() == "dev"


class TestParseEncryptionKeys:
    @pytest.mark.parametrize("keys_str", [None, "", " , ,"])
    def test_empty_input_returns_none(self, keys_str: str | None) -> None:
        assert _parse_encryption_keys(keys_str) is None

    def test_parses_keys_and_skips_blank_segments(self) -> None:
        key = base64.b64encode(b"k" * 32).decode()
        assert _parse_encryption_keys(f" {key} ,, {key}") == [b"k" * 32, b"k" * 32]

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid base64"):
            _parse_encryption_keys("abc")

    def test_wrong_length_raises(self) -> None:
        short = base64.b64encode(b"short").decode()
        with pytest.raises(ValueError, match="must be 32 bytes"):
            _parse_encryption_keys(short)


class TestClientRegistrationExpiry:
    @staticmethod
    def _oauth_settings(expiry: int | None) -> Any:
        settings = create_test_settings()
        settings.oauth_enabled = True
        settings.oauth_client_id = "client-id"
        settings.oauth_client_secret = SecretStr("client-secret")
        settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.com")
        settings.oauth_client_secret_expiry_seconds = expiry
        return settings

    def test_registrations_are_given_a_lifetime(self) -> None:
        # Without one, /register — unauthenticated by protocol design —
        # grows the store without bound, in Redis as well as in memory.
        server = create_mcp_server(
            settings=self._oauth_settings(30 * 24 * 60 * 60),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert server.settings.auth is not None
        options = server.settings.auth.client_registration_options
        assert options is not None
        assert options.client_secret_expiry_seconds == 30 * 24 * 60 * 60

    def test_expiry_can_be_disabled(self) -> None:
        server = create_mcp_server(
            settings=self._oauth_settings(None),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert server.settings.auth is not None
        options = server.settings.auth.client_registration_options
        assert options is not None
        assert options.client_secret_expiry_seconds is None


class TestOAuthCallbackRoute:
    def test_oauth_enabled_registers_callback_and_healthz(self) -> None:
        settings = create_test_settings()
        settings.oauth_enabled = True
        settings.oauth_client_id = "client-id"
        settings.oauth_client_secret = SecretStr("client-secret")
        settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.com")

        server = create_mcp_server(
            settings=settings,
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        app = server.streamable_http_app()
        with TestClient(app) as client:
            healthz = client.get("/healthz")
            callback = client.get("/oauth/yandex/callback?code=x&state=missing")

        assert healthz.status_code == 200
        assert callback.status_code == 400


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
