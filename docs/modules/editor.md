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
What exists today, after issues #30, #61 and #68: the master notes format of ADR-0005, in
`studentassistant.editor.notes_format` (never calls Claude, never writes or reads the vault
itself), "prepárame el tema", the first version of the notes, in
`studentassistant.editor.inputs` and `studentassistant.editor.generate`, the section-level edit
ops in `studentassistant.editor.edits` and the doubts resolution in
`studentassistant.editor.doubts`. The conversational edit loop (#63) and "¿por qué?" are later
issues.

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
  provenance or whose `path` `source_exists` says is missing (for a PDF, its `source_id`, so a
  cited page is checked too: `sources/pdf/page-001.pdf#page=3`), every `[^ia]` in `estricto`, a
  section without anchor, a repeated anchor and a repeated definition. Each message names the
  section anchor and the block (number and opening words) so it can be sent back to the model as
  is. It never patches the notes: the editor is re-asked (ADR-0005).
- `topic_source_resolver(vault, subject_slug, topic_slug) -> SourceExists` -- the
  `source_exists` for a topic, locating `sources/<kind>/<file>` through
  `vault.sources_directory` and `sessions/<id>/transcript.jsonl` through
  `vault.sessions_directory`, and `sources/pdf/<file>#page=K` through
  `sources.pdf_has_page` (the page must be within the imported PDF's `page_count`); any other
  path (`..` included) does not exist.

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

### "Prepárame el tema" -- `generate.py`, `inputs.py`
`await generate_notes(vault, subject_slug, topic_slug, *, client, sync, digest=None,
on_event=None, confirm_over_cap=False, clock=..., max_page_images=20,
max_attachment_bytes=24 MiB) -> GenerationResult` writes the topic's notes with the `editor` role:
`client` is `get_client("editor", ledger=LedgerBinding(vault, subject, topic))` (Opus from
`[llm.roles.editor]`; tests pass `FakeClaude().client("editor")`), `sync` the vault's `GitSync`,
`digest(vault, subject, topic) -> str | None` reads the topic digest (none until #56 writes
`state/digest.md`), `on_event(kind, payload)` an async sink for the `notes.generated` event.

- **Input** (`assemble_input(vault, subject, topic, *, prompt, digest=None, max_page_images,
  max_attachment_bytes) -> EditorInput`, blocking), stable parts first for caching. System: the
  `editor_generate` prompt, then the topic block (subject, title, fidelity mode from `topic.yaml`
  -- anything but `ampliado` is `estricto` --, the subject's `style_guide`), cached by the llm
  client. First user message: the **catalogue** of citable sources with the exact footnote
  definition of each (`[Apuntes, página 1](../sources/notes/page-001.jpg)`; a PDF page as
  `[PDF, página 84](../sources/pdf/page-001.pdf#page=3)`: the link counts pages of the stored file,
  the text shows `sources.original_page`); the **sources** -- each notes and book page as its
  transcription (`sources/<kind>/page-NNN.md`, #50, or a sidecar `transcription` string) plus its
  image (the cropped `page-NNN.page.jpg`, else the still) when `needs_image(transcription, meta)`:
  no transcription, a scheme (mermaid block, nested list, arrow), an uncertain word `[[?...]]` or a
  sidecar `transcription_confidence` below 0.8; each stored PDF as `sources.pdf_document_block`;
  each web snapshot as text -- ending in a cache breakpoint; then the **transcript** from the
  `transcript.final` events grouped by the observer's outline (`load_observer_snapshot(...,
  write_back=False)`: sections in outline order with their linked pages and concepts, unassigned
  segments last, each line `[<session> t=HH:MM:SS-HH:MM:SS] text`), the observer's notes, the
  **pending items** (`kind`, `text`, refs; open ones as doubts not to be resolved by guessing,
  closed ones labelled by `status` -- resolved by the student, auto-resolved by the observer or
  dismissed, which is no decision -- with their `resolution` when there is one), the digest, the current `apuntes.md` (keep its anchors) and the
  instruction, ending in a second breakpoint so re-asks read it all from the cache. At most
  `max_page_images` images (the API refuses more than 20 of this size per request) and
  `max_attachment_bytes` of base64 attachments (requests are capped at 32 MB) are sent; a PDF past
  the budget goes as its per-page extracted text; what was left out is in `EditorInput.omitted`.
  `record_content` is the message with every image/document replaced by
  `{"type": "image_ref"|"document_ref", "source_id": ...}`.
- **Answer**: plain text, the whole of `notes/apuntes.md` (`notes_text` drops a code fence around
  it). It is validated (`validate(text, mode, topic_source_resolver(...))`); a `max_tokens` stop or
  an empty answer is an error too. Failures are re-asked with the Spanish error list, at most
  `MAX_REASKS` (2) times, the conversation growing by the answer and the re-ask.
- **Valid**: `vault.write_notes` (removing any draft), `GitSync.checkpoint("Apuntes vN de
  <subject>/<topic>: <title>")` and `GitSync.create_notes_tag` -- `<subject>/<topic>/apuntes-vN`,
  `N` one past the highest, so a regeneration is the next version.
  **Still invalid**: `vault.write_notes_draft` (`notes/borrador.md`; `apuntes.md` left as it
  was), committed without a tag; `draft`, `errors` and a Spanish `warning` in the result.
- `GenerationResult` (also the `notes.generated` payload): `subject`, `topic`, `draft`, `path`
  (vault-relative), `version`, `tag`, `commit`, `attempts`, `errors`, `warning`, `model`.
- **Conversation** `conversations/editor.jsonl`: `context` (model, prompt hash, `detail`: reason
  `generate`, fidelity mode, cited-source ids, sessions, images, documents, omitted, whether a
  previous version and a digest were given), each `user` turn (record form), each `assistant`
  answer (content, usage), a `validation` per answer (`attempt`, `errors`) and `notes.generated`.
- Errors: `CostConfirmationRequiredError` past a cost cap without `confirm_over_cap` (nothing
  sent or written); `RefusalError` on `stop_reason: refusal`; any other `LLMError` once the
  client's retries are spent; the vault's errors for an unknown topic. Nothing is written then.
- Every call streams (the llm client always does), and is capped and recorded in the topic's
  ledger through the client's binding. There is no live text preview yet: `LLMClient` returns the
  final message only.
- Entry point: the server's `POST /api/subjects/{s}/topics/{t}/notes/generate`
  (`docs/modules/server.md`). The voice command "ya está, prepárame el tema" does not exist yet
  (the command grammar is stt's).
- `assemble_input(..., instruction=None)`: `instruction` replaces the closing request, so another
  task (the doubts resolution) reads the same, cached, input.

### Edit ops -- `edits.py`
Pure code (no vault, no LLM): how the editor changes notes it already wrote, by section anchor and
block number, instead of rewriting them. Meant for the edit loop (#63) as well.
- `EditOp` (flat, so it can be a strict tool input): `op` in `EDIT_OP_NAMES` --
  `replace_block` (`section`, `block`, `text`), `insert_after` (`section`, `block` -- `0` = before
  the first block --, `text`), `delete_block` (`section`, `block`), `replace_section` (`section`,
  `text`: the whole body; the heading and its anchor stay). `section` is the anchor without `#`;
  blocks are numbered from 1 within their section as the validator numbers them, and every number
  of one edit refers to the notes before any op of it. `text` is Markdown blocks with no section
  heading.
- `NewFootnote` (`label`, `definition` -- what follows `[^label]: `): appended to the run of
  definitions ending the document (one is started when there is none); the same label with the
  same definition is a no-op, with another definition an error.
- `apply_edits(notes, ops, footnotes=()) -> str`: every other byte unchanged; new blocks are
  separated by blank lines. Raises `EditError` (`errors`: Spanish, for re-asking) listing every
  unknown anchor, missing block, two ops on one block, `replace_section` mixed with other ops of
  its section, a heading inside a text or a clashing footnote label; nothing is applied then. The
  result is not validated here: callers run `validate`.
- `describe_sections(notes) -> str`: the block map (`#anchor -- heading`, then `bloque N (kind):
  opening words`) sent to the editor so it can address blocks.

### Doubts resolution -- `doubts.py`
The observer's pending doubts (#55) worked through after the notes exist, with the `editor` role
and the `editor_doubts` prompt, over `assemble_input` with a task instruction (so the sources are
read from the cache). Every call goes through `llm.structured` (strict tool) and is recorded in
`conversations/editor.jsonl` (`context` with `reason` `doubts_review`/`doubt_answer`, `user`,
`assistant`, `validation`, then `pending.reviewed` or `pending.resolved`).
- `await review_doubts(vault, subject, topic, *, client, sync, host=None, digest=None,
  confirm_over_cap=False, ...) -> ReviewResult` (tool `resolve_doubts`, `DoubtsReviewOutput`: one
  `DoubtDecision` per open doubt plus `footnotes`). `auto_resolve` needs a `resolution` and at
  least one `Evidence` (`source_id` from the catalogue, or `sessions/<id>#t=HH:MM:SS-HH:MM:SS` of a
  topic session, and a `quote`) and may carry `edits`; `ask` needs a `question` and 1-3
  `suggestions`, or for a `contradiction` at least two `options` (`SourceOption`: citable
  `source_id`, `says`), and no edits. Every open doubt must get one decision, and the edits of all
  auto-resolutions together must apply and leave notes that pass `validate`. A failing answer is
  sent back (a `tool_result` error with the Spanish list) at most `MAX_REASKS` (2) times; past that
  nothing is auto-resolved, every doubt becomes a question (the editor's own question when it was
  a valid one) and the result has a Spanish `warning`. No open doubt: no call. No notes yet:
  `NotesMissingError`. `ReviewResult`: `auto_resolved`, `asked` (ids), `notes_changed`,
  `session_id`, `commit`, `attempts`, `warning`, `model`.
- `await answer_doubt(vault, subject, topic, pending_id, answer, *, client, sync, ...) ->
  ResolutionResult` (tool `apply_decision`, `DecisionOutput`: `resolution`, `edits`,
  `footnotes`). `DoubtAnswer`: `suggestion` (1-based, into the latest question's suggestions),
  `answer` (free text, up to 2000 characters, alone or as a comment), `source_id` (one of the
  question's `options`: the source that is right in a contradiction; the others become
  `discarded`) and `keep_discarded` (only with `source_id`: the editor adds a note with the other
  version, cited to its source -- "En tus apuntes pone 1791"). An answer that does not fit is
  `InvalidAnswerError` before any call. The edits are checked like the review's and re-asked; past
  the re-asks the decision is still recorded (resolution = the student's decision), the notes
  untouched, with a `warning`. Without notes yet no call is made. The item's `resolution` is the
  editor's one-sentence summary.
- `await dismiss_doubt(vault, subject, topic, pending_id, *, sync, host=None) ->
  ResolutionResult`: closed as `dismissed`, no call.
- `list_doubts(vault, subject, topic) -> DoubtsQueue` (blocking, reads only): `open_count`,
  `current` (the first open doubt: the one to ask next, one at a time) and `items`, each a `Doubt`
  -- `item` (the observer's `PendingItem`), `question` (the latest `DoubtQuestion` of its id or
  merged ids: `question`, `suggestions`, `options`, `asked_at`) and `outcome` (`DoubtOutcome`, see
  below) --, open ones first.
- **Events** (ADR-0003). Closing a doubt writes an `observer.state_op` `resolve_pending` event
  (status `auto_resolved`, origin `editor`; or `resolved`/`dismissed`, origin `user`) -- what closes
  the item in the observer's fold -- followed by `PENDING_RESOLVED_KIND = "pending.resolved"`
  whose payload is the `DoubtOutcome` (`pending_id`, `status`, `resolution`, `evidence`, `answer`,
  `suggestion`, `chosen_source`, `discarded`, `keep_discarded`, `notes_changed`, `warning`); a
  question is `PENDING_QUESTION_KIND = "pending.question"` (origin `editor`, payload the
  `DoubtQuestion`). They go to a **review session**: a session of the topic started and ended at
  once for them (`vault.start_session`/`end_session`, no transcript, no lifecycle events), so they
  fold after every study session before it; it shows in the topic's session list with no
  minutes. While the topic has an unended session nothing is sent or written
  (`OpenSessionError`), since that session's later events would fold before the review's. Then
  `review/pending.yaml` and the snapshot are regenerated (`load_observer_snapshot`), and the notes
  (`vault.write_notes`, when edited) and the events are committed with a Spanish summary
  (`Dudas de <s>/<t> revisadas: ...`, `Duda resuelta en ...`, `Duda descartada en ...`); no notes
  tag is created.
- Errors (`DoubtError`, Spanish messages): `UnknownDoubtError`, `DoubtClosedError`,
  `InvalidAnswerError`, `OpenSessionError`, `NotesMissingError`; plus the llm errors as in
  `generate_notes`, with nothing written.
- Limitation: a student's answer is not a source of the catalogue, so the edits it leads to cite
  the sources the doubt is about (the page with the illegible word); an explanation found in no
  source needs `[^ia]` in `ampliado` or stays out of the notes in `estricto`.
- Entry points: the server's `GET/POST /api/subjects/{s}/topics/{t}/doubts...`
  (`docs/modules/server.md`).
