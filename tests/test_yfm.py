import pytest

from mcp_wiki.yfm import MAX_WARNINGS, YFM_CHEATSHEET, validate_yfm

FENCE = "```"

BATTERY = "\n\n".join(
    [
        "# YFM battery",
        '{% note info "Заметка" %}\n\nТекст заметки.\n\n{% endnote %}',
        '{% cut "Раскрыть" %}\n\nСкрытый текст.\n\n{% endcut %}',
        "{% list tabs %}\n\n- Таб 1\n\n  Контент таба 1.\n\n"
        "- Таб 2\n\n  Контент таба 2.\n\n{% endlist %}",
        "#|\n|| Ячейка 1 | Ячейка 2 ||\n|| Ячейка 3 | Ячейка 4 ||\n|#",
        "| A | B |\n|---|---|\n| 1 | 2 |",
        "- [ ] не сделано\n- [x] сделано",
        f"{FENCE}\n{{% note %}} внутри код-фенса — не директива\n{FENCE}",
    ]
)


class TestCleanContent:
    def test_empty(self) -> None:
        assert validate_yfm("") == []

    def test_plain_markdown(self) -> None:
        content = "# Title\n\nSome **bold** text with a [link](https://ya.ru).\n"
        assert validate_yfm(content) == []

    def test_live_smoke_battery_is_clean(self) -> None:
        assert validate_yfm(BATTERY) == []

    def test_pipe_tables_and_task_lists_not_flagged(self) -> None:
        content = "| A | B |\n|---|---|\n| 1 | 2 |\n\n- [ ] todo\n- [x] done"
        assert validate_yfm(content) == []

    def test_directive_inside_code_fence_ignored(self) -> None:
        content = f"{FENCE}\n{{% note %}}\n<details>\n> [!NOTE]\n{FENCE}"
        assert validate_yfm(content) == []

    def test_directive_inside_tilde_fence_ignored(self) -> None:
        content = "~~~\n{% cut %}\n~~~"
        assert validate_yfm(content) == []

    def test_longer_closing_fence_accepted(self) -> None:
        content = f"{FENCE}\n{{% note %}}\n{FENCE}`"
        assert validate_yfm(content) == []

    def test_html_inside_code_span_ignored(self) -> None:
        assert validate_yfm("Use `<br>` for line breaks in HTML.") == []

    def test_autolink_not_flagged_as_html(self) -> None:
        assert validate_yfm("See <https://wiki.yandex.ru> for details.") == []

    def test_one_line_directive_pair(self) -> None:
        assert validate_yfm("{% cut %}short{% endcut %}") == []


class TestStructuralWarnings:
    def test_unclosed_note(self) -> None:
        warnings = validate_yfm("{% note info %}\n\ntext")
        assert len(warnings) == 1
        assert "line 1" in warnings[0]
        assert "{% endnote %}" in warnings[0]

    def test_orphan_endcut(self) -> None:
        warnings = validate_yfm("text\n\n{% endcut %}")
        assert len(warnings) == 1
        assert "line 3" in warnings[0]
        assert "no matching" in warnings[0]

    def test_nested_cuts_balanced(self) -> None:
        content = "{% cut %}\n{% cut %}\ninner\n{% endcut %}\n{% endcut %}"
        assert validate_yfm(content) == []

    def test_nested_cut_missing_one_close(self) -> None:
        content = "{% cut %}\n{% cut %}\ninner\n{% endcut %}"
        warnings = validate_yfm(content)
        assert len(warnings) == 1
        assert "{% cut %}" in warnings[0]

    def test_unclosed_list_tabs(self) -> None:
        warnings = validate_yfm("{% list tabs %}\n\n- Tab\n")
        assert len(warnings) == 1
        assert "{% endlist %}" in warnings[0]

    def test_unclosed_code_fence(self) -> None:
        warnings = validate_yfm(f"text\n{FENCE}python\ncode")
        assert len(warnings) == 1
        assert "line 2" in warnings[0]
        assert "never closed" in warnings[0]

    def test_unclosed_multiline_table(self) -> None:
        warnings = validate_yfm("#|\n|| a | b ||")
        assert len(warnings) == 1
        assert "'|#'" in warnings[0]

    def test_orphan_table_close(self) -> None:
        warnings = validate_yfm("text\n|#")
        assert len(warnings) == 1
        assert "never opened" in warnings[0]

    def test_unknown_directive_not_flagged(self) -> None:
        assert validate_yfm("{% include notitle [x](y) %}") == []


class TestGfmIsms:
    @pytest.mark.parametrize(
        "alert", ["NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"]
    )
    def test_gfm_alert_flagged(self, alert: str) -> None:
        warnings = validate_yfm(f"> [!{alert}]\n> Body text.")
        assert len(warnings) == 1
        assert f"[!{alert}]" in warnings[0]
        assert "{% note info %}" in warnings[0]

    def test_gfm_alert_case_insensitive(self) -> None:
        warnings = validate_yfm("> [!note]\n> Body.")
        assert len(warnings) == 1

    def test_plain_blockquote_not_flagged(self) -> None:
        assert validate_yfm("> Just a quote.") == []

    def test_html_details_flagged_with_cut_hint(self) -> None:
        warnings = validate_yfm("<details><summary>Spoiler</summary>text</details>")
        assert warnings
        assert "{% cut" in warnings[0]

    def test_html_br_flagged_with_hint(self) -> None:
        warnings = validate_yfm("first line<br>second line")
        assert len(warnings) == 1
        assert "<br>" in warnings[0]

    def test_html_img_with_attributes_flagged(self) -> None:
        warnings = validate_yfm('<img src="x.png" width="200"/>')
        assert len(warnings) == 1
        assert "![alt](url)" in warnings[0]

    def test_generic_type_parameter_not_flagged(self) -> None:
        assert validate_yfm("Функция принимает List<int> и возвращает T.") == []

    def test_comparison_prose_not_flagged(self) -> None:
        assert validate_yfm("если a<b и b>c, то интервал пуст") == []


class TestOutputShape:
    def test_warnings_sorted_by_line_number(self) -> None:
        content = "{% note %}\ntext\n> [!NOTE]\n<br>"
        warnings = validate_yfm(content)
        lines = [int(w.split(":")[0].removeprefix("line ")) for w in warnings]
        assert lines == sorted(lines)

    def test_cap_with_suppression_summary(self) -> None:
        content = "\n".join("> [!NOTE]" if i % 2 == 0 else "" for i in range(60))
        warnings = validate_yfm(content)
        assert len(warnings) == MAX_WARNINGS + 1
        assert "suppressed" in warnings[-1]


class TestCheatsheet:
    def test_cheatsheet_passes_own_validation(self) -> None:
        assert validate_yfm(YFM_CHEATSHEET) == []
