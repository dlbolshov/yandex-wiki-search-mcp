"""Production wiring: the lifespan and the OAuth store the server actually builds.

Every other test injects a fake lifespan and a mocked protocol, so the code
that turns Settings into a live WikiClient and an OAuth store runs nowhere —
a setting dropped on the way through would be invisible.
"""

import base64
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import AnyHttpUrl, SecretStr

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.oauth.stores.memory import InMemoryOAuthStore
from mcp_wiki.mcp.oauth.stores.redis import RedisOAuthStore
from mcp_wiki.mcp.server import create_mcp_server, make_wiki_lifespan
from mcp_wiki.settings import Settings
from mcp_wiki.wiki.custom.client import WikiClient
from tests.mcp.conftest import create_test_settings, make_test_lifespan


def run_lifespan(settings: Settings) -> Any:
    """Enter the real lifespan. It ignores the server argument it is handed."""
    return make_wiki_lifespan(settings)(cast(Any, None))


def oauth_store(server: Any) -> Any:
    """The store the server built for its OAuth provider."""
    return server._auth_server_provider._store


def oauth_settings(**overrides: Any) -> Settings:
    settings = create_test_settings()
    settings.oauth_enabled = True
    settings.oauth_client_id = "client-id"
    settings.oauth_client_secret = SecretStr("client-secret")
    settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.com")
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class TestWikiLifespan:
    async def test_it_builds_a_client_from_the_settings(self) -> None:
        settings = create_test_settings()
        settings.wiki_max_retries = 5
        settings.wiki_api_base_url = "https://api.example.test"
        settings.wiki_web_base_url = "https://wiki.example.test"

        async with run_lifespan(settings) as context:
            wiki = context.wiki
            assert isinstance(wiki, WikiClient)
            # Settings that silently fail to arrive here change behavior
            # nowhere else: retries, auth and the organization all live in
            # the client.
            assert wiki._token == "test-token"
            assert wiki._org_id == "test-org"
            assert wiki._max_retries == 5
            assert wiki._base_url == "https://api.example.test"
            assert wiki._session is not None and not wiki._session.closed
            assert context.web_base_url == "https://wiki.example.test"

        assert wiki._session is None, "the lifespan must close what it opened"

    async def test_an_iam_token_reaches_the_client(self) -> None:
        settings = create_test_settings()
        settings.wiki_token = None
        settings.wiki_iam_token = SecretStr("iam-token")

        async with run_lifespan(settings) as context:
            wiki = context.wiki
            assert isinstance(wiki, WikiClient)
            assert wiki._iam_token == "iam-token"
            assert wiki._token is None

    def test_the_server_builds_its_own_lifespan_when_given_none(self) -> None:
        server = create_mcp_server(settings=create_test_settings())

        # `settings` is the public home of the lifespan in mcp 2.x; the
        # private `_mcp_server` this used to read became `_lowlevel_server`.
        assert server.settings.lifespan is not None


class TestOAuthStoreSelection:
    def test_memory_is_the_default(self) -> None:
        server = create_mcp_server(
            settings=oauth_settings(oauth_store="memory"),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert isinstance(oauth_store(server), InMemoryOAuthStore)

    def test_redis_is_built_from_the_redis_settings(self) -> None:
        key = base64.b64encode(b"k" * 32).decode()
        server = create_mcp_server(
            settings=oauth_settings(
                oauth_store="redis",
                oauth_encryption_keys=SecretStr(key),
                redis_endpoint="redis.example.test",
                redis_port=6380,
            ),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert isinstance(oauth_store(server), RedisOAuthStore)

    def test_redis_without_encryption_keys_is_refused(self) -> None:
        with pytest.raises(ValueError, match="OAUTH_ENCRYPTION_KEYS"):
            create_mcp_server(settings=oauth_settings(oauth_store="redis"))

    def test_an_unknown_store_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unsupported OAuth store"):
            create_mcp_server(settings=oauth_settings(oauth_store="mongo"))


class TestOAuthConfigurationGuards:
    """Settings validates these too; the server refuses rather than trusting it."""

    @pytest.mark.parametrize(
        ("field", "message"),
        [
            ("oauth_client_id", "OAuth client ID must be set"),
            ("oauth_client_secret", "OAuth client secret must be set"),
            ("mcp_server_public_url", "MCP server public url must be set"),
        ],
    )
    def test_a_missing_credential_is_refused(self, field: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            create_mcp_server(settings=oauth_settings(**{field: None}))


class TestScopes:
    def test_scopes_are_dropped_when_disabled(self) -> None:
        server = create_mcp_server(
            settings=oauth_settings(oauth_use_scopes=False),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert server.settings.auth is not None
        assert server.settings.auth.required_scopes is None

    def test_read_only_asks_for_the_read_scope_only(self) -> None:
        server = create_mcp_server(
            settings=oauth_settings(wiki_read_only=True),
            lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
        )

        assert server.settings.auth is not None
        assert server.settings.auth.required_scopes == ["wiki:read"]
