# Module: web

**Lives in:** `web/`.

## Responsibility
- **Web capture page** (`/capture`): the development capture client (ADR-0001, ADR-0008) --
  laptop camera preview, high-resolution stills (burst), Web Speech API transcription (or audio
  streaming in server STT mode), session buttons, live transcript; speaks protocol v1 exactly
  like the Android app.

Spanish review UI served by the backend (localhost trusted; other LAN clients use the pairing
token):
- Study desk: subjects/topics, per-topic card (sources, sessions, minutes, pending, materials).
- Notes viewer: rendered `apuntes.md`; clicking a provenance footnote opens the sources panel
  (zoomable page image + its transcription, transcript excerpt, book/PDF page, web snapshot);
  `[^ia]` content highlighted.
- Editor chat (streamed), applied edits shown as a diff; optional push-to-talk.
- Pending panel and the doubts-resolution flow; version history; live session view; generators.

## Boundaries
- Talks only to the backend REST/SSE API; no direct vault or LLM access.

## Tests
vitest + Testing Library with a mocked API.
