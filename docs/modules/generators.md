# Module: generators

**Lives in:** `backend/src/studentassistant/generators/`.

## Responsibility
From the master notes (+ sources for context), role `generator`: outline (+ mermaid mind map),
quiz (YAML: type, question, options, answer, explanation, source ref, difficulty), flashcards
(+ Anki `.apkg`, CSV), exercises and mock exam with solutions (printable PDF), slides (Marp
Markdown -> PDF/PPTX). Every artifact records the notes version it was built from and is marked
stale when the notes change. Every item keeps provenance.

## The framework (#67)

A generator is a `Generator` subclass registered under a `kind`; the framework runs it over a
topic's `notes/apuntes.md`, stores what it returns under `generated/` through
`studentassistant.vault`, and tells when it is stale. Generators never write the vault nor run git.

### Writing a generator -- `base.py`, `registry.py`

```python
from studentassistant.generators import Generator, GeneratorContext, GeneratorOutput
from studentassistant.generators import ItemProvenance, register

@register                                # on generators.default_registry
class OutlineGenerator(Generator):
    kind = "esquema"                     # slug [a-z0-9-]; CLI/API name and file stem
    title = "Esquema"                    # Spanish, for the student
    version = 1                          # bump when the output changes shape
    options_model = OutlineOptions       # a Pydantic model; NoOptions by default

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        result = await context.structured(
            [{"role": "user", "content": [context.notes_block()]}], Outline,
            tool_name="record_outline", tool_description="...",
            system=prompt.content, prompt_hash=prompt.hash,
        )
        return GeneratorOutput(
            files={"esquema.md": render(result.value)},
            items=[ItemProvenance(item="n1", anchors=["definicion"]), ...],
            model=result.responses[-1].model, prompt_hash=prompt.hash,
        )
```

The built-in generator modules are imported by `studentassistant.generators/__init__.py` so they
register themselves.

