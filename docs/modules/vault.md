# Module: vault

**Lives in:** `backend/src/studentassistant/vault/`. Decision: ADR-0002, ADR-0003.

## Responsibility
The only writer of the vault and the only module that runs git on it.

## Layout (format version 1)

```text
vault.yaml                                   format_version, created_at, student display name
.gitattributes                               *.jsonl merge=union
subjects/<subject-slug>/subject.yaml         name, style_guide (editor preferences)
subjects/<subject-slug>/topics/<topic-slug>/
  topic.yaml                                 title, fidelity_mode, created_at, sessions list
  sources/notes/page-NNN.jpg                 chosen still, downscaled
  sources/notes/page-NNN.page.jpg            cropped/deskewed page
  sources/notes/page-NNN.md                  page transcription (derived)
  sources/notes/page-NNN.yaml                capture_id, session, captured_at, transcript span, source context
  sources/book/…  sources/pdf/…  sources/web/NNN-<slug>.md (+ .yaml: url, fetched_at)
  sessions/<session-id>/session.yaml         started/ended, host, duration, protocol version
  sessions/<session-id>/transcript.jsonl     final segments (seq, t_start, t_end, text, words?)
  sessions/<session-id>/events.jsonl         event log (ADR-0003)
  state/observer-snapshot.json               fold snapshot (optimisation)
  state/digest.md                            topic digest for resuming
  review/pending.yaml                        pending-review items and their resolution
  conversations/observer-<session-id>.jsonl  observer role conversation
  conversations/editor.jsonl                 editor role conversation
  notes/apuntes.md                           master notes (ADR-0005)
  generated/                                 outline.md, quiz.yaml, flashcards.apkg, exam.md, slides.md …
  ledger.jsonl                               LLM usage and cost per call
```

Slugs are lowercase ASCII with hyphens derived from the Spanish name (accents stripped); ids
of sessions are `YYYYMMDD-HHMMSS`.

## Public surface
`Vault.open(path)`, `Vault.init(path)`, subject/topic/session accessors, append helpers for JSONL
files (atomic, fsync), `put_source(...)`, `checkpoint(message)`, `sync()`; `VaultIndex` (SQLite,
FTS5) with `rebuild()`.

## Boundaries
- Pure storage: no LLM, no HTTP. Refuses files that look like secrets.

## Tests
Against `tmp_vault` fixtures with a local bare repo as "remote"; never GitHub.
