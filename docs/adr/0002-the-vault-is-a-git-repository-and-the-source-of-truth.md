# ADR-0002: The vault is a git repository and the source of truth

Status: accepted (2026-09-24)

## Context
The product's output (notes, the conversation history, the sources they came from and the state
the LLM roles need to continue) must survive the PC and be restorable on another PC by
installing the app and configuring GitHub.

## Decision
- All content lives in **the vault**: a git repository separate from this code repository,
  hosted as a **private** GitHub repository the student owns. Default local path
  `~/StudentAssistant/vault`, configurable.
- Layout (normative details in `docs/modules/vault.md`):
  `vault.yaml` (format version) / `subjects/<subject>/subject.yaml` /
  `subjects/<subject>/topics/<topic>/` with `topic.yaml`, `sources/{notes,book,pdf,web}/`,
  `sessions/<session-id>/{session.yaml,transcript.jsonl,events.jsonl}`, `notes/apuntes.md`,
  `review/pending.yaml`, `conversations/*.jsonl`, `state/` (observer snapshots, topic digest),
  `generated/`, `ledger.jsonl` (LLM usage and cost).
- Only `studentassistant.vault` writes the vault or runs git on it.
- Commits are batched (debounced) with meaningful messages and forced at checkpoints (capture
  stored, session end, each editor change). Push is debounced and retried; a failed push never
  loses a local commit. Pull (`--rebase`) happens at startup and at session start.
- `*.jsonl` files are append-only and merged with `merge=union` (`.gitattributes`).
- Notes versions are git tags (`<topic-slug>/apuntes-vN`); no `v1/v2` copies of files.
- The SQLite database (search, listings) is a **derived cache** under `~/.cache/studentassistant/`,
  rebuildable from the vault at any time (`studentassistant index rebuild`). It is never the only
  copy of anything.
- Secrets (API keys, tokens) never enter the vault; the vault writer refuses files matching
  secret patterns.
- Images are stored in plain git, downscaled (long edge <= 2400 px, JPEG q85) plus the cropped
  page; audio is NOT stored in the vault by default (transcript only). Git LFS for images/audio is
  an opt-in to be decided once the vault size is measured.
- One active writer at a time (one student, possibly several PCs): the session start pulls and
  records the active host; divergence that union merge cannot resolve is surfaced to the user,
  never auto-resolved by discarding.

## Consequences
- A new PC is: install app -> `studentassistant setup` -> clone vault -> index rebuild.
- The vault is browsable on GitHub itself: notes render and their provenance footnotes link to
  the source images (ADR-0005).
