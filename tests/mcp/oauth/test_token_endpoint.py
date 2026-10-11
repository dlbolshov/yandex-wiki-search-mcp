"""The OAuth flow over real HTTP, through the SDK's own handlers.

The provider tests pin what the provider raises; these pin what a client
then receives. Which exceptions the SDK's /token handler turns into an
OAuth error response — and which it lets escape as a bare 500 — is the
SDK's business, so only a request through it can tell.
"""

import base64
import hashlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yarl
from aioresponses import aioresponses
from pydantic import AnyHttpUrl, SecretStr
from starlette.testclient import TestClient

from mcp_wiki.mcp.context import AppContext
from mcp_wiki.mcp.server import create_mcp_server, http_app_options
from tests.mcp.conftest import create_test_settings, make_test_lifespan
from tests.mcp.oauth.helpers import CLIENT_REDIRECT_URI

YANDEX_TOKEN_URL = "https://oauth.yandex.test/token"
CODE_VERIFIER = "verifier-" + "x" * 50
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)
YANDEX_TOKENS = {
    "access_token": "ya-access-1",
    "token_type": "bearer",
    "expires_in": 3600,
    "refresh_token": "ya-refresh-1",
}


def build_client(expiry: int | None = 30 * 24 * 60 * 60) -> TestClient:
    settings = create_test_settings()
    settings.oauth_enabled = True
    settings.oauth_client_id = "yandex-app-id"
    settings.oauth_client_secret = SecretStr("yandex-app-secret")
    settings.oauth_server_url = AnyHttpUrl("https://oauth.yandex.test")
    settings.mcp_server_public_url = AnyHttpUrl("https://mcp.example.test")
    settings.oauth_client_secret_expiry_seconds = expiry

    server = create_mcp_server(
        settings=settings,
        lifespan=make_test_lifespan(AppContext(wiki=AsyncMock())),
    )
    # A provider exception the SDK does not handle must surface as the 500
    # the client would see, not be re-raised into the test.
    return TestClient(
        server.streamable_http_app(**http_app_options(settings)),
        raise_server_exceptions=False,
    )


@pytest.fixture
def http() -> Iterator[TestClient]:
    with build_client() as client:
        yield client


def register(http: TestClient) -> dict[str, Any]:
    response = http.post(
        "/register",
        json={
            "redirect_uris": [CLIENT_REDIRECT_URI],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def authorize(http: TestClient, registration: dict[str, Any]) -> str:
    """Walk /authorize and the Yandex callback; return the client's mcp_ code."""
    to_yandex = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": CLIENT_REDIRECT_URI,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state",
        },
        follow_redirects=False,
    )
    assert to_yandex.status_code == 302, to_yandex.text
    state_id = yarl.URL(to_yandex.headers["location"]).query["state"]

    to_client = http.get(
        "/oauth/yandex/callback",
        params={"code": "ya-code-1", "state": state_id},
        follow_redirects=False,
    )
    assert to_client.status_code == 302, to_client.text
    return yarl.URL(to_client.headers["location"]).query["code"]


def redeem_code(http: TestClient, registration: dict[str, Any], code: str) -> Any:
    return http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT_URI,
            "code_verifier": CODE_VERIFIER,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
        },
    )


class TestYandexRefusalIsAnOAuthError:
    """A refusal from Yandex's /token reaches the client as 400 invalid_grant."""

    def test_on_the_code_exchange(self, http: TestClient) -> None:
        registration = register(http)
        code = authorize(http, registration)

        with aioresponses() as yandex:
            yandex.post(
                YANDEX_TOKEN_URL,
                status=400,
                payload={
                    "error": "invalid_grant",
                    "error_description": "Code has expired",
                },
            )
            response = redeem_code(http, registration, code)

        assert response.status_code == 400
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "error": "invalid_grant",
            "error_description": (
                "Yandex OAuth refused the authorization code: "
                "HTTP 400 invalid_grant: Code has expired"
            ),
        }

        # The code was spent on the way in, which is why invalid_grant is
        # the honest answer: retrying it cannot work.
        again = redeem_code(http, registration, code)
        assert again.status_code == 400
        assert again.json()["error"] == "invalid_grant"

    def test_on_a_refresh(self, http: TestClient) -> None:
        registration = register(http)
        code = authorize(http, registration)
        with aioresponses() as yandex:
            yandex.post(YANDEX_TOKEN_URL, payload=YANDEX_TOKENS)
            tokens = redeem_code(http, registration, code)
        assert tokens.status_code == 200, tokens.text

        with aioresponses() as yandex:
            yandex.post(
                YANDEX_TOKEN_URL, status=400, payload={"error": "invalid_grant"}
            )
            response = http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.json()["refresh_token"],
                    "client_id": registration["client_id"],
                    "client_secret": registration["client_secret"],
                },
            )

        assert response.status_code == 400
        assert response.json() == {
            "error": "invalid_grant",
            "error_description": (
                "Yandex OAuth refused the refresh token: HTTP 400 invalid_grant"
            ),
        }


def test_a_registration_without_an_expiry_stays_usable() -> None:
    # With the expiry disabled the SDK stamps 0, RFC 7591's "never expires".
    # A store that read 0 as a deadline would refuse the client at once.
    with build_client(expiry=None) as http:
        registration = register(http)
        assert registration["client_secret_expires_at"] == 0

        code = authorize(http, registration)
        with aioresponses() as yandex:
            yandex.post(YANDEX_TOKEN_URL, payload=YANDEX_TOKENS)
            response = redeem_code(http, registration, code)

    assert response.status_code == 200, response.text
