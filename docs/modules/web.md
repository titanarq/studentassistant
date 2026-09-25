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
  `/subjects/<subject>/topics/<topic>` -> `TopicPage`, anything else -> `App`); the backend's SPA fallback serves the app for every non-API path, so
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
  (>= 1.1), so topics may carry `last_session_at_ms` and `pending_count`; both stay optional and
  a topic without them shows only its name.
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
  `PdfUploadForm` ("Añadir un PDF": a file input, an optional "Páginas" text such as `82-94`, sent
  as typed). `api.ts`: `uploadPdf(subjectId, topicId, file, pages)` posts the multipart form to
  `POST /api/subjects/{s}/topics/{t}/sources/pdf` -> `{kind: "ok", imported} | {kind: "refused",
  status, detail} | {kind: "error", status} | {kind: "unreachable"}`; a refusal's Spanish
  `detail` (413 too large, 422 unreadable or bad range) is shown as it comes.

## Boundaries
- Talks only to the backend REST/SSE API; no direct vault or LLM access.

## Tests
vitest + Testing Library with a mocked API (`src/test/mockApi.ts`: `stubApi({path: response})`
stubs `fetch` by method and path).
