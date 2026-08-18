"""HTTP-level contract for the page endpoints.

The tool-layer tests for these run against a mocked WikiProtocol, which
asserts nothing about the URL, the query parameters or the request body.
Without the checks here a typo in a path passes the whole suite and shows
up only in the weekly contract sweep, which is opt-in and does not run on
forks.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest
from aiohttp import ClientResponse
from aiohttp.streams import StreamReader
from aioresponses import aioresponses
from pydantic import BaseModel

from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import (
    AttachmentNotFound,
    PageNotFound,
    ResponseTooLarge,
    WikiApiError,
)
from mcp_wiki.wiki.proto.common import YandexAuth
from mcp_wiki.wiki.proto.types.pages import AttachmentDeleteResponse
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


@contextmanager
def _extras_allowed(model: type[BaseModel]) -> Iterator[None]:
    """Run a block with `model` accepting unknown keys, then restore it.

    Mirrors scripts/contract_sweep.enable_extras_detection: production models
    ignore extras for token economy, so a test that wants to see one has to ask
    for the sweep's configuration explicitly.
    """
    original = model.model_config.get("extra", "ignore")
    model.model_config["extra"] = "allow"
    for cached in (
        "__pydantic_core_schema__",
        "__pydantic_validator__",
        "__pydantic_serializer__",
    ):
        if cached in model.__dict__:
            delattr(model, cached)
    model.model_rebuild(force=True)
    try:
        yield
    finally:
        model.model_config["extra"] = original
        model.model_rebuild(force=True)


class TestPageDeleteComment:
    async def test_deletes_and_returns_the_updated_count(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(payload={"comments_count": 3})
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/comments/11",
                callback=capture.callback,
            )
            response = await wiki_client.page_delete_comment(42, comment_id=11)

        assert response.comments_count == 3
        assert (response.page_id, response.comment_id, response.deleted) == (
            42,
            11,
            True,
        )
        capture.assert_called_once()
        capture.last_request.assert_headers(AUTH_HEADERS)

    async def test_empty_body_still_carries_an_acknowledgment(
        self, wiki_client: WikiClient
    ) -> None:
        # comments_count is the only thing the API sends, and _drop_none would
        # reduce a model holding just that to `{}` — a successful call with no
        # evidence in it. The id pair and `deleted` are the floor.
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/comments/11",
                status=204,
                body=b"",
            )
            response = await wiki_client.page_delete_comment(42, comment_id=11)

        assert response.model_dump() == {
            "page_id": 42,
            "comment_id": 11,
            "deleted": True,
        }

    async def test_a_non_object_body_does_not_escape_wikierror(
        self, wiki_client: WikiClient
    ) -> None:
        # grid_delete guards this the same way: a bare ValidationError would
        # bypass the WikiError hierarchy every caller above the client uses.
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/comments/11",
                payload=["unexpected"],
            )
            response = await wiki_client.page_delete_comment(42, comment_id=11)

        assert response.deleted is True

    async def test_404_carries_the_api_envelope_not_page_not_found(
        self, wiki_client: WikiClient
    ) -> None:
        # A 404 here is ambiguous (page or comment), and the API's own
        # envelope already names the culprit — so no PageNotFound mapping.
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/comments/999",
                status=404,
                payload={
                    "error_code": "NOT_FOUND",
                    "debug_message": "No Comment matches the given query.",
                },
            )
            with pytest.raises(WikiApiError, match="No Comment matches"):
                await wiki_client.page_delete_comment(42, comment_id=999)


class TestPageDownloadAttachment:
    async def test_returns_the_raw_bytes(self, wiki_client: WikiClient) -> None:
        blob = b"\x89PNG\r\n\x1a\n binary"
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                body=blob,
            )
            data = await wiki_client.page_download_attachment(42, file_id=5)

        assert data == blob

    async def test_404_maps_to_attachment_not_found(
        self, wiki_client: WikiClient
    ) -> None:
        # A miss answers with a placeholder GIF body, not the JSON error
        # envelope — the client names the miss itself.
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                status=404,
                body=b"GIF89a...",
            )
            with pytest.raises(AttachmentNotFound, match="file 5 on page 42"):
                await wiki_client.page_download_attachment(42, file_id=5)


async def _capture_too_large(response: ClientResponse) -> ResponseTooLarge | None:
    """Run the capped read inside gather() and hand back the refusal it raised."""
    try:
        await WikiClient._read_capped(response, "GET", "path", 10)
    except ResponseTooLarge as exc:
        return exc
    return None


def _live_stream(
    prefetched: bytes, *, later: bytes = b"", declared: int | None = None
) -> tuple[ClientResponse, Callable[[], Awaitable[None]]]:
    """A real StreamReader holding `prefetched`, plus a coroutine feeding `later`.

    aioresponses hands the whole body over in one piece, so nothing it drives can
    tell a drained read from a single one. Feeding the rest only after the read
    has started is what makes these tests discriminate: on a single
    `read(n)` the body ends at `prefetched`.
    """
    reader = StreamReader(
        mock.Mock(_reading_paused=False),
        limit=2**16,
        loop=asyncio.get_event_loop(),
    )
    reader.feed_data(prefetched)

    async def feed_the_rest() -> None:
        await asyncio.sleep(0)
        if later:
            reader.feed_data(later)
        reader.feed_eof()

    response = cast(
        ClientResponse, SimpleNamespace(content=reader, content_length=declared)
    )
    return response, feed_the_rest


class TestDownloadCeiling:
    async def test_content_length_over_the_cap_refuses_before_reading(self) -> None:
        # The stream is left with nothing in it and no EOF, so any attempt to
        # read would block: returning at all proves the refusal came from the
        # declared length. (aioresponses cannot drive this branch — it sets no
        # Content-Length, so everything it mocks takes the streaming path.)
        response, _ = _live_stream(b"", declared=100)

        with pytest.raises(ResponseTooLarge, match="declared 100 bytes"):
            await asyncio.wait_for(
                WikiClient._read_capped(response, "GET", "path", 10), timeout=2
            )

    async def test_content_length_under_the_cap_is_read_normally(self) -> None:
        response, feed_the_rest = _live_stream(b"abc", later=b"def", declared=6)

        body, _ = await asyncio.gather(
            WikiClient._read_capped(response, "GET", "path", 10),
            feed_the_rest(),
        )

        assert body == b"abcdef"

    async def test_a_lying_content_length_does_not_defeat_the_ceiling(self) -> None:
        # Content-Length is the compressed size on a compressed response, so a
        # small declared length can precede a large decompressed body. The
        # stream loop is the real guard, not the header.
        response, feed_the_rest = _live_stream(b"x" * 8, later=b"x" * 8, declared=5)

        refused, _ = await asyncio.gather(
            _capture_too_large(response),
            feed_the_rest(),
        )

        assert refused is not None
        assert "ran past the 10-byte ceiling" in str(refused)

    async def test_a_body_at_the_cap_still_arrives(
        self, wiki_client: WikiClient
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                body=b"x" * 10,
            )
            assert (
                await wiki_client.page_download_attachment(42, file_id=5, max_bytes=10)
                == b"x" * 10
            )

    async def test_a_body_arriving_in_pieces_is_read_to_completion(self) -> None:
        # aiohttp's StreamReader.read(n) returns whatever is already buffered,
        # up to n — not n bytes. A body under the cap that arrives in several
        # network chunks must be drained, not silently truncated.
        response, feed_the_rest = _live_stream(b"abc", later=b"def")

        body, _ = await asyncio.gather(
            WikiClient._read_capped(response, "GET", "path", 10),
            feed_the_rest(),
        )

        assert body == b"abcdef"

    async def test_a_body_that_crosses_the_cap_late_is_still_refused(self) -> None:
        # The overflow arrives only after the read has begun, so the ceiling has
        # to hold across chunk boundaries rather than on the first buffer alone.
        # With a single read(n) the body would stop at 8 bytes and pass.
        response, feed_the_rest = _live_stream(b"x" * 8, later=b"x" * 8)

        refused, _ = await asyncio.gather(
            _capture_too_large(response),
            feed_the_rest(),
        )

        assert refused is not None
        assert "no Content-Length" in str(refused)
        assert "ran past the 10-byte ceiling" in str(refused)

    async def test_an_error_body_is_truncated_not_refused(
        self, wiki_client: WikiClient
    ) -> None:
        # The ceiling must not swallow the API's own explanation: a capped
        # request that fails still has to surface the error, so the error body
        # is truncated rather than turned into a ResponseTooLarge.
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                status=403,
                payload={"error_code": "FORBIDDEN", "debug_message": "nope"},
            )
            with pytest.raises(WikiApiError, match="FORBIDDEN"):
                await wiki_client.page_download_attachment(42, file_id=5, max_bytes=1)

    async def test_a_huge_error_body_stops_at_the_error_ceiling(self) -> None:
        response, feed_the_rest = _live_stream(b"y" * 8, later=b"y" * 8)

        body, _ = await asyncio.gather(
            WikiClient._read_truncated(response, 10),
            feed_the_rest(),
        )

        assert body == b"y" * 10

    async def test_no_max_bytes_reads_everything(self, wiki_client: WikiClient) -> None:
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                body=b"x" * 100,
            )
            assert len(await wiki_client.page_download_attachment(42, file_id=5)) == 100

    async def test_a_download_is_not_retried(self, wiki_client: WikiClient) -> None:
        # GETs are retryable by default, but repeating this one re-transfers
        # the whole file for no gain.
        capture = RequestCapture(status=503)
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                callback=capture.callback,
                repeat=True,
            )
            with pytest.raises(WikiApiError):
                await wiki_client.page_download_attachment(42, file_id=5)

        capture.assert_request_count(1)


class TestPageDeleteAttachment:
    async def test_builds_the_acknowledgment_from_a_204(
        self, wiki_client: WikiClient
    ) -> None:
        capture = RequestCapture(status=204)
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5",
                callback=capture.callback,
            )
            response = await wiki_client.page_delete_attachment(42, file_id=5)

        assert response == AttachmentDeleteResponse(page_id=42, file_id=5, deleted=True)
        capture.assert_called_once()
        capture.last_request.assert_headers(AUTH_HEADERS)

    async def test_a_future_json_body_reaches_the_model(
        self, wiki_client: WikiClient
    ) -> None:
        # Same contract as grid_delete: the acknowledgment fields are ours, but
        # a body the API starts sending must still pass through validation,
        # where the contract sweep can see undeclared keys. Constructing the
        # model directly instead of merging would make that drift invisible.
        # extra="allow" is what scripts/contract_sweep.py flips these models to,
        # so this asserts under exactly the configuration the drift job runs in.
        with _extras_allowed(AttachmentDeleteResponse), aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5",
                payload={"files_count": 7},
            )
            response = await wiki_client.page_delete_attachment(42, file_id=5)

        assert (response.page_id, response.file_id, response.deleted) == (42, 5, True)
        assert (response.model_extra or {}).get("files_count") == 7

    async def test_404_carries_the_api_envelope(self, wiki_client: WikiClient) -> None:
        with aioresponses() as mocked:
            mocked.delete(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/999",
                status=404,
                payload={
                    "error_code": "NOT_FOUND",
                    "debug_message": "No File matches the given query.",
                },
            )
            with pytest.raises(WikiApiError, match="No File matches"):
                await wiki_client.page_delete_attachment(42, file_id=999)


class TestUserGetCurrent:
    async def test_parses_the_identity(self, wiki_client: WikiClient) -> None:
        capture = RequestCapture(
            payload={
                "username": "david",
                "home_cluster": "users/david",
                "identity": {"uid": "113000", "cloud_uid": "aje8rk"},
                "org": {"dir_id": "752289", "collab_id": "9166c4"},
            }
        )
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/users/me",
                callback=capture.callback,
            )
            user = await wiki_client.user_get_current()

        assert user.username == "david"
        assert user.home_cluster == "users/david"
        assert user.identity is not None
        assert user.identity.uid == "113000"
        assert user.org is not None
        assert user.org.dir_id == "752289"
        capture.assert_called_once()
        capture.last_request.assert_headers(AUTH_HEADERS)


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
