"""What a client receives from /token when Yandex refuses a grant.

The provider tests pin what the provider raises; these pin what a client
then receives. Which exceptions the SDK's /token handler turns into an
OAuth error response — and which it lets escape as a bare 500 — is the
SDK's business, so only a request through it can tell.
"""

from collections.abc import Iterator

import pytest
from aioresponses import aioresponses
from starlette.testclient import TestClient

from tests.mcp.oauth.http_flow import (
    YANDEX_TOKEN_URL,
    YANDEX_TOKENS,
    authorize,
    oauth_app,
    redeem_code,
    register,
)


@pytest.fixture
def http() -> Iterator[TestClient]:
    with oauth_app() as app:
        yield app.http


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
    with oauth_app(expiry=None) as app:
        http = app.http
        registration = register(http)
        assert registration["client_secret_expires_at"] == 0

        code = authorize(http, registration)
        with aioresponses() as yandex:
            yandex.post(YANDEX_TOKEN_URL, payload=YANDEX_TOKENS)
            response = redeem_code(http, registration, code)

    assert response.status_code == 200, response.text
