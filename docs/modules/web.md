# Module: web

**Lives in:** `web/`.

## Responsibility
- **Web capture page** (`/capture`): the development capture client (ADR-0001, ADR-0008) --
  laptop camera preview, high-resolution stills (burst), Web Speech API transcription (or audio
  streaming in server STT mode), session buttons, live transcript; speaks protocol v1 exactly
  like the Android app.
- **Pairing page** (`/pair`, `src/pairing/`): asks `POST /api/pair/codes` (#89) for a one-time
  code and shows a QR of exactly `{url, code}` (`qrPayload()`), the URL and the code as text,
  and a countdown to `expires_at`; on expiry the QR gives way to a "Generar un código nuevo"
  button. Refused (403), failing and unreachable backends each get a Spanish explanation.
  Pairing happens **on the PC itself only**: the backend mints codes for loopback callers only
  and serves the web app to other LAN clients only with a bearer token (#104), so the page has
  no unauthenticated exemption. It handles only the pairing code -- never a device token -- and
  stores nothing.

Spanish review UI served by the backend (localhost trusted; other LAN clients use the pairing
token):
- Study desk: subjects/topics, per-topic card (sources, sessions, minutes, pending, materials).
- Notes viewer: rendered `apuntes.md`; clicking a provenance footnote opens the sources panel
  (zoomable page image + its transcription, transcript excerpt, book/PDF page, web snapshot);
  `[^ia]` content highlighted.
- Editor chat (streamed), applied edits shown as a diff; optional push-to-talk.
- Pending panel and the doubts-resolution flow; version history; live session view; generators.

## Skeleton (public surface)
- `web/` is a Vite + React + TypeScript (`"strict": true`) app; `npm` with a committed
  `package-lock.json`. Commands, run inside `web/`:
  - `npm run dev` -- Vite dev server. It proxies `/api` (HTTP) and `/ws` (WebSocket, `ws: true`)
    to the local backend at `http://127.0.0.1:<port>`, where `<port>` is `SA_SERVER_PORT` when
    set and `8765` otherwise (the `server.port` default of `studentassistant.config`); the rule
    is `backendTarget()` in `web/backend-target.ts`.
  - `npm run build` -- type-checks (`tsc` over `tsconfig.json` and `tsconfig.node.json`; a type
    error fails the build) and writes the bundle to `backend/src/studentassistant/server/static/`
    (`build.outDir`, emptied on each build, gitignored). The backend serves that directory at
    `/` (#85).
  - `npm test` -- vitest with Testing Library in `jsdom` (`src/test/setup.ts` loads
    `@testing-library/jest-dom`); `scripts/test.sh web` runs it with `--run`.
- `src/Router.tsx` picks the page from `window.location.pathname` (`/pair` -> `PairPage`,
  `/subjects/<subject>/topics/<topic>` -> `TopicPage`, `/subjects/<subject>/topics/<topic>/notes`
  -> `NotesPage`, `/subjects/<subject>/topics/<topic>/pending` -> `PendingPage`,
  `/subjects/<subject>/topics/<topic>/versions` -> `VersionsPage`, anything else
  -> `App`); the backend's SPA fallback serves the app for every non-API path, so
  no router library is used.
- `src/pairing/api.ts`: `requestPairingCode()` -> `{kind: "ok", pairing} | {kind: "refused"} |
  {kind: "error", status} | {kind: "unreachable"}`, and `qrPayload(pairing)`.
- `src/desk/api.ts`: the study desk's read client. `fetchSubjects()`, `fetchTopics(subjectId)`
  (decoded strictly as protocol `rest.subjects.list.response` / `rest.topics.list.response`) and
  `fetchTopicSummary(subjectId, topicId)` (`GET /api/subjects/{s}/topics/{t}/summary`, #38,
  decoded as `TopicSummary`) -> `{kind: "ok", value} | {kind: "not-found", detail} | {kind:
  "error", status} | {kind: "unreachable"}` (`describeFailure()` puts a failure in one Spanish
  sentence); `topicPath(s, t)` builds the topic page path. The web app declares no protocol
  version on REST: served by the backend's own build, it is answered as that backend's version
  (>= 1.1), so topics may carry `last_session_at_ms`, `pending_count` and (1.3)
  `digest_excerpt`; all stay optional, the desk does not show the excerpt (its topic summary
  does), and a topic without them shows only its name.
- `src/App.tsx` is the study desk (`/`, heading "Mesa de estudio"): every subject (a region named
  after it) with its topics, each a link to its topic page followed by "Sesión abierta", "Última
  sesión: <fecha>" and "<n> dudas por revisar" when the list carries them. Empty states: no
  subjects, a subject without topics; a failing topic list is reported inside its subject only.
- `src/topic/`: `TopicPage` (`← Mesa de estudio` link, heading "Tema <topic name>", "Asignatura
  <subject name>", the ids until the lists answer) shows `TopicCard` and `PdfUploadForm`; an
  unknown topic (404) shows the backend's Spanish detail and no upload form, and a successful
  upload (`onImported`) reloads the card. `TopicCard` is the card of VISION §2 ("Resumen del
  tema"): Fuentes (✓/○ handwritten pages, book pages, PDF, webs), Sesiones (count and minutes of
  conversation), Pendiente (doubts to review), Material (`Apuntes v<N>` from `notes_version`, then
  Esquema, Quiz, Flashcards, Examen, Diapositivas marked present when a file under `generated/`
  is named `outline`/`quiz`/`flashcards`/`exam`/`slides` or their Spanish names, `MATERIALS`).
  `PrepareTopic` ("Prepárame el tema", below) sits above the upload form.
  `PdfUploadForm` ("Añadir un PDF": a file input, an optional "Páginas" text such as `82-94`, sent
  as typed). `api.ts`: `uploadPdf(subjectId, topicId, file, pages)` posts the multipart form to
  `POST /api/subjects/{s}/topics/{t}/sources/pdf` -> `{kind: "ok", imported} | {kind: "refused",
  status, detail} | {kind: "error", status} | {kind: "unreachable"}`; a refusal's Spanish
  `detail` (413 too large, 422 unreadable or bad range) is shown as it comes.
  The card's "Apuntes v<N>" is a link to the notes viewer once a notes version exists, followed by
  "(versiones)", a link to the notes version history, and its
  Pendiente item always links to the pending-doubts panel.
- `src/notes/` (#52): the notes viewer. `NotesPage` (`← Tema <name>` link, "Apuntes de <name> ·
  versión <N>") fetches `GET /api/subjects/{s}/topics/{t}/notes` and renders it with `NotesView`;
  a 404 shows the backend's Spanish detail ("Todavía no hay apuntes de este tema.").
  - `markdown.ts`: `parseNotes(text) -> {blocks, footnotes}` and `parseInline(text)`, a reader of
    the notes format of docs/modules/editor.md (headings with `{#anchor}`, paragraphs, nested and
    loose lists, pipe tables, rules, fenced code, quotes, footnote definitions; inline strong/em,
    code, links, `[^label]`, `[[?word]]`). It builds a tree rendered as React elements, so the
    notes' HTML is never markup; only `http(s):`, `mailto:` and `#` links become links.
  - `provenance.ts`: `parseProvenance(label, definition)` -> `page` (notes/book), `pdf` (file,
    `#page=K`), `web`, `transcript` (session id, span), `ia` or `unknown`; `sourceVaultId`,
    `originalPage(meta, page)` (`first_page + page - 1`, the rule of `sources.pdf.original_page`).
  - `NotesView`: headings keep their anchor as `id` plus a `#` link; each reference is a link to
    its definition (`#fn-<label>`, numbered by first citation, `[IA]` for `[^ia]`) that opens the
    sources panel; blocks citing `[^ia]` get the `notes-ia` highlight; the definitions are listed
    under "Fuentes" and open the panel too. `[[?word]]` is underlined as a doubtful word.
  - `SourcePanel` (non-modal `dialog` named after the source): a notes/book page shows the
    flattened `page-NNN.page.jpg` (falling back to the cited file) with zoom (Alejar/Acercar/
    Tamaño original, `+`/`-`/`0` on the focused image) and its transcription (the sidecar's
    `transcription`, else `page-NNN.md`); a PDF page shows `page-NNN.pKKK.jpg` and `.txt`,
    "PDF «<original_name>», página <original page>" and an "Abrir el PDF" link; a web snapshot its
    text and sidecar `url`; a transcript span its segments with `MM:SS` timestamps
    (`GET /api/sessions/{id}/transcript`). Focus moves to the panel title; Escape or "Cerrar"
    closes it and returns the focus to the reference. The panel is fixed to the viewport edge
    (a bottom sheet under 40rem), so it never scrolls or rewraps the notes.
  - `api.ts`: `fetchNotes`, `fetchSourceMeta`, `fetchSourceText`, `fetchTranscript` (all
    `ReadResult`), `sourceUrl(vaultId)`. Images load by plain `<img src>`, so they rely on the same
    localhost trust as every other request of the web app.
  - Beside the notes (a sticky column from 80rem, below them otherwise) `NotesPage` shows the
    editor chat (`src/chat/`, #71). After a turn or an undo that changed the notes it reads them
    again (only the latest read is shown) and `NotesView` highlights (`notes-changed`) every
    top-level block inside a section the turn touched (`changedSections`, the turn's
    `changed_sections` anchors, subsections included); an undo clears the highlight. `NotesView`'s
    `onAskWhy` puts a "¿Por qué?" button (named "¿Por qué pusiste esto?") on every top-level block
    with text, disabled while the editor is busy (`askDisabled`); the page sends `whyQuestion(block,
    section)` through the same chat. A topic without notes (404) shows `PrepareTopic` under the
    backend's detail and reads the notes again when it is done; the chat appears once notes exist.
- `src/chat/` (#71): the chat with the editor over the editor chat API (docs/modules/server.md).
  - `sse.ts`: `readSse(body, onEvent)`, a reader of a `text/event-stream` body from a `fetch` POST
    (events split at blank lines, `event:` + joined `data:` lines, comments ignored; rejects when
    the stream breaks).
  - `api.ts`: `sendChatMessage(s, t, message, {confirmOverCap, onDelta, onRestart})` posts `POST
    .../notes/chat` and reads its stream (`reply.delta` -> `onDelta(text, attempt)`,
    `reply.restart` -> `onRestart(attempt)`) -> `ChatOutcome` = `ActionResult<RevisionResult>`
    (an error before the stream or an `error` event is `refused` with its status and Spanish
    `detail` and `code`, `overCap` when the code is `cost_cap_reached`, never read from the
    wording) `| {kind: "interrupted"}` when the stream ends or
    breaks before `result`/`error`; `fetchChatHistory -> ReadResult<ChatHistory>` (`GET
    .../notes/chat`), `undoLastTurn -> ActionResult<UndoResult>` (`POST .../notes/chat/undo`),
    `describeChatFailure`. Bodies are read leniently (`readRevision`, `readHistory`, `readUndo`).
  - `diff.ts`: `parseDiff(unified)` -> hunk/add/del/context lines (file header dropped),
    `diffStats`. `DiffView` shows a turn's diff in an open `details` "Cambios en los apuntes
    (<n> líneas añadidas, <m> quitadas)", added lines in `<ins>`, removed ones in `<del>`.
  - `why.ts`: `blockExcerpt(block)` (the block's text without footnote references nor `[[?..]]`
    marks, cut at `EXCERPT_CHARS` = 280) and `whyQuestion(block, section)` -> `"¿Por qué pusiste
    esto? (en la sección #<anchor>) «<excerpt>»"` (`null` for a rule). Until the dedicated "¿por
    qué?" of #69 exists, the editor answers it as a chat-only turn (the revise prompt already
    handles questions without edits).
  - `useEditorChat(s, t, onNotesChanged)`: reads the conversation once, then runs one turn or undo
    at a time (`busy`); the running turn's reply grows with each delta and restarts on
    `reply.restart`; the `result`'s `reply` replaces it. `canUndo` starts from the history's
    `can_undo` and turns on after an applied turn with a commit. A cut stream reads the history
    and the notes again (the turn goes on in the backend). A reached cost cap sets `overCap`, and
    `retry` repeats the message with `confirm_over_cap` in place of the failed entry. An undo
    says "Se ha deshecho el cambio «<summary>».", marks the turn undone and reads the history
    again (diffs of this page's turns are kept by commit).
  - `EditorChat` ("Hablar con el editor"): a `log` of the turns ("Tú:" / "Editor:", "El editor
    está pensando…" until the first delta, "Cambio aplicado: <summary>" or "Cambio deshecho:
    ...", the turn's warning, a failure as an alert, the `DiffView` of a live applied turn), a
    hint while empty, "Mensaje para el editor" (Enter sends, Shift+Enter is a new line; up to
    4000 characters), "Enviar" and "Deshacer el último cambio" (enabled when `canUndo` and idle),
    and "Continuar igualmente" after a reached cost cap.
- `src/pending/` (#80): the pending-doubts panel and the doubts-resolution flow. `PendingPage`
  (`← Tema <name>` link, "Dudas pendientes", "<N> dudas por revisar" in a polite live region, a
  "Por revisar / Cerradas / Todas" filter applied on the page, `applyFilter`) reads the editor's
  doubts queue `GET /api/subjects/{s}/topics/{t}/doubts` (#68) and reads it again every `POLL_MS`
  (5 s, `pollMs` prop) while the page is visible, so the count and cards follow a live session; a
  failed re-read keeps the last queue and says so, and an older read never overwrites a newer one.
  Cards are grouped by kind (`byKind`: contradiction, possible_error, illegible, incomplete,
  unexplained_concept, then unknown kinds), one region per kind. The topic's notes are shown under
  the cards ("Apuntes · versión <N>", `NotesView` with the `SourcePanel`).
  - One doubt at a time: the open doubt being resolved (the queue's `current`, unless the student
    pressed "Resolver esta duda" on another) carries `DoubtResolver`, a form "Resolver la duda":
    the editor's question (or a note that there is none yet), one button per suggested answer
    (`{"suggestion": n}`, 1-based), for a contradiction the options as radios "<source>: «says»"
    (`sourceLabel`: "Tus apuntes, página 3", "El libro, página 12", "El PDF, página 82"...) with
    "Guardar también una nota con lo que dicen las otras" (`keep_discarded`), free text (alone, or
    as the comment of a source), "Responder" and "Descartar". After an answer or a dismissal the
    page says what happened ("Duda resuelta: <resolution>", "Los apuntes se han actualizado.", the
    warning), reads the queue again and, when `notes_changed`, the notes. Only one doubts operation
    of the page runs at a time.
  - "Preparar las preguntas" (shown while an open doubt has no question) calls `POST
    .../doubts/review` and says what it did (`describeReview`: "El editor ha resuelto 2 dudas con
    tus fuentes y tiene 1 pregunta para ti.").
  - Refusals show the backend's Spanish `detail` (409 closed doubt, unended session, another
    operation running, no notes yet; 422; 502; 503). The page branches on the error body's `code`
    (protocol 1.2, never on the wording of `detail`): an unknown doubt (404) or a closed one
    (`doubt_closed`) makes the page read the queue again; a reached cost cap (`cost_cap_reached`)
    offers "Continuar igualmente", which repeats the same request with `confirm_over_cap: true`.
  - `PendingCard`: an `article` "<kind label>: <text>" with the status, a per-kind hint while
    open, what it refers to (`describeRefs`: pages, conversation fragments, sources), merged
    duplicates, the resolution once closed, and its `children` (the form or the pick button).
  - `api.ts`: the observer's queue `fetchPending(subject, topic, filter) -> ReadResult<TopicPending>`
    (`GET .../pending`, strict `decodeTopicPending`), `kindLabel`, `statusLabel`.
  - `doubts.ts`: `fetchDoubts -> ReadResult<DoubtsQueue>` (strict `decodeDoubtsQueue`: items
    `{item, question, outcome}`), `reviewDoubts(s, t, confirmOverCap)`, `answerDoubt(s, t, id,
    answer, confirmOverCap)`, `dismissDoubt(s, t, id)` -> `ActionResult<T>` = `{kind: "ok", value}
    | {kind: "refused", status, detail, code, overCap} | {kind: "error", status} | {kind:
    "unreachable"}` (results read leniently: `ReviewResult` {`auto_resolved`, `asked`,
    `notes_changed`, `warning`}, `ResolutionResult` {`pending_id`, `status`, `resolution`,
    `notes_changed`, `warning`}), `describeActionFailure`, `describeReview`, `isOverCap(code)`, and the
    generic `postAction(path, body, read)`.
- `src/versions/` (#72): the notes version history over the versions API of #64
  (docs/modules/server.md). `VersionsPage` (`← Tema <name>` link, "Versiones de los apuntes de
  <name>") lists every version newest first ("Versión <N>", "la de los apuntes actuales" on the
  one `apuntes.md` is, the tag date in `es-ES`, the commit message) and says when the notes
  changed after the latest version. "Comparar": "Desde"/"Hasta" selects ("Hasta" also offers
  "Apuntes actuales", `to` `null`), starting at `defaultComparison` (the latest version against
  the current notes when they changed after it, else the latest two; none with a single unchanged
  version); only the latest comparison read is shown. "Ver los cambios": "En línea" or "Lado a
  lado". "Restaurar la versión <N>" (every version but the current one) asks for a confirmation
  (a group with "Sí, restaurar" / "Cancelar"), then says "Se ha restaurado la versión <K> como
  versión <N>." and the backend's warning, and reads the history (and so the comparison) again;
  a refusal (409 another notes operation, already that version) shows its Spanish `detail`.
  - `VersionDiffView`: one `article` "<title>: <Nueva|Quitada|Modificada>" per section that
    changed or moved (`sectionTitle`: the newer title, "Inicio de los apuntes" for the preamble),
    with "Sección renombrada, antes «<old>», cambiada de sitio.", the line counts and the section's
    diff, inline (`<ins>`/`<del>`) or as a two-column table (`sideBySide(lines)`, removed runs
    paired with the added runs next to them); the unchanged sections in a closed `details`; the
    footnote labels under "Fuentes citadas"; identical versions say so.
  - `api.ts`: `fetchVersions`, `fetchVersionDiff(s, t, from, to | null)`, `restoreVersion(s, t,
    n)` -> `ActionResult` (bodies read leniently: `readVersions`, `readDiff`, `readRestore`).
- `src/topic/PrepareTopic.tsx` (#80): "Prepárame el tema" on the topic page. `generateNotes(s, t,
  confirmOverCap)` posts `POST .../notes/generate`; when the result is not a draft the component
  then calls `POST .../doubts/review`, as the doubts API asks of the web, and shows "Apuntes v<N>
  listos.", what the review did and a "Ver las dudas" link to the panel. A draft is reported and
  not reviewed. Refusals are shown in Spanish; a reached cost cap offers "Continuar igualmente",
  which repeats the step that stopped with `confirm_over_cap`. The topic card reloads afterwards.

## Boundaries
- Talks only to the backend REST/SSE API; no direct vault or LLM access.

## Tests
vitest + Testing Library with a mocked API (`src/test/mockApi.ts`: `stubApi({path: response})`
stubs `fetch` by method and path; `sseResponse(events)` is a complete event stream and
`streamResponse()` one the test feeds event by event with `push`, `close` and `fail`).
