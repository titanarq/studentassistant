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
- Voice tutor (study mode): answer the student's questions about a topic from its notes and
  sources, with the refs.

## Public surface
What exists today, after issues #30, #61, #68, #63, #64, #69, #70, #65 and #313: the master notes format
of ADR-0005, in
`studentassistant.editor.notes_format` (never calls Claude, never writes or reads the vault
itself), "prepárame el tema", the first version of the notes, in
`studentassistant.editor.inputs` and `studentassistant.editor.generate`, the section-level edit
ops in `studentassistant.editor.edits`, the doubts resolution in
`studentassistant.editor.doubts`, the contradictions between sources in
`studentassistant.editor.contradictions`, the conversational revision of the notes in
`studentassistant.editor.revise`, the student's own edits in `studentassistant.editor.direct_edit`
(with the shared write lock of `studentassistant.editor.notes_lock`), the notes versions in
`studentassistant.editor.versions`,
"¿Por qué pusiste esto?" in `studentassistant.editor.explain`, the subject style guide in
`studentassistant.editor.style_guide` and the voice tutor in `studentassistant.editor.tutor`
(#82).

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
  | pasted image | `[Imagen pegada 1](../sources/images/img-001.png)` | `sources/images/img-001.png` | same |
  | AI | `[^ia]: Ampliado por la IA: no está en tus fuentes` | -- | -- |
  | student | `[^est]: Escrito por el estudiante` | -- | -- |

  `[^est]` marks a block the student wrote in the document themselves (#313); it is allowed in
  both fidelity modes, and the server adds it on a student save to a block citing nothing (the
  editor may later replace it with a source footnote). A pasted image is a source of kind
  `images` (`vault.put_pasted_image`); these two follow the ADR-0005 proposal of epic #311.

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
- `notes_revision(text) -> str` -- the content revision token of the notes: the SHA-256 hex of
  the UTF-8 text. Every read (`GET .../notes`) and write result (`StudentEditResult`,
  `RevisionResult`, `UndoResult`, `RestoreResult`, `GenerationResult`) carries it, so two writers
  (the student and the editor) never lose each other's update.
- Provenance: `Provenance` (`kind`, `text`, `source_id`, `path`, `link`, `definition(label)`);
  builders `page_provenance("notes"|"book", number, extension="jpg")`,
  `pdf_provenance(number, extension="pdf", page=None)`, `web_provenance(file_name, title=None)`,
  `image_provenance(number, extension="png")`,
  `transcript_provenance(session_id, start_seconds, end_seconds)`, `IA_PROVENANCE` and
  `EST_PROVENANCE` (kind `student`);
  `parse_provenance(definition)` raises `ProvenanceError` (Spanish message) for anything but
  `[^ia]`, `[^est]` or one relative link to a known source shape.
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
`digest(vault, subject, topic) -> str | None` reads the topic digest (`state/digest.md`; the
server passes `observer.topic_digest`), `on_event(kind, payload)` an async sink for the `notes.generated` event.

- **Input** (`assemble_input(vault, subject, topic, *, prompt, digest=None, max_page_images,
  max_attachment_bytes) -> EditorInput`, blocking), stable parts first for caching. System: the
  `editor_generate` prompt, then the topic block (subject, title, fidelity mode from `topic.yaml`
  -- anything but `ampliado` is `estricto` --, the subject's `style_guide`), cached by the llm
  client. First user message: the **catalogue** of citable sources with the exact footnote
  definition of each (`[Apuntes, página 1](../sources/notes/page-001.jpg)`; a PDF page as
  `[PDF, página 84](../sources/pdf/page-001.pdf#page=3)`: the link counts pages of the stored file,
  the text shows `sources.original_page`; a book page as `[Libro «<título>», página 83](...)`,
  `page_citation_text`: the page number printed on it or said, `book_page` in its sidecar (#58),
  else its stored number, and the topic's book title when `vault.get_book` has one, also in the
  heading `## Páginas del libro «<título>»`), grouped by **source role** (`source_role(kind)`,
  `CitableSource.role`): `### Apuntes del estudiante` (kind `notes`, `STUDENT_SOURCE_KINDS`: they
  give the notes their structure and emphasis; the transcript is the student's too) and
  `### Fuentes complementarias` (book, PDF, web: they complement, each cited with its own
  footnote), followed by `DISAGREEMENT_RULE` (a disagreement between sources is never settled
  silently); the **sources**, each labelled the same way (`STUDENT_LABEL` under the notes pages'
  heading, `SUPPLEMENTARY_LABEL` under the book's and in each PDF and web source) -- each notes and book page as its
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
  the budget goes as its per-page text (`sources.read_pdf_page_text`: the extracted
  `page-NNN.pKKK.txt`, or for a scanned page its vision transcription `page-NNN.pKKK.md`, headed
  `página escaneada, transcrita`; a scanned page not transcribed yet is left out); what was left out is in `EditorInput.omitted`.
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
- **Contradictions** (`detect_contradictions=True`, `host=None`): after a valid version is written
  and when the topic has at least two citable sources (sessions included), `generate_notes` calls
  `contradictions.detect_contradictions` (below) with the same client; the ids raised are the
  result's `contradictions`. An unended session of the topic (`OPEN_SESSION_WARNING`, no call) or
  any failure of the detection (`DETECTION_FAILED_WARNING`, logged) never loses the written notes:
  it is the result's `warning`. A draft is not searched.
- `GenerationResult` (also the `notes.generated` payload): `subject`, `topic`, `draft`, `path`
  (vault-relative), `version`, `tag`, `commit`, `attempts`, `errors`, `warning`, `contradictions`,
  `model`, `revision` (of `apuntes.md` after it; for a draft, of the notes left as they were).
- **Conversation** `conversations/editor.jsonl`: `context` (model, prompt hash, `detail`: reason
  `generate`, fidelity mode, cited-source ids, sessions, images, documents, omitted, whether a
  previous version and a digest were given), each `user` turn (record form), each `assistant`
  answer (content, usage), a `validation` per answer (`attempt`, `errors`), the detection's own
  records (below) and `notes.generated`.
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
  `text`: the whole body; the heading and its anchor stay), `move_section` (`section`, `after`:
  the section and its subsections -- the deeper headings after it -- go after section `after` and
  its subsections, or first when `after` is empty or null; moves apply in the order given, after
  the block ops, and the final run of footnote definitions stays last), `add_section` (`after`,
  `level` 2-6, `title`, `anchor`, `text`: a new section `## title {#anchor}` with `text` as its
  blocks -- none when empty -- goes after section `after` and its subsections, or first when
  `after` is empty; `section` is not used; `after` may name a section an earlier `add_section` of
  the same edit added; new sections are added after the block ops and before the moves, so a move
  may name one; the final footnotes run stays last, and a notes text that is only `# Tema` grows
  into a document this way). `section` is the anchor without `#`;
  blocks are numbered from 1 within their section as the validator numbers them, and every number
  of one edit refers to the notes before any op of it. `text` is Markdown blocks with no section
  heading.
- `NewFootnote` (`label`, `definition` -- what follows `[^label]: `): appended to the run of
  definitions ending the document (one is started when there is none); the same label with the
  same definition is a no-op, with another definition an error. A definition an edit orphans (a
  label cited before and nowhere after, e.g. the `[^ia]` of a deleted paragraph) is removed.
- `apply_edits(notes, ops, footnotes=()) -> str`: every other byte unchanged; new blocks are
  separated by blank lines. Raises `EditError` (`errors`: Spanish, for re-asking) listing every
  unknown anchor, missing block, two ops on one block, `replace_section` mixed with other ops of
  its section, a heading inside a text, a clashing footnote label, a section moved twice, after
  itself or one of its subsections, or after an unknown anchor, and for `add_section` an anchor
  that is missing, malformed or already taken, an empty title, a level outside 2-6 or an unknown
  `after`; nothing is applied then. The
  result is not validated here: callers run `validate`.
- `describe_sections(notes) -> str`: the block map (`#anchor -- heading`, then `bloque N (kind):
  opening words`) sent to the editor so it can address blocks; notes without sections say so and
  point at `add_section`.

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
  once for them (`vault.start_session(..., kind="review")`/`end_session`, no transcript, no
  lifecycle events), so they fold after every study session before it; its `kind` keeps it out
  of the topic's study sessions (#191): the session list labels it «Revisión de dudas», and the
  topic list's `last_session_at_ms` and the topic card's counts leave it out. While the topic has an unended session nothing is sent or written
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

### Contradictions between sources -- `contradictions.py`
The editor never chooses silently between two sources that disagree (1769 in the notes, 1765 in
the book): the `editor_generate` and `editor_revise` prompts tell it to write the version of the
student's notes and the other one, each cited, and the disagreement is raised as a
`contradiction` pending doubt the student settles through the doubts flow.
- `await detect_contradictions(vault, subject, topic, *, client, sync, host=None, digest=None,
  confirm_over_cap=False, clock=..., max_page_images=20, max_attachment_bytes=24 MiB) ->
  ContradictionsResult`: role `editor`, prompt `editor_contradictions`, over `assemble_input` with
  a task instruction (listing the topic's known `contradiction` items, open or closed, so they are
  not repeated), strict tool `report_contradictions` through `llm.structured`
  (`ContradictionsOutput`: `contradictions`, each a `Contradiction` -- Spanish `text`, `question`,
  `sides`: `ContradictionSide` with a citable `source_id` from the catalogue or a
  `sessions/<id>#t=HH:MM:SS-HH:MM:SS` span of a topic session, and what it `says`).
- **Checks** (`contradiction_errors`): a non-empty text and question, every side citable and
  saying something, at least two distinct sources. A failing answer is sent back (a `tool_result`
  error with the Spanish list) at most `MAX_REASKS` (2) times; past that the invalid
  contradictions are dropped (`dropped`), the valid ones of the last answer kept, with a Spanish
  `warning`.
- **Written**: each accepted contradiction is an `observer.state_op` `add_pending` event (origin
  `editor`, `kind: contradiction`, `pending_id` `contradiccion-<hex>`, `source_refs` every side's
  source) followed by a `pending.question` (`DoubtQuestion` whose `options` are the sides), in a
  review session as `doubts.py` writes its events, so `list_doubts` shows it with its options and
  `answer_doubt(..., DoubtAnswer(source_id=...))` settles it at once (or `review_doubts` asks it
  again). A contradiction whose set of sources equals an existing `contradiction` item's (open or
  closed) or an earlier one of the same answer is not added (`duplicates`). Then
  `review/pending.yaml` and the snapshot are regenerated and one commit is made
  (`Contradicciones en <s>/<t>: N contradicción(es) nueva(s) entre fuentes`). Nothing accepted: no
  event, no commit.
- `ContradictionsResult`: `subject`, `topic`, `raised` (pending ids), `duplicates`, `dropped`,
  `session_id`, `commit`, `attempts`, `warning`, `model`.
- **Conversation** `conversations/editor.jsonl`: `context` (reason `contradictions`), `user`,
  `assistant`, `validation` per call, then `contradictions.detected` (the result).
- Errors: `OpenSessionError` (an unended session; checked before any call, nothing sent), plus the
  llm errors as in `generate_notes`, with nothing written but the conversation records.
- Entry points: `generate_notes` (above); no endpoint of its own -- the raised items surface
  through the doubts endpoints and UI.

### Revising the notes in conversation -- `revise.py`
The student talks to the editor about notes it already wrote ("demasiado resumido", "pon un
ejemplo", "no inventes", "usa la explicación del libro"), role `editor`, prompt `editor_revise`,
over `assemble_input` with a task instruction (the sources come from the cache) plus, uncached,
the last `HISTORY_TURNS` (12) turns, the block map (`describe_sections`) with the current fidelity
mode, and the student's message.
- **The latest version** (#313): a turn holds no lock across its Claude call. It remembers the
  notes its block map was built from and applies its change under the topic's short write lock
  (`editor.notes_lock.holding_notes`, a per-topic `threading.Lock` taken in the worker thread,
  shared with the student's save); when the stored notes changed meanwhile (the student saved), the
  ops are not applied: the editor is re-asked with the new block map and the note
  `NOTES_CHANGED_NOTE` ("el estudiante ha cambiado los apuntes mientras respondías"), counting
  against `MAX_REASKS`; past them nothing is applied and the `warning` says the notes changed.
  Turns of one topic still run one at a time (the server's notes lock).
- **A growing document**: a topic without `apuntes.md` is revised from `# <topic title>` (the
  first applied change creates the file, typically with `add_section`); `NotesMissingError` is no
  longer raised by a turn.
- `await revise_notes(vault, subject, topic, message, *, client, sync, on_reply=None,
  on_event=None, digest=None, confirm_over_cap=False, clock=..., ...) -> RevisionResult`. The
  editor first writes its Spanish reply as text -- streamed through `LLMClient.create(on_text=...)`
  to `on_reply("reply.delta", {"text", "attempt"})` -- and then, if anything changes, calls the
  strict tool `apply_edits` once (`EditsOutput`: `ops` (the `EditOp`s above), `footnotes`,
  `summary` (one Spanish sentence), `fidelity_mode` (`estricto`/`ampliado`, only when the student
  sets it: recorded in `topic.yaml` with `vault.set_fidelity_mode`), `proposed_style_rules` (when
  an instruction looks general -- "me gustan las tablas para comparar", "siempre un ejemplo" --:
  rules proposed for the subject's style guide, **not written**; the reply asks the student) and
  `confirmed_style_rules` (a rule proposed in an earlier turn and still pending that the student
  confirms in the chat: appended with `style_guide.append_rules`)). No tool call: a chat-only
  turn, nothing written but the conversation.
- **Checks**: the ops must apply (`apply_edits`) and the edited notes must pass `validate` in the
  mode the turn leaves (so "no inventes" must also remove every `[^ia]` block); a `summary` is
  required when something is applied, at most 5 proposed and 5 confirmed style rules of 300
  characters, and a confirmed rule must be a pending proposal of an earlier turn. A failure is sent back as a `tool_result`
  error with the Spanish list, at most `MAX_REASKS` (2) times, and `on_reply("reply.restart",
  {"attempt"})` tells the caller to drop the reply streamed so far. Past the re-asks nothing is
  applied and the result has `errors` and a Spanish `warning`.
- **Applied**: the notes (`vault.write_notes`), `topic.yaml` and `subject.yaml` as needed, then
  `GitSync.checkpoint("Apuntes de <s>/<t> revisados: <summary>")` at once; no notes tag.
  `on_event("notes.edited", payload)` gets the result without the `notes` text.
- `RevisionResult`: `subject`, `topic`, `message`, `reply`, `applied`, `summary`, `ops`,
  `footnotes`, `fidelity_mode` (the new one, when changed), `style_rules` (added to the guide:
  the confirmed ones), `proposed_style_rules` (proposed, minus those the guide has), `notes_changed`,
  `changed_sections` (anchors the ops touched; a new section's `anchor`), `diff` (unified diff of
  `apuntes.md`), `notes` (the new text when changed), `paths` (vault-relative files the commit
  changed), `commit`, `revision` (of the notes after the turn, `None` while there are none),
  `attempts`, `errors`, `warning`, `model`.
- `await undo_last_revision(vault, subject, topic, *, sync, on_event=None) -> UndoResult`: the
  latest applied turn not yet undone is reverted with `GitSync.revert_paths(commit, paths, ...)`
  (a `git revert` of that commit restricted to its `paths`, so the ledger and conversation lines it
  carried stay) and committed as `Deshecho en <s>/<t>: <summary>`; `notes.undone` to `on_event`.
  Undoing again goes one turn further back. `UndoResult`: `undone_commit`, `summary`, `commit`,
  `notes_changed`, `diff`, `notes`, `paths`, `revision`. No Claude call. A student save after the
  turn changes `apuntes.md`, so undoing it then is an `UndoConflictError`.
- `chat_history(vault, subject, topic) -> ChatHistory` (blocking, reads only): `turns`
  (`ChatTurn`: `time`, `kind` -- `revise`, or `explain` for a "¿Por qué?" answer --, `message`,
  `reply`, `applied`, `summary`, `changed_sections`, `commit`, `undone`, `warning`, `refs`,
  `proposed_style_rules` -- the turn's proposals the subject's guide does not have yet, also shown
  to the editor in the conversation so far) and `can_undo`. The explanations are also in the
  conversation the editor is given on a turn, and so are the student's own edits (the
  `student_edit` records of `direct_edit.py`, as "El estudiante editó él mismo los apuntes
  (#anchors)" plus the diff, cut at `STUDENT_EDIT_DIFF_CHARS`); `chat_history` leaves those out.
- **Conversation** `conversations/editor.jsonl`: `context` (reason `revise`), `user`, `assistant`,
  `validation` per call, then one `revision` record per turn (the `RevisionResult`) and one
  `notes.undone` per undo (the `UndoResult`) -- what `chat_history` and the undo read.
- Errors (`RevisionError`, Spanish): `InvalidMessageError` (empty, or over 4000 characters),
  `NothingToUndoError`, `UndoConflictError` (a file of the
  turn changed afterwards -- a later turn, a regeneration, a doubt's edit); plus the llm errors as
  in `generate_notes`, with nothing written but the conversation records.
- Limitations: a turn is committed as soon as it is applied; when the sync loop happened to commit
  the files first, `commit` is `None` and that turn cannot be undone. Nothing here streams to the
  web itself: that is the server's `POST .../notes/chat` (SSE, `docs/modules/server.md`).

### The student editing the document -- `direct_edit.py`
The web's document editor (#316) saves the whole `apuntes.md` the student ended up with. No Claude
call.
- `normalise_student_text(text) -> str` (pure; unchanged text comes back byte for byte): a section
  heading without anchor gets `{#slug}` (`vault.slugs.slugify` of its title, `-2`, `-3`... when
  taken; `seccion` for a title without letters); an image block linking
  `../sources/images/img-NNN.<ext>` gets the footnote of that image (`[^img001]: [Imagen pegada
  1](../sources/images/img-001.png)`, unless one of its footnotes already cites it); any other
  paragraph, list-item group or table without a footnote reference gets `[^est]` (at the end of
  its last line; in a table, inside the last cell of the last row); every footnote definition
  not in the document's final run is moved to it, and the new definitions are appended there.
  What it cannot fix (an undefined reference, a repeated anchor) is left for `validate`.
- `await save_student_edit(vault, subject, topic, text, base_revision, *, sync, on_event=None,
  clock=...) -> StudentEditResult`: normalises, then under the topic's write lock
  (`notes_lock.holding_notes`) compares `base_revision` with the stored notes' revision (`None`:
  the topic has no notes, so a document can start from nothing) -- a mismatch is
  `NotesChangedError` (`text`, `revision`: the current notes; `""`/`None` when there are none) --,
  validates in the topic's fidelity mode (`[^est]` passes in both) -- failures are
  `StudentEditInvalidError` (`errors`, Spanish) --, then writes (`vault.write_notes`) and commits
  at once (`Apuntes de <s>/<t> editados por el estudiante`). An empty text or one over
  `MAX_NOTES_CHARS` is `StudentEditInvalidError` too. The same text as stored writes nothing
  (`notes_changed: false`, no record, no event).
- `StudentEditResult`: `subject`, `topic`, `revision`, `commit`, `diff`, `notes` (as saved),
  `changed_sections` (anchors whose section was added, removed, changed or moved:
  `versions.compare_notes`), `normalised` (the server changed the text), `notes_changed`.
- Written with it: a `student_edit` record in `conversations/editor.jsonl` (the result without
  `notes`), read by the editor's next turn, and `on_event("notes.edited", {...result without
  notes, "origin": "user"})`.
- Entry point: the server's `PUT /api/subjects/{s}/topics/{t}/notes` (`docs/modules/server.md`).

### Notes versions -- `versions.py`
A version is a `<subject>/<topic>/apuntes-vN` tag (made by "prepárame el tema" and by a restore),
read through `GitSync.list_notes_tags` and `GitSync.read_file_at`. No Claude call.
- `list_versions(vault, subject, topic, *, sync) -> NotesVersions` (blocking, reads only):
  `versions` oldest first (`NotesVersion`: `version`, `tag`, `commit`, `tagged_at`, `message`,
  `current` -- the current `apuntes.md` is exactly its text), `has_notes`,
  `changed_since_latest` (revisions or doubts edited the notes after the latest tag).
- `read_version(vault, subject, topic, version, *, sync) -> VersionText` (`text` as tagged).
- `diff_versions(vault, subject, topic, from_version, to_version=None, *, sync) -> VersionDiff`:
  `to_version=None` compares with the current `apuntes.md`. `compare_notes(before, after)` is the
  pure part. **By section**: sections are matched by anchor (an anchorless one by position), the
  preamble is the section with key `PREAMBLE_KEY` (`""`); each `SectionDiff` has `status`
  (`added`, `removed`, `changed`, `unchanged`), `moved` (its place among the sections both sides
  share changed), `renamed` (heading title changed), both titles, `level` and a unified `diff` of
  its heading and blocks. Sections come in the newer side's order, a removed one right after the
  section it followed. The run of footnote definitions is left out of the sections and compared
  by label (`FootnotesDiff`: `added`, `removed`, `changed`); `diff` is the whole-document unified
  diff, `identical` whether the texts are equal.
- `await restore_version(vault, subject, topic, version, *, sync, on_event=None) ->
  RestoreResult`: writes that version's text as `apuntes.md` (`vault.write_notes`, which drops a
  draft), commits it (`Apuntes vN de <s>/<t>: restaurada la versión K`) and tags it as the next
  version -- nothing is rewound; every version stays. The restored text is validated against
  today's sources and fidelity mode: `errors` and a Spanish `warning` report what no longer holds
  (a purged source), but the restore is done anyway. `on_event("notes.restored", payload)` gets
  the result without `notes`. `RestoreResult`: `restored_version`, `version`, `tag`, `commit`,
  `path`, `diff` (from the notes before), `notes`, `revision`, `errors`, `warning`.
- Errors (`VersionError`, Spanish): `UnknownVersionError` (no such version, or its file missing at
  the tag), `NothingToRestoreError` (the current notes already are that version; nothing
  written), `VersionError` for a diff against notes that do not exist.
- A restore changes `apuntes.md`, so undoing an earlier chat turn afterwards is an
  `UndoConflictError`, like after a regeneration.
- Entry points: the server's `GET/POST /api/subjects/{s}/topics/{t}/notes/versions...`
  (`docs/modules/server.md`).

### "¿Por qué pusiste esto?" -- `explain.py`
The editor explains one block of the notes from the sources it cites, looked at again; role
`editor`, prompt `editor_explain`, no tool, nothing of the notes changed.
- `BlockAnchor` (`section`: anchor without `#`, `None` for the preamble; `block`: 1-based, as the
  validator numbers blocks; `quote`: the text the student sees or its beginning, up to
  `MAX_QUOTE_CHARS` (2000)). `find_block(document, anchor) -> (section, number, Block)`: the
  numbered block when it exists and matches the quote (compared as letters and digits only,
  casefolded, footnote references, `[[?...]]` marks and list markers dropped), else the first
  block of the section, then of the notes, holding the quote. `BlockNotFoundError` (an
  `InvalidMessageError`, Spanish) when nothing matches or the block is a title, rule or footnotes.
- `await explain_block(vault, subject, topic, anchor, *, client, sync=None, on_reply=None,
  confirm_over_cap=False, clock=..., max_page_images=20, max_attachment_bytes=24 MiB) ->
  ExplanationResult`. The input (`assemble_explanation`, blocking; small, not the whole topic):
  system = the prompt and the topic block of `assemble_input`; one user message with the block,
  its footnote definitions (and the labels that cite nothing usable), its whole section as
  context, then every cited source -- a notes/book page as its transcription **and always its
  image** (the cropped page first), a PDF page as its extracted text `page-NNN.pKKK.txt`, or when that
  is empty (a scanned page) its vision transcription `page-NNN.pKKK.md` (`sources.read_pdf_page_text`;
  the stored PDF as a document when there is neither), a web snapshot as its text (up to 30,000
  characters), a transcript span as that session's segments within 30 s of it (the ones inside
  marked `<- citado`), `[^ia]` said to be the AI's, a source no longer in the topic said so --,
  the topic's pending items (their decisions explain choices) and the question. The answer streams
  as `on_reply("reply.delta", {"text", "attempt": 1})`.
- `ExplanationResult`: `subject`, `topic`, `section`, `block`, `block_text`, `question` (`¿Por qué
  pusiste esto? (en la sección #<anchor>) «<excerpt>»`), `reply`, `refs` (`ChatRef`: `label`,
  `kind`, `text`, `source_id`, `path`, one per footnote the block cites, in order), `images`,
  `omitted`, `warning` (an empty or cut answer), `model`.
- **Conversation** `conversations/editor.jsonl`: `context` (reason `explain`, section, block,
  sources, images, documents, omitted), `user` (record form), `assistant`, then one
  `explanation` record (the `ExplanationResult`) -- what `chat_history` reads as a `kind`
  `explain` turn; `sync.note_change()` lets the sync loop commit it.
- Errors: `NotesMissingError` (no notes), `BlockNotFoundError`; `RefusalError` (the records of
  the call kept, no `explanation`), `CostConfirmationRequiredError` and the llm errors as in
  `generate_notes`.
- Entry point: the server's `POST .../notes/why` (SSE, `docs/modules/server.md`).

### The subject style guide -- `style_guide.py`
The student's general preferences for a subject, kept in `subjects/<s>/subject.yaml`
(`style_guide`, free text) and given to the editor in the topic block of `assemble_input` for
every topic of the subject (generation, revision, doubts). Blocking functions; every write goes
through `vault.set_style_guide` and is committed at once.
- Rules: `parse_rules(text)` -- one per non-blank line, a leading `- `/`* `/`• ` dropped, spaces
  collapsed (`normalize_rule`); `format_rules(rules)` -- `- <rule>` lines, or `None`.
- `read_style_guide(vault, subject) -> StyleGuide` (`subject`, `rules`, `added`, `commit`).
- `add_style_rules(vault, subject, rules, *, sync) -> StyleGuide`: the student confirms proposed
  rules; each new one (ignoring case) is appended as a `- rule` line after the text as it was, and
  committed as `Guía de estilo de <s>: «rule»; ...`; nothing new, nothing written.
  `append_rules(vault, subject, rules) -> added` is the same without the checks or the commit
  (the revision turn commits it with the notes).
- `replace_style_rules(vault, subject, rules, *, sync) -> StyleGuide`: the whole list (edit,
  delete, reorder; `[]` clears), written as `- rule` lines and committed as `Guía de estilo de <s>
  editada`; the same rules as now write nothing, so a free-text guide is not reformatted.
- Errors: `InvalidStyleRuleError` (`StyleGuideError`, Spanish): an empty rule, one over
  `MAX_RULE_CHARS` (300), or more than `MAX_RULES` (50) in the guide; nothing written. The vault's
  `SubjectNotFoundError` for an unknown subject.
- Entry points: the server's `GET/PUT /api/subjects/{s}/style-guide` and `POST
  /api/subjects/{s}/style-guide/rules` (`docs/modules/server.md`); a confirmation said in the chat
  goes through `revise_notes` instead.

### The voice tutor -- `tutor.py`
Study mode (#82): the student asks about a topic out loud -- "¿qué era la derivada?", "ponme un
ejemplo", "¿y eso por qué?" -- and the editor answers from what the topic already has; role
`editor`, prompt `editor_tutor`, no tool, nothing of the notes changed.
- `await ask_tutor(vault, subject, topic, question, *, client, sync=None, on_reply=None,
  digest=None, confirm_over_cap=False, clock=..., max_page_images=20, max_attachment_bytes=24 MiB)
  -> TutorAnswer`. The input is `assemble_input` (catalogue, sources, transcript, doubts'
  decisions, current notes: the cached prefix of the revision chat) with `TUTOR_INSTRUCTION`,
  then, uncached, the last `HISTORY_TURNS` (6) questions and answers and the question (spaces
  collapsed). The answer is Spanish plain text meant to be read aloud (short, no Markdown, formulas
  in words), streamed as `on_reply("reply.delta", {"text", "attempt": 1})`. It cites the current
  notes' footnote labels (`[^p4]`) after what it takes from them, says so when something is not in
  the notes nor the sources, and adds outside knowledge only in `ampliado`, saying it is not from
  the sources.
- `TutorAnswer`: `subject`, `topic`, `question`, `reply` (with the `[^label]` marks), `refs`
  (`ChatRef` per label the reply cites that the notes define with a usable provenance, in order
  of first citation: `cited_refs(document, reply)`), `warning` (empty or cut answer), `model`.
- `tutor_history(vault, subject, topic) -> TutorHistory` (blocking, reads only): `turns`
  (`TutorTurn`: `time`, `question`, `reply`, `refs`, `warning`), oldest first.
- **Conversation** `conversations/tutor.jsonl`, apart from `editor.jsonl` (the editor chat's
  history and undo never see the tutor): `context` (reason `tutor` plus the input summary),
  `user`, `assistant`, then one `tutor.answer` record (the `TutorAnswer`); `sync.note_change()`
  lets the sync loop commit it.
- Errors: `InvalidMessageError` (empty, or over `MAX_QUESTION_CHARS` = 1000), `NotesMissingError`
  (nothing sent); `RefusalError` (the records of the call kept, no `tutor.answer`),
  `CostConfirmationRequiredError` and the llm errors as in `generate_notes`.
- Entry point: the server's `GET/POST /api/subjects/{s}/topics/{t}/tutor` (SSE,
  `docs/modules/server.md`).
