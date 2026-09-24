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
What exists today, after issues #19, #20 and #21: the vault itself, its subjects and its topics,
their sessions with the two append-only logs, their sources, the secret guard, the helpers all of
them are written with, and the git sync that commits, pushes and pulls them. The layout above is the target, not the state -- see "Not written
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
`list_sessions(vault, subject_slug, topic_slug)` returns the `SessionMeta` of every session the
topic lists, open or ended, ordered by id, and writes nothing; `read_topic_events(vault,
subject_slug, topic_slug)` yields `(session_id, Event)` for every session of the topic, sessions
in id order and events sorted by `seq` within each (a `merge=union` may have reordered the lines).
A missing topic is a `TopicNotFoundError`; a listed session whose `events.jsonl` is missing is a
`SessionFileError`.

### Topic state -- `state.py`
`write_observer_snapshot(vault, subject_slug, topic_slug, snapshot)` writes any Pydantic model as
indented JSON (`files.write_json_atomic`: declaration order, final newline, atomic, secret guard)
to `state/observer-snapshot.json`, creating `state/`, and returns the path;
`read_observer_snapshot(vault, subject_slug, topic_slug, model)` validates it into `model`, or
returns `None` when none was written. The vault never imports the observer: the caller passes
the model. An unreadable snapshot (not UTF-8, not JSON, not the model) is a `SnapshotFileError`
(a `StateError`); a missing topic is a `TopicNotFoundError`. `observer_snapshot_path(...)` and
`state_directory(...)` (imported from `state.py`) give the paths.

### JSONL logs -- `jsonl.py`
`append_jsonl(path, obj)` writes one compact JSON object per line with a single write, flush and
`fsync`, after the secret guard; a torn last line a crash left is truncated away first.
`read_jsonl(path, model)` yields every complete line parsed into `model` and silently leaves out
whatever follows the last newline; a complete line that is not JSON or not the model is a
`JsonlError`. `last_seq(path)` is the highest `seq` among the complete lines (0 for an empty or
missing file), which stays right after a `merge=union` reordered lines.

### Ledger -- `ledger.py`
`LedgerEntry` is one line of a topic's `ledger.jsonl`: `time` (timezone-aware, kept in UTC),
`role`, `model`, `prompt_hash?`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `estimated_usd?` (`None` when the model has no price), `subject`, `topic`,
`session?`. `append_ledger_entry(vault, subject_slug, topic_slug, entry)` appends it through
`append_jsonl` (secret guard included), creating the file on first use; an unknown subject or topic
is the usual `SubjectNotFoundError`/`TopicNotFoundError`, and an entry whose `subject`/`topic` are
not the slugs it is written under is a `LedgerError`. `read_ledger(vault, subject_slug, topic_slug)`
returns the entries in file order (empty without a file, a torn last line ignored);
`read_all_ledgers(vault)` yields every entry of every topic, subjects and topics by slug;
`ledger_path(...)` gives the path. Pricing and caps are the llm module's; this module never
imports it and runs no git.

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
file in the target's own directory, fsync of file and directory, then rename), `dump_yaml(mapping)`,
`write_json_atomic(path, model)` (indented JSON of any Pydantic model, final newline)
and `write_yaml_atomic(path, model)` (keys in declaration order, every declared
key written even when its value is `None`, no `---` or `...` marker, and no wrapping at PyYAML's
default 80 columns) and `read_yaml(path, model)`.

### Git sync -- `git.py`, `sync.py`
Only these two modules run git on the vault. `GitRunner(root, identity, timeout)` runs `git` as a
subprocess in the vault root with a `GitIdentity(name, email)` as author and committer (passed in
the environment, so no git configuration decides it), never prompts (`GIT_TERMINAL_PROMPT=0`,
SSH `BatchMode`), is killed after `timeout`, and returns a `GitResult` (`ok`, `describe()`) whose
output went through `redact` (URL userinfo and the secret-guard patterns become `***`);
`check(...)` raises `GitCommandError` instead.

