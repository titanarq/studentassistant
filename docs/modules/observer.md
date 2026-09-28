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
- Requests to the assistant detected in the raw transcript (#314), and the same classification of
  a message typed in the workspace chat (#327): `assistant.request` events for the editor's chat
  turns (#315) -- edits, questions, "prepárame el tema", incorporating, setting aside or restoring
  pages, and answers to the doubt asked in the chat.

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
is `auto_resolved` from the backend's own origins (`ops.AUTOMATIC_ORIGINS`: `observer` and
`sources`, #360) and `resolved` from anyone else. An `add_pending` that
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
- **Doubts from the editor** (#325): the editor never writes a doubt into the notes; what an
  editor write leaves unresolved (an illegible word, sources that disagree, something missing) is
  an `add_pending` of origin `editor` (`editor.doubts.raise_doubts`, `created_by: editor`), merged
  by this same rule into an open item it duplicates, followed by a `pending.question`; the
  workspace chat asks the open items one at a time (`in_chat` questions). With an unended session
  of the topic these events, and the editor's `resolve_pending` closes, go to **that live
  session** through the server's session bus (so the live loop folds them in `seq` order with the
  session's own events, and the pending count updates), never to a review session, whose events
  would fold before the session's later ones; without one they go to a review session as before.
  Nothing changes in the fold itself.
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
`studentassistant.vault` root), and only `live.py` and `requests.py` import
`studentassistant.llm` (checked by `tests/observer/test_boundaries.py`).

### Live loop -- `live.py`, `context.py`
`ObserverLoop(bus, lookup, *, settings=ObserverSettings(), client_factory=None, digest=..., clock=...)`
subscribes to the session bus (`start()`; `stop()` folds what was delivered and waits for the
calls in flight at most `[observer] stop_timeout_seconds`, default 20, then cancels them -- the
unanswered batch is caught up later, below; `wait_bounded`, #408). `lookup(session_id)` gives an attached session's vault handle
(`SessionBus.attached`); `client_factory(LedgerBinding)` builds the session's `observer` client
(`default_client_factory(settings, transport)`: `get_client("observer", ...)` bound to the
session's ledger, so every call is capped and recorded); `digest(vault, subject, topic)` reads the
topic digest (the server passes `topic_digest`, below). The server builds one when `create_app` gets an `llm_transport`
(`serve` passes the real one) and `[observer] enabled`.

- **Input** (`OBSERVER_KINDS`): `transcript.final`, `capture.stored`, `page.transcribed`
  (`PAGE_TRANSCRIPTION_KIND`: the page transcription, #50, MUST publish it with
  `payload.capture_id` and `payload.text`), `button`, `marker`, `command`, `observer.state_op` of
  any origin but the backend's own (`ops.AUTOMATIC_ORIGINS`: `observer`, and `sources` for an
  illegible page's `add_pending`, #360; shown as `student op`), and the lifecycle events. Each becomes one line
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
  `observer.state_op` (origin `observer`). The tool input is parsed tolerantly
  (`ToolCall.parsed_input()`, #320), so a call with a small JSON defect is applied as is.
  Malformed or inapplicable ops, or no tool call, are re-asked `client.structured_reasks` times
  (once on the API, twice on the claude-code backend, `[llm] structured_reasks`; the errors go
  back as an `is_error` tool result); what is still invalid is logged and dropped. Every `tool_use` gets its `tool_result` at the start of the next user turn.
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
  with what follows, comes on the bus; the ops of `ops.AUTOMATIC_ORIGINS` -- the observer's and
  the sources module's -- are never returned). They are sent first, as one batch headed
  `catch-up: N events of session ...`, at once, newest
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

### Requests to the assistant -- `requests.py`, `assistant_request.py` (#314, #327)
During a session Sonnet (role `observer`) reads the raw transcript and detects when the student is
addressing the assistant and what they want (epic #311). `RequestDetector(bus, lookup, *,
settings, client_factory=None, clock=SystemClock())` (`start()`/`stop()`/`flush(session_id)`/
`wait_idle`/`status`, plus `sources_lookup=None`) lives in `studentassistant.observer.requests`
(it imports the llm module, so it is not re-exported, like `MessageClassifier`); the event model
and the context models are re-exported by `studentassistant.observer`.

- **Mode** (`[observer] request_detection`, `SA_OBSERVER__REQUEST_DETECTION`): `observer`
  (default: Sonnet, below), `wake_word` (the deterministic "anel" of `stt`, #318, no Claude call;
  see **Wake-word mode**) or `off` (`start()` is a no-op, nothing is produced). The server builds it
  next to the observer loop (an `llm_transport`, `[observer] enabled` and `request_detection` not
  `off`), starts/stops it in the lifespan and registers `flush` with `add_before_ended` after the
  observer loop's; in `wake_word` mode it first registers the voice-command detector's `drain`, so
  a wake word in the last final is still a request.
- **Wake-word mode** (#318): the detector subscribes to `voice.command`, `transcript.final` and
  `session.ended` only and never calls Claude. Each `voice.command` `assistant_request` (the stt
  grammar's wake word "anel" and its spellings; the word lives only in the grammar file) becomes
  a persisted `assistant.request` at once: origin `stt`, `detector: "wake_word"`, `kind`
  `prepare_notes` when the query contains "prepárame el tema" (compared with `stt.commands.normalise`,
  whole words), else `edit`; `text` = the query; `summary` = the query on one line cut to 140
  characters at a word boundary; `segment_ids` = `[the command's segment]`; `t_start_ms` /
  `t_end_ms` from that segment's `transcript.final` (the command's `t` when it was not seen).
  Numbering continues the session's `req-<n>` (a resumed session's earlier requests counted); a
  segment already part of a request, or an empty query, produces nothing. The Sonnet mode ignores
  `assistant_request` commands (it does not read `voice.command`). Known limitation: a request is
  one final segment (see `stt.md`).
- **Input**: `transcript.final` (`segment_id`, `text`, `session_start_ms`, `session_end_ms`) and
  the lifecycle events. It keeps the newest `request_window_segments` finals (default 12) of each
  session and which of them it has examined; a final repeating an id, or empty, is ignored.
- **Trigger**, independent of the batch loop: `request_debounce_seconds` (default 1.5) with no
  new final, or `request_max_wait_seconds` (default 8) after the first final not examined yet,
  whenever at least one final is unexamined. One call per session is in flight; finals that arrive
  meanwhile coalesce into the next call (sent at once when its deadline already passed). `clock`
  gives `now()` (records), `monotonic()` and `sleep()` (the triggers), so tests drive it.
- **Context** (#327): each call first awaits `sources_lookup(subject, topic) -> RequestContext`
  (`SourcesLookup`, injected by the server: the editor's `source_status` and the doubt the chat is
  asking, so observer never imports editor; none or a failing one gives an empty context). It is
  rendered at the top of the user turn -- uncached, after the stable prefix -- as the topic's
  sources, one line each (`<source id>: página N (apuntes) -- pendiente | incorporada | apartada:
  <motivo>`, `RequestSource.line`), and the doubt asked now (`AskedDoubtRef`: `pending_id`,
  question, numbered suggestions) or `none`.
- **Call**: self-contained (no growing conversation): system = the `observer_requests` prompt +
  the topic block (`render_topic`, no digest), the cached prefix with the tool; one user turn = the
  context, then the window, one line per final `<id> [<start>s-<end>s] <mark> <text>`, `<mark>`
  being `new`, `seen` or the `req-N` it already belongs to. The strict tool `report_requests`
  (`ReportRequests`: `{requests: [{kind, summary, segment_ids, targets, pending_id, answer}]}`,
  `tool_choice: auto`); `kind` is `edit`, `question`, `prepare_notes`, `incorporate`,
  `set_aside`, `restore`, `doubt_answer` or `study` («ya está, quiero estudiar», «vamos a estudiar
  esto», «pasa a estudiar»: end the capture and switch the topic to Estudiar, #335; talking about
  studying as content, a plan for later or asking for one material is not `study`). Sonnet resolves «la página 3», «las dos últimas», «la
  que está borrosa» to `targets` from the sources list; a reference it cannot resolve is reported
  as a `question` (the editor asks back); a comment about the app itself, not the notes or the
  topic («apunta una mejora: que se pueda …», «esto es un bug: …», «la aplicación debería …»), is
  a `question` too (#472): the editor records it in the vault's feedback inbox with its
  `report_feedback` tool (`docs/modules/editor.md`) and never changes the notes, and a feedback
  turn that was classified `edit` anyway is not re-asked for `apply_edits`; «la segunda», «pone "escrita"» while a doubt is asked are
  a `doubt_answer` (`answer`: the suggestion's number as digits, or the words). Plain dictation
  must yield an empty list. A request is refused when its `summary` is empty or over 140
  characters, or its `segment_ids` are empty, outside the window, repeated, not consecutive in
  window order, or already part of a request (earlier or in the same answer); a target kind
  without `targets` or with one not in the sources list, a `set_aside`/`restore` of a source that
  is not a captured page (`notes`, `book`), and a `doubt_answer` without an asked doubt, for
  another doubt or with an empty answer are refused too (targets are de-duplicated, fields of
  other kinds dropped; an empty `pending_id` of a `doubt_answer` is the asked one). Valid ones are
  published at once; the refused ones are re-asked `client.structured_reasks` times (`[llm]
  structured_reasks`: once on the API, twice on the claude-code backend by default; the turns so
  far, the answer, then an `is_error` tool result with the reasons), and what is still invalid is
  logged and dropped. Once answered, the finals that were `new` are examined, whatever the answer.
  `flush` sends the last window with a line saying the session is ending.
- **Output**: one persisted `assistant.request` event (`ASSISTANT_REQUEST_KIND`, origin
  `observer`) per request, payload `AssistantRequest` (frozen, `extra="forbid"`):
  `request_id` (`req-<n>`, the session's n-th spoken request), `kind` (`RequestKind`,
  `REQUEST_KINDS`), `summary` (Spanish, <= 140 characters), `text` (the span's finals joined with
  a space), `segment_ids`, `t_start_ms`/`t_end_ms` (the span's session times), `detector`
  (`observer`; the wake word, #318, writes `wake_word` with origin `stt`; a typed message
  `typed`), and by kind `targets` (topic-relative source ids; required for `incorporate`,
  `set_aside`, `restore`, `TARGET_KINDS`) or `pending_id` + `answer` (required for
  `doubt_answer`); a typed request also has `message_id` (`msg-<hex>`), id `req-t<n>` and no
  segments. `payload()` dumps it without defaults, so an event written before #327 reads and
  dumps back unchanged. Consumers (#315) validate it with `AssistantRequest`. The fold ignores
  the kind. When a session is first seen (a start, a resume, a restart) the session's earlier
  `assistant.request` events are read back, so numbering goes on (typed `req-t<n>` ids do not
  count) and their segments stay assigned.
- **Restart catch-up** (#408): when a session is first seen, the detector also rebuilds its window
  from the session's `transcript.final` events: every final up to the newest one examined before
  -- one shown in an answered call's window (the `user` record's `detail.examined`, or for a
  record written before #408 the segment ids of its window lines) or part of a request -- is
  examined, the later ones are not; the newest `request_window_segments` are kept and, when some
  are unexamined, the trigger examines them as if they had just arrived (once: after that call
  they are examined, and their segments assigned to any request found). The `context` record then
  has `unexamined` (the count). `stop()` waits for the calls in flight at most `[observer]
  stop_timeout_seconds` (default 20), then cancels them (their finals stay unexamined and are
  caught up when the session is opened again).
- **Cost caps**: calls are bound to the session's ledger (`default_client_factory`). A reached cap
  keeps the finals unexamined and publishes one `observer.status` (`status: paused`, `reason`,
  `cap`, `limit_usd`, `total_usd`, `detector: "requests"`); the next new final tries again and a
  successful call publishes `status: running` (`detector: "requests"`). Any other Claude failure
  does the same with `status: error` plus an `observer.call_failed` notice (`detector:
  "requests"`). A failed call is never retried until a new final arrives.
- **Conversation file**: `conversations/observer-requests-<session-id>.jsonl`, the live loop's
  record shapes: a `context` record when the session is first seen (`reason` `start`/`resume`,
  `requests` read back, `window_segments`, `unexamined` when the catch-up found some), each
  `user` turn (the first one of a call with `detail.examined`: the window's segment ids) and
  `assistant` answer (model, prompt hash, usage) and `status` changes (with `detector`).
- **Typed messages** (#327): `MessageClassifier(client_factory, *, sources_lookup=None,
  clock=None).classify(vault, subject, topic, text, *, session_id=None, selected=()) ->
  list[ReportedRequest]`
  classifies one message typed in the workspace chat with the same prompt, tool and checks: the
  user turn is the context plus the message as the single segment `m1`, which every request of
  it shares (a typed message holds at least one request, usually one). Re-asks follow
  `structured_reasks`; valid requests of a partly refused answer are kept. It raises
  `ClassificationError` when Claude fails or no valid answer comes; the server then keeps the
  raw text as a request. Calls are bound to the topic's ledger (its live session when given) and
  recorded in `conversations/observer-messages.jsonl` (`user`, `assistant`). It works whatever
  `request_detection` says (that switch is about speech). The wake word (#318) keeps producing
  `edit`/`prepare_notes` only.
- **The Recursos selection** (#433): `selected` is what the student had selected in Recursos
  when typing (topic-relative ids, a PDF page as `<pdf>#page=K`). `RequestContext.selected`
  renders it after the doubt as `Selected by the student now (in Recursos), in order:` with one
  source line each (a PDF page as `<id>#page=K: page K of <the PDF's line>`), or `... : nothing.`;
  it is `None` for the spoken detector, which renders no such block (unchanged). The
  `observer_requests` prompt rule: a deictic or unspecified referent of a typed message («esto»,
  «esta», «estas», «esta captura», «estas páginas», «el texto», «incorpóralas», «apártalas») means
  the selection, and the `targets` of an `incorporate`/`set_aside`/`restore` are then exactly the
  selected sources; a source the message names («la página 3») wins over the selection; needing
  a referent with nothing selected and nothing named is a `question` whose summary asks for it
  («¿De qué páginas hablas? Selecciónalas en Recursos o dime su número.»). Checks: targets name
  stored sources (a `#page=K` is dropped, repeats removed) and stay among the topic's sources; a
  selected source that is not a captured page passes the `set_aside`/`restore` check (the server's
  turn says it cannot be set aside), an unselected one is still refused. The prompt's version is
  its content hash (ADR-0004), so the edited prompt is a new version. The eval set scores spoken
  request detection only (kinds and segments), so it has no typed-selection case.

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
