"""Every prompt whose reply lands in a chat states the same reply format (light Markdown)."""

import pytest

from studentassistant.llm import load_prompt

WRITTEN = ["editor_revise", "editor_incorporate", "editor_explain", "editor_study_chat"]


@pytest.mark.parametrize("name", WRITTEN)
def test_written_chat_prompts_ask_for_light_markdown(name: str) -> None:
    content = " ".join(load_prompt(name).content.split())
    assert "light Markdown only" in content
    assert "no headings, no tables, no raw HTML" in content
    assert "spanish" in content.lower()


@pytest.mark.parametrize("name", ["editor_revise", "editor_incorporate", "editor_explain"])
def test_footnote_references_stay_as_written(name: str) -> None:
    assert "`[^p4]`" in load_prompt(name).content


def test_spoken_tutor_prompt_stays_plain_text() -> None:
    content = " ".join(load_prompt("editor_tutor").content.split())
    assert "not Markdown" in content
    assert "un enlace" in content
