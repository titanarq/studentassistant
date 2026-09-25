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
- `src/topic/`: `TopicPage` (heading "Tema <topic>") is the topic page; for now it only hosts
  `PdfUploadForm` ("Añadir un PDF": a file input, an optional "Páginas" text such as `82-94`, sent
  as typed). `api.ts`: `uploadPdf(subjectId, topicId, file, pages)` posts the multipart form to
  `POST /api/subjects/{s}/topics/{t}/sources/pdf` -> `{kind: "ok", imported} | {kind: "refused",
  status, detail} | {kind: "error", status} | {kind: "unreachable"}`; a refusal's Spanish
  `detail` (413 too large, 422 unreadable or bad range) is shown as it comes.
- `src/App.tsx` is the placeholder study desk: heading "Mesa de estudio", fetches
  `GET /api/health` on mount, decodes it strictly as `rest.health.response` and shows the
  backend `protocol_version` (Spanish loading/error states; any other shape is the error state).

## Boundaries
- Talks only to the backend REST/SSE API; no direct vault or LLM access.

## Tests
vitest + Testing Library with a mocked API.
