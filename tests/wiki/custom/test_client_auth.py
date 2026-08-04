"""Authorization and organization headers.

Every supported way to authenticate and to name an organization, plus the
configuration errors — these are the messages an operator sees when a
deployment is wired up wrong, so they are worth pinning.
"""

import pytest
from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import WikiConfigError
from mcp_wiki.wiki.proto.common import YandexAuth
from tests.aioresponses_utils import RequestCapture

PAGE_URL = "https://api.wiki.yandex.net/v1/pages/1"


async def _capture_headers(client: WikiClient, **kwargs: object) -> RequestCapture:
    capture = RequestCapture(payload={"id": 1})
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, callback=capture.callback)
        await client.page_get(1, **kwargs)  # type: ignore[arg-type]
    capture.assert_called_once()
    return capture


class TestAuthHeader:
    async def test_oauth_token_uses_the_configured_scheme(
        self, wiki_client: WikiClient
    ) -> None:
        capture = await _capture_headers(wiki_client)

        capture.last_request.assert_header("Authorization", "OAuth test-token")

    async def test_iam_token_uses_bearer(self, wiki_client_iam: WikiClient) -> None:
        # IAM tokens are always Bearer, regardless of wiki_auth_scheme.
        capture = await _capture_headers(wiki_client_iam)

        capture.last_request.assert_header("Authorization", "Bearer test-iam-token")

    async def test_per_request_token_wins_over_the_configured_one(
        self, wiki_client: WikiClient, yandex_auth: YandexAuth
    ) -> None:
        capture = await _capture_headers(wiki_client, auth=yandex_auth)

        capture.last_request.assert_headers(
            {"Authorization": "OAuth auth-token", "X-Org-Id": "auth-org"}
        )

    async def test_no_credentials_at_all_is_a_config_error(
        self, wiki_client_no_auth: WikiClient
    ) -> None:
        with pytest.raises(WikiConfigError, match="No authentication method"):
            await wiki_client_no_auth.page_get(1)


class TestOrganizationHeader:
    async def test_org_id_sets_x_org_id(self, wiki_client: WikiClient) -> None:
        capture = await _capture_headers(wiki_client)

        capture.last_request.assert_header("X-Org-Id", "test-org")
        assert "X-Cloud-Org-Id" not in capture.last_request.headers

    async def test_cloud_org_id_sets_x_cloud_org_id(
        self, wiki_client_cloud_org: WikiClient
    ) -> None:
        capture = await _capture_headers(wiki_client_cloud_org)

        capture.last_request.assert_header("X-Cloud-Org-Id", "test-cloud-org")
        assert "X-Org-Id" not in capture.last_request.headers

    async def test_per_request_cloud_org_wins_over_the_configured_org(
        self, wiki_client: WikiClient, yandex_auth_cloud: YandexAuth
    ) -> None:
        # The server-wide org_id must not tag along, or the API sees both.
        capture = await _capture_headers(wiki_client, auth=yandex_auth_cloud)

        capture.last_request.assert_header("X-Cloud-Org-Id", "cloud-org")
        assert "X-Org-Id" not in capture.last_request.headers

    async def test_both_org_ids_is_a_config_error(
        self, wiki_client_both_orgs: WikiClient
    ) -> None:
        with pytest.raises(WikiConfigError, match="Only one of org_id or cloud_org_id"):
            await wiki_client_both_orgs.page_get(1)

    async def test_no_org_names_both_ways_to_supply_one(
        self, wiki_client_no_org: WikiClient
    ) -> None:
        with pytest.raises(WikiConfigError) as excinfo:
            await wiki_client_no_org.page_get(1)

        message = str(excinfo.value)
        assert "WIKI_ORG_ID" in message
        assert "?orgId=" in message
