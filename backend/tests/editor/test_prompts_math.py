"""The chat prompts and LaTeX (#532): rendered text is written in `$...$`, spoken text is not."""

from __future__ import annotations

import pytest

from studentassistant.llm.prompts import load_prompt

RENDERED = [
    "editor_study_chat",
    "editor_explain",
    "editor_revise",
    "editor_incorporate",
    "editor_doubts",
]


@pytest.mark.parametrize("name", RENDERED)
def test_displayed_answers_are_written_in_latex(name: str) -> None:
    content = load_prompt(name).content
    assert "`$...$`" in content
    assert "`$$...$$`" in content


def test_the_spoken_tutor_still_writes_no_latex() -> None:
    content = load_prompt("editor_tutor").content
    assert "no formulas written in LaTeX" in content
    assert "`$...$`" not in content
