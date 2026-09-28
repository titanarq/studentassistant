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
  Spanish explanation. The backend closes the session socket itself when the session ends
  (`SESSION_NOT_ACTIVE_CLOSE`, 4404): a close while the page's own end request is in flight is
  the normal end, not a lost connection (#319; if the end then fails, both are shown), and a 4404
  close mid-capture says "La sesión ha terminado en el servidor…" instead of the lost connection. A long session does not silently lose the screen or the camera (#256):
  while a session runs the screen holds a Screen Wake Lock (`wakeLock.ts`), asked for again each
  time the page becomes visible and released on **Terminar**, a lost connection or unmount (a
  browser without the API, or a refusal, is silent); a camera track that ends mid-session (lid
  closed, unplugged, another app took it) shows the "Cámara desconectada" alert ("La cámara se ha
  desconectado…") with **Reactivar cámara**, which requests the camera again on the same preview
  (a refusal keeps the alert with its own sentence) while **Capturar** stays disabled; and none of them touches the socket, the transcript or the uploaded bursts. The capture runs
  only while the tab is visible (#425, replacing #256's "Aviso de pestaña oculta"): when
  `document.visibilityState` turns `hidden` the screen stops the camera and the recognizer (or the
  audio stream), sends `button: pause` (`reason: "hidden"` since #454) over the still-open socket and shows the status line
  "Captura en pausa" («Captura en pausa: la pestaña está oculta»); visible again it sends
  `resume` and starts the camera and the microphone again in the connection's STT mode. A screen
  opened in a hidden tab says `pause` right after `hello`, and a reconnect while hidden says it
  again after the new `hello.ack`. When nothing sends for `[server] capture_idle_end_seconds` the
  backend ends the session itself; its 4404 close reason ends with `(idle)` and the page says «La
  sesión terminó por inactividad…». Failures behind the scenes (#262) show as a discreet status list,
  "Estado del servidor", one Spanish line per problem ("El observador ha fallado 3 veces: …",
  "El observador está en pausa: …", "No se han podido transcribir N páginas: …", "La bóveda no se
  ha podido subir a GitHub (N veces): …"), nothing while the session is healthy and never a
  modal: the screen asks `GET /api/sessions/{id}/health` every `HEALTH_POLL_MS` (15 s; the first
  ask after one interval, one ask at a time) while the session runs, and stops on **Terminar**, a
  lost connection, unmount or a 404. The web has no **Terminar y preparar apuntes** since #413
  (human decision 2026-09-27: it stays only on the Android app): **Terminar** ends the session
  with the 1.0 body (no `prepare_notes`), and on the standalone page it leads to "Sesión
  terminada" (`SessionEnded` in `CapturePage.tsx`), which says the notes are built in Construir,
  where the chat prepares the whole topic on request («prepárame el tema»), links **Abrir en
  Construir** (`.../workspace` of the session's topic) and offers **Volver a la lista de
  sesiones** (the picker). The page runs on the PC itself under loopback trust
  (`docs/modules/server.md`), so it asks for no token and stores nothing: no token, no session
  state, no offline spool (the Android app owns the spool).
- **Capture reconnect** (#411): a dropped capture connection (a backend restart, a blip) no
  longer ends the capture. On a close that is not final (anything but the page's own end, 4404,
  or a protocol/trust refusal: 1002, 1003, 1007, 1008) of a running session, in either STT
  mode since #419, `SessionSocket` (given `reconnect`) reports `reconnecting` once and retries with the
  backoff `RECONNECT_DELAYS_MS` (1, 2, 5, 10 s, then every 30 s; reset by every resume). Each
  attempt first asks `POST /api/sessions/{id}/resume` (a restarted backend refuses the socket
  of an unresumed session): no answer or a 5xx tries again later, any other refusal ends it
  like a 4404 close; then it dials again with a new `hello` (a new clock offset). While
  offline, finals, buttons, markers and command `ack`s wait in a bounded in-memory queue
  (`MAX_QUEUED_FRAMES`, 500, oldest dropped); partials and audio are dropped. After the new
  `hello.ack` the queue goes out in order and the socket reports `reconnected` (the backend
  drops a final whose `segment_id` it already handled, so a frame sent twice is harmless). In
  `server` STT mode (#419) the socket puts its own `seq` on every audio frame, since the
  backend feeds frames contiguously from its own next `seq`: a restarted backend starts again
  at 0, and one that kept the session knows only the frames that reached it. Every
  `hello.ack` restarts the numbering at 0, and a server `ack` whose `audio_seq` is at or past
  the next number moves it to `audio_seq + 1` -- the resume `ack` a backend that kept the
  session sends right after `hello.ack` (a frame sent before that ack arrived is dropped by
  the backend as a duplicate and answered with the same ack). In a running connection an
  `ack` never reaches the next number. The audio of the outage itself is lost; the microphone
  keeps running. A hidden tab (#425) says `pause` again on the new connection in either mode.
  A reconnect may answer with the other `stt_mode` (a backend restarted with another config,
  #447): from that `hello.ack` on the socket sends only what the new mode takes (no audio to a
  client-mode backend; no `transcript.client.*`, kept finals included, to a server-mode one,
  which would close with 1008), and the screen, on `reconnected`, stops the running transcriber
  and starts the new mode's one (the camera goes on; a hidden tab starts it when it comes back)
  and updates the mode in "Estado de la conexión".
  The screen meanwhile keeps the camera, the recognizer (or audio stream) and the wake lock running and the controls
  enabled, shows «Reconectando…» in "Estado de la conexión" and, after a reconnect,
  «Conexión recuperada» for `RECOVERED_MS` (5 s); only after `LONG_OUTAGE_MS` (2 min) of outage
  does it show the blocking "Se ha perdido la conexión con el servidor…" (still retrying; a
  later reconnect clears it). A burst whose upload was unreachable, a 5xx, or a 409 while the
  connection is down (a restarted backend before the resume) is «Pendiente de subir» and is
  sent again, with the same `capture_id`, after the reconnect (or after `UPLOAD_RETRY_MS`, 10 s,
  if the connection stayed up); a burst listed in the resume's `received_capture_ids` becomes
  «Guardada» without a new upload; any other 4xx stays «Error». A `beforeunload` warning asks
  before leaving only while a burst is uploading or pending or the socket's queue is not empty.
  Inside the study workspace (`CapturePage` with `preset`, `CaptureScreen` `embedded`) a
  session ended elsewhere says to go on in Construir or Estudiar instead of the list of sessions.
- **Embedded capture in the workspace's Captura tab** (#413): `CaptureScreen` `embedded` is a
  `section` ("Captura en curso") with an `h2`, not a `main` with an `h1` (the workspace owns
  both), and shows no "Dudas pendientes" line (since #450 the workspace has no counter either; the doubts are
  asked in the chat). Since #470 (human feedback 2026-09-28) it is only the camera preview and
  a bottom toolbar (`.capture-toolbar`, same place and look as the Recursos toolbar) with one
  icon button, a camera, `aria-label`/tooltip «Capturar página». Gone from the embedded screen:
  the visible title and subject · topic line, the connection pill while connected, the «Cámara»
  and «Controles» headings, the camera status sentence, **Importante**, **Libro** / **Apuntes**,
  **Terminar**, the live transcript and the photo strip. **Importante** and the source switch stay
  reachable by voice (`stt/commands.yaml`); without **Terminar** the session ends by the backend's
  idle auto-end once the workspace is left (#425), or on `/capture`. The `h2` and the connection
  and camera status lines stay for screen readers only (`.capture-sr-only`). What is wrong still
  shows, compactly, over the top of the preview (`.capture-notices`, each at most three lines,
  the whole sentence its tooltip; a server problem one line each): the connection when it is not
  plainly open («Conectando…», «Reconectando…», «Conexión recuperada», lost), a blocking message,
  a failure, the pause of a hidden tab, the STT warning, a lost camera with **Reactivar cámara**,
  the server health lines, and «N fotos pendientes de subir» / «N fotos no se han podido subir».
  The screen reports `onRecordingChange(live && !paused)` to its host. The whole topic is
  prepared by asking the chat («prepárame el tema»), never by a button. The standalone
  `/capture` keeps everything above (its `main`, `h1`, doubts line, every button, transcript and
  photos), and has no «Terminar y preparar apuntes» either (see above); the Android app keeps
  its own. The former `NotesProgress` view and `fetchNotesGeneration` are gone.
- **Voice tutor** (#82, `src/tutor/`): once a topic is chosen, the capture page's picker also
  offers "Preguntar al tutor" (section "Estudiar con el tutor"; `SessionPicker`'s optional
  `onTutor(TutorTopic {subjectId, topicId, subjectName, topicName})`), and `CapturePage` shows
  `TutorScreen` for that topic instead -- no session is opened; "← Volver" returns to the picker.
  The student asks by voice ("Preguntar por voz": one Web Speech recognition, `es-ES`, not
  continuous, its interim text shown as "Lo que te oigo"; pressing again stops it; the final text
  is sent at once) or types ("Escribe tu pregunta", Enter or "Preguntar", up to 1000 characters).
  The answer streams ("El tutor está pensando…" until the first delta), each `[^label]` shown as
  `[label]`, then "Fuentes:" lists its refs (`[label] text`); a "Ver los apuntes del tema" link
  leads to the notes page and its sources panel. With "Leer las respuestas en voz alta" (on by
  default where `speechSynthesis` exists) the answer is read aloud in Spanish without the marks,
  and "Parar de leer" stops it. The topic's earlier questions are shown first. A missing
  recognition API, a denied microphone, silence, a network or other recognition failure each get
  their own Spanish sentence (`VOICE_PROBLEMS`) and typing still works; a browser without
  synthesis says the answers are only written. A reached cost cap offers "Continuar
  igualmente" (the same question with `confirm_over_cap`).
  - `api.ts`: `fetchTutorHistory(s, t) -> ReadResult<TutorTurn[]>` (`GET .../tutor`),
    `askTutor(s, t, question, {confirmOverCap, style, onDelta}) -> StreamOutcome<TutorAnswer>`
    (`POST .../tutor`, read with the editor chat's `streamTurn`; `style` `"spoken"` |
    `"written"` is sent only when given, the voice tutor gives none), `describeTutorFailure`,
    `readTutorAnswer`, `readTutorHistory`, `readSections`, `tutorPath`. `TutorAnswer` (and each
    `TutorTurn`) carries `style` (`spoken` for turns recorded before #334) and `sections`
    (`SectionRef {anchor, title}`, written answers only).
  - `voiceQuestion.ts`: `listenForQuestion(callbacks)` (a `VoiceQuestionStarter`:
    `onInterim`, `onFinal`, `onProblem(VoiceProblemCode)`, `onEnd`; returns `{stop()}`),
    `voiceQuestionSupported()`; the recognition constructor comes from
    `capture/webSpeechTranscriber.ts`.
  - `VoiceInputButton.tsx` (#428): the chats' microphone button on top of `listenForQuestion`
    (props `value`, `onChange`, `onFinal`, `disabled`, `icon`, and the seams `listen` /
    `voiceSupported`), used by the Estudiar chat and the idle Construir chat. Idle it reads
    **Hablar** (`aria-label` "Dictar el mensaje por voz"); listening "Escuchando… (pulsa para
    parar)" with `aria-pressed="true"`. With `icon` (#458, the workspace chat) it shows a
    microphone icon instead of the text, with the same accessible names and a `title` ("Hablar:
    dictar el mensaje por voz", "Escuchando… (pulsa para parar)" while listening). A press while
    listening calls `stop()` (what was said so far is kept). The interim
    text goes into the chat's input; the final text is handed to `onFinal`, which each chat sends
    through its typed path (no voice mark). No result puts back the text from before and shows one
    line of `CHAT_VOICE_PROBLEMS` (the tutor's `VOICE_PROBLEMS` reworded for a message, e.g. "No te
    he oído. Pulsa «Hablar» y habla."), cleared on the next press or when the student types. A
    browser without recognition gets a disabled button with `title` and a visible hint "Este
    navegador no reconoce la voz: escribe tu mensaje." (its `aria-describedby`). Unmounting, or
    `disabled` turning true, stops a running recognition; what arrives after unmounting is dropped.
    Styles in `voiceInput.css`.
  - `speech.ts`: `SpeechOutput {supported, speak(text, onEnd?), cancel()}`,
    `browserSpeechOutput()` (`speechSynthesis`, `es-ES`, a Spanish voice when the browser lists
    one), `spokenText(reply)` and `shownText(reply)`.
  - `TutorScreen({subjectId, topicId, subjectName, topicName, onClose?, speech?, listen?,
    voiceSupported?})`: the last three are the seams tests use.
- **Study workspace** (`/subjects/<s>/topics/<t>/workspace`, "Espacio de estudio", `src/workspace/`,
  #312, epic #311): one screen for a topic, opened from the study desk (the row's **Construir**, "Sesión
  abierta", a created topic; #368) and from the topic page's **Construir**. The page uses the whole
  screen width (16 px side gutter, #450). The header is one compact bar (#450): on the left the
  switch **Construir · Estudiar** (`ModeSwitch`) immediately followed by the topic's name (the
  page's `h1`, a link to the topic page); right-aligned (`.workspace-bar-end`, where the settings
  and the profile will go) a **Versiones · vN** link to `.../versions` (`VersionsPage`), shown
  once the notes are read, whose accessible name carries the current `vN`, and **Mesa de
  estudio** (`/`). There is no doubts counter and no spend line since #450 (the doubts are asked
  in the chat; `WorkspaceCost` is gone). The page's root is its `main` (#413), so the embedded
  capture has none of its own. The split follows with a minimal gap. Two columns (a CSS grid):
  - **Shared frame (#487).** The header band, the desk, the columns and the cards are
    `WorkspaceFrame({mode, subjectId, topicId, topicName, label, className?, barEnd?, views, view,
    onView, leftLabel, leftClassName?, left, chat, documentClassName?, documentPanel?, document,
    detail?})` (`src/workspace/WorkspaceFrame.tsx`), used by Construir and by the study screen
    (Estudiar) alike: a `main.workspace` (`data-mode`, `data-view`, `data-detail`) with the band
    (`ModeSwitch` marking `mode`, the topic's `h1` linking to the topic page, `barEnd` and **Mesa de
    estudio**), the one-column switch (`views`, always in the order document | left | chat; its
    type `NarrowView`), the left column with the left card (`.workspace-sources`, named
    `leftLabel`) above the chat card (`.workspace-chat`, "Chat"), the document card
    (`.workspace-document`, "Documento") and `detail` in the document's cell (#473). Its styles are
    `workspace.css`, so every rule of the frame below (the fluid split, the one-viewport layout,
    the cards and tokens of #485, the one-column views) applies to both modes; neither mode copies
    them. The chat input is `workspace/chat/ChatComposer.tsx` (#487), shared by the workspace chat
    and the study chat: the textarea (Enter sends, Shift+Enter is a new line), then one row with
    the send button (`submitLabel`, **Enviar** by default), the microphone (`VoiceInputButton` with
    `icon`, the Web Speech path of #428; `voice: null` hides it) and the chat's own icon buttons
    (`actions`); `before` is what goes above the textarea (the Recursos chips).
  - **Visual zones (#485).** The page is a desk (`--desk`: a shade darker than `--paper` in the
    light theme, darker than the slate in the dark one) under the header bar, which is a solid band
    edge to edge (`--header-bg`, ballpoint blue in light, a deep ink blue in dark; its text
    `--on-header`, its links `--on-header-muted`, the mode switch's edge `--header-line`, its focus
    ring `--on-header`). Captura/Recursos, the chat and the document are three cards on the desk,
    with a gap between them: each has a `--line` border, `--radius-l` corners and `--card-shadow`,
    and its own surface -- Captura/Recursos `--paper`, the chat `--chat-surface` (a calm blue-grey
    tint in light, a bluish slate in dark), the document `--surface` (the white sheet / the slate
    board). All of them are tokens of `tokens.css` for both themes; `src/styles/tokens.test.ts`
    computes the WCAG contrast of every text colour on the new backgrounds (at least 4.5:1) and of
    the band's borders and focus rings (at least 3:1) in both themes, and
    `src/workspace/layout.test.ts` pins which token each zone uses.
  - Left, top: a tab list (`WorkspaceTabs`, `role="tablist"`, automatic activation, Left/Right
    with wrap-around, Home/End) with **Captura** and **Recursos**. Both panels stay mounted and
    the inactive one is only `hidden`. Since #450 showing **Recursos** pauses a running capture
    exactly like a hidden browser tab (#425): `CapturePage`/`CaptureScreen` get `suspended`, the
    camera and the recognizer (or the audio stream) stop, the still-open socket says `button:
    pause`, and the tab reads "Captura en pausa"; back on **Captura** the socket says `resume` and
    the camera and microphone start again ("Captura en curso"). Since #470 those states are an
    icon next to «Captura», not text: a red dot (`.workspace-rec-on`, pulsing; still with
    `prefers-reduced-motion`) while the capture records (`CapturePage`'s `onRecordingChange`), a
    grey one while it does not (paused on Recursos, or not recording); the dot's `role="img"`
    name ("en curso" / "en pausa") keeps the tab's accessible name. Since #454 (protocol 1.7) that
    pause says `reason: "student"` (`SessionSocket.sendPause`; a bare `pause` right after `hello`,
    repeated with the reason after `hello.ack`, and never a reason to a backend older than 1.7),
    so the backend's idle auto-end (#425, `[server] capture_idle_end_seconds`, 5 min by default)
    never ends the session while the page stays open on Recursos; a hidden browser tab says
    `reason: "hidden"` (it wins over Recursos; each change between the two is a new `pause`) and
    the auto-end applies to it as before. While paused the chat is told no capture runs, so it offers
    its own microphone button. **Captura** is `CapturePage` with `preset` = the URL's subject and topic.
    **Recursos** (`ResourcesTab`) lists the topic's sources grouped by kind ("Páginas de apuntes",
    "Páginas del libro", "PDF", "Webs", "Imágenes", "Fragmentos de la transcripción"), built by
    `resourceList(sources, tree)` (`resources.ts`) from the topic's source list
    (`fetchTopicSources`, `GET .../sources`, #323, read again each time the tab is shown) and the
    notes' provenance footnotes: every stored `notes`/`book`/`pdf`/`web`/`images` source by its
    real file, in the backend's order, uncited webs included, then the cited sources the list
    lacks (PDF pages, transcript spans). A cited stored source the list lacks is marked
    `unlisted` and shown only once its metadata was read and does not say `removed` (#456): a
    source the student retired (#451) never comes back through a footnote, while the footnote
    itself keeps opening it in the viewer. When that request does not answer a list (a backend older
    than #323, any failure) it falls back to the summary counts (`GET .../summary`; a failure of
    that read is the tab's error): handwritten/book pages and PDFs are numbered `page-001…` up to
    the count, webs (named after their title) are openable only when the notes cite them (the
    rest are not shown; #461 dropped the "Hay N webs…" line), and images pasted
    into the notes (`sources/images/img-NNN.<ext>`, #316) are listed when the notes cite them. Each stored
    source is one card (thumbnail -- the flattened `page-NNN.page.jpg`, else the capture; a PDF's
    first page image; the pasted image -- and below it one muted, ellipsized line with its name,
    "Página 3 · apuntes", "Libro, página 83", "PDF «nombre»", "Web: …", "Imagen pegada 1", or
    "Imagen recortada 2" for a region the editor cropped from a page (sidecar `origin: cropped`,
    #493; pasted and cropped images share the group «Imágenes»); no
    state chip since #461), its state derived by `resourceStates` (`resources/state.ts`, #328):
    - pending: kept and not cited by the current notes -- no mark at all;
    - incorporated ("added", #461): kept and linked by a provenance footnote definition of the
      current notes (`citedSources`, via `parseProvenance`), so it follows `notes.changed`
      without a request. It shows a small round badge with a check in the thumbnail's
      bottom-right corner (`role="img"`, `aria-label` and `title` «Incorporada a los apuntes»,
      `INCORPORATED_LABEL`);
    - **Apartada**: the sidecar's `triage.status` is `set_aside` (#324), read from
      `GET /api/sources/{id}/meta`, with its reasons in Spanish (`blank` "En blanco",
      `duplicate` "Repetida de la página N" from `duplicate_of`, `blurry` "Borrosa", `partial`
      "Puede estar cortada", `same_content` "Mismo contenido que la página N") and "(la
      apartaste tú)" when `decided_by` is `student`.
    A `flagged` capture is pending (or incorporated) plus a small «!» badge in the thumbnail's
    bottom-left corner (`aria-label`/`title` "Aviso: <reason>"); a source
    without a `triage` block (stored before #324, or not a capture), or whose metadata cannot be
    read, is kept. Kept sources come first in source order, then a collapsed "Apartadas (N)"
    group (a disclosure button with `aria-expanded`), a set-aside card's reason as a muted line
    under its name. No counts line and no hint paragraph since #461 (both read as a log). The
    only per-card action is **delete** (#450): a trash
    button over the thumbnail's top-right corner («Borrar <title>», `aria-haspopup="dialog"`) asks in
    the app's confirmation modal (#486, `useConfirm`, below; not inside the card since #486, never
    `window.confirm`): «¿Borrar esta fuente?» (message: "«<title>» dejará de aparecer entre las fuentes del
    tema. Lo que los apuntes ya citan de ella se sigue pudiendo consultar."), **Borrar** (trash icon,
    destructive) / **Cancelar** (focused); Escape, **Cancelar** or a click on the backdrop cancel and
    the focus goes back to the trash button. **Borrar** calls
    `deleteSource(vaultId)` while the modal stays open, its button «Borrando…» and nothing able to
    close it (`resources.ts`, `DELETE /api/sources/{vault_id}`, the same
    `sourceUrl` path the reads use); on a 204 the card leaves at once (the tab keeps the retired
    `vault_id`s for its lifetime, so neither a stale cached meta nor a footnote brings it back)
    and the list and metadata are read again. The backend retires the source (#451, a soft delete: its files stay and a
    citation of it keeps resolving, but it leaves the topic's source list) and answers 204. A
    405/501 (an older backend) says «No se pudo borrar: Este servidor todavía no permite borrar
    fuentes.», any other refusal its `detail`, in the modal with only **Cerrar** (focused). `decodeSourceMeta` accepts the
    meta's optional `removed` boolean (#451; before #461 the strict decoder refused the whole
    meta once the backend sent it). Cited transcript spans follow, as plain buttons under
    "Fragmentos de la transcripción" (no count badge since #461). The metadata is read by `useSourceMetas`
    (`resources/useSourceMetas.ts`) at most `META_CONCURRENCY` (4) at a time and cached per
    source (every listed or cited stored source, the `unlisted` ones too); every one is read again each time the tab is shown and after each change
    of the notes (a new tree), which is how a `capture.triaged` change shows up. Choosing a
    source (set-aside ones too) hands it to the page (`onOpen(resource, trigger)`, the card's
    button as the focus-return target), which since #473 shows its detail **over the document
    column** (see the right column below); the Recursos list stays as it was in its box (before
    #473 the `SourcePanel` replaced the list inside the box, where the capture was tiny).
    **Controls over a thumbnail (#473).** The checkbox, the trash button and the corner badges are
    "glass" so the page shows through them: a thin dark veil (`--glass-veil`, 28 % black) with a
    light ring (`--glass-ring`, 75 % white), a light glyph and a 2 px `backdrop-filter` blur, all
    defined on `.resource-card` in `workspace/workspace.css`. The dark veil keeps the ring and glyph
    readable on a white page, the light ring keeps the control's edge visible on a dark photo.
    Hover and keyboard focus make the veil denser (`--glass-veil-strong`), focus adds the accent
    focus ring, a selected checkbox is solid accent with its check, and the trash button's hover or
    focus fills it with the danger red (`--glass-danger`). The «Incorporada a los apuntes» and «!»
    badges keep their colour but only partly opaque (`--glass-ok`, `--glass-warn`, 60 %): fixed
    tints, not the theme tokens, since they sit on the image and the dark theme's pale `--ok` and
    `--warn` would wash out the white glyph. `workspace/layout.test.ts` pins these rules (jsdom
    paints nothing).
    **Thumbnails and the selection (#432, #450).** The grids are `minmax(min(8rem, 100%), 1fr)`
    columns (two at most below 36rem, no horizontal scroll) and each thumbnail fills its column's
    width at the image's natural aspect ratio (no empty band above or below, never cropped).
    Each stored-source card (kept and set-aside; transcript spans are not selectable) has a
    checkbox over the thumbnail's top-left corner, apart from the button that opens it, labelled «Seleccionar <title>»: a
    click or Space toggles it, Shift-click sets the range from the last toggled card in the
    visible order (kept, then the set-aside ones when shown) to the state the clicked one takes.
    A selected card shows a checkmark and an outline. While N > 0 a bar above the grid reads «N
    seleccionadas» with **Quitar selección**. The selection is workspace state
    (`resources/selection.ts`: `useSourceSelection()` in `WorkspacePage`, shared through
    `SourceSelectionContext`, read with `useSelection()`; null outside a workspace page, where the
    tab shows no checkboxes), so it survives switching tabs and opening/closing a source; it
    holds `{id, title}` in the order selected, `id` the topic-relative source id the chat events
    use (`sources/notes/page-003.jpg`, `topicSourceId(ref)`; a PDF is one source) and `title` the
    chip's short title (`chipTitle`: «Pág. 3», «Libro p. 12», the PDF's name, the web's title,
    «Imagen 1»). After each read of the list, ids no longer listed are dropped (`prune`; the
    pure helpers `toggled`, `ranged`, `pruned` are tested on their own).
    **Adding sources (#461, replacing #384's «Añadir fuente» panel).** Under the scrolling list,
    a compact icon toolbar (`resources/AddSourceToolbar.tsx`, `role="group"` «Añadir fuentes»;
    `position: sticky; bottom: 0` inside the tab panel, so it stays at the bottom of the
    Captura/Recursos box while the list scrolls) has three icon buttons with Spanish
    `aria-label`/`title`: **Añadir una página web** («Añadir una página web (URL)») opens a small
    popover above the toolbar with the address («Dirección de la página», focused) and **Añadir**
    (`addWebPage`, `POST .../web-pages`); **Subir archivos PDF** opens the file picker (several
    PDFs at once, `multiple`), each uploaded whole in turn (`uploadPdf`, `POST .../sources/pdf`;
    the page range stays on the topic page's `PdfUploadForm`), with «Subiendo N de M…» while it
    runs; **Libro de texto** («Título del libro de texto») shows the topic page's
    `BookTitleForm` in the popover (its heading visually hidden). A success closes the popover and
    re-reads the source list and the metadata (as a new `refreshKey` does), with no status line
    or log; a refusal stays in the popover as a Spanish `role="alert"` (an invalid address, the
    backend's `detail`, «No se ha añadido «x.pdf»: …» per refused PDF with **Cerrar**, «Esa página
    ya era una fuente del tema: «…».»). Escape, or the same button again, closes the popover and
    gives the focus back to its button. With no source at all the list says «Este tema todavía no
    tiene fuentes: captura páginas en la pestaña Captura o añádelas abajo.».
  - Left, bottom: the chat slot `WorkspaceChatSlot`, the live chat panel (`src/workspace/chat/`,
    #317; the notes page keeps `EditorChat`). A section named "Chat con el asistente" (its
    `aria-label`; no visible heading since #458, to save height): a `role="log"` list
    of turns, oldest first (not itself a live region since #412, see below). A spoken request shows "Por voz · HH:MM" and
    "Pediste: <request_summary>" (the short line of what was asked; `summary` stays the applied
    change's) with a **…** button (`aria-expanded`, `aria-controls`; "Ver lo que dijiste" /
    "Ocultar lo que dijiste") that shows the raw `transcript.text` and its session time range
    (`00:02:34–00:03:10`) and hides it again; a typed turn shows "Escribiste" and the message.
    Then "Asistente" and the reply: "En cola…" for a `request.detected` whose turn has not
    started (several keep their order), «Respondiendo…» once it started (with a small spinner while the entry is on its way and has no
    text yet, #452), the text
    streamed from `reply.delta` (dropped on `reply.restart`; deltas of an older attempt ignored),
    replaced by the authoritative `turn.result`; "Cambio aplicado: <summary>" with **Ver los
    cambios** (`aria-expanded`, shows the existing `DiffView`) for a change seen live, or **Ver las
    versiones** (the topic's versions page) for one read from the history, which has no diffs; a
    `turn.error` shows "No se pudo completar: <detail>" (`role="alert"`), and a
    `cost_cap_reached` one **Continuar igualmente**, which confirms that request past the cap
    through `POST .../workspace/messages` `{confirm_over_cap: true, turn_id}` (#351): the same
    request as it was classified, whatever its kind (`edit`, `question`, "prepárame el tema",
    `incorporate`, `doubt_answer`; never a batch of a run), goes back to "En cola…" and its new
    turn (same `request_id`) streams into the same entry; a refused confirmation (404: it no
    longer waits, e.g. after a restart) shows its detail. Below, a textarea labelled
    "Mensaje para el asistente" (the label visually hidden in the one-viewport layout since #458;
    placeholder "Chatea con el asistente…") (Enter sends, Shift+Enter is a new line; above it, since #432,
    one chip per source selected in **Recursos** -- its short title and × «Quitar <title> de la
    selección», the first five and «+N» past six, and **Quitar selección**; shown in the narrow
    layout's Chat view too) and, in one row under it, **Enviar**
    (disabled while empty or while this page's post or undo runs), the microphone icon button
    (see below) and an undo icon button (`aria-label` and `title` "Deshacer el último cambio",
    #458; `POST .../notes/chat/undo`, as the notes page's chat). While the stream is down:
    "Sin conexión en directo con el asistente; reintentando…" (`role="status"`).
    Since #329 the panel is where the student drives the work (epic #311), with no buttons to
    incorporate or answer:
    - **Typed messages** go to the request classifier like spoken ones: `POST
      .../workspace/messages` `{text}` (#327), or `{text, selected_source_ids: [...]}` when the
      Recursos selection is not empty (#432; topic-relative ids in selection order; the backend
      resolves «esto», «estas páginas» against them, #433, and ignored the field before it). The
      selection is kept after a send, for a follow-up on the same pages. The message shows in the history at once as
      "Escribiste" with «Respondiendo…» and the spinner (the input is emptied only once it is
      there, #452) until the 202; its `requests` (`req-t<n>`) then take its place as queued entries ("En cola…"; a second
      request of the same message shows "Y además: <summary>") until their turns stream in (a
      typed `request.detected` of the stream for one already shown adds nothing). A refusal shows
      "No se pudo completar: <detail>" (`role="alert"`) under the message. The panel never posts
      to `POST .../notes/chat` (#351).
    - **Incorporations** (`kind: incorporate`): "Incorporadas: página 3, página 4" (each source
      a button that opens it in **Recursos**, like provenance clicks), the reply, "Cambio
      aplicado" with its diff (the history's incorporation turns carry their diff), and "Ha
      surgido 1 duda…" / "Han surgido N dudas…" when the result raised doubts.
    - **A whole-topic run** ("prepárame el tema", batched, #326) is one entry: its
      `incorporation.progress` as "3 de 8 páginas" (with a `<progress>`), and each batch (an
      `incorporate` turn without request) listed below it ("Tandas de la preparación"). A run
      started elsewhere (the end of a session) gets an entry "Preparación del tema" of its own,
      finished when its progress is complete or the generation's `notes.changed` arrives.
    - **Triage** (`set_aside` / `restore` turns, `triage` in the history): the request, then
      short lines. A set-aside whose `targets` carry triage reasons (#351) shows one line per
      target with the Recursos tab's reason text (`resources/state.ts` `reasonText`): «Página 9
      apartada: en blanco», «Página 4 apartada», «Página 1 ya estaba apartada: repetida de la
      página 2» (the page a button that opens it in Recursos); otherwise (a restore, no reasons,
      an older turn) the backend's reply («He apartado la página 9.»).
    - **Doubts** (`doubt.asked` / `doubt.resolved`, #325; `doubt` turns of the history): a
      highlighted entry "Asistente · Duda" with the question, the suggestions as a numbered list
      (`<ol>` "Sugerencias"), for a contradiction each option with its source ("Página 3:
      «1760»", the source opening in Recursos) and the other sources it is about ("Sobre: …").
      No answer buttons and no "Descartar" link: the student types or says the answer ("la 2",
      "pone «escrita»", "descártala"), which becomes a `doubt_answer` turn (its reply is the
      resolution); `doubt.resolved` marks the doubt "Duda resuelta" / "Duda descartada" with
      "Respondida: <resolution>" (and "Respondiste: «…»" when the history knows the answer).
      While one is asked, the input's placeholder asks to answer it (see below).
      `doubts.auto_resolved` (`doubts_resolved` in the history) is one short line.
    - **"Ya está, quiero estudiar"** (a `study` request, #335, #337): "Pasando a Estudiar…"
      while it runs, then the backend's one line («He cerrado la captura y marcado los apuntes v5
      como versión de estudio.») and one link styled as a button, **Ir a Estudiar**, to the
      turn's `action.path` (the study screen). Only a `turn.result` of kind `study` whose
      `action` is `{kind: "go_study", path}` with an in-app path (`/...`) gets it; no other turn
      shows any button of its own. These turns are not in the history, so the button does not
      survive a reload (human decision). `study.marked` needs nothing in the chat.
    - The header's pending-doubts counter is read again on every `doubt.asked`,
      `doubt.resolved` and `doubts.auto_resolved` (`WorkspaceState.doubtsChanged`).
    - `chat/api.ts`: `fetchWorkspaceHistory` (`GET .../notes/chat` read with `turn_id`,
      `origin`, `request_summary`, `transcript` and, since #329, the `incorporate`, `triage`,
      `doubt` and `doubts_resolved` fields), `readWorkspaceEvent(event, data)` (the stream's
      events decoded, `doubt.asked`, `doubt.resolved`, `doubts.auto_resolved` and
      `incorporation.progress` included; unknown ones are `null`, the list is open),
      `readOutcome` (a `RevisionResult`, a `prepare_notes` `GenerationResult`, an
      `IncorporationResult`, a `TriageTurn`, a `doubt_answer` `ResolutionResult` or a `study`
      `StudyTurn`, as one `TurnOutcome`; a triage one with its `targets`, a study one with its
      `action`), `postMessage(s, t, text, selectedSourceIds = [])` (`POST
      .../workspace/messages`, the typed messages) and `confirmOverCap(s, t, turnId)` (the same
      route with `{confirm_over_cap: true, turn_id}`, for "Continuar igualmente", #351).
    - `chat/sources.ts`: `sourceName(id)` («página 3», «página 83 del libro», else the Recursos
      title); the item that opens a source in Recursos is `resources.ts`'s `sourceItem(id)`
      (`OpenSource` takes that item's `definition` as its optional third argument).
    - `chat/stream.ts`: `connectWorkspaceStream(s, t, {onEvent, onOpen(reconnected), onDown,
      retryDelays?}) -> close()`: `GET .../workspace/stream` read with `fetch` + `readSse`; a
      failed attempt or a drop is retried after 1 s, 2 s, 5 s, 10 s, then every 30 s
      (`RETRY_DELAYS_MS`, reset by each successful open).
    - `chat/turns.ts`: `reduceChat`, the pure merge of the history, the stream's events and this
      page's sends. Turns are keyed by `turn_id`; a queued request by `request_id` until its
      `turn.started`; a doubt by `doubt:<pending_id>`, an auto-resolution line by
      `auto:<pending_ids>`; a batch carries its run's key as `parent`. A typed message is a
      sending entry until the POST answers (its first request keeps that entry's key; a typed
      `request.detected` with the same text binds it in place first). Request ids are only
      matched among the entries still on their way on this page (`isOpen`: typed ids restart per
      session, #452), so an older finished `req-t1` never swallows a new message; a POST that
      queued no request keeps the message, failed in Spanish. While an entry waits (queued or
      running) and the stream says nothing for `QUIET_MS` (20 s, doubling up to 60 s), the
      history is read again, so an answer the stream missed still shows.
      "Continuar igualmente" (`confirm.start`) puts the stopped entry back in the queue without
      its turn, so the request's new `turn.started` (same `request_id`) takes it again; it is
      offered (`canRetry`) for an entry with a `turn_id` and a `request_id`. A history read replaces the entries it has (by `turn_id` or those keys,
      keeping a live entry's key -- so a doubt is announced once -- and its diff) and keeps, after
      it, the live ones it does not have yet; a `doubt_answer` entry whose doubt the history shows
      answered is dropped, and so is a queued typed request whose typed turn (same words, not
      older than a minute before it) the history already has, one for one.
    - `chat/useWorkspaceChat.ts`: the hook. Every (re)open of the stream re-reads the history;
      a reopen also reloads the notes, so a turn or a change that happened while disconnected is
      neither missed nor duplicated. Every `notes.changed` calls the workspace state's
      `reloadNotes` (with the sections the turn changed when its `turn.result` was seen, else
      none), and so does a typed turn's own result that changed the notes.
    - The request's rendering is one switch on the entry's origin (`Request` in `ChatPanel.tsx`:
      voice, typed, or `system` for what the assistant does on its own), the reply's on its kind
      (`Reply`, `DoubtEntry`).
    - **App feedback chip** (#472): a turn whose result or history turn carries `feedback`
      `{id, kind, title}` (the editor recorded a bug or an improvement of the app in the vault's
      feedback inbox instead of editing, `docs/modules/editor.md`) shows, under its reply, a small
      chip «Bug apuntado» / «Mejora apuntada» (`src/chat/FeedbackChip.tsx`, the title as its
      tooltip and as visually hidden text). `workspace/chat/api.ts` and `turns.ts` read it
      (`readFeedback`) from the live `turn.result` and from `GET .../notes/chat`, the history's
      value winning, so the chip survives a reload. Triage is not in the web: the CLI does it.
    - **Crop line** (#493): a finished turn whose `crop` names a new `source_id` (the editor cropped
      a page region into `sources/images/`, `docs/modules/editor.md`) shows «Recorte añadido:
      Imagen recortada N», a button that opens it in **Recursos** («Ver la fuente: imagen
      recortada N»; `readCropId`, `croppedImageName`), live and from the history, hidden once the
      turn is undone. A failed crop (`crop.error`) shows only its reply.
    - **Input on screen, log following the newest turn (#412, #450).** On a wide screen at least
      30rem tall (`workspace.css`, `min-width: 56.3125rem and min-height: 30rem`) the page is
      exactly one viewport high and never scrolls: the header on top, each column scrolling inside
      its share. The columns' proportions are fluid (#450): the left column is 50 % at 900 px and
      shrinks linearly to 25 % at 1200 px and wider (`clamp(max(18rem, 25%), calc(50% - (100vw -
      56.25rem) * 0.97), 50%)`), the document takes the rest. On the left the tabs' list stays put
      and only the Recursos panel scrolls (the list is also `position: sticky` outside this
      layout). The Captura panel (`.workspace-tabpanel-capture`, a `WorkspaceTab` `className`)
      never scrolls since #470: the running capture fills it, the preview's box
      (`.capture-stage`) takes the free height and the video scales into it (`object-fit:
      contain`), the toolbar keeps its height below. Outside this layout the box is 4:3, at most
      60vh. The
      Captura/Recursos box takes a fixed share of the left column (#458): 50 % at 720 px of
      viewport height, a third of every extra pixel of height taken off, down to 38 % from about
      1080 px (`clamp(38%, calc(50% - (100dvh - 45rem) / 3), 50%)`, never under 8rem). The chat
      takes the rest (`flex: 1 1 0; min-height: 0`, never `min-content`, which let a long reply
      push the form off the screen, #463): its form keeps its height (`flex: none`), so the input
      and **Enviar** are always visible, and only the log shrinks and scrolls; the input can be
      dragged taller only up to `min(12rem, 22dvh)`; `src/workspace/layout.test.ts` pins these
      rules (jsdom does no layout); the
      capture preview is capped at 32vh and the transcript at 20vh there, scoped to `.workspace`
      so `/capture` is unchanged. The log (`.ws-chat-log`) is its own scroll area (60vh at most
      outside that layout) and follows the newest turn through `src/chat/useFollowLog.ts`
      (`useFollowLog(content) -> {ref, onScroll, unseen, follow}`: on every change of `content` --
      a turn added, its status or its streamed reply grown -- it sets `scrollTop` to the end,
      unless the student scrolled more than 48px up; then **Nuevos mensajes ↓** shows until they
      come back to the end or press it; sending a message follows again). Only the latest turn is
      announced: the log is `aria-live="off"`, and a visually hidden `aria-live="polite"` region
      (`data-testid="ws-chat-latest"`, `latestLine(entry)`) says "Asistente: <status>" while a
      turn is sent, queued or running (a whole-topic run: its running batch) and "Asistente:
      <reply>" once it is done, never a streamed fragment, never a turn read from the history
      (only turns seen on their way on this page, and a doubt asked in the chat). The **…** of a
      spoken request has a `title` and an `aria-describedby` text ("Muestra la transcripción de lo
      que dijiste y cuándo lo dijiste"). `WorkspaceChatSlot` takes `capturing` (the page's
      Captura state) and passes it to `ChatPanel`: the placeholder is "Chatea con el asistente…"
      (#458) except while a doubt is asked: "Responde a la duda (escribiendo o de viva voz) o pide
      otra cosa…" while a capture runs, else "Responde a la duda (escribiendo o con el micrófono)
      o pide otra cosa…"; the empty-chat hint and a doubt's hint change the same way ("hablando o
      escribiendo" / "escribiendo o con el micrófono"). **Microphone (#428):** while no capture
      runs, `VoiceInputButton` with `icon` (a microphone icon, #458) sits in **Enviar**'s row; the spoken message goes through the
      same `send` as a typed one, and one arriving while the assistant is busy stays in the input,
      unsent. While a capture runs the button is absent (the capture's speech already reaches the
      chat); a capture starting removes it and stops its recognition, as unmounting the panel does.
  - Right: the document (`DocumentPanel`, #316), headed "Apuntes · vN · guardado" with an
    **Editar** button. The header stays pinned at the top of the card (#485): the card is a flex
    column that does not scroll, the header (`.workspace-document-header`, `flex: none`, sticky)
    on top and `.workspace-document-body` below it, which alone scrolls, so **Editar** is on screen
    at the bottom of a long document (the editor's column scrolls the same way). Below 900 px on
    a screen at least 30rem tall the document view is one viewport high in the same way; on a
    shorter one the card is no scroll container and the header sticks to the top of the page. The
    body: `NotesView` (no "¿Por qué?" here), with the sections the last applied turn
    changed highlighted and pasted images shown from the topic's sources. A provenance footnote (in
    the document or in a chat answer), a chat source link or a Recursos card opens that source's
    **detail over the document column** (#473): `SourcePanel`'s `overlay` variant in its own grid
    cell over the document, so `DocumentPanel` is neither remounted nor scrolled and the left
    column's tab does not change (a running capture is not paused by it). The detail has an X
    close button top right (`aria-label` «Cerrar», `title` «Cerrar (Esc)»), the capture large and,
    below it, its transcription; Escape anywhere closes it (unless something inside took the key)
    and the focus goes back to what opened it. A notes/book page with a transcription can be
    corrected there: «Editar la transcripción» turns it into a textarea («Corrige la
    transcripción») with **Guardar** / **Cancelar** (Escape in the textarea leaves the edit, a
    second Escape closes the detail); **Guardar** refuses a blank text («La transcripción no puede
    quedar vacía.») and calls `saveTranscription(vaultId, text)` (`notes/api.ts`, `PUT
    /api/sources/{vault_id}/transcription`, `server.md`); on success the stored text is shown with
    «Transcripción guardada.», on a refusal «No se pudo guardar: <detail>» (a 405, an older
    backend: «Este servidor todavía no permite corregir transcripciones.»; no network: the usual
    unreachable message). The `panel` variant (the study desk's notes page) has no edit button.
    Without notes (404) it says "Todavía no hay apuntes: pídeselos al asistente en el
    chat." and **Editar** starts a document from `# <topic name>` (saved with `base_revision:
    null`).
  - **Editar** replaces the document with the notes editor (`NoteEditor`, below), headed
    "Editando sobre vN" with **Cancelar** and **Guardar**. **Cancelar** leaves at once when nothing
    changed; with unsaved changes it asks in the confirmation modal (#486; `window.confirm` before):
    «¿Descartar los cambios que no has guardado?» (`DISCARD_CHANGES`), **Descartar** (destructive) /
    **Seguir editando** (focused, keeps the text). **Guardar**:
    `PUT .../notes {text, base_revision}` with the revision the edit started from; on success the
    workspace re-reads the notes (the saved sections highlighted) and the panel leaves edit mode.
    `409 notes_changed`: "Los apuntes han cambiado mientras editabas" with "Tu versión" and
    "Versión actual" side by side and **Descartar mis cambios** (leave edit mode, re-read) or
    **Reintentar sobre la versión actual** (the student's text stays in the editor, the base
    becomes the current revision; they review and save again -- nothing is overwritten silently).
    `409 notes_busy`: "Se están preparando los apuntes; espera a que terminen." `422`: the Spanish
    `errors` listed. While editing, a newer revision in the workspace state (the assistant changed
    the notes) shows the non-blocking "El asistente ha cambiado los apuntes. Tu texto sigue como lo
    dejaste; al guardar se comprobará." and never replaces the student's text.
  - Below 900 px (56.25rem) the columns become one and a switch **Documento** |
    **Captura/Recursos** | **Chat** (`aria-pressed` buttons, the root's `data-view`) shows one
    part; the others are `display: none`, never unmounted. Opening a source (#473) shows its
    detail in the document view's place (the switch moves to **Documento**), and closing it goes
    back to the view it was opened from.
  - `state.ts`: `useWorkspaceState(subjectId, topicId) -> WorkspaceState {subjectId, topicId,
    notes, changedSections, reloadNotes(changedSections?)}`, where `notes` is `{kind: "loading"} |
    {kind: "ready", text, revision, version} | {kind: "empty"} | {kind: "failed", message}` from
    `GET .../notes` (only the latest read is kept; `revision` is null while the backend does not
    send it, before #313), plus `doubtsKey` / `doubtsChanged()` (#329: the chat bumps it on a
    doubt event and the header's counter is read again); `WorkspaceContext` / `useWorkspace()`
    give it to the page's children.
    The chat panel calls `reloadNotes(sections)` on every `notes.changed` of the workspace stream,
    after an applied typed turn or an undo, and after the stream reconnects. #316 and #317 build
    on this module. `notes/api.ts`'s `TopicNotes` accepts the optional `revision` field.
- **Notes editor** (`src/noteEditor/`, #316): `NoteEditor({initialText, initialMode?, uploadImage,
  resolveImage?, renderPreview, onChange?, leading?, actions?})` with a ref handle `getText()`.
  A **Visual** | **Markdown** switch (the text carries over both ways). Visual is Milkdown
  (`visualEditor.ts`, loaded on demand as its own ~450 kB / 136 kB gzip chunk):
  `createVisualEditor(root, markdown, {onChange, resolveImage}) -> VisualEditor {getMarkdown,
  insertMarkdown, setBlock, toggleStrong, toggleEmphasis, bulletList, orderedList, rule,
  insertTable, addRow, addColumn, focus, view, destroy}`. Provenance references are atomic chips
  (`[^p3]` "p3", `[^ia]` "IA" highlighted, `[^est]` "tú" in green; moved or deleted whole, never
  typed into); a heading's `{#anchor}` is hidden by a decoration but stays in the text and the
  saved Markdown. Markdown is a `textarea` beside a live `NotesView` preview. The toolbar has block
  type (Párrafo / Título 2 / Título 3), Negrita, Cursiva, Lista, **Tabla** (visual: a 3x2 table,
  plus "+fila"/"+col"; Markdown: a table skeleton at the cursor) and **Imagen** (a file picker).
  Pasting or dropping a PNG/JPEG/WebP image anywhere in the editor uploads it
  (`POST .../sources/images`, `notes/api.ts` `uploadPastedImage`) and inserts the answered
  `markdown` at the cursor; meanwhile a "Subiendo imagen pegada…" card shows, and a failure shows
  "No se pudo subir la imagen: <detail>" and inserts nothing; other files are refused in Spanish.
  - **Source preservation** (`preserve.ts`): remark re-serialises every block in its own style, so
    the editor keeps each top-level block's source text (from the mdast offsets) and on save writes
    a block still equal to the loaded one (ProseMirror equality, heading ids aside) as that text,
    joined by the original separators; only changed or new blocks are serialised, in the notes'
    style (`-` bullets, `---` rules and table delimiters, unpadded tables, no escapes in `{#anchor}`
    or `[[?word]]`: `tidyBlock`). Milkdown 7.22 drops title-less images (a `null` title fails the
    schema); the editor's image schema fixes that.
  - **Spike** (#316; corpus `src/noteEditor/fixtures/*.md`: the example of `editor.md`, anchors
    with `_`/`-`, `[^p4]`/`[^ia]`/`[^est]` in paragraphs, list items and table cells, the final run
    of definitions of every source kind, nested lists, a table, a pasted image, `[[?soberanía]]`,
    `---`). Criterion: load + save with no edit gives the same text, and a one-word edit changes
    only its block. Out of the box, every candidate fails every case:

    | candidate (version, licence) | round trip | one-word edit | why |
    |---|---|---|---|
    | MDXEditor 4.3 (MIT, Lexical; largest) | 0/9 | -- | escapes `[^p2]` as `\[^p2]`, `*` bullets, padded tables, `\_` in anchors |
    | Milkdown 7.22 (MIT, ProseMirror + remark) | 0/9 | 0/9 | `*` bullets, `***` rules, padded tables, blank lines inside the footnote run, `\_` in anchors, `\[\[?`, drops title-less images |
    | TipTap 3.31 + `@tiptap/markdown` (MIT) | 0/9 | 0/9 | no footnotes (`\[^p2\]`), padded tables, `\_` in anchors |
    | Toast UI Editor 3.2 (MIT, unmaintained since 2022) | 0/9 | 0/9 | escapes `1\.` and `\{\#anchor\}`, `*` bullets, `\[^ia\]` definitions |

    Milkdown is the only one that parses GFM footnotes into nodes (so references can be chips) and
    tables; with the preservation layer, its stringify options and `tidyBlock` it passes 9/9 and
    9/9 (`roundTrip.test.ts`, plus an edited heading keeping `{#funciones_del_lenguaje}`), so it
    is the visual editor; the raw Markdown mode stays for anything it cannot show.
- **Study screen** (`/subjects/<s>/topics/<t>/study`, "Estudiar", `src/study/`, #333, epic #332):
  the second mode of a topic, beside the workspace (Construir). `ModeSwitch({subjectId, topicId,
  current: "build" | "study", navigate?})` is the header switch **Construir · Estudiar** (a `nav`
  "Modo del tema" with two links, the current one `aria-current="page"`), shown in the study
  header and mounted in the workspace header. **Construir** is a plain link to `.../workspace`
  (the notes stay editable there). **Estudiar**, from Construir (#337), posts `POST .../study`
  (#335, `study/api.ts` `switchToStudy`: the backend ends the topic's capture session -- a capture
  page open on the workspace sees its session closed, #319 -- and labels the current notes
  "versión de estudio"), showing "Pasando a Estudiar…" (`aria-disabled`, further clicks ignored)
  and opening `.../study` only on success (`navigate`, `window.location.assign` by default). `409
  notes_busy` says "Se están preparando los apuntes; espera a que terminen." and anything else
  (no notes, unreachable backend) the backend's Spanish `detail` or a Spanish fallback, below
  the switch (`role="alert"`); nothing navigates then and there is no confirmation dialog. From
  the study screen Estudiar posts nothing. Since #487 the screen is the workspace's frame
  (`WorkspaceFrame` with `mode="study"`, root `main.workspace.study` named "Estudiar", above): the
  same header band (the switch, the topic's name as the `h1`, **Mesa de estudio**), desk, cards,
  fluid split, one-viewport layout from 900 px on (the page never scrolls; the options' body, the
  chat's log, the document's body and the material's body scroll inside their cards) and single
  column below 900 px; `study.css` only styles what is inside the cards. The study version from
  `GET .../study` (`fetchStudyState`) is named under the heading "Material de estudio" of the left
  card: "Apuntes vN · versión de estudio" and, when `study_current` is false, "Has cambiado los
  apuntes después de la versión de estudio (vN)." (informative only); nothing when no version was
  marked yet. Two columns:
  - Left card ("Opciones de estudio", `.workspace-sources.study-card`; in two columns only its
    body `.study-card-body` scrolls), top: "Repasos para hoy" (`ReviewsToday`) -- this topic's entry of `GET
    /api/practice/summary` (`desk/practiceSummary.ts`): "N para repasar · M nuevas" with
    "Repasar ahora" (opens **Tarjetas de memoria**), else "Nada que repasar hoy." with
    "Próximo repaso: <fecha>." when there is one.
  - Left card, below: the study options (`options.ts`, `studyOptions(studyState)`) in the order
    **Esquema** (kind `esquema`), **Ejercicios** (the `exercises` of kind `examen`), **Examen**
    (its `questions`), **Quiz** (kind `quiz`), **Tarjetas de memoria** (the practice queue, kind
    `flashcards`), **Diapositivas** (kind `diapositivas`, #382, backend #380); each a button with `aria-expanded` and a text badge of its `options[].state`
    from `GET .../study` (#337; no longer computed from `GET .../generated`): `listo` "Listo",
    `desactualizado` "Desactualizado" (its `stale_reason` as `title`, else a generic reason) or
    `sin_generar` "Sin generar" (also an option the answer does not list).
  - Chat card (bottom of the left column, as the workspace chat; its input always on screen): the
    question chat (`chat/StudyChat.tsx`, #336), a region "Preguntas sobre el
    documento" (its heading visually hidden since #487, as the workspace chat has none) that answers questions about the document and **never edits the notes** (the only
    requests it sends are `GET` and `POST .../tutor`). Its `log` (a scroll area, `aria-live="off"`, #412) shows the
    topic's earlier **written** turns of `GET .../tutor` (the voice tutor's spoken ones are left
    out), then the fixed line "Solo respondo preguntas: no cambio los apuntes. Para cambiarlos, ve
    a Construir." (a link to `.../workspace`) and, since #487, the workspace chat's input
    (`ChatComposer`): a textarea ("Pregunta sobre el documento…", label "Tu pregunta", up to 1000
    characters, disabled while an answer comes; Enter or **Preguntar** asks, Shift+Enter is a new
    line) with, in the row below it, **Preguntar** and the microphone icon button («Dictar el
    mensaje por voz», tooltip «Hablar: dictar el mensaje por voz»; `VoiceInputButton` with `icon`,
    #428; `StudyPage` passes its `listen`/`voiceSupported` seams; disabled while an answer comes,
    which stops a running recognition):
    the interim text shows in the input and the final text, trimmed to 1000 characters, is asked
    exactly as a typed question; one arriving while an answer comes stays in the input. A question is `askTutor(...,
    {style: "written"})`: "Pensando…" until the first delta, then the reply streams; the answer is
    light Markdown rendered by `chat/ReplyView.tsx` through the notes' `parseNotes`/`parseInline`
    (no raw HTML). **Citation chips**, inline where cited: every `[§anchor]` is a button "§
    <section title>" (the title from the answer's `sections`, else the document's heading;
    accessible name "Ir a la sección <title>") that scrolls the document to that section and
    highlights it (`focusSections`); every `[^label]` a button with the label ("p3", "Ver la
    fuente p3") that opens its source in `SourcePanel` as a provenance footnote does and
    highlights every block citing that label (`NotesView`'s `focusLabel`), scrolling to the first
    citation. A chip for an anchor the document no longer has is disabled with the title "Esa
    sección ya no está en los apuntes". Errors: a reached cost cap shows its `detail` and
    "Continuar igualmente" (the same question with `confirm_over_cap`); another question of the
    topic running (409) "Espera a que termine la respuesta anterior."; no notes (409) "Todavía
    no hay apuntes: constrúyelos en Construir."; anything else the backend's Spanish `detail`.
    Since #412 the log is its own scroll area (`.ws-chat-log` since #487: it takes what the chat
    card leaves in two columns, 60vh at most elsewhere) and follows the newest turn as the
    answer streams (`useFollowLog`, as the workspace chat; asking a question follows again),
    unless the student scrolled up, then **Nuevos mensajes ↓**. A visually hidden
    `aria-live="polite"` region (`data-testid="study-chat-latest"`) says "Asistente: Pensando…"
    (or the `generation.started` text) while a question runs and "Asistente: <answer>" (without
    its `[§…]`/`[^…]` marks) once it ended; the history is never announced.
  - **Generation turns** (#367, backend #366): a request such as «hazme un quiz» is detected by
    the backend, which generates the material on the same `POST .../tutor` stream. The chat reads
    it with `chat/api.ts`'s `askStudyChat` (`style: "written"`; `chat/api.ts`'s `streamTurn`
    hands any other event to `onEvent`): `generation.started` `{kind, option, text}` replaces
    "Pensando…" with its `text` and a busy indicator (the turn stays `aria-busy`, the input
    disabled); a `result` with `kind: "generation"` `{option, material_kind, reply, items,
    warnings, study}` becomes a finished turn: `reply`, each warning below it, and one button
    **Abrir «<título de la opción>»** (titles from `STUDY_OPTIONS`, «Diapositivas» included since #382;
    none for an option the page does not know). A `result` without `kind` (or `kind: "answer"`) is an
    ordinary answer, as before. Errors are the tutor's (cost cap with "Continuar igualmente",
    which re-sends the same text confirmed; 409 busy; no notes; the backend's `detail`); a failed
    generation leaves no turn. History: `GET .../tutor` turns with `kind: "generation"` (read as
    `TutorTurn.generation` `{option, items}` in `tutor/api.ts`) show as finished generation
    turns with **Abrir**, whatever their `style`; older turns read unchanged.
  - **App feedback chip** (#472): a finished answer whose `result` or history turn carries
    `feedback` (`TutorTurn.feedback` in `tutor/api.ts`, `readFeedback`) shows the same chip as the
    workspace chat, «Bug apuntado» / «Mejora apuntada» (`src/chat/FeedbackChip.tsx`), live and
    after a reload.
  - The page (`StudyPage`) takes a generation's `result.study` as its `StudyState` (the badges
    turn "Listo" without another `GET .../study`; a read in course is dropped) and, if the open
    option shows that `material_kind`, remounts its content so it reads the new material.
    **Abrir** opens that option's panel as a click in the list does, reloading its content when
    it was already open.
  - Right: the document card (`.workspace-document.study-right`), `NotesView` read-only (no "¿Por
    qué?"): its header (`.workspace-document-header`, pinned like Construir's, #485) with
    "Apuntes", its version and one link "Editar en Construir" (`.../workspace`), and below it the
    body (`.workspace-document-body.study-document`, the part that scrolls) inside
    `.study-document-area`; without notes "Todavía no hay apuntes: constrúyelos en Construir.". A
    provenance footnote opens `SourcePanel` in the slide-over place.
  - `OptionPanel`: opening an option slides a labelled `region` (not a modal) over the right edge
    of the document's body, below the document's header and inside the document card
    (`data-panel="open"`), which reflows to its left and stays scrollable; the panel's header
    (title and "Cerrar") is pinned (`position: sticky`, out of its scrolling body) like the
    document's header; its heading takes the focus,
    "Cerrar" and Escape close it and give the focus back to the option button. Content
    (`OptionContent`) is the existing page with `embedded` (no crumbs, heading, stale note or quiz
    generate form): Esquema -> `MaterialPreviewPage` of `esquema.md`, Ejercicios ->
    `ExercisesView` (the exercises of `examen.yaml` one at a time, "Ver solución", "Anterior" /
    "Siguiente"; `exam/api.ts`'s `StoredExam` now carries `exercises`), Examen -> `ExamPage`,
    Quiz -> `QuizPage`, Tarjetas de memoria -> `PracticePage` (its "En los apuntes:" links as
    "§ <section title>" chips), Diapositivas -> `SlidesView` (#382): **Descargar PDF** and
    **Descargar PowerPoint** (`fileUrl` of `diapositivas.pdf` / `.pptx`; once `GET .../generated`
    answers, an export its `diapositivas` artifact does not list shows "PDF: no se pudo
    exportar." instead of a link) above `MaterialPreviewPage` of `diapositivas.md` (the deck's
    Marp source, as on the topic card page); the preview reports no anchors, so the slides
    highlight no section. There is **no "Generar" button** (human decision: everything the
    student asks goes through the chat): an option "Sin generar" says "Todavía no está
    generado." and what to ask the chat (e.g. «Pídelo en el chat: «hazme un quiz».»); a
    "Desactualizado" one shows its reason and the same hint above the material. The hint's
    phrase is a button that puts it in the chat's input (no send) and, in one column, shows the
    chat.
  - Highlight: `PracticePage`, `QuizPage`, `ExamPage` and `ExercisesView` take an optional
    `onFocusAnchors(anchors)`: the item shown (the current card or exercise; the quiz or exam
    question the focus is in; a chip's one anchor) reports its `anchors`, and the page passes the
    ones the notes have to `NotesView`'s `focusSections`, which marks every block of those
    sections (`notes-focus`, blue, apart from the green "changed") and scrolls the document (not
    the page) to the first. An anchor the notes lack is ignored.
  - Below 900 px the columns become one with the frame's switch **Documento** | **Estudiar** |
    **Chat** (the root's `data-view`: `document`, `left` -- the default -- or `chat`); opening an
    option shows the document with the panel over it full-width, closing it goes back to
    Estudiar.
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
- Study desk: subjects/topics, the entry into each topic's Construir (workspace) and Estudiar
  (study screen), creating subjects and topics; per-topic card (sources, sessions, minutes,
  pending, materials).
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
- Design system (#296): `src/styles/` is imported once by `src/main.tsx`, before the pages.
  `tokens.css` holds every colour, font, type step, spacing, radius and shadow as a CSS custom
  property: a light "notebook paper" theme (blue ink accent, highlighter yellow for AI-added
  content and **Importante**, red pen for errors) and a dark "blackboard" theme chosen by
  `prefers-color-scheme`. `fonts.css` self-hosts Atkinson Hyperlegible (interface) and Literata
  (notes, sources, footnotes, questions), both OFL (`styles/fonts/`); nothing loads from a CDN.
  `base.css` styles the plain elements inside `:where()` (so any class wins); `components.css`
  has the shared pieces: `.crumbs` (the header line of every page: "← Tema X" / "← Mesa de
  estudio" back link, and on topic sub-pages a "Mesa de estudio" home link on the right),
  `.page-context`, `.panel`, `.card`, `.badge`, `[role="alert"]` / `[role="note"]` messages and
  `.sheet` (the notes body: a sheet with a red margin line). Each page sets its column width with
  `--page-width` and its own CSS (`src/<page>/*.css`) uses only the tokens. The phone breakpoint
  stays 48rem (44 px touch targets below it).
- **Confirmations (#486): `src/ui/ConfirmDialog.tsx` is the one to reuse for every "¿seguro?"** --
  no `window.confirm`, no inline question inside a card. `ConfirmProvider` is mounted once at the
  app root (`main.tsx`, around `Router`); a component asks with `const confirm = useConfirm()` and
  `await confirm(options)` -> `Promise<boolean>` (true confirmed; false cancelled, closed, or
  replaced by a newer question). `ConfirmOptions`: `title` (the question, the dialog's accessible
  name), `message?` (its description), `confirmLabel?` («Aceptar»), `cancelLabel?` («Cancelar»),
  `confirmIcon?` (a tick), `icon?` (the card's icon; a warning when destructive, a question mark
  otherwise), `destructive?` (red-pen confirm button, focus on the cancel button; otherwise the
  focus is on the confirm button), and `onConfirm?: () => Promise<string | null>` with
  `busyLabel?` («Un momento…»): work run while the modal stays open and busy (buttons disabled,
  the focus on the card, Tab and Shift+Tab kept on it (#491: with nothing tabbable inside, the
  browser would move it out, where a later Escape reaches window handlers), Escape and the backdrop
  ignored), null closing it confirmed, a Spanish sentence (or a thrown
  error) shown as its `role="alert"` with only **Cerrar** (answers false). `useConfirm()` outside
  the provider throws; tests render under it (`render(ui, {wrapper: ConfirmProvider})`). The modal
  is a native `<dialog>` opened with `showModal()` (top layer: the page behind is inert), whose
  Tab and Shift+Tab cycle between its own enabled, rendered controls in document order (#495:
  past the last one Chromium would otherwise put a Tab stop on `<body>`, where Escape reaches
  window handlers; a control under `display: none`, `visibility: hidden`, a closed `<details>` or
  a `<fieldset disabled>` is left out, and one that does not take the focus is passed over),
  labelled by its `h2` and described by the message; Escape (handled on the dialog, so a
  panel behind that closes on Escape never sees it) and a click on the backdrop (a press that
  started there) cancel; the focus returns to the element that had it; the page's scroll is locked
  (`overflow: hidden` on `<html>`) while it is open. `confirmDialog.css` uses tokens only:
  `--scrim` dims the whole viewport (`::backdrop`), the card is `--surface` with `--card-shadow` +
  `--shadow-float` (#485), the destructive button `--correction` / `--correction-hover` with
  `--on-correction` text (AA checked in `tokens.test.ts`); at phone width the buttons stack full
  width. The icons are `src/ui/icons.tsx` (`TrashIcon`, `CloseIcon`, `CheckIcon`, `WarningIcon`,
  `QuestionIcon`, `RestoreIcon`: inline `currentColor` SVG, `aria-hidden`). Every confirmation of
  the app is on it since #491 (the last inline one, `VersionsPage`'s restore, moved); none uses
  `window.confirm` or `window.alert`.
- `src/Router.tsx` picks the page from `window.location.pathname` (`/pair` -> `PairPage`,
  `/capture` -> `CapturePage`, `/live` -> `LivePage`,
  `/subjects/<subject>/topics/<topic>` -> `TopicPage`, `/subjects/<subject>/topics/<topic>/notes`
  -> `NotesPage`, `/subjects/<subject>/topics/<topic>/pending` -> `PendingPage`,
  `/subjects/<subject>/topics/<topic>/versions` -> `VersionsPage`, `.../quiz` -> `QuizPage`, `.../exam` -> `ExamPage`, `.../practice` -> `PracticePage`,
  `.../workspace` -> `WorkspacePage`, `.../study` -> `StudyPage`, `.../material/<name>` -> `MaterialPreviewPage`, `/subjects/<subject>/style-guide` -> `StyleGuidePage`, anything else
  -> `App`); the backend's SPA fallback serves the app for every non-API path, so
  no router library is used.
- `src/capture/` is the capture page. Nothing outside the directory imports it except
  `src/Router.tsx`, the study workspace (`CapturePage` only) and the study desk
  (`src/desk/DeskCreate.tsx`: `createSubject` / `createTopic` of `api.ts` and `describeFailure` of
  `failures.ts`, #368), and inside it only `api.ts` and
  `sessionSocket.ts` reach the network:
  - `CapturePage.tsx`: `CapturePage({now?, preset?, onRunningChange?, suspended?, onRecordingChange?})` -- shows `SessionPicker`
    until a session is open, then `CaptureScreen` keyed by `session_id` (`embedded` when there
    is a `preset`), then, standalone only, `SessionEnded` after **Terminar** (#413; with a
    `preset` an end goes straight back to `TopicSessionStart`), so a second session in
    the same visit is a new component and not the old one with new props. That state is all the
    page remembers: nothing of a session survives a reload. With `preset` `{subjectId, topicId}`
    (#312) `TopicSessionStart` replaces the picker: no subject/topic lists and no tutor, just
    "Empezar una sesión nueva" / "Continuar la sesión abierta" for that topic (same resume-or-start
    rule as the picker; since #461 no sentence about the session above the button, and inside the
    workspace its «Capturar» heading is visually hidden, the Captura tab names it); `onRunningChange(running)` reports whether a session's screen is shown.
  - `SessionPicker.tsx`: `SessionPicker({onSession?, now?})` -- subject and topic lists with
    their create forms; for the chosen topic it resumes `open_session_id` or starts a new
    session, and reports the result as `OpenedSession {session, subjectName, topicName}`.
  - `CaptureScreen.tsx`: `CaptureScreen({session, subjectName, topicName, onEnded?, now?,
    playShutter?, flashMs?, embedded?, longOutageMs?, reconnectDelaysMs?})` -- the running session, where socket, transcriber and camera meet:
    it builds the transcriber `hello.ack.stt_mode` asks for, uploads each burst, renders the
    buttons, the thumbnails, the transcript and the pending counter, and owns every Spanish
    message of the page. Also exports `captureCapabilities()` (which omits `audio_format` when
    `audioStreamSupported()` is false, so the backend cannot pick a `server` mode the client
    could not obey), `shutterClick()` and `FLASH_MS`.
  - `sessionHealth.ts` (#262): `useSessionHealth(sessionId, active, intervalMs = HEALTH_POLL_MS)`
    -> the lines to show; `fetchSessionHealth(id)` (the summary, `"gone"` on 404, `null` for an
    unusable answer, which keeps the previous lines), `healthLines(health)` (the Spanish lines,
    none when healthy) and `sessionHealthPath(id)`. The body is checked by hand (web-only route,
    no protocol schema).
  - `wakeLock.ts`: `ScreenWakeLock({wakeLock?, visibility?})` -- `start()` / `stop()`
    (both idempotent) and `held`; requests `navigator.wakeLock.request("screen")`, re-requests on
    `visibilitychange` to visible after the browser released it, releases a lock that arrives
    after `stop()`, and never throws or reports.
  - `api.ts`: the REST client -- `listSubjects()`, `createSubject(name)`, `listTopics(id)`,
    `createTopic(id, name)`, `startSession(subjectId, topicId, clientTimeMs)`,
    `resumeSession(id)`, `endSession(id, reason, clientTimeMs)` (never `prepare_notes`, #413)
    and
    `uploadCaptures(id, metadata, images)` (multipart: the `METADATA_PART` part plus one
    `image_N` part per still). Every call decodes its answer with the `src/protocol/` decoders
    and returns `ApiResult<T>` = `{kind: "ok", value} | {kind: "refused", status, detail} |
    {kind: "error", status} | {kind: "unexpected", status, expected, problem} | {kind:
    "unreachable"}`; `refused` carries the backend's own Spanish `detail` and `unexpected` is a
    2xx body that is not the message the endpoint promises, which is reported and never used.
    `failures.ts`: `describeFailure(prefix, failure)` turns one into a Spanish sentence.
  - `sessionSocket.ts`: `SessionSocket({wsPath, clientTimeMs, capabilities?, onEvent?,
    reconnect?: {resume?, delaysMs?, clock?, maxQueued?}})` (#411: `resume()` answers a
    `ResumeOutcome` `ok | ended | retry`; getters `reconnecting`, `queuedCount`; events
    `reconnecting`, `reconnected`) --
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
- `src/App.tsx` is the study desk (`/`, heading "Mesa de estudio"), the single way into a topic's
  Construir and Estudiar (#368, epic #365): every subject (a region named after it) with its
  topics. The desk never picks a topic's screen for the student (human decision): each row has
  two explicit buttons, **Construir** (`.../workspace`) and **Estudiar** (`.../study`), and the
  topic's name links to its topic card page (the Ficha), after "Sesión abierta" (a link to the
  topic's `.../workspace`), "Última sesión: <fecha>" and "<n> dudas por revisar" when the list
  carries them. Under each subject's name a
  "Guía de estilo" link to its style guide page; below its topics **Nuevo tema** (an
  `aria-expanded` toggle) shows a form "Nuevo tema de <subject>" (field "Nombre del tema",
  **Crear**); below the subjects the block "Nueva asignatura" (field "Nombre de la asignatura",
  **Crear**, shown once the subject list answered). Both post through `capture/api.ts`
  `createSubject` / `createTopic` (`src/desk/DeskCreate.tsx`); an empty name says "Escribe el
  nombre de la asignatura." / "Escribe el nombre del tema." and a failure is the capture picker's
  sentence (`capture/failures.ts`: "No se ha podido crear la asignatura: <detail>" and so on). A
  created subject is listed at once (without topics); a created topic opens in its
  `.../workspace` (`App({navigate?})`, `window.location.assign` by default). The desk links
  neither `/capture` nor `/live` any more; both keep working (the Android WebView, bookmarks).
  Empty states: no subjects ("Crea la primera en «Nueva asignatura» y dale un tema."), a subject
  without topics; a failing topic list is reported inside its subject only.
- `src/desk/entry.ts` (#368): the path builders `topicWorkspacePath`, `topicStudyPath`,
  `topicCardPath`, each over a `TopicRef {subject_id, topic_id}` (any protocol `Topic` or
  practice-summary row). There is no state-based entry choice: the student moves between a
  topic's screens with the rows' Construir / Estudiar buttons and the headers' switch.
  Under the heading, "Gasto de hoy" (`src/desk/DeskCost.tsx`, #260) reads `GET /api/cost` (with
  `subject`, `topic` and `session` of the first topic whose list entry carries `open_session_id`,
  once the lists answer) and shows "Hoy (UTC): <day_usd> USD de <max_usd_per_day> USD" ("(sin
  límite)" for a null cap) and, with an open session, "Sesión abierta: <session_usd> USD de
  <max_usd_per_session> USD"; an `alert` banner ("Se ha alcanzado el límite de gasto: el
  observador está en pausa y el editor pedirá confirmación antes de cada llamada.",
  `PAUSED_BANNER`) when `observer_paused` or `editor_needs_confirmation` is true, and a `note`
  when unpriced calls make the spend an underestimate. A failed read is one plain Spanish line
  (no alert). Caps are changed in the configuration file only. After it, "Repasos para hoy"
  (`src/desk/DeskPractice.tsx`, #285) reads `GET /api/practice/summary` (#280) once: when some
  topic has due or new items, a region named "Repasos para hoy" with the totals ("13 pendientes,
  5 nuevas en 2 temas.") and one row per such topic in the backend's order ("Historia · La
  Revolución Francesa — 12 pendientes, 5 nuevas", a zero count left out), the name linking to
  the topic's study screen (`.../study`, #368), which shows its reviews and opens the practice
  queue from **Tarjetas de memoria**; with practice material but nothing to review, "Nada que repasar hoy.
  Próximo repaso: <fecha>." (the earliest `next_due`); without any practice material (no topics
  and no warnings) the block is not rendered. The response's `warnings` are listed (a list named
  "Avisos de los repasos"); a failed read is one plain Spanish line (no alert) and the rest of
  the desk is unaffected. Below the phone breakpoint
  (48rem, `src/desk/desk.css` on `main.desk`) the desk is one column with 44 px links and
  buttons and no horizontal scroll; wider it keeps the browser's default flow.
- `src/desk/practiceSummary.ts` (#285): `fetchPracticeSummary()` (`GET /api/practice/summary`,
  decoded strictly as `PracticeSummary`: `now`, `topics[]` with `subject_id`, `subject_name`,
  `topic_id`, `topic_title`, `due`, `new`, `next_due` (ISO or null), `totals`, `warnings`) -> the
  `ReadResult` of `desk/api.ts`; `topicsToReview`, `nextDue`, `countsText` ("12 pendientes, 5
  nuevas") and `formatDue` for the block.
- `src/desk/costApi.ts` (#260): `fetchCostStatus(session?)` (`GET /api/cost`, decoded as
  `CostStatus`) and `fetchTopicCost(s, t)` (`GET /api/subjects/{s}/topics/{t}/cost`, decoded as
  `TopicCost`: `total`, `sessions[]` with `session_id` and `started_at_ms`, `no_session`), both
  -> the `ReadResult` of `desk/api.ts` (whose `getJson` it reuses); `formatUsd` ("0,1234 USD",
  four decimals, always labelled USD, never euros) and `formatTokens`.
- `src/topic/`: `TopicPage` (`← Mesa de estudio` link, heading "Tema <topic name>", "Asignatura
  <subject name>", the ids until the lists answer), reached from the topic's name on the desk, has at
  the top the pair **Construir** (`.../workspace`) / **Estudiar** (`.../study`), plain links in a
  `nav` "Abrir el tema" (#368; it replaced the single "Abrir espacio de estudio" link), then shows `TopicCard`, a line sending the student to Construir,
  `MaterialsPanel` ("Material de estudio", `src/materials/`, below), `PdfUploadForm`,
  `WebSearchPanel`, `WebPageForm`, `BookTitleForm` and `TopicCostBlock` (below the phone
  breakpoint, 48rem, `topic/topic.css` on `main.topic-page`: one column, full-width fields,
  44 px links, buttons and fields, no horizontal scroll); an unknown topic (404) shows the
  backend's Spanish detail and none of the forms,
  and a successful upload (`onImported`) or a kept web page (`onKept`) reloads the card. `TopicCard` is the card of VISION §2 ("Resumen del
  tema"): Fuentes (✓/○ handwritten pages, book pages, PDF, webs), Sesiones (count and minutes of
  conversation), Pendiente (doubts to review), Material (`Apuntes v<N>` from `notes_version`, then
  Esquema, Quiz, Flashcards, Examen, Diapositivas marked present when a file under `generated/`
  is named `outline`/`quiz`/`flashcards`/`exam`/`slides` or their Spanish names, `MATERIALS`).
  Generating, previewing and downloading each material is `MaterialsPanel` (#79; the card's
  former "Descargas" row moved there, per material).
  There is no "Prepárame el tema" button (#434, removed with `PrepareTopic`): nothing writes the
  notes on a click. Above the materials a line reads "Para redactar los apuntes, ve a Construir y
  pídeselo al asistente en el chat.", its "Construir" a link to `topicWorkspacePath`; the whole-topic
  generation is asked for in the workspace chat (the chat's `prepare_notes` request, which still
  uses `POST .../notes/generate`).
  `PdfUploadForm` ("Añadir un PDF": a file input, an optional "Páginas" text such as `82-94`, sent
  as typed). `api.ts`: `uploadPdf(subjectId, topicId, file, pages)` posts the multipart form to
  `POST /api/subjects/{s}/topics/{t}/sources/pdf` -> `{kind: "ok", imported} | {kind: "refused",
  status, detail} | {kind: "error", status} | {kind: "unreachable"}`; a refusal's Spanish
  `detail` (413 too large, 422 unreadable or bad range) is shown as it comes.
  `TopicCostBlock` (#260, section "Coste", not shown for an unknown topic, reloaded with the
  card) reads `fetchTopicCost` and shows "Total del tema: <usd> USD · <n> tokens · <n> llamadas",
  a list "Coste por sesión" with one row per session ("Sesión del <fecha>", or "Sesión <id>" when
  its start is unknown) and, when there are any, "Fuera de las sesiones (editor, material)" for
  the calls bound to no session; "Todavía no hay gasto en este tema." when there is nothing, and
  the `note` "Hay llamadas sin precio conocido: el total se queda corto." (`UNPRICED_WARNING`)
  when `total.unpriced_calls` > 0.
  `BookTitleForm` (#214, "Libro de texto", not shown for an unknown topic) shows the topic's
  textbook title as `Libro «<title>»` (the title book pages are cited with) or "Este tema aún no
  tiene libro de texto." when none is set, and a "Título del libro" input (`maxLength` 200,
  `BOOK_TITLE_MAX`, prefilled with the stored title) with "Guardar"; the title the backend
  answers replaces the shown one ("Título del libro guardado."). A 422's string `detail` (empty
  title, or one that looks like a key) is shown as it comes; any other failure shows a generic
  Spanish message and the shown title stays. `api.ts`: `fetchBook(s, t)` (`GET .../book`) and
  `saveBook(s, t, title)` (`PUT .../book` with `{"title"}`) -> `BookResult`: `{kind: "ok",
  title: string | null} | {kind: "refused", status, detail} | {kind: "error", status} | {kind:
  "unreachable"}`.
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
    `#page=K`), `image` (a pasted image, `sources/images/`, #316), `web`, `transcript` (session
    id, span), `ia` or `unknown`; `sourceVaultId`, `originalPage(meta, page)` (`first_page + page - 1`, the rule of `sources.pdf.original_page`).
  - `NotesView`: headings keep their anchor as `id` plus a `#` link; each reference is a link to
    its definition (`#fn-<label>`, numbered by first citation, `[IA]` for `[^ia]`) that opens the
    sources panel; blocks citing `[^ia]` get the `notes-ia` highlight; the definitions are listed
    under "Fuentes" and open the panel too. `[[?word]]` is underlined as a doubtful word. Web
    snapshots are external sources (#59): their references get `notes-ref-web` (green, dotted)
    and the aria label "Fuente externa (web): <text>" (other sources "Fuente: <text>"), and their
    definition under "Fuentes" gets `notes-footnote-external` and "· fuente externa".
  - Mermaid (#479): a fence whose info string is `mermaid` (any case) renders as `MermaidBlock`
    everywhere `NotesView` is used (document panel, `NotesPage`, `StudyPage`, `PendingPage`,
    `MaterialPreviewPage`, the editor's Markdown preview). `notes/mermaid.ts` imports the `mermaid`
    package lazily (`loadMermaid()`, one dynamic `import()` on the first diagram, so pages without
    one never fetch it and it is its own chunk outside the main bundle) and `renderMermaid(source,
    theme)` renders one diagram at a time after `mermaid.initialize({startOnLoad: false,
    securityLevel: 'strict', theme, suppressErrorRendering: true})`. The theme is `dark` or
    `default` from `useDarkScheme()` (`notes/useColorScheme.ts`), which follows
    `prefers-color-scheme` -- the app has no theme switch of its own -- and a change re-renders
    the diagram. The SVG goes in a `figure.notes-mermaid`: the one piece of notes-derived markup
    inserted as HTML, mermaid's own output sanitized by `strict`. Until it is drawn the source
    shows as a `notes-code` block; a source mermaid cannot parse stays that way plus
    «No se pudo dibujar el diagrama» (`notes-mermaid-error`), and the rest of the notes renders as
    usual. The visual editor keeps a mermaid fence as a code block and saves it unchanged
    (fixture `noteEditor/fixtures/mermaid.md`). `web/package.json` `overrides` pins `lodash-es`
    to `4.18.1` (#482): mermaid 12.0.0 depends on `chevrotain ~11.1.2`, whose packages pin
    `lodash-es` 4.17.23 exactly (high advisories GHSA-r5fr-rjxr-66jc, GHSA-f23m-r3pf-42rh; 4.18.0
    is deprecated as a bad release), and npm's only suggested fix is a downgrade to mermaid 11.
    Drop the override once a mermaid release depends on a fixed `lodash-es`.
  - `SourcePanel` (non-modal `dialog` named after the source): a notes/book page shows the
    flattened `page-NNN.page.jpg` (falling back to the cited file) with zoom (Alejar/Acercar/
    Tamaño original, `+`/`-`/`0` on the focused image) and its transcription (the sidecar's
    `transcription`, else `page-NNN.md`); a PDF page shows `page-NNN.pKKK.jpg` and `.txt`,
    "PDF «<original_name>», página <original page>" and an "Abrir el PDF" link; a web snapshot its
    text and "Fuente externa: copia de <url> (<fetched_at>)" from its sidecar; a transcript span its segments with `MM:SS` timestamps
    (`GET /api/sessions/{id}/transcript`). Focus moves to the panel title; Escape or "Cerrar"
    closes it and returns the focus to the reference. From the phone breakpoint up the panel is
    fixed to the viewport edge, so it never scrolls or rewraps the notes; below it, see
    "Phone width" next.
  - Phone width (#263): below **48rem** (the phone breakpoint, 360-430 px phones and the Android
    study desk WebView; `@media (max-width: 48rem)` in `notes.css`, `desk/desk.css` and
    `topic/topic.css`) the notes page is one column -- notes, then the sources panel (in flow,
    sticky to the bottom of the screen), then the chat -- with no horizontal scroll of the page
    and 44 px buttons. The sources panel and the chat are collapsible there, both collapsed at
    first: the "Fuentes" and "Chat con el editor" toggle buttons (`aria-expanded`,
    `aria-controls`; hidden from 48rem up, where nothing collapses) show or hide them. A footnote
    (or a chat source) expands the sources panel on the cited source, "Cerrar"/Escape collapses it
    again, and "¿Por qué pusiste esto?" expands the chat before scrolling to it; with no source
    open the expanded panel says "Toca una referencia de los apuntes para ver aquí su fuente.".
    From 48rem to 80rem the layout is the wide one with the chat under the notes; from 80rem up
    the chat is the sticky column beside them.
  - `api.ts`: `fetchNotes`, `fetchSourceMeta`, `fetchSourceText`, `fetchTranscript` (all
    `ReadResult`), `sourceUrl(vaultId)`. Images load by plain `<img src>`, so they rely on the same
    localhost trust as every other request of the web app. The student's edits (#316):
    `saveNotes(s, t, text, baseRevision) -> SaveNotesResult` (`saved {text, revision,
    changedSections, notesChanged}` | `changed {text, revision}` (409 `notes_changed`, also told by
    the body's `text` when the code is missing) | `busy` (409 `notes_busy`) | `invalid {errors}`
    (422; a request-validation 422 gives one generic Spanish error) | `failed {message}`),
    `uploadPastedImage(s, t, file) -> ok {sourceId, markdown} | failed {message}` (multipart `file`
    part; the server's Spanish `detail`) and `notesImageUrl(s, t, src)` (a `../sources/<kind>/<file>`
    link through the read API, else `null`). `parseInline` reads `![alt](src)` as an `image` node;
    `NotesView` shows it through its optional `resolveImage` (without one, "[Imagen: alt]").
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
    (`POST .../notes/why`, #69), and the answer's sources open in the `SourcePanel`. A topic without notes (404) shows, under the
    backend's detail, the same line with the "Construir" link as the topic page (#434: no button
    writes the notes; they are asked for in the workspace chat); the chat appears once notes exist.
- `src/chat/` (#71): the chat with the editor over the editor chat API (docs/modules/server.md).
  - `feedback.ts` / `FeedbackChip.tsx` / `feedbackChip.css` (#472): `FeedbackRef` `{id, kind:
    "bug" | "mejora", title}`, `readFeedback(value)` (a turn's `feedback`, `null` when absent or
    malformed), `feedbackLabel` («Bug apuntado» / «Mejora apuntada») and the chip both chats
    render on a turn that recorded app feedback (`data-testid="feedback-chip"`, class
    `feedback-chip-<kind>`).
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
  lado". "Restaurar la versión <N>" (every version but the current one) asks in the confirmation
  modal (#491, `useConfirm`): «¿Restaurar la versión <N>?», the message "Los apuntes actuales
  pasarán a ser los de la versión <N>, guardados como una versión nueva. No se pierde ninguna
  versión." (plus "Los cambios hechos después de la versión <latest> no tienen versión propia."
  when the notes changed after it), **Restaurar** (`RestoreIcon`) / **Cancelar**. Confirming
  restores while the modal shows «Restaurando…»; on success it closes, the page says "Se ha
  restaurado la versión <K> como versión <N>." and the backend's warning, and reads the history
  (and so the comparison) again; a refusal (409 another notes operation, already that version)
  stays in the modal as "No se pudo restaurar la versión: <Spanish `detail`>" with **Cerrar**.
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
  <c> de <n>." with "Repetir el quiz" and, when some answer was wrong, "Repetir las falladas"
  (#282): a round with only those questions (same UI, meta prefixed "Repaso de falladas: "),
  posted with `questions` (their ids) as a partial attempt. "Intentos anteriores" lists the
  latest ten results, a partial one marked "(repaso de falladas)" (`QuizResult.partial`). The
  form "Generar un quiz" ("Número de preguntas" 1-30, "Dificultad" Variada/Fácil/Media/Difícil,
  "Generar quiz"; "Generar igualmente" past a cost cap) posts `POST .../generated/quiz` and reads
  the quiz again. `api.ts`: `fetchQuiz`, `fetchQuizResults`, `generateQuiz`, `saveQuizResult` ->
  `ActionResult` (read leniently: `readStoredQuiz`, `readResult(s)`).
- `src/exam/` (#283): `ExamPage` at `<topic path>/exam` ("Corregir examen" in the materials panel
  links to it once the exam exists; `← Tema <name>`, heading "Corregir examen de <name>", "<n>
  preguntas · <p> puntos · <d> minutos · de los apuntes v<N>", a `note` when stale). Each question
  is a group "Pregunta <n>" with its points and statement; "Ver solución y criterios" reveals the
  worked solution, "En los apuntes:" links and a points input per rubric criterion ("<criterio>
  (máx. <p>)", decimal comma accepted, empty is 0; out of range it is `aria-invalid` with "Entre 0
  y <p>."), and the question's "<x> de <p>". "Total: <x> de <y> (<z> %)" adds up as the student
  types; "Guardar corrección" (disabled while a value is invalid) posts it and the `status` says
  "Corrección guardada: …"; a refusal (the exam regenerated meanwhile) is an `alert`. Without an
  exam it says so and links to the topic's study materials. "Correcciones anteriores" lists the
  latest ten. `api.ts`: `fetchExam`, `fetchExamResults`, `saveExamResult` -> `ActionResult` (read
  leniently: `readStoredExam`, `readResult(s)`), `formatNumber`.
- `src/practice/` (#81): `PracticePage` at `<topic path>/practice` (the topic card's "Práctica" ->
  "Practicar con repetición espaciada" links to it; heading "Practicar <name>", "<d> para repasar
  · <n> nuevas · <l> de <t> ya vistas", the backend's warnings as `note`s). One `article` at a
  time: a flashcard ("Mostrar respuesta", its back and "En los apuntes:" links, then the ratings
  "Otra vez"/"Difícil"/"Bien"/"Fácil") or a quiz question (radios or "Tu respuesta", "Comprobar",
  "✓ Correcta"/"✗ Incorrecta…" -- a short answer that does not match asks "¿La has acertado?" --,
  the explanation, then "Difícil"/"Bien"/"Fácil" for a right answer or "Siguiente" for a wrong
  one). Each review is posted at once; the `status` line says when the item comes back ("Bien:
  volverá en 6 días."), an item rated "Otra vez" is asked again at the end of the session, a
  refusal is an `alert` and keeps the item. With nothing left: "¡Hecho! Has repasado…" or "No
  tienes nada que repasar ahora." with "Próximo repaso: <fecha>", and "Volver a comprobar".
  Setting aside (#281): each item has "Descartar esta tarjeta" / "Descartar esta pregunta"; on
  success it leaves the session ("Tarjeta descartada: ya no saldrá en la práctica.") and joins
  the collapsible "Descartadas (<n>)" list (`details`, from the queue's `suspended`; the meta line
  adds "· <n> descartadas"), where "Recuperar" brings it back ("Recuperada: volverá a salir cuando
  le toque, con su historial."); a refusal is an `alert` and changes nothing.
  `api.ts`: `fetchPractice`, `sendReview`, `sendSuspension` -> `ActionResult` (read leniently:
  `readQueue`, `readOutcome`, `readSuspension`), `describeInterval`.
- `src/materials/` (#79): `MaterialsPanel`, section "Material de estudio" on the topic page, over
  `GET .../generated` (`MaterialsStatus`) and `GET /api/generators` (only for each kind's
  description). One item per kind in the order of study (`STUDY_ORDER`: esquema, quiz, flashcards,
  examen, diapositivas, then any other alphabetically), named by its Spanish title: "○ Sin
  generar" or "✓ Generado el <fecha y hora> · de los apuntes v<N>", a "Desactualizado" badge with
  the backend's `stale_reason` when stale, then its links -- "Hacer el quiz" (the quiz page),
  "Corregir examen" (the exam correction page),
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
  rendered by `NotesView` over `parseNotes`, so no markup of it reaches the DOM (a mermaid mind map
  is drawn as a diagram, #479; a Marp deck shows as its Markdown source). `api.ts`: `fetchGenerators`, `fetchMaterials`,
  `fetchGeneratedText` (`ReadResult`), `generateMaterial(s, t, kind, confirmOverCap)`
  (`ActionResult<Generated>`: `kind`, `notesVersion`, `warnings`), `fileUrl`, `previewPath`,
  `generatedName`; lenient readers `readGenerators`, `readMaterials`, `readGenerated`.

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
`src/test/setup.ts` mocks the `mermaid` package for every test with `src/test/fakeMermaid.ts`
(#479): jsdom cannot lay out SVG, so `fakeMermaid.render` answers an SVG
(`data-testid="mermaid-svg"`, `data-theme` of the last `initialize`) and rejects a source whose
first line is `invalid`; `fakeMermaid.loads` counts the lazy imports. It also installs
`src/test/dialog.ts` (#486): jsdom's `HTMLDialogElement` has no behaviour, so `show()`,
`showModal()`, `close()` (the `close` event in a task) and Escape's cancelable `cancel` on the
topmost modal follow the HTML spec there (the page behind is not inert in jsdom).
`src/capture/testing/` holds the fakes the capture
tests run on, because jsdom has none of these APIs: `installCaptureFakes()` installs the media
devices / stream / track, `ImageCapture`, `SpeechRecognition`, `WebSocket` and
`AudioContext`/`AudioWorklet` fakes at once (`installMediaFakes()`,
`installSpeechRecognitionFake()`, `installWebSocketFake()` and `installAudioFakes()` install one
family each, `installCanvasFakes()` and `fakePreview()` cover the canvas fallback and the preview
element), and every installer returns a `restore()`. `installSpeechRecognitionFake(globals,
{phrases: true})` (or `installCaptureFakes({speechPhrases: true})`) is a browser with contextual
biasing: `FakeBiasingSpeechRecognition` records the `phrases` each `start()` found in
`phrasesAtStart`, and `FakeSpeechRecognitionPhrase` sits on the `SpeechRecognitionPhrase` global. `installCaptureFakes()`
also installs a fake `navigator.wakeLock` (`installWakeLockFake()`, `FakeWakeLock` /
`FakeWakeLockSentinel` with `releaseFromBrowser()`; `{wakeLock: false}` is a browser without the
API), `setVisibility("hidden" | "visible")` hides or shows the tab and fires `visibilitychange`,
and `devices.plugInCamera()` hands out a fresh live video track after the old one ended. Tests drive them -- a fake recognition emits
results, ends and errors, a fake socket records what was sent and lets a test push server events
in -- so no test touches a real camera, microphone, network or backend.

Waits (#430): under host load (every core busy) Testing Library's role queries cost hundreds of
milliseconds each, so a test must not assume a page's reads land within the default waits.
`src/test/setup.ts` sets `configure({asyncUtilTimeout: LOAD_TIMEOUT})`, so every `findBy*` /
`waitFor` waits up to 5 s (`src/test/timeouts.ts`). An element a page renders from a read of its
own (a separate fetch from the one the test first awaited) is awaited with `findBy*`, never read
with a synchronous `getBy*` right after the first wait. A test that drives a page through several
reads passes `PAGE_TEST_TIMEOUT` (15 s) as the third argument of `it`; the global `testTimeout`
stays vitest's default. Waits are condition waits only: no fixed sleeps.

The shared frame (#487): `src/workspace/WorkspaceFrame.test.tsx` pins the frame's structure (the
band with the mode switch, the title and the right-hand links; the left card above the chat card;
the document card; the switch's order; the detail and `data-panel`); `src/study/StudyPage.test.tsx`
that Estudiar renders in it (options card, chat card with the textarea and the microphone icon,
document card with its pinned header, the material panel under it with its own header);
`src/study/chat/StudyChat.test.tsx` the study chat's composer (textarea, **Preguntar** and the
microphone icon in one row; Enter asks, Shift+Enter does not); and `src/study/layout.test.ts`
reads `study.css` from disk to pin that it redefines none of the frame's rules and that only the
options' body, the document's body and the material's body scroll.
