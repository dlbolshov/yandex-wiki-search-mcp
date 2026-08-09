from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from mcp.server.mcpserver import Context

import mcp_wiki.mcp.request_ctx as request_ctx
from mcp_wiki.mcp.utils import get_yandex_auth, resolve_page_locator


def make_request(query_params: dict[str, str]) -> Any:
    return SimpleNamespace(query_params=query_params)


def make_ctx(query_params: dict[str, str] | None = None) -> Context[Any, Any]:
    request = None if query_params is None else make_request(query_params)
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))
    return cast(Context[Any, Any], ctx)


@contextmanager
def stashed_request(query_params: dict[str, str] | None) -> Iterator[None]:
    """What the middleware publishes, without running the middleware."""
    request = None if query_params is None else make_request(query_params)
    token = request_ctx._current_request.set(request)
    try:
        yield
    finally:
        request_ctx._current_request.reset(token)


class TestGetYandexAuth:
    def test_no_request_and_no_token_yields_empty_auth(self) -> None:
        auth = get_yandex_auth(make_ctx())

        assert auth.token is None
        assert auth.cloud_org_id is None
        assert auth.org_id is None

    def test_reads_org_ids_from_query_params(self) -> None:
        auth = get_yandex_auth(make_ctx({"cloudOrgId": " cloud-1 ", "orgId": "org-1"}))

        assert auth.cloud_org_id == "cloud-1"
        assert auth.org_id == "org-1"

    def test_org_id_alone_leaves_cloud_org_untouched(self) -> None:
        # The per-request override params are independent: supplying only
        # orgId must not touch cloud_org_id, and vice versa.
        auth = get_yandex_auth(make_ctx({"orgId": "org-1"}))

        assert auth.cloud_org_id is None
        assert auth.org_id == "org-1"

    def test_blank_query_params_do_not_override(self) -> None:
        # "?cloudOrgId=  " arrives as whitespace; treating it as a value
        # would send a bogus header to the API.
        auth = get_yandex_auth(make_ctx({"cloudOrgId": "  ", "orgId": ""}))

        assert auth.cloud_org_id is None
        assert auth.org_id is None

    def test_uses_the_request_access_token(self) -> None:
        token = SimpleNamespace(token="oauth-token")
        with patch("mcp_wiki.mcp.utils.get_access_token", return_value=token):
            auth = get_yandex_auth(make_ctx())

        assert auth.token == "oauth-token"


class TestGetYandexAuthWithoutAContext:
    """The path the configuration resource takes.

    The SDK injects no Context into a static-URI resource, so the org comes
    from the request the middleware stashed instead.
    """

    def test_reads_org_ids_from_the_stashed_request(self) -> None:
        with stashed_request({"cloudOrgId": " cloud-1 ", "orgId": "org-1"}):
            auth = get_yandex_auth()

        assert auth.cloud_org_id == "cloud-1"
        assert auth.org_id == "org-1"

    def test_nothing_stashed_yields_empty_auth(self) -> None:
        # Also the stdio case, and the way a middleware regression degrades:
        # no override rather than a failure.
        with stashed_request(None):
            auth = get_yandex_auth()

        assert auth.cloud_org_id is None
        assert auth.org_id is None

    def test_an_explicit_context_wins_over_the_stash(self) -> None:
        # Handlers that get a Context must not depend on the middleware, so a
        # ctx carrying its own request is authoritative.
        with stashed_request({"orgId": "from-stash"}):
            auth = get_yandex_auth(make_ctx({"orgId": "from-ctx"}))

        assert auth.org_id == "from-ctx"

    def test_a_context_without_a_request_falls_back_to_the_stash(self) -> None:
        with stashed_request({"orgId": "from-stash"}):
            auth = get_yandex_auth(make_ctx())

        assert auth.org_id == "from-stash"


class TestResolvePageLocator:
    def test_rejects_a_slug_that_normalizes_to_nothing(self) -> None:
        with pytest.raises(ValueError, match="Slug must not be empty"):
            resolve_page_locator(page_id=None, slug=" / ")

    def test_normalizes_a_full_url(self) -> None:
        page_id, slug = resolve_page_locator(
            page_id=None, slug="https://wiki.yandex.ru/users/test/page/"
        )

        assert page_id is None
        assert slug == "users/test/page"
