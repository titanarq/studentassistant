"""Prompt registry: versioned `prompts/<name>.md` files, each with a content hash (ADR-0004)."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from studentassistant.llm.errors import PromptNotFoundError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def content_hash(content: str) -> str:
    """`sha256:<hex>` of the UTF-8 content: equal for identical text, different otherwise."""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


class Prompt(BaseModel, frozen=True):
    """One prompt file: its name (the file stem), text and content hash."""

    name: str
    content: str
    hash: str

    def render(self, **values: str) -> str:
        """The content with `{placeholder}`s filled in (`str.format`)."""
        return self.content.format(**values)


class PromptRegistry:
    """Loads prompts by name from a directory (the package's `prompts/` by default)."""

    def __init__(self, directory: Path = PROMPTS_DIR) -> None:
        self.directory = directory

    def names(self) -> list[str]:
        return sorted(path.stem for path in self.directory.glob("*.md"))

    def get(self, name: str) -> Prompt:
        path = self.directory / f"{name}.md"
        if "/" in name or "\\" in name or not path.is_file():
            raise PromptNotFoundError(f"no prompt named {name!r} in {self.directory}")
        content = path.read_text(encoding="utf-8")
        return Prompt(name=name, content=content, hash=content_hash(content))


@lru_cache(maxsize=1)
def default_registry() -> PromptRegistry:
    return PromptRegistry()


def load_prompt(name: str) -> Prompt:
    """The packaged prompt `name` (`prompts/<name>.md`)."""
    return default_registry().get(name)
