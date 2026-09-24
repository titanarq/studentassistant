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
What exists today, after issues #19 and #20: the vault itself, its subjects and its topics, their
sessions with the two append-only logs, their sources, the secret guard, and the helpers all of
them are written with. The layout above is the target, not the state -- see "Not written
yet" at the end of this section for what no code touches.

### The vault -- `vault.py`
`Vault.init(path, student)` creates the directory (parents included), writes `vault.yaml` and
`.gitattributes` and runs `git init -b main`; it refuses a `path` that already exists, so a typo
cannot turn a directory that already holds something into a vault. `Vault.open(path)` reads one
back without writing to it. An open vault is a frozen dataclass of `path` and `meta`. Every refusal
is a `VaultError`: `VaultNotFoundError` (no such directory), `VaultMetaError` (`vault.yaml` missing
or not readable as a `VaultMeta`) and, under that one, `VaultFormatError` (a `format_version` other
than the single one this backend reads and writes, which is what a vault written by a newer backend
looks like).

### Subjects -- `subjects.py`
`create_subject(vault, name, style_guide=None)` writes `subjects/<slug>/subject.yaml`, the slug
being `slugify(name)` with a numeric suffix when another subject already has it; `list_subjects(vault)`
returns every subject sorted by slug; `get_subject(vault, slug)` reads one; `subject_directory(vault, slug)`
is where a subject lives whether or not it exists yet. The three first ones return a `StoredSubject`,
a frozen dataclass of `slug` and `subject`. Refusals are a `SubjectError`: `SubjectNotFoundError`
(no such subject directory) and `SubjectFileError` (its `subject.yaml` missing or not readable as a
`Subject`).

### Topics -- `topics.py`
`create_topic(vault, subject_slug, title)` writes
`subjects/<subject-slug>/topics/<topic-slug>/topic.yaml`, born with `sessions` empty and
`fidelity_mode` at its default; `list_topics(vault, subject_slug)` sorts by slug;
`get_topic(vault, subject_slug, topic_slug)` reads one; `topics_directory(vault, subject_slug)` and
`topic_directory(vault, subject_slug, topic_slug)` give the paths. The three first ones return a
`StoredTopic`, a frozen dataclass of `slug` and `topic`. Refusals are a `TopicError`
(`TopicNotFoundError`, `TopicFileError`), or the subject errors above when the subject the topic is
asked for under is not there or not readable.

### Sessions -- `session_models.py`, `sessions.py`
`start_session(vault, subject_slug, topic_slug, host, protocol_version)` creates
`sessions/<session-id>/` with `session.yaml` (`SessionMeta`: `id` `YYYYMMDD-HHMMSS` in UTC,
`started_at`, `ended_at`, `host`, `protocol_version`), an empty `transcript.jsonl` and an empty
`events.jsonl`, and appends the id to the topic's `sessions` list; a second session started in the
same second takes the next free second. `resume_session(vault, subject_slug, topic_slug)` reopens
the latest listed session whose `ended_at` is unset (`NoOpenSessionError` when there is none);
`end_session(session, ended_at=None)` records `ended_at`, after which the handle refuses appends
(`SessionEndedError`). `sessions_directory(...)` gives the path. The `Session` handle has
`append_event(kind, origin, payload=None, t=None, schema_version=EVENT_SCHEMA_VERSION)` and
`append_transcript(t_start, t_end, text, words=None)`, each returning the line written: the store
assigns each log its own `seq` from 1 with no gaps, continuing from the highest `seq` in the file
on resume, and `t` defaults to the milliseconds since `started_at` (never below the last `t`
written). `read_events()` and `read_transcript()` read the logs back. `Event` is the envelope of
ADR-0003 (`seq`, `t`, `origin` in `phone`/`stt`/`observer`/`editor`/`user`, `kind`,
`schema_version`, `payload`); an unknown `kind`, an older `schema_version` and an extra key are
accepted on read. `TranscriptSegment` is `seq`, `t_start`, `t_end`, `text`, optional `words`
(`TranscriptWord`). Refusals are a `SessionError` (`NoOpenSessionError`, `SessionEndedError`,
`SessionFileError`). This module writes files and never runs git.

