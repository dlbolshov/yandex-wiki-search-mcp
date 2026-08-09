"""HTTP-level contract for the page endpoints that only the live sweep covered.

The tool-layer tests for these run against a mocked WikiProtocol, so until
now nothing offline asserted the URL, the query parameters or the request
body — a typo in a path would have passed the whole suite and surfaced only
in the weekly (opt-in) contract sweep.
"""

import re

import pytest
from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import PageNotFound
from mcp_wiki.wiki.proto.common import YandexAuth
from tests.aioresponses_utils import RequestCapture

COMMENTS_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/42/comments.*")
RESOURCES_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/42/resources.*")
ATTACHMENTS_URL = re.compile(
    r"https://api\.wiki\.yandex\.net/v1/pages/42/attachments.*"
)
DESCENDANTS_URL = re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/descendants.*")

AUTH_HEADERS = {"Authorization": "OAuth test-token", "X-Org-Id": "test-org"}


class TestPageGetComments:
    async def test_sends_page_size_and_parses_results(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(
            payload={
                "results": [
                    {"id": 1, "body": "first", "author": {"id": 7, "username": "ann"}},
                    {"id": 2, "body": "reply", "parent_id": 1},
                ],
                "next_cursor": "cur-2",
            }
        )
        with aioresponses() as mocked:
            mocked.get(COMMENTS_URL, callback=capture.callback)
            response = await wiki_client.page_get_comments(42, page_size=25)

        assert [comment.id for comment in response.results] == [1, 2]
        assert response.results[0].author is not None
        assert response.results[0].author.username == "ann"
        assert response.next_cursor == "cur-2"

        capture.assert_called_once()
        capture.last_request.assert_headers(AUTH_HEADERS)
        capture.last_request.assert_params({"page_size": 25})
        assert "cursor" not in capture.last_request.params

    async def test_cursor_is_forwarded(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(COMMENTS_URL, callback=capture.callback)
            await wiki_client.page_get_comments(42, cursor="cur-2")

        capture.last_request.assert_params({"cursor": "cur-2"})

    async def test_404_reports_the_page(self, wiki_client: WikiClient) -> None:
        with aioresponses() as mocked:
            mocked.get(COMMENTS_URL, status=404, payload={})
            with pytest.raises(PageNotFound, match="42"):
                await wiki_client.page_get_comments(42)


class TestPageGetResources:
    async def test_sends_every_optional_filter(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(
            payload={"results": [{"type": "grid", "item": {"id": "g-1"}}]}
        )
        with aioresponses() as mocked:
            mocked.get(RESOURCES_URL, callback=capture.callback)
            response = await wiki_client.page_get_resources(
                42,
                resource_types=["attachment", "grid"],
                q="report",
                page_size=10,
                cursor="cur-1",
                order_by="created_at",
                order_direction="desc",
            )

        assert response.results[0].type == "grid"
        # types is comma-joined, not repeated — the API reads a single value.
        capture.last_request.assert_params(
            {
                "types": "attachment,grid",
                "q": "report",
                "page_size": 10,
                "cursor": "cur-1",
                "order_by": "created_at",
                "order_direction": "desc",
            }
        )

    async def test_omits_filters_that_were_not_given(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(RESOURCES_URL, callback=capture.callback)
            await wiki_client.page_get_resources(42)

        assert set(capture.last_request.params) == {"page_size"}


class TestPageGetAttachments:
    async def test_parses_attachments(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(
            payload={
                "results": [
                    {
                        "id": 5,
                        "name": "spec.pdf",
                        "download_url": "https://files.test/spec.pdf",
                        "mimetype": "application/pdf",
                    }
                ]
            }
        )
        with aioresponses() as mocked:
            mocked.get(ATTACHMENTS_URL, callback=capture.callback)
            response = await wiki_client.page_get_attachments(42, page_size=50)

        assert response.results[0].name == "spec.pdf"
        assert response.results[0].download_url == "https://files.test/spec.pdf"
        capture.last_request.assert_params({"page_size": 50})


class TestPageGetDescendants:
    async def test_sends_normalized_slug_and_parses_items(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(
            payload={
                "results": [
                    {"id": 2, "slug": "users/test/root/a"},
                    {"id": 3, "slug": "users/test/root/a/b"},
                ],
                "next_cursor": None,
            }
        )
        with aioresponses() as mocked:
            mocked.get(DESCENDANTS_URL, callback=capture.callback)
            response = await wiki_client.page_get_descendants(
                "https://wiki.yandex.ru/users/test/root/",
                include_self=True,
                page_size=50,
            )

        assert [item.slug for item in response.results] == [
            "users/test/root/a",
            "users/test/root/a/b",
        ]
        assert response.next_cursor is None
        # include_self travels as a lowercase string, not Python's "True".
        capture.last_request.assert_params(
            {"slug": "users/test/root", "include_self": "true", "page_size": 50}
        )


class TestPageAddComment:
    async def test_sends_body_only_for_a_top_level_comment(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"id": 11, "body": "hi"})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/42/comments",
                callback=capture.callback,
            )
            comment = await wiki_client.page_add_comment(42, body="hi")

        assert comment.id == 11
        capture.last_request.assert_json_body({"body": "hi"})

    async def test_sends_parent_and_thread_for_a_reply(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"id": 12, "body": "re", "parent_id": 11})
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/pages/42/comments",
                callback=capture.callback,
            )
            await wiki_client.page_add_comment(42, body="re", parent_id=11, thread_id=3)

        capture.last_request.assert_json_body(
            {"body": "re", "parent_id": 11, "thread_id": 3}
        )


class TestPageDeleteAndRecover:
    async def test_delete_returns_the_recovery_token(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"recovery_token": "rt-1"})
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42", callback=capture.callback
            )
            response = await wiki_client.page_delete(42)

        assert response.recovery_token == "rt-1"
        capture.last_request.assert_headers(AUTH_HEADERS)

    async def test_recover_posts_to_the_token_path(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(
            payload={"id": 42, "slug": "users/test/page", "pages_count": 1}
        )
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/recovery_tokens/rt-1/recover",
                callback=capture.callback,
            )
            response = await wiki_client.page_recover("rt-1")

        assert response.id == 42
        assert response.pages_count == 1
        capture.assert_called_once()


class TestPerRequestAuthReachesTheseEndpoints:
    async def test_comments_use_the_per_request_organization(
        self, wiki_client: WikiClient, yandex_auth_cloud: YandexAuth
    ) -> None:
        capture = RequestCapture(payload={"results": []})
        with aioresponses() as mocked:
            mocked.get(COMMENTS_URL, callback=capture.callback)
            await wiki_client.page_get_comments(42, auth=yandex_auth_cloud)

        headers = capture.last_request.headers
        assert headers["Authorization"] == "OAuth auth-token"
        assert headers["X-Cloud-Org-Id"] == "cloud-org"
        # The server-wide org is replaced, not merged.
        assert "X-Org-Id" not in headers
