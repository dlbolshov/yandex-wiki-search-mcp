from mcp_wiki.wiki.custom.errors import (
    GridConflict,
    WikiApiError,
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

    def test_other_409s_stay_plain(self) -> None:
        error = build_api_error(409, b'{"error_code": "REVISION_MISMATCH"}')

        assert not isinstance(error, GridConflict)
        assert isinstance(error, WikiApiError)

    def test_409_without_an_envelope_stays_plain(self) -> None:
        error = build_api_error(409, b"")

        assert not isinstance(error, GridConflict)
