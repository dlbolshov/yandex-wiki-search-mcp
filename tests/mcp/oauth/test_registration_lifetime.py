"""Dynamic registrations expire, public ones included — over real HTTP.

What reaches the store depends on what the SDK's /register stamps, and that
depends on the client: one registered with token_endpoint_auth_method
"none" is issued no secret, and so no client_secret_expires_at either. Only
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


def test_a_confidential_registration_expires_when_its_client_was_told(
    store: StoreKind,
) -> None:
    with oauth_app(store) as app:
        body = register(app.http, "client_secret_post")
        remaining = app.remaining(body["client_id"])

    assert body["client_secret_expires_at"] == body["client_id_issued_at"] + EXPIRY
    assert remaining is not None
    assert EXPIRY - SLACK < remaining <= EXPIRY


@pytest.mark.parametrize("auth_method", ["none", "client_secret_post"])
def test_nothing_expires_with_the_expiry_switched_off(
    store: StoreKind, auth_method: str
) -> None:
    # The SDK stamps a confidential client 0 here, RFC 7591's "never": the
    # in-memory store once read it as a 1970 deadline and swept the lot.
    with oauth_app(store, expiry=None) as app:
        body = register(app.http, auth_method)
        remaining = app.remaining(body["client_id"])

    assert remaining is None


def test_a_token_grant_renews_a_public_registration(store: StoreKind) -> None:
    with oauth_app(store) as app:
        registration = register(app.http, "none")
        code = authorize(app.http, registration)
        with aioresponses() as yandex:
            yandex.post(YANDEX_TOKEN_URL, payload=YANDEX_TOKENS)
            token = redeem_code(app.http, registration, code)
        remaining = app.remaining(registration["client_id"])

    assert token.status_code == 200, token.text
    # EXPIRY is 30 days, shorter than the refresh token on purpose: a TTL
    # past it can only have come from the grant, which sets no less than
    # the refresh token it issued lives.
    assert remaining is not None
    assert REFRESH_TOKEN_TTL_SECONDS - SLACK < remaining <= REFRESH_TOKEN_TTL_SECONDS
