# Module: observer

**Lives in:** `backend/src/studentassistant/observer/`. Decision: ADR-0003.

## Responsibility
- Knowledge-state model (outline sections, segment->section assignment, concepts, capture<->
  segment links, source context, pending items) and its pure fold over events.
- The live observer loop (role `observer`, Sonnet): batches final segments, commands and page
  transcriptions; append-only cached conversation; emits validated state ops; coalesces when
  behind; never blocks capture.
- Pending-review queue: illegible word, mentioned-not-explained concept, incomplete information,
  possible error, contradiction between sources; deduplicated; count published to the phone.
- Topic digest at session end, used to resume a topic and as editor input.

## Boundaries
- Never writes notes; that is the editor's job.