- `GeneratorContext`: `vault`, `subject`, `topic`, `topic_title`, `notes_text`, `notes` (the
  parsed `NotesDocument` of `editor.notes_format`), `sections` (`NoteSection`: `anchor`, `title`,
  `level`) and `anchors`, `basis` (`NotesBasis`, below), `options` (validated), `client` (a
  `generator` `LLMClient` bound to the topic's ledger), `kind`, `confirm_over_cap`.
  `notes_block()` is a text block with the notes, their version and the anchors to cite;
  `structured(messages, output, *, tool_name, tool_description, system, prompt_hash,
  max_tokens)` is `llm.structured` on the context's client (the cost-cap confirmation passed on),
  recording the turns in `conversations/generator.jsonl`; `record(kind, **fields)` appends a
  record there.
- `GeneratorOutput`: `files` (name relative to `generated/` -> `str | bytes`; every name starts
  with `<kind>.`, `<kind>-` or `<kind>/`, and none ends in `.meta.yaml`), `items`
  (`ItemProvenance`: `item` id, `anchors` without `#`), `item_texts` (item id -> the text of it
  that must come from the notes it cites, checked by the grounding check below), `warnings`
  (Spanish), `model`, `prompt_hash`.
- `GeneratorRegistry`: `register(cls)` (decorator; a bad kind, a missing title or a second class
  for a kind is `ValueError`), `unregister(kind)`, `kinds()`, `classes()`, `lookup(kind)`,
  `kind in registry`, `get(kind)` (a new instance, `UnknownGeneratorError` with a Spanish message
  listing the known kinds). `default_registry` / `register` are the process-wide ones.

### Running -- `run.py`

`await run_generator(vault, subject, topic, kind, *, client, sync, registry=default_registry,
options=None, confirm_over_cap=False, clock=...) -> GenerateResult`:

1. the generator of `kind` (`UnknownGeneratorError`); `options` validated against its
   `options_model`, unknown keys refused (`InvalidOptionsError`, Spanish);
2. `notes/apuntes.md` read (`NoNotesError` when there is none) and its `NotesBasis` taken:
   `sha256` of the text, `version`/`tag` of the newest `apuntes-vN`, `changed_since_version`;
3. the generator runs; its files are checked (`GeneratorOutputError`: no files, a name outside
   its kind, a manifest name). Claude errors (`CostConfirmationRequiredError`, `RefusalError`,
   `LLMError`) propagate and nothing is written;
4. items with no anchor, or anchors the notes lack, are **reported, not dropped**: listed in
   `unresolved` (with the missing anchors) and in a Spanish warning;
5. every item with a text in `item_texts` and at least one anchor the notes have is checked
   against those sections (see "Grounding check"); those under `grounding_min_support` are
   **reported, not dropped**: listed in `ungrounded` and in one Spanish warning;
6. stored: files the previous run of the kind wrote and this one did not are removed, the files
   written, then the manifest `generated/<kind>.meta.yaml` (`ArtifactMeta`: `kind`,
   `generator_version`, `built_at`, `model`, `prompt_hash`, `options`, `notes` (the basis),
   `files`, `items`, `unresolved`, `ungrounded`, `warnings`), and one commit `Generar <kind> de <s>/<t>
   (apuntes vN)`. A final `material.generated` record goes to `conversations/generator.jsonl`.

`GenerateResult`: `subject`, `topic`, `kind`, `files` (vault-relative, the manifest last),
`removed`, `notes`, `commit`, `items` (count), `unresolved`, `ungrounded`, `warnings`, `model`.

### Grounding check -- `grounding.py` (#278)

Deterministic, no Claude: every generated item is checked against the note sections it cites, so
an item whose content the master notes do not hold is reported instead of silently trusted
(VISION §5.6).

- The text checked is the generator's `item_texts[item]`: quiz -- the answer and the explanation
  (a true/false: the statement when it is true, plus the explanation; `Verdadero`/`Falso` alone
  says nothing); flashcards -- the back; examen -- the statement and the rubric criteria, numbers
  removed (the worked solution is **not** checked: the numbers it computes cannot be judged by word
  overlap, and neither can the data an exercise states); diapositivas -- the bullets; esquema -- the node's gloss, else its title. An item without a text is not checked.
- `content_words(text)`: the eval rubric's normalisation (`evals/scoring.py`, reimplemented so
  generators do not import the eval harness): unreadable-word marks become their word, footnote
  references, `{#anchor}`s, link targets and punctuation go, accents folded, lower case; words of
  three letters or more that are not Spanish stop words, numbers always kept. A few words a
  generated item says about itself or uses to ask for work (`META_WORDS`: respuesta, correcta,
  verdadero, falso, apuntes, calcula, justifica, plantea...) are not content.
- `section_text(notes, anchor)`: the cited section's heading title and blocks plus its
  subsections' (deeper headings that follow it), footnote definitions left out; `None` when the
  notes lack the anchor.
- `support(text, sections)`: the share of the text's content words found in the sections, in
  [0, 1] (4 decimals); a text with no content word claims nothing and scores 1.
