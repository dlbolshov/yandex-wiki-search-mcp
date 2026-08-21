import base64
import importlib.metadata
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp import Client
from mcp.server import MCPServer
from pydantic import AnyHttpUrl, SecretStr
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import (
    _parse_encryption_keys,
    create_mcp_server,
    http_app_options,
    run_options,
    server_description,
    server_version,
)
from tests.mcp.conftest import (
    MCP_HTTP_HEADERS,
    create_test_settings,
    initialize_request,
    make_test_lifespan,
)

READ_ONLY_TOOL_NAMES = [
    "page_search",
    "page_get",
    "page_get_descendants",
    "page_get_comments",
    "page_get_resources",
    "page_get_grids",
    "grid_get",
    "page_get_attachments",
    "page_read_attachment",
    "user_get_current",
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
    "page_edit",
    "page_clone",
    "page_append_content",
    "page_add_comment",
    "page_delete_comment",
    "page_delete_attachment",
    "page_delete",
    "page_recover",
    "page_upload_attachment",
    "page_download_attachment",
]

EXPECTED_TOOL_NAMES = READ_ONLY_TOOL_NAMES + NON_READ_TOOL_NAMES

MANIFEST_PATH = Path(__file__).resolve().parents[3] / "manifest.json"


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class TestToolRegistration:
    @pytest.mark.parametrize("tool_name", EXPECTED_TOOL_NAMES)
    async def test_tool_is_registered(
        self,
        client: Client,
        tool_name: str,
    ) -> None:
        result = await client.list_tools()
        tool_names = [tool.name for tool in result.tools]
        assert tool_name in tool_names


class TestReadOnlyModeToolRegistration:
    @pytest.mark.parametrize("tool_name", READ_ONLY_TOOL_NAMES)
    async def test_read_only_tools_are_registered(
        self,
        client_read_only: Client,
        tool_name: str,
    ) -> None:
        result = await client_read_only.list_tools()
        tool_names = [tool.name for tool in result.tools]
        assert tool_name in tool_names

    @pytest.mark.parametrize("tool_name", NON_READ_TOOL_NAMES)
    async def test_non_read_tools_are_not_registered(
        self,
        client_read_only: Client,
        tool_name: str,
    ) -> None:
        result = await client_read_only.list_tools()
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
        assert "Add and delete comments" not in server.instructions
        assert "Grid mutations" not in server.instructions
        assert "yfm_warnings" not in server.instructions
        # read guidance stays
        assert "page_search" in server.instructions
        assert "fetch_all" in server.instructions


