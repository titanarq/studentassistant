"""The master notes format of ADR-0005: parser, byte-stable serializer, provenance and validator.

`notes/apuntes.md` is Markdown. Its sections are headings of level 2 or deeper carrying a stable
anchor (`## 2. Causas {#causas}`); everything before the first of them (the `# Tema` title, an
introduction) is the preamble. Inside a section the text is split into blocks at blank lines:
paragraphs, list-item groups (a loose list stays one group) and tables, each of which must carry
at least one provenance footnote reference (`[^p4]`); runs of footnote definitions
(`[^p4]: [Apuntes, página 4](../sources/notes/page-004.jpg)`) are blocks of their own.

Every definition other than `[^ia]` is a Markdown link relative to `notes/apuntes.md`, so it works
when the vault is browsed on GitHub; the source it points to is named by its topic-relative id
(`sources/notes/page-004.jpg`, `sessions/<id>#t=00:02:34-00:03:10`). `[^ia]` marks content that is
in none of the student's sources and is only allowed in the `ampliado` fidelity mode.

Parsing keeps every byte: `serialize(parse(text)) == text` for any text. The validator only
reports; it never patches the notes (ADR-0005: the editor is re-asked instead). This module never
reads the vault itself: whether a cited source exists is asked of an injected `source_exists`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field

from studentassistant.vault import Vault, sessions_directory, sources_directory

FidelityMode = Literal["estricto", "ampliado"]
BlockKind = Literal["title", "paragraph", "list", "table", "rule", "footnotes"]
ProvenanceKind = Literal["notes", "book", "pdf", "web", "transcript", "ia"]

# Blocks the student reads as content: each must end with at least one provenance footnote.
CONTENT_KINDS: tuple[str, ...] = ("paragraph", "list", "table")

IA_LABEL = "ia"
IA_TEXT = "Ampliado por la IA: no está en tus fuentes"
TRANSCRIPT_FILE_NAME = "transcript.jsonl"
# `apuntes.md` lives in `notes/`, one level below the topic directory the source ids start at.
LINK_PREFIX = "../"

_LINE = re.compile(r"[^\n]*\n|[^\n]+$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
_ANCHOR = re.compile(r"^(.*?)[ \t]*\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}$")
_DEFINITION = re.compile(r"^\[\^([^\]\s]+)\]:[ \t]?(.*)$")
_REFERENCE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
_RULE = re.compile(r"^[ \t]{0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_LINK = re.compile(r"^\[([^\]]+)\]\(([^()\s]+)\)[ \t]*$")
_TIME = r"\d{2}:[0-5]\d:[0-5]\d"
_TARGETS: dict[str, re.Pattern[str]] = {
    "notes": re.compile(r"^sources/notes/page-(?P<page>\d{3,})\.[A-Za-z0-9]+$"),
    "book": re.compile(r"^sources/book/page-(?P<page>\d{3,})\.[A-Za-z0-9]+$"),
    "pdf": re.compile(r"^sources/pdf/page-(?P<page>\d{3,})\.[A-Za-z0-9]+(?:#page=(?P<at>\d+))?$"),
    "web": re.compile(r"^sources/web/\d{3,}-[a-z0-9-]+\.md$"),
    "transcript": re.compile(
        rf"^sessions/(?P<session>\d{{8}}-\d{{6}})/{re.escape(TRANSCRIPT_FILE_NAME)}"
        rf"#t=(?P<start>{_TIME})-(?P<end>{_TIME})$"
    ),
}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProvenanceError(ValueError):
    """A footnote definition that is not a provenance this format knows; the message says why."""


# --------------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------------


class Provenance(_Model):
    """Where one footnote says a block comes from.

    `source_id` is topic-relative, as ADR-0005 writes it (`sources/notes/page-004.jpg`,
    `sessions/20260924-183000#t=00:02:34-00:03:10`); `path` is the file that must exist for the
    citation to resolve; `link` is the target written in `apuntes.md`. `[^ia]` has none of them.
    """

    kind: ProvenanceKind
    text: str
    source_id: str | None = None
    path: str | None = None

    @property
    def link(self) -> str | None:
        """The Markdown link target, relative to `notes/apuntes.md`, or `None` for `[^ia]`."""
        if self.kind == "ia" or self.source_id is None or self.path is None:
            return None
        if self.kind == "transcript":
            fragment = self.source_id.partition("#")[2]
            return f"{LINK_PREFIX}{self.path}#{fragment}"
        return f"{LINK_PREFIX}{self.source_id}"

    def definition(self, label: str) -> str:
        """The footnote definition line citing this provenance under `label` (no newline)."""
        if self.kind == "ia":
            return f"[^{IA_LABEL}]: {IA_TEXT}"
        return f"[^{label}]: [{self.text}]({self.link})"


_PAGE_TEXT = {"notes": "Apuntes", "book": "Libro", "pdf": "PDF"}


def page_provenance(
    kind: Literal["notes", "book"], number: int, extension: str = "jpg"
) -> Provenance:
    """A page of the notebook (`notes`) or textbook (`book`): `sources/<kind>/page-NNN.<ext>`."""
    path = f"sources/{kind}/page-{number:03d}.{extension.lstrip('.').lower()}"
    return Provenance(
        kind=kind, text=f"{_PAGE_TEXT[kind]}, página {number}", source_id=path, path=path
    )


def pdf_provenance(number: int, extension: str = "pdf", page: int | None = None) -> Provenance:
    """A stored PDF source `sources/pdf/page-NNN.<ext>`, optionally at one of its pages."""
    path = f"sources/pdf/page-{number:03d}.{extension.lstrip('.').lower()}"
    text = f"PDF, página {page if page is not None else number}"
    source_id = path if page is None else f"{path}#page={page}"
    return Provenance(kind="pdf", text=text, source_id=source_id, path=path)


def web_provenance(file_name: str, title: str | None = None) -> Provenance:
    """A web snapshot `sources/web/NNN-<slug>.md`, shown as `title` (default: its file name)."""
    path = f"sources/web/{file_name}"
    return Provenance(kind="web", text=f"Web: {title or file_name}", source_id=path, path=path)


def transcript_provenance(session_id: str, start_seconds: int, end_seconds: int) -> Provenance:
    """A span of a session's transcript: `sessions/<id>#t=HH:MM:SS-HH:MM:SS`."""
    start, end = _clock(start_seconds), _clock(end_seconds)
    return Provenance(
        kind="transcript",
        text=f"Transcripción, {start}–{end}",
        source_id=f"sessions/{session_id}#t={start}-{end}",
        path=f"sessions/{session_id}/{TRANSCRIPT_FILE_NAME}",
    )


