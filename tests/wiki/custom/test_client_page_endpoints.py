"""HTTP-level contract for the page endpoints.

The tool-layer tests for these run against a mocked WikiProtocol, which
asserts nothing about the URL, the query parameters or the request body.
Without the checks here a typo in a path passes the whole suite and shows
up only in the weekly contract sweep, which is opt-in and does not run on
forks.
"""

import asyncio
import errno
import logging
import os
import re
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast
from unittest import mock

import pytest
from aiohttp import ClientResponse
from aiohttp.streams import StreamReader
from aioresponses import aioresponses
from pydantic import BaseModel

from mcp_wiki.wiki.custom import client as client_module
from mcp_wiki.wiki.custom.client import WikiClient
from mcp_wiki.wiki.custom.errors import (
    AttachmentNotFound,
    PageNotFound,
    ResponseTooLarge,
    WikiApiError,
    WikiLocalFileError,
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

# Mirrors Executor.submit's own signature, so the stub below overrides it
# rather than shadowing it with a narrower one.
_T = TypeVar("_T")


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


class TestReadAttachmentBytes:
    async def test_returns_the_body_with_its_content_type(
        self, wiki_client: WikiClient
    ) -> None:
        blob = b"\x89PNG\r\n\x1a\n binary"
        with aioresponses() as mocked:
            mocked.get(
                "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download",
                body=blob,
                content_type="image/png",
            )
            reply = await wiki_client.page_read_attachment_bytes(42, file_id=5)

        # The header rides along: the tool above decides how the bytes
        # travel (image block vs text vs blob) and needs the wire's claim.
        assert reply.content == blob
        assert reply.mimetype == "image/png"

    async def test_a_callable_ceiling_is_picked_by_the_content_type(
        self, wiki_client: WikiClient
    ) -> None:
        # One 10-byte body, one mime-dependent cap: images get 100, the
        # rest get 4. Which ceiling applied is observable in the outcome.
        def ceiling(content_type: str | None) -> int:
            if content_type is not None and content_type.startswith("image/"):
                return 100
            return 4

        url = "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download"
        with aioresponses() as mocked:
            mocked.get(url, body=b"x" * 10, content_type="image/png")
            reply = await wiki_client.page_read_attachment_bytes(
                42, file_id=5, max_bytes=ceiling
            )
        assert reply.content == b"x" * 10

        with aioresponses() as mocked:
            mocked.get(url, body=b"x" * 10, content_type="text/plain")
            with pytest.raises(ResponseTooLarge):
                await wiki_client.page_read_attachment_bytes(
                    42, file_id=5, max_bytes=ceiling
                )

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
                await wiki_client.page_read_attachment_bytes(42, file_id=5)


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
            reply = await wiki_client.page_read_attachment_bytes(
                42, file_id=5, max_bytes=lambda _: 10
            )
            assert reply.content == b"x" * 10

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

    async def test_the_cap_never_asks_for_more_than_it_will_keep(self) -> None:
        # _drain is shared with the streaming download, and it used to request
        # a flat DOWNLOAD_CHUNK_SIZE (1 MiB) whatever the ceiling was — so a
        # 64 KiB error-body cap could materialize a whole chunk before the
        # check below it fired, quietly undoing the one guarantee
        # ResponseTooLarge exists to make.
        requested: list[int] = []
        response, feed_the_rest = _live_stream(b"x" * 8, later=b"x" * 8)
        inner = response.content

        class _Spy:
            def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
                requested.append(n)
                return inner.iter_chunked(n)

        spied = cast(
            ClientResponse, SimpleNamespace(content=_Spy(), content_length=None)
        )

        body, _ = await asyncio.gather(
            WikiClient._drain(spied, 10),
            feed_the_rest(),
        )

        assert requested == [11], "the ceiling plus the byte that proves overflow"
        assert body == b"x" * 11

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
                await wiki_client.page_read_attachment_bytes(
                    42, file_id=5, max_bytes=lambda _: 1
                )

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
            reply = await wiki_client.page_read_attachment_bytes(42, file_id=5)
            assert len(reply.content) == 100

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
                await wiki_client.page_read_attachment_bytes(42, file_id=5)

        capture.assert_request_count(1)


DOWNLOAD_URL = "https://api.wiki.yandex.net/v1/pages/42/attachments/5/download"


async def _eventually_empty(path: Path, deadline_seconds: float = 5.0) -> None:
    """Wait for `path` to hold nothing, then assert it.

    Cleanup after a cancelled or failed download is queued on that call's
    worker and the pool is shut down with `wait=False`, deliberately: waiting
    would block the event loop for the duration of an in-flight write. So the
    `.part` disappears a moment after the call returns, and a test has to wait
    for it rather than assume the loop was held hostage until it was gone.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if not _dir_names(path):
            return
        await asyncio.sleep(0.01)
    assert _dir_names(path) == []


def _dir_names(path: Path) -> list[str]:
    # Sync on purpose: pathlib inside an async function trips ASYNC240,
    # while os.listdir trips PTH208 — a sync helper satisfies both.
    return [p.name for p in path.iterdir()]


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode semantics: chmod on Windows only toggles read-only",
)
class TestDownloadFilePermissions:
    async def test_a_new_file_gets_the_umask_default_not_0600(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # tempfile.mkstemp hardcodes 0600 in defiance of the umask, and
        # os.replace moves the inode, so that mode would become the delivered
        # file's — every download owner-only. os.open with 0o666 lets the
        # kernel apply the umask, exactly like a plain open(path, "wb").
        target = tmp_path / "fresh.bin"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        umask = os.umask(0o022)
        os.umask(umask)
        assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~umask

    async def test_a_download_is_never_executable(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # 0o666 carries no execute bit, so a downloaded script or binary
        # cannot run until the user chmods it deliberately.
        target = tmp_path / "script.sh"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"#!/bin/sh\necho hi\n")
            await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert not stat.S_IMODE(target.stat().st_mode) & 0o111

    async def test_overwrite_keeps_the_replaced_file_mode(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # Writing over a file does not change its permissions, and neither
        # should this: someone set 0640 on purpose.
        target = tmp_path / "kept.bin"
        target.write_bytes(b"old")
        target.chmod(0o640)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"new")
            await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target), overwrite=True
            )

        assert target.read_bytes() == b"new"
        assert stat.S_IMODE(target.stat().st_mode) == 0o640


class TestDownloadMimeAgreement:
    async def test_the_saved_mime_is_sniffed_not_taken_from_the_header(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # The same PNG read inline reports image/png (magic bytes decide), so
        # saving it must not report application/octet-stream — one file, one
        # answer, whichever tool the caller reached for.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        target = tmp_path / "shot.png"
        with aioresponses() as mocked:
            mocked.get(
                DOWNLOAD_URL,
                body=png,
                headers={"Content-Type": "application/octet-stream"},
            )
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert result.mimetype == "image/png"

    async def test_the_header_still_answers_for_what_magic_cannot_see(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(
                DOWNLOAD_URL, body=b"id,name\n", headers={"Content-Type": "text/csv"}
            )
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(tmp_path / "rows.csv")
            )

        assert result.mimetype == "text/csv"


class TestPartPathBudget:
    """The `.part` name arithmetic, pinned without needing the platform.

    Every download test can only exercise the leg it runs on, and the Windows
    CI image has long paths enabled — so the MAX_PATH budget was green there
    for a reason that had nothing to do with the budget being right (it landed
    on exactly 260, one past what CreateFileW takes). These call `_part_path`
    directly with `os.name` forced, so both halves are checked everywhere.
    """

    def test_the_posix_budget_fits_name_max(self) -> None:
        part = client_module._part_path(Path("/srv/files") / ("x" * 300 + ".bin"))

        assert len(part.name.encode()) <= client_module._NAME_MAX

    def test_the_windows_budget_fits_max_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Built before os.name is forced: pathlib dispatches on it, and a
        # WindowsPath cannot be instantiated off Windows. Only the separator
        # count matters to the arithmetic, and that is the same either way.
        parent = Path("C:/" + "d" * 90)
        targets = [parent / ("x" * n + ".bin") for n in (4, 30, 200, 245)]
        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.name", "nt")

        for target in targets:
            part = client_module._part_path(target)
            # MAX_PATH counts the terminating NUL, so 259 is the longest path
            # CreateFileW accepts without the `\\?\` prefix.
            assert len(str(part)) < client_module._WINDOWS_MAX_PATH

    def test_a_parent_leaving_no_room_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The `or "download"` fallback knows nothing about the budget, so a
        # parent this long used to produce a 276-character `.part` path and an
        # ERROR_PATH_NOT_FOUND with nothing in it the caller could act on.
        target = Path("C:/" + "d" * 250) / "report.bin"
        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.name", "nt")

        with pytest.raises(WikiLocalFileError, match="too long for this system"):
            client_module._part_path(target)


class TestDownloadTargetErrors:
    async def test_a_directory_target_is_refused_before_any_transfer(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # Path.exists() is true for a directory, so this used to answer "pass
        # overwrite=true" — advice that then transferred the whole file and
        # died on a raw IsADirectoryError.
        target = tmp_path / "adir"
        target.mkdir()
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="is a directory"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )

        assert not [n for n in _dir_names(tmp_path) if n.endswith(".part")]

    async def test_a_directory_target_is_refused_with_overwrite_too(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "adir"
        target.mkdir()
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="is a directory"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target), overwrite=True
                )

    async def test_a_parent_that_is_a_file_is_a_wiki_error(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "afile"
        blocker.write_bytes(b"x")
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(
                WikiLocalFileError, match="a parent path component is a file"
            ):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(blocker / "child.bin")
                )

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "the target path alone runs past MAX_PATH, so this only passes on "
            "a Windows box with long paths enabled — where it proves nothing. "
            "TestPartPathBudget pins the Windows arithmetic instead."
        ),
    )
    async def test_a_very_long_target_name_still_works(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # The .part prefix used to be the whole target name plus 14 bytes,
        # which overflows NAME_MAX for a legal 245-character target.
        target = tmp_path / ("x" * 245 + ".bin")
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert target.read_bytes() == b"payload"
        assert result.size_bytes == 7

    async def test_a_failed_download_leaves_no_directories_behind(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # The .part file and the parent tree are created on the first chunk,
        # so a request that never delivers a body does not touch the disk.
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, status=404, body=b"GIF89a")
            with pytest.raises(AttachmentNotFound):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "a" / "b" / "c" / "r.pdf")
                )

        assert _dir_names(tmp_path) == []


class TestDownloadCommitFailures:
    async def test_a_full_disk_at_fsync_is_a_wiki_error(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # With delayed allocation ENOSPC surfaces at fsync, after every
        # write() succeeded. It must arrive wrapped in the WikiError
        # hierarchy, and the .part must not survive the failure.
        # Only the DATA fsync may satisfy this test: stubbing os.fsync
        # wholesale would also cover _fsync_directory, and the assertion would
        # then pass even if the data fsync were deleted outright — leaving the
        # durability guarantee unpinned. The .part fd is a regular file; the
        # directory fsync gets a directory fd, and is let through.
        synced: list[str] = []

        def full_disk(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                synced.append("dir")
                return
            synced.append("data")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.fsync", full_disk)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="No space left"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "big.bin")
                )

        assert synced == ["data"], "the refusal must come from the data fsync"
        assert _dir_names(tmp_path) == []

    @pytest.mark.skipif(
        os.name != "posix", reason="only POSIX can fsync a directory descriptor"
    )
    async def test_a_finished_download_flushes_the_directory_entry(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The only test that fails if _fsync_directory stops doing anything.
        # Every other assertion about it holds just as well when it never
        # runs, which is how a whole-function `# pragma: no cover` let the
        # body be replaced with `return` at a still-green 100%.
        synced: list[str] = []
        real_fsync = os.fsync

        def spy(fd: int) -> None:
            synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "data")
            real_fsync(fd)

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.fsync", spy)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(tmp_path / "durable.bin")
            )

        assert synced == ["data", "dir"], "the rename must be flushed too"

    async def test_a_name_taken_mid_transfer_is_refused_with_the_remedy(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The kernel's EEXIST at link time is the overwrite=false contract
        # holding under a race — but the caller must still get the usual
        # "already exists" WikiError, not a bare FileExistsError.
        def taken(src: object, dst: object, **kwargs: object) -> None:
            raise FileExistsError(errno.EEXIST, "File exists")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.link", taken)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="already exists"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "raced.bin")
                )

        assert _dir_names(tmp_path) == []

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(errno.EPERM, id="posix-eperm"),
            # The Windows half: CPython maps ERROR_NOT_SUPPORTED to ENODEV,
            # which the errno set used to omit — so the fallback written for
            # FAT/exFAT and SMB never engaged on the platform that has them,
            # and every overwrite=false download to such a volume failed
            # after the whole transfer had already run.
            pytest.param(errno.ENODEV, id="windows-enodev"),
            pytest.param(errno.ENOTSUP, id="notsup"),
        ],
    )
    async def test_a_filesystem_without_hardlinks_still_delivers(
        self,
        code: int,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # FAT/exFAT sticks and some network mounts cannot link(2) at all;
        # the fallback re-checks existence and renames instead of failing
        # the download after the whole transfer already ran.
        def no_links(src: object, dst: object, **kwargs: object) -> None:
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.link", no_links)
        target = tmp_path / "fat32.bin"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert target.read_bytes() == b"payload"
        assert result.size_bytes == 7
        assert _dir_names(tmp_path) == [target.name]

    async def test_the_fallback_still_refuses_an_existing_target(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No hardlinks AND the name got taken mid-transfer: the fallback's
        # existence re-check must refuse rather than clobber. The squatter is
        # planted by the link stub itself — the only deterministic way to make
        # the file appear after the probe and the open but before the commit.
        target = tmp_path / "squatted.bin"

        def no_links(src: object, dst: object, **kwargs: object) -> None:
            target.write_bytes(b"squatter")
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.link", no_links)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="already exists"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )

        assert target.read_bytes() == b"squatter"
        assert _dir_names(tmp_path) == [target.name]


class TestDownloadCancellation:
    async def test_a_cancel_mid_transfer_leaves_nothing_behind(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # The reason every filesystem step runs on one dedicated worker: a
        # cancelled await must not close a descriptor another thread is still
        # writing to, and the `.part` must not survive the cancellation.
        wrote = asyncio.Event()
        real_write_all = client_module._write_all

        def slow_write(fd: int, chunk: bytes) -> None:
            wrote.set()
            time.sleep(0.15)
            real_write_all(fd, chunk)

        with (
            mock.patch.object(client_module, "_write_all", slow_write),
            aioresponses() as mocked,
        ):
            mocked.get(DOWNLOAD_URL, body=b"x" * 4096)
            task = asyncio.create_task(
                wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "cancelled.bin")
                )
            )
            await wrote.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        await _eventually_empty(tmp_path)

    async def test_a_cancel_while_the_part_is_being_created_leaves_nothing(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # The other side of the same race: the cancel lands while the worker is
        # still inside _open_part, so the awaiting side never learns the pair
        # exists. The cleanup task is queued behind the open on the same
        # worker, which is what makes it see the finished state.
        opening = asyncio.Event()
        real_open_part = client_module._open_part

        def slow_open(target: Path, *, overwrite: bool) -> tuple[int, Path]:
            opening.set()
            time.sleep(0.15)
            return real_open_part(target, overwrite=overwrite)

        with (
            mock.patch.object(client_module, "_open_part", slow_open),
            aioresponses() as mocked,
        ):
            mocked.get(DOWNLOAD_URL, body=b"x" * 4096)
            task = asyncio.create_task(
                wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "cancelled.bin")
                )
            )
            await opening.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        await _eventually_empty(tmp_path)


class TestDownloadChunking:
    async def test_a_body_arriving_in_several_chunks_is_assembled(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # Everything about the streaming path only happens from the second
        # chunk onwards: reuse of the already-open descriptor, `head`
        # accumulation across the boundary, and `size` summation.
        target = tmp_path / "multi.png"
        # A PNG signature split so the magic bytes straddle two chunks.
        chunks = [b"\x89PNG\r", b"\n\x1a\n" + b"\x00" * 32, b"\xff" * 16]
        with (
            mock.patch.object(client_module, "DOWNLOAD_CHUNK_SIZE", 8),
            aioresponses() as mocked,
        ):
            mocked.get(DOWNLOAD_URL, body=b"".join(chunks))
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert target.read_bytes() == b"".join(chunks)
        assert result.size_bytes == len(b"".join(chunks))
        # head was assembled across the boundary, so the sniff still works
        assert result.mimetype == "image/png"

    async def test_a_write_failure_mid_transfer_leaves_no_part(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        real_write_all = client_module._write_all
        calls = {"n": 0}

        def fail_on_second(fd: int, chunk: bytes) -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            real_write_all(fd, chunk)

        with (
            mock.patch.object(client_module, "DOWNLOAD_CHUNK_SIZE", 8),
            mock.patch.object(client_module, "_write_all", fail_on_second),
            aioresponses() as mocked,
        ):
            mocked.get(DOWNLOAD_URL, body=b"x" * 64)
            with pytest.raises(WikiLocalFileError, match="Cannot write to"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "half.bin")
                )

        await _eventually_empty(tmp_path)


class TestDownloadPlacementFailures:
    async def test_a_link_failure_outside_the_fallback_set_is_re_raised(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # EIO is a real failure, not "this filesystem cannot hardlink". Widening
        # the errno set into a blanket `except OSError` would turn every link
        # failure into the racy exists()-then-replace path; this pins that it
        # does not.
        def broken_disk(src: object, dst: object, **kwargs: object) -> None:
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.link", broken_disk)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="Cannot finish writing"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "eio.bin")
                )

        assert _dir_names(tmp_path) == []

    async def test_a_cleanup_failure_after_the_link_still_delivers(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Past the link the file is in place and correct, so a `.part` that
        # will not go away is a stray file, not a failed download. It used to
        # be reported as "Cannot finish writing", which sent the caller to
        # re-fetch something already delivered — and their retry then found
        # their own new file and demanded overwrite=true.
        real_unlink = os.unlink

        def refuse(path: object, **kwargs: object) -> None:
            if str(path).endswith(".part"):
                raise OSError(errno.EACCES, "Permission denied")
            real_unlink(path)  # type: ignore[arg-type]

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.unlink", refuse)
        target = tmp_path / "delivered.bin"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with caplog.at_level(logging.WARNING, logger="mcp_wiki.wiki.custom.client"):
                result = await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )

        assert result.size_bytes == 7
        assert target.read_bytes() == b"payload"
        assert "was not removed" in caplog.text
        # Honest about the cost: the stray .part survives, and saying so is
        # the point — the alternative was calling a finished download failed.
        assert [n for n in _dir_names(tmp_path) if n.endswith(".part")]

    async def test_a_pool_that_refuses_cleanup_keeps_the_original_error(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # ThreadPoolExecutor.submit raises RuntimeError once the interpreter
        # is tearing down. Letting that escape would replace the transfer's
        # own exception — a task swallowing its own cancellation — so it is
        # logged instead. Deliberately not run inline: _discard_part would
        # close the descriptor from the event loop while the worker may still
        # be writing to it, which is the race this design exists to prevent.
        class RefusingPool(ThreadPoolExecutor):
            def submit(
                self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
            ) -> "Future[_T]":
                if getattr(fn, "__name__", "") == "discard_leftover":
                    raise RuntimeError("cannot schedule new futures")
                return super().submit(fn, *args, **kwargs)

        monkeypatch.setattr(
            "mcp_wiki.wiki.custom.client.ThreadPoolExecutor", RefusingPool
        )

        def full_disk(fd: int, data: object) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.write", full_disk)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with (
                caplog.at_level(logging.WARNING, logger="mcp_wiki.wiki.custom.client"),
                pytest.raises(WikiLocalFileError, match="Cannot write to"),
            ):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "gone.bin")
                )

        assert "could not schedule cleanup" in caplog.text
        assert [n for n in _dir_names(tmp_path) if n.endswith(".part")]

    @pytest.mark.skipif(
        os.name != "posix",
        reason="fchmod is only called on POSIX; Windows chmod toggles read-only",
    )
    async def test_a_chmod_failure_is_a_wiki_error_and_leaves_nothing(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # vfat/exFAT and many FUSE and SMB mounts answer EPERM to fchmod, and
        # `inherited` is only set on the overwrite path. It used to escape as a
        # bare PermissionError with the fd leaked and the .part orphaned.
        target = tmp_path / "pre.bin"
        target.write_bytes(b"old")

        def refuse(fd: int, mode: int) -> None:
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.fchmod", refuse)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"new")
            with pytest.raises(WikiLocalFileError, match="Cannot set permissions"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target), overwrite=True
                )

        assert _dir_names(tmp_path) == [target.name]
        assert target.read_bytes() == b"old"


class TestDownloadErrorArms:
    async def test_an_empty_attachment_still_produces_a_file(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # No chunk ever reaches the sink, so the `.part` has to be created
        # after the request instead of during it.
        target = tmp_path / "empty.bin"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"")
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert target.read_bytes() == b""
        assert result.size_bytes == 0

    @pytest.mark.skipif(
        os.name != "posix", reason="only POSIX can fsync a directory descriptor"
    )
    async def test_an_unflushable_directory_still_reports_success(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Past the rename the file is in place and correct; a directory fsync
        # that fails puts only the entry's durability in doubt, so it is
        # logged rather than raised — reporting failure would send the caller
        # to re-download something already complete.
        real_fsync = os.fsync

        def fail_on_dir(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "Input/output error")
            real_fsync(fd)

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.fsync", fail_on_dir)
        target = tmp_path / "kept.bin"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with caplog.at_level(logging.WARNING, logger="mcp_wiki.wiki.custom.client"):
                result = await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )

        assert target.read_bytes() == b"payload"
        assert result.size_bytes == 7
        # The warning is the whole point: without asserting it, deleting the
        # log line leaves this test just as green.
        assert "was not flushed" in caplog.text

    async def test_an_unstattable_target_defers_to_open_part(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The probe is advisory: an OSError it cannot interpret must not fail
        # the call, because _open_part produces the real diagnosis.
        real_stat = Path.stat
        target = tmp_path / "odd.bin"

        def flaky(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if self == target:
                raise OSError(errno.EIO, "Input/output error")
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", flaky)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            # The probe swallows it and defers; _open_part re-stats and is the
            # one that names the problem.
            with pytest.raises(WikiLocalFileError, match="Cannot inspect"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )

    async def test_an_uncreatable_part_is_a_wiki_error(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: object, **kwargs: object) -> int:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr("mcp_wiki.wiki.custom.client.os.open", refuse)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="Cannot write beside"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "ro.bin")
                )

    async def test_an_uncreatable_parent_directory_is_a_wiki_error(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_mkdir = Path.mkdir

        def refuse(
            self: Path,
            mode: int = 0o777,
            parents: bool = False,
            exist_ok: bool = False,
        ) -> None:
            if self != tmp_path:
                raise OSError(errno.EACCES, "Permission denied")
            real_mkdir(self, mode, parents, exist_ok)

        monkeypatch.setattr(Path, "mkdir", refuse)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="Cannot create the directory"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "sub" / "x.bin")
                )


class TestOpenPartRechecks:
    """`_open_part` re-checks what `_probe_target` already rejected.

    The probe is an early exit, not the authority: a target that appears
    between the probe and the open must still be caught, and these drive that
    window by neutralizing the probe.
    """

    async def test_a_target_that_appears_after_the_probe_is_refused(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "raced.bin"
        target.write_bytes(b"appeared after the probe")
        monkeypatch.setattr(
            "mcp_wiki.wiki.custom.client._probe_target", lambda *a, **k: None
        )
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="already exists"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )

        assert target.read_bytes() == b"appeared after the probe"

    async def test_a_directory_that_appears_after_the_probe_is_refused(
        self, wiki_client: WikiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "raced_dir"
        target.mkdir()
        monkeypatch.setattr(
            "mcp_wiki.wiki.custom.client._probe_target", lambda *a, **k: None
        )
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"payload")
            with pytest.raises(WikiLocalFileError, match="is a directory"):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target), overwrite=True
                )


class TestDownloadAttachmentToDisk:
    async def test_streams_the_body_to_the_target_file(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # Big enough to cross several stream chunks, and a missing parent
        # directory on purpose: the client must create it.
        blob = bytes(range(256)) * 1024
        target = tmp_path / "out" / "report.pdf"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=blob, content_type="application/pdf")
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )

        assert target.read_bytes() == blob
        assert result.page_id == 42
        assert result.file_id == 5
        assert result.path == str(target.resolve())
        assert result.size_bytes == len(blob)
        assert result.mimetype == "application/pdf"
        # The temp file was renamed, not left beside the result.
        assert _dir_names(target.parent) == [target.name]

    async def test_refuses_an_existing_target_without_overwrite(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "exists.bin"
        target.write_bytes(b"old")
        # No route is mocked: had the client touched the wire at all,
        # aioresponses would fail the request with a connection error, and
        # the assertion below would see the wrong exception type.
        with aioresponses(), pytest.raises(WikiLocalFileError, match="already exists"):
            await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target)
            )
        assert target.read_bytes() == b"old"

    async def test_overwrite_replaces_the_file(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "exists.bin"
        target.write_bytes(b"old")
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"new bytes")
            await wiki_client.page_download_attachment(
                42, file_id=5, save_to=str(target), overwrite=True
            )
        assert target.read_bytes() == b"new bytes"

    async def test_a_miss_leaves_nothing_behind(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "missing.gif"
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, status=404, body=b"GIF89a...")
            with pytest.raises(AttachmentNotFound):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target)
                )
        # Neither the target nor an orphaned .part file.
        assert _dir_names(tmp_path) == []

    async def test_a_failure_leaves_the_existing_file_intact(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # overwrite=true promises replacement on success, not destruction on
        # failure: the rename never happens, so the old bytes survive.
        target = tmp_path / "precious.bin"
        target.write_bytes(b"precious")
        with aioresponses() as mocked:
            mocked.get(
                DOWNLOAD_URL,
                status=500,
                payload={"error_code": "INTERNAL", "debug_message": "boom"},
            )
            with pytest.raises(WikiApiError):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(target), overwrite=True
                )
        assert target.read_bytes() == b"precious"
        assert _dir_names(tmp_path) == [target.name]

    async def test_tilde_expands_to_the_home_directory(
        self,
        wiki_client: WikiClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # HOME is what posixpath.expanduser reads; ntpath reads USERPROFILE and
        # ignores HOME entirely, so setting only the first passes on Linux and
        # macOS while the Windows CI job writes into the runner's real profile
        # directory and then fails the assert. Both, and the test is honest on
        # every leg of the matrix.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, body=b"x")
            result = await wiki_client.page_download_attachment(
                42, file_id=5, save_to="~/tilde.bin"
            )
        assert (tmp_path / "tilde.bin").read_bytes() == b"x"
        assert result.path == str((tmp_path / "tilde.bin").resolve())

    async def test_a_disk_download_is_not_retried(
        self, wiki_client: WikiClient, tmp_path: Path
    ) -> None:
        # Same argument as the inline download, plus one more: a retried
        # sink would append the retry's chunks after the first attempt's.
        capture = RequestCapture(status=503)
        with aioresponses() as mocked:
            mocked.get(DOWNLOAD_URL, callback=capture.callback, repeat=True)
            with pytest.raises(WikiApiError):
                await wiki_client.page_download_attachment(
                    42, file_id=5, save_to=str(tmp_path / "f.bin")
                )
        capture.assert_request_count(1)

    async def test_a_sink_on_a_retryable_call_is_a_programming_error(
        self, wiki_client: WikiClient
    ) -> None:
        # The guard behind the tests above: _request_raw itself refuses a
        # sink on anything retryable, so no future caller can pair them.
        async def sink(_: bytes) -> None:  # pragma: no cover - never invoked
            raise AssertionError("must not be called")

        with pytest.raises(ValueError, match="retryable"):
            await wiki_client._request_raw("GET", "v1/anything", body_sink=sink)


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
