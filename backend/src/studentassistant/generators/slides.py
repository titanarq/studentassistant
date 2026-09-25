"""The slides generator (kind `diapositivas`): a Marp deck of the topic, exported to PDF and PPTX.

Claude (role `generator`, prompt `generator_slides`) turns the master notes into a short deck: a
title and, per slide, a title, a few bullets, optional speaker notes, the note anchors it comes
from and, optionally, one figure. Figures are the source pages the notes cite (a notebook or
textbook page, a PDF page's thumbnail); Claude is given their list, not their images, and each
figure a slide uses is copied under `generated/diapositivas/` and credited on that slide's footer
("Imagen: Libro, página 12").

Files under `generated/`:

- `diapositivas.md` -- the deck as Marp Markdown (front matter `marp: true`), the images linked
  relative to it, so it opens as is in Marp for VS Code or `marp --preview`;
- `diapositivas/figura-NN.<ext>` -- the figures the deck shows;
- `diapositivas.pdf`, `diapositivas.pptx` -- the exports by Marp CLI (`MarpExporter`, configured
  by `[generators]` in the config). When Marp is not installed, fails or times out, the Markdown
  is still stored and a Spanish warning says why there is no PDF/PPTX; the option `export=false`
  skips the export altogether.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.config import GeneratorsSettings, Settings
from studentassistant.editor.notes_format import (
    Provenance,
    ProvenanceError,
    parse_provenance,
)
from studentassistant.generators.base import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
)
from studentassistant.generators.registry import register
from studentassistant.llm import load_prompt
from studentassistant.vault import VaultError, get_subject, read_source, topic_directory

logger = logging.getLogger(__name__)

KIND = "diapositivas"
PROMPT_NAME = "generator_slides"
TOOL_NAME = "record_slides"
MARKDOWN_NAME = f"{KIND}.md"
PDF_NAME = f"{KIND}.pdf"
PPTX_NAME = f"{KIND}.pptx"
ASSETS_DIR = KIND
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
STDERR_TAIL = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlidesOptions(_Strict):
    size: int = Field(
        default=12, ge=1, le=40, description="Cuántas diapositivas, como mucho (sin la portada)."
    )
    export: bool = Field(default=True, description="Exportar también a PDF y PowerPoint.")


class DraftSlide(_Strict):
    """One slide as Claude records it."""

    title: str = Field(min_length=1, description="The slide's short title, in Spanish.")
    bullets: list[str] = Field(description="Its bullets, each one short idea, in Spanish.")
    notes: str = Field(default="", description="Optional speaker notes, in Spanish.")
    anchors: list[str] = Field(
        default_factory=list, description="Anchors (no `#`) of the note sections it comes from."
    )
    image: str | None = Field(
        default=None, description="The id of a figure from the list to show on it, if any."
    )


class DraftDeck(_Strict):
    title: str = Field(min_length=1, description="The deck's title: the topic's.")
    subtitle: str | None = Field(default=None, description="An optional one-line subtitle.")
    slides: list[DraftSlide]


@dataclass(frozen=True)
class Figure:
    """A source page the notes cite that a slide may show.

    `id` is what Claude is given (`f1`...), `text` the citation's text ("Libro, página 1"),
    `source` the file shown (topic-relative), `anchors` the sections citing it.
    """

    id: str
    text: str
    source: str
    anchors: tuple[str, ...]


def collect_figures(context: GeneratorContext) -> list[Figure]:
    """The figures of the notes: every notes/book page image and PDF page they cite, in order.

    A page with a cropped version (`page-NNN.page.jpg`) shows the crop; a PDF page shows its
    thumbnail (`page-NNN.pKKK.jpg`); a source whose image is not stored is left out.
    """
    definitions = {d.label: d for d in context.notes.footnotes}
    topic_dir = topic_directory(context.vault, context.subject, context.topic)
    found: dict[str, tuple[str, list[str]]] = {}
    for section in context.notes.sections:
        for block in section.blocks:
            for label in block.footnote_refs:
                definition = definitions.get(label)
                if definition is None:
                    continue
                try:
                    provenance = parse_provenance(definition)
                except ProvenanceError:
                    continue
                source = _figure_file(provenance, topic_dir)
                if source is None:
                    continue
                text, anchors = found.setdefault(source, (provenance.text, []))
                if section.anchor is not None and section.anchor not in anchors:
                    anchors.append(section.anchor)
    return [
        Figure(id=f"f{number}", text=text, source=source, anchors=tuple(anchors))
        for number, (source, (text, anchors)) in enumerate(found.items(), start=1)
    ]


def _figure_file(provenance: Provenance, topic_dir: Path) -> str | None:
    if provenance.path is None or provenance.kind not in ("notes", "book", "pdf"):
        return None
    path = PurePosixPath(provenance.path)
    candidates: list[PurePosixPath] = []
    if provenance.kind == "pdf":
        page = (provenance.source_id or "").partition("#page=")[2]
        if not page.isdigit():
            return None
        candidates.append(path.with_name(f"{path.stem}.p{int(page):03d}.jpg"))
    elif path.suffix.lower() in IMAGE_EXTENSIONS:
        candidates += [path.with_name(f"{path.stem}.page.jpg"), path]
    for candidate in candidates:
        if (topic_dir / candidate).is_file():
            return candidate.as_posix()
    return None


def _figures_block(figures: list[Figure]) -> dict[str, Any]:
    lines = [
        f"- `{figure.id}`: {figure.text} (citada en {', '.join('#' + a for a in figure.anchors)})"
        for figure in figures
    ]
    heading = "Figuras de las fuentes que puedes mostrar (campo `image`):"
    return {"type": "text", "text": heading + "\n" + "\n".join(lines)}


# --------------------------------------------------------------------------------------------
# Marp Markdown
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlacedFigure:
    """A figure shown on the deck: the file under `generated/` and its credit."""

    name: str
    credit: str


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _comment_safe(text: str) -> str:
    """Text that cannot close or nest an HTML comment."""
    return text.replace("-->", "→").replace("<!--", "").replace("--", "–")


def _heading_text(text: str) -> str:
    return _one_line(text).lstrip("#").strip()


def _bullet(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    first = re.sub(r"^([-*+]|\d+[.)])\s+", "", lines[0]) if lines else ""
    return "\n".join([f"- {first}", *(f"  {line}" for line in lines[1:])])


def _directive(name: str, value: str) -> str:
    # A JSON string is a YAML double-quoted scalar, which is how Marp reads directive values.
    return f"<!-- {name}: {json.dumps(_comment_safe(value), ensure_ascii=False)} -->"


def render_markdown(
    deck: DraftDeck,
    figures: dict[int, PlacedFigure],
    *,
    subject_name: str,
) -> str:
    """The deck as Marp Markdown; `figures` maps a slide's index to the figure it shows."""
    title = _heading_text(deck.title)
    front_matter = [
        "---",
        "marp: true",
        "theme: default",
        "paginate: true",
        "lang: es",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "---",
    ]
    cover = ["<!-- _class: lead -->", "<!-- _paginate: false -->", "", f"# {title}", ""]
    if deck.subtitle and deck.subtitle.strip():
        cover += [_one_line(deck.subtitle), ""]
    cover.append(_one_line(subject_name))
    parts = ["\n".join(front_matter + [""] + cover)]
    for index, slide in enumerate(deck.slides):
        lines = [f"## {_heading_text(slide.title)}", ""]
        lines += [_bullet(bullet) for bullet in slide.bullets if bullet.strip()]
        figure = figures.get(index)
        if figure is not None:
            lines += ["", f"![bg right:40% contain]({figure.name})", ""]
            lines.append(_directive("_footer", f"Imagen: {figure.credit}"))
        if slide.notes.strip():
            lines += ["", "<!--", _comment_safe(slide.notes.strip()), "-->"]
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts) + "\n"


