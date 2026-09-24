# Module: web

**Lives in:** `web/`.

## Responsibility
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
