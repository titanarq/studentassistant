"""The outline generator (kind `esquema`): the topic's notes as a hierarchical outline + mind map.

`OutlineGenerator` asks Claude (role `generator`, prompt `generator_outline.v1`) for the outline of
the master notes and writes `generated/esquema.md`: the outline as nested Markdown (top-level
nodes as `##` headings, deeper ones as nested bullets, every node with links to the note sections
it came from) followed by a fenced `mermaid` `mindmap` block GitHub renders.

Strict tools do not accept recursive schemas, so Claude answers with `OutlineDraft`: a flat list
of nodes, each naming its parent by id. `OutlineDraft.to_outline(title)` builds the `Outline`
tree from it. Each node is one provenance item (ADR-0005), named by its number in the outline
(`1`, `1.2`, `1.2.1`) with the anchors it cites; a node with no anchor, or with one the notes do
not have, is kept, marked in the Markdown and reported by the framework (`unresolved`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from studentassistant.generators.base import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
)
from studentassistant.generators.registry import register
from studentassistant.llm import load_prompt

KIND = "esquema"
FILE_NAME = f"{KIND}.md"
PROMPT_NAME = "generator_outline.v1"
TOOL_NAME = "record_outline"
TOOL_DESCRIPTION = (
    "Record the outline of the notes: every node with its id, its parent's id (null for a "
    "top-level node), its Spanish title, an optional one-line gloss and the section anchors "
    "of the notes it was built from."
)
MAX_DEPTH = 4
"""Deepest level a node may be at (top-level nodes are level 1)."""
NOTES_LINK = "../notes/apuntes.md"
"""`generated/esquema.md` -> `notes/apuntes.md`, relative, so the links work on GitHub."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _clean_anchors(anchors: list[str]) -> list[str]:
    """Anchors without a leading `#`, blanks dropped, repeats removed, order kept."""
    seen: list[str] = []
    for anchor in anchors:
        cleaned = anchor.strip().lstrip("#").strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- the outline -----------------------------------------------------------------------------------


class OutlineNode(_Strict):
    """One node of the outline: a title, an optional gloss, its children and its note anchors."""

    title: str = Field(description="Spanish, one line.")
    gloss: str | None = Field(default=None, description="An optional one-line explanation.")
    anchors: list[str] = Field(
        default_factory=list, description="Section anchors of the notes it came from, no `#`."
    )
    children: list[OutlineNode] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = _one_line(value)
        if not value:
            raise ValueError("a node's title may not be empty")
        return value

    @field_validator("gloss")
    @classmethod
    def _gloss(cls, value: str | None) -> str | None:
        return (_one_line(value) or None) if value is not None else None

    @field_validator("anchors")
    @classmethod
    def _anchors(cls, value: list[str]) -> list[str]:
        return _clean_anchors(value)

    def depth(self) -> int:
        """Levels from this node down (1 for a leaf)."""
        return 1 + max((child.depth() for child in self.children), default=0)


class Outline(_Strict):
    """The outline of a topic: its title and the tree of nodes (at most `MAX_DEPTH` deep)."""

    title: str = Field(description="The topic's title, the mind map's root.")
    nodes: list[OutlineNode] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = _one_line(value)
        if not value:
            raise ValueError("the outline's title may not be empty")
        return value

    @model_validator(mode="after")
    def _depth(self) -> Self:
        deepest = max(node.depth() for node in self.nodes)
        if deepest > MAX_DEPTH:
            raise ValueError(f"the outline is {deepest} levels deep; at most {MAX_DEPTH}")
        return self

    def walk(self) -> Iterator[tuple[str, int, OutlineNode]]:
        """Every node in document order as `(number, level, node)`: `("1.2", 2, node)`."""

        def visit(
            nodes: list[OutlineNode], prefix: str, level: int
        ) -> Iterator[tuple[str, int, OutlineNode]]:
            for index, node in enumerate(nodes, start=1):
                number = f"{prefix}{index}"
                yield number, level, node
                yield from visit(node.children, f"{number}.", level + 1)

        return visit(self.nodes, "", 1)

    def provenance(self) -> list[ItemProvenance]:
        """One item per node, named by its number, with the anchors it cites."""
        return [
            ItemProvenance(item=number, anchors=node.anchors) for number, _, node in self.walk()
        ]


# -- what Claude answers ---------------------------------------------------------------------------


class OutlineDraftNode(_Strict):
    id: str = Field(description="Unique within the outline, e.g. `n1`, `n1a`.")
    parent: str | None = Field(
        description="The id of an earlier node this one hangs from; null for a top-level node."
    )
    title: str = Field(description="Spanish, short, one line.")
    gloss: str | None = Field(description="One Spanish line explaining the node, or null.")
    anchors: list[str] = Field(
        description="Section anchors of the notes this node comes from, without `#`."
    )


