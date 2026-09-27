# ADR-0003: Sessions are event-sourced

Status: accepted (2026-09-24)

## Context
"The state of the LLMs" must be persisted and restorable. LLMs keep no state between calls; the
state is whatever we feed them. It must also be auditable ("why did you put this?").

## Decision
- A session belongs to exactly one topic of one subject; the models' context for a session is
  that topic only (its digest, snapshot and events), never other topics.
- A study session is an **append-only event log** (`events.jsonl`) plus the transcript
  (`transcript.jsonl`). Event kinds include: session lifecycle, transcript segment final,
  capture stored, voice/button command, source context change, marker (important), observer
  state ops, pending item added/resolved, page transcription stored, capture triaged
  (`capture.triaged`), assistant request detected from the transcript or typed in the chat
  (`assistant.request`, origin `observer`, `stt` for the wake word, `user` when typed; kinds
  include `study`), notes edited by the student (`notes.edited`, origin `user`).
- Every event has a monotonically increasing `seq`, a session-relative timestamp, its origin
  (`phone`, `stt`, `observer`, `editor`, `sources`, `user`) and a schema version. `sources` is
  what the sources module produces on its own (page transcription, capture triage); a student's
  own decision about a source (set aside, restore) stays `user`.
- The observer's knowledge state (outline, section assignment of segments, concepts, capture <->
  segment links, source context, pending items) is a **pure fold** of the events: the observer
  emits *state ops*, never a mutable blob. Snapshots in `state/` are an optimisation, always
  reproducible from the log.
- The web's workspace stream (`notes.changed`, `study.marked` `{version, tag}`, chat turn events)
  is a live view for the browser, not part of the log.
- Each LLM role's conversation is persisted as JSONL in the vault (`conversations/`), with model
  id, prompt file hash and usage per call, so a role can be resumed on another PC.

- **Purge.** (a) Context: the observer's live conversation is rolled over to snapshot + digest +
  a short tail when it passes a token threshold and at every session end (the state is the fold,
  so nothing is lost). (b) Storage: `studentassistant purge` applies a per-topic retention
  policy (burst originals, rolled-over conversations, events folded into a snapshot, old
  generated artifacts), never removing what the current notes cite; soft purge is a normal
  commit, hard purge (history rewrite) is explicit.

## Consequences
- Replaying a session through a new observer version is possible (and is how the observer is
  tested).
- Event schemas are part of the contract and versioned; readers accept older versions.
