"""The organization is chosen once, by one function, for everyone.

The two ids move as a unit. Derived independently, a request carrying
?cloudOrgId= on a server whose default is a plain WIKI_ORG_ID would send —
or report — both at once, a pair the settings validator forbids and no
request ever actually carries.
"""

import pytest

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.proto.common import YandexAuth, select_org


class TestSelectOrg:
    def test_falls_back_to_the_server_defaults(self) -> None:
        assert select_org(None, default_org_id="srv", default_cloud_org_id=None) == (
            "srv",
            None,
        )

    def test_auth_without_an_org_keeps_the_defaults(self) -> None:
        assert select_org(
            YandexAuth(token="t"), default_org_id="srv", default_cloud_org_id=None
        ) == ("srv", None)

    def test_request_org_replaces_the_default(self) -> None:
        assert select_org(
            YandexAuth(token="t", org_id="req"),
            default_org_id="srv",
            default_cloud_org_id=None,
        ) == ("req", None)

    def test_request_cloud_org_replaces_a_plain_default_entirely(self) -> None:
        # The pair moves as a unit: the server-wide org must not survive
        # alongside the request's cloud org.
        assert select_org(
            YandexAuth(token="t", cloud_org_id="req-cloud"),
            default_org_id="srv",
            default_cloud_org_id=None,
        ) == (None, "req-cloud")

    def test_request_org_replaces_a_cloud_default_entirely(self) -> None:
        assert select_org(
            YandexAuth(token="t", org_id="req"),
            default_org_id=None,
            default_cloud_org_id="srv-cloud",
        ) == ("req", None)


class TestClientAgreesWithSelectOrg:
    """What select_org returns is what actually goes on the wire."""

    @pytest.mark.parametrize(
        ("default_org", "default_cloud", "auth"),
        [
            ("srv", None, None),
            ("srv", None, YandexAuth(token="t", cloud_org_id="req-cloud")),
            (None, "srv-cloud", YandexAuth(token="t", org_id="req")),
            ("srv", None, YandexAuth(token="t", org_id="req")),
        ],
    )
    def test_headers_match_the_selection(
        self,
        default_org: str | None,
        default_cloud: str | None,
        auth: YandexAuth | None,
    ) -> None:
        client = WikiClient(
            token="test-token", org_id=default_org, cloud_org_id=default_cloud
        )
        headers = client._build_headers(auth)

        org_id, cloud_org_id = select_org(
            auth,
            default_org_id=default_org,
            default_cloud_org_id=default_cloud,
        )
        assert headers.get("X-Org-Id") == org_id
        assert headers.get("X-Cloud-Org-Id") == cloud_org_id
        # Never both — that is exactly what the API rejects.
        assert not (org_id and cloud_org_id)