class TestToolAnnotations:
    async def test_every_tool_declares_a_closed_world(
        self,
        client: Client,
    ) -> None:
        # open_world_hint left unset defaults to true; every tool here talks
        # to one configured Wiki organization, so all must say otherwise.
        result = await client.list_tools()
        for tool in result.tools:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.open_world_hint is False, tool.name


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
        # download writes to the local disk, so it is gated the same way
        assert "page_download_attachment" not in tool_names
        # only the local-filesystem tools are gated; other writes stay
        assert "page_clone" in tool_names
        assert "page_create" in tool_names
        # the instructions must not advertise the tool that is not there
        assert server.instructions is not None
        assert "upload attachments" not in server.instructions
        assert "- Add and delete comments" in server.instructions
        # nor may the always-registered read tool point at it: its oversize
        # refusal names page_download_attachment only when that tool exists
        read_tool = next(
            t for t in await server.list_tools() if t.name == "page_read_attachment"
        )
        assert read_tool.description is not None
        assert "page_download_attachment" not in read_tool.description
        assert "download_url" in read_tool.description

    async def test_the_read_tool_points_at_the_download_tool_when_it_exists(
        self,
    ) -> None:
        server = create_mcp_server(
            settings=create_test_settings(),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        read_tool = next(
            t for t in await server.list_tools() if t.name == "page_read_attachment"
        )
        assert read_tool.description is not None
        assert "page_download_attachment" in read_tool.description

    async def test_the_read_tool_drops_the_pointer_under_read_only(self) -> None:
        # WIKI_READ_ONLY also removes the download tool, so the pointer has to
        # go with it — the read tool itself stays registered either way.
        server = create_mcp_server(
            settings=create_test_settings(read_only=True),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        read_tool = next(
            t for t in await server.list_tools() if t.name == "page_read_attachment"
        )
        assert read_tool.description is not None
        assert "page_download_attachment" not in read_tool.description
        assert "download_url" in read_tool.description

    async def test_local_upload_tool_is_offered_without_oauth(self) -> None:
        server = create_mcp_server(
            settings=create_test_settings(),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        tool_names = [tool.name for tool in await server.list_tools()]
        assert "page_upload_attachment" in tool_names
        assert "page_download_attachment" in tool_names
        assert server.instructions is not None
        assert "upload attachments from the local filesystem" in server.instructions


class TestManifestSync:
    async def test_manifest_tools_match_the_registered_surface(
        self,
        mcp_server: MCPServer[Any],
    ) -> None:
        # manifest.json is the MCPB bundle metadata: its tools list is what
        # clients show before installing. Nothing generates it from the
        # code, so without this check a rename on one side or an addition on
        # the other drifts apart in silence.
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
        client: Client,
    ) -> None:
        result = await client.list_resources()
        resource_uris = [str(resource.uri) for resource in result.resources]
        assert "wiki-mcp://configuration" in resource_uris


class TestServerConfiguration:
    """Identity as the client sees it.

    mcp 2.x has no separate `initialize()` step on the high-level Client —
    connecting negotiates the era and the metadata is on the object.
    `server_info` is Implementation | None because 2026-era identity is
    optional wire metadata, so each of these asserts it is actually sent.
    """

    async def test_server_has_correct_name(
        self,
        client: Client,
    ) -> None:
        assert client.server_info is not None
        assert client.server_info.name == "Yandex Wiki Search MCP"

    async def test_server_has_instructions(
        self,
        client: Client,
    ) -> None:
        assert client.instructions

    async def test_server_has_a_description(
        self,
        client: Client,
    ) -> None:
        # Short, for a client's server list — distinct from `instructions`,
        # which is long-form guidance for the model.
        assert client.server_info is not None
        assert client.server_info.description == server_description()
        assert client.server_info.description != client.instructions

    async def test_server_info_reports_package_version(
        self,
        client: Client,
    ) -> None:
        expected = importlib.metadata.version("yandex-wiki-search-mcp")
        assert client.server_info is not None
        assert client.server_info.version == expected


class TestHealthz:
    def test_healthz_returns_200(self, mcp_server: MCPServer[Any]) -> None:
        app = mcp_server.streamable_http_app(**http_app_options(create_test_settings()))
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


class TestServerDescription:
    def test_comes_from_package_metadata(self) -> None:
        # Not repeated in the source: the same sentence is already in
        # pyproject.toml, manifest.json and server.json.
        summary = importlib.metadata.metadata("yandex-wiki-search-mcp")["Summary"]
        assert server_description() == summary
        assert summary

    def test_is_none_when_the_package_is_not_installed(self) -> None:
        # None rather than "": the field is optional on the wire, so an
        # uninstalled dev tree advertises no description instead of a blank.
        with patch(
            "importlib.metadata.metadata",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            assert server_description() is None


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

        app = server.streamable_http_app(**http_app_options(settings))
        with TestClient(app) as client:
            healthz = client.get("/healthz")
            callback = client.get("/oauth/yandex/callback?code=x&state=missing")

        assert healthz.status_code == 200
        assert callback.status_code == 400


class TestHttpTransportSettings:
    @pytest.mark.parametrize("flag", [True, False])
    def test_stateless_http_and_json_response_follow_settings(self, flag: bool) -> None:
        # mcp 2.x moved these off the constructor, so `mcp.settings` no longer
        # carries them; they land on the session manager the app is built with.
        settings = create_test_settings()
        settings.stateless_http = flag
        settings.json_response = flag

        server = create_mcp_server(
            settings=settings,
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )
        server.streamable_http_app(**http_app_options(settings))

        assert server.session_manager.stateless is flag
        assert server.session_manager.json_response is flag


class TestRunOptions:
    """run() is overloaded per transport and rejects foreign keywords."""

    def test_stdio_takes_none(self) -> None:
        settings = create_test_settings()
        settings.transport = "stdio"

        assert run_options(settings) == {}

    def test_sse_takes_the_binding_only(self) -> None:
        # json_response/stateless_http are streamable-http concepts; passing
        # them to the SSE transport is a TypeError when it starts.
        settings = create_test_settings()
        settings.transport = "sse"

        assert run_options(settings) == {"host": "0.0.0.0", "port": 8000}

    def test_streamable_http_takes_the_binding_and_the_behaviour(self) -> None:
        settings = create_test_settings()
        settings.transport = "streamable-http"

        assert run_options(settings) == {
            "host": "0.0.0.0",
            "port": 8000,
            "json_response": True,
            "stateless_http": True,
        }


class TestHostIsPassedToTheApp:
    """The 421 trap.

    mcp 2.x auto-arms DNS rebinding protection when `host` is a loopback
    address and no transport_security is given — and streamable_http_app()
    defaults `host` to 127.0.0.1. A server built without the setting's host
    therefore rejects every MCP request behind a real hostname with 421 while
    /healthz keeps answering 200, so nothing that watches the health endpoint
    would notice. These two tests pin both halves.
    """

    @staticmethod
    def _server() -> MCPServer[Any]:
        return create_mcp_server(
            settings=create_test_settings(),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

    def test_configured_host_serves_a_foreign_host_header(self) -> None:
        # TestClient sends `Host: testserver`, which the loopback allowlist
        # rejects — exactly what a real hostname runs into.
        settings = create_test_settings()
        assert settings.host == "0.0.0.0"

        app = self._server().streamable_http_app(**http_app_options(settings))
        with TestClient(app) as client:
            response = client.post(
                "/mcp", json=initialize_request(), headers=MCP_HTTP_HEADERS
            )

        assert response.status_code == 200

    def test_omitting_the_host_would_reject_it(self) -> None:
        # Not a wish, a guard: this is what the deployment does if
        # http_app_options() ever stops carrying `host`.
        app = self._server().streamable_http_app()
        with TestClient(app) as client:
            response = client.post(
                "/mcp", json=initialize_request(), headers=MCP_HTTP_HEADERS
            )

        assert response.status_code == 421
