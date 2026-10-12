import io
import json

import pytest
from pydantic import BaseModel, ValidationError

from mcp_wiki.wiki.custom.errors import (
    GridConflict,
    WikiApiError,
    WikiLocalFileError,
    WikiRateLimited,
    WikiResponseError,
    build_api_error,
)


class TestBuildApiError:
    def test_parses_error_envelope(self) -> None:
        error = build_api_error(
            400,
            b'{"error_code": "ANCHOR_NOT_FOUND", "debug_message": "Anchor not found"}',
        )
        assert isinstance(error, WikiApiError)
        assert error.status == 400
        assert error.error_code == "ANCHOR_NOT_FOUND"
        assert error.debug_message == "Anchor not found"

    def test_non_json_payload(self) -> None:
        error = build_api_error(502, b"<html>Bad gateway</html>")
        assert error.status == 502
        assert error.error_code is None

    def test_non_utf8_payload(self) -> None:
        error = build_api_error(502, b"\x80\xff bad bytes")
        assert error.status == 502
        assert error.error_code is None
        assert error.message is None

    def test_empty_payload(self) -> None:
        error = build_api_error(500, b"")
        assert error.status == 500
        assert str(error) == "Wiki API request failed with status 500"


class TestGridConflict:
    """409 CONFLICTING_OPERATION is a per-grid lock, not a broken request."""

    PAYLOAD = (
        b'{"error_code": "CONFLICTING_OPERATION",'
        b' "debug_message": "Conflicting operation in progress"}'
    )

    def test_conflicting_operation_gets_its_own_type(self) -> None:
        error = build_api_error(409, self.PAYLOAD)

        assert isinstance(error, GridConflict)
        assert isinstance(error, WikiApiError)
        assert error.status == 409
        assert error.error_code == "CONFLICTING_OPERATION"

    def test_message_keeps_the_diagnosis_and_adds_the_recovery(self) -> None:
        # The bare API text says only that something conflicts, which leaves an
        # agent guessing between retrying and giving up.
        message = str(build_api_error(409, self.PAYLOAD))

        assert "Conflicting operation in progress" in message
        assert "not applied" in message
        assert "re-read the grid" in message
        assert "one at a time" in message

    def test_a_429_is_a_rate_limit_with_its_recovery(self) -> None:
        # The API sends no Retry-After and no JSON envelope with it.
        error = build_api_error(429, b"")

        assert isinstance(error, WikiRateLimited)
        assert isinstance(error, WikiApiError)
        assert error.status == 429
        assert str(error) == (
            f"Wiki API request failed with status 429. {WikiRateLimited.RECOVERY}"
        )

    def test_other_409s_stay_plain(self) -> None:
        error = build_api_error(409, b'{"error_code": "REVISION_MISMATCH"}')

        assert not isinstance(error, GridConflict)
        assert isinstance(error, WikiApiError)

    def test_409_without_an_envelope_stays_plain(self) -> None:
        error = build_api_error(409, b"")

        assert not isinstance(error, GridConflict)


class _TwoFields(BaseModel):
    id: int
    title: str


class TestWikiResponseError:
    def test_a_read_says_what_failed_and_nothing_about_retrying(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _TwoFields.model_validate_json(b"<html>Moved</html>")

        error = WikiResponseError("GET", "v1/pages/1", caught.value)

        assert str(error) == (
            "GET v1/pages/1 succeeded, but its response could not be read: "
            "_TwoFields Invalid JSON: expected value at line 1 column 1."
        )

    def test_a_write_says_it_may_have_been_applied(self) -> None:
        error = WikiResponseError("POST", "v1/pages", ValueError("bad"))

        assert str(error).endswith(
            "The change may have been applied: read the current state before retrying."
        )

    def test_a_post_that_only_reads_says_so(self) -> None:
        error = WikiResponseError("POST", "v1/search", ValueError("x"), writes=False)

        assert "may have been applied" not in str(error)

    def test_names_the_first_field_and_counts_the_rest(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _TwoFields.model_validate({"title": None})

        error = WikiResponseError("GET", "v1/x", caught.value)

        assert "_TwoFields id: Field required (and 1 more)." in str(error)

    def test_never_echoes_the_body(self) -> None:
        # The body is the caller's data and may be large; the field and the
        # complaint are what a reader needs.
        with pytest.raises(ValidationError) as caught:
            _TwoFields.model_validate({"id": "not-a-number-secret", "title": "t"})

        assert "secret" not in str(WikiResponseError("GET", "v1/x", caught.value))

    def test_a_json_error_keeps_its_own_text(self) -> None:
        with pytest.raises(json.JSONDecodeError) as caught:
            json.loads(b"<html/>")

        error = WikiResponseError("DELETE", "v1/grids/g", caught.value)

        assert "Expecting value: line 1 column 1 (char 0)" in str(error)


class TestWikiLocalFileError:
    def test_carries_the_errno_text(self) -> None:
        error = WikiLocalFileError(
            "Cannot read /x", cause=PermissionError(13, "Permission denied")
        )

        assert str(error) == "Cannot read /x (Permission denied)"

    def test_an_oserror_without_one_is_not_printed_as_none(self) -> None:
        error = WikiLocalFileError(
            "Cannot read /x", cause=io.UnsupportedOperation("fileno")
        )

        assert str(error) == "Cannot read /x (fileno)"
