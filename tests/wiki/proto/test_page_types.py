"""Token-economy behavior of the wire models (v0.8.0).

Fixed-shape models drop unknown keys and omit None from dumps; dynamic models
(grid values) keep unknown keys. Payload shapes mirror the live API as probed
by scripts/contract_sweep.py on 2026-08-02 — see docs/api-notes.md.
"""

from mcp_wiki.wiki.proto.types.pages import (
    DescendantItem,
    DescendantsResponse,
    GridCellsResponse,
    GridMutationResponse,
    PageComment,
    RecoverPageResponse,
    SearchResponse,
    WikiAttachment,
    WikiGrid,
    WikiGridRow,
    WikiGridStructure,
    WikiGridSummary,
    WikiPage,
)

LIVE_USER = {
    "id": 80616691,
    "identity": {"uid": "1130000067296925", "cloud_uid": "aje8rk0gjh0qq7q7mmt4"},
    "username": "david",
    "display_name": "Давид Большов",
    "is_dismissed": False,
    "affiliation": "",
}


class TestFixedShapeModels:
    def test_unknown_keys_are_dropped(self) -> None:
        page = WikiPage.model_validate(
            {"id": 1, "slug": "users/x", "brand_new_api_key": "surprise"}
        )
        assert page.model_extra in (None, {})
        assert "brand_new_api_key" not in page.model_dump()

    def test_none_values_omitted_from_dump(self) -> None:
        page = WikiPage.model_validate({"id": 1, "slug": "users/x"})
        dumped = page.model_dump()
        assert dumped == {"id": 1, "slug": "users/x"}

    def test_none_values_omitted_in_nested_models(self) -> None:
        response = DescendantsResponse.model_validate(
            {"results": [{"id": 1, "slug": "a"}, {"id": 2, "slug": "b"}]}
        )
        dumped = response.model_dump(mode="json", by_alias=True)
        assert dumped == {"results": [{"id": 1, "slug": "a"}, {"id": 2, "slug": "b"}]}

    def test_explicit_values_survive(self) -> None:
        item = DescendantItem(id=5, slug="users/x/child")
        assert item.model_dump() == {"id": 5, "slug": "users/x/child"}

    def test_search_response_dump_has_no_null_cursors(self) -> None:
        response = SearchResponse.model_validate(
            {
                "results": [{"slug": "a", "title": "A", "content": "snippet"}],
                "next_cursor": None,
                "prev_cursor": None,
            }
        )
        assert response.model_dump(mode="json") == {
            "results": [{"slug": "a", "title": "A", "content": "snippet"}]
        }


