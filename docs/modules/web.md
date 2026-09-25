# Module: web

**Lives in:** `web/`.

## Responsibility
- **Web capture page** (`/capture`, `src/capture/`): the development capture client (ADR-0001,
  ADR-0008) -- the laptop's own camera and microphone, speaking capture protocol v1 exactly like
  the Android app. Two steps: the student picks a subject and a topic (creating either one if it
  is missing), and the page resumes that topic's `open_session_id` or starts a new session; from
  then on the capture screen runs it -- camera preview, bursts of 3 high-resolution stills
  uploaded as one multipart `POST /api/sessions/{id}/captures`, the **Capturar** / **Importante**
  / **Libro** / **Apuntes** / **Terminar** buttons, a thumbnail strip with each burst's upload
  state (subiendo / guardada / duplicada / error, a duplicate counting as stored), the transcript
  the backend normalises (partials grey, finals black, a final replacing the partials that share
  its `segment_id`) and the pending-doubts counter. It opens the session socket with `hello`
  first, keeps `hello.ack.clock_offset_ms`, and lets `hello.ack.stt_mode` choose the recognizer:
  the Web Speech API (`es-ES`) in `client` mode, microphone audio streamed as PCM16 frames in
  `server` mode. In `client` mode it biases the recognizer towards the session's vocabulary
  hints (protocol 1.4, #227): the list of `hello.ack.vocabulary_hints`, replaced by every
  `notice` that carries one, becomes the `phrases` of each recognition it (re)starts where the
  browser has contextual biasing (`SpeechRecognitionPhrase`); a browser without it, or whose
  service answers `phrases-not-supported`, recognizes without them and shows nothing. In
  `server` mode a degraded `stt.status` (protocol 1.5, #222) shows its Spanish `detail` (or a
  fallback sentence per state) as an alert, "Estado de la transcripción", until an `ok` status
  clears it; the session goes on meanwhile. A server `capture_now` command takes a burst with that `command_id` and is
  answered with an `ack`. Denied or missing camera/microphone, a browser without
  `SpeechRecognition`, a non-secure context and a lost backend connection each get their own
  Spanish explanation. The page runs on the PC itself under loopback trust
  (`docs/modules/server.md`), so it asks for no token and stores nothing: no token, no session
  state, no offline spool (the Android app owns the spool).
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
  `/capture` -> `CapturePage`, `/live` -> `LivePage`,
  `/subjects/<subject>/topics/<topic>` -> `TopicPage`, `/subjects/<subject>/topics/<topic>/notes`
  -> `NotesPage`, `/subjects/<subject>/topics/<topic>/pending` -> `PendingPage`,
  `/subjects/<subject>/topics/<topic>/versions` -> `VersionsPage`, `.../quiz` -> `QuizPage`,
  `.../material/<name>` -> `MaterialPreviewPage`, `/subjects/<subject>/style-guide` -> `StyleGuidePage`, anything else
  -> `App`); the backend's SPA fallback serves the app for every non-API path, so
  no router library is used.