- `find_ungrounded(notes, items, texts, min_support)`: scores each item against the sections of
  its anchors that resolve; an item with no resolvable anchor is left to `unresolved` and not
  scored (no double count). Below `min_support` it is an `UngroundedItem` (`item`, `support`,
  `anchors`: the resolved ones) in the manifest's `ungrounded` and `GenerateResult.ungrounded`,
  summarised by `ungrounded_warning` ("N elementos no se apoyan claramente en los apuntes: q3
  (50 %, #reglas), ... Revísalos antes de estudiar con ellos.", the first ten listed).
- Threshold: `[generators] grounding_min_support` (`SA_GENERATORS__GROUNDING_MIN_SUPPORT`,
  0-1, default 0.6), passed to `run_generator(..., grounding_min_support=)` by the CLI and the
  REST route. Manifests written before the check have no `ungrounded` and load as `[]`.

### Stale detection

Computed on read, never stored: `artifact_status(vault, subject, topic, kind, *, registry)` ->
`ArtifactStatus` (`kind`, `title` (none for a kind no longer registered), `generated`, `stale`,
`stale_reason` (Spanish), `files` (vault-relative), `meta`). Stale when the current `apuntes.md`
is gone or its SHA-256 is not the manifest's (a revision, a restore, a doubt's edit; going back to
the same text is fresh again), when the registered generator's `version` is newer than the
manifest's, or when the manifest cannot be read. `materials_status(vault, subject, topic, *,
registry)` -> `MaterialsStatus` (`has_notes`, `notes_sha256`, `artifacts`: every registered kind,
sorted, then any other kind with a manifest). `read_artifact_meta(...)` reads one manifest
(`GenerationError` when unreadable).

### Quiz -- `quiz.py` (#75)

Kind `quiz`, title "Quiz", prompt `prompts/generator_quiz.md`. Options (`QuizOptions`): `size`
(1-30, default 10), `difficulty` (`easy`, `medium`, `hard` or `mixed`, the default), `types`
(any of `multiple_choice`, `true_false`, `short_answer`; all by default). Claude records a
`QuizDraft`; a question with no text or answer, a multiple choice whose answer is not one of its
(de-duplicated) options, a true/false that is not `Verdadero`/`Falso`, or one of a type not asked
for is dropped with a Spanish warning; the rest is cut to `size` (fewer is warned), numbered
`q1`..., and no usable question at all is a `StructuredOutputError` (nothing written).

`generated/quiz.yaml` (`Quiz`): `title`, `difficulty`, `questions` -- each `id`, `type`,
`difficulty`, `question`, `options` (`[Verdadero, Falso]` for a true/false, empty for a short
answer), `answer` (the option's exact text, or the expected short answer), `explanation`,
`anchors` (its source ref: the note sections; also the item provenance of the manifest).

Taking it: `read_quiz(vault, s, t) -> StoredQuiz | None` (`quiz`, the manifest's `built_at`,
`notes_version`, `warnings`, `stale`, `stale_reason`). `record_quiz_result(vault, s, t,
attempt, *, sync, clock) -> QuizResult` grades a `QuizAttempt` (`built_at` of the quiz answered,
`answers` of `question` id, `given`, `self_assessed`, `duration_seconds`; optional
`questions`, #282: a partial attempt asking only those ids, e.g. retaking the ones answered
wrong -- only they are graded and `total` is their number): a choice or a short
answer equal to the expected one after `normalize_answer` (case, accents, spaces, surrounding
punctuation) is right; a short answer that is not is judged by `self_assessed` when given
(`graded_by: student`); no answer is wrong. The `QuizResult` (`time`, `quiz_built_at`,
`generator_version`, `notes_version`, `notes_sha256`, `total`, `correct`, `duration_seconds`,
`answers`: `GradedAnswer` with `expected`, `correct`, `graded_by`, `anchors`; `questions`: the
ids asked in quiz order for a partial attempt, `None` for a full one and for lines written before
the field) is appended to `study/quiz-results.jsonl` (`vault.study`) and committed (`Resultado
del quiz de s/t: c/n`, `Resultado parcial del quiz ...` when partial).
Refusals (`GenerationError`, Spanish): `QuizNotFoundError`, `QuizChangedError` (the quiz was
generated again since `built_at`, also for a partial attempt), `InvalidAttemptError` (an unknown
id in `answers` or `questions`, an id twice, an answer to a question not asked). `quiz_results(vault, s, t)` reads the
history. REST: `server/quiz_routes.py` (`docs/modules/server.md`); web: `src/quiz/`.

### Practice with spaced repetition -- `practice.py` (#81)

Not a generator: it practises the topic's current flashcards and quiz questions. Items
(`practice_items(vault, s, t) -> (items, warnings)`) are `PracticeItem` (`key`, `source`
`flashcards|quiz`, `prompt`, `answer`, `question_type`, `options`, `explanation`, `anchors`), keyed
stably: `flashcards:<card id>` and `quiz:<first 12 hex of sha256(normalize_answer(question))>`
(`quiz_item_key`), so a regenerated quiz asking the same question keeps its history; an item that
leaves the material is no longer offered. Warnings (Spanish): no flashcards nor quiz, a stale
material.

- History: every review is a `PracticeReview` (`time`, `item`, `source`, `rating`, and for a
  question `given`, `correct`, `graded_by`; `anchors`) appended to `study/practice.jsonl`
  (`vault.study`, merges by union). `practice_history(...)` reads it. The schedule is never
  stored: `replay(reviews)` recomputes each item's `ItemState` (`reviews`, `repetitions`,
  `lapses`, `ease`, `interval_days`, `first_review`, `last_review`, `last_rating`, `due`) in time
  order, so two PCs' logs give the same schedule.
- `schedule(state, rating, now)` (pure), SM-2 variant, ratings `again|hard|good|easy`: ease starts
  at 2.5, never under 1.3 nor over 3.5; `again` -0.2 ease, repetitions to 0 (a lapse when it was
  learned), due in 10 minutes; `hard` -0.15 ease, 1 day first, else interval × 1.2; `good` 1 day,
  then 6, then interval × ease; `easy` +0.15 ease, 4 days first, else interval × ease × 1.3;
  intervals capped at 365 days.
- `practice_queue(vault, s, t, *, now=None, new_limit=10, tz=None) -> PracticeQueue`: `queue` of
  `QueuedItem` (`item`, `state` or none) -- the items due (oldest due first), then never-seen items
  up to `new_limit` minus those first reviewed on the day of `now` (in `tz`, local by default) --,
  `counts` (`total`, `due`, `new`, `unseen`, `learned`, `new_today`, `suspended`), `next_due`,
  `suspended`, `warnings`.
- `practice_summary(vault, *, now=None, new_limit=10, tz=None) -> PracticeSummary` (#280): every
  topic's `practice_queue` counts (`TopicPracticeSummary`: subject/topic ids and names, `due`,
  `new`, `next_due`), topics without practice items omitted, sorted by `due` then `new` (desc),
  with `totals` (`due`, `new`, `topics`); an unreadable subject or topic is skipped and named in
  Spanish `warnings`. Backs `GET /api/practice/summary`.
- `record_practice_review(vault, s, t, answer, *, sync, clock) -> ReviewOutcome` (`review`,
  `state`): `PracticeAnswer` (`item`, `rating`, `given`, `self_assessed`). A flashcard needs a
  `rating` (`InvalidReviewError`); a question is graded by `quiz.grade` -- wrong is `again`, right
  is `good` unless the rating says `hard` or `easy`. An unknown key is
  `PracticeItemNotFoundError`. The review is appended to `study/practice.jsonl` before the call
  returns, then only `sync.note_change()`: the reviews of one sitting are not checkpointed one by
  one but committed together by `GitSync.run_due()` (after `commit_quiet_seconds` of quiet, at the
  latest `commit_max_delay_seconds` after the first review) under the batch summary, or by
  `flush()` at shutdown / `sync()` like any other pending change.
- Setting an item aside (#281): `suspend_practice_item(vault, s, t, key, *, sync, clock)` and
  `restore_practice_item(...)` -> `SuspensionOutcome` (`item`, `suspended`, `suspended_at`,
  `changed`) append a `PracticeSuspension` (`time`, `item`, `source`, `action` `suspend|restore`)
  to the same `study/practice.jsonl` -- a distinct line kind (`extra="forbid"` tells it from a
  `PracticeReview`), so old logs still load and `merge=union` keeps both PCs' lines -- then only
  `sync.note_change()`. The latest record per item in time order (file order on a tie) wins
  (`suspensions(lines) -> {key: suspended_at}`; `practice_log(...)` reads both kinds). Idempotent:
  a request that changes nothing writes nothing (`changed: false`); an unknown key is
  `PracticeItemNotFoundError`. A suspended item is never queued (neither due nor new, and it does
  not set `next_due`); `counts.suspended` counts it (`total` still includes it, `unseen` and
  `learned` do not; one started today still counts in `new_today`), and
  `PracticeQueue.suspended` / `suspended_items(vault, s, t)` list the current ones as
  `SuspendedItem` (`key`, `source`, `prompt`, `suspended_at`), latest first. `replay` ignores the
  suspensions, so a restored item keeps its previous history and schedule.

Full quiz attempts (`quiz-results.jsonl`) do not feed the schedule. REST:
`server/practice_routes.py` (`docs/modules/server.md`); web: `src/practice/`.

### CLI and API

- `studentassistant generate <kind> --topic <subject>/<topic> [-o key=value ...]
  [--confirm-over-cap]`: runs in-process on the configured vault with the real transport, prints
  the files, the notes version and the warnings; the commit is local and the server's sync pushes
  it. Values of `-o` are JSON when they parse (`size=10`, `split=true`), text otherwise.
- REST (`server/generators_routes.py`, see `docs/modules/server.md`): `GET /api/generators`,
  `GET .../topics/{t}/generated`, `POST .../topics/{t}/generated/{kind}`.

## Outline -- kind `esquema` (#74)

`generators/outline.py`; `OutlineGenerator` (title `Esquema`, version 1, no options) is registered
on `default_registry`, so `studentassistant generate esquema --topic <s>/<t>` and
`POST .../generated/esquema` run it. It writes one file, `generated/esquema.md`, plus the
framework's `generated/esquema.meta.yaml` (notes version, provenance, stale marking).

- Claude (role `generator`, prompt `prompts/generator_outline.v1.md`, tool `record_outline`)
  answers an `OutlineDraft`: a **flat** list of `OutlineDraftNode` (`id`, `parent` -- an earlier
  node's id or null --, `title`, `gloss` or null, `anchors`), because strict tools refuse
  recursive schemas. The draft is checked (unique ids, parents before children, at most
  `MAX_DEPTH` = 4 levels, no empty title); a draft that fails is re-asked once by
  `llm.structured`. `OutlineDraft.to_outline(title)` builds the tree.
- `Outline` (`title` -- the topic's, the mind map's root --, `nodes`) and `OutlineNode` (`title`,
  `gloss`, `anchors` without `#`, `children`): titles and glosses are one line, an empty title or
  more than `MAX_DEPTH` levels is a `ValidationError`. `walk()` yields `(number, level, node)` in
  document order (`"1"`, `"1.2"`, `"1.2.1"`); `provenance()` is one `ItemProvenance` per node,
  named by that number.