IA_PROVENANCE = Provenance(kind="ia", text=IA_TEXT)


def parse_provenance(definition: FootnoteDefinition) -> Provenance:
    """What a footnote definition cites.

    Raises:
        ProvenanceError: when the definition is not `[^ia]` and not a single Markdown link to a
            source of the topic, relative to `notes/apuntes.md`.
    """
    if definition.label == IA_LABEL:
        return IA_PROVENANCE
    match = _LINK.match(definition.text.strip())
    if match is None:
        raise ProvenanceError(
            f"la nota [^{definition.label}] no es un enlace Markdown a una fuente del tema"
        )
    text, target = match.groups()
    if not target.startswith(LINK_PREFIX):
        raise ProvenanceError(
            f"la nota [^{definition.label}] enlaza a {target}, que no es una fuente del tema"
            f" (el enlace debe empezar por {LINK_PREFIX})"
        )
    relative = target.removeprefix(LINK_PREFIX)
    for kind, pattern in _TARGETS.items():
        found = pattern.match(relative)
        if found is None:
            continue
        if kind == "transcript":
            session = found.group("session")
            return Provenance(
                kind="transcript",
                text=text,
                source_id=f"sessions/{session}#t={found.group('start')}-{found.group('end')}",
                path=f"sessions/{session}/{TRANSCRIPT_FILE_NAME}",
            )
        return Provenance(
            kind=kind,  # type: ignore[arg-type]
            text=text,
            source_id=relative,
            path=relative.partition("#")[0],
        )
    raise ProvenanceError(
        f"la nota [^{definition.label}] enlaza a {target}, que no es una página de apuntes, de"
        " libro o de PDF, una página web guardada ni un fragmento de transcripción"
    )


