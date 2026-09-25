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
  sources/pdf/page-NNN.pdf                   imported PDF (only the kept page range)
  sources/pdf/page-NNN.pKKK.txt|.jpg         page K's extracted text and thumbnail (derived)
  sources/pdf/page-NNN.yaml                  original_name, original_sha256, original_page_count, first_page, last_page, page_count
  sources/book/…  sources/web/NNN-<slug>.md (+ .yaml: url, fetched_at)
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
What exists today, after issues #19, #20, #21, #22 and #135: the vault itself, its subjects and its topics,
their sessions with the two append-only logs, their sources, the secret guard, the helpers all of
them are written with, the read-only functions the web read API uses, the git sync that commits, pushes and pulls them, and the `setup` that
creates or clones the vault from GitHub. The layout above is the target, not the state -- see "Not written
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
`read_session_transcript(vault, subject_slug, topic_slug, session_id)` returns the
`TranscriptSegment`s of one listed session, open or ended, sorted by `seq` (a torn last line left
out), reading the files directly: no `Session` handle, nothing written. An id that is not a
session id or that the topic does not list is a `SessionNotFoundError`; a missing
`transcript.jsonl` is a `SessionFileError`.

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
`put_source(vault, subject_slug, topic_slug, kind, name, content, meta, derived=None)` stores
bytes or text under `sources/<kind>/` and a `.yaml` sidecar of `meta` next to it, returning the
content's path: `page-NNN.<ext>` + `page-NNN.yaml` for `notes`, `book` and `pdf` (the extension
taken from `name`) and `NNN-<slug>.md` + `NNN-<slug>.yaml` for `web` (the slug from `name`).
`derived` (paged kinds only) maps suffixes to files written in the same call as
`page-NNN.<suffix>` -- a suffix is dot-separated lowercase letters and digits with at least one
dot, e.g. `p003.txt`, `p003.jpg` for a PDF's page 3 -- all guarded before anything is written and
removed again if any write fails; they are never listed as sources. The number is one past
the highest already in the directory, derived files included. `sources_directory(...)` gives the
path; `SOURCE_KINDS` lists the kinds and `SourceKind` is their `Literal` type. Refusals are a `SourceError` (`UnknownSourceKindError`, or a
paged `name` without extension); nothing of a refused source is left on disk.
`list_sources(vault, subject_slug, topic_slug)` returns a `StoredSource` (`kind`, `path` -- the
content's vault-relative POSIX path --, `meta` -- the parsed sidecar, or `None`) per stored source,
ordered by kind (`SOURCE_KINDS` order) then number; sidecars, derived files (`page-NNN.md` beside
another `page-NNN.<ext>`, `page-NNN.page.jpg`) and symlinks are not listed, and a topic without
`sources/` lists as empty. `read_source(vault, vault_relative_path)` returns a `SourceContent`
(`content` bytes, `meta` of the page's sidecar or `None`, `media_type` guessed from the extension,
`application/octet-stream` when unknown). The path is taken literally (never URL-decoded) and must
be `subjects/<slug>/topics/<slug>/sources/<kind>/<file>`: absolute, `..`/`.`/empty segments,
backslashes, NUL, anything outside a topic's `sources/<kind>/`, or a file that resolves (symlinks
followed) anywhere but that directory is a `SourcePathError`; a well-formed path with no file is a
`SourceNotFoundError`; a sidecar that is not a YAML mapping is a `SourceFileError` (all three are
`SourceError`s). A symlinked sidecar is never followed. Neither function writes or runs git.

### Notes and generated material -- `notes.py`
`read_notes(vault, subject_slug, topic_slug)` returns the text of `notes/apuntes.md`, or `None`
when it has not been written yet (a symlink or non-UTF-8 file is a `NotesError`, a `VaultError`).
`list_generated(vault, subject_slug, topic_slug)` returns the sorted vault-relative POSIX paths of
every file under `generated/`, subdirectories included and symlinks skipped; an empty list when
the directory does not exist. `notes_path(...)` and `generated_directory(...)` give the paths.
Nothing here writes or runs git; writing notes and generated material belongs to the editor and
generators tasks.