- `src/capture/` is the capture page. Nothing outside the directory imports it except
  `src/Router.tsx`, and inside it only `api.ts` and `sessionSocket.ts` reach the network:
  - `CapturePage.tsx`: `CapturePage` -- shows `SessionPicker` until a session is open, then
    `CaptureScreen` keyed by `session_id`, so a second session in the same visit is a new
    component and not the old one with new props. That state is all the page remembers: nothing
    of a session survives a reload.
  - `SessionPicker.tsx`: `SessionPicker({onSession?, now?})` -- subject and topic lists with
    their create forms; for the chosen topic it resumes `open_session_id` or starts a new
    session, and reports the result as `OpenedSession {session, subjectName, topicName}`.
  - `CaptureScreen.tsx`: `CaptureScreen({session, subjectName, topicName, onEnded?, now?,
    playShutter?, flashMs?})` -- the running session, where socket, transcriber and camera meet:
    it builds the transcriber `hello.ack.stt_mode` asks for, uploads each burst, renders the
    buttons, the thumbnails, the transcript and the pending counter, and owns every Spanish
    message of the page. Also exports `captureCapabilities()` (which omits `audio_format` when
    `audioStreamSupported()` is false, so the backend cannot pick a `server` mode the client
    could not obey), `shutterClick()` and `FLASH_MS`.
  - `api.ts`: the REST client -- `listSubjects()`, `createSubject(name)`, `listTopics(id)`,
    `createTopic(id, name)`, `startSession(subjectId, topicId, clientTimeMs)`,
    `resumeSession(id)`, `endSession(id, reason, clientTimeMs)` and
    `uploadCaptures(id, metadata, images)` (multipart: the `METADATA_PART` part plus one
    `image_N` part per still). Every call decodes its answer with the `src/protocol/` decoders
    and returns `ApiResult<T>` = `{kind: "ok", value} | {kind: "refused", status, detail} |
    {kind: "error", status} | {kind: "unexpected", status, expected, problem} | {kind:
    "unreachable"}`; `refused` carries the backend's own Spanish `detail` and `unexpected` is a
    2xx body that is not the message the endpoint promises, which is reported and never used.
    `failures.ts`: `describeFailure(prefix, failure)` turns one into a Spanish sentence.
  - `sessionSocket.ts`: `SessionSocket({wsPath, clientTimeMs, capabilities?, onEvent?})` --
    dials the `ws_path` of the start/resume answer (`socketUrl()`), sends `hello` first and
    exposes the handshake as `handshake: Promise<HandshakeResult>` plus the getters
    `protocolVersion`, `sttMode`, `clockOffsetMs` and `audioFormat`; sends
    `sendTranscript(segment, kind)`, `sendButton(button, clientTimeMs, source?)`,
    `sendAck(commandId, clientTimeMs)` and `sendAudio(frame)`, and `close()`. Decoded server
    events arrive through `onEvent` as `SessionSocketEvent` (`transcript`, `command`, `notice`,
    `sttStatus` (1.5), `ack`, `closed`, `failed`, `rejected`). `CAPTURE_CAPABILITIES`, `CLIENT_AUDIO_FORMAT`
    (pcm16 / 16 kHz / mono) and `WEB_SPEECH_PROVIDER` are the `hello` defaults.
  - `transcriber.ts`: the seam a provider is swapped at (ADR-0008) --
    `ClientTranscriber {readonly provider: string; start(): Promise<void>; stop(): void;
    setVocabularyHints?(hints)}` (the latest hints replace the previous ones and apply from the
    next recognition on; a transcriber that has nothing to bias leaves it out), given
    `TranscriberCallbacks {onSegment(segment, kind), onProblem?(problem)}` at construction. A
    failure is a `TranscriberProblem {code, detail, recoverable}` whose `code` is a
    `TranscriberProblemCode` (`unsupported`, `permission-denied`, `network`, `unavailable`);
    `start()` rejects with a `TranscriberError` carrying it. The two implementations:
    `webSpeechTranscriber.ts` (`WebSpeechTranscriber`, `SpeechRecognition` /
    `webkitSpeechRecognition` at `WEB_SPEECH_LANGUAGE = "es-ES"`, continuous with interim
    results, restarting itself on `end` and on recoverable errors, `webSpeechSupported()` to ask
    first; `vocabularyHints` option / `setVocabularyHints()` set each recognition's `phrases` at
    `VOCABULARY_HINT_BOOST` (2.0) where `speechRecognitionPhraseConstructor()` finds the API, and
    stop doing so for the session after `phrases-not-supported`; `SpeechGrammarList` is not used,
    since the specification dropped grammars and no engine applies them) and `audioStreamTranscriber.ts` (`AudioStreamTranscriber`, `audioStreamSupported()`),
    which sends audio to an `AudioFrameSink` -- `SessionSocket.sendAudio` -- and calls
    `onSegment` never, because the transcript comes back as server `transcript.*` events.
  - `audioFrames.ts` + `pcmWorklet.ts`: the binary audio of protocol v1, and no browser API in
    the first. `encodeAudioFrame({seq, clientTimeMs, pcm})` writes `AUDIO_MAGIC` (`"SAAF"`), the
    MAJOR/MINOR version bytes, a big-endian u32 `seq` and a big-endian u64 client time in an
    `AUDIO_HEADER_SIZE` (18) byte header, followed by whole PCM16 little-endian samples; an
    out-of-range field throws `AudioFrameError`. `downmixToMono()`, `pcm16Bytes()` and
    `AudioResampler` (to `PCM_SAMPLE_RATE_HZ` = 16000) do the arithmetic. `pcmWorklet.ts` is the
    processor the audio thread runs (`PCM_WORKLET_PROCESSOR`, `PCM_WORKLET_CHUNK_MS` = 100);
    the main thread imports only its name and the `PcmWorkletChunk` shape.
  - `camera.ts`: `Camera({onLost?})` -- `start(preview?)` opens the track asking for
    `MAX_STILL_EDGE_PX` as an `ideal` edge (a wish, so a smaller camera is not refused for it),
    `takePhoto()` grabs one still (`ImageCapture.takePhoto()` where the browser has it, a canvas
    grab at the track's real `getSettings()` size where it does not), `takeBurst(trigger)`
    returns a `CapturedBurst {metadata, images}` of `BURST_LENGTH` (3) stills with a fresh
    lowercase UUID `capture_id` and one `image_N` entry per still (`imagePartName()`), and
    `stop()` releases everything. A failure is a `CameraError` with a `CameraProblemCode`
    (`unsupported`, `permission-denied`, `missing-device`, `in-use`, `lost`, `unavailable`).
  - The device and protocol modules report codes and an English `detail` for the log; the page
    owns the Spanish, one message per code.
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
  sesión: <fecha>" and "<n> dudas por revisar" when the list carries them ("Sesión abierta" is a
  link to the live session view, `/live`); under each subject's name a "Guía de estilo" link to
  its style guide page. Empty states: no
  subjects, a subject without topics; a failing topic list is reported inside its subject only.