# --------------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------------


@dataclass
class Exported:
    """What an export produced: files named as under `generated/`, and Spanish warnings."""

    files: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class SlidesExporter(Protocol):
    async def export(self, markdown: str, assets: dict[str, bytes]) -> Exported: ...


class MarpExporter:
    """Runs Marp CLI on the deck, in a temporary directory, once for the PDF and once for PPTX.

    The deck and its figures are written there as they are laid out under `generated/`, and Marp
    reads local images (`--allow-local-files`). A missing command, a failure or a timeout gives a
    warning, never an exception.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        browser_path: Path | None = None,
    ) -> None:
        self.command = list(command)
        self.timeout_seconds = timeout_seconds
        self.browser_path = browser_path

    @classmethod
    def from_settings(cls, settings: GeneratorsSettings) -> MarpExporter:
        return cls(
            settings.marp_command,
            timeout_seconds=settings.marp_timeout_seconds,
            browser_path=settings.marp_browser_path,
        )

    async def export(self, markdown: str, assets: dict[str, bytes]) -> Exported:
        return await asyncio.to_thread(self._export, markdown, assets)

    def _export(self, markdown: str, assets: dict[str, bytes]) -> Exported:
        exported = Exported()
        with tempfile.TemporaryDirectory(prefix="sa-marp-") as scratch:
            root = Path(scratch)
            (root / MARKDOWN_NAME).write_text(markdown, encoding="utf-8")
            for name, content in assets.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for name, flag, label in ((PDF_NAME, "--pdf", "PDF"), (PPTX_NAME, "--pptx", "PPTX")):
                problem = self._run(root, flag, name)
                output = root / name
                if problem is None and output.is_file():
                    exported.files[name] = output.read_bytes()
                    continue
                exported.warnings.append(
                    f"No se ha podido exportar las diapositivas a {label}: "
                    f"{problem or 'Marp no ha generado el archivo.'}"
                )
                if problem is not None and problem.startswith("no se encuentra"):
                    break  # the same for the PPTX; one warning is enough
        return exported

    def _run(self, root: Path, flag: str, name: str) -> str | None:
        arguments = [*self.command, "--allow-local-files", flag]
        if self.browser_path is not None:
            arguments += ["--browser-path", str(self.browser_path)]
        arguments += ["--output", name, MARKDOWN_NAME]
        try:
            process = subprocess.Popen(
                arguments,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,  # its own process group, killed whole on a timeout
            )
        except FileNotFoundError:
            return (
                f"no se encuentra Marp CLI (`{self.command[0]}`). Instálalo con "
                "`npm install -g @marp-team/marp-cli` o configura `generators.marp_command`."
            )
        except OSError as error:
            return f"no se ha podido ejecutar Marp ({error})."
        try:
            _, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            return f"Marp no ha terminado en {self.timeout_seconds:g} segundos."
        completed = subprocess.CompletedProcess(arguments, process.returncode, b"", stderr)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()[-STDERR_TAIL:]
            logger.warning("marp %s failed (%s): %s", flag, completed.returncode, detail)
            return f"Marp ha fallado (código {completed.returncode}): {detail or 'sin detalle'}"
        return None


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill Marp and whatever it started (npx, the browser), then reap it."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("marp (pid %s) did not exit after being killed", process.pid)


# --------------------------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------------------------


def slide_id(index: int) -> str:
    """The provenance item id of the slide at `index` (0-based; the cover is not an item)."""
    return f"d{index + 1:02d}"


@register
class SlidesGenerator(Generator):
    kind = KIND
    title = "Diapositivas"
    description = "Una presentación del tema en Marp, exportada a PDF y PowerPoint."
    version = 1
    options_model = SlidesOptions

    exporter: ClassVar[SlidesExporter | None] = None
    """The exporter to use; `None` builds a `MarpExporter` from the configuration."""

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        options = context.options
        assert isinstance(options, SlidesOptions)
        prompt = load_prompt(PROMPT_NAME)
        figures = await asyncio.to_thread(collect_figures, context)
        content: list[dict[str, Any]] = [context.notes_block()]
        if figures:
            content.append(_figures_block(figures))
        content.append(
            {
                "type": "text",
                "text": f"Haz como mucho {options.size} diapositivas (sin contar la portada).",
            }
        )
        result = await context.structured(
            [{"role": "user", "content": content}],
            DraftDeck,
            tool_name=TOOL_NAME,
            tool_description="Record the slide deck made from the notes.",
            system=prompt.content,
            prompt_hash=prompt.hash,
        )
        deck = result.value
        warnings: list[str] = []
        slides = [slide for slide in deck.slides if slide.title.strip()]
        if len(slides) > options.size:
            warnings.append(
                f"Claude ha propuesto {len(slides)} diapositivas; "
                f"se guardan las {options.size} primeras."
            )
            slides = slides[: options.size]
        if not slides:
            warnings.append("Claude no ha propuesto ninguna diapositiva: solo hay portada.")
        deck = deck.model_copy(update={"slides": slides})

        placed, assets, figure_warnings = await asyncio.to_thread(
            _place_figures, context, slides, figures
        )
        warnings += figure_warnings
        subject_name = await asyncio.to_thread(_subject_name, context)
        markdown = render_markdown(deck, placed, subject_name=subject_name)
        files: dict[str, str | bytes] = {MARKDOWN_NAME: markdown, **assets}
        if options.export:
            exporter = self.exporter or MarpExporter.from_settings(Settings().generators)
            exported = await exporter.export(markdown, assets)
            files.update(exported.files)
            warnings += exported.warnings
        return GeneratorOutput(
            files=files,
            items=[
                ItemProvenance(
                    item=slide_id(index), anchors=[anchor.lstrip("#") for anchor in slide.anchors]
                )
                for index, slide in enumerate(slides)
            ],
            item_texts={
                slide_id(index): "\n".join(slide.bullets) for index, slide in enumerate(slides)
            },
            warnings=warnings,
            model=result.responses[-1].model,
            prompt_hash=prompt.hash,
        )


def _place_figures(
    context: GeneratorContext, slides: list[DraftSlide], figures: list[Figure]
) -> tuple[dict[int, PlacedFigure], dict[str, bytes], list[str]]:
    """The figures the slides show, copied as `diapositivas/figura-NN.<ext>`, each once."""
    by_id = {figure.id: figure for figure in figures}
    prefix = f"subjects/{context.subject}/topics/{context.topic}/"
    placed: dict[int, PlacedFigure] = {}
    assets: dict[str, bytes] = {}
    shown: dict[str, PlacedFigure] = {}
    unknown: list[str] = []
    for index, slide in enumerate(slides):
        if slide.image is None or not slide.image.strip():
            continue
        figure = by_id.get(slide.image.strip())
        if figure is None:
            unknown.append(slide.image)
            continue
        if figure.id in shown:
            placed[index] = shown[figure.id]
            continue
        try:
            data = read_source(context.vault, prefix + figure.source).content
        except VaultError:
            logger.exception("could not read the figure %s", figure.source)
            continue
        suffix = PurePosixPath(figure.source).suffix.lower() or ".jpg"
        name = f"{ASSETS_DIR}/figura-{len(assets) + 1:02d}{suffix}"
        assets[name] = data
        shown[figure.id] = placed[index] = PlacedFigure(name=name, credit=figure.text)
    warnings = []
    if unknown:
        warnings.append(
            "Claude ha pedido figuras que no están entre las fuentes "
            f"({', '.join(sorted(set(unknown)))}); se han omitido."
        )
    return placed, assets, warnings


def _subject_name(context: GeneratorContext) -> str:
    try:
        return get_subject(context.vault, context.subject).subject.name
    except VaultError:
        return context.subject
