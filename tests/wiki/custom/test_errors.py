from mcp_wiki.wiki.custom.errors import WikiApiError, build_api_error


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
