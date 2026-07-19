from mcp_wiki.wiki.custom.anchors import append_content_to_anchor_source


class TestAppendContentToAnchorSource:
    def test_heading_anchor_mid_document(self) -> None:
        result = append_content_to_anchor_source(
            "# Root\n\n## Section {#release-notes}\n\nBody",
            appended_content="\n\nAppended.",
            anchor="#release-notes",
        )
        assert result == "# Root\n\n## Section {#release-notes}\n\nAppended.\n\nBody"

    def test_heading_anchor_at_end_of_document(self) -> None:
        result = append_content_to_anchor_source(
            "# Root\n\n## Section {#release-notes}",
            appended_content="\nAppended.",
            anchor="#release-notes",
        )
        assert result == "# Root\n\n## Section {#release-notes}\nAppended."

    def test_inline_anchor_link(self) -> None:
        result = append_content_to_anchor_source(
            "Intro\n#[Release notes](release-notes)\nBody",
            appended_content="Appended.",
            anchor="release-notes",
        )
        assert result == "Intro\n#[Release notes](release-notes)\nAppended.\nBody"

    def test_anchor_macro(self) -> None:
        result = append_content_to_anchor_source(
            'Intro\n{{anchor href="release-notes"}}\nBody',
            appended_content="Appended.",
            anchor="#release-notes",
        )
        assert result == 'Intro\n{{anchor href="release-notes"}}\nAppended.\nBody'

    def test_anchor_without_trailing_newline_separator(self) -> None:
        result = append_content_to_anchor_source(
            "## Section {#notes}",
            appended_content="\nAppended.",
            anchor="notes",
        )
        assert result == "## Section {#notes}\nAppended."

    def test_missing_anchor_returns_none(self) -> None:
        result = append_content_to_anchor_source(
            "# Root\n\nNo anchors here.",
            appended_content="Appended.",
            anchor="#release-notes",
        )
        assert result is None

    def test_anchor_id_is_escaped_in_pattern(self) -> None:
        result = append_content_to_anchor_source(
            "## Section {#notes.v2}\n\nBody",
            appended_content="\n\nAppended.",
            anchor="#notes.v2",
        )
        assert result == "## Section {#notes.v2}\n\nAppended.\n\nBody"
        assert (
            append_content_to_anchor_source(
                "## Section {#notesXv2}\n\nBody",
                appended_content="Appended.",
                anchor="#notes.v2",
            )
            is None
        )
