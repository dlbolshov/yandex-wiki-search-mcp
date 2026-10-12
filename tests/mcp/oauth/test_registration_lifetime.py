"""Dynamic registrations expire after an idle window — over real HTTP.

What reaches the store depends on what the SDK's /register stamps, and that
depends on the client: one registered with token_endpoint_auth_method
"none" is issued no secret, and so no client_secret_expires_at either; one
issued a secret is stamped 0, "never", since secrets no longer expire. Only
a request through the SDK's own handlers pins that, rather than what a
hand-built record assumes the SDK sends.
"""

import pytest
from aioresponses import aioresponses

from mcp_wiki.mcp.oauth.store import REFRESH_TOKEN_TTL_SECONDS
from tests.mcp.oauth.http_flow import (
    EXPIRY,
    YANDEX_TOKEN_URL,
    YANDEX_TOKENS,
    StoreKind,
    authorize,
    oauth_app,
    redeem_code,
    register,
)

# Covers the seconds a test takes between the server stamping a deadline
# and the test reading it back.
SLACK = 60


@pytest.fixture(params=["memory", "redis"])
def store(request: pytest.FixtureRequest) -> StoreKind:
    return request.param


def test_a_public_registration_expires(store: StoreKind) -> None:
    with oauth_app(store) as app:
        body = register(app.http, "none")
        remaining = app.remaining(body["client_id"])

    # What the fix rests on: no secret, so the SDK sets no deadline of its
    # own, and before it the record was kept forever in either store.
    assert body.get("client_secret") is None
    assert body.get("client_secret_expires_at") is None
    assert remaining is not None
    assert EXPIRY - SLACK < remaining <= EXPIRY


def test_a_confidential_registration_gets_the_same_window(store: StoreKind) -> None:
    with oauth_app(store) as app:
        body = register(app.http, "client_secret_post")
        remaining = app.remaining(body["client_id"])

    # Its secret never expires on its own — a deadline there cut off clients
    # in daily use — so the client is told 0 and the idle window applies.
    assert body["client_secret"]
    assert body["client_secret_expires_at"] == 0
    assert remaining is not None
    assert EXPIRY - SLACK < remaining <= EXPIRY


@pytest.mark.parametrize("auth_method", ["none", "client_secret_post"])
def test_nothing_expires_with_the_expiry_switched_off(
    store: StoreKind, auth_method: str
) -> None:
    # The SDK stamps a confidential client 0, RFC 7591's "never" — which the
    # in-memory store once read as a 1970 deadline, sweeping the lot.
    with oauth_app(store, expiry=None) as app:
        body = register(app.http, auth_method)
        remaining = app.remaining(body["client_id"])

    assert remaining is None


@pytest.mark.parametrize("auth_method", ["none", "client_secret_post"])
def test_a_token_grant_renews_a_registration(
    store: StoreKind, auth_method: str
) -> None:
    with oauth_app(store) as app:
        registration = register(app.http, auth_method)
        code = authorize(app.http, registration)
        with aioresponses() as yandex:
            yandex.post(YANDEX_TOKEN_URL, payload=YANDEX_TOKENS)
            token = redeem_code(app.http, registration, code)
        remaining = app.remaining(registration["client_id"])

    assert token.status_code == 200, token.text
    # The window restarts when the grant's last token stops working — here
    # the 31-day refresh token, the access token lasting an hour — so a
    # client coming back after both finds its registration and signs in.
    window = REFRESH_TOKEN_TTL_SECONDS + EXPIRY
    assert remaining is not None
    assert window - SLACK < remaining <= window


def test_a_registration_outlives_the_access_token_it_was_issued(
    store: StoreKind,
) -> None:
    # Yandex ID tokens commonly last a year, and bearer requests renew
    # nothing: the client comes back only once the token has expired, so
    # the registration has to outlast the token by a whole idle window, or
    # the client finds it gone the day it needs a new one.
    year = 365 * 24 * 60 * 60
    with oauth_app(store) as app:
        registration = register(app.http, "none")
        code = authorize(app.http, registration)
        with aioresponses() as yandex:
            yandex.post(YANDEX_TOKEN_URL, payload={**YANDEX_TOKENS, "expires_in": year})
            token = redeem_code(app.http, registration, code)
        remaining = app.remaining(registration["client_id"])

    assert token.status_code == 200, token.text
    assert remaining is not None
    assert year + EXPIRY - SLACK < remaining <= year + EXPIRY
