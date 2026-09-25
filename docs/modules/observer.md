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
- Context purge: roll the live conversation over to snapshot + digest + tail past a token
  threshold and at session end; context is always one topic only (ADR-0003).

## Public surface
What exists today, after issue #29: the knowledge-state model, its ops, the pure fold, the
snapshot and the vault-backed loader. The live loop, the pending queue's deduplication, the
digest and the purge are not written yet. Everything below is re-exported by
`studentassistant.observer`.

### Event kinds the fold reads -- `ops.py`, `fold.py`
- `STATE_OP_EVENT_KIND = "observer.state_op"`: the one event kind that carries a state op; its
  `payload` is the op itself (`op_payload(op)` writes it, `parse_op(payload)` reads it). Any
  origin may emit it (the user resolves pending items too).
- `SEGMENT_EVENT_KIND = "transcript.final"` (`payload["segment_id"]`) and
  `CAPTURE_EVENT_KIND = "capture.stored"` (`payload["capture_id"]`): fact events written by the
  capture side; the fold only registers their id, so ops can reference that segment or capture.
  A repeated id keeps its first registration.
- Producers MUST emit them: the stt/server segment ingestion MUST append `transcript.final`
  with `payload.segment_id` and the server capture ingestion MUST append `capture.stored` with
  `payload.capture_id`, using the protocol ids (`segment_id` of `transcript.final`, `capture_id`
  of the captures REST call), so ops can reference those segments and captures.
- Every other kind is ignored.

### State ops -- `ops.py`
Frozen Pydantic v2 models (`extra="forbid"`) in the discriminated union `StateOp` on the field
`op` (`OP_NAMES` lists them): `add_section` (`section_id`, `title`, `parent_id?`),
`rename_section` (`section_id`, `title`), `assign_segments` (`section_id`, `segment_ids`; the last
assignment of a segment wins), `add_concept` (`concept_id`, `name`, `section_id?`, `segment_ids`),
`link_capture` (`capture_id`, `segment_ids`; links accumulate), `set_source_context` (`kind` in
`notes`/`book`/`pdf`/`web`, `reference?`), `add_pending` (`pending_id`, `category` in
`illegible`/`unexplained_concept`/`incomplete`/`possible_error`/`contradiction`, `description`,
`segment_ids`, `capture_ids`), `resolve_pending` (`pending_id`, `resolution`), `note` (`text`,
`segment_ids`). A malformed payload is a `pydantic.ValidationError`.

### Topic state -- `state.py`
`TopicState`: `sections` (the outline, by id in the order added; `outline(parent_id)`),
`segments` and `captures` (registered ids -> the `EventRef` that stored them), `assignments`
(segment -> section; `segments_of(section_id)`), `concepts`, `capture_links` (capture -> segments),
`source_context` (`SourceContext` or `None`), `pending` (`PendingItem`s open and resolved;
`open_pending()`, `resolved_pending()`) and `notes` (`ObserverNote`). Every item keeps the
`EventRef` (`session_id`, `seq`) of the event that made it, because `seq` restarts per session.

### Fold -- `fold.py`
`fold(events) -> TopicState` over the `(session_id, Event)` pairs (`TopicEvent`) of every session
of a topic in `(session_id, seq)` order, as `vault.read_topic_events` yields them. Pure and
deterministic: no I/O, input never mutated, equal input gives an equal state (and equal JSON).
`validate_op(state, op)` checks an op before it is applied and raises an `ObserverStateError`
(a `ValueError`): `UnknownIdError` (`op`, `entity` -- section, segment, capture or pending
item -- and `id`), `DuplicateIdError`, `PendingAlreadyResolvedError`. `apply_op(state, op, at)`
returns the new state. `fold` never skips an op: it raises the error with `at` set to the event;
`InvalidEventError` is a read kind with a malformed payload and `EventOrderError` events out of
order.

### Snapshot -- `snapshot.py`
`ObserverSnapshot`: `state_version` (`STATE_VERSION`, bumped when the fold changes meaning),
`cursor` (the `EventRef` of the last event folded, `None` before any), `event_count` and `state`.
`advance_snapshot(snapshot | None, tail) -> ObserverSnapshot` folds the tail on a copy (a tail
event at or before the cursor is an `EventOrderError`); `fold_from(snapshot, tail) -> TopicState`
equals `fold` over all the events for every split point; `snapshot_of(events)` folds from scratch.

### Loader -- `loader.py`
`load_observer_snapshot(vault, subject_slug, topic_slug, *, write_back=True) -> ObserverSnapshot`
reads the events (`read_topic_events`) and the stored snapshot (`read_observer_snapshot`), folds
only the tail after it and writes the result back (`write_observer_snapshot`) when it changed and
`write_back` is true (a read-only caller such as the topic list passes `False`). The stored snapshot is
discarded and the log folded from scratch when it is unreadable, of another `state_version`, or
no longer matches the log (its `event_count`-th event is not its cursor: a session pulled from
another PC with an earlier id, or events appended before the cursor). An op that cannot be
folded raises and nothing is written. Nothing in `studentassistant.observer` opens a file, runs
git, imports `studentassistant.llm`/`anthropic` or imports a vault submodule: only the
`studentassistant.vault` root (checked by `tests/observer/test_boundaries.py`).

## Boundaries
- Never writes notes; that is the editor's job.
