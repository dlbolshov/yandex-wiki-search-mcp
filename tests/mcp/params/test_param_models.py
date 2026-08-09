"""Argument models reject what the Wiki API would reject anyway.

These are the shapes an LLM fills in, so blank strings and half-filled
objects are ordinary traffic, not malformed input. Catching them here turns
a confusing API error into a message naming the field.
"""

import pytest
from pydantic import ValidationError

from mcp_wiki.mcp.params import GridCellPatch, GridColumnSpec, GridSortEntry
from mcp_wiki.wiki.proto.types.pages import GridUpdateRequest


class TestGridCellPatch:
    def test_a_column_slug_alone_is_enough(self) -> None:
        patch = GridCellPatch(row_id=1, value="x", column_slug="status")

        assert patch.column_id is None
        assert patch.to_payload() == {
            "row_id": 1,
            "value": "x",
            "column_slug": "status",
        }

    def test_an_explicit_null_column_reference_stays_null(self) -> None:
        # An LLM filling the schema often sends both keys, one of them null.
        patch = GridCellPatch(row_id=1, value="x", column_id=None, column_slug="status")

        assert patch.column_id is None
        assert "column_id" not in patch.to_payload()

    def test_a_string_row_id_is_trimmed(self) -> None:
        assert GridCellPatch(row_id="  row-1  ", value=None, column_id="c1").row_id == (
            "row-1"
        )

    def test_a_blank_row_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            GridCellPatch(row_id="   ", value="x", column_id="c1")

    def test_a_blank_column_reference_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            GridCellPatch(row_id=1, value="x", column_slug="   ")

    @pytest.mark.parametrize(
        "columns",
        [
            {},
            {"column_id": "c1", "column_slug": "status"},
        ],
    )
    def test_exactly_one_column_reference_is_required(
        self, columns: dict[str, str]
    ) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            GridCellPatch(row_id=1, value="x", **columns)


class TestGridColumnSpec:
    def test_blank_text_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            GridColumnSpec(title="  ", slug="c", type="string", required=False)

    def test_extra_keys_pass_through_to_the_api(self) -> None:
        spec = GridColumnSpec(
            title="Status",
            slug="status",
            type="select",
            required=True,
            **{"select_options": ["a", "b"]},
        )

        assert spec.to_payload()["select_options"] == ["a", "b"]


class TestGridSortEntry:
    def test_a_blank_column_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            GridSortEntry(column="   ")

    def test_the_wire_form_is_a_single_pair(self) -> None:
        assert GridSortEntry(column="status", direction="desc").to_mapping() == {
            "status": "desc"
        }


class TestGridUpdateRequestSort:
    """`default_sort` is a list of one-key mappings — the API's own shape."""

    def test_an_entry_with_two_columns_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one column slug"):
            GridUpdateRequest(revision="r1", default_sort=[{"a": "asc", "b": "desc"}])

    def test_an_empty_entry_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one column slug"):
            GridUpdateRequest(revision="r1", default_sort=[{}])

    def test_a_blank_column_slug_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            GridUpdateRequest(revision="r1", default_sort=[{"   ": "asc"}])