def _clock(seconds: int) -> str:
    if seconds < 0:
        raise ValueError(f"a transcript time cannot be negative: {seconds}")
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# --------------------------------------------------------------------------------------------
# Document model
# --------------------------------------------------------------------------------------------


class FootnoteDefinition(_Model):
    """`[^label]: text`; `text` keeps any indented continuation lines, newlines included."""

    label: str
    text: str


class Block(_Model):
    """One block of the notes: `text` as written (no final newline) and the `trailing` newline
    and blank lines that follow it, so that `text + trailing` is exactly what was parsed."""

    kind: BlockKind
    text: str
    trailing: str = "\n"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def footnote_refs(self) -> list[str]:
        """The footnote labels this block cites, in order, each once; none for definitions."""
        if self.kind == "footnotes":
            return []
        return list(dict.fromkeys(_REFERENCE.findall(self.text)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def definitions(self) -> list[FootnoteDefinition]:
        """The footnote definitions of a `footnotes` block; empty for any other block."""
        if self.kind != "footnotes":
            return []
        found: list[FootnoteDefinition] = []
        for line in self.text.split("\n"):
            match = _DEFINITION.match(line)
            if match is not None:
                found.append(FootnoteDefinition(label=match.group(1), text=match.group(2)))
            elif found:
                last = found[-1]
                found[-1] = FootnoteDefinition(label=last.label, text=f"{last.text}\n{line}")
        return found

    def render(self) -> str:
        return self.text + self.trailing


class Heading(_Model):
    """A section heading: `raw` is the line as written (no newline); `anchor` its `{#id}`."""

    raw: str
    level: int
    title: str
    anchor: str | None


class Section(_Model):
    """A heading of level 2 or deeper, the blank lines after it and its blocks up to the next."""

    heading: Heading
    heading_trailing: str = "\n"
    blocks: list[Block] = []

    @property
    def anchor(self) -> str | None:
        return self.heading.anchor

    def render(self) -> str:
        return self.heading.raw + self.heading_trailing + "".join(b.render() for b in self.blocks)


class NotesDocument(_Model):
    """A parsed `apuntes.md`: blank lines it starts with, preamble blocks and sections."""

    leading: str = ""
    preamble: list[Block] = []
    sections: list[Section] = []

    @property
    def footnotes(self) -> list[FootnoteDefinition]:
        """Every footnote definition of the document, in the order written."""
        blocks = [*self.preamble, *(b for s in self.sections for b in s.blocks)]
        return [d for b in blocks for d in b.definitions]

    def section(self, anchor: str) -> Section:
        """The section whose heading carries `{#anchor}`; `KeyError` when there is none."""
        for section in self.sections:
            if section.anchor == anchor:
                return section
        raise KeyError(anchor)


# --------------------------------------------------------------------------------------------
# Parse / serialize
# --------------------------------------------------------------------------------------------


def parse(text: str) -> NotesDocument:
    """Parse `apuntes.md` into sections, blocks and footnote definitions; never fails.

    Anything the format does not recognise is kept as a paragraph, so `serialize(parse(text))`
    always gives `text` back; whether the notes follow ADR-0005 is `validate`'s question.
    """
    # Only `\n` ends a line (a `\r` before it stays with it): `str.splitlines` would also split at
    # characters such as U+2028 that Markdown treats as text.
    lines = _LINE.findall(text)
    index = 0
    while index < len(lines) and _blank(lines[index]):
        index += 1
    leading = "".join(lines[:index])

    preamble: list[Block] = []
    headed: list[tuple[Heading, str, list[Block]]] = []
    blocks = preamble
    while index < len(lines):
        line = lines[index]
        heading = _HEADING.match(_content(line))
        if heading is not None and len(heading.group(1)) >= 2:
            index, trailing = _take_blank(lines, index + 1, _ending(line))
            blocks = []
            headed.append((_heading(_content(line), heading), trailing, blocks))
            continue
        kind, end = _block_extent(lines, index)
        body = "".join(lines[index : end - 1]) + _content(lines[end - 1])
        index, trailing = _take_blank(lines, end, _ending(lines[end - 1]))
        if _continues_list(blocks, kind, body):
            previous = blocks[-1]
            blocks[-1] = Block(
                kind="list", text=previous.text + previous.trailing + body, trailing=trailing
            )
        else:
            blocks.append(Block(kind=kind, text=body, trailing=trailing))
    sections = [
        Section(heading=heading, heading_trailing=trailing, blocks=blocks)
        for heading, trailing, blocks in headed
    ]
    return NotesDocument(leading=leading, preamble=preamble, sections=sections)


def serialize(notes: NotesDocument) -> str:
    """The Markdown of `notes`; byte for byte the text it was parsed from if nothing changed."""
    return (
        notes.leading
        + "".join(b.render() for b in notes.preamble)
        + "".join(s.render() for s in notes.sections)
    )


def _blank(line: str) -> bool:
    return line.strip() == ""


def _content(line: str) -> str:
    return line.rstrip("\r\n")


def _ending(line: str) -> str:
    return line[len(_content(line)) :]


def _take_blank(lines: list[str], index: int, ending: str) -> tuple[int, str]:
    """Collect the blank lines from `index`, prefixed by the `ending` of the line before them."""
    trailing = [ending]
    while index < len(lines) and _blank(lines[index]):
        trailing.append(lines[index])
        index += 1
    return index, "".join(trailing)


def _heading(line: str, match: re.Match[str]) -> Heading:
    level = len(match.group(1))
    title = match.group(2)
    anchor_match = _ANCHOR.match(title)
    anchor = None
    if anchor_match is not None:
        title, anchor = anchor_match.group(1), anchor_match.group(2)
    return Heading(raw=line, level=level, title=title, anchor=anchor)


def _classify(line: str) -> BlockKind:
    content = _content(line)
    if _DEFINITION.match(content):
        return "footnotes"
    heading = _HEADING.match(content)
    if heading is not None and len(heading.group(1)) == 1:
        return "title"
    if content.lstrip().startswith("|"):
        return "table"
    if _LIST_ITEM.match(content):
        return "list"
    if _RULE.match(content):
        return "rule"
    return "paragraph"


def _block_extent(lines: list[str], start: int) -> tuple[BlockKind, int]:
    """The kind of the block starting at `start` and the index one past its last line.

    A block runs to the next blank line, section heading or, for content, footnote definition
    line; a title and a rule are one line each; a footnotes block also stops at the first
    non-indented line that is not a definition.
    """
    kind = _classify(lines[start])
    if kind in ("title", "rule"):
        return kind, start + 1
    end = start + 1
    while end < len(lines):
        line = lines[end]
        content = _content(line)
        if _blank(line):
            break
        heading = _HEADING.match(content)
        if heading is not None:
            break
        is_definition = _DEFINITION.match(content) is not None
        if kind == "footnotes":
            if not is_definition and not content.startswith((" ", "\t")):
                break
        elif is_definition:
            break
        end += 1
    return kind, end


def _continues_list(blocks: list[Block], kind: BlockKind, body: str) -> bool:
    """Whether a chunk after blank lines still belongs to the list block before it.

    It does when that block is a list and the chunk is another item of it (a loose list) or is
    indented (a nested paragraph or list): the whole list-item group is one block.
    """
    if not blocks or blocks[-1].kind != "list":
        return False
    return kind == "list" or (kind == "paragraph" and body.startswith((" ", "\t")))


# --------------------------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------------------------

SourceExists = Callable[[str], bool]


def validate(
    notes: NotesDocument | str,
    mode: FidelityMode = "estricto",
    source_exists: SourceExists | None = None,
) -> list[str]:
    """Every way `notes` breaks ADR-0005, as Spanish messages suitable for re-asking the editor.

    Reports: a content block (paragraph, list-item group, table) without a footnote; a footnote
    reference with no definition; a definition that is not a provenance this format knows or whose
    source does not exist (asked of `source_exists` with the topic-relative path, e.g.
    `sources/notes/page-004.jpg`); any `[^ia]` in `estricto` mode; a section without anchor, a
    repeated anchor and a repeated footnote definition. An empty list means the notes are valid.
    `source_exists=None` skips only the existence check. The notes are never changed.
    """
    document = parse(notes) if isinstance(notes, str) else notes
    errors: list[str] = []
    definitions: dict[str, FootnoteDefinition] = {}
    for definition in document.footnotes:
        if definition.label in definitions:
            errors.append(f"La nota al pie [^{definition.label}] está definida más de una vez.")
            continue
        definitions[definition.label] = definition

    seen_anchors: set[str] = set()
    for section in document.sections:
        anchor = section.anchor
        if anchor is None:
            errors.append(
                f"La sección «{section.heading.title}» no tiene ancla estable:"
                " escribe su título como «## Título {#ancla}»."
            )
        elif anchor in seen_anchors:
            errors.append(f"El ancla #{anchor} se repite en más de una sección.")
        else:
            seen_anchors.add(anchor)

    for where, block in _located_blocks(document):
        refs = block.footnote_refs
        if block.kind in CONTENT_KINDS and not refs:
            errors.append(
                f"{where}: no tiene nota al pie de procedencia; cita la fuente de la que sale"
                f" o, si no está en tus fuentes, pregunta al estudiante."
            )
        for label in refs:
            if label == IA_LABEL and mode == "estricto":
                errors.append(
                    f"{where}: usa [^ia] (contenido que no está en tus fuentes), que el modo"
                    " de fidelidad estricto no permite; pregunta al estudiante en su lugar."
                )
            if label not in definitions:
                errors.append(f"{where}: cita [^{label}], que no tiene definición.")

    for label, definition in definitions.items():
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError as error:
            errors.append(f"Nota al pie [^{label}]: {error}.")
            continue
        if provenance.kind == "ia":
            if mode == "estricto":
                errors.append(
                    "Nota al pie [^ia]: el modo de fidelidad estricto no admite contenido"
                    " ampliado por la IA."
                )
            continue
        if (
            source_exists is not None
            and provenance.path is not None
            and not source_exists(provenance.path)
        ):
            errors.append(
                f"Nota al pie [^{label}]: la fuente {provenance.source_id} no existe en el tema."
            )
    return errors


def _located_blocks(document: NotesDocument) -> list[tuple[str, Block]]:
    """Every block with the Spanish name of where it is: section anchor, block number, opening."""
    located: list[tuple[str, Block]] = []
    for number, block in enumerate(document.preamble, start=1):
        located.append((f"Antes de la primera sección, bloque {number} ({_opening(block)})", block))
    for section in document.sections:
        name = f"#{section.anchor}" if section.anchor else f"«{section.heading.title}»"
        for number, block in enumerate(section.blocks, start=1):
            located.append((f"Sección {name}, bloque {number} ({_opening(block)})", block))
    return located


def _opening(block: Block, width: int = 40) -> str:
    words = " ".join(_REFERENCE.sub("", block.text).split())
    kind = {"paragraph": "párrafo", "list": "lista", "table": "tabla"}.get(block.kind, block.kind)
    snippet = words if len(words) <= width else words[:width].rstrip() + "…"
    return f"{kind} «{snippet}»"


# --------------------------------------------------------------------------------------------
# Resolver over the vault's public API
# --------------------------------------------------------------------------------------------

_SOURCE_PATH = re.compile(r"^sources/(?P<kind>notes|book|pdf|web)/(?P<name>[A-Za-z0-9._-]+)$")
_TRANSCRIPT_PATH = re.compile(rf"^sessions/(?P<session>\d{{8}}-\d{{6}})/{TRANSCRIPT_FILE_NAME}$")


def topic_source_resolver(vault: Vault, subject_slug: str, topic_slug: str) -> SourceExists:
    """A `source_exists` for one topic, locating each path through the vault's public directories.

    Only the paths a provenance can name are answered (`sources/<kind>/<file>` and
    `sessions/<id>/transcript.jsonl`); anything else, `..` included, does not exist.
    """

    def source_exists(path: str) -> bool:
        target: Path
        if (source := _SOURCE_PATH.match(path)) is not None:
            directory = sources_directory(vault, subject_slug, topic_slug, source.group("kind"))
            target = directory / source.group("name")
        elif (transcript := _TRANSCRIPT_PATH.match(path)) is not None:
            directory = sessions_directory(vault, subject_slug, topic_slug)
            target = directory / transcript.group("session") / TRANSCRIPT_FILE_NAME
        else:
            return False
        return target.is_file()

    return source_exists