class OutlineDraft(_Strict):
    """The outline as Claude gives it: nodes in document order, each naming its parent."""

    nodes: list[OutlineDraftNode]

    @model_validator(mode="after")
    def _tree(self) -> Self:
        if not self.nodes:
            raise ValueError("the outline has no nodes")
        levels: dict[str, int] = {}
        for node in self.nodes:
            if node.id in levels:
                raise ValueError(f"node id {node.id!r} is repeated")
            if node.parent is None:
                level = 1
            elif node.parent in levels:
                level = levels[node.parent] + 1
            else:
                raise ValueError(
                    f"node {node.id!r} names parent {node.parent!r}, which is not an earlier node"
                )
            if level > MAX_DEPTH:
                raise ValueError(f"node {node.id!r} is at level {level}; at most {MAX_DEPTH}")
            if not _one_line(node.title):
                raise ValueError(f"node {node.id!r} has an empty title")
            levels[node.id] = level
        return self

    def to_outline(self, title: str) -> Outline:
        """The `Outline` tree of these nodes under `title`."""
        built: dict[str, OutlineNode] = {}
        roots: list[OutlineNode] = []
        for draft in self.nodes:
            node = OutlineNode(title=draft.title, gloss=draft.gloss, anchors=draft.anchors)
            built[draft.id] = node
            (roots if draft.parent is None else built[draft.parent].children).append(node)
        return Outline(title=title, nodes=roots)


# -- rendering -------------------------------------------------------------------------------------


def _anchor_links(anchors: list[str], known: set[str] | None) -> str:
    if not anchors:
        return "*(sin sección de los apuntes)*"
    links = []
    for anchor in anchors:
        if known is not None and anchor not in known:
            links.append(f"`#{anchor}` *(no está en los apuntes)*")
        else:
            links.append(f"[#{anchor}]({NOTES_LINK}#{anchor})")
    return ", ".join(links)


_ENTITY = re.compile(r"#(?=\w+;)")
_TAG = re.compile(r"<([^<>]*)>")


def markdown_text(text: str) -> str:
    """`text` for the Markdown body: a `<` is escaped so GitHub does not take it for HTML."""
    return text.replace("<", "&lt;")


def mermaid_label(text: str) -> str:
    """`text` as a quoted mermaid node label.

    Inside `"..."` mermaid takes brackets and parentheses literally; what is left is replaced: the
    double quote (it would end the string), the backtick (it starts a Markdown string), `<` and a
    `<...>` pair (read as HTML), `#name;` (read as an entity code) and `%%` (a comment).
    """
    label = _one_line(text)
    label = label.replace('"', "'").replace("`", "'")
    label = _TAG.sub(r"‹\1›", label).replace("<", "‹")
    label = _ENTITY.sub("# ", label)
    label = label.replace("%%", "% %")
    return f'"{label}"'


def render_mindmap(outline: Outline) -> str:
    """The fenced `mermaid` `mindmap` block of `outline` (ends with a newline)."""
    lines = ["```mermaid", "mindmap", f"  root(({mermaid_label(outline.title)}))"]
    for number, level, node in outline.walk():
        node_id = "n" + number.replace(".", "_")
        shape = "({})" if level == 1 else "[{}]"
        lines.append("  " * (level + 1) + node_id + shape.format(mermaid_label(node.title)))
    lines.append("```")
    return "\n".join(lines) + "\n"


def render_outline(outline: Outline, *, known_anchors: set[str] | None = None) -> str:
    """`esquema.md`: the outline as nested Markdown, then its mind map.

    Top-level nodes are `##` headings (`## 1. Título`) followed by their gloss and the note
    sections they come from; deeper nodes are nested bullets (`- **1.2 Título**: glosa · [#a]`).
    With `known_anchors`, an anchor the notes do not have is marked instead of linked.
    """
    parts = [f"# Esquema: {markdown_text(outline.title)}\n"]
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            parts.append("\n".join(bullets) + "\n")
            bullets.clear()

    for number, level, node in outline.walk():
        links = _anchor_links(node.anchors, known_anchors)
        if level == 1:
            flush()
            parts.append(f"## {number}. {markdown_text(node.title)}\n")
            gloss = f"{markdown_text(node.gloss)} · " if node.gloss else ""
            parts.append(f"{gloss}Apuntes: {links}\n")
        else:
            title = markdown_text(node.title)
            gloss = f": {markdown_text(node.gloss)}" if node.gloss else ""
            bullets.append("  " * (level - 2) + f"- **{number} {title}**{gloss} · {links}")
    flush()
    parts.append("## Mapa mental\n")
    parts.append(render_mindmap(outline))
    return "\n".join(parts)


# -- the generator ---------------------------------------------------------------------------------


@register
class OutlineGenerator(Generator):
    kind = KIND
    title = "Esquema"
    description = "Esquema jerárquico del tema con un mapa mental."
    version = 1

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        prompt = load_prompt(PROMPT_NAME)
        result = await context.structured(
            [{"role": "user", "content": [context.notes_block()]}],
            OutlineDraft,
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
            system=prompt.content,
            prompt_hash=prompt.hash,
        )
        outline = result.value.to_outline(context.topic_title)
        known = set(context.anchors)
        return GeneratorOutput(
            files={FILE_NAME: render_outline(outline, known_anchors=known)},
            items=outline.provenance(),
            model=result.responses[-1].model,
            prompt_hash=prompt.hash,
        )
