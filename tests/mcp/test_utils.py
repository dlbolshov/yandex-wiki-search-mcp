from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import Context

from mcp_wiki.mcp.utils import get_yandex_auth, resolve_page_locator


def make_ctx(query_params: dict[str, str] | None = None) -> Context[Any, Any, Any]:
    request = (
        None if query_params is None else SimpleNamespace(query_params=query_params)
    )
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))
    return cast(Context[Any, Any, Any], ctx)


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