- `src/topic/`: `TopicPage` (`← Mesa de estudio` link, heading "Tema <topic name>", "Asignatura
  <subject name>", the ids until the lists answer) shows `TopicCard`, `PrepareTopic`,
  `MaterialsPanel` ("Material de estudio", `src/materials/`, below), `PdfUploadForm` and
  `WebSearchPanel`; an unknown topic (404) shows the backend's Spanish detail and neither form,
  and a successful upload (`onImported`) or a kept web page (`onKept`) reloads the card. `TopicCard` is the card of VISION §2 ("Resumen del
  tema"): Fuentes (✓/○ handwritten pages, book pages, PDF, webs), Sesiones (count and minutes of
  conversation), Pendiente (doubts to review), Material (`Apuntes v<N>` from `notes_version`, then
  Esquema, Quiz, Flashcards, Examen, Diapositivas marked present when a file under `generated/`
  is named `outline`/`quiz`/`flashcards`/`exam`/`slides` or their Spanish names, `MATERIALS`).
  Generating, previewing and downloading each material is `MaterialsPanel` (#79; the card's
  former "Descargas" row moved there, per material).
  `PrepareTopic` ("Prepárame el tema", below) sits above the materials and the upload form.
  `PdfUploadForm` ("Añadir un PDF": a file input, an optional "Páginas" text such as `82-94`, sent
  as typed). `api.ts`: `uploadPdf(subjectId, topicId, file, pages)` posts the multipart form to
  `POST /api/subjects/{s}/topics/{t}/sources/pdf` -> `{kind: "ok", imported} | {kind: "refused",
  status, detail} | {kind: "error", status} | {kind: "unreachable"}`; a refusal's Spanish
  `detail` (413 too large, 422 unreadable or bad range) is shown as it comes.
  `WebSearchPanel` (#59, section "Buscar en Internet"): a "Qué buscar" search box and "Buscar"
  button (an empty query says "Escribe qué quieres buscar." without calling), then the topic's
  searches ("Búsquedas del tema", newest first, the ones asked by voice too): "«<query>» (pedida
  en voz | pedida aquí | pedida por el editor) — Buscando… | <n> páginas | No se encontró nada
  útil. | No se pudo buscar: <message>", each offered page a link (new tab) with its host, "·
  recomendada" (`relevant`), "· sin confirmar en la búsqueda" (`found_in_search` false), its
  summary and "Guardar como fuente" -- once kept, "Guardada como fuente externa (<source_id>)";
  a refused keep shows "No se ha guardado: <detail>" under the page. While a search is `queued`
  the list is read again every `pollMs` (2 s). A list that cannot be read is reported as plain
  text (no `alert`). `webSearchApi.ts`: `fetchWebSearches(s, t)` (`GET .../web-searches` ->
  `WebSearch[]`: `search_id`, `query`, `requested_by`, `session_id`, `queued_at`, `status`,
  `results` of `WebResult` `url`/`title`/`summary`/`relevant`/`found_in_search`, `reason`,
  `message`, `kept` of `KeptResult` `index`/`url`/`source_id`/`kept_by`), `queueWebSearch(s, t,
  query)` (`POST`, the new `search_id`), `keepWebResult(s, t, searchId, index)` (`POST
  .../{search_id}/results/{index}/keep` -> `KeptSource` `source_id`/`vault_id`/`title`/`url`), all
  `ApiResult` (`ok` | `refused` with the Spanish `detail` | `error` | `unreachable`), and
  `describeApiFailure(result)`; `addWebPage(s, t, url)` (`POST .../web-pages` `{url, via: "url"}`
  -> protocol `WebPageAddResponse`, checked with `decodeWebPageAddResponse`).
  `WebPageForm` (#62, section "Añadir una página web", after the search panel): a «Dirección de
  la página» URL box and «Guardar como fuente» («Descargando…» while the backend fetches it). An
  address that is not `http(s)://...` is refused without calling («Pega la dirección completa de
  la página...»); then «Página «<título>» guardada como fuente externa (<source_id>).», «Esa
  página ya era una fuente del tema: «<título>».» (`already_kept`, the card is not reloaded) or
  «No se ha guardado la página: <detail>». A stored page reloads the topic card (`onAdded`).
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
    under "Fuentes" and open the panel too. `[[?word]]` is underlined as a doubtful word. Web
    snapshots are external sources (#59): their references get `notes-ref-web` (green, dotted)
    and the aria label "Fuente externa (web): <text>" (other sources "Fuente: <text>"), and their
    definition under "Fuentes" gets `notes-footnote-external` and "· fuente externa".
  - `SourcePanel` (non-modal `dialog` named after the source): a notes/book page shows the
    flattened `page-NNN.page.jpg` (falling back to the cited file) with zoom (Alejar/Acercar/
    Tamaño original, `+`/`-`/`0` on the focused image) and its transcription (the sidecar's
    `transcription`, else `page-NNN.md`); a PDF page shows `page-NNN.pKKK.jpg` and `.txt`,
    "PDF «<original_name>», página <original page>" and an "Abrir el PDF" link; a web snapshot its
    text and "Fuente externa: copia de <url> (<fetched_at>)" from its sidecar; a transcript span its segments with `MM:SS` timestamps
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
    with text, disabled while the editor is busy (`askDisabled`), and hands the block, its
    section's anchor and its number as the backend counts blocks (from 1 after a heading of level
    2 or deeper; before the first section from the start, the `# title` included); the page asks
    `chat.ask({section, block, quote: blockExcerpt(block)}, whyQuestion(block, section))`
    (`POST .../notes/why`, #69), and the answer's sources open in the `SourcePanel`. A topic without notes (404) shows `PrepareTopic` under the
    backend's detail and reads the notes again when it is done; the chat appears once notes exist.
- `src/chat/` (#71): the chat with the editor over the editor chat API (docs/modules/server.md).
  - `sse.ts`: `readSse(body, onEvent)`, a reader of a `text/event-stream` body from a `fetch` POST
    (events split at blank lines, `event:` + joined `data:` lines, comments ignored; rejects when
    the stream breaks).
  - `api.ts`: `askWhy(s, t, {section, block, quote}, {confirmOverCap, onDelta, onRestart})` posts
    `POST .../notes/why` and reads the same kind of stream -> `WhyOutcome` =
    `StreamOutcome<ExplanationResult>` (`question`, `reply`, `refs`: `{label, kind, text}`,
    `warning`; `readExplanation`); history turns carry `kind` (`revise`/`explain`) and `refs`.
    `sendChatMessage(s, t, message, {confirmOverCap, onDelta, onRestart})` posts `POST
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
    esto? (en la sección #<anchor>) «<excerpt>»"` (`null` for a rule), the entry shown while the
    answer streams (then the backend's `question`, the same form).
  - `useEditorChat(s, t, onNotesChanged)`: reads the conversation once, then runs one turn or undo
    at a time (`busy`); the running turn's reply grows with each delta and restarts on
    `reply.restart`; the `result`'s `reply` replaces it. `canUndo` starts from the history's
    `can_undo` and turns on after an applied turn with a commit. A cut stream reads the history
    and the notes again (the turn goes on in the backend). A reached cost cap sets `overCap`, and
    `retry` repeats the message (or the "¿Por qué?") with `confirm_over_cap` in place of the
    failed entry. `ask(anchor, message)` runs a "¿Por qué?" the same way; its entry gets the
    answer's `refs`. An undo
    says "Se ha deshecho el cambio «<summary>».", marks the turn undone and reads the history
    again (diffs of this page's turns are kept by commit).
  - `EditorChat` ("Hablar con el editor"): a `log` of the turns ("Tú:" / "Editor:", "El editor
    está pensando…" until the first delta, "Cambio aplicado: <summary>" or "Cambio deshecho:
    ...", the turn's warning, a failure as an alert, the `DiffView` of a live applied turn), a
    hint while empty, "Mensaje para el editor" (Enter sends, Shift+Enter is a new line; up to
    4000 characters), "Enviar" and "Deshacer el último cambio" (enabled when `canUndo` and idle),
    and "Continuar igualmente" after a reached cost cap. An entry with `refs` lists "Fuentes:",
    each a button ("Ver la fuente: <text>") that opens it in the sources panel (`onOpenSource`).
  - Proposed style rules (#216): a turn's `proposed_style_rules` (the `result` event and the
    history turns, which carry only those the subject's guide does not have yet) become the
    entry's `proposedRules`, shown in a group "Propuesta para la guía de estilo", each «rule»
    with "Guardar para toda la asignatura" (named "Guardar para toda la asignatura: <rule>").
    `useEditorChat`'s `confirmRule(rule)` posts it alone to `POST
    /api/subjects/{s}/style-guide/rules` (one at a time, `confirming`); once saved, that rule and
    any other already in the answered guide (`sameRule`) leave every entry, and `ruleNotice`
    says "Guardado en la guía de estilo de la asignatura: «rule»." (or that the guide already
    had it, or why it failed) with a "Ver la guía de estilo" link (`styleGuidePath`).
- `src/styleGuide/` (#216): the subject's style guide over the API of #70
  (docs/modules/server.md). `StyleGuidePage` (`/subjects/<s>/style-guide`: `← Mesa de estudio`,
  "Guía de estilo de <subject name>") lists the rules ("Reglas de la guía de estilo"), each with
  "Editar" (a "Regla <n>" text box, "Guardar"/"Cancelar") and "Borrar", and a "Nueva regla" box
  with "Añadir" (disabled at `MAX_RULES`, 50; up to `MAX_RULE_CHARS`, 300, characters). Every
  change writes the whole list (`PUT .../style-guide`) and shows the list the backend answers;
  a rule already in the guide (`sameRule`: list marker dropped, spaces collapsed, case ignored)
  is refused on the page; a refusal (422 invalid rule, 404 unknown subject, 503) shows its
  Spanish `detail`. An unknown subject shows the backend's detail and no form.
  - `api.ts`: `fetchStyleGuide(s) -> ReadResult<StyleGuide>` (`subject`, `rules`, `added`,
    `commit`; `readStyleGuide`, lenient), `confirmStyleRules(s, rules)` (POST `.../rules`) and
    `saveStyleGuide(s, rules)` (PUT) -> `ActionResult<StyleGuide>` (a FastAPI validation list as
    `detail` is a plain `error`), `sameRule`, `styleGuidePagePath(s)`.
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
- `src/live/` (#57): the live session view, `/live` (`← Mesa de estudio`, "Sesión en directo"),
  read-only, over the backend's `GET /api/live` stream (docs/modules/server.md).
  - `live.ts`: `subscribeLive(onEvent, onConnection, factory = defaultSource)` opens an
    `EventSource` on `LIVE_URL` (tests hand a `LiveSourceFactory`, `src/live/testLive.ts`'s
    `fakeSources()`), reads each event leniently (`parseLiveEvent(name, data)`: an unusable one is
    dropped) and returns the close function; `onConnection(false)` on an error (the source
    reconnects by itself), `true` when it opens again. `reduceLive(state, event)` folds the events
    into a `LiveState` (`phase` `connecting`/`idle`/`live`/`ended`, `session`, `segments`,
    `partial`, `captures`, `outline`, `openPending`): a snapshot replaces everything, except that a
    snapshot without a session keeps the last session shown and marks it ended; a final replaces
    the partial of its segment and a late partial is ignored; captures are updated by id.
    `outlineTree(sections)` nests the outline (an orphan stays at the top), `contextLabel`,
    `statusLabel`.
  - `LivePage`: a `status` region (connecting, "No hay ninguna sesión en marcha...", "La sesión ha
    terminado.", a lost connection), then "Tema <name>" (a link to the topic page; the name from
    `fetchTopics`, the id until it answers) with "en marcha" while live; "Transcripción" (a
    polite `log` of the finals with `MM:SS`, the current partial in italics below), "Esquema"
    ("<n> dudas por revisar" and the nested sections with "<n> fragmentos"), "Páginas capturadas"
    (one `article` per capture, "Apuntes, página 3" or "<Apuntes|Libro|PDF|Web|Página> <n>", its
    time, the `page_path` image through `sourceUrl`, "Transcribiendo…"/"Transcrita"/"No se pudo
    transcribir: <message>" and the transcription in a `details`). The stream is closed when the
    page goes away.
- `src/quiz/` (#75): `QuizPage` at `<topic path>/quiz` (the topic card's "Quiz" links to it;
  `← Tema <name>` link, heading "Quiz de <name>", "<n> preguntas · dificultad <d> · de los
  apuntes v<N>", a `note` when the quiz is stale). Each question is a group "Pregunta <n>" with
  radios (multiple choice, true/false) or a "Tu respuesta" text; "Corregir" shows per question
  "✓ Correcta" or "✗ Incorrecta. La respuesta es: …", the explanation and "En los apuntes:" links
  to `<topic path>/notes#<anchor>`; a short answer that does not match (compared like the backend,
  `normalizeAnswer`) asks "¿La has acertado?" (Sí/No). "Aciertos: <c> de <n>"; "Guardar
  resultado" (once every short answer is judged) posts the attempt and shows "Resultado guardado:
  <c> de <n>." with "Repetir el quiz". "Intentos anteriores" lists the latest ten results. The
  form "Generar un quiz" ("Número de preguntas" 1-30, "Dificultad" Variada/Fácil/Media/Difícil,
  "Generar quiz"; "Generar igualmente" past a cost cap) posts `POST .../generated/quiz` and reads
  the quiz again. `api.ts`: `fetchQuiz`, `fetchQuizResults`, `generateQuiz`, `saveQuizResult` ->
  `ActionResult` (read leniently: `readStoredQuiz`, `readResult(s)`).
- `src/materials/` (#79): `MaterialsPanel`, section "Material de estudio" on the topic page, over
  `GET .../generated` (`MaterialsStatus`) and `GET /api/generators` (only for each kind's
  description). One item per kind in the order of study (`STUDY_ORDER`: esquema, quiz, flashcards,
  examen, diapositivas, then any other alphabetically), named by its Spanish title: "○ Sin
  generar" or "✓ Generado el <fecha y hora> · de los apuntes v<N>", a "Desactualizado" badge with
  the backend's `stale_reason` when stale, then its links -- "Hacer el quiz" (the quiz page),
  "Ver <nombre>" per top-level `.md` file (the preview page), a `download` link per
  `.apkg`/`.csv`/`.pdf`/`.pptx` ("flashcards (Anki)", "diapositivas (PowerPoint)"...) -- and
  "Generar" / "Generar de nuevo" ("Generando…" while it runs), which posts `POST
  .../generated/<kind>` with default options (`{options: {}, confirm_over_cap}`; the quiz page
  keeps its own options form). The button is disabled while running and while the topic has no
  notes ("Todavía no hay apuntes: prepara el tema..."); a kind no longer registered (no title)
  has none. A refusal is an `alert` with the Spanish `detail`, plus "Generar igualmente" past the
  cost cap; a generation's warnings are listed under the item. After a generation the page
  reloads (`onGenerated`: the card and, through `refreshKey`, the section); without
  `onGenerated` the section reads itself again. A list that cannot be read is plain text.
  `MaterialPreviewPage` at `<topic path>/material/<name>` (a top-level file name): `← Tema
  <name>`, heading "<title> de <tema>" (the file's stem in brackets when it is not the kind's own,
  "Ejercicios y examen (examen-soluciones)"), "De los apuntes v<N> · Descargar <name>", a `note`
  "Desactualizado <reason>" when stale, then the file (`GET .../generated/files/<name>` as text)
  rendered by `NotesView` over `parseNotes`, so no markup of it reaches the DOM (a mind map or a
  Marp deck shows as its Markdown source). `api.ts`: `fetchGenerators`, `fetchMaterials`,
  `fetchGeneratedText` (`ReadResult`), `generateMaterial(s, t, kind, confirmOverCap)`
  (`ActionResult<Generated>`: `kind`, `notesVersion`, `warnings`), `fileUrl`, `previewPath`,
  `generatedName`; lenient readers `readGenerators`, `readMaterials`, `readGenerated`.
- `src/topic/PrepareTopic.tsx` (#80): "Prepárame el tema" on the topic page. `generateNotes(s, t,
  confirmOverCap)` posts `POST .../notes/generate`; when the result is not a draft the component
  then calls `POST .../doubts/review`, as the doubts API asks of the web, and shows "Apuntes v<N>
  listos.", what the review did and a "Ver las dudas" link to the panel. A draft is reported and
  not reviewed. Refusals are shown in Spanish; a reached cost cap offers "Continuar igualmente",
  which repeats the step that stopped with `confirm_over_cap`. The topic card reloads afterwards.

## Boundaries
- Talks only to the backend REST/SSE API; no direct vault or LLM access.
- The capture page adds one thing to that surface: the session WebSocket at the `ws_path` the
  start/resume answer returned. It is the only socket `web/` opens, and `src/capture/` modules
  reach the wire only through `api.ts` (REST) and `sessionSocket.ts` (WebSocket) -- the
  transcribers and the camera never call `fetch` or open a socket themselves.
- Every JSON body, in and out, is encoded and decoded by the TypeScript bindings of
  `src/protocol/`; the capture page adds no message type and no schema of its own. The one wire
  format it does implement itself is the binary audio frame, in `audioFrames.ts`, because the
  bindings carry no binary layout -- module:protocol owns that definition (`protocol/README.md`).

## Tests
vitest + Testing Library with a mocked API (`src/test/mockApi.ts`: `stubApi({path: response})`
stubs `fetch` by method and path; `sseResponse(events)` is a complete event stream and
`streamResponse()` one the test feeds event by event with `push`, `close` and `fail`).
`src/capture/testing/` holds the fakes the capture
tests run on, because jsdom has none of these APIs: `installCaptureFakes()` installs the media
devices / stream / track, `ImageCapture`, `SpeechRecognition`, `WebSocket` and
`AudioContext`/`AudioWorklet` fakes at once (`installMediaFakes()`,
`installSpeechRecognitionFake()`, `installWebSocketFake()` and `installAudioFakes()` install one
family each, `installCanvasFakes()` and `fakePreview()` cover the canvas fallback and the preview
element), and every installer returns a `restore()`. `installSpeechRecognitionFake(globals,
{phrases: true})` (or `installCaptureFakes({speechPhrases: true})`) is a browser with contextual
biasing: `FakeBiasingSpeechRecognition` records the `phrases` each `start()` found in
`phrasesAtStart`, and `FakeSpeechRecognitionPhrase` sits on the `SpeechRecognitionPhrase` global. Tests drive them -- a fake recognition emits
results, ends and errors, a fake socket records what was sent and lets a test push server events
in -- so no test touches a real camera, microphone, network or backend.