class TestDeclaredLiveExtras:
    def test_page_access_fields(self) -> None:
        page = WikiPage.model_validate(
            {
                "id": 1,
                "access_policy": {"access_type": "inherited"},
                "access_lists": {"direct": []},
                "owner": {"user": LIVE_USER},
            }
        )
        assert page.access_policy == {"access_type": "inherited"}
        assert page.access_lists == {"direct": []}
        assert page.owner is not None

    def test_owner_user_is_trimmed_like_every_other_user_reference(self) -> None:
        page = WikiPage.model_validate(
            {"id": 1, "owner": {"user": LIVE_USER, "group": None}}
        )

        assert page.owner is not None
        assert page.owner.user is not None
        assert page.owner.user.username == "david"
        owner = page.model_dump()["owner"]
        assert "identity" not in owner["user"]
        assert "is_dismissed" not in owner["user"]
        assert "affiliation" not in owner["user"]
        assert "group" not in owner

    def test_comment_live_shape(self) -> None:
        comment = PageComment.model_validate(
            {
                "id": 1026442,
                "body": "sweep reply",
                "inline_text": None,
                "parent_id": 1026439,
                "author": LIVE_USER,
                "thread_id": None,
                "created_at": "2026-08-02T20:09:05.706Z",
                "is_deleted": False,
                "resolve_status": "unresolved",
                "reactions": [],
                "thread_info": None,
            }
        )
        assert comment.author is not None
        assert comment.author.username == "david"
        assert comment.is_deleted is False
        dumped = comment.model_dump()
        assert "user" not in dumped
        assert "identity" not in dumped["author"]
        assert dumped["author"]["display_name"] == "Давид Большов"

    def test_attachment_live_shape(self) -> None:
        attachment = WikiAttachment.model_validate(
            {
                "id": 24964111,
                "name": "sweep.txt",
                "is_downloadable": True,
                "download_url": "/users/x/.files/sweep.txt",
                "user": LIVE_USER,
                "has_preview": False,
                "check_status": "ready",
            }
        )
        assert attachment.is_downloadable is True
        assert attachment.user is not None
        assert attachment.user.id == LIVE_USER["id"]
        assert "identity" not in attachment.model_dump()["user"]

    def test_recover_response_live_shape(self) -> None:
        response = RecoverPageResponse.model_validate(
            {"id": 42, "slug": "users/x/p", "pages_count": 1}
        )
        assert response.slug == "users/x/p"
        assert response.pages_count == 1

    def test_cell_updates_answer_with_cells_and_nothing_else(self) -> None:
        response = GridCellsResponse.model_validate(
            {"revision": "5", "cells": [{"row_id": 1, "value": 42}]}
        )

        assert response.cells == [{"row_id": 1, "value": 42}]
        # No empty `results` alongside them: an agent checking that key to
        # confirm the mutation landed would read [] as "nothing changed".
        assert response.model_dump() == {
            "revision": "5",
            "cells": [{"row_id": 1, "value": 42}],
        }

    def test_row_mutations_answer_with_results_and_no_cells_key(self) -> None:
        response = GridMutationResponse.model_validate(
            {"revision": "5", "results": [{"id": 1, "row": [1, 2]}]}
        )

        assert "cells" not in response.model_dump()


class TestSchemaTitles:
    def test_no_autogenerated_titles_in_schema(self) -> None:
        schema = WikiPage.model_json_schema()
        assert "title" not in schema
        for name, prop in schema["properties"].items():
            assert "title" not in prop, name

    def test_no_titles_in_nested_defs(self) -> None:
        schema = PageComment.model_json_schema()
        for def_name, def_schema in schema.get("$defs", {}).items():
            assert "title" not in def_schema, def_name
            for name, prop in def_schema.get("properties", {}).items():
                assert "title" not in prop, f"{def_name}.{name}"


class TestDynamicModels:
    def test_grid_keeps_unknown_keys(self) -> None:
        grid = WikiGrid.model_validate(
            {"id": "g1", "title": "t", "future_grid_key": {"nested": 1}}
        )
        assert grid.model_extra == {"future_grid_key": {"nested": 1}}
        assert grid.model_dump()["future_grid_key"] == {"nested": 1}

    def test_grid_row_keeps_unknown_keys_and_drops_none(self) -> None:
        row = WikiGridRow.model_validate({"id": 7, "row": [1, None], "custom": "x"})
        dumped = row.model_dump()
        assert dumped["custom"] == "x"
        assert "pinned" not in dumped
        assert dumped["row"] == [1, None]

    def test_declared_none_is_dropped_but_user_null_survives(self) -> None:
        # "this column is empty" must stay distinguishable from "no such
        # column"; a declared field that the API did not send is just noise.
        row = WikiGridRow.model_validate(
            {"id": 7, "row": [1, None], "user_col": None, "pinned": None}
        )
        dumped = row.model_dump()
        assert dumped["user_col"] is None
        assert "pinned" not in dumped
        assert dumped["row"] == [1, None]

    def test_grid_structure_keeps_unknown_blocks(self) -> None:
        structure = WikiGridStructure.model_validate(
            {"columns": [], "future_layout_block": {"row_height": 2}}
        )
        assert structure.model_dump()["future_layout_block"] == {"row_height": 2}

    def test_grid_summary_stays_strict(self) -> None:
        summary = WikiGridSummary.model_validate({"id": "g1", "brand_new_key": 1})
        assert "brand_new_key" not in summary.model_dump()