### JSONL logs -- `jsonl.py`
`append_jsonl(path, obj)` writes one compact JSON object per line with a single write, flush and
`fsync`, after the secret guard; a torn last line a crash left is truncated away first.
`read_jsonl(path, model)` yields every complete line parsed into `model` and silently leaves out
whatever follows the last newline; a complete line that is not JSON or not the model is a
`JsonlError`. `last_seq(path)` is the highest `seq` among the complete lines (0 for an empty or
missing file), which stays right after a `merge=union` reordered lines.

### Sources -- `sources.py`
`put_source(vault, subject_slug, topic_slug, kind, name, content, meta)` stores bytes or text
under `sources/<kind>/` and a `.yaml` sidecar of `meta` next to it, returning the content's path:
`page-NNN.<ext>` + `page-NNN.yaml` for `notes`, `book` and `pdf` (the extension taken from `name`)
and `NNN-<slug>.md` + `NNN-<slug>.yaml` for `web` (the slug from `name`). The number is one past
the highest already in the directory, derived files included. `sources_directory(...)` gives the
path; `SOURCE_KINDS` lists the kinds. Refusals are a `SourceError` (`UnknownSourceKindError`, or a
paged `name` without extension); nothing of a refused source is left on disk.

### Secret guard -- `secrets.py`
`looks_like_secret(content)` returns the name of the first pattern the text or bytes match
(`anthropic-api-key`, `github-token`, `github-fine-grained-token`, `aws-access-key-id`,
`private-key-block`) or `None`; `guard(content)` raises `SecretRefused` naming the pattern and
never the matched text. Every writer of `files.py` and `jsonl.py` runs it before touching the disk.

### Models, slugs and writers -- `models.py`, `slugs.py`, `files.py`
`models.py` holds `FORMAT_VERSION`, `DEFAULT_FIDELITY_MODE` and the `VaultFileModel` every file
model derives from (`extra="forbid"`: a key this backend does not declare means a newer one wrote
the file), with `VaultMeta`, `Subject` and `Topic`; their fields are declared in the order the
layout above shows them, because that order is what the dump writes. `slugs.py` holds `slugify(name)`
and `unique_slug(base, taken)`. `files.py` is the only way a whole vault file reaches the disk:
`write_text_atomic(path, text)` and `write_bytes_atomic(path, content)` (secret guard, temporary
file in the target's own directory, fsync of file and directory, then rename), `dump_yaml(mapping)`
and `write_yaml_atomic(path, model)` (keys in declaration order, every declared
key written even when its value is `None`, no `---` or `...` marker, and no wrapping at PyYAML's
default 80 columns) and `read_yaml(path, model)`.

`studentassistant.vault` re-exports the vault, subject, topic, session, source, JSONL and
secret-guard names of this section; the YAML models, the slug helpers and the file writers are
imported from their own module.

### Not written yet
As of issue #20 no code reads or writes these parts of the layout:
- **notes** -- `notes/apuntes.md`, and with it the provenance footnotes and the version tags of
  ADR-0005.
- **generated** -- `generated/` and everything the generators put in it.
- Also unwritten: `state/`, `review/pending.yaml`, `conversations/` and `ledger.jsonl`; the git
  cycle of `checkpoint`/`sync` -- commits, push and pull with the bare-repo remote (task
  vault-git-sync); the derived SQLite/FTS5 `VaultIndex` and its rebuild; and the retention `purge`
  described below.

## Purge
`studentassistant purge [--topic] [--dry-run] [--hard]`: retention policy per topic (ADR-0003);
never removes what the current notes cite.

## Boundaries
- Pure storage: no LLM, no HTTP. Refuses files that look like secrets.

## Tests
Against `tmp_vault` fixtures with a local bare repo as "remote"; never GitHub.