### Reading with ids from outside
Every reader above (`list_sources`, `read_session_transcript`, `read_notes`, `list_generated`)
goes through `require_topic(vault, subject_slug, topic_slug)` (`topics.py`): a value that is not a
slug (`slugs.is_slug`: `[a-z0-9]` runs joined by single hyphens) is a `SubjectNotFoundError` or a
`TopicNotFoundError` without touching the disk, so an id from a URL cannot walk out of its topic.

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
Only these two modules, the setup and the purge below run git on the vault.
`GitRunner(root, identity, timeout, environment=None)` runs `git` as a
subprocess in the vault root with a `GitIdentity(name, email)` as author and committer (passed in
the environment, so no git configuration decides it), never prompts (`GIT_TERMINAL_PROMPT=0`,
SSH `BatchMode`), is killed after `timeout`, and returns a `GitResult` (`ok`, `describe()`) whose
output went through `redact` (URL userinfo and the secret-guard patterns become `***`);
`check(...)` raises `GitCommandError` instead. `environment` adds variables to every command: it
is how a GitHub credential reaches git (see "GitHub and setup"), never a URL or `.git/config`.

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
- `create_notes_tag(subject_slug, topic_slug, message=None)` commits what is pending and puts the
  annotated tag `<subject-slug>/<topic-slug>/apuntes-vN` on HEAD, N one past the highest existing
  for that subject and topic (`notes_tag_name`), pushed by the next push;
  `list_notes_tags(subject_slug, topic_slug)` returns the `NotesTag`s (`name`, `version`,
  `commit`) oldest first. A non-slug subject or topic raises `ValueError`. Tags are keyed by
  subject as well as topic because topic slugs are unique only within a subject (#143): two
  subjects' `introduccion` topics keep separate version sequences. The earlier topic-only form
  `<topic-slug>/apuntes-vN` is not read and needs no migration: no writer created notes tags
  before this format (the editor, which will, is not written yet), so no vault holds one. ADR-0002
  still shows the topic-only form.
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

### GitHub and setup -- `github.py`, `setup.py`
`studentassistant setup` gets a fresh PC to a working vault (ADR-0002: install -> setup -> clone
-> index rebuild). GitHub is reached only through subprocesses (`gh`, `git`), never HTTP.

`GitHubHost` (protocol, `github.py`): `name`, `authenticated()`, `remote_url(repo)` (the
`https://github.com/<owner>/<name>.git` git uses; no credential in it), `repo_exists(repo)`,
`repo_is_private(repo)` (`None` when the host cannot tell), `create_private_repo(repo)` and `git_environment()` (the variables a git child process needs to
authenticate). Two implementations, both taking a `remote_base` (default `GITHUB_URL`; tests point
it at `file://` bare repositories):
- `GhCliHost(gh="gh", remote_base, timeout)` -- `gh auth status` decides `authenticated`;
  `gh api repos/<repo>` answers `repo_exists` (a 404 is `False`); `gh repo view <repo> --json
  visibility` answers `repo_is_private`; `gh repo create <repo> --private`
  creates; git borrows `gh`'s credential through `gh auth git-credential` as credential helper,
  given as `GIT_CONFIG_*` environment variables (other helpers reset first), so nothing is written
  to any git configuration.
- `TokenHost(token, remote_base, timeout)` -- a fine-grained token from `GH_TOKEN` or
  `GITHUB_TOKEN` (`TOKEN_ENV_VARS`, first non-empty wins, `token_from_environment()`). The token
  reaches git only as the `STUDENTASSISTANT_GIT_TOKEN` (`GIT_TOKEN_ENV_VAR`) variable of the child
  process, which an environment-given credential helper reads: never in a remote URL,
  `.git/config`, the vault, `config.toml` or a message; `repr()` omits it. `repo_exists` is a
  `git ls-remote`; `repo_is_private` is always `None`. `create_private_repo` always refuses with `NO_GH_CREATE_MESSAGE` (install and
  log in to `gh`, or create an empty private repository by hand and re-run: creating is a REST call
  and this module has no HTTP client).

`select_host(environ=None, gh="gh", remote_base=GITHUB_URL)` returns the `GhCliHost` when `gh auth
status` succeeds, otherwise a `TokenHost` when a token is set, otherwise raises `GitHubHostError`
(`NO_CREDENTIALS_MESSAGE`). Every `gh`/`git` output is passed through `redact` before it reaches a
`GitHubHostError` message; messages are Spanish, shown to the student as they are.

`setup.py` (`repo` is always `owner/name`, checked by `config.check_repo_name`):
- `create_vault(path, repo, student, host, author_email=..., timeout=..., warn=<no-op>)` -- refuses
  a non-empty `path` (an empty directory is accepted) and a repository that exists with commits;
  creates it private when it does not exist; an existing empty one is used once `repo_is_private`
  says so -- a public one is refused, and when the host cannot tell (token, no `gh`) `warn` gets
  `NOT_KNOWN_PRIVATE_WARNING` (Spanish: confirm on GitHub it is private; the CLI prints it); then `Vault.init(path,
  student)`, commits the first files (`vault creado`), adds `origin`, pushes `main` and verifies
  push access. Everything GitHub could refuse is checked before anything is written locally.
- `clone_vault(path, repo, host, post_clone=<no-op>, author_email=..., timeout=...)` -- refuses a
  non-empty `path` and a repository that does not exist; clones; `Vault.open` checks the format (a
  `VaultFormatError` becomes a Spanish `SetupError` and the clone stays in place); verifies push
  access; then calls `post_clone(vault)` once (where the index rebuild will plug in).
- Idempotence: a git repository at `path` whose `origin` is `host.remote_url(repo)` (a trailing
  `.git` or `/` ignored) is accepted as already set up -- only push access is checked again (and a
  create whose first push never happened is pushed); `post_clone` is not called.
- `verify_push_access(runner)` -- `git push --dry-run origin main`; failing it is a `SetupError`.
- `check_remote_access(path, host, repo=None, author_email=..., timeout=...) -> RemoteAccess`
  -- for `studentassistant doctor`: the (redacted) `origin` URL, `repo_matches` (`None` without
  `repo` or `host`) and `push_error` (`None` when the dry-run push succeeded, else the Spanish
  reason). `host=None` runs git with no extra credentials. No repository or no `origin` is a
  `SetupError`. Writes nothing.
- Both return a `SetupResult` (`vault`, `repo`, `action`: `created`, `cloned` or
  `already-set-up`); every refusal is a `SetupError` (a `VaultError`) with a Spanish message, or
  the host's `GitHubHostError`.

The command (`studentassistant.cli`): `studentassistant setup [--vault-repo owner/name] [--path
PATH] [--create|--clone] [--student NAME]` asks in Spanish for whatever is missing (create or
clone, repository, local path defaulting to `vault.path`, and for a new vault the student's name,
defaulting to the repository owner, which is also what it takes unattended), runs the flow with
`select_host()` and then `config.write_vault_config(vault_path, repo)`. That writer merges
`vault.path` (absolute) and `vault.repo` into the TOML at `config_toml_path()` (`SA_CONFIG` or
`~/.config/studentassistant/config.toml`), keeping every other key and comment and the file's permission bits
(a new file is `0600`, `NEW_CONFIG_MODE`), writing nothing else, and leaving the file untouched when it already holds those values: re-running `setup` with
the same answers changes nothing and exits 0. `vault.repo` (`VaultSettings.repo`, optional,
`SA_VAULT__REPO`) is the `owner/name` of the vault's GitHub repository.

`studentassistant.vault` re-exports the vault, subject, topic, session, topic-state, source, JSONL,
ledger, notes, git sync and secret-guard names of this section; the YAML models, the slug helpers, the
file writers, `redact`, `summarize_changes` and the GitHub and setup names are imported from their
own module (`studentassistant.vault.github`, `studentassistant.vault.setup`).

### Not written yet
As of issues #21, #117, #119 and #135 no code reads or writes these parts of the layout:
- **notes** -- writing `notes/apuntes.md`, and with it the provenance footnotes of ADR-0005
  (reading exists: `read_notes`; its version tags exist: `create_notes_tag`).
- **generated** -- writing `generated/` and everything the generators put in it (listing exists:
  `list_generated`).
- Also unwritten: `state/digest.md`, `review/pending.yaml` and `conversations/`; the
  derived SQLite/FTS5 `VaultIndex` and its rebuild; and the retention `purge` described below.

## Purge -- `purge.py`
`studentassistant purge [--topic <subject>/<topic>] [--dry-run] [--hard] [--yes]` applies a
per-topic retention policy (ADR-0003, issue #31) so the vault does not grow forever. Names are
imported from `studentassistant.vault.purge`.

**Policy** -- `VaultPurgeSettings` (`studentassistant.config`, `[vault.purge]` /
`SA_VAULT__PURGE__*`):

| key | default | candidate |
|---|---|---|
| `require_notes_tag` | `true` | (eligibility) the topic has a notes tag `<subject-slug>/<topic-slug>/apuntes-vN` |
| `burst_originals` | `true` | `sources/notes/page-NNN.burst<K>.<ext>`: the other stills of a burst |
| `observer_conversations` | `true` | `conversations/observer-<session-id>.jsonl` of an ended session |
| `folded_events` | `true` | the events the observer snapshot folded, replaced by that snapshot |
| `generated_max_age_days` | unset (keep) | files under `generated/` last committed longer ago |

Burst originals are a naming convention for capture processing: it stores the stills it did not
keep as derived files `burst<K>.<ext>` of the page (`put_source(..., derived=...)`); the page
itself (`page-NNN.<ext>`), its sidecar, its crop and its transcription are never candidates.

**Never removed**: transcripts, `session.yaml`, `notes/` and its history (tags), sources other
than burst originals, the editor conversation, and anything `notes/apuntes.md` names by a
topic-relative path (`cited_paths`: any `sources/...`, `sessions/...`, `generated/...`,
`conversations/...` it contains, footnote or not) -- a cited candidate is listed as protected.
A topic is skipped (with the reason) while a session is open, when it has no notes, and, with
`require_notes_tag`, before its notes are accepted.

**Folded events**: `Compaction(session_id, seq, kind, payload, origin="observer")`, built by the
caller from the observer's snapshot (the vault never imports the observer), replaces every event
of the topic up to and including `(session_id, seq)`: sessions before it keep an empty
`events.jsonl`, and the cursor's session starts with one `kind` event at that `seq` (its `t` the
replaced event's), followed by its later events byte for byte. The fold of snapshot + remaining
events equals the pre-purge state (`tests/observer/test_compaction.py`); purging again changes
nothing. A compaction naming a session the topic does not list, or a `seq` its log lacks, is a
`PurgeError`. The events replaced are then only in git history: a later observer version cannot
refold them from the working tree.

**API**: `plan_topic_purge(sync, subject_slug, topic_slug, policy=None, compaction=None,
now=None) -> TopicPurgePlan` (`items`: `PurgeItem(path, reason, size_before, size_after)` --
`size_after` `None` for a removal --, `protected`, `skipped`, `saved_bytes`) reads only.
`apply_purge(sync, plans, hard=False, before_commit=None) -> PurgeResult` removes and rewrites
the files (the secret guard runs on every rewrite before anything is touched), calls
`before_commit(plan)` (the CLI refreshes the observer snapshot there) and commits everything as
one commit `purga: N archivos borrados, M registros compactados (size)`
(`PURGE_COMMIT_PREFIX`); no items, no commit. That is the soft purge: all of it is recoverable
from git history.

**`--hard`** (confirmed by typing `reescribir`, or `--yes`): after the soft commit,
`rewrite_history(sync, paths)` drops from every commit of `main` and from every tag
(`git filter-branch --index-filter ... --tag-name-filter cat`) every path a purge commit ever
deleted in the planned topics (`purged_history_paths`, so earlier soft purges are reclaimed too),
deletes `refs/original/`, force-pushes `main` with `--force-with-lease` against the
remote-tracking branch and then every local tag with `--force-with-lease=refs/tags/<t>:<sha>`,
`<sha>` being what `git ls-remote --tags` gave before the rewrite (empty: the tag must still be
absent), so a tag another PC created or moved meanwhile makes the push fail instead of being
overwritten; a tag only the remote has is left as it is. Then it expires the reflog and runs
`gc --prune=now`; `HistoryRewrite` reports the object store size before and after. The CLI first
commits pending changes and `sync()`s with the remote, refusing to rewrite when that fails; a
remote that still moved on makes the lease refuse the push, which is a `PurgeError` (the local
history is rewritten by then; the message says how to push it). Rewritten `events.jsonl` keep
their older versions in history (only deleted files are rewritten out).

Consequence for other PCs: their clones hold the old history. Each must be cloned again
(`studentassistant setup --clone` into an empty directory) or, when it has nothing unpushed,
reset with `git fetch origin && git reset --hard origin/main`; a pull or push from an old clone
would bring the purged files back. GitHub may keep the old objects reachable by SHA until its own
garbage collection. Stop the backend while purging: the purge runs its own `GitSync`.

The CLI prints each topic's items (`se borra` / `se compacta`, reason, size), protected paths and
skip reasons; `--dry-run` adds the total it would free (and, with `--hard`, how many paths it
would drop from history) and changes nothing. A soft purge is pushed at once when the vault has
its remote (a failed push is retried by the next sync).

## Boundaries
- Pure storage: no LLM, no HTTP. Refuses files that look like secrets.

## Tests
Against `tmp_vault` fixtures with a local bare repo as "remote"; never GitHub. Setup tests use
`tests/github_fakes.py`: a `LocalHost` over bare repositories under `tmp_path`, a fake `gh` script
put first on `PATH`, `file://` remote bases, and `SA_CONFIG` inside `tmp_path`.
