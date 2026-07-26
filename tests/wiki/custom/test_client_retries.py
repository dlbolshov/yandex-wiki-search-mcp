"""Retry behavior of WikiClient._request."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from aiohttp import ServerDisconnectedError, ServerTimeoutError
from aioresponses import aioresponses

from mcp_wiki.wiki.custom.client import (
    RETRY_BASE_DELAY,
    WikiClient,
    _backoff_delay,
    _retry_delay,
)
from mcp_wiki.wiki.custom.errors import PageNotFound, WikiApiError

PAGE_URL = "https://api.wiki.yandex.net/v1/pages/1"
SEARCH_URL = "https://api.wiki.yandex.net/v1/search"
CREATE_URL = "https://api.wiki.yandex.net/v1/pages"
PAGE_PAYLOAD = {"id": 1, "slug": "a/b", "title": "T"}


@pytest.fixture
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Record backoff sleeps instead of performing them."""
    recorded: list[float] = []

    async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    yield recorded


def request_count(mocked: aioresponses) -> int:
    requests: dict[Any, list[Any]] = mocked.requests or {}
    return sum(len(calls) for calls in requests.values())


class TestRetries:
    async def test_get_retried_on_503(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, status=503)
            mocked.get(PAGE_URL, payload=PAGE_PAYLOAD)

            page = await wiki_client_retrying.page_get(1)

            assert request_count(mocked) == 2
        assert page.id == 1
        assert len(sleep_calls) == 1
        assert 0 < sleep_calls[0] <= RETRY_BASE_DELAY

    async def test_get_gives_up_after_max_retries(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            for _ in range(3):
                mocked.get(PAGE_URL, status=502)

            with pytest.raises(WikiApiError) as exc_info:
                await wiki_client_retrying.page_get(1)

            assert request_count(mocked) == 3
        assert exc_info.value.status == 502
        assert len(sleep_calls) == 2

    async def test_connection_error_retried(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, exception=ServerDisconnectedError())
            mocked.get(PAGE_URL, payload=PAGE_PAYLOAD)

            page = await wiki_client_retrying.page_get(1)

        assert page.id == 1
        assert len(sleep_calls) == 1

    async def test_connection_error_reraised_after_max_retries(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            for _ in range(3):
                mocked.get(PAGE_URL, exception=ServerDisconnectedError())

            with pytest.raises(ServerDisconnectedError):
                await wiki_client_retrying.page_get(1)

        assert len(sleep_calls) == 2

    async def test_server_timeout_error_is_not_retried(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        # ServerTimeoutError subclasses ClientConnectionError, but it is a timeout.
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, exception=ServerTimeoutError())

            with pytest.raises(ServerTimeoutError):
                await wiki_client_retrying.page_get(1)

        assert sleep_calls == []

    async def test_search_is_retried_despite_being_a_post(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(SEARCH_URL, status=504)
            mocked.post(SEARCH_URL, payload={"results": [], "has_next": False})

            result = await wiki_client_retrying.page_search("q")

            assert request_count(mocked) == 2
        assert result.results == []
        assert len(sleep_calls) == 1

    async def test_write_request_is_not_retried(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.post(CREATE_URL, status=503)

            with pytest.raises(WikiApiError) as exc_info:
                await wiki_client_retrying.page_create(
                    slug="a/b", title="T", content="c"
                )

            assert request_count(mocked) == 1
        assert exc_info.value.status == 503
        assert sleep_calls == []

    async def test_upload_part_is_retried(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        url = "https://api.wiki.yandex.net/v1/upload_sessions/s1/upload_part?part_number=1"
        with aioresponses() as mocked:
            mocked.put(url, status=502)
            mocked.put(url, status=204)

            await wiki_client_retrying._upload_part(
                "s1", part_number=1, data=b"payload"
            )

            assert request_count(mocked) == 2
        assert len(sleep_calls) == 1

    async def test_retries_disabled_by_max_retries_zero(
        self,
        wiki_client: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, status=503)

            with pytest.raises(WikiApiError):
                await wiki_client.page_get(1)

            assert request_count(mocked) == 1
        assert sleep_calls == []

    async def test_404_is_not_retried(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, status=404)

            with pytest.raises(PageNotFound):
                await wiki_client_retrying.page_get(1)

            assert request_count(mocked) == 1
        assert sleep_calls == []


class TestRetryAfter:
    async def test_retry_after_seconds_is_honored(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, status=429, headers={"Retry-After": "0.5"})
            mocked.get(PAGE_URL, payload=PAGE_PAYLOAD)

            page = await wiki_client_retrying.page_get(1)

        assert page.id == 1
        assert sleep_calls == [0.5]

    async def test_retry_after_above_cap_fails_fast(
        self,
        wiki_client_retrying: WikiClient,
        sleep_calls: list[float],
    ) -> None:
        with aioresponses() as mocked:
            mocked.get(PAGE_URL, status=429, headers={"Retry-After": "60"})

            with pytest.raises(WikiApiError) as exc_info:
                await wiki_client_retrying.page_get(1)

            assert request_count(mocked) == 1
        assert exc_info.value.status == 429
        assert sleep_calls == []

    def test_retry_after_nan_fails_fast(self) -> None:
        # float("nan") parses fine but fails every comparison; without the
        # "not <=" guard it would leak into asyncio.sleep(nan).
        assert _retry_delay(1, "nan") is None

    def test_retry_after_http_date_falls_back_to_backoff(self) -> None:
        delay = _retry_delay(1, "Wed, 21 Oct 2015 07:28:00 GMT")

        assert delay is not None
        assert 0 < delay <= RETRY_BASE_DELAY

    def test_backoff_grows_with_attempt(self) -> None:
        assert RETRY_BASE_DELAY / 2 <= _backoff_delay(1) <= RETRY_BASE_DELAY
        assert RETRY_BASE_DELAY <= _backoff_delay(2) <= RETRY_BASE_DELAY * 2