`GitSync(vault, settings=None, clock=None)` drives one vault; `settings` is a `VaultGitSettings`
(`studentassistant.config`, `[vault.git]` / `SA_VAULT__GIT__*`), `clock` any object with
`monotonic()` (`SystemClock` by default; tests inject a manual one). It never sleeps and starts no
thread:
- `note_change()` -- called after a vault write; cheap, runs no git. The batch is committed by
  `run_due()` once nothing changed for `commit_quiet_seconds` (default 30) or at the latest
  `commit_max_delay_seconds` (default 300) after its first change, with an aggregated Spanish
  subject (`sesión 20260924-183000: 12 segmentos, 2 capturas`; `summarize_changes` builds it from
  the staged diff: transcript lines are segments, event lines events, new `sources/notes/*.yaml`
  captures, other new source sidecars `fuentes`; otherwise `N archivos cambiados`).
- `checkpoint(message)` -- commits every pending change now, `message` as subject and the summary
  as body; returns the commit or `None` (nothing to commit: never an empty commit; or a failure,
  kept in `status().last_error`). Never raises.
- Push: a commit schedules a push `push_debounce_seconds` (default 120) after the first unpushed
  commit (later commits do not postpone it). `run_due()` pushes when due; `push_now()` pushes at
  once; `flush()` commits what is pending and pushes (session end, shutdown). `git push
  --follow-tags <remote> main`. A failure (`offline`, `auth`, `rejected`, `error`) is recorded as a
  `PushFailure` and retried after `push_backoff_initial_seconds` (default 15), doubling per
  consecutive failure up to `push_backoff_max_seconds` (default 900). Never raised; local commits
  are never touched by a failed push. A `rejected` push (the remote moved on) keeps being retried
  and keeps failing until a `sync()` rebases the local commits.
- `sync()` -- commits what is pending, then `git pull --rebase <remote> main` (nothing to do when
  the remote has no `main` yet). `*.jsonl` conflicts resolve by `merge=union`; any other conflict
  aborts the rebase, leaving HEAD, the local commits and the working tree as they were, and returns
  a `SyncResult` with `outcome="conflict"` and the `conflicts` paths (ADR-0002: surfaced, never
  auto-resolved). Other outcomes: `ok`, `offline`, `auth`, `error`. After a successful sync with
  local commits ahead, a push is scheduled at once. Never raises. Meant for backend start and
  session start (wiring owned by `server`), before capture writes start.
- `create_notes_tag(topic_slug, message=None)` commits what is pending and puts the annotated tag
  `<topic-slug>/apuntes-vN` on HEAD, N one past the highest existing (`notes_tag_name`), pushed by
  the next push; `list_notes_tags(topic_slug)` returns the `NotesTag`s (`name`, `version`,
  `commit`) oldest first. A non-slug raises `ValueError`.
- `status()` -- a `SyncStatus` snapshot that runs no git: `pending_changes`, `last_commit`,
  `last_commit_at`, `pending_commits` (ahead of the remote), `last_push_at`, `last_push_failure`,
  `consecutive_push_failures`, `next_push_due` (clock time), `last_sync`, `last_error`.
- `run(interval=1.0)` -- the asyncio loop: `run_due()` in a worker thread every `interval`, until
  cancelled. Every other method blocks on git; async callers use `asyncio.to_thread`.

Config keys (`[vault.git]`): `author_name` (default: the `student` of `vault.yaml`),
`author_email` (default `estudiante@studentassistant.invalid`), `remote` (`origin`),
`commit_quiet_seconds`, `commit_max_delay_seconds`, `push_debounce_seconds`,
`push_backoff_initial_seconds`, `push_backoff_max_seconds`, `timeout_seconds` (120, per git
command).

`studentassistant.vault` re-exports the vault, subject, topic, session, topic-state, source, JSONL,
ledger, git sync and secret-guard names of this section; the YAML models, the slug helpers, the
file writers, `redact` and `summarize_changes` are imported from their own module.

### Not written yet
As of issues #21, #117 and #119 no code reads or writes these parts of the layout:
- **notes** -- `notes/apuntes.md`, and with it the provenance footnotes of ADR-0005 (its version
  tags exist: `create_notes_tag`).
- **generated** -- `generated/` and everything the generators put in it.
- Also unwritten: `state/digest.md`, `review/pending.yaml` and `conversations/`; the
  derived SQLite/FTS5 `VaultIndex` and its rebuild; and the retention `purge` described below.

## Purge
`studentassistant purge [--topic] [--dry-run] [--hard]`: retention policy per topic (ADR-0003);
never removes what the current notes cite.

## Boundaries
- Pure storage: no LLM, no HTTP. Refuses files that look like secrets.

## Tests
Against `tmp_vault` fixtures with a local bare repo as "remote"; never GitHub.