- `render_outline(outline, *, known_anchors=None) -> str` (pure): `# Esquema: <tema>`, top-level
  nodes as `## 1. Título` with their gloss and `Apuntes:` links (`../notes/apuntes.md#<anchor>`),
  deeper nodes as nested bullets `- **1.2 Título**: glosa · [#ancla](...)`, then `## Mapa mental`
  with `render_mindmap(outline)`: one fenced `mermaid` `mindmap` block (`root(("Tema"))`, level 1
  `n1("...")`, deeper `n1_2["..."]`). `mermaid_label(text)` quotes each label and replaces what
  mermaid would misread (`"` and backtick -> `'`, `<...>` -> `‹...›`, `#name;` entity codes,
  `%%`); `<` in the Markdown body is written `&lt;`.
- Provenance: a node with no anchor, or citing an anchor the notes lack, is kept, marked in the
  Markdown (`*(sin sección de los apuntes)*`, `` `#x` *(no está en los apuntes)* ``) and reported
  by the framework in `unresolved` and a warning.
- Golden rendering: `backend/tests/fixtures/generators/esquema.md` (open it on GitHub to see the
  mind map).

## Flashcards -- `flashcards.py` (kind `flashcards`, #76)

Options `size` (1-100, default 20: at most that many cards). Claude (prompt
`prompts/generator_flashcards.md`, tool `record_flashcards`) returns `DraftDeck`: cards with a
Spanish `front` (question), `back` (answer), `anchors` and, optionally, the `id` of an earlier card
it restates. Files under `generated/`:

