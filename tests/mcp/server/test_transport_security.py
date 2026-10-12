"""Which Host and Origin the HTTP endpoint answers: DNS-rebinding protection.

Without OAuth the endpoint authenticates nobody, so a page the operator
opens could drive it: its own name re-resolved to 127.0.0.1 makes a request
to its origin reach this server, same-origin as far as the browser knows,
with the page's name in `Host`. Loopback publishing (`-p 127.0.0.1:8000`)
does nothing about that; the Host check does. Over real HTTP through the
SDK's app, because the SDK is what enforces it, on both protocol eras.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthToken
from pydantic import AnyHttpUrl, SecretStr
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.oauth.provider import YandexOAuthAuthorizationServerProvider
from mcp_wiki.mcp.server import (
    allowed_hosts_summary,
    create_mcp_server,
    http_app_options,
    transport_security,
)
from mcp_wiki.settings import Settings
from tests.mcp.conftest import (
    MCP_HTTP_HEADERS,
    create_test_settings,
    initialize_request,
    make_test_lifespan,
)

LOOPBACK = [
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "[::1]",
    "[::1]:*",
]


def plain_http(**overrides: Any) -> Settings:
    settings = create_test_settings()
    settings.transport = "streamable-http"
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


def with_oauth(settings: Settings) -> Settings:
    settings.oauth_enabled = True
    settings.oauth_client_id = "client-id"
    settings.oauth_client_secret = SecretStr("client-secret")
    settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.com")
    return settings


class TestTheRule:
    def test_without_oauth_only_loopback_names_pass(self) -> None:
        security = transport_security(plain_http())

        assert security is not None
        assert security.enable_dns_rebinding_protection is True
        assert security.allowed_hosts == LOOPBACK
        assert "http://localhost:*" in security.allowed_origins
        assert "https://[::1]:*" in security.allowed_origins

    def test_a_listed_name_passes_bare_and_on_any_port(self) -> None:
        # A proxy on 443 sends no port in Host; a direct client sends one.
        security = transport_security(
            plain_http(mcp_allowed_hosts=["wiki.example.com", "mcp-wiki:8000"])
        )

        assert security is not None
        assert security.allowed_hosts == [
            "wiki.example.com",
            "wiki.example.com:*",
            "mcp-wiki:8000",
        ]

    def test_an_ipv6_name_without_a_port_gets_one(self) -> None:
        security = transport_security(plain_http(mcp_allowed_hosts=["[::1]"]))

        assert security is not None
        assert security.allowed_hosts == ["[::1]", "[::1]:*"]

    @pytest.mark.parametrize("hosts", [["*"], ["localhost", "*"]])
    def test_a_star_switches_the_check_off(self, hosts: list[str]) -> None:
        assert transport_security(
            plain_http(mcp_allowed_hosts=hosts)
        ) == TransportSecuritySettings(enable_dns_rebinding_protection=False)

    @pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
    def test_oauth_checks_nothing_unless_asked(self, host: str) -> None:
        # A bearer token is no credential a browser attaches by itself, and
        # an OAuth deployment sits behind a proxy whose Host may be rewritten.
        # Explicitly off, not None: given None the SDK would arm its own
        # loopback-only check for a loopback bind.
        assert transport_security(
            with_oauth(plain_http(host=host))
        ) == TransportSecuritySettings(enable_dns_rebinding_protection=False)

    def test_oauth_honours_an_explicit_list(self) -> None:
        settings = with_oauth(plain_http(mcp_allowed_hosts=["mcp.example.com"]))

        security = transport_security(settings)

        assert security is not None
        assert security.allowed_hosts == ["mcp.example.com", "mcp.example.com:*"]


class TestTheStartupLine:
    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            (create_test_settings(), "-"),
            (plain_http(), "localhost,127.0.0.1,[::1]"),
            (plain_http(mcp_allowed_hosts=["mcp-wiki"]), "mcp-wiki"),
            (plain_http(mcp_allowed_hosts=["*"]), "*"),
            # Under OAuth with nothing listed nothing is checked, whatever
            # the bind.
            (with_oauth(plain_http()), "*"),
            (with_oauth(plain_http(host="127.0.0.1")), "*"),
        ],
        ids=["stdio", "default", "listed", "off", "oauth", "oauth-loopback"],
    )
    def test_says_what_is_checked(self, settings: Settings, expected: str) -> None:
        assert allowed_hosts_summary(settings) == expected


def serve(settings: Settings, wiki: AsyncMock) -> Any:
    server = create_mcp_server(
        settings=settings,
        lifespan=make_test_lifespan(AppContext(wiki=wiki)),
    )
    return server.streamable_http_app(**http_app_options(settings))


def tools_call() -> dict[str, Any]:
    # A 2025-era stateless call needs no initialize first: one request is
    # all a rebinding page has to make.
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "user_get_current", "arguments": {}},
    }


MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def modern(
    method: str, name: str | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """A 2026-07-28 request: no handshake, the method in a header too."""
    params: dict[str, Any] = {"_meta": MODERN_META}
    headers = {
        **MCP_HTTP_HEADERS,
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if name is not None:
        params |= {"name": name, "arguments": {}}
        headers["Mcp-Name"] = name
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers


class TestOverHttp:
    @pytest.fixture
    def wiki(self) -> AsyncMock:
        return AsyncMock()

    def test_the_modern_era_is_checked_too(self, wiki: AsyncMock) -> None:
        # A different handler in the SDK from the 2025-era one; a page needs
        # no handshake here either.
        body, headers = modern("tools/call", "user_get_current")
        with TestClient(
            serve(plain_http(), wiki), base_url="http://attacker.example:8000"
        ) as client:
            refused = client.post("/mcp", json=body, headers=headers)
        listing, list_headers = modern("tools/list")
        with TestClient(
            serve(plain_http(), wiki), base_url="http://localhost:8000"
        ) as client:
            served = client.post("/mcp", json=listing, headers=list_headers)

        assert refused.status_code == 421
        wiki.user_get_current.assert_not_awaited()
        assert served.status_code == 200, served.text

    def test_an_oauth_server_on_loopback_serves_its_public_name(self) -> None:
        # The deployment behind a same-host proxy: bound to 127.0.0.1 (the
        # default), reached as wiki.example.com, with a valid token. Given no
        # transport_security, the SDK would arm its loopback-only check for
        # that bind and turn every such request away with a 421.
        settings = with_oauth(plain_http(host="127.0.0.1"))
        settings.oauth_use_scopes = False
        server = create_mcp_server(
            settings=settings,
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )
        provider = server._auth_server_provider
        assert isinstance(provider, YandexOAuthAuthorizationServerProvider)
        store = provider._store
        asyncio.run(
            store.save_oauth_token(
                OAuthToken(
                    access_token="bearer-1", token_type="Bearer", expires_in=3600
                ),
                "client-1",
                [],
                None,
            )
        )
        app = server.streamable_http_app(**http_app_options(settings))
        with TestClient(app, base_url="https://wiki.example.com") as client:
            response = client.post(
                "/mcp",
                json=initialize_request(),
                headers={**MCP_HTTP_HEADERS, "Authorization": "Bearer bearer-1"},
            )

        assert response.status_code == 200, response.text

    def test_a_rebound_page_is_refused_before_any_tool_runs(
        self, wiki: AsyncMock
    ) -> None:
        with TestClient(
            serve(plain_http(), wiki), base_url="http://attacker.example:8000"
        ) as client:
            headers = {**MCP_HTTP_HEADERS, "Origin": "http://attacker.example:8000"}
            hello = client.post("/mcp", json=initialize_request(), headers=headers)
            call = client.post("/mcp", json=tools_call(), headers=headers)

        assert hello.status_code == 421
        assert call.status_code == 421
        wiki.user_get_current.assert_not_awaited()

    @pytest.mark.parametrize(
        "host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "localhost"]
    )
    def test_a_local_client_is_served(self, wiki: AsyncMock, host: str) -> None:
        # No Origin, as every non-browser client sends none. Host is set
        # directly: TestClient cannot parse an IPv6 base_url.
        with TestClient(
            serve(plain_http(), wiki), base_url="http://localhost"
        ) as client:
            response = client.post(
                "/mcp",
                json=initialize_request(),
                headers={**MCP_HTTP_HEADERS, "Host": host},
            )

        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("origin", "status"),
        [("http://evil.example", 403), ("null", 403), ("http://localhost:6274", 200)],
        ids=["foreign-page", "opaque", "local-browser-client"],
    )
    def test_a_browser_origin_must_be_local_too(
        self, wiki: AsyncMock, origin: str, status: int
    ) -> None:
        with TestClient(
            serve(plain_http(), wiki), base_url="http://localhost:8000"
        ) as client:
            response = client.post(
                "/mcp",
                json=initialize_request(),
                headers={**MCP_HTTP_HEADERS, "Origin": origin},
            )

        assert response.status_code == status

    def test_a_listed_name_is_served(self, wiki: AsyncMock) -> None:
        settings = plain_http(mcp_allowed_hosts=["wiki.example.com"])
        with TestClient(
            serve(settings, wiki), base_url="https://wiki.example.com"
        ) as client:
            response = client.post(
                "/mcp", json=initialize_request(), headers=MCP_HTTP_HEADERS
            )

        assert response.status_code == 200

    def test_the_health_check_is_not_behind_it(self, wiki: AsyncMock) -> None:
        # /healthz is outside the MCP transport; a probe by any name works.
        with TestClient(
            serve(plain_http(), wiki), base_url="http://attacker.example:8000"
        ) as client:
            response = client.get("/healthz")

        assert response.status_code == 200
