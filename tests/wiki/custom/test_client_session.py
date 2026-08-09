"""Session lifecycle, request tracing, and the upload paths beyond the happy one."""

import logging
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import WikiClient, _build_trace_config
from mcp_wiki.wiki.custom.errors import WikiApiError
from tests.aioresponses_utils import RequestCapture

UPLOAD_PART_URL = re.compile(
    r"https://api\.wiki\.yandex\.net/v1/upload_sessions/session-1/upload_part.*"
)
APPEND_URL = "https://api.wiki.yandex.net/v1/pages/10/append-content"


class TestSessionLifecycle:
    async def test_using_the_client_before_prepare_says_so(self) -> None:
        client = WikiClient(token="t", org_id="o")

        with pytest.raises(RuntimeError, match="not prepared"):
            _ = client._http

    async def test_prepare_is_idempotent(self) -> None:
        client = WikiClient(token="t", org_id="o")
        try:
            await client.prepare()
            first = client._session
            await client.prepare()

            assert client._session is first, "a second prepare must not leak a session"
        finally:
            await client.close()

    async def test_close_without_prepare_is_a_no_op(self) -> None:
        client = WikiClient(token="t", org_id="o")

        await client.close()

        assert client._session is None

    async def test_prepare_reopens_a_closed_session(self) -> None:
        client = WikiClient(token="t", org_id="o")
        await client.prepare()
        closed = client._session
        await client.close()

        await client.prepare()
        try:
            assert client._session is not None
            assert client._session is not closed
        finally:
            await client.close()


class TestRequestTracing:
    """The DEBUG trace logs method, path, outcome and duration — never payloads.

    aioresponses answers above the transport, so aiohttp's trace hooks never
    fire under the rest of the suite; the callbacks are driven directly here.
    """

    @staticmethod
    async def _fire(callback: Any, ctx: Any, **params: Any) -> None:
        """Drive one trace callback; aiohttp would pass the session and params."""
        await callback(None, ctx, SimpleNamespace(**params))

    async def test_a_finished_request_logs_status_and_duration(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        trace = _build_trace_config()
        ctx = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger="mcp_wiki.wiki.custom.client"):
            await self._fire(trace.on_request_start[0], ctx)
            await self._fire(
                trace.on_request_end[0],
                ctx,
                method="GET",
                url=SimpleNamespace(path="/v1/pages/42"),
                response=SimpleNamespace(status=200),
            )

        message = caplog.records[-1].getMessage()
        assert "GET /v1/pages/42 -> 200" in message
        assert "ms)" in message

    async def test_a_failed_request_logs_the_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        trace = _build_trace_config()
        ctx = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger="mcp_wiki.wiki.custom.client"):
            await self._fire(trace.on_request_start[0], ctx)
            await self._fire(
                trace.on_request_exception[0],
                ctx,
                method="POST",
                url=SimpleNamespace(path="/v1/pages"),
                exception=ConnectionResetError("boom"),
            )

        message = caplog.records[-1].getMessage()
        assert "POST /v1/pages" in message
        assert "ConnectionResetError" in message


class TestUploadAttachment:
    async def test_a_missing_file_is_reported_before_any_request(
        self, wiki_client: WikiClient
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(
                "https://api.wiki.yandex.net/v1/upload_sessions",
                exception=AssertionError("no request may be made"),
            )
            with pytest.raises(FileNotFoundError, match=r"nope\.txt"):
                await wiki_client.page_upload_attachment(10, file_path="/nope.txt")

    async def test_append_markup_writes_a_file_macro_to_the_page(
        self, wiki_client: WikiClient
    ) -> None:
        append_capture = RequestCapture(payload={"id": 10})

        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "spec.pdf"
            file_path.write_bytes(b"%PDF-1.4")

            with aioresponses() as mocked:
                mocked.post(
                    "https://api.wiki.yandex.net/v1/upload_sessions",
                    payload={"session_id": "session-1"},
                )
                mocked.put(UPLOAD_PART_URL, payload={})
                mocked.post(
                    "https://api.wiki.yandex.net/v1/upload_sessions/session-1/finish",
                    payload={},
                )
                mocked.post(
                    "https://api.wiki.yandex.net/v1/pages/10/attachments",
                    payload={
                        "results": [
                            {
                                "id": 1,
                                "name": "spec.pdf",
                                "download_url": "https://files.test/spec.pdf",
                            }
                        ]
                    },
                )
                mocked.post(APPEND_URL, callback=append_capture.callback)

                result = await wiki_client.page_upload_attachment(
                    10, file_path=str(file_path), append_markup=True
                )

        assert result.appended_markup is True
        assert result.appended_content == (
            '{% file src="https://files.test/spec.pdf" name="spec.pdf" %}'
        )
        append_capture.assert_called_once()
        assert (
            append_capture.last_request.get_json_body()["content"]
            == result.appended_content
        )


class TestAnchorFallback:
    async def test_an_anchor_absent_from_the_source_reraises(
        self, wiki_client: WikiClient
    ) -> None:
        # The fallback rewrites the page only when it can find the anchor in
        # the markup. When it cannot, the caller must see the API's own
        # error rather than a silent no-op.
        with aioresponses() as mocked:
            mocked.post(
                APPEND_URL,
                status=400,
                payload={"error_code": "ANCHOR_NOT_FOUND"},
            )
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/10.*"),
                payload={"id": 10, "content": "# Title\n\nno anchor here\n"},
            )

            with pytest.raises(WikiApiError, match="ANCHOR_NOT_FOUND"):
                await wiki_client.page_append_content(
                    10, content="x", anchor="#missing"
                )

    async def test_any_other_error_is_not_swallowed_by_the_fallback(
        self, wiki_client: WikiClient
    ) -> None:
        # The rewrite is for one specific refusal. A permission error or a
        # bad payload must surface as itself, not send us reading the page.
        with aioresponses() as mocked:
            mocked.post(APPEND_URL, status=403, payload={"error_code": "ACCESS_DENIED"})

            with pytest.raises(WikiApiError, match="ACCESS_DENIED"):
                await wiki_client.page_append_content(
                    10, content="x", anchor="#somewhere"
                )

    async def test_a_page_without_content_reraises(
        self, wiki_client: WikiClient
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(
                APPEND_URL,
                status=400,
                payload={"error_code": "ANCHOR_NOT_FOUND"},
            )
            mocked.get(
                re.compile(r"https://api\.wiki\.yandex\.net/v1/pages/10.*"),
                payload={"id": 10},
            )

            with pytest.raises(WikiApiError, match="ANCHOR_NOT_FOUND"):
                await wiki_client.page_append_content(
                    10, content="x", anchor="#missing"
                )
