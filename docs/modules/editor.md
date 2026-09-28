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
- Doubts resolution: auto-resolve pending items with cited evidence, ask the rest one by one
  (in the workspace chat, #325), record decisions. Doubts never go into the notes.
- "¿Por qué pusiste esto?": explain a paragraph from its cited sources.
- Style guide learning per subject; notes versions (git tags) and diffs.
- Voice tutor (study mode): answer the student's questions about a topic from its notes and
  sources, with the refs; in writing too, for the study screen's question chat, citing sections.

## Public surface
What exists today, after issues #30, #61, #68, #63, #64, #69, #70, #65, #313, #325 and #326: the master notes format
of ADR-0005, in
`studentassistant.editor.notes_format` (never calls Claude, never writes or reads the vault
itself), "prepárame el tema", the first version of the notes, in
`studentassistant.editor.inputs` and `studentassistant.editor.generate`, the section-level edit
ops in `studentassistant.editor.edits`, the doubts resolution in
`studentassistant.editor.doubts`, the contradictions between sources in
`studentassistant.editor.contradictions`, the conversational revision of the notes in
`studentassistant.editor.revise`, the incremental incorporation of a few sources per request
and the source states in `studentassistant.editor.incorporate`, the student's own edits in `studentassistant.editor.direct_edit`
(with the shared write lock of `studentassistant.editor.notes_lock`), the notes versions in
`studentassistant.editor.versions`,
"¿Por qué pusiste esto?" in `studentassistant.editor.explain`, the subject style guide in
`studentassistant.editor.style_guide` and the voice tutor and the study screen's question chat in
`studentassistant.editor.tutor` (#82, #334), the app feedback both chats record in
`studentassistant.editor.feedback` (#472), and cropping a region of a stored page image in
`studentassistant.editor.crop` (#484).

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
  | cropped image | `[Imagen recortada 2](../sources/images/img-002.jpg)` | `sources/images/img-002.jpg` | same |
  | AI | `[^ia]: Ampliado por la IA: no está en tus fuentes` | -- | -- |
  | student | `[^est]: Escrito por el estudiante` | -- | -- |

  `[^est]` marks a block the student wrote in the document themselves (#313); it is allowed in
  both fidelity modes, and the server adds it on a student save to a block citing nothing (the
  editor may later replace it with a source footnote). A pasted image is a source of kind
  `images` (`vault.put_pasted_image`); these two follow the ADR-0005 proposal of epic #311. A
  region cropped from a stored page (`editor.crop`, #484) is an `images` source too, with the same
  source-id shape and the distinct text «Imagen recortada N» (`IMAGE_CROP_TEXT`,
  `cropped_image_provenance(number, extension="jpg")`, next to `IMAGE_TEXT` /
  `image_provenance`).

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
- `validate(..., editor_written=True, previous=None)` (#325): a write of the editor (generation,
  revision turn, doubt answer, a review's auto-resolution; incorporation, #326, reuses it) also
  reports a `[[?...]]` mark (`has_doubt_mark`) in every block the write adds or changes --
  **doubts never go into the notes** --, naming the section and block in Spanish. A block whose
  text is exactly one of `previous`'s (the notes before the write; a multiset, block for block)
  keeps its legacy marks until it is touched, so old notes stay editable. The student's saves
  (`direct_edit.py`) are validated without it.
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
  max_attachment_bytes) -> EditorInput`, blocking), stable parts first for caching. Captures set aside
  by triage (`sources.set_aside_ids`, #324) are left out of the catalogue and the sources, and so
  are the open pending items whose page refs are all set-aside captures (flagged ones stay in). System: the
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
  silently nor written as both versions: the notes carry the student's notes version and the
  disagreement is a `contradiction` doubt with the sides as options; no marks, alternative
  readings or disagreement lists in the notes, #325); the **sources**, each labelled the same way (`STUDENT_LABEL` under the notes pages'
  heading, `SUPPLEMENTARY_LABEL` under the book's and in each PDF and web source) -- each notes and book page as its
  transcription (`sources/<kind>/page-NNN.md`, #50, or a sidecar `transcription` string) plus its
  image (the cropped `page-NNN.page.jpg`, else the still) when `needs_image(transcription, meta)`:
  no transcription, a scheme (mermaid block, nested list, arrow), an uncertain word `[[?...]]` or a
  sidecar `transcription_confidence` below 0.8 (a source may keep marks; only the notes may not);
  each stored PDF as `sources.pdf_document_block`;
  each web snapshot as text -- ending in a cache breakpoint; then the **transcript** from the
  `transcript.final` events grouped by the observer's outline (`load_observer_snapshot(...,
  write_back=False)`: sections in outline order with their linked pages and concepts, unassigned
  segments last, each line `[<session> t=HH:MM:SS-HH:MM:SS] text`), the observer's notes, the
  **pending items** (`kind`, `text`, refs; open ones as doubts not to be resolved by guessing
  nor marked in the notes,
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
  it), then optionally one call of the strict tool `report_doubts` (`DOUBTS_TOOL`, `DoubtsReport`:
  `doubts`, a list of `EditorDoubt`, below), offered with `tool_choice: auto`. It is validated
  (`validate(text, mode, topic_source_resolver(...), editor_written=True, previous=<the notes
  before>)`, plus `editor_doubt_errors`); a `max_tokens` stop or an empty answer is an error too.
  Failures are re-asked with the Spanish error list (as the `tool_result` of a tool call, when
  there was one), at most `MAX_REASKS` (2) times, the conversation growing by the answer and the
  re-ask.
- **Doubts** (#325): with a valid version, the reported doubts become pending items with their
  questions (`doubts.raise_doubts`, below; `live` is the server's sink into the topic's live
  session); their ids are the result's `doubts`. Failing to record them is only a `warning`.
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
  `doubts`, `model`, `revision` (of `apuntes.md` after it; for a draft, of the notes left as they were).
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
  (`docs/modules/server.md`) with `[editor] prepare_mode = "single"`; by default (`batched`) the
  server runs `incorporate.incorporate_pending` instead (below). The voice command "ya está, prepárame el tema" does not exist yet
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
- `describe_sections(notes, settled=()) -> str`: the block map (`#anchor -- heading`, then
  `bloque N (kind): opening words`) sent to the editor so it can address blocks; notes without
  sections say so and point at `add_section`. A block whose key is in `settled`
  (`reviewed.settled_blocks`) is marked `[revisado]` (`SETTLED_MARK`), with one legend line
  (`SETTLED_LEGEND`) at the end. Every block map the editor sees (revision turns, incorporations,
  the doubts review and answer, and their re-asks after a student save) passes the topic's
  settled blocks.

### Doubts resolution -- `doubts.py`
The observer's pending doubts (#55) worked through after the notes exist, with the `editor` role
and the `editor_doubts` prompt, over `assemble_input` with a task instruction (so the sources are
read from the cache). Every call goes through `llm.structured` (strict tool) and is recorded in
`conversations/editor.jsonl` (`context` with `reason` `doubts_review`/`doubt_answer`, `user`,
`assistant`, `validation`, then `pending.reviewed` or `pending.resolved`).
- `await review_doubts(vault, subject, topic, *, client, sync, host=None, digest=None,
  confirm_over_cap=False, ..., pending_ids=None, live=None) -> ReviewResult` (`pending_ids`: only
  those open doubts, as the chat reviews the ones it is about to ask) (tool `resolve_doubts`, `DoubtsReviewOutput`: one
  `DoubtDecision` per open doubt plus `footnotes`). `auto_resolve` needs a `resolution` and at
  least one `Evidence` (`source_id` from the catalogue, or `sessions/<id>#t=HH:MM:SS-HH:MM:SS` of a
  topic session, and a `quote`) and may carry `edits`; `ask` needs a `question` and 1-3
  `suggestions`, or for a `contradiction` at least two `options` (`SourceOption`: citable
  `source_id`, `says`), and no edits. Every open doubt must get one decision, and the edits of all
  auto-resolutions together must apply and leave notes that pass `validate`. A failing answer is
  sent back (a `tool_result` error with the Spanish list) at most `MAX_REASKS` (2) times; past that
  nothing is auto-resolved, every doubt becomes a question (the editor's own question when it was
  a valid one) and the result has a Spanish `warning`. No open doubt: no call. No notes yet:
  `NotesMissingError`. `ReviewResult`: `auto_resolved`, `asked` (ids), `notes_changed`, `summary`
  (one Spanish line reporting the auto-resolutions, "He resuelto con tus fuentes 2 dudas: ...",
  for the chat), `revision`, `session_id`, `commit`, `attempts`, `warning`, `model`. The edits are
  applied under the topic's write lock on the latest notes, as the answer's are (below); since
  #410 the notes write and its own checkpoint (`Apuntes de <s>/<t>: dudas resueltas con fuentes`,
  an answer's `...: duda resuelta: <doubt>`) are one locked step (`checkpointing`, below), and the
  events are committed after it; `commit` is the events' commit, else the notes'.
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
  editor's one-sentence summary. **On the latest notes** (#325): no lock is held across the call;
  the edits are applied under the short per-topic write lock (`notes_lock`) only when the notes
  are still those the block map was built from, else the editor is re-asked with the new block
  map (`NOTES_CHANGED_NOTE`, counting against `MAX_REASKS`), like a revision turn. `live=None` as
  for the review. `ResolutionResult` carries the notes' `revision`.
- `await dismiss_doubt(vault, subject, topic, pending_id, *, sync, host=None, live=None) ->
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
  `DoubtQuestion`; `in_chat: true` when it is the question asked in the workspace chat). Without
  an unended session of the topic they go to a **review session**: a session of the topic started
  and ended at once for them (`vault.start_session(..., kind="review")`/`end_session`, no
  transcript, no lifecycle events), so they fold after every study session before it; its `kind`
  keeps it out of the topic's study sessions (#191): the session list labels it «Revisión de
  dudas», and the topic list's `last_session_at_ms` and the topic card's counts leave it out.
  **With an unended session** (#325) they go to **that live session** through `live`
  (`LiveSink`: `async (kind, origin, payload) -> session_id`, the server's publish on the session
  bus, so the live observer folds them too and their `seq` follows the session's); a review
  session there would fold before the session's later events. Without `live`, or when it cannot
  reach the session (not active on this backend), it is `OpenSessionError` as before, checked
  before any call; a session that ended in between gets a review session. Then
  `review/pending.yaml` and the snapshot are regenerated (`load_observer_snapshot`), and the notes
  (`vault.write_notes`, when edited) and the events are committed with a Spanish summary
  (`Dudas de <s>/<t> revisadas: ...`, `Duda resuelta en ...`, `Duda descartada en ...`); no notes
  tag is created.
- Errors (`DoubtError`, Spanish messages): `UnknownDoubtError`, `DoubtClosedError`,
  `InvalidAnswerError`, `OpenSessionError`, `NotesMissingError`; plus the llm errors as in
  `generate_notes`, with nothing written.
- **Doubts out of an editor write** (#325): `EditorDoubt` (`kind` -- a `PendingKind` --, `text`,
  `question`, `suggestions` 1-3, `options` (`SourceOption`s; a `contradiction` needs two distinct
  citable sources), `refs` (catalogue ids or transcript spans)) is what a revision turn
  (`EditsOutput.doubts`) and a generation (`report_doubts`) report instead of writing a doubt into
  the notes; `editor_doubt_errors(doubts, assembled)` checks them (Spanish, re-asked; at most
  `MAX_EDITOR_DOUBTS` = 10). `await raise_doubts(vault, subject, topic, doubts, *, sync, host=None,
  live=None) -> ids` writes each as an `observer.state_op` `add_pending` (origin `editor`,
  `pending_id` `duda-<hex>` or `contradiccion-<hex>`, `source_refs` its refs and options; the fold
  merges one that duplicates an open item into it, `observer.pending`) plus a `pending.question`
  not asked yet, in the live session or a review session, with one commit.
- **One at a time in the chat** (#325): `ask_plan(vault, subject, topic) -> AskPlan` (blocking,
  reads only): `asked` (an open doubt asked in the chat, `in_chat`, not answered yet: nothing else
  is asked meanwhile), `to_review` (relevant open doubts with no question yet) and `to_ask`
  (relevant open doubts with a question, not asked), in the queue's order. Relevant: the item's
  refs (its captures' pages -- `inputs.capture_pages` --, its sources, its segments' sessions)
  overlap the sources the current notes cite, or it has none; an item whose pages and sources are
  all set aside by capture triage (#324) is never asked; without notes nothing is asked. `await
  ask_in_chat(vault, subject, topic, pending_id, *, sync, host=None, live=None) -> AskedDoubt`
  writes the item's latest question again with `in_chat: true` (a generic one when it has none);
  `AskedDoubt` (`pending_id`, `question`, `suggestions`, `options`, `refs`, `session_id`,
  `commit`) is the `doubt.asked` payload. The server's `doubt_chat.py` drives it.
- `doubt_chat_turns(vault, subject, topic) -> [DoubtChatTurn]` (blocking, reads only): every
  doubt asked in the chat, oldest first (`time` from its session's start plus the event's `t`),
  with `pending_id`, `kind`, `question`, `suggestions`, `options`, `refs`, `status`, `resolution`,
  `answer` (the suggestion, source or words the student gave), `notes_changed`, `resolved_time`;
  `chat_history` shows them as turns of kind `doubt`.
- Limitation: a student's answer is not a source of the catalogue, so the edits it leads to cite
  the sources the doubt is about (the page with the illegible word); an explanation found in no
  source needs `[^ia]` in `ampliado` or stays out of the notes in `estricto`.
- Entry points: the server's `GET/POST /api/subjects/{s}/topics/{t}/doubts...`
  (`docs/modules/server.md`).

### Contradictions between sources -- `contradictions.py`
The editor never chooses silently between two sources that disagree (1769 in the notes, 1765 in
the book): the `editor_generate` and `editor_revise` prompts tell it to write the version of the
student's notes (never both versions, #325) and to report the disagreement as a `contradiction`
doubt with the sides as options (`EditorDoubt`), which the student settles in the chat. This
module's separate search after a generation still runs as below.
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
  on_event=None, digest=None, confirm_over_cap=False, clock=..., ..., request=None,
  turn_id=None, selected_sources=None, session_id=None) -> RevisionResult`. The
  editor first writes its Spanish reply as text -- streamed through `LLMClient.create(on_text=...)`
  to `on_reply("reply.delta", {"text", "attempt"})` -- and then, if anything changes, calls the
  strict tool `apply_edits` once (`EditsOutput`: `ops` (the `EditOp`s above), `footnotes`,
  `summary` (one Spanish sentence), `fidelity_mode` (`estricto`/`ampliado`, only when the student
  sets it: recorded in `topic.yaml` with `vault.set_fidelity_mode`), `proposed_style_rules` (when
  an instruction looks general -- "me gustan las tablas para comparar", "siempre un ejemplo" --:
  rules proposed for the subject's style guide, **not written**; the reply asks the student) and
  `confirmed_style_rules` (a rule proposed in an earlier turn and still pending that the student
  confirms in the chat: appended with `style_guide.append_rules`), `doubts` (#325: the
  `EditorDoubt`s the change leaves unresolved, never written into the notes)). No tool call: a
  chat-only turn, nothing written but the conversation -- except for a request classified `edit`
  (`expects_change`, set by the server's request consumer, #452): an answer without the call is
  re-asked once (`NO_CALL_NOTE`: call `apply_edits` with the change, or say plainly nothing
  changed and why; the chat gets a `reply.restart`), and a turn that still calls no tool carries
  `NO_CHANGE_WARNING` («Los apuntes no han cambiado…»), so a reply that only *says* it changed the
  notes never passes silently. A `question` keeps chat-only answers without a re-ask. After an applied change its doubts are
  recorded with `doubts.raise_doubts` (`live`, `host`: `revise_notes`' parameters; the server
  passes its sink into the topic's live session); their ids are the result's `doubts`, and a
  failure to record them is a `warning`, the change kept.
- **The student's selection** (#433): `selected_sources` is the Recursos selection of a typed
  workspace message (topic-relative ids in order, a PDF page as `<pdf>#page=K`).
  `inputs.selection_input` lays it out as `## Selección actual del estudiante (Recursos)`: each
  captured page `### Seleccionada: <cita> (<id>)` with its transcription and always its image, a
  PDF page with its text, a pasted image with the image, anything else by name (a set-aside page
  says it must be restored before it is cited). Its images come first out of `max_page_images`
  and `max_attachment_bytes`; `assemble_input` gets what is left. The block goes after the cached
  topic prefix, just before the turn text, which says «esto» / «estas páginas» are those sources.
  `[]` (a typed message with nothing selected) adds a note instead: do not guess which pages
  «esto» is, ask the student to select them in Recursos or name them. `None` (spoken requests,
  the old notes chat) adds nothing. The context record carries `selected_sources` and
  `selected_images`.
- **Spoken requests** (#315): `request` (`ChatRequestRef`: `request_id`, `summary`, `session_id`,
  `segment_ids`, `t_start_ms`, `t_end_ms`, `text`) is the `assistant.request` the turn answers
  (the server's `assistant_requests.py` passes it, with the raw `text` as `message`). The turn is
  then `origin` `voice`: the new message is introduced to the editor as a literal transcription of
  what the student said (`SPOKEN_NOTE`), and in the conversation so far such turns read
  "Estudiante (en voz alta, transcrito): ...". `turn_id` is the caller's id of the turn (the
  workspace stream's), stored as given.
- **Checks**: the ops must apply (`apply_edits`) and the edited notes must pass `validate` in the
  mode the turn leaves, with `editor_written=True` against the notes before (no `[[?...]]` in a
  block it changes), the `doubts` must pass `editor_doubt_errors` (so "no inventes" must also remove every `[^ia]` block); a `summary` is
  required when something is applied, at most 5 proposed and 5 confirmed style rules of 300
  characters, and a confirmed rule must be a pending proposal of an earlier turn. A failure is sent back as a `tool_result`
  error with the Spanish list, at most `MAX_REASKS` (2) times, and `on_reply("reply.restart",
  {"attempt"})` tells the caller to drop the reply streamed so far. Past the re-asks nothing is
  applied and the result has `errors` and a Spanish `warning`.
- **Applied**: the notes (`vault.write_notes`), `topic.yaml` and `subject.yaml` as needed, then
  `GitSync.checkpoint("Apuntes de <s>/<t> revisados: <summary>")` at once, as one locked step
  (#410): `editor.notes_lock.checkpointing(sync)` holds the vault's git lock (`GitSync.locked()`,
  re-entrant per thread) around the writes and the checkpoint, inside the topic's notes lock
  (always that order), so the sync loop's batch commit can never take the turn's files first. The
  same step is used by an incorporation, a doubt's edit and the student's save. When the git lock
  stays busy past `[vault.git] timeout_seconds` the change is still written, `commit` is `None`,
  the sync loop commits it later, and the turn's `warning` is `NOT_UNDOABLE_WARNING` ("... este
  cambio no se podrá deshacer"). No notes tag.
  `on_event("notes.edited", payload)` gets the result without the `notes` text.
- `RevisionResult`: `subject`, `topic`, `turn_id`, `origin` (`typed` | `voice`), `request` (the
  `ChatRequestRef`, `None` when typed), `message`, `reply`, `applied`, `summary`, `ops`,
  `feedback` (the `FeedbackRef` of the app feedback the turn recorded, #472, else `None`),
  `footnotes`, `fidelity_mode` (the new one, when changed), `style_rules` (added to the guide:
  the confirmed ones), `proposed_style_rules` (proposed, minus those the guide has), `notes_changed`,
  `doubts` (pending ids raised),
  `changed_sections` (anchors the ops touched; a new section's `anchor`), `diff` (unified diff of
  `apuntes.md`), `notes` (the new text when changed), `paths` (vault-relative files the commit
  changed), `commit`, `revision` (of the notes after the turn, `None` while there are none),
  `attempts`, `errors`, `warning`, `model`.
- `await undo_last_revision(vault, subject, topic, *, sync, on_event=None) -> UndoResult`: the
  latest applied turn (a revision or, since #326, an incorporation) not yet undone is reverted with `GitSync.revert_paths(commit, paths, ...)`
  (a `git revert` of that commit restricted to its `paths`, so the ledger and conversation lines it
  carried stay) and committed as `Deshecho en <s>/<t>: <summary>`; `notes.undone` to `on_event`.
  Undoing again goes one turn further back. `UndoResult`: `undone_commit`, `summary`, `commit`,
  `notes_changed`, `diff`, `notes`, `paths`, `revision`. No Claude call. A student save after the
  turn changes `apuntes.md`, so undoing it then is an `UndoConflictError`. A revert that would
  change no file (a turn applied before #410 whose files a batch commit took, so its commit does
  not carry them) is refused too, an `UndoConflictError` ("... deshacerlo no cambiaría nada"), with
  nothing committed and **no** `notes.undone` recorded: the chat shows the error, never a success.
- `chat_history(vault, subject, topic) -> ChatHistory` (blocking, reads only): `turns`
  (`ChatTurn`: `time`, `kind` -- `revise`, or `explain` for a "¿Por qué?" answer --, `turn_id`,
  `origin` (`typed` | `voice`), `request_summary` (the spoken request's short line, `None` when
  typed), `transcript` (its `ChatRequestRef`, `None` when typed), `message`, `reply`, `applied`,
  `summary` (the applied change's), `changed_sections`, `commit`, `undone`, `warning`, `refs`,
  `feedback` (the recorded app feedback's `FeedbackRef`, #472),
  `proposed_style_rules` -- the turn's proposals the subject's guide does not have yet, also shown
  to the editor in the conversation so far) and `can_undo`. Since #325 also, in time order, turns
  of kind `doubt` (a doubt asked in the chat, `doubt_chat_turns`: `pending_id`, `question` --
  also the `reply` --, `suggestions`, `options`, `doubt_refs`, `status`, `resolution`, `answer`;
  `applied` when its answer changed the notes) and `doubts_resolved` (a review's short line of
  auto-resolved doubts, from its `pending.reviewed` record's `summary`: `reply`, `pending_ids`).
  Since #326 also turns of kind `incorporate` (an `incorporation` record: `source_ids`, `message`,
  `reply`, `summary`, `diff`, `commit`, `applied`, `warning`, undone like a revision turn).
  Since #327 also turns of kind `triage`: captures set aside or restored from the chat, written
  by the server with `record_triage_turn(vault, subject, topic, TriageTurn)` as a `triage` record
  (`turn_id`, `origin`, `request`, `decision` -- also the turn's `summary` --, `source_ids`,
  `message`, the short `reply`, `applied` and, since #351, for a `set_aside`, `targets`: each
  `TriageTarget` `{source_id, reasons, duplicate_of, already}` with its capture triage reasons,
  also on the `ChatTurn`); no notes change, nothing to undo; the editor's
  conversation shows it as the student's message plus that line. The explanations are also in the
  conversation the editor is given on a turn, and so are the student's own edits (the
  `student_edit` records of `direct_edit.py`, as "El estudiante editó él mismo los apuntes
  (#anchors)" plus the diff, cut at `STUDENT_EDIT_DIFF_CHARS`); `chat_history` leaves those out.
- **Conversation** `conversations/editor.jsonl`: `context` (reason `revise`), `user`, `assistant`,
  `validation` per call, then one `revision` record per turn (the `RevisionResult`) and one
  `notes.undone` per undo (the `UndoResult`) -- what `chat_history` and the undo read. A
  `revision` record written before #315 has no `turn_id`, `origin` or `request` and reads as a
  typed turn.
- Errors (`RevisionError`, Spanish): `InvalidMessageError` (empty, or over 4000 characters),
  `NothingToUndoError`, `UndoConflictError` (a file of the
  turn changed afterwards -- a later turn, a regeneration, a doubt's edit); plus the llm errors as
  in `generate_notes`, with nothing written but the conversation records.
- Limitations: a turn's write and its commit are one locked step, so its commit always carries its
  files; only a git lock busy past its timeout (another process holding git) leaves `commit`
  `None`, and that turn -- which says so in its `warning` -- cannot be undone. Nothing here streams to the
  web itself: that is the server's `POST .../notes/chat` (SSE) and the workspace stream
  (`docs/modules/server.md`).

### Incorporating a few sources per request -- `incorporate.py`
Incorporation into the document is iterative and asked in the chat (epic #311, #326): "incorpora
la página 3", "incorpora las dos últimas". Each request is one small, separate `editor` call
(Opus, prompt `editor_incorporate`) on the current notes plus **only** those sources -- never the
whole topic at once. Recognising the request and resolving which pages it means is the chat
router's (#327); it calls the functions below (the server's `NotesGenerator.incorporate`).
- **States**: `source_status(vault, subject, topic) -> list[SourceStatus]` (blocking, reads only;
  public for the chat router and `GET .../sources/status`): one per stored source, in catalogue
  order (notes pages, book pages, PDFs, web pages, then pasted images), with `source_id`
  (topic-relative), `kind`, `number` (from the file name), `label` («la página 3», «la página 12
  del libro», «el PDF «tema.pdf»», «la web «…»»), `state` and `reason`. `apartada`: set aside by
  capture triage (`sources.triage_status`), `reason` the Spanish reasons («borrosa», «repetida de
  la página 1»); `incorporada`: the current notes cite it (a footnote definition links its file,
  a PDF at any page, `cited_source_paths(notes)`); else `pendiente`.
- `await incorporate_sources(vault, subject, topic, source_ids, *, client, sync, on_reply=None,
  on_event=None, request=None, confirm_over_cap=False, clock=..., turn_id=None,
  max_sources=3, max_page_images=20, max_attachment_bytes=24 MiB, live=None, host=None) ->
  IncorporationResult`. `source_ids` are topic-relative (a PDF page's `#page=K` is dropped: the PDF
  is the source), deduplicated. **Refused before any call** (`IncorporationError`, Spanish):
  none (`NoSourcesError`), more than `max_sources` -- the server passes `[editor]
  incorporate_max_sources`, default 3 -- (`TooManySourcesError`, suggesting smaller steps), a
  source the topic lacks or a pasted image (`UnknownSourceError`), a set-aside one
  (`SourceSetAsideError`: «La página N está apartada (<motivo>); recupérala antes si quieres
  incorporarla.»). A source already cited may be incorporated again (the prompt says to refine,
  not duplicate); a topic without notes starts from `# <topic title>`.
- **Input** (`assemble_incorporation`, blocking), stable parts first: system = the prompt and the
  topic block of `assemble_input`; one user message with the current notes (text and
  `describe_sections` block map), the requested sources exactly as `assemble_input` gives them
  (a page's transcription plus its image when `needs_image`, a PDF as its document or page
  texts, a web page as its text, under the same student/supplementary labels), the transcript
  segments of each capture's sidecar `transcript_window` in its own `session` (citable spans), the
  open pending items whose refs name those sources (their captures' pages or source ids), the
  catalogue of just those sources and the instruction, with one cache breakpoint at the end for
  the re-asks. No other source, no whole-topic transcript, no digest.
- **Answer**: a streamed Spanish reply (`on_reply` `reply.delta`/`reply.restart`, as a revision
  turn) and one call of the strict tool `apply_edits` (`IncorporationOutput`: `ops` -- the edit
  ops, `add_section` included --, `footnotes`, `summary`, `nothing_new` -- the requested sources
  that add nothing new, said in the reply -- and `doubts`, `EditorDoubt`s as #325 defines).
  **Checks**: a call is required; a summary; `nothing_new` names only requested sources; the
  doubts pass `editor_doubt_errors` (a contradiction may also name a source the notes already
  cite); the ops apply; the notes pass `validate(..., editor_written=True, previous=<the notes
  before>)`; every requested source is cited by the new notes unless in `nothing_new`. Failures
  are re-asked with the Spanish list, at most `MAX_REASKS` (2); past them nothing is applied
  (`errors`, `warning`).
- **Applied on the latest revision**, under the per-topic write lock (`notes_lock`), only when the
  notes are still those the input was built from; else the editor is re-asked with the new notes
  and block map (`NOTES_CHANGED_NOTE`), counting against `MAX_REASKS`. One commit `Apuntes de
  <s>/<t>: incorporada la página 3` (`incorporadas la página 3 y la página 1 del libro`); no tag.
  Its doubts are raised with `doubts.raise_doubts` (`live`, `host`) and so asked in the chat;
  `on_event("notes.incorporated", <result without notes>)` when applied.
- `IncorporationResult` (also the `incorporation` record of `conversations/editor.jsonl`, after
  the `context` -- reason `incorporate`, `incorporated` ids --, `user`, `assistant` and
  `validation` records): `subject`, `topic`, `turn_id`, `origin` (`typed` | `voice`), `request`
  (`ChatRequestRef`), `source_ids`, `message` (the request's text, or «Incorpora la página 3.»),
  `reply`, `applied`, `summary`, `ops`, `footnotes`, `nothing_new`, `notes_changed`, `doubts`,
  `changed_sections`, `diff`, `notes`, `paths`, `commit`, `revision`, `attempts`, `errors`,
  `warning`, `model`. `chat_history` shows it as a turn of kind `incorporate`;
  `undo_last_revision` undoes it.
- **The whole topic in small batches**: `await incorporate_pending(vault, subject, topic, *,
  client, sync, batch_size=2, max_sources=3, on_event=None, on_progress=None, turns=None,
  confirm_over_cap=False, clock=..., detect_contradictions=True, ..., live=None, host=None) ->
  PendingIncorporationResult` runs `incorporate_sources` over the `pendiente` sources in order
  (`pending_batches`: notes pages first, then book, PDF, web; pasted images never), one batch
  after another, each its own call, commit and chat entry (`turns`, a `BatchTurns` with
  `begin(source_ids) -> (turn_id, reply sink)` and `end(result, error)`), with
  `on_progress({done, total, source_ids})` after each (the server's `incorporation.progress`). A
  cost cap, a refusal, a failed call or a batch that could not be applied stops it with the done
  ones kept (`stopped`, `remaining`, a Spanish `warning`; calling again continues) -- a failure
  of the very first batch is raised. At the end, when the notes changed, the next notes version is
  tagged (`GitSync.create_notes_tag`, as a generation does) and, with at least two sources not set
  aside, `contradictions.detect_contradictions` runs (its failure or an unended session is only a
  warning). Nothing pending: no call, `NOTHING_PENDING_WARNING`. `PendingIncorporationResult`:
  `total`, `done`, `remaining`, `batches` (the `IncorporationResult`s), `stopped`,
  `notes_changed`, `version`, `tag`, `commit`, `contradictions`, `doubts`, `revision`,
  `attempts`, `warning`, `model`. The server's `NotesGenerator` runs it for "prepárame el tema"
  (`prepare_notes` requests and `POST .../notes/generate`) unless `[editor] prepare_mode =
  "single"` (default `"batched"`), with `[editor] incorporate_batch_size` (default 2).

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
- Written with it: a `notes.reviewed` record (reason `student_edit`, the keys of the blocks the
  save added or changed: `reviewed.changed_block_keys`), a `student_edit` record in
  `conversations/editor.jsonl` (the result without `notes`), read by the editor's next turn, and `on_event("notes.edited", {...result without
  notes, "origin": "user"})`.
- Entry point: the server's `PUT /api/subjects/{s}/topics/{t}/notes` (`docs/modules/server.md`).

### Notes versions -- `versions.py`
A version is a `<subject>/<topic>/apuntes-vN` tag (made by "prepárame el tema" and by a restore),
read through `GitSync.list_notes_tags` and `GitSync.read_file_at`. No Claude call.
- `list_versions(vault, subject, topic, *, sync) -> NotesVersions` (blocking, reads only):
  `versions` oldest first (`NotesVersion`: `version`, `tag`, `commit`, `tagged_at`, `message`,
  `current` -- the current `apuntes.md` is exactly its text; `study` -- it was labelled
  "versión de estudio" at some point), `has_notes`, `changed_since_latest` (revisions or doubts
  edited the notes after the latest tag), `study_version` (the latest study label, `StudyLabel`
  `{version, tag, notes_sha256, marked_at}`, or null) and `study_current` (the current
  `apuntes.md`'s SHA-256 equals the label's).
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
- `mark_study_version(vault, subject, topic, *, sync, clock=...) -> StudyVersion` (blocking, #335):
  labels the current notes "versión de estudio" when the topic switches to Estudiar. When
  `apuntes.md` is exactly the latest `apuntes-vN` tag's text that version is labelled; otherwise
  the notes are committed (`Apuntes vN de <s>/<t>: versión de estudio`) and tagged as the next
  version first (`GitSync.create_notes_tag`). The label is recorded in `study/version.yaml`
  (`StudyVersionFile`: `latest` plus the appended `history`, through
  `vault.study.write_study_file`) and committed. Idempotent: with unchanged notes it returns the
  same label and writes nothing. A **label, not a freeze**: nothing locks the notes; an edit
  afterwards only makes `study_current` false (and the generated materials stale, as always).
  `StudyVersion`: `version`, `tag`, `notes_sha256`, `marked_at`, `created_tag`.
  `read_study_label` reads the file (None before the first label); `study_current(label, notes)`.
- Errors (`VersionError`, Spanish): `UnknownVersionError` (no such version, or its file missing at
  the tag), `NothingToRestoreError` (the current notes already are that version; nothing
  written), `NotesMissingError` (no notes to label), `VersionError` for a diff against notes that
  do not exist.
- A restore changes `apuntes.md`, so undoing an earlier chat turn afterwards is an
  `UndoConflictError`, like after a regeneration.
- Entry points: the server's `GET/POST /api/subjects/{s}/topics/{t}/notes/versions...` and
  `POST .../study` (`docs/modules/server.md`).

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

### The voice tutor and the study chat -- `tutor.py`
Study mode (#82): the student asks about a topic out loud -- "¿qué era la derivada?", "ponme un
ejemplo", "¿y eso por qué?" -- and the editor answers from what the topic already has; role
`editor`, prompt `editor_tutor`, no tool, nothing of the notes changed.
- `await ask_tutor(vault, subject, topic, question, *, client, style="spoken", sync=None,
  on_reply=None, digest=None, confirm_over_cap=False, clock=..., max_page_images=20, max_attachment_bytes=24 MiB)
  -> TutorAnswer`. The input is `assemble_input` (catalogue, sources, transcript, doubts'
  decisions, current notes: the cached prefix of the revision chat) with `TUTOR_INSTRUCTION`,
  then, uncached, the last `HISTORY_TURNS` (6) questions and answers and the question (spaces
  collapsed). The answer is Spanish plain text meant to be read aloud (short, no Markdown, formulas
  in words), streamed as `on_reply("reply.delta", {"text", "attempt": 1})`. It cites the current
  notes' footnote labels (`[^p4]`) after what it takes from them, says so when something is not in
  the notes nor the sources, and adds outside knowledge only in `ampliado`, saying it is not from
  the sources.
- **Written style** (`style="written"`, #334): the study screen's question chat (epic #332). Prompt
  `editor_study_chat` (its own file, not a section of `editor_tutor`) and `WRITTEN_INSTRUCTION`;
  the question is marked as typed, not recognised. The answer is Spanish, short, light Markdown
  (paragraphs, lists, bold; no headings, tables or code blocks), cites the footnote labels as the
  spoken style does and each section of the current `apuntes.md` it draws on as `[§anchor]` (a
  heading's `{#anchor}`; `[§ #anchor]` is read too). A request to change the document ("cámbiame
  esta definición") is answered "Eso se cambia en Construir: pídeselo allí al asistente." (prompt
  rule): its only tool is `report_feedback` (app feedback, `feedback.py`, #472, below), nothing
  under `notes/` is written, no notes commit or tag. The client
  is the caller's: the server passes the role `[editor] study_chat_role` names (`editor`, Opus, by
  default; `observer`, Sonnet, to compare), and the ledger records it. Each style is given only its
  own earlier turns as history (the voice tutor and the study chat are two conversations in one
  file); the `context` record carries `style`.
- `TutorAnswer`: `subject`, `topic`, `style` (`spoken` | `written`), `question`, `reply` (with the
  `[^label]` and `[§anchor]` marks), `refs` (`ChatRef` per label the reply cites that the notes
  define with a usable provenance, in order of first citation: `cited_refs(document, reply)`),
  `sections` (written style only: `SectionRef` `{anchor, title}` per `[§anchor]` the reply cites
  that the current notes have, in order of first citation, `title` the heading's title as written:
  `cited_sections(document, reply) -> (sections, unknown)`), `feedback` (written style: the
  `FeedbackRef` of the app feedback the turn recorded, #472, else `None`; also on `TutorTurn`),
  `warning` (empty or cut answer, and in
  the written style the cited anchors the notes lack, `§a, §b`; warnings are joined), `model`.
- `tutor_history(vault, subject, topic) -> TutorHistory` (blocking, reads only): `turns`
  (`TutorTurn`: `time`, `kind`, `style`, `question`, `reply`, `refs`, `sections`, `warning`,
  `option`, `items`), oldest first, both styles. Records written before #334 read as
  `style: "spoken"`, `sections: []`; `kind` is `answer` for every `tutor.answer` record
  (`option`/`items` null) and `generation` for a `tutor.generation` one.
- `record_generation(vault, subject, topic, TutorGeneration, *, sync=None)` (#366): appends a
  study chat generation turn ("hazme un quiz", matched and run by the server, not by the editor)
  as a `tutor.generation` record (`TutorGeneration`: `style` `written`, `question`, `reply` the
  Spanish sentence, `option` the study option, `material_kind`, `items`, `warnings`, `model`);
  it reads back as a `generation` turn (its `warnings` joined into `warning`) and, like the
  answers, as history for the next written question. A failure to record is logged, never
  raised.
- **Conversation** `conversations/tutor.jsonl`, apart from `editor.jsonl` (the editor chat's
  history and undo never see the tutor): `context` (reason `tutor` plus the input summary),
  `user`, `assistant`, then one `tutor.answer` record (the `TutorAnswer`); `sync.note_change()`
  lets the sync loop commit it. Generation turns add one `tutor.generation` record each.
- Errors: `InvalidMessageError` (empty, or over `MAX_QUESTION_CHARS` = 1000), `NotesMissingError`
  (nothing sent); `RefusalError` (the records of the call kept, no `tutor.answer`),
  `CostConfirmationRequiredError` and the llm errors as in `generate_notes`.
- Entry point: the server's `GET/POST /api/subjects/{s}/topics/{t}/tutor` (SSE,
  `docs/modules/server.md`).

### Reviewed and settled blocks -- `reviewed.py` (#474)
What the student already reviewed is derived, not stored per block: append-only `notes.reviewed`
records in `conversations/editor.jsonl` (the existing conversation API, no vault layout change),
`detail = {"reason": "student_edit" | "doubt_closed", "blocks": [<key>, ...]}`.
- `block_key(text) -> str`: SHA-256 (hex) of a block's text without footnote references,
  whitespace collapsed. Editing a block changes its key, so it is no longer reviewed until it is
  reviewed again; moving it or changing only its footnotes keeps it.
- Written when the student saves the notes (`direct_edit.save_student_edit`: the blocks added or
  changed, `changed_block_keys(before, after)`) and when the student answers or dismisses a doubt
  (`doubts.answer_doubt` / `dismiss_doubt`, after the close: the blocks then citing its sources,
  `blocks_citing(notes, item_sources(...))` -- its captures' pages, its source refs, the sessions
  of its transcript segments). `record_reviewed` writes nothing without blocks, and a failure to
  write is only logged.
- `reviewed_keys(vault, s, t) -> set[str]`: every key the records cover.
- `settled_blocks(vault, s, t, notes) -> set[str]`: the keys of the blocks of `notes` that are
  reviewed, carry no `[[?` mark and cite no source an open doubt names (`open_doubt_sources`; a
  doubt whose pages are all set aside by triage is never asked, so it does not count). The block
  map marks them `[revisado]`.

### Overlapping captures: lock, contradictions, capture facts -- `overlap.py` (#474)
Deterministic checks behind building the notes from successive, overlapping captures; an editor
answer that breaks one is re-asked with the Spanish errors like any other validation error.
- `settled_block_errors(before, after, settled, doubted=()) -> list[str]`: every settled block of
  `before` must still be in `after` with the same `block_key` (text modulo footnote refs; it may
  move or change its footnotes), and must not come to cite a source it did not cite that is in
  `doubted` (source keys an open doubt names): citing it would make the block unsettled, so a turn
  could unlock the block it was told not to touch -- e.g. a re-capture of a settled line whose
  transcriber doubts are open, added as corroboration, would bring those doubts back to the
  doubts review. A new citation of a source without open doubts stays allowed and the block stays
  settled. Checked on every incorporation (`incorporate._check`, with `doubted` =
  `open_doubt_sources` plus the sources of the doubts the incorporation raises) and on the doubts
  review's auto-resolution edits (`doubts._check_review`, text and deletion only). `settled` and
  `doubted` are computed from the notes and doubts before the turn's own edits, so what a turn
  writes never changes which blocks it may touch. Not on a doubt answer, and **not on a revision
  turn, even one with a Recursos selection**: there the student may ask to change a settled block,
  so the lock is only the `editor_revise` prompt's rule (do not rewrite a «[revisado]» block from
  a capture unless the student asks for that block), not a validator check.
- `same_kind_contradiction_errors(doubts, requested) -> list[str]`: a `contradiction` doubt
  between a requested capture and a capture of the same kind (notes/notes, book/book) not
  requested now is refused: the editor keeps the notes' reading or replaces it when the new one is
  clearly better. Captures incorporated together, and notes vs book, may still contradict. Checked
  on incorporations (requested = the sources) and on revision turns with a Recursos selection
  (requested = the selection).
- `capture_facts(vault, s, t, requested, notes) -> str`: the `## Datos de cada captura` section
  -- per capture of kind notes/book, the requested ones first, then those the notes cite: its
  `[[?` marks (count and the first `MAX_LISTED_MARKS`), its sharpness (triage metrics, else the
  selected still's) and its triage reasons in Spanish. Given in the incorporation input (after the
  transcript segments) and after a revision turn's selection.
- The doubts review (`review_doubts`), when the block map marks a settled block, gets
  `SETTLED_REVIEW_RULE`: a doubt about what a settled block already says is auto-resolved without
  edits, its evidence the source that block cites, never asked. "Never ask a doubt a settled block
  already holds" is **prompt-enforced only**: telling whether a doubt concerns what a settled block
  says is semantic, so no deterministic check backs it; only the lock above is checked.
- Coverage: the synthetic overlapping captures of `backend/tests/fixtures/overlap/` (four pages of
  one notebook page, two of them re-captures, plus the reference notes the editor should end with)
  drive the FakeClaude tests of the lock, the contradiction refusal, the block map marks, the
  reviewed records and the overlap-aware prompts, and the eval's *unique* score
  (`evals.scoring.score_unique`: the share of generated units not repeating an earlier one;
  `docs/modules/infra.md`, "Evals"), which measures repeated overlapping content in a real run.

### App feedback from the chat -- `feedback.py` (#472)
The student reports a bug of the app itself or asks for an improvement («apunta una mejora: …»,
«esto es un bug: …», «la app debería …») in the workspace chat (a revision turn, Construir) or the
study chat (the written tutor, Estudiar); the editor records it in the vault's feedback inbox
(`vault.feedback`, `feedback/inbox.jsonl`) for the maintainer to triage by hand. **The backend
never calls GitHub** (the code repository is public, the vault is private; no token in the
backend): the maintainer reads the inbox with `studentassistant feedback list` and marks items
with `studentassistant feedback mark` (`docs/modules/server.md`, CLI).
- Tool `report_feedback` (`FEEDBACK_TOOL`, strict, `feedback_tool()`): `FeedbackReport` --
  `kind` (`bug` | `mejora`), `title` (short, Spanish), `body` (the student's words quoted plus a
  one- or two-sentence summary). It is offered next to `apply_edits` in `revise_notes` and as the
  only tool (`tool_choice: auto`) of the written style of `ask_tutor`; the voice tutor does not get
  it. The rule of when to call it is the prompt `editor_feedback` (`feedback_instruction()`,
  appended to `REVISE_INSTRUCTION` / `WRITTEN_INSTRUCTION`): only for feedback about the app,
  never for the notes' content, never together with another tool, ask when in doubt; the text
  reply is only the Spanish confirmation («He apuntado la mejora: …»).
- `parse_feedback(response) -> FeedbackReport | None` (the first valid call; a malformed one is
  logged and `None`), `has_feedback_call(response)`, `confirmation(ref)` (the reply used when the
  model wrote none: «He apuntado el bug: <title>.»), `excerpt(lines, message)` (the last chat lines
  plus the message, the newest `EXCERPT_CHARS` = 600).
- `await record_feedback(vault, report, *, subject_slug, topic_slug, mode, route, session_id,
  chat_excerpt, sync, clock=None) -> FeedbackRef | None`: stores the item with
  `vault.add_feedback` (in a worker thread) with a `FeedbackContext` -- `subject`, `topic`, `route`
  (`workspace` | `study`), `session_id` (revision: the caller's `session_id`, else the spoken
  request's session; study chat: `None`), `mode` (`construir` | `estudiar`), `excerpt` (the last
  two turns, each line cut at 200 characters) -- then `sync.note_change()`. Returns the
  `FeedbackRef` (`id`, `kind`, `title`) the turn's result carries (`RevisionResult.feedback`,
  `TutorAnswer.feedback`, and in the history `ChatTurn.feedback` / `TutorTurn.feedback`), which
  the web shows as the chip «Mejora apuntada» / «Bug apuntado» (`CHIP_LABELS`).
- **A feedback turn never changes the notes**: in `revise_notes` a response with a
  `report_feedback` call ignores any `apply_edits` of the same response, is not re-asked for
  `apply_edits` when the request classified as `edit`, and gets no `NO_CHANGE_WARNING`. The
  editor's history text shows such a turn as «[Apuntado como comentario sobre la aplicación:
  <title>]».
- A malformed call or a store that fails (busy lock, secret guard, validation) never fails the
  turn: nothing is recorded, `feedback` stays `None` and the turn's `warning` gets
  `NOT_RECORDED_WARNING` («No he podido apuntar tu comentario sobre la aplicación; vuelve a
  decírmelo.»).

### Cropping a region of a page image -- `crop.py` (#484)
The student asks for only part of a stored page («solo el diagrama de la página 3»); Sonnet
locates the region and deterministic code cuts it out, cleans it up and stores it as a new,
separately cited source. The original page is never modified. A crop reads and writes the vault
and calls Claude, so it is not an `EditOp` of the pure `apply_edits` pipeline: the revise turn
runs it first and then applies an ordinary edit citing it (`crop_image`, below, #493).
- `await crop_source_image(vault, subject_slug, topic_slug, vault_relative_path, description, *,
  settings=None, client=None, added_at=None) -> CroppedImage`: reads the page with
  `vault.read_source` (a `notes` or `book` page, a PDF page thumbnail, any stored image of a
  media type in `inputs.IMAGE_MEDIA_TYPES`), locates the region, cleans the cut up and stores it.
  Nothing is committed (the caller commits). The page is decoded once
  (`captures.decode_image`, EXIF orientation applied) **before** any Claude call, and the image
  sent to Sonnet is that decoded page re-encoded (`oriented_image`: PNG for a PNG, else JPEG at
  `[sources] capture_jpeg_quality`, no EXIF left), so the box maps to the pixels Sonnet saw;
  the cut is made on the same decoded pixels (`clean_decoded`). Errors, each with nothing written:
  `SourcePathError` / `SourceNotFoundError` bubble from `read_source`; `CropError` (a
  `ValueError`, Spanish message) when the source is not an image Claude reads, or one OpenCV
  cannot decode (a GIF), both refused before the Sonnet call (`UNSUPPORTED_IMAGE_MESSAGE`), or the
  description is empty (`EMPTY_REGION_MESSAGE`); `RegionNotFoundError` (a `CropError`) when
  Claude refuses or gives no valid box after the re-ask; `BlurryCropError` (a `CropError`,
  `BLURRY_CROP_MESSAGE` «El recorte solicitado sale borroso; prueba con otra foto de la página.»);
  any other `LLMError` of the call.
- **Locating the region** (`locate_region(client, image, media_type, description) ->
  BoundingBox`): one call through `studentassistant.llm` with role `observer` (Sonnet by default,
  `crop_client(settings=..., transport=..., ledger=...)`; no model id here), the page as an image
  content block (the `inputs` shape, `region_request`) plus the description, answered through the
  strict tool `crop_region` (`llm.structured`, `max_tokens` 300). `BoundingBox` is `x0`, `y0`,
  `x1`, `y1`, fractions of the image's width/height from its top-left corner, each in `[0, 1]`,
  `x0 < x1`, `y0 < y1`, no extra keys; a box breaking these fails validation and is re-asked by
  `structured` itself, never used raw. The prompt `editor_crop` asks for a **tight** box around only
  the requested content, excluding blank margins, the desk/background and fingers holding the
  page: that is the whole reframing step, there is no separate pass.
- **Cleanup** (`clean_crop(image, media_type, box, *, min_sharpness, jpeg_quality) -> CleanCrop`,
  and `clean_crop_async`, the same in a worker thread): pure, deterministic `numpy`/`cv2` code
  with no LLM call -- the same bytes and box always give byte-identical output -- built from
  `studentassistant.sources.captures`' functions, none reimplemented. (1) *Crop*: the box is
  mapped to pixels (`box_pixels`: outward-rounded, clamped to the image, at least one pixel each
  way) and the image cut to it. (2) *Sharpness filter*: the cut's `captures.sharpness` (variance
  of the Laplacian) below `[editor] crop_min_sharpness` (default
  `DEFAULT_TRIAGE_MIN_SHARPNESS`, the capture triage's scale: a crop too blurry to keep as a
  capture is too blurry to cite; `ge=0`) is refused. (3) *Deskew*: `captures.find_page` searches
  the cut for a quadrilateral (a sheet, a card, a framed figure photographed slightly askew); when
  it finds one, `captures.crop_page` warps it flat (perspective transform), otherwise the plain
  axis-aligned cut is kept. A PNG source gives a PNG crop, anything else a JPEG
  (`captures.encode_jpeg`, `[sources] capture_jpeg_quality`). `CleanCrop`: `data`,
  `content_type`, `extension`, `width`, `height`, `sharpness` (of the cut, before any warp),
  `deskewed`.
- **Stored** as a new `images` source through `vault.put_source(..., "images", ..., meta)`
  (`sources/images/img-NNN.<ext>` + `img-NNN.yaml`), in a worker thread. The sidecar:
  `origin: cropped` (`CROPPED_ORIGIN`), `cropped_from` (the page's vault-relative path),
  `bbox` (`[x0, y0, x1, y1]`, the fractions used), `requested_region` (the description, stripped),
  `content_type`, `sha256` (of the stored crop) and `added_at` (now, UTC, by default) -- readable
  back as `vault.read_source(...).meta` of the new file, so the crop traces back to its page.
- `CroppedImage` (what it returns): `path` (vault-relative), `source_id`
  (`sources/images/img-NNN.<ext>`), `number`, `box`, `crop`, `meta`, `provenance`
  (`cropped_image_provenance`), and the properties `footnote_label` (`imgNNN`), `footnote`
  (`[^imgNNN]: [Imagen recortada N](../sources/images/img-NNN.<ext>)`) and `markdown`
  (`![Imagen recortada N](../sources/images/img-NNN.<ext>)`): what a caller needs to write an
  `insert_after`/`replace_block` edit showing and citing the crop; `paths` (the image and its
  sidecar, vault-relative).
- **From the workspace chat** (#493): a revise turn (`revise_notes`) offers the editor, next to
  `apply_edits` and `report_feedback`, the strict tool `crop_image` (`CROP_TOOL`,
  `CropImageRequest`: `source` -- a topic-relative source id as the catalogue gives it, a PDF page
  as `sources/pdf/<file>.pdf#page=K`, whose rendered page `<stem>.pKKK.jpg` is what is cropped
  (`crop_source_path`) --, `region`, `op` `insert_after` | `replace_block`, `section`, `block`,
  `summary`). One crop per turn, never together with `apply_edits`. Before anything is cropped the
  call is checked: the source must be one the notes cite or one of the Recursos selection, the
  region and summary non-empty, and the anchor must apply (`placeholder_edit` through
  `apply_edits`); a failure is sent back (`tool_result` error naming `crop_image`) like a failing
  change. Then `crop_source_image` runs (client: `revise_notes(crop_client=...)`, role `observer`;
  `settings`), outside `apply_edits` and outside the notes lock, and `crop_edit` builds an
  ordinary `EditOp` whose `text` is the image link plus `[^imgNNN]` and the `NewFootnote` of
  «Imagen recortada N»; that `EditsOutput` goes through the same `_check`, the notes lock and the
  one locked write + checkpoint as any change, the crop's image and sidecar among the commit's
  `paths` (so undoing the turn removes them too). A student save meanwhile is re-asked with the
  new block map, and the crop already made for the same source and region is reused, never cut
  twice. A crop stored in an attempt whose change is never applied is retired
  (`vault.remove_source`). A crop that fails (`CropError`: not an image, undecodable, blurry, box
  refused; the source not found) changes nothing: the turn ends, the streamed reply is dropped
  (`reply.restart`) and replaced by `CROP_FAILED_PREFIX` + the Spanish reason («No he podido añadir
  el recorte: …»), and the result's `crop` carries the error. `RevisionResult.crop` (`CropRef`:
  `source`, `region`, `source_id` and `path` of the new image, or `error`) is in the `revision`
  record and the `notes.edited` event. The reply of a successful crop is the editor's own
  confirmation («He añadido el recorte del diagrama de la página 3.»). The request classifier's
  prompt (`observer_requests`) lists these requests as `edit`, so a spoken or typed one reaches the
  editor with `expects_change`.
- Tests: `FakeClaude` scripts the box (valid, out of bounds and re-asked, refusal); fixture images
  built in the tests cover the sharp/blurred filter, a rotated rectangle deskewed and an
  axis-aligned one left as the plain crop, byte-identical output, and with `tmp_vault` the stored
  file, its sidecar and the footnote, an undecodable GIF refused without a call, and an EXIF-rotated
  page shown upright to Sonnet (`tests/editor/test_crop_*.py`); the chat turn -- success with its
  link, footnote, commit and undo, a cited PDF page, each failure, a source not allowed, crop and
  `apply_edits` together, the crop outside the notes lock with a student save redone on, a retired
  crop, the classifier prompt -- in `tests/editor/test_revise_crop.py`.
