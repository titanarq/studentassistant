# Module: editor

**Lives in:** `backend/src/studentassistant/editor/`. Decision: ADR-0005.

## Responsibility
- Notes format: parser/validator of `apuntes.md` (anchors, provenance footnotes, `[^ia]`,
  fidelity mode).
- Generation ("prepárame el tema", role `editor`, Opus): from page transcriptions (+ images for
  schemes), the transcript grouped by section, other sources, pending items, digest and the
  subject style guide.
- Edit loop: chat reply + section-level edit ops, validated, applied, committed with a summary;
  conversation persisted.
- Doubts resolution: auto-resolve pending items with cited evidence, ask the rest one by one,
  record decisions.
- "¿Por qué pusiste esto?": explain a paragraph from its cited sources.
- Style guide learning per subject; notes versions (git tags) and diffs.

## Public surface
What exists today, after issue #30: the master notes format of ADR-0005, in
`studentassistant.editor.notes_format`. It never calls Claude, never writes the vault and never
reads it itself; generating and editing the notes are later issues.

### The format of `notes/apuntes.md`
- **Preamble**: whatever comes before the first section -- the `# Tema` title and, optionally, an
  introduction.
- **Sections**: every heading of level 2 or deeper, with a stable anchor kept across edits:
  `## 2. Causas {#causas}`, `### 2.1. La máquina de vapor {#maquina-de-vapor}`. An anchor is
  letters, digits, `-` and `_`, unique in the document.
- **Blocks**: the text of a section split at blank lines -- a paragraph, a list-item group (a
  loose list and the indented paragraphs of its items stay one block) or a table -- and also the
  one-line `# title`, a `---` rule and a run of footnote definitions. Every paragraph, list-item
  group and table carries at least one provenance footnote reference (`[^p4]`) somewhere in it,
  usually at its end (in a table, inside a cell).
- **Provenance footnotes**: `[^label]: [texto](enlace)`, one Markdown link relative to
  `notes/apuntes.md`, so it works when the vault is browsed on GitHub. The source is named by its
  topic-relative id, as ADR-0005 writes it:

  | kind | definition | source id | file that must exist |
  |---|---|---|---|
  | notes page | `[Apuntes, página 4](../sources/notes/page-004.jpg)` | `sources/notes/page-004.jpg` | same |
  | book page | `[Libro, página 12](../sources/book/page-012.jpg)` | `sources/book/page-012.jpg` | same |
  | PDF page | `[PDF, página 3](../sources/pdf/page-001.pdf#page=3)` | `sources/pdf/page-001.pdf#page=3` | `sources/pdf/page-001.pdf` |
  | web snapshot | `[Web: Máquina de vapor](../sources/web/001-maquina-de-vapor.md)` | `sources/web/001-maquina-de-vapor.md` | same |
  | transcript span | `[Transcripción, 00:02:34–00:03:10](../sessions/20260924-183000/transcript.jsonl#t=00:02:34-00:03:10)` | `sessions/20260924-183000#t=00:02:34-00:03:10` | `sessions/20260924-183000/transcript.jsonl` |
  | AI | `[^ia]: Ampliado por la IA: no está en tus fuentes` | -- | -- |

- **Fidelity mode** (`FidelityMode`): `estricto` (default) allows no `[^ia]`; `ampliado` allows it,
  always defined and marked. Where a topic's mode is kept is not this module's business: the
  validator takes it as an argument.

### Functions and models
- `parse(text) -> NotesDocument` -- never fails; anything unrecognised is kept as a paragraph.
  `NotesDocument` has `leading` (blank lines at the start), `preamble: list[Block]`,
  `sections: list[Section]`, the property `footnotes: list[FootnoteDefinition]` (every definition,
  in order) and `section(anchor)`. `Section` has `heading: Heading` (`raw`, `level`, `title`,
  `anchor`), `heading_trailing` and `blocks`; `Block` has `kind` (`title`, `paragraph`, `list`,
  `table`, `rule`, `footnotes`), `text`, `trailing` (the newline and blank lines after it) and the
  computed `footnote_refs` (labels cited, each once) and `definitions`. All are frozen Pydantic v2
  models; an edit builds new ones with `model_copy(update=...)`.
- `serialize(notes) -> str` -- the concatenation of every part as parsed:
  `serialize(parse(text)) == text` byte for byte, for any text.
- Provenance: `Provenance` (`kind`, `text`, `source_id`, `path`, `link`, `definition(label)`);
  builders `page_provenance("notes"|"book", number, extension="jpg")`,
  `pdf_provenance(number, extension="pdf", page=None)`, `web_provenance(file_name, title=None)`,
  `transcript_provenance(session_id, start_seconds, end_seconds)` and `IA_PROVENANCE`;
  `parse_provenance(definition)` raises `ProvenanceError` (Spanish message) for anything but
  `[^ia]` or one relative link to a known source shape.
- `validate(notes, mode="estricto", source_exists=None) -> list[str]` -- Spanish messages, empty
  when the notes are valid; `notes` is a `NotesDocument` or its text. It reports every content
  block without footnote, every reference without definition, every definition that is not a
  provenance or whose `path` `source_exists` says is missing, every `[^ia]` in `estricto`, a
  section without anchor, a repeated anchor and a repeated definition. Each message names the
  section anchor and the block (number and opening words) so it can be sent back to the model as
  is. It never patches the notes: the editor is re-asked (ADR-0005).
- `topic_source_resolver(vault, subject_slug, topic_slug) -> SourceExists` -- the
  `source_exists` for a topic, locating `sources/<kind>/<file>` through
  `vault.sources_directory` and `sessions/<id>/transcript.jsonl` through
  `vault.sessions_directory`; any other path (`..` included) does not exist.

### Example
```python
from studentassistant.editor.notes_format import parse, serialize, topic_source_resolver, validate

text = """# La Revolución Industrial

## 1. Contexto {#contexto}

Empezó en Gran Bretaña a mediados del siglo XVIII.[^p1][^t1]

Las fábricas cambiaron la vida urbana.

[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)
[^t1]: [Transcripción, 00:02:34–00:03:10](../sessions/20260924-183000/transcript.jsonl#t=00:02:34-00:03:10)
"""
notes = parse(text)
assert serialize(notes) == text
assert notes.section("contexto").blocks[0].footnote_refs == ["p1", "t1"]

errors = validate(notes, "estricto", topic_source_resolver(vault, "historia", "la-revolucion-industrial"))
# ["Sección #contexto, bloque 2 (párrafo «Las fábricas cambiaron la vida urbana.»): no tiene
#   nota al pie de procedencia; cita la fuente de la que sale o, si no está en tus fuentes,
#   pregunta al estudiante.", ...plus one per cited source missing from the topic]
```

Fixtures: `backend/tests/fixtures/notes/apuntes.md` (every source kind) and `ampliado.md` (`[^ia]`).
