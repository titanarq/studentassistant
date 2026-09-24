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
What exists today, after issue #19: the vault itself, its subjects and its topics, and the helpers
those three are written with. The layout above is the target, not the state -- see "Not written
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

### Models, slugs and writers -- `models.py`, `slugs.py`, `files.py`
`models.py` holds `FORMAT_VERSION`, `DEFAULT_FIDELITY_MODE` and the `VaultFileModel` every file
model derives from (`extra="forbid"`: a key this backend does not declare means a newer one wrote
the file), with `VaultMeta`, `Subject` and `Topic`; their fields are declared in the order the
layout above shows them, because that order is what the dump writes. `slugs.py` holds `slugify(name)`
and `unique_slug(base, taken)`. `files.py` is the only way a vault file reaches the disk:
`write_text_atomic(path, text)` (temporary file in the target's own directory, fsync of file and
directory, then rename), `write_yaml_atomic(path, model)` (keys in declaration order, every declared
key written even when its value is `None`, no `---` or `...` marker, and no wrapping at PyYAML's
default 80 columns) and `read_yaml(path, model)`.

`studentassistant.vault` re-exports the vault, subject and topic names above; the models, the slug
helpers and the file writers are imported from their own module.

### Not written yet
As of issue #19 no code reads or writes these parts of the layout:
- **sessions** -- `sessions/<session-id>/` with its `session.yaml`, `transcript.jsonl` and
  `events.jsonl`, and the append helpers the JSONL files need. A topic's `sessions` list stays
  empty because nothing records a session against it (task vault-session-store).
- **sources** -- `sources/notes/`, `sources/book/`, `sources/pdf/` and `sources/web/`: no page
  image, page file or transcription lands under a topic yet.
- **notes** -- `notes/apuntes.md`, and with it the provenance footnotes and the version tags of
  ADR-0005.
- **generated** -- `generated/` and everything the generators put in it.
- Also unwritten: `state/`, `review/pending.yaml`, `conversations/` and `ledger.jsonl`; the git
  cycle of commits, checkpoints, push and pull with the bare-repo remote (task vault-git-sync); the
  derived SQLite/FTS5 index and its rebuild; the writer's refusal of files that look like secrets;
  and the retention purge described below.

## Purge
`studentassistant purge [--topic] [--dry-run] [--hard]`: retention policy per topic (ADR-0003);
never removes what the current notes cite.

## Boundaries
- Pure storage: no LLM, no HTTP. Refuses files that look like secrets.

## Tests
Against `tmp_vault` fixtures with a local bare repo as "remote"; never GitHub.
