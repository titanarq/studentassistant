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
  threshold and at session end (#60); context is always one topic only (ADR-0003).

## Public surface
What exists today, after issues #29, #31, #51, #55, #176, #56 and #60: the knowledge-state model,
its ops, the pure fold, the snapshot, the vault-backed loader, the live loop with its context
purge, the pending-review queue, the compaction the vault purge (#31) writes and the topic digest.
Everything below except the live loop is
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
- `COMPACTED_EVENT_KIND = "observer.compacted"`: written only by the vault purge (#31) in place
  of every earlier event of the topic; its payload (`compaction_payload(snapshot)`:
  `state_version`, `state`) becomes the fold's state at that event. A newer `state_version` or a
  payload that is not a `TopicState` is an `InvalidEventError`; an older version is taken as it is.
- Every other kind is ignored.

### State ops -- `ops.py`
Frozen Pydantic v2 models (`extra="forbid"`) in the discriminated union `StateOp` on the field
`op` (`OP_NAMES` lists them): `add_section` (`section_id`, `title`, `parent_id?`),
`rename_section` (`section_id`, `title`), `assign_segments` (`section_id`, `segment_ids`; the last
assignment of a segment wins), `add_concept` (`concept_id`, `name`, `section_id?`, `segment_ids`),
`link_capture` (`capture_id`, `segment_ids`; links accumulate), `set_source_context` (`kind` in
`notes`/`book`/`pdf`/`web`, `reference?`), `add_pending` (`pending_id`, `kind` in
`PENDING_KINDS` -- `illegible`/`unexplained_concept`/`incomplete`/`possible_error`/`contradiction`
--, `text` (Spanish), `segment_ids`, `capture_ids`, `source_refs`; a payload written before #55
with `category`/`description` is read as `kind`/`text`), `resolve_pending` (`pending_id`,
`resolution?`, `status?` in `resolved`/`auto_resolved`/`dismissed`; only a dismissal may go
without a resolution), `note` (`text`, `segment_ids`). A malformed payload is a
`pydantic.ValidationError`.

### Topic state -- `state.py`
`TopicState`: `sections` (the outline, by id in the order added; `outline(parent_id)`),
`segments` and `captures` (registered ids -> the `EventRef` that stored them), `assignments`
(segment -> section; `segments_of(section_id)`), `concepts`, `capture_links` (capture -> segments),
`source_context` (`SourceContext` or `None`), `pending` (`PendingItem`s open and closed;
`open_pending()`, `resolved_pending()` for the closed ones), `pending_aliases` (a merged
`add_pending` id -> the item it joined; `pending_item(id)` follows it) and `notes`
(`ObserverNote`). A `PendingItem` is `id`, `kind`, `text`, `refs` (`PendingRefs`: `pages` --
capture ids --, `segments`, `sources`), `created_by` (the origin of the event that added it),
`status` (`PendingStatus`: `open`/`auto_resolved`/`resolved`/`dismissed`), `resolution`,
`added_at`, `resolved_at` and `merged_ids`. Every item keeps the
`EventRef` (`session_id`, `seq`) of the event that made it, because `seq` restarts per session.

### Fold -- `fold.py`
`fold(events) -> TopicState` over the `(session_id, Event)` pairs (`TopicEvent`) of every session
of a topic in `(session_id, seq)` order, as `vault.read_topic_events` yields them. Pure and
deterministic: no I/O, input never mutated, equal input gives an equal state (and equal JSON).
`validate_op(state, op)` checks an op before it is applied and raises an `ObserverStateError`
(a `ValueError`): `UnknownIdError` (`op`, `entity` -- section, segment, capture or pending
item -- and `id`), `DuplicateIdError` (a merged pending id counts as taken),
`PendingAlreadyResolvedError` (the item is closed). `apply_op(state, op, at, origin="observer")`
returns the new state. `created_by` is the event's origin; a `resolve_pending` without `status`
is `auto_resolved` from the observer and `resolved` from anyone else. An `add_pending` that
duplicates an open item is merged into it (see the pending-review queue). `fold` never skips an op: it raises the error with `at` set to the event;
`InvalidEventError` is a read kind with a malformed payload and `EventOrderError` events out of
order.

### Snapshot -- `snapshot.py`
`ObserverSnapshot`: `state_version` (`STATE_VERSION`, bumped when the fold changes meaning),
`cursor` (the `EventRef` of the last event folded, `None` before any), `event_count` and `state`.
`advance_snapshot(snapshot | None, tail) -> ObserverSnapshot` folds the tail on a copy (a tail
event at or before the cursor is an `EventOrderError`); `fold_from(snapshot, tail) -> TopicState`
equals `fold` over all the events for every split point; `snapshot_of(events)` folds from scratch.
`compaction_payload(snapshot)` is the payload of the `observer.compacted` event that replaces the
events `snapshot` folded (`studentassistant purge` builds a `vault.purge.Compaction` from it and
the snapshot's cursor): the fold of the compacted log equals the fold of the original one.
`STATE_VERSION` lives in `state.py` (the fold checks compaction payloads against it) and is still
re-exported from here.

### Pending-review queue -- `pending.py`
Doubts accumulate without interrupting the student; only a counter reaches the phone.
- **Deduplication** (in the fold, so a replay always merges the same way):
  `find_duplicate(state, kind, text, refs)` is the first open item of the same `kind` whose text
  is very similar (`text_similarity >= STRONG_SIMILARITY`, 0.85) or whose refs overlap (a shared
  page, segment or source) with a text somewhat similar (`>= OVERLAP_SIMILARITY`, 0.5). Texts
  naming different numbers are never the same doubt; closed items are never merged into.
  `text_similarity` compares without case, accents or punctuation (best of a character ratio and
  a word Jaccard). A merge joins the refs, keeps the first text and records the id.
- **Review file**: `pending_review(state) -> PendingReview` (`format_version`, `open_count`,
  `items`: open ones first) is `review/pending.yaml`, written through
  `vault.write_pending_review` by the live loop after each change and by the loader.
- **Prompt**: the `observer` prompt describes each kind with a Spanish example and asks not to
  re-add an open doubt, and to close one (`resolve_pending`) only when the session settled it.
- **Web**: `GET /api/subjects/{subject_id}/topics/{topic_id}/pending` (server module).
- The topic list's `pending_count` (#147) and the summary's `open_pending` are
  `len(open_pending())` of this same fold, so they count merged doubts once.

### Loader -- `loader.py`
`load_observer_snapshot(vault, subject_slug, topic_slug, *, write_back=True) -> ObserverSnapshot`
reads the events (`read_topic_events`) and the stored snapshot (`read_observer_snapshot`), folds
only the tail after it and writes the result back (`write_observer_snapshot`) when it changed and
`write_back` is true (a read-only caller such as the topic list passes `False`); when the pending
items changed too (from the stored snapshot's, none when it was not usable) it regenerates
`review/pending.yaml` (`write_pending_review`). The stored snapshot is
discarded and the log folded from scratch when it is unreadable, of another `state_version`, or
no longer matches the log (its `event_count`-th event is not its cursor: a session pulled from
another PC with an earlier id, or events appended before the cursor). An op that cannot be
folded raises and nothing is written. The vault purge reads the snapshot it compacts to with
`write_back=False`. Nothing in `studentassistant.observer` opens a file, runs
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
topic digest (the server passes `topic_digest`, below). The server builds one when `create_app` gets an `llm_transport`
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
  Every failed call (any `LLMError` but a reached cap, or an unexpected error of the batch) is
  also published as a transient notice `observer.call_failed` (`CALL_FAILED_EVENT_KIND`,
  `persist=False`; `kind`: `unavailable` (transient or retries exhausted) | `refused` |
  `invalid` (structured output) | `error`, and `reason`, the error's text for logs), which the
  server's per-session health counts (#262, `docs/modules/server.md`). The gateway does not
  forward it.
- **Pending notice**: when a folded event changes the pending items (a new item, a merge, a close
  of any origin), the loop publishes a transient `notice` (`NOTICE_EVENT_KIND`, `persist=False`,
  `payload.pending_count` = the open count; the gateway forwards it to the phone as the protocol
  `notice`) and regenerates `review/pending.yaml`. The count is also published when a session is
  first observed.
- **Ending**: `flush(session_id)` is registered with `SessionService.add_before_ended` (after the
  gateway's STT flush, so it sees the last finals): it waits for the call in flight and sends what
  is still waiting, so its ops land before `session.ended` (bounded by the end hook timeout; a call
  is shielded, never cancelled). `wait_idle(session_id)` waits without sending; `session.ended`
  forgets the session.
- **Catch-up, no batch is lost** (#176, `catchup.py`): every answered batch (valid ops or not,
  after the re-ask) is acknowledged with a persisted `observer.ack` event (`ACK_EVENT_KIND`,
  origin `observer`, after the batch's ops; `payload.through` = the `EventRef` of the newest
  stored event the batch carried; the fold ignores it). When the loop opens a session (the first
  event of a start or resume, or a restart), `unanswered(events, session_id, before)` returns the
  topic's batch-kind events after the newest acknowledgement and before the opening event (which,
  with what follows, comes on the bus; the observer's own ops are never returned). They are sent
  first, as one batch headed `catch-up: N events of session ...`, at once, newest
  `[observer] catch_up_max_items` kept (default 200; the rest are logged). So the last batch of a
  session whose call outlives the end hook's timeout (its ops and ack are refused by the ended
  session) is answered at the start of the topic's next session; the waiting items of a session
  the backend stopped without ending, or of a resumed one, at its resume. The guarantee is at
  least once: a crash between a batch's ops and its ack sends the batch again, and duplicate ids
  are refused and duplicate doubts merged. A topic with no ack yet is not replayed: the first
  open writes a baseline ack (`through` = the newest event before it, `null` for none). The
  `context` record carries the `catch_up` count.
- **Context purge** (#60, `[observer] context_max_tokens`, default 80000, and
  `context_tail_segments`, default 8): the size of the conversation is the last call's prompt
  (uncached, cache-written and cache-read input tokens) plus its output. After an answered batch
  (its ops published and its `observer.ack` written) that leaves it at the threshold or more, the
  conversation is dropped: no tool result is owed any more, and the next call opens a new one
  with the same system blocks (prompt + topic block with the digest) and tool, so the cached
  prefix still hits, then a single user turn: `render_state(..., rolled=True, tail=...)` -- the
  state folded when that call is made (so it holds the ops just published) plus the batch lines
  of the newest `context_tail_segments` answered segments --, and the batch. Batch numbers go on.
  Once that first call is answered, a persisted `observer.context_rolled` event
  (`CONTEXT_ROLLED_EVENT_KIND`, origin `observer`; ignored by the fold and the catch-up) records
  it: `reason: threshold`, `before_tokens`, `after_tokens` (that call's prompt tokens),
  `threshold_tokens`, `dropped_turns`, `tail_segments`. At session end (`flush`, after the last
  batch) the conversation is always dropped the same way: `reason: session_end`,
  `after_tokens: 0`, when there was one. The conversation file keeps every dropped turn as history
  and gets a `context` record (reason `rollover`, `before_tokens`, `event_count`, `cursor`,
  `tail_segments`; or reason `session_end`); nothing ever reads it back into a context. The
  catch-up is unaffected: a batch is only dropped from the conversation after its ack.
- **Purge** (#31): `compactable_snapshot(events)` (`catchup.py`) is the fold of the topic up to
  the newest acknowledged `through` (`acknowledged_through`), and it is all the vault purge may
  compact. The unanswered events and the newest `observer.ack` (always written after what it
  answers) stay after the compaction's cursor as they were. So after a purge `unanswered` still
  returns what is owed, on this PC or any other, and a purged topic is still acknowledged, so no
  baseline ack is written that could skip it. A topic whose acks name no event (none yet, or only
  a `through: null` baseline) is not compacted.

### Topic digest -- `digest.py` (#56)
`state/digest.md` (Spanish Markdown) is how a topic is resumed another day ("continúa el tema")
and an input of the editor. It holds, in this order: the title, a one-paragraph summary (sessions
with content, the last one's date and sections, the open doubt count -- the excerpt), the subject,
`## Índice` (the outline, nested, with each section's segment count), `## Sesiones` (one block per
session: date from the session id -- its UTC start shown in `[observer] digest_timezone` --, `terminada`/`sin terminar`, minutes from the events' `t`; the
sources set, sections worked on -- by the session its segments came from --, new sections still
empty, new concepts, segment and capture counts, doubts added and settled, the last
`NOTES_PER_SESSION` (5) observer remarks, each cut at 240 characters) and `## Dudas abiertas`.
A session the vault purge compacted (#31: its `events.jsonl` emptied) is still described from
the folded state, without the status, length and sources only its events held.
- `render_digest(subject_name, topic_title, events, state, *, timezone=UTC) -> str`: pure and
  deterministic (no clock, no Claude): the same log and zone always give the same text.
  `session_date(session_id, timezone=UTC)` is the `dd/mm/yyyy` of the id's start in that zone (an
  id not of the `YYYYMMDD-HHMMSS` form is shown as it is).
- `observer.digest_timezone` (`SA_OBSERVER__DIGEST_TIMEZONE`, #202): the IANA zone the digest
  dates sessions in (`Europe/Madrid`); unset, the PC's local zone (`TZ`, else `/etc/localtime`,
  else UTC). An unknown name is refused when the settings load. `ObserverSettings.digest_zone()`
  gives the `tzinfo`; the app passes it to `DigestOnEnd`.
- `regenerate_topic_digest(vault, subject_slug, topic_slug, *, timezone=UTC) -> bool`: renders it from the vault
  (`read_topic_events`, `load_observer_snapshot(write_back=False)`) and writes it with
  `vault.write_topic_digest` only when the text changed; returns whether it wrote.
- `DigestOnEnd(lookup, *, timezone=UTC)`: the async end hook the app registers with
  `SessionService.add_before_close` (after the transcript drain, so `session.ended` is in the log
  and the file lands in the end's checkpoint). It runs whether or not the observer uses Claude.
- `topic_digest(vault, subject_slug, topic_slug) -> str | None`: the stored digest, `None` before
  the topic's first session end. The server gives it to `ObserverLoop(digest=...)`, so a new
  session of the topic or a resume puts it in the cached prefix (`render_topic`), and to the
  editor's `generate_notes(digest=...)`.
- `digest_excerpt(text, limit=400) -> str | None`: the summary paragraph, for the web summary
  (`digest_excerpt`), `GET .../digest` and the phone's topic list (`Topic.digest_excerpt`,
  protocol 1.3, #192) (server module).

## Boundaries
- Never writes notes; that is the editor's job.
