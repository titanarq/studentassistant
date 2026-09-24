"""The prompt registry: `prompts/<name>.md` by name, with a stable content hash."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.llm import PromptNotFoundError, PromptRegistry, content_hash, load_prompt
from studentassistant.llm.prompts import PROMPTS_DIR


def test_the_packaged_prompts_directory_exists_and_loads() -> None:
    assert PROMPTS_DIR.is_dir()
    registry = PromptRegistry()

    assert "structured-output" in registry.names()
    prompt = load_prompt("structured-output")
    assert "{tool_name}" in prompt.content
    assert prompt.hash == content_hash(prompt.content)


def test_the_hash_is_stable_for_identical_content_and_changes_with_it(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("Eres el observador.\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("Eres el observador.\n", encoding="utf-8")
    registry = PromptRegistry(tmp_path)

    first = registry.get("a")
    assert first.hash == registry.get("b").hash
    assert first.hash == registry.get("a").hash
    assert first.hash.startswith("sha256:")

    (tmp_path / "a.md").write_text("Eres el observador atento.\n", encoding="utf-8")
    assert registry.get("a").hash != first.hash


def test_names_lists_markdown_files_only(tmp_path: Path) -> None:
    (tmp_path / "observer.md").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    assert PromptRegistry(tmp_path).names() == ["observer"]


@pytest.mark.parametrize("name", ["missing", "../config", "sub/prompt"])
def test_an_unknown_prompt_raises_a_typed_error(tmp_path: Path, name: str) -> None:
    with pytest.raises(PromptNotFoundError):
        PromptRegistry(tmp_path).get(name)


def test_render_fills_placeholders() -> None:
    assert "`record_topic`" in load_prompt("structured-output").render(tool_name="record_topic")
