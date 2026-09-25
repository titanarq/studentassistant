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
What exists today, after issues #29 and #51: the knowledge-state model, its ops, the pure fold, the
snapshot, the vault-backed loader and the live loop. The pending queue's deduplication (#55), the
digest (#56) and the purge (#60) are not written yet. Everything below except the live loop is
re-exported by `studentassistant.observer`; the live loop is `studentassistant.observer.live`
(its batch rendering `studentassistant.observer.context`), kept out of the package root so that
importing the state model never imports the llm module.

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
git, imports `anthropic`, `studentassistant.server` or a vault submodule (only the
`studentassistant.vault` root), and only `live.py` imports `studentassistant.llm` (checked by
`tests/observer/test_boundaries.py`).

### Live loop -- `live.py`, `context.py`
`ObserverLoop(bus, lookup, *, settings=ObserverSettings(), client_factory=None, digest=..., clock=...)`
subscribes to the session bus (`start()`; `stop()` folds what was delivered and waits for the
calls in flight). `lookup(session_id)` gives an attached session's vault handle
(`SessionBus.attached`); `client_factory(LedgerBinding)` builds the session's `observer` client
(`default_client_factory(settings, transport)`: `get_client("observer", ...)` bound to the
session's ledger, so every call is capped and recorded); `digest(vault, subject, topic)` reads the
topic digest (none until #56). The server builds one when `create_app` gets an `llm_transport`
(`serve` passes the real one) and `[observer] enabled`.

- **Input** (`OBSERVER_KINDS`): `transcript.final`, `capture.stored`, `page.transcribed`
  (`PAGE_TRANSCRIPTION_KIND`: the page transcription, #50, MUST publish it with
  `payload.capture_id` and `payload.text`), `button`, `marker`, `command`, `observer.state_op` of
  any origin but `observer` (shown as `student op`), and the lifecycle events. Each becomes one line
  of the pending batch (`context.batch_item`).
- **Scope**: the first event of a session loads its topic's snapshot (`load_observer_snapshot`)
  and builds a fresh conversation: system = the `observer` prompt + the topic block (subject,
  title, digest); first user turn = the folded state (`render_state`: outline, concepts, captures,
  source context, open pending items, recent notes -- ids, never the earlier segments' text) and
  the first batch. Nothing of another topic is ever read. A resumed session, or a new session of
  the topic, is always rebuilt this way, never from the full history.
- **Triggers** (`[observer]`, `SA_OBSERVER__*`): `batch_segments` final segments (default 6) or
  `batch_speech_seconds` of speech (default 30) waiting, or at once for a capture, a page
  transcription or a `switch_source` button. One call per session is in flight; what arrives
  meanwhile coalesces into the next batch, which also waits until the ops just published are
  folded. The consumer never waits for Claude and the bus never waits for the consumer.
- **Answer**: the strict tool `apply_state_ops` (`ApplyStateOps`: `ops: list[StateOp]`, each
  op's `op` a required enum; `state_ops_tool()`), `tool_choice: auto`. Each op is parsed and
  validated in order against the current state; valid ones are published as
  `observer.state_op` (origin `observer`). Malformed or inapplicable ops, or no tool call, are
  re-asked once (the errors go back as an `is_error` tool result); what is still invalid is
  logged and dropped. Every `tool_use` gets its `tool_result` at the start of the next user turn.
- **Caching**: tools + system are the cached prefix; each request also marks the newest block of
  the conversation, which grows append-only by one user turn and one answer per call.
- **Conversation file**: `conversations/observer-<session-id>.jsonl` through
  `append_conversation_record`: a `context` record (reason `start`/`resume`, the snapshot's
  `event_count` and `cursor`, model, prompt hash), then each `user` turn and `assistant` answer
  (model, prompt hash, usage), and `status` changes.
- **Cost caps**: a reached cap (`CostCapReachedError`, nothing sent) pauses the observer: the batch
  is kept, `status(session_id)` is `paused` and one `observer.status` event (`status: paused`,
  `reason`, `cap`, `limit_usd`, `total_usd`) is published; the next new item tries again, and a
  successful call publishes `status: running`. Any other Claude failure keeps the batch the same
  way with `status: error`. A failed call is never retried until something new arrives.
- **Ending**: `flush(session_id)` is registered with `SessionService.add_before_ended` (after the
  gateway's STT flush, so it sees the last finals): it waits for the call in flight and sends what
  is still waiting, so its ops land before `session.ended` (bounded by the end hook timeout; a call
  is shielded, never cancelled). `wait_idle(session_id)` waits without sending; `session.ended`
  forgets the session.

## Boundaries
- Never writes notes; that is the editor's job.