- `flashcards.yaml` (`FlashcardsFile`: `deck` = `<subject name>::<topic title>`, `deck_id`,
  `cards`: `id`, `front`, `back`, `anchors`) -- read back by the next generation;
- `flashcards.csv` (`id,anverso,reverso,secciones`, the section titles joined with `; `);
- `flashcards.apkg` (genanki, built in memory): one deck per topic, note type `ANKI_MODEL_ID`
  (fields Anverso, Reverso, Apuntes; `$..$`/`$$..$$` as MathJax, `**bold**`, line breaks), tags
  the subject and topic slugs.

Stable ids, so re-importing the deck into Anki updates the notes instead of duplicating them:
`deck_id_for(subject, topic)` is derived from the slugs, the note type id is fixed, and each
note's GUID is `guid_for(subject, topic, card id)`. A card's `id` (`c<8 hex>[-n]`) is kept across
generations (`assign_ids`): the previous cards are shown to Claude, whose reused `id` is kept when
it is an earlier card's and not given twice; else a card whose normalized front equals an earlier
card's takes its id; else a new id from the front's hash. Items are the card ids with their
anchors. The web ("Material de estudio", #79) downloads the `.apkg` and `.csv` through `GET .../generated/files/{name}`.

## Exercises and mock exam -- `exam.py` (kind `examen`, #77)

Options `exercises` (0-30, default 6), `questions` (1-20, default 5), `total_points` (default 10)
and `duration_minutes` (10-300, default 60). Claude (prompt `prompts/generator_exam.md`, tool
`record_exam`) returns `DraftExam`: the exam's `instructions`, the practice `exercises` and the
`exam` questions, each a `DraftQuestion` (`statement`, `difficulty` `baja|media|alta`, `points`
(exam questions), worked `solution`, `rubric` of `RubricCriterion` (`criterion`, `points`),
`anchors`). The prompt asks for Unicode mathematics rather than LaTeX, since the output is
printed. Files under `generated/`, the statements apart from the solutions:

- `examen.md` -- the statements: exercises (with their difficulty), then the exam with its
  duration, total points, instructions and every question's points;
- `examen-soluciones.md` -- solutions, rubrics as a `Criterio | Puntos` table, and the note
  sections each item comes from;
- `examen.pdf`, `examen-soluciones.pdf` -- the same as A4 PDFs (`render_pdf`: PyMuPDF `Story`
  over HTML rendered from the same data; `**bold**`, `*italic*`, `- ` and `1. ` lists, any
  `$..$` left as LaTeX source in monospace), the exam with a name/date line, a box to answer each
  question sized by its points, and `<title> · Página i de n` at the foot of every page.
- `examen.yaml` (`ExamFile`, since generator version 2, #283) -- the machine-readable exam the web
  corrects: `title`, `instructions`, `duration_minutes`, `total_points`, `exercises` and
  `questions`, each an `ExamQuestion` (`id`, `number`, `statement`, `difficulty`, `points`,
  `solution`, `rubric`, `anchors`). An exam built by version 1 has none and shows as stale.

Extra items beyond the options are cut, questions without points, questions not adding up to
`total_points` and rubrics not adding up to their question's points are kept and reported as
Spanish warnings. Items are `e<n>` (exercise) and `p<n>` (exam question) with their anchors. The
web lists the two PDFs and previews the Markdown in "Material de estudio" (#79) through `GET
.../generated/files/{name}`.

Correcting it -- `exam_results.py` (#283): the student sits the exam on paper and grades it in the
web with the rubric. `read_exam(vault, s, t) -> StoredExam | None` (`exam` from `examen.yaml`, the
manifest's `built_at`, `notes_version`, `warnings`, `stale`, `stale_reason`; `None` without an
exam or without `examen.yaml`). `record_exam_result(vault, s, t, attempt, *, sync, clock) ->
ExamResult` checks an `ExamAttempt` (`built_at` of the exam corrected, `questions`: per question
id the `awarded` points, one per rubric criterion in order -- a question without rubric is one
criterion "Pregunta completa" worth its points; a question left out scores 0): every value
between 0 and its criterion's points, the question's score capped at its points (its `points`,
else its rubric's sum). The `ExamResult` (`time`, `exam_built_at`, `generator_version`,
`notes_version`, `notes_sha256`, `score`, `total` -- the questions' points added up --,
`percentage`, `questions`: `QuestionScore` with `points`, `score`, `criteria` awarded and
`anchors`) is appended to `study/exam-results.jsonl` (`vault.study`) and committed (`Corrección
del examen de s/t: x/y`). Refusals (`GenerationError`, Spanish): `ExamNotFoundError`,
`ExamChangedError` (generated again since `built_at`), `InvalidCorrectionError` (unknown question,
one corrected twice, not one value per criterion, points out of range). `exam_results(vault, s,
t)` reads the history. REST: `server/exam_routes.py` (`docs/modules/server.md`); web: `src/exam/`.

## Slides -- `slides.py` (kind `diapositivas`, #78)

Options `size` (1-40, default 12: at most that many slides besides the cover) and `export`
(default true). Claude (prompt `prompts/generator_slides.md`, tool `record_slides`) returns
`DraftDeck`: `title`, optional `subtitle`, and `slides` with a Spanish `title`, `bullets`, optional
speaker `notes`, `anchors` and, optionally, the `image` id of a figure. Figures
(`collect_figures`) are the source pages the notes cite: notes/book page images (the cropped
`page-NNN.page.jpg` when stored) and PDF pages (their `page-NNN.pKKK.jpg` thumbnail), offered as
`f1`, `f2`... with their citation text and the sections citing them; Claude does not see them.
Files under `generated/`:

- `diapositivas.md` -- Marp Markdown (`render_markdown`): front matter `marp: true`, a cover
  (title, subtitle, subject name), one slide per draft; a figure is a `![bg right:40% contain]`
  background linked relative to the deck and credited in the slide's `_footer` ("Imagen: Libro,
  página 12"); speaker notes as HTML comments;
- `diapositivas/figura-NN.<ext>` -- the figures shown, copied from the sources;
- `diapositivas.pdf`, `diapositivas.pptx` -- exported by Marp CLI (`MarpExporter`: run in a
  temporary directory with `--allow-local-files --pdf|--pptx --output <file>`, its process group
  killed on a timeout). A missing `marp`, a failure or a timeout is a Spanish warning and the
  Markdown is still stored (an older PDF/PPTX is then removed, never left stale).

Configuration `[generators]` (`SA_GENERATORS__*`): `marp_command` (default `["marp"]`, e.g.
`["npx", "--yes", "@marp-team/marp-cli"]`), `marp_timeout_seconds` (180), `marp_browser_path`
(unset: Marp finds Chrome/Chromium itself); the same section holds `grounding_min_support`
(see "Grounding check"). Marp needs Node and a Chromium-based browser; it is
not a Python dependency. Items are the slides (`d01`, `d02`...) with their anchors. The web lists
the PDF/PPTX in "Material de estudio" (#79) through `GET .../generated/files/{name}`. Tests use a stand-in
exporter (`SlidesGenerator.exporter`) and a fake `marp` script; a real export is
`@pytest.mark.integration` (`SA_TEST_MARP`).
