"""SVG diagrams the tutor-editor draws into the notes (#511).

Mermaid (a ```` ```mermaid ```` fence written with `apply_edits`) covers flowcharts, mind maps and
sequences; a figure Mermaid cannot draw -- a triangle with its heights, a circuit, a labelled cell,
an axis with a curve -- the editor draws as SVG with the strict tool `draw_diagram` of a revise
turn. The SVG is LLM output, so it is never stored as given: `vault.sanitize_svg` rebuilds it from
an allow-list (no scripts, event handlers or external references), and `vault.put_source` stores
the sanitized drawing as another `images` source of the topic, `sources/images/img-NNN.svg`, with a
sidecar `origin: drawn`, `title`, `content_type: image/svg+xml`, `sha256` and `added_at`.

The notes show it as an image and cite it like a crop: `![Diagrama N](../sources/images/
img-NNN.svg)[^imgNNN]` and the footnote `[^imgNNN]: [Diagrama N](../sources/images/img-NNN.svg)`
(`notes_format.diagram_provenance`). `DrawnDiagram` has the same shape a crop has
(`crop.CroppedImage`: `path`, `source_id`, `number`, `meta`, `paths`, `footnote`, `markdown`), so
the revise turn stores, commits, retires and undoes a diagram exactly as it does a crop.
Nothing here calls Claude or commits.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.edits import EditOp, NewFootnote
from studentassistant.editor.notes_format import LINK_PREFIX, Provenance, diagram_provenance
from studentassistant.llm import strict_tool
from studentassistant.vault import (
    SVG_MEDIA_TYPE,
    SvgError,
    Vault,
    put_source,
    sanitize_svg,
    topic_directory,
)
from studentassistant.vault.sources import SIDECAR_SUFFIX

DIAGRAM_TOOL = "draw_diagram"
DIAGRAM_TOOL_DESCRIPTION = (
    "Draw one SVG diagram (a figure Mermaid cannot draw: geometry, a circuit, a labelled drawing,"
    " a graph of a function) and insert it, cited, where `op`/`section`/`block` say. For"
    " flowcharts, mind maps and sequences write a ```mermaid fence with the edit tool instead."
)
DRAWN_ORIGIN = "drawn"
IMAGES_KIND = "images"
MAX_TITLE_CHARS = 200


class DrawDiagramRequest(BaseModel):
    """The input of the `draw_diagram` tool: the drawing, what it shows, where it goes."""

    model_config = ConfigDict(extra="forbid")

    svg: str = Field(
        description='The whole drawing as one `<svg xmlns="http://www.w3.org/2000/svg"'
        ' viewBox="...">` document: static shapes and text only (no script, event handler,'
        " link, external image, font or stylesheet). Labels in Spanish."
    )
    title: str = Field(description="What the diagram shows, one short Spanish phrase.")
    op: Literal["insert_after", "replace_block"] = Field(
        description="`insert_after`: the diagram goes after block `block` (0 = first);"
        " `replace_block`: it replaces block `block`."
    )
    section: str = Field(description="The anchor of the section, without `#`.")
    block: int = Field(description="The block number within the section, as the block map shows.")
    summary: str = Field(description="What this turn changes, one short Spanish sentence.")


@dataclass(frozen=True)
class DrawnDiagram:
    """A diagram stored by `store_diagram`, with what the notes need to cite and show it."""

    # Vault-relative path of the new file (`subjects/.../sources/images/img-NNN.svg`).
    path: str
    # Topic-relative source id (`sources/images/img-NNN.svg`), as the notes cite it.
    source_id: str
    number: int
    # The sidecar written next to the file.
    meta: dict[str, Any]
    # `«Diagrama N»`, pointing at the new file.
    provenance: Provenance

    @property
    def paths(self) -> list[str]:
        """The vault-relative files stored: the drawing and its sidecar."""
        directory, _, name = self.path.rpartition("/")
        return [self.path, f"{directory}/{name.split('.', 1)[0]}{SIDECAR_SUFFIX}"]

    @property
    def footnote_label(self) -> str:
        """`imgNNN`, the label every image of the topic uses."""
        return f"img{self.number:03d}"

    @property
    def footnote(self) -> str:
        """`[^imgNNN]: [Diagrama N](../sources/images/img-NNN.svg)`."""
        return self.provenance.definition(self.footnote_label)

    @property
    def markdown(self) -> str:
        """The image link: `![Diagrama N](../sources/images/img-NNN.svg)`."""
        return f"![{self.provenance.text}]({LINK_PREFIX}{self.source_id})"


def diagram_tool() -> dict[str, Any]:
    """The strict `draw_diagram` tool definition offered by a revise turn."""
    return strict_tool(DIAGRAM_TOOL, DIAGRAM_TOOL_DESCRIPTION, DrawDiagramRequest)


def request_errors(request: DrawDiagramRequest) -> tuple[bytes | None, list[str]]:
    """`(sanitized drawing, errors)` of a `draw_diagram` input: an SVG `sanitize_svg` refuses, an
    empty or overlong title, an empty summary (the anchor is checked by the caller)."""
    errors: list[str] = []
    svg: bytes | None = None
    try:
        svg = sanitize_svg(request.svg)
    except SvgError as error:
        errors.append(f"`{DIAGRAM_TOOL}`: {error}")
    title = request.title.strip()
    if not title:
        errors.append(f"Indica en `title` qué muestra el diagrama de `{DIAGRAM_TOOL}`.")
    elif len(title) > MAX_TITLE_CHARS:
        errors.append(f"El `title` de `{DIAGRAM_TOOL}` es demasiado largo.")
    if not request.summary.strip():
        errors.append("Falta el resumen (`summary`) del cambio.")
    return svg, errors


async def store_diagram(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    svg: bytes | str,
    title: str,
    *,
    added_at: datetime | None = None,
) -> DrawnDiagram:
    """Sanitize `svg` and store it as a new `images` source of the topic; nothing is committed.

    Raises:
        SvgError: the drawing is not one `sanitize_svg` keeps; nothing is written.
        Whatever `vault.put_source` raises for the new file.
    """
    data = sanitize_svg(svg)
    meta: dict[str, Any] = {
        "origin": DRAWN_ORIGIN,
        "title": " ".join(title.split()),
        "content_type": SVG_MEDIA_TYPE,
        "sha256": hashlib.sha256(data).hexdigest(),
        "added_at": added_at or datetime.now(UTC),
    }
    stored = await asyncio.to_thread(
        put_source, vault, subject_slug, topic_slug, IMAGES_KIND, "diagram.svg", data, meta
    )
    number = int(stored.name.split(".", 1)[0].removeprefix("img-"))
    source_id = stored.relative_to(topic_directory(vault, subject_slug, topic_slug)).as_posix()
    return DrawnDiagram(
        path=stored.relative_to(vault.path).as_posix(),
        source_id=source_id,
        number=number,
        meta=meta,
        provenance=diagram_provenance(number),
    )


def diagram_edit(request: DrawDiagramRequest, diagram: DrawnDiagram) -> tuple[EditOp, NewFootnote]:
    """The ordinary edit that shows and cites a diagram: the image link with its footnote reference
    as the block's text, and the «Diagrama N» footnote definition."""
    label = diagram.footnote_label
    op = EditOp(
        op=request.op,
        section=request.section,
        block=request.block,
        text=f"{diagram.markdown}[^{label}]",
    )
    definition = diagram.footnote.removeprefix(f"[^{label}]: ")
    return op, NewFootnote(label=label, definition=definition)


def placeholder_edit(request: DrawDiagramRequest) -> EditOp:
    """The request's edit with a stand-in text: checks the anchor before anything is stored."""
    return EditOp(op=request.op, section=request.section, block=request.block, text="-")


__all__ = [
    "DIAGRAM_TOOL",
    "DRAWN_ORIGIN",
    "DrawDiagramRequest",
    "DrawnDiagram",
    "diagram_edit",
    "diagram_tool",
    "placeholder_edit",
    "request_errors",
    "store_diagram",
]
