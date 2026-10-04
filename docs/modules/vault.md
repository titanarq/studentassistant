# Module: vault

**Lives in:** `backend/src/studentassistant/vault/`. Decision: ADR-0002, ADR-0003.

## Responsibility
The only writer of the vault and the only module that runs git on it.

## Layout (format version 2)

```text
vault.yaml                                   format_version (2), created_at, student display name, legacy_root_user?
.gitattributes                               *.jsonl merge=union, .sa/active.yaml merge=sa-active
.sa/active.yaml                              active-host record: host, user, session, claimed/released
users/<user-id>/profile.json                 id, name, email, photo, created_at (users.py, #546)
users/<user-id>/photo.jpg                    the profile photo: a downscaled JPEG, quality 85 (optional)
users/<user-id>/subjects/                    that student's content: the subjects/ tree below, inside it
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
  sources/book/page-NNN.*                    textbook pages, as notes pages; the sidecar adds book_page (+ _from, _printed, _spoken)
  sources/book/book.yaml                     the topic's textbook: title (never listed as a source)
  sources/web/NNN-<slug>.md (+ .yaml: url, fetched_at)
  sources/images/img-NNN.png|jpg|webp        an image the student pasted into the notes (+ .yaml: origin: pasted, content_type, sha256, added_at),
                                             or a region cropped from a stored page (origin: cropped, + cropped_from, bbox, requested_region)
  sources/images/img-NNN.svg                 a diagram the editor drew, sanitized (+ .yaml: origin: drawn, title, content_type, sha256, added_at)
  sessions/<session-id>/session.yaml         started/ended, host, protocol version, kind
  sessions/<session-id>/transcript.jsonl     final segments (seq, t_start, t_end, text, words?)
  sessions/<session-id>/events.jsonl         event log (ADR-0003)
  state/observer-snapshot.json               fold snapshot (optimisation)
  state/digest.md                            topic digest for resuming
  review/pending.yaml                        pending-review items and their resolution
  conversations/observer-<session-id>.jsonl  observer role conversation
  conversations/editor.jsonl                 editor role conversation
  conversations/web-search.jsonl             web searches: queued, results, failed, kept (sources, #59)
  notes/apuntes.md                           master notes (ADR-0005)
  notes/borrador.md                          a generation that failed the validator (editor)
  generated/                                 outline.md, quiz.yaml, flashcards.apkg, exam.md, slides.md …
  study/quiz-results.jsonl                   every quiz attempt, graded (generators, #75)
  study/version.yaml                         the "versión de estudio" label + history (editor, #335)
  ledger.jsonl                               LLM usage and cost per call
feedback/inbox.jsonl                         app bugs/improvements reported in a chat (editor, #472); one for the
                                             whole vault, each item's context naming the user who reported it
```

Everything from `subjects/` down is written where the handle the writer was given points ("The
vault -- `vault.py`" below), and since format version 2 (#548, epic #544) the only handle with
content of its own to write is a user's: the `subjects/...` lines above are the tree as a handle
holds it, so each one lives at `users/<user-id>/` + that path, with the same inner layout and the
same vault-relative ids, now relative to that folder. What the repository root keeps is what is
the repository's -- `vault.yaml`, `.gitattributes`, `.sa/`, the locks and `feedback/inbox.jsonl` --
and it holds nobody's subjects. `create_user` (`users.py`, #546) is what makes a user's folder,
with its `profile.json` and an empty `subjects/` that survives a clone, and `Vault.init` calls it
for the first one, so a vault is never born with nowhere to put a subject.

`FORMAT_VERSION` is 2 and `LEGACY_FORMAT_VERSION` is the 1 this backend still reads -- reads only
so that it can be migrated. A format-1 vault holds its content at the repository root, where it is
nobody's; `Vault.open` refuses one with a Spanish message naming
`studentassistant vault migrate-users`, and `Vault.open_for_migration` is the single way in, for
the migration that moves the root's `subjects/` into its first user's folder in one commit
("Migration to users" below). A vault of any other version is a `VaultFormatError`, which is what
one written by a newer backend looks like. The two directions do not forgive each other: a format-2
backend refuses a vault an older installation left behind until that command has run, and an older
backend refuses a migrated one.

Slugs are lowercase ASCII with hyphens derived from the Spanish name (accents stripped); ids
of sessions are `YYYYMMDD-HHMMSS`. A user's id is `slugify(name)` with the first free numeric
suffix when another user already has it (the rule a subject's slug follows), and never changes when
the name is edited, because every subject, session and notes tag under it is named after the path
it lives at.

## Public surface
What exists today, after issues #19, #20, #21, #22, #135, #23, #546, #547 and #548: the vault
itself and the two handles on it (the repository root and one user's folder), its users and their
profiles, the migration that gives a format-1 vault's root content to its first user,
its subjects and its topics, their sessions with the two append-only logs, their sources, the
secret guard, the helpers all of them are written with, the read-only functions the web read API
uses, the git sync that commits, pushes and pulls them -- one per repository, with a view per
user -- and the `setup` that creates or clones the
vault from GitHub, and the derived SQLite index, one database per user. The layout above is the target, not the state -- see "Not written
yet" at the end of this section for what no code touches.

### The vault -- `vault.py`
`Vault.init(path, student, *, email=None)` creates the directory (parents included), writes
`vault.yaml` and `.gitattributes`, runs `git init -b main` and creates the vault's FIRST user
(`create_user(vault, student, email)`, #548), because format 2 leaves nowhere else to put content:
a vault born without a user would be one whose own student could not write a subject into it until
somebody added them to it. `student` is the display name `vault.yaml` records -- the one the web UI
and the editor call the student by -- and the name that first user is created with, their id derived
from it the way a subject's slug is; `email` (`None` until the student gives one) goes into their
profile. The handle returned is still the root one, the repository's, from which `for_user` narrows
to that first user's folder. A `student` or an `email` no profile accepts is a `UserProfileError`
(Spanish, for whoever typed the name) and leaves a vault there with no user in it, which
`studentassistant users add` (#549) can still put right; `init` refuses a `path` that already
exists, so a typo cannot turn a directory that already holds something into a vault.

`Vault.open(path)` reads one back without writing to it, and refuses a format-1 vault, whose
content is at the root and is nobody's: this backend reads that layout only in order to migrate it.
The refusal is a `VaultNeedsMigrationError` -- a `VaultFormatError`, so whatever catches the format
errors catches it -- whose Spanish message names the vault's path and
`studentassistant vault migrate-users` (`MIGRATE_USERS_COMMAND`), because that message is what a
student reads when the backend refuses the vault an older installation left them, and a refusal
that did not say how to fix it would look like their notes were lost.
`Vault.open_for_migration(path)` is the one way to open a format-1 vault: the handle `open` would
return -- the same root, the same read-only opening, nothing written -- except that its `meta` says
format 1, which is what lets the migration read the `student` its first user is named after and
know that the content to move is the root's own. Nothing else may open one this way, and it refuses
a vault of any other version, both because there is no root content of theirs to move and because
writing `vault.yaml` back through a handle that says format 1 would undo a migration. An open vault
is a frozen dataclass of `path`, `meta`, `root` and `user_id`. Every refusal is a `VaultError`:
`VaultNotFoundError` (no such directory), `VaultMetaError` (`vault.yaml` missing or not readable as
a `VaultMeta`) and, under that one, `VaultFormatError` (a `format_version` other than the two this
backend reads -- 2, which it writes, and 1, which it migrates -- which is what a vault written by a
newer backend looks like).

**Two handles on one vault** (#546, ADR-0002, epic #544). A handle knows two directories: `root`,
the git repository (`.git`, `vault.yaml`, `.gitattributes`, `.sa/`, the locks), and `path`, where
content is written. `Vault.init` and `Vault.open` return a ROOT handle -- `root == path` and
`user_id is None` -- and `vault.for_user(user_id)` returns a USER handle: a frozen copy with the
same `root` and the same `meta`, `path == root / "users" / user_id` and that `user_id`. Every reader
and writer of this package builds its paths from `vault.path`, so the handle it is given is what
decides whose content it touches, and the vault-relative ids it returns
(`subjects/<s>/topics/<t>/sources/...`) stay relative to it: nothing else about them changes, and
the ids the web and the phone already use mean the same thing inside a user's folder. `for_user`
writes nothing and reads nothing but the check that `users/<user-id>/profile.json` is there -- no
profile, no user. It raises `UserNotFoundError` for a missing profile and for an id that is not a
slug, the second without touching the disk, and `ValueError` when called on a user handle, since a
user's folder holds no `users/` of its own. `UserNotFoundError` and its parent `UserError` are
declared in `errors.py`, because `vault.py` raises them and so does `users.py`, which imports it.

**What the repository owns** (#547). Git, the cross-process locks, `.sa/active.yaml` and the
feedback inbox are the repository's rather than one student's, so every module here that reads or
writes one of them does it at `vault.root`, whichever of the two handles it was given: git runs in
`root` and every pathspec it is handed carries the `users/<user-id>/` prefix when the content is a
user's; a lock taken through a user handle is the same `VaultLock`, and the same file under
`<root>/.git/studentassistant-locks/`, as one taken through the root (`repository_root`); the
active-host record and the `.gitattributes` line that gives it its merge driver are written at
`root`; and so is `feedback/inbox.jsonl`. What a handle still decides is what is *somebody's*: the
paths and the notes version tags a sync translates for one user (`GitSync.for_user`), the one index
database per user (`user_index_path`), the per-user breakdown of the size report (`VaultStats.users`)
and the topic a purge plans and rewrites inside. All of it keeps the behaviour it had on a root
handle of a vault with no `users/` folder -- which since #548 is a vault awaiting its migration
(the one `open_for_migration` opens) or one whose users somebody removed by hand, and is no longer
every vault: the unprefixed notes tags, the single index database at the configured path and an
empty `users` list are that case, not a special one. A format-2 vault's root handle has nothing of
anybody's at the root to read, so the index, the size report and the purge reach a student's
content through a user's handle or a view of the sync.

### Users -- `users.py` (#546)
The users of the vault: one folder each under `users/`, and the profile that says who they are.
Names are imported from `studentassistant.vault.users` and re-exported by `studentassistant.vault`.

`UserProfile` (a `VaultFileModel`, so a key this backend does not declare means a newer one wrote
the file) is the whole of `users/<id>/profile.json`: `id` (the folder's name, which an edit never
changes), `name`, `email` and `photo`, both `None` until the student gives one, and `created_at`.
`photo` is the name of the file beside the profile (`photo.jpg`), never a path or a URL.

- `create_user(vault, name, email=None)` adds somebody to the vault, and is the root handle's alone
  -- on a user handle it is a `ValueError`, because adding somebody is a decision about the whole
  repository. The id is `slugify(name)` with the first free numeric suffix, so two students called
  "Ana García" are two folders and neither one's notes are written over the other's. It writes
  `users/<id>/profile.json` with `write_json_atomic` and `users/<id>/subjects/.gitkeep`: git tracks
  no empty directory, so a profile on its own would arrive from a clone as a folder with nowhere to
  put a first subject. A refusal writes nothing and creates no folder, and a `FileExistsError` says
  the id picked is taken already -- the same user created twice at once -- without overwriting it.
- `prospective_user(vault, name, email=None)` is the user `create_user` would add -- the same id
  rule, the same validation, the same profile -- with nothing written and no folder created, for a
  caller that has to say what adding somebody would do instead of keeping a second copy of the rule
  that could drift from it: the migration's `--dry-run` names the user the root's content would
  become (#548). `created_at` is this moment and is the one field the later `create_user` does not
  keep, a profile recording when the user was added and not when a dry run guessed at them. It
  refuses a user handle (`ValueError`) and a name or an email no profile accepts
  (`UserProfileError`) as `create_user` does.
- `list_users(vault)` reads every profile and sorts by `name` case-insensitively and then by id, so
  a listing never depends on the order the file system gives. An accented vowel therefore sorts by
  its code point, after every plain letter: this backend has no collation table, and a screen that
  wants one re-sorts what it is given. A vault with no user yet has no `users/` directory either,
  and lists as empty.
- `get_user(vault, user_id)` reads one profile. `update_user(vault, user_id, *, name=None,
  email=None)` edits the name and the email, keeping the id, the photo and `created_at`: a field
  left at `None` is kept as it is, an `email` of `""` (or of nothing but whitespace) clears it,
  which is how the profile screen says "no address", and a profile that comes out the same is not
  rewritten. The user is looked up first, so an id that is not there is `UserNotFoundError` whatever
  comes with it.
- `set_user_photo`, `remove_user_photo` and `read_user_photo` are the photo, below.
- `user_ids(vault)` lists the sorted directory names under `users/` reading no `profile.json`,
  a broken one included: it is still a user somebody may recover, and a new one taking its id would
  write over them. A caller that must survive a profile it cannot read -- the selection screen, the
  migration -- lists these and reads each one itself.

**Validation.** A `name` is trimmed and between 1 and `MAX_USER_NAME_CHARACTERS` (80) characters;
an `email` is trimmed, at most `MAX_USER_EMAIL_CHARACTERS` (254, the longest address the RFCs
allow) and `^[^@\s]+@[^@\s]+\.[^@\s]+$`. Anything else is a `UserProfileError` -- a `UserError` and
a `ValueError` -- with a Spanish message, because that message is what the «Editar perfil» screen
shows the student (#553, #555), and nothing is written. `UserNotFoundError` is an id with no
readable `profile.json`, or one that is not a slug at all; `UserFileError` is a `profile.json` that
is there and cannot be read back as a `UserProfile`, which says the vault's own content is damaged
and is the student's to recover, not a wrong id to retry. All three are `UserError`s, and a
`UserError` is a `VaultError`.

**The photo.** `set_user_photo(vault, user_id, content, content_type)` takes the bytes a client
uploaded with the media type it declares: one of `USER_PHOTO_CONTENT_TYPES` (`image/jpeg`,
`image/png`, `image/webp`, the three `sources.py` accepts for a pasted image), at most
`MAX_USER_PHOTO_BYTES` (5 MiB) and decodable by OpenCV, else a `UserProfileError`. What is stored is
not what arrived: the image is shrunk so its long edge is at most `USER_PHOTO_LONG_EDGE_PX` (512)
-- averaged with `INTER_AREA`, never enlarged, so a smaller one keeps its size -- and re-encoded as
a JPEG of `USER_PHOTO_JPEG_QUALITY` (85) through the secret guard and `write_bytes_atomic`. One
format under one name, in the user's own folder, is what keeps `profile.photo` a file name, and
what turns a 12-megapixel phone selfie into a few tens of kilobytes in a repository every PC clones
and pushes. The image is written before the profile that names it, so a write that fails halfway
leaves a profile with no photo rather than a photo no profile mentions, and a client that follows
the profile never asks for a file that is not there. `remove_user_photo(vault, user_id)` deletes the
file and sets `photo` to `None`; removing what is not there is not an error, so a repeated «Quitar
foto» gets the same answer instead of a `404` nobody can act on. `read_user_photo(vault, user_id)`
gives the stored bytes or `None` -- `None` for a profile that names no photo, for one that names a
file this module never writes (so a hand-edited profile cannot point a route at another path in the
vault), and for one whose photo the folder has lost. Decoding, shrinking and encoding are CPU-bound
and the atomic writers fsync, so the photo calls block in the way the capture processing does: a
server caller runs them in a worker thread (`asyncio.to_thread`).

This module builds its paths from `vault.root`, not from `vault.path`: users belong to the
repository, not to one user's content, so a user handle lists and edits profiles exactly as a root
one does. Nothing here runs git; the sync commits what these writers leave behind.

### Migration to users -- `migrate.py` (#548)
The move that turns a format-1 vault into a format-2 one whose first user owns everything the root
held, in one commit, losing nothing (epic #544, "Migration"; ADR-0002). Names are imported from
`studentassistant.vault.migrate`, which the CLI imports directly: nothing here is re-exported by
`studentassistant.vault`.

`migrate_to_users(vault, sync, *, name=None, email=None, dry_run=False) -> MigrationReport`. `vault`
is the root handle `Vault.open_for_migration` gives -- the only one that opens a format-1 vault --
and `sync` the repository's own `GitSync`, every step of a migration being the whole repository's:
a user handle, or a user's view of the sync, is a `MigrationError`. `name` and `email` are the first
user's, and `name` defaults to `vault.yaml`'s `student`, the content at the root being that
student's since it is the name they gave `setup --create`. One run does this, in this order:

1. `sync.sync()`, which commits whatever is pending and rebases onto the remote: a migration moves
   the whole of a student's content at once, so it starts from a vault every PC of it agrees on. A
   `conflict` refuses and names the paths, because moving both sides of a divergence under a user
   would leave the student to settle it inside a folder neither PC wrote; an `offline` remote does
   not refuse -- what a migration does is local, and the push it ends with is retried by the next
   sync -- but the report says which of the two happened.
2. It refuses while any session of any topic is unended, and names them: a capture still writing a
   transcript would write it where the directory it is writing into just went. The sync of step 1
   has by then committed what that capture had written, which is what a sync does anyway and loses
   nothing; what has not happened is any part of the move.
3. `create_user` makes the first user and their `profile.json` with it.
4. `git mv` of every entry of the root `subjects/` into `users/<id>/subjects/`: a rename git records
   as one, never a copy and a delete, which is what keeps `git log --follow` of a moved
   `notes/apuntes.md` reaching the commits it had before, and what keeps moving a vault of
   photographs as cheap as moving one file. An entry git tracks nothing in cannot be moved and is
   not: it stays on disk at the root and is named in `left_behind`, so nothing a student had there
   is quietly left out of their folder. The root `subjects/` the moves emptied is then removed,
   format 2 keeping none; one that is not empty stays, and so does one this process may not write
   to, neither worth failing a migration that has moved everything else.
5. `vault.yaml` is rewritten as format 2 naming `legacy_root_user: <id>`, the field `sync.py` reads
   to know whose the notes versions tagged before the move were ("Git sync" below).
6. ONE commit, `migración: contenido al usuario <id>` (`migration_commit_subject(user_id)`, whose
   `MIGRATION_COMMIT_PREFIX` a `git log --grep` finds as `purge.PURGE_COMMIT_PREFIX` does for a
   purge's), carries the moves, the profile and `vault.yaml` together: a vault is either migrated or
   it is not, and a reader that opened it between two commits would find content that is nobody's.
7. Every notes tag `<s>/<t>/apuntes-vN` is re-created as the annotated tag
   `<id>/<s>/<t>/apuntes-vN` on that commit, with the same version number and the same message, the
   old tag kept where it was (`sync.retag_notes`): a study label names the tag it was made from, and
   a version a student read by its tag keeps reading.
8. `push_now()`, so the other PCs of the vault get the move and its tags. A push that fails undoes
   nothing; the report carries the `PushFailure` and the next sync retries it.

`MigrationReport` (frozen) says what the run did: `dry_run`, `migrated` (whether a migration commit
was made -- `False` in a dry run and in a vault that was already format 2), `user_id` and
`user_name`, `subjects`, `moved_files` (the repository-relative paths the move took, named as they
were before it, `subjects/...`), `tags` (`RetaggedNotes` each: `subject`, `topic`, `version`,
`old_name`, `new_name`, `message`, `commit`, `created`), `commit`, `sync_outcome` and
`sync_message`, `pushed` and `push_failure`, `left_behind`, and `reason`, which is Spanish and is
what a run with nothing to do says instead of a report of moves.

`dry_run=True` answers what a run would do and changes nothing: no `sync()` (which commits), no
user, no move, no commit, no tag -- the user it names comes from `prospective_user`, the plan from
the vault as this PC holds it, so the one thing a dry run cannot see is what the remote has and this
PC has not pulled. A vault that is already format 2 is not an error either: the run does nothing and
`reason` says so, which is what makes a second `migrate-users` -- the one a student who did not read
the first output runs -- harmless.

Refusals are two kinds. `MigrationRefusedError` (a `MigrationError`, a `VaultError`) is a vault this
backend will not migrate in the state it is in -- the sync came back with a conflict, a session of
some topic is unended, another process holds the repository's git lock, or the user's folder is
there already -- and its Spanish message is the one a caller shows as it is, because what it says is
for the student whose vault it is (a conflict to settle, a capture to end, a backend to stop) and
not for whoever reads a log: no migration was made, the vault is still format 1 with its content at
the root, and the only thing a run that was not a dry one has done by then is the commit of what was
pending, which the sync it starts with makes and which loses nothing. `MigrationError` is the rest:
a handle or a sync that is not the repository's, or a migration commit that did not happen -- which
leaves the moves and `vault.yaml` on disk, in the layout that file then names, so the vault still
reads and the next sync commits what the migration staged; what it will not have is the re-created
tags. A `name` or an `email` no profile accepts is a `UserProfileError` and writes nothing; a
session a topic lists and whose files do not read is the usual `SubjectError`, `TopicError` or
`SessionFileError`, a vault to fix before moving all of it; a git that fails a step or stays busy
past the sync's timeout is a `GitCommandError`.

Nothing here rebuilds the derived index and nothing here reopens the vault: the handle a migration
is given keeps the `meta` it was opened with, so a caller that goes on using the vault opens it
again, and the CLI rebuilds the indexes after a migration that worked (one database per user since
#547). Stop the backend first, which is what the command's own help says:
`studentassistant vault migrate-users [--name NAME] [--email EMAIL] [--dry-run]` on the configured
vault prints in Spanish the user the root's content became, the subjects and how many files moved,
the commit and its subject, every notes version re-created (old name, new name, and a name that was
taken already), what the sync before it and the push after it had to say, and any entry left behind
at the root; then it rebuilds the per-user indexes, and a refusal exits 1 with its Spanish reason.
An unmigrated vault is refused by every other command that opens one the way the backend does --
`cost`, `import-pdf`, `index rebuild`, `vault stats`, `feedback` and `purge` among them, all through
`Vault.open` -- with the message above, and by the server itself, whose `SessionService` opens the
vault the same way: `migrate-users` is the only command that goes through `open_for_migration`, and
`doctor` the only one that turns the refusal into an `aviso` naming the command instead of a failure
(`install/doctor.py`, `docs/modules/infra.md`).
The procedure for the PC's real vault -- stop the service, a dry run, the run, `vault stats` after
it, start the service again -- is `docs/runbooks/operations.md`.

### Subjects -- `subjects.py`
`create_subject(vault, name, style_guide=None)` writes `subjects/<slug>/subject.yaml`, the slug
being `slugify(name)` with a numeric suffix when another subject already has it; `list_subjects(vault)`
returns every subject sorted by slug; `get_subject(vault, slug)` reads one; `subject_directory(vault, slug)`
is where a subject lives whether or not it exists yet. The three first ones return a `StoredSubject`,
a frozen dataclass of `slug` and `subject`. Refusals are a `SubjectError`: `SubjectNotFoundError`
(no such subject directory) and `SubjectFileError` (its `subject.yaml` missing or not readable as a
`Subject`).
`set_style_guide(vault, slug, style_guide)` replaces the subject's `style_guide` (blank or `None`
clears it), keeping the other fields; the editor's revision loop appends the student's general
preferences with it.
`subject_slugs(vault)` lists the sorted slugs of the directories under `subjects/` without reading
any `subject.yaml` (a half-created subject is listed; no `subjects/` directory lists as empty), for a
caller that reads each subject itself and must survive one it cannot read (the sources catch-up).

### Topics -- `topics.py`
`create_topic(vault, subject_slug, title)` writes
`subjects/<subject-slug>/topics/<topic-slug>/topic.yaml`, born with `sessions` empty and
`fidelity_mode` at its default; `list_topics(vault, subject_slug)` sorts by slug;
`get_topic(vault, subject_slug, topic_slug)` reads one; `topics_directory(vault, subject_slug)` and
`topic_directory(vault, subject_slug, topic_slug)` give the paths. The three first ones return a
`StoredTopic`, a frozen dataclass of `slug` and `topic`.
`set_fidelity_mode(vault, subject_slug, topic_slug, mode)` records `estricto` or `ampliado`
(anything else `ValueError`) in `topic.yaml`, keeping the other fields. Refusals are a `TopicError`
(`TopicNotFoundError`, `TopicFileError`), or the subject errors above when the subject the topic is
asked for under is not there or not readable.
`topic_slugs(vault, subject_slug)` lists the sorted slugs of the directories under the subject's
`topics/` reading no YAML, neither `subject.yaml` nor any `topic.yaml` (a half-created topic is
listed; no `topics/` directory lists as empty).

### Sessions -- `session_models.py`, `sessions.py`
`start_session(vault, subject_slug, topic_slug, host, protocol_version)` creates
`sessions/<session-id>/` with `session.yaml` (`SessionMeta`: `id` `YYYYMMDD-HHMMSS` in UTC,
`started_at`, `ended_at`, `host`, `protocol_version`, `kind`: `study`, the default and what a file
without the key reads as, or `review` for a session the backend opens and ends at once only to hold
events written outside a study session, such as a doubt's resolution; `SessionMeta.is_study`;
`start_session(..., kind="review")` writes it), an empty `transcript.jsonl` and an empty
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
`write_pending_review(vault, subject_slug, topic_slug, review)` writes the observer's pending
queue model as deterministic YAML (`dump_yaml` of `model_dump(mode="json")`, atomic, secret guard)
to `review/pending.yaml`, creating `review/`, and returns the path; `pending_review_path(...)`
gives it. The observer regenerates it from its fold after every change (#55); the index reads it.
`write_topic_digest(vault, subject_slug, topic_slug, text)` writes the observer's topic digest
(Markdown text, atomic, secret guard) to `state/digest.md`, creating `state/`, and returns the
path; `read_topic_digest(...)` returns it, or `None` when none was written (an unreadable file is
a `DigestFileError`, a `StateError`); `topic_digest_path(...)` gives it (#56).

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
`cache_write_tokens`, `estimated_usd?` (`None` when the model has no price), `billing?`
(`subscription` for a call on the user's Claude plan through Claude Code, whose `estimated_usd` is
the cost the CLI reported; `None` for the API), `subject`, `topic`,
`session?`. `append_ledger_entry(vault, subject_slug, topic_slug, entry)` appends it through
`append_jsonl` (secret guard included), creating the file on first use; an unknown subject or topic
is the usual `SubjectNotFoundError`/`TopicNotFoundError`, and an entry whose `subject`/`topic` are
not the slugs it is written under is a `LedgerError`. `read_ledger(vault, subject_slug, topic_slug)`
returns the entries in file order (empty without a file, a torn last line ignored);
`read_all_ledgers(vault)` yields every entry of every topic, subjects and topics by slug;
`ledger_path(...)` gives the path. Pricing and caps are the llm module's; this module never
imports it and runs no git.

### Conversations -- `conversations.py`
An LLM role's conversation (ADR-0003) is `conversations/<name>.jsonl` under its topic, `name` being
lowercase letters, digits and hyphens (`observer-<session-id>`, `editor`, `web-search`: the
topic's web search log, `search.*` records with a `detail`, written by `sources.web`); anything else is a
`ConversationError`. `ConversationRecord` is one line: `time` (timezone-aware, kept in UTC), `kind`
(the role's choice; the observer writes `context`, `user`, `assistant`, `status`), `message?` (the
API message it carries), `model?`, `prompt_hash?`, `usage?` (token counts) and `detail?`.
`append_conversation_record(vault, subject_slug, topic_slug, name, record)` appends it through
`append_jsonl` (secret guard included), creating `conversations/` on first use;
`read_conversation(...)` returns the records in file order (empty without a file);
`conversation_path(...)` and `conversations_directory(...)` give the paths. An unknown subject or
topic is the usual `SubjectNotFoundError`/`TopicNotFoundError`. The vault knows nothing about
Claude: it stores what the role hands it.

### Sources -- `sources.py`
`put_source(vault, subject_slug, topic_slug, kind, name, content, meta, derived=None)` stores
bytes or text under `sources/<kind>/` and a `.yaml` sidecar of `meta` next to it, returning the
content's path: `page-NNN.<ext>` + `page-NNN.yaml` for `notes`, `book` and `pdf` (the extension
taken from `name`), `NNN-<slug>.md` + `NNN-<slug>.yaml` for `web` (the slug from `name`) and
`img-NNN.<ext>` + `img-NNN.yaml` for `images` (the extension from `name`: `.png`, `.jpg` --
`.jpeg` is stored as `.jpg` -- or `.webp`, anything else a `SourceError`).
`derived` (paged kinds only) maps suffixes to files written in the same call as
`page-NNN.<suffix>` -- a suffix is dot-separated lowercase letters and digits with at least one
dot, e.g. `p003.txt`, `p003.jpg` for a PDF's page 3 -- all guarded before anything is written and
removed again if any write fails; they are never listed as sources. The number is one past
the highest already in the directory, derived files included. Numbering is atomic per
`sources/<kind>/` directory across every thread and every process on the vault: choosing the
number and writing the content, derived files and sidecar under it happen under that directory's
vault lock (see "Cross-process locks" below), so concurrent writers into one topic (a capture
stored while a PDF upload runs, or the CLI's `import-pdf` while the server runs) always get
distinct numbers and never overwrite each other. A writer waits at most
`SOURCE_LOCK_TIMEOUT_SECONDS` (120) and then raises `VaultBusyError` with nothing written.
`sources_directory(...)` gives the
path; `SOURCE_KINDS` lists the kinds and `SourceKind` is their `Literal` type. Refusals are a `SourceError` (`UnknownSourceKindError`, or a
paged `name` without extension); nothing of a refused source is left on disk.
`put_page_transcription(vault, vault_relative_path, text, *, page=None) -> Path` writes a page's
Markdown transcription as `page-NNN.md` beside a stored page of `notes`, `book` or `pdf` (with
`page=K`, `page-NNN.pKKK.md`: the transcription of scanned page `K` of a stored PDF, #257)
(`vault_relative_path` is the page or any file derived from it, checked like `read_source`'s),
atomically, guarded, under the directory's lock, replacing an earlier one; `SourcePathError` for
anything else, `SourceNotFoundError` when the page's sidecar is not there.
`update_page_meta(vault, vault_relative_path, updates) -> Path` merges `updates` into a stored
page's sidecar (same path rules and errors; other keys kept in order; guarded, atomic, under the
directory's lock): what `sources` learns after storing, such as a textbook page's `book_page`.
`edit_page_transcription(vault, vault_relative_path, text, *, edited_at=None) -> Path` (#473) is
the student's hand correction of a photographed page's transcription (`EDITABLE_TRANSCRIPTION_KINDS`:
`notes`, `book`; a PDF page or any other kind is a `SourcePathError`; a derived file, a sidecar or
a removed page a `SourceNotFoundError`). The page must already have a transcription (its
`page-NNN.md` or a sidecar `transcription` string), else `NoTranscriptionError`: the student
corrects what the transcriber read, never races it. The text (blank once stripped is a
`ValueError`) is written as `page-NNN.md` ending in one newline, and a sidecar `transcription`
string is replaced too, so every reader (the editor, the index, `/meta`, the Recursos viewer)
reads the correction. The sidecar records it as the student's: `transcription_edited`
(`TRANSCRIPTION_EDITED_KEY`) `{at, by: student, previous_sha256, original_sha256}` -- the hash of
the text this correction replaced and of the text before the first correction (the machine's,
kept across later corrections). Both files are written atomically under the directory's lock and
pass the secret guard (`SecretRefused`, nothing written); an unreadable sidecar is a
`SourceFileError`. The caller commits: the server commits what was pending first, so the
machine's text stays in git history, then the correction.
`put_pasted_image(vault, subject_slug, topic_slug, content, content_type, *, added_at=None) ->
Path` stores an image the student pasted into the notes (#313) as `sources/images/img-NNN.<ext>`,
the extension from `content_type` (`IMAGE_EXTENSIONS`: `image/png`, `image/jpeg`, `image/webp`),
with a sidecar `origin: pasted`, `content_type`, `sha256` of the content and `added_at` (now, UTC,
by default); another type or an empty content is a `SourceError`, nothing written. The notes cite
it as `[Imagen pegada N](../sources/images/img-NNN.<ext>)` (source id
`sources/images/img-NNN.<ext>`, ADR-0005 proposal in epic #311). Images are listed, read and
indexed as sources like the others (the index keeps no text of them). A region the editor crops
out of a stored page (`editor.crop`, #484) is stored with `put_source(..., "images", ...)` as
another `img-NNN.<ext>`, whose sidecar has `origin: cropped` instead of `pasted`, plus
`cropped_from` (the vault-relative path of the page it was cut from, which is never modified),
`bbox` (`[x0, y0, x1, y1]`, fractions of the page image) and `requested_region` (the student's
description); the notes cite it as `[Imagen recortada N](../sources/images/img-NNN.<ext>)`.
An SVG diagram the editor draws (`editor.diagram`, #511) is stored with `put_source(...,
"images", "diagram.svg", ...)` as `img-NNN.svg` (sidecar `origin: drawn`, `title`,
`content_type: image/svg+xml`, `sha256`, `added_at`); the notes cite it as
`[Diagrama N](../sources/images/img-NNN.svg)`. `put_source` stores an `.svg` image only as
`sanitize_svg` rebuilds it (an unusable one is `SvgError`, nothing written); `put_pasted_image`
never accepts SVG.

**SVG sanitizer** (`svg.py`, #511): `sanitize_svg(content) -> bytes` parses an SVG (refusing any
DOCTYPE/ENTITY declaration and anything over `MAX_SVG_BYTES`, 256 KiB) and rebuilds it from an
allow-list: only the drawing elements of `ALLOWED_ELEMENTS` in the SVG namespace (an unqualified
`<svg>` is put in it) -- `script`, `foreignObject`, `image`, `a`, `iframe`, the animation elements
and anything foreign are dropped with their subtree --; attributes unqualified (plus
`xlink:href`, `xml:space`, `xml:lang`), no `on*` handler, no `javascript:`/`vbscript:`/`data:`
value, `href` only as `#id`, `url()` only as `url(#id)`; CSS (`<style>`, `style=`) dropped when it
holds `@import`, an external `url()`, `expression(`, a backslash escape or a script scheme;
comments and processing instructions dropped. The output is deterministic UTF-8 XML and
sanitizing it again changes nothing (`is_safe_svg(content)` checks exactly that). `SvgError` is a
`ValueError` with a Spanish message. Tests: `tests/vault/test_svg.py`.
`set_book(vault, subject_slug, topic_slug, title) -> Book` / `get_book(...) -> Book | None` keep
the topic's textbook (`Book(title)`, spaces collapsed; `ValueError` for an empty title) in
`sources/book/book.yaml`, which is not a source, not numbered and not indexed as one.
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

**Removing a source is a soft delete** (#451): no module deletes a source's files (only the purge
removes burst originals, below). `remove_source(vault, vault_relative_path, *, removed_at=None, sha256=None, added_at=None) ->
Path` retires one listed source (same path rules as `read_source`; a derived file, a sidecar, a
path with nothing listed there, a source removed already or -- when `sha256`/`added_at` is given
-- a source whose sidecar does not record them, another one that reused the path, is a
`SourceNotFoundError`): under the
directory's lock it merges `removed: {at: <ISO 8601, now UTC by default>, by: student}`
(`REMOVED_KEY`) into the sidecar, creating one if the source had none, and returns the sidecar's
path; the caller commits. From then on `list_sources` leaves the source out (pass
`include_removed=True` to see it), and so does every listing, catalogue or context built from it
(the web's source lists, the editor's catalogue, the Recursos selection check, triage, the index),
while `read_source` still serves its content, its derived files and its sidecar, so a footnote of
the notes that cites it keeps resolving. `is_removed(meta)` tells a removed sidecar;
`removed_source_paths(vault, s, t)` gives a topic's removed sources' vault-relative paths. `retire_orphan_sidecar(vault, vault_relative_path, *, removed_at=None, sha256=None, added_at=None) -> Path | None` (#502)
retires the sidecar a source's content left behind (the content file gone, its sidecar still
there, e.g. a batch commit took the sidecar of a crop whose image a revert then removed): the
same `removed` mapping, under the directory's lock, returning the sidecar's path; `None` with
nothing written when the content is there, another file shares the sidecar's stem (a pasted
`img-001.png` next to a gone `img-001.jpg`: the sidecar is that file's), there is no sidecar, it
is removed already or it does not record the given `sha256`/`added_at`, all checked under the
lock. The
caller commits. The
number of a removed source is never reused (its files are still in the directory).

### Study history -- `study.py`
`study/<name>.jsonl` under a topic: what the student did with the generated material (#75 writes
`quiz-results`). `append_study_record(vault, s, t, name, record)` appends any Pydantic `record`
(`append_jsonl`: secret guard, fsynced; `*.jsonl` merges with `union`) and returns the path;
`read_study_records(vault, s, t, name, model)` reads them back (empty when there is none);
`study_log_path`, `study_directory`. A study document that is one file rather than a log is
`study/<name>.yaml`: `write_study_file(vault, s, t, name, model)` writes any `VaultFileModel`
whole (atomic, deterministic dump, secret guard) and `read_study_file(vault, s, t, name, model)`
reads it (None when absent); `study_file_path`. The editor's study label is `study/version.yaml`
(#335). A name that is not `[a-z0-9-]` is `StudyLogError`; an unknown topic raises as
`get_topic`. Nothing here runs git. Imported from
`studentassistant.vault.study` (not re-exported by the package).

### Feedback inbox -- `feedback.py` (#472, #476, #547)
`feedback/inbox.jsonl` at the vault root (not under a topic, and not under a user's either): the
bugs and improvements of the app itself the student reported in the workspace chat or the study
chat (`editor.feedback`). It is about the app, which every student of the vault uses, so there is
one inbox for the whole repository and every function here reads and writes it at `vault.root`,
whichever handle it is given -- a user's folder holds no `feedback/` of its own. What a user's
handle adds is who reported the item: its id goes into the item's `context.user`, so the maintainer
knows which student to ask and one student's report is not read as another's. The
maintainer triages it by hand with the CLI (`studentassistant feedback list|mark`,
`docs/modules/server.md`); nothing here or anywhere in the backend calls GitHub. Append-only JSONL
(`append_jsonl`: secret guard, fsynced; `*.jsonl` merges with `union`), two kinds of line:
- `{"record": "item", "id", "created_at", "kind", "title", "body", "context"}` -- a new item:
  `id` (below), `kind` `bug` | `mejora`, `title` (≤ `MAX_TITLE_CHARS` = 140 characters, counted
  after runs of whitespace are collapsed; `collapse_title`), `body` (≤ 4000), `context` (`FeedbackContext`: `user`,
  `subject`, `topic`, `route`, `session_id`, `mode` `construir` | `estudiar`, `excerpt` ≤ 1200 characters).
  Every field of `FeedbackContext` is optional and `user` is the newest of them: a line written
  before a vault had users has none, reads back with `user=None` and folds exactly as it did.
- `{"record": "status", "id", "time", "status", "issue"}` -- a later change: `status` `nuevo` |
  `triado` | `descartado`, `issue` the triage reference (an issue number of the code repository,
  or `null`).

Ids (#476): a new item gets `fb-` plus six random characters of `FEEDBACK_ID_ALPHABET`
(lower case, no `0`/`o`/`1`/`l`/`i` look-alikes; e.g. `fb-k7m2qx`), drawn again if it is already in
the inbox or all digits. Two PCs that both report something before their vaults sync therefore
give their items different ids without any coordination (about 10⁹ values; the local lock only
guards one PC), and the id stays short enough to type in `feedback mark`, which ignores case and
surrounding spaces. Inboxes written before #476 hold `fb-N` ids (one past the highest in the
file); `FEEDBACK_ID_PATTERN` accepts both forms, so those items are still read, folded and marked.

Reading folds the lines in file order into `FeedbackItem`s (the entry's fields plus `status`,
default `nuevo`, `issue` and `updated_at` from the last change); a change for an unknown id is
ignored. A legacy `fb-N` two PCs both allocated before a union merge is **ambiguous**: every item
line with it is kept and listed (same id, oldest first), a change with that id folds into every
item line before it (nothing records which PC's item it meant), and `get_feedback` /
`set_feedback_status` refuse it with `FeedbackAmbiguousError` (a `FeedbackError`; `.items` names
every item) and write nothing -- the CLI prints them and exits 1; the maintainer renames one of
the ids in the file by hand.
- `add_feedback(vault, kind, title, body, context=None, *, clock=None) -> FeedbackItem` -- allocates
  the id and appends under the `feedback` lock, taken on the repository so a user's handle and the
  root hold one and the same lock and the server and the CLI of one PC never give two items one id
  (`VaultBusyError` after 30 s, nothing written), creating the root's `feedback/` on first use;
  `SecretRefused` or a `ValidationError` write nothing. An item reported through a user's handle
  records that user in its context, unless the `context` the caller passed already names one.
- `set_feedback_status(vault, id, status, issue=None, *, clock=None) -> FeedbackItem` -- appends a
  change under the same lock; `issue=None` keeps the item's current reference.
  `FeedbackNotFoundError` (a `FeedbackError`, a `VaultError`) for a malformed or unknown id.
- `list_feedback(vault, status=None)` (oldest first by `created_at`, file order among equal
  times; filtered by status), `get_feedback(vault, id)`, `feedback_path(vault)` -- the repository's
  inbox, so a status change the maintainer marks from the root reaches an item a student reported
  through their handle; a line that is not a feedback line raises `JsonlError`.
- The models (`FeedbackContext`, `FeedbackItem`, `FeedbackKind`, `FeedbackStatus`, `FeedbackMode`,
  `FEEDBACK_KINDS`, `FEEDBACK_STATUSES`), the functions and errors (`FeedbackNotFoundError`,
  `FeedbackAmbiguousError`) are re-exported by the package. `MAX_TITLE_CHARS` and
  `collapse_title` (in `vault.feedback`) are also the limit of the editor's `report_feedback`
  tool, so a title the vault would refuse is a malformed call there, never cut silently.
  Nothing here runs git: the caller notes the change for the sync loop (`editor.feedback`); a CLI
  change is committed by whichever later batch commit stages the vault (`git add --all`).

### Notes and generated material -- `notes.py`
`read_notes(vault, subject_slug, topic_slug)` returns the text of `notes/apuntes.md`, or `None`
when it has not been written yet (a symlink or non-UTF-8 file is a `NotesError`, a `VaultError`).
`list_generated(vault, subject_slug, topic_slug)` returns the sorted vault-relative POSIX paths of
every file under `generated/`, subdirectories included and symlinks skipped; an empty list when
the directory does not exist. `notes_path(...)` and `generated_directory(...)` give the paths.
`write_notes(vault, subject_slug, topic_slug, text)` writes `notes/apuntes.md` atomically
(creating `notes/`, secret guard included) and removes a leftover draft;
`write_notes_draft(...)` writes `notes/borrador.md` (a generation the editor's validator
rejected), leaving `apuntes.md` untouched; `read_notes_draft(...)` and `notes_draft_path(...)`
mirror the notes ones. Both writers return the path, refuse an unknown topic like the readers and
a symlinked `notes/` or file with `NotesError`. Nothing here runs git (the editor commits and
tags through `GitSync`, the generators commit through it).
`write_generated(vault, subject_slug, topic_slug, name, content)` writes `generated/<name>`
(text as UTF-8 or bytes, atomically, secret guard included, creating subdirectories);
`read_generated(...)` returns its bytes or `None`; `remove_generated(...)` removes it (and the
directories it leaves empty), `False` when it was not there. `name` is relative to `generated/`,
`/`-separated segments of `[A-Za-z0-9._-]` not starting with a dot (`check_generated_name`); a bad
name or a symlink on the way is a `NotesError`.

### Reading with ids from outside
Every reader above (`list_sources`, `read_session_transcript`, `read_notes`, `list_generated`)
goes through `require_topic(vault, subject_slug, topic_slug)` (`topics.py`): a value that is not a
slug (`slugs.is_slug`: `[a-z0-9]` runs joined by single hyphens) is a `SubjectNotFoundError` or a
`TopicNotFoundError` without touching the disk, so an id from a URL cannot walk out of its topic.
A user id is guarded the same way and by the same `is_slug` (`Vault.for_user`, `get_user`): an id
that is not a slug is a `UserNotFoundError` before any path is built, so it cannot name a directory
outside `users/` either.

### Cross-process locks -- `locking.py`
Decision (#165): two processes on one PC (the server and a CLI command, or two CLI commands) are
kept apart by locks, not by the CLI refusing while a server runs. Each lock is an advisory
`fcntl.flock` on `.git/studentassistant-locks/<name>.lock` (`LOCKS_DIRNAME`): inside git's private
directory, so never committed, pushed or indexed; the OS drops it when its process dies, so a
crash never leaves the vault locked. `vault_lock(root, name)` returns the process-wide
`VaultLock` of that name (one object per lock file, shared by the threads of the process;
re-entrant per thread, only the outermost hold touches the file); `lock.hold(timeout)` is the
context manager, and a lock not obtained in time raises `VaultBusyError` (a `VaultError`, Spanish
message naming the lock) -- no wait is unbounded.

**The locks are the repository's** (#547). A vault holds one folder per user and a caller works
either through the repository root or through one of those folders, so `repository_root(path)`
turns the path a caller has into the directory holding `.git/` before anything is named after it:
`path` itself when it has a `.git/`, `path.parent.parent` when `path` is a `users/<user-id>/` whose
parent's parent has one, and `path` unchanged otherwise. Only that one shape is recognised: a
directory deeper inside a user's folder (`users/<id>/subjects/...`) comes back as it is and, having
no `.git/` of its own, would get the in-process lock alone -- so a caller passes a handle's `path`,
never a directory under it, which is why `directory_lock`'s first argument is the handle and its
second the directory to lock. Every lock function takes either handle
and gives the repository's lock, so two processes on one vault share one lock file whichever handle
each of them works through, and a directory's lock is the same lock -- and a real cross-process
one, not the in-process fallback -- for both. A `sources/<kind>/` of one user is therefore a
different lock from the same-named one of another, and the root and that user's handle agree on
which lock a directory has.

Three locks exist:
- `directory_lock(root, directory)` -- one per `sources/<kind>/` (named by a hash of its path
  relative to the repository), around number allocation and the writes under it (`sources.py`).
- `git_lock(root)` -- around every git command `GitSync` runs, held for a whole operation (the
  `add`/`diff`/`commit` of a batch, a push, a `pull --rebase` with its abort and refs, a tag, a
  revert) and by `rewrite_history` for the whole purge rewrite (`GitSync.locked()`). The wait is
  the `timeout_seconds` of `[vault.git]`; a busy lock counts as a failed git command: `checkpoint`
  returns `None` with `last_error` set and the batch still pending, `push_now` schedules a retry,
  `sync` is an `error` result, `list_notes_tags`/`create_notes_tag`/`revert_paths` raise
  `GitCommandError`, `rewrite_history` a `PurgeError`.
- `vault_lock(root, "feedback")` -- around each write of the feedback inbox (`feedback.py`, #472):
  the id allocation and its append, or the id check and a status change's append.
A directory no repository holds -- one with no `.git/` and not a `users/<user-id>/` of a vault that
has one -- only gets the in-process part, and no lock file is written anywhere; a user's folder of a
vault is not such a directory, and gets the repository's real lock like the root does. The batch commit stages
`git add --all -- . ':(exclude,glob)**/.*.tmp'`: a writer's temporary file (`files.py`) is never
staged, so a commit while another thread or process is mid-write neither fails on the file
vanishing nor commits half of it. Not locked: the other writers (sessions, JSONL, notes, state)
of two processes writing the same file at once, `setup` (a fresh vault), and the index's
read-only git commands.

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
Only these two modules, the setup, the purge and the index below run git on the vault (the index
only read-only commands: `rev-parse`, `tag --list`).
`GitRunner(root, identity, timeout, environment=None)` runs `git` as a
subprocess in the vault root with a `GitIdentity(name, email)` as author and committer (passed in
the environment, so no git configuration decides it), never prompts (`GIT_TERMINAL_PROMPT=0`,
SSH `BatchMode`), is killed after `timeout`, and returns a `GitResult` (`ok`, `describe()`) whose
output went through `redact` (URL userinfo and the secret-guard patterns become `***`);
`check(...)` raises `GitCommandError` instead. `environment` adds variables to every command: it
is how a GitHub credential reaches git during `setup`/`doctor` (see "GitHub and setup"), never a
URL; `GitSync` passes none and relies on the credential helper `setup` wrote to the vault's own
`.git/config` (a command, never a secret; "Credential helper" below).

That `root` is the repository's -- the directory holding `.git/`, a `Vault`'s `root`, and never one
user's folder of it (#547): a vault is one repository with one HEAD, one index and one set of tags
whichever user's content a command is about, and a path given to a command is relative to that
root. A caller that has one user's paths prefixes them (`UserGitSync`, below).

`GitSync(vault, settings=None, clock=None)` drives one vault repository; `settings` is a `VaultGitSettings`
(`studentassistant.config`, `[vault.git]` / `SA_VAULT__GIT__*`), `clock` any object with
`monotonic()` (`SystemClock` by default; tests inject a manual one). `vault` may be a root handle
or a user's and it makes no difference to what git does: the runner and the lock it holds are
`vault.root`'s either way, and a batch stages the whole repository (`git add --all -- .` from
`root`), so every user's content travels in the same commits and the same pushes -- one `GitSync`
per repository per process, and `for_user` is what scopes paths and notes tags to one student.
It never sleeps and starts no thread:
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
- `read_file_at(revision, path) -> str | None`: a vault-relative file as committed at a commit
  or tag (`git cat-file blob`, bytes as stored -- not redacted, it is the vault's own content --
  decoded as UTF-8), `None` when that revision has no such file or is unknown; `ValueError` for a
  path outside the vault. `path` is relative to the repository, as git holds it: one user's file is
  read through `for_user`, whose `path` is relative to that user's folder. The editor's notes
  versions read old `apuntes.md` with it.
- `revert_paths(commit, paths, message) -> str | None`: a `git revert` of `commit` restricted to
  the vault-relative `paths` -- each goes back to its content in `commit^` (removed if `commit`
  created it), the other files of `commit` are kept -- committed under `message` after committing
  what is pending; `None` when nothing differs. `RevertConflictError` (`path`) when a path is not,
  at HEAD, what `commit` left (a later change would be lost), `ValueError` for an unknown commit
  or a path outside the vault. The paths are the repository's here too, and a user's undo goes
  through the view, which prefixes them and gives the conflict back as the caller named it. The
  editor's undo of a revision turn.
- `create_notes_tag(subject_slug, topic_slug, message=None)` commits what is pending and puts the
  annotated tag `<subject-slug>/<topic-slug>/apuntes-vN` on HEAD, N one past the highest existing
  for that subject and topic (`notes_tag_name(subject_slug, topic_slug, version, user_id=None)`,
  whose prefix `notes_tag_prefix(subject_slug, topic_slug, user_id=None)` builds), pushed by the next push;
  `list_notes_tags(subject_slug, topic_slug)` returns the `NotesTag`s (`name`, `version`,
  `commit`, `tagged_at`, `message` -- the first line of the tag's message) oldest first;
  `create_notes_tag` returns the new tag as listed. A non-slug subject or topic raises `ValueError`. Tags are keyed by
  subject as well as topic because topic slugs are unique only within a subject (#143): two
  subjects' `introduccion` topics keep separate version sequences. The earlier topic-only form
  `<topic-slug>/apuntes-vN` is not read and needs no migration: no writer created notes tags
  before this format (the editor, which will, is not written yet), so no vault holds one. On a
  `GitSync` these are the repository's own unprefixed tags, which is what the content at the root
  of a format-1 vault has; one user's notes are tagged by their view, which prefixes the tag with
  their id and lists nothing else (ADR-0002's `<user-id>/<subject-slug>/<topic-slug>/apuntes-vN`).
  Re-creating the tags of a vault a migration moves under that prefix, on the migration's own
  commit, is `retag_notes`, next.
- `retag_notes(user_id, subject_slug, topic_slug, tags, commit="HEAD") -> list[NotesTag]` (#548)
  re-creates the `tags` it is given as that user's notes versions of the topic, on `commit`: each
  new tag is annotated and keeps the version number and the message of the one it copies, so only
  the name changes, to the form that user's handle writes and lists (`user_id=None` gives the
  repository's own unprefixed one). Copying rather than tagging the next free version is the point:
  a notes version a student read by its tag, and a study label (`study/version.yaml`) that names
  one, keep the number they were given when the content they tag moves into its user's folder. The
  tags copied are left exactly as they were -- this adds names, it never moves or deletes one -- and
  a name that is taken already is skipped rather than refused, so doing it twice does nothing the
  second time. Nothing is committed and no push is scheduled: the caller owns both, which for the
  migration that needs this is its one commit and the `push_now()` that follows it. A non-slug
  subject or topic, or a `commit` this vault may not be asked about, is a `ValueError`; git refusing
  a tag, or another process keeping it busy past `timeout_seconds`, is a `GitCommandError`. It
  returns the new tags as git lists them, oldest version first, a skipped name not among them. The
  migration is its only caller.
- `status()` -- a `SyncStatus` snapshot that runs no git: `pending_changes`, `last_commit`,
  `last_commit_at`, `pending_commits` (ahead of the remote), `last_push_at`, `last_push_failure`,
  `consecutive_push_failures`, `next_push_due` (clock time), `last_sync`, `last_error`,
  `divergence`.
- Divergence: a `conflict` sync keeps both sides -- the local HEAD (still checked out) and the
  fetched remote commit are pinned under `refs/studentassistant/divergence/local` and `/remote`
  (`DIVERGENCE_LOCAL_REF`, `DIVERGENCE_REMOTE_REF`) and described by `status().divergence`, a
  `Divergence` (`paths`, `local_commit`, `remote_commit`, `detected_at`). The refs are the
  repository's, in `root`'s `.git`, whichever handle the sync was given, and so are the `paths`
  listed, which are the ones git holds.
  `divergent_versions(path)` returns a `DivergentVersions` (`path`, `local`, `remote`: each
  side's text, `None` where that side has no file), or `None` for a path not diverging. A failed
  sync for another reason keeps the divergence; the next successful sync clears it and deletes
  the refs. How the student picks a side is not written yet (web UI).
- `request_push()` -- makes a push due now for the next `run_due()` (runs no git): the
  active-host claim at session start reaches the remote without the start waiting on the network.
- The pull defines the `sa-active` merge driver (`merge.sa-active.driver=true`) on its command
  line: `.sa/active.yaml` keeps the remote's side of a rebase, so it is never a conflict.
- `run(interval=1.0)` -- the asyncio loop: `run_due()` in a worker thread every `interval`, until
  cancelled. Every other method blocks on git; async callers use `asyncio.to_thread`.

**One user's view: `sync.for_user(user_id) -> UserGitSync`** (#547, epic #544, ADR-0002). What the
editor, the generators and the server hand a student's code: the same repository's sync, with the
two things that name content translated to that student. It shares the parent's state, git lock,
commits, push schedule and background loop -- a view has neither `run()` nor `run_due()`, so it
cannot start a second loop or drive one, and the parent's `run()` is the one that keeps going -- and
delegates as they stand: `note_change()`, `checkpoint(message)`, `flush()`, `push_now()`,
`request_push()`, `sync()`, `status()` and `locked()`, with `settings`, `identity` and `git` as
properties of the parent's. A `status()` is therefore the repository's snapshot down to the
divergence it lists, whose paths are the ones git holds, not this user's.
- `vault` is that user's handle and `prefix` their `users/<user-id>/`, so a purge or an editor
  given a view plans and writes inside that folder (`purge.py`, below).
- `read_file_at(revision, path)`, `revert_paths(commit, paths, message)` and
  `divergent_versions(path)` take -- and give back, in `RevertConflictError.path` and
  `DivergentVersions.path` -- paths relative to `users/<user-id>/`, like every id the vault's
  readers and writers return for that user; the view prefixes them before git sees them. An empty
  or absolute path, or one carrying `..`, is a `ValueError` here exactly as on the parent.
- `read_file_at` also reaches a revision from *before* the user's folder existed: when the
  user-prefixed path is not in that revision and `vault.yaml`'s `legacy_root_user` names this user,
  the root path is read instead, and without that field there is simply no such file. That is what
  keeps the notes versions of the vault's first user -- committed while their content was still the
  repository's own -- readable after the migration moves it, and `migrate_to_users` is what writes
  the field (#548). The field is read from `vault.yaml` and not from `vault.meta`, which is the
  snapshot `Vault.open` took: the migration writes it into a vault a handle is already open on, and
  the versions committed before the move have to stay readable to whoever holds that handle. A file
  that cannot be read names nobody. It is re-read on every such miss
  rather than cached, so a caller that loops over revisions a user's folder is not in pays a
  `vault.yaml` read per revision.
- `create_notes_tag(subject_slug, topic_slug, message=None)` and
  `list_notes_tags(subject_slug, topic_slug)` use and list only
  `<user-id>/<subject-slug>/<topic-slug>/apuntes-vN`: two users with the same subject and topic
  slugs keep two version sequences, and neither one's view shows the other's tags or the
  repository's unprefixed ones. `NotesTag` keeps its fields; its `name` is the tag as git holds it,
  so it carries the user's id first, while `version` counts within that user's subject and topic.
- `for_user` raises `UserNotFoundError` for an id that is not a slug or is not a user of this
  vault, and takes the repository's root handle first, so a sync that was itself given a user
  handle still gives every user's view.

Config keys (`[vault.git]`): `author_name` (default: the `student` of `vault.yaml`),
`author_email` (default `estudiante@studentassistant.invalid`), `remote` (`origin`),
`commit_quiet_seconds`, `commit_max_delay_seconds`, `push_debounce_seconds`,
`push_backoff_initial_seconds`, `push_backoff_max_seconds`, `timeout_seconds` (120, per git
command), `active_host_stale_seconds` (21600: an active-host claim older than this is ignored).

### Active host -- `active.py`
One active writer between PCs (ADR-0002). `.sa/active.yaml` is an `ActiveHost` (`host`, `user?`,
`session_id?`, `subject?`, `topic?`, `claimed_at`, `released_at?`; `released`). The record is
metadata about the sync, not content, and it is the repository's rather than one student's: one PC
captures at a time whatever user it captures as, so there is one record for the whole vault and
every function here reads and writes it at `vault.root`, whichever handle it is given -- a user's
folder holds no `.sa/` of its own (`active_host_path(vault)` is the root's, and so is the
`.gitattributes` that gives the record its merge driver). What the handle does say is *who* is
capturing: `user` is the id of that student.
`claim_active_host(vault, host, session_id=None, subject_slug=None, topic_slug=None,
claimed_at=None, *, user_id=None)` writes a fresh claim (and appends the `sa-active` line to an
older vault's `.gitattributes`, `ensure_active_host_attribute`), recording as `user` the `user_id`
it was given or, without one, the id of the handle it was given when that handle is a user's -- so
a session start that already works through a user's handle says who it is for without being told,
and a claim on the root handle records no user unless one is passed.
`release_active_host(vault, host,
session_id=None, released_at=None)` sets `released_at` only when the record is still that host's
unreleased claim of that session (a newer claim is never overwritten), else returns `None`;
`read_active_host(vault)` returns the record or `None` (missing, or unreadable: logged). A record
written before a vault held several users has no `user` and still reads, warning included: it
simply does not say who it was.
`active_host_warning(record, host, stale_after, now=None)` / `check_active_host(vault, host,
stale_after, now=None)` return an `ActiveHostWarning` (`record`, Spanish `message`: that PC has a
session open and may have unpushed changes) only for another host's unreleased claim younger
than `stale_after` seconds (`is_stale`). The message names the student that PC is capturing as when
the record says who it is -- `El equipo «<host>», con el usuario «<user>», tiene abierta ...`
against `El equipo «<host>» tiene abierta ...` -- because on a vault several students share
"another PC is capturing" is only half of what the student needs to decide whether to wait.
A warning, never a refusal. These write files only;
committing and pushing is the caller's (`server`: claimed, checkpointed and `request_push()`ed at
session start after the pull and check; released before the end's checkpoint and push; the
warning from the pulls at vault open and session start is `SessionService.host_warning`, shown by
`GET /api/vault/status`).

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
  given as `GIT_CONFIG_*` environment variables (other helpers reset first).
  `credential_helper()` returns the same helper with `gh` at the absolute path `shutil.which`
  resolves now (`None` when `gh` is not found), for `setup` to persist (#308).
- `TokenHost(token, remote_base, timeout)` -- a fine-grained token from `GH_TOKEN` or
  `GITHUB_TOKEN` (`TOKEN_ENV_VARS`, first non-empty wins, `token_from_environment()`). The token
  reaches git only as the `STUDENTASSISTANT_GIT_TOKEN` (`GIT_TOKEN_ENV_VAR`) variable of the child
  process, which an environment-given credential helper reads: never in a remote URL,
  `.git/config`, the vault, `config.toml` or a message; `repr()` omits it. `repo_exists` is a
  `git ls-remote`; `repo_is_private` is always `None`; `credential_helper()` is `None` (the
  token cannot be persisted: a service that must push with it needs it in its own environment). `create_private_repo` always refuses with `NO_GH_CREATE_MESSAGE` (install and
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
  student)`, which makes the vault's first user as it makes the vault (#548), so the repository
  pushed to GitHub holds somebody's folder from its first commit and the student who just ran
  `setup --create` can write a subject straight away; commits the first files (`vault creado`), adds
  `origin`, pushes `main` and verifies push access. Everything GitHub could refuse is checked before
  anything is written locally.
- `clone_vault(path, repo, host, post_clone=<no-op>, author_email=..., timeout=..., warn=<no-op>)`
  -- refuses a non-empty `path` and a repository that does not exist; clones; then opens what it
  cloned, and a `format_version` this backend cannot read at all -- a vault a newer backend wrote --
  becomes a Spanish `SetupError` with the clone left in place for that newer backend. A format-1 one
  is NOT refused (#548): a repository an older installation pushed is cloned without complaint, the
  handle the result carries is `open_for_migration`'s, and `warn` gets the `Vault.open` refusal it
  would have been -- «Aviso: El vault de … es de formato 1 …», naming
  `studentassistant vault migrate-users`, the one command that unlocks what has just been downloaded
  (`setup` gives it `typer.echo`, so the student reads it).
  Refusing it instead would leave a student who brought their notes to a new PC with a directory
  they may not use and no way to find out why. Push access is verified and `post_clone(vault)`
  called once (the CLI passes the index rebuild).
- Idempotence: a git repository at `path` whose `origin` is `host.remote_url(repo)` (a trailing
  `.git` or `/` ignored) is accepted as already set up -- only push access is checked again (and a
  create whose first push never happened is pushed); `post_clone` is not called. A format-1 vault
  already on this PC warns the same way, through the same `warn`.
- Credential helper (#308): every flow (created, cloned, already set up) calls
  `ensure_credential_helper(path, host, author_email=..., timeout=...) -> bool | None`, which
  writes `host.credential_helper()` to the vault's own `.git/config` (`None`: the host has none;
  `True`/`False`: whether it changed; a git failure is a `SetupError`), and then verifies push
  access *without* the host's environment (`credentials.unattended_runner`) when a helper was
  written, so setup proves the systemd service -- which has neither the shell's `PATH` nor the
  host's `GIT_CONFIG_*` -- can push. `doctor --fix` calls it to repair an older vault.
- `verify_push_access(runner)` -- `git push --dry-run origin main`; failing it is a `SetupError`.
- `check_remote_access(path, host, repo=None, author_email=..., timeout=...) -> RemoteAccess`
  -- for `studentassistant doctor`: the (redacted) `origin` URL, `repo_matches` (`None` without
  `repo` or `host`) and `push_error` (`None` when the dry-run push succeeded, else the Spanish
  reason). `host=None` runs git with no extra credentials. No repository or no `origin` is a
  `SetupError`. Writes nothing.
- Both return a `SetupResult` (`vault`, `repo`, `action`: `created`, `cloned` or
  `already-set-up`); every refusal is a `SetupError` (a `VaultError`) with a Spanish message, or
  the host's `GitHubHostError`.

### Credential helper -- `credentials.py`
Why (#308): `setup` authenticated through the host's environment only, so the systemd service's
`GitSync` (no `gh` on its `PATH`, no global `credential.helper`) failed every push with `auth`.
`CredentialHelper(base, command)` (`key` = `helper_key(base)` = `credential.<base>.helper`);
`install_credential_helper(runner, helper) -> bool` makes the repository's `--local` values for
that key exactly `""` (resets the helpers of the user's global configuration for that URL) then
`command`, e.g. `!'/home/linuxbrew/.linuxbrew/bin/gh' auth git-credential`; unchanged when
already so; `configured_helpers(runner, base)` reads them. No token is ever written.
`unattended_environment()` / `unattended_runner(root, identity, timeout)` run git as the service
does: `GIT_CONFIG_COUNT=0`, `GIT_TERMINAL_PROMPT=0`, empty `GIT_ASKPASS`/`SSH_ASKPASS` (no GUI
prompt) and a `PATH` of git's own directory plus `/usr/local/bin:/usr/bin:/bin`;
`probe_unattended_access(runner)` is `git ls-remote --heads origin` through it (`doctor`).

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
The repository prompt proposes `<login>/studentassistant-vault` (`DEFAULT_VAULT_REPO_NAME`), where
`<login>` is `github.github_login()`: one `gh api user -q .login` bounded by
`LOGIN_LOOKUP_TIMEOUT_SECONDS`. When `gh` is missing, not logged in, fails or times out the prompt
has no default: no owner is guessed, and the Linux user name is never used as one (it need not be
a GitHub account). The CLI reaches it through `cli._github_login`, which tests replace; an
unattended `setup --vault-repo ...` never looks it up.

**Restore drill** (`backend/tests/server/test_restore_drill.py`, #261): the promise "a new PC
restores everything with setup + clone + index rebuild" is tested end to end. A vault is built
through the public paths only (the sample recording replayed through `create_app`, `notes/generate`
and one `notes/chat` revision, `FakeClaude` per role), pushed by the app's `GitSync` to a local
bare repository standing in for GitHub, cloned into a fresh directory with `clone_vault` (a
`LocalHost`; `post_clone` rebuilds a fresh index, as the CLI's does), and compared with the
original: the study desk over REST (subjects, topics, each topic's summary, sessions, doubts,
notes versions and chat history), `load_observer_snapshot` per topic, `apuntes.md` and its tags,
`list_doubts`, the cost ledger totals and `VaultIndex.search` results. Since #370 the build also
makes the Construir and Estudiar state through REST only -- a pasted image saved into the notes
(`sources/images`, `PUT notes`), a second session of two blank captures the triage sets aside, a
typed workspace message that sets the book page aside and restores one blank page, a typed
message answered by an editor turn, the switch to Estudiar (`POST study`), one written study chat
question (`POST tutor`), a quiz and flashcards, one quiz result, one practice review and a last
save that leaves the materials stale -- and the compare covers it: the workspace chat
(`notes/chat`), `sources` and `sources/status` (state and triage reason), every source's meta and
bytes (the pasted image included), `study` (label, `study_current`, option states), `tutor`,
`generated` (status and stale reasons), `quiz/results`, the topic's `practice` queue and
`/api/practice/summary` (their `now` aside). Anything a future change stops committing (a file
written outside the vault, a cache mistaken for content) fails it.

### Derived index -- `index.py`
A SQLite database at `vault.index_path` (`VaultSettings.index_path`, `SA_VAULT__INDEX_PATH`,
default `~/.cache/studentassistant/index.sqlite3`): a cache (ADR-0002), never inside the vault,
every row read from vault files, so it can be deleted at any time.

An index is built over the handle it is given (#547), so a vault with users has **one database per
user**, each beside the configured path -- `user_index_path(index_path, user_id)` is
`<dir>/<stem>-<user_id><suffix>`, i.e. `index-ana.sqlite3` next to `index.sqlite3`, and never
inside the vault either, so one directory holds the whole cache and can be thrown away with it --
and each built over that user's handle: what a search answers with is one student's own notes, and
never another's. A vault with no `users/` folder -- a format-1 one awaiting its migration (#548) --
keeps the single database at the path itself.
`user_index_path` checks the id with `is_slug` before any path is built out of it, so a value that
came from a URL cannot name a database somewhere else, and raises `UserNotFoundError` for one that
is not a slug. Nothing derives it for a caller: `VaultIndex.open` indexes the handle it is given
into whatever `path` it is handed, so passing it the shared `index.sqlite3` with a user's handle
fills the repository's own database with that one student's content -- and, since `is_current()` is
keyed to the handle's resolved path, one file shared by two handles is rebuilt from scratch on every
switch between them.

`VaultIndex.open(vault, path)` creates the file (and its parents) when missing and makes it
current: it is rebuilt from scratch when it is new, not a usable SQLite file, of another
`INDEX_SCHEMA_VERSION`, built for another handle (the resolved `vault.path` it records, so one
user's database is not another's) or at another git HEAD than the
repository's (a clone, a pull); otherwise it is updated. The HEAD is the repository's because git
runs in `vault.root` whichever handle is indexed -- the history and the tags are the
repository's -- and a pull that moved it may have moved that user's tags with it. `is_current()` says whether schema, handle and
HEAD match; `refresh()` is `rebuild()` when not current and `update()` otherwise. `rebuild()`
drops every table and indexes the whole of the handle's content; `update()` is incremental: it walks the handle's `subjects/`
(symlinks never followed), compares each file's fingerprint (mtime ns, size, inode) with the one
recorded, and re-reads only the *units* one of whose files appeared, changed or disappeared. A
unit is `subject.yaml`; `topic.yaml`; a session's `session.yaml` + `transcript.jsonl`
(`events.jsonl` is not indexed); one `sources/<kind>/` directory; `notes/apuntes.md`;
`review/pending.yaml` (a removed source, #451, is neither listed nor searched: its row and the
texts of its page -- transcriptions, PDF page texts -- are left out). Both return an `IndexReport` (`rebuilt`, `units_indexed`, `units_removed`,
`documents`, `skipped`, `users`): a unit whose files this backend cannot read is left out and listed in
`skipped` as `(unit, reason)`, never failing the rest, and is retried when its files change. The
notes version tags are the handle's own content's -- `<subject-slug>/<topic-slug>/apuntes-vN` for
the repository's, `<user-id>/<subject-slug>/<topic-slug>/apuntes-vN` for a user's, the old
topic-only form being no notes version -- listed with the `tag --list` glob that prefix gives, so
a user's index holds only their own versions; they are re-listed on every update, and the HEAD the index reflects
is recorded. `run(interval=5.0)` is an asyncio loop calling `update()` in a worker thread until
cancelled (how a running backend keeps it current after vault writes); every other method
blocks. One lock serialises a `VaultIndex`, so threads may share it; `close()` it, or use it as a
context manager.

`rebuild_index(vault, path)` is what `studentassistant index rebuild` runs: open, rebuild, close (a
corrupt file is replaced). A user handle rebuilds that user's one database at `path`, which is what
their caller got from `user_index_path`. A root handle of a vault with users has nothing of
anybody's at the root to index, so it rebuilds one database per user instead -- each at
`user_index_path(path, that user's id)` over that user's own handle, in `list_users` order -- and
leaves `path` itself alone, not even removing a database that was there: the report then carries
every user's own under `users` (`IndexReport.users`, otherwise empty) as `(user id, report)` pairs,
counts the sum of them in the fields above, and names a unit one of them left out as
`<user-id>/<unit>`, because two users' units are the same names. A root handle of a vault with no
`users/` folder rebuilds the one database at `path`, as before. A folder under `users/` whose
`profile.json` this backend cannot read back is a `UserFileError`: there is then no list of users
to rebuild an index for. A database that cannot be created is a `VaultIndexError`.

Listings (frozen dataclasses, deterministic order, dates as ISO 8601 strings): `subjects()` ->
`IndexedSubject(slug, name)`; `topics(subject=None)` -> `IndexedTopic(subject, slug, title,
fidelity_mode, created_at)`; `sessions(subject=None, topic=None)` -> `IndexedSession(subject,
topic, id, started_at, ended_at, host)`; `sources(subject=None, topic=None)` ->
`IndexedSource(subject, topic, kind, path, meta)` in `list_sources` order, `path` being what
`read_source` takes; `pending(subject=None, topic=None)` -> `PendingItem(subject, topic,
position, item)`, the entries of `review/pending.yaml` as the file holds them (a top-level list,
or the `items` list of a mapping; their schema is the observer's); `note_versions(subject=None,
topic=None)` -> `NoteVersion(subject, topic, version, name, commit)`, keyed by subject and topic as
the tags are (so two subjects' `introduccion` never share a sequence) and, on a user's index, by
that user: only their own versions are listed, `name` is the tag as git holds it and so carries
their id first, while `subject`, `topic` and `version` are the parts the index read out of it --
the same two slugs and the same counting their notes have always had.

`search(query, subject=None, topic=None, kinds=None, limit=20)` -> `SearchHit`s over FTS5
(`unicode61`, diacritics removed: `fotosintesis` finds `fotosíntesis`). `query` is plain text,
never FTS5 syntax: every word must appear, as a prefix; a query without a word matches nothing.
`kinds` narrows to `DOC_KINDS`: `notes` (`notes/apuntes.md`), `page` (a page transcription
`sources/{notes,book,pdf}/page-NNN.md`), `pdf` (the extracted text `sources/pdf/page-NNN.pKKK.txt`
of page K of a stored PDF; for a scanned page, whose `.txt` is empty, its Claude vision
transcription `page-NNN.pKKK.md` instead when that is stored and not empty -- same kind and
`source`, `path` naming the `.md`; a page with text keeps its `.txt`; the `.md` lives in the
same `sources/pdf/` unit, so a transcription written after the import is picked up by `update()`
as well as by `rebuild()`), `web` (`sources/web/NNN-<slug>.md`) and `transcript` (one final
segment). Ranked by BM25, ties by path then `seq`, so the same vault always gives the
same list. A hit has `kind`, `path` (the file's path relative to the handle, so relative to their
own folder on a user's index -- the path the web opens and every id the web and the phone already
use), `source` (for a page, its original
`page-NNN.<ext>`, which the web opens; for a PDF page, `sources/pdf/page-NNN.pdf#page=K` as
provenance cites it; for web, the page itself; `None` for notes and
transcripts), `subject`, `topic`, and for a transcript `session`, `seq` and `t_start`; `snippet`
puts each matched term between `SNIPPET_START` (`\x02`) and `SNIPPET_END` (`\x03`), control
characters no vault text holds, so the web can highlight without trusting any markup. An unknown
kind or a `limit` below 1 is a `ValueError`.

The command: `studentassistant index rebuild` rebuilds the configured vault's index and prints the
number of searchable documents and any unit left out -- on a root handle of a vault with users that
is every user's database, the documents the sum of theirs and a unit left out named
`<user-id>/<unit>`; `setup` rebuilds it right after a clone
(ADR-0002: install -> setup -> clone -> index rebuild), and `vault migrate-users` ends a migration
that worked with the same call on a root handle (#548), so the databases of the vault that has just
been rearranged appear one per user. The format-1 database at the configured path is left where it
is, as every root-handle rebuild leaves it: a cache nobody opens once every handle is a user's, and
one the student may delete. A rebuild the migration's CLI cannot do is said in Spanish and is not a
failure of the migration, which is committed and complete -- `index rebuild` recreates a cache from
the vault whenever it is asked.

`studentassistant.vault` re-exports the vault, subject, topic, session, topic-state, source, JSONL,
ledger, notes, git sync (with `Divergence`, `DivergentVersions`, `UserGitSync`, `notes_tag_prefix`),
active-host (`ActiveHost`,
`ActiveHostWarning`, `claim_active_host`, `release_active_host`, `read_active_host`,
`check_active_host`, `active_host_warning`) and secret-guard names of this section; the YAML models, the slug helpers, the
file writers, `redact`, `summarize_changes` and the GitHub, setup, index and migration names are
imported from their own module (`studentassistant.vault.github`, `studentassistant.vault.setup`,
`studentassistant.vault.index`, `studentassistant.vault.migrate`).

### Not written yet
As of issues #21, #117, #119, #135 and #61 (which writes the notes) no code reads or writes these parts of the layout:
- **generated** -- writing `generated/` and everything the generators put in it (listing exists:
  `list_generated`).
- Also unwritten: the retention `purge` described below. (`conversations/` is written since #51.)

## Size report -- `stats.py`
`vault_stats(vault, top=10) -> VaultStats` measures the vault with pure reads (no git command, no
write; links are not followed), so the open question "images in plain git or Git LFS" (VISION §10)
can be decided with real numbers (#284). Names are imported from `studentassistant.vault.stats`.
The walk starts at the handle's `path`, so a root handle measures the whole repository and a user's
handle their folder, and the report says whose bytes they are (#547): a vault holds one folder per
user and two students may well study a subject of the same slug, so one number for both would be
nobody's.
- **Categories** (`categorize(parts)`, `CATEGORIES`, Spanish `CATEGORY_LABELS`): every regular file
  of the working tree (the handle's directory minus `.git`) by where the layout puts it -- under a topic's
  `sources/`: `source_images` (`.jpg .jpeg .png .webp .heic .heif .gif .bmp`, any case), `pdfs`,
  `other_sources` (transcriptions, sidecars, web pages...); a topic's `sessions/`,
  `conversations/`, `notes/`, `generated/`, `study/`; and `other` for everything else
  (`vault.yaml`, `topic.yaml`, `state/`, `review/`, ledgers, `subject.yaml`...).
  A user's path categorizes as the same path inside their folder -- `users/ana/subjects/...` is a
  topic's exactly as `subjects/...` is, because a topic's layout is the same under whichever user it
  lives -- and what no topic area covers is `other`, their `profile.json` and `photo.jpg` included.
  `VaultStats.categories` lists every category in that order (`CategorySize`: bytes, files);
  `largest_categories(n)` ranks the non-empty ones.
- **Subjects and topics**: `subjects` (`SubjectSize`, the whole `subjects/<slug>/` directory, each
  with its `topics` as `TopicSize`), largest first -- the subjects directly under the handle's
  `path`, which on a root handle of a vault with users is nothing, because a student's content is
  inside their folder.
- **Per user** (a root handle only): `users` (`UserSize(id, bytes, files, subjects)`: that user's
  whole folder, profile and photo included, with their own subjects and topics as above), largest
  first and ties by id; empty on a user's handle, whose `subjects` are that student's. One walk of
  the tree totals both the whole and each user's folder, so the per-user numbers are not a second
  read of the same files -- but they do not add up to `working_tree_bytes`, because what belongs to
  the repository itself (`vault.yaml`, `.gitattributes`, `feedback/inbox.jsonl`) is in the total and
  in the categories and in no user's entry.
- **Largest files**: `largest_files`, the `top` biggest (`FileSize`: path, bytes,
  category), largest first. The path is relative to the handle's `path`, so on a root handle it
  carries the `users/<id>/` prefix that says whose file it is, and on a user's handle it does not.
- **Git store**: `git` (`GitStoreSize`, from `git_store_size(vault)`), read from `.git/objects` on
  the filesystem: `pack_bytes`/`packs` (`objects/pack/*`, counting `.pack` files) and
  `loose_bytes`/`loose_objects` (`objects/xx/*`); `None` without a `.git` directory. The object
  store is the repository's, so it is read at `vault.root` and a user's handle reports the history
  its own content is committed into -- there is no `.git/` inside their folder, and every user's
  report therefore carries the same store, which is why the store is the repository's to decide
  about and not one student's.
- `working_tree_bytes`, `working_tree_files`, and the computed `git_bytes` and `total_bytes`
  (working tree + git store; what `doctor` compares with `[vault] size_warning_mb`, default 1024).

`studentassistant vault stats [--top N] [--json]` prints it as a Spanish table (total, per
category, per subject and topic, the N largest files) or, with `--json`, as the model's JSON, which
carries `users` since #547. The table reads `subjects`, which a root handle of a vault whose content
is inside user folders has none of, so its per-subject section is skipped and the largest files are
listed with the `users/<id>/...` paths that say whose they are; the per-user breakdown reaches the
table when the CLI is handed the user handles (#549 and later).
`studentassistant doctor`'s `Tamaño del vault` line is `ok` under the threshold and `aviso` over it,
naming the three categories that weigh most and pointing at `studentassistant purge` and the Git
LFS question (`install.doctor.check_vault_size`).

## Purge -- `purge.py`
`studentassistant purge [--topic <subject>/<topic>] [--dry-run] [--hard] [--yes]` applies a
per-topic retention policy (ADR-0003, issue #31) so the vault does not grow forever. Names are
imported from `studentassistant.vault.purge`.

A purge is planned and applied through the handle its sync has (#547), so a user's view of the sync
(`GitSync.for_user`) purges one student's topic and nothing of anybody else's: the topic is looked
for under their folder (`require_topic` on their handle), the notes version that says the notes were
accepted is one of *their* tags -- so a topic of another user with the same two slugs is a different
topic that stays skipped and untouched -- and the paths a plan reports are theirs, relative to
`users/<id>/` like every id the vault's readers give a caller, which are the paths that caller can
open. What git is asked is the repository's, because git runs at `vault.root`: every pathspec a
purge hands it -- a `git log` after a file's last commit, the paths a history rewrite drops --
carries the `users/<id>/` prefix, and what git answers with is turned back into the handle's paths
before a plan or a report shows it.

**Policy** -- `VaultPurgeSettings` (`studentassistant.config`, `[vault.purge]` /
`SA_VAULT__PURGE__*`):

| key | default | candidate |
|---|---|---|
| `require_notes_tag` | `true` | (eligibility) the topic has a notes tag `<subject-slug>/<topic-slug>/apuntes-vN` (`<user-id>/`-prefixed on a user's view) |
| `burst_originals` | `true` | `sources/<kind>/page-NNN.burst<K>.<ext>`, any kind: other stills |
| `observer_conversations` | `true` | `conversations/observer-<session-id>.jsonl` of an ended session |
| `folded_events` | `true` | the events the observer snapshot folded, replaced by that snapshot |
| `generated_max_age_days` | unset (keep) | files under `generated/` last committed longer ago |

Eligibility is per student: a user's view asks their own tags, so a topic whose notes another user
accepted under the same two slugs stays skipped for this one, and the Spanish skip reason names the
tag form it looked for
(`sus apuntes aún no están aceptados (no hay etiqueta <user-id>/<subject-slug>/<topic-slug>/apuntes-vN)`).

Burst originals come from capture processing as it was before #426 (`sources.store_capture`,
#44): it stored the stills it did not keep as derived files `burst<K>.<ext>` of the page, in
whichever `sources/<kind>/` the session was on, and the purge looks in every kind. Captures
stored since #426 keep only the chosen still, so `burst_originals` only matters for sessions
stored before that change; the page itself (`page-NNN.<ext>`), its sidecar, its crop and its transcription are never candidates.

**Never removed**: transcripts, `session.yaml`, `notes/` and its history (tags), sources other
than burst originals (a source the student removed, #451, is only marked `removed` in its
sidecar and is never a purge candidate either), the editor conversation, and anything `notes/apuntes.md` names by a
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
nothing. The CLI builds it from `observer.catchup.compactable_snapshot`, which stops at the
observer's newest acknowledged event, so the events the observer still owes (#176 catch-up) and
the newest `observer.ack` stay in the log. A compaction naming a session the topic does not list, or a `seq` its log lacks, is a
`PurgeError`. The events replaced are then only in git history: a later observer version cannot
refold them from the working tree.

**API**: `plan_topic_purge(sync: GitSync | UserGitSync, subject_slug, topic_slug, policy=None,
compaction=None, now=None) -> TopicPurgePlan` (`items`: `PurgeItem(path, reason, size_before,
size_after)` -- `size_after` `None` for a removal --, `protected`, `skipped`, `saved_bytes`) reads
only; its `path`s are the handle's, so a user's view plans inside that user's folder only and
reports the paths their caller opens.
`apply_purge(sync: GitSync | UserGitSync, plans, hard=False, before_commit=None) -> PurgeResult`
removes and rewrites
the files (the secret guard runs on every rewrite before anything is touched), calls
`before_commit(plan)` (the CLI refreshes the observer snapshot there) and commits everything as
one commit `purga: N archivos borrados, M registros compactados (size)`
(`PURGE_COMMIT_PREFIX`); no items, no commit. The `plans` are read as the handle `sync` has, so a
user's view writes and removes inside that user's folder only, while the commit and the push are
the repository's, as every sync's are. That is the soft purge: all of it is recoverable
from git history.

**`--hard`** (confirmed by typing `reescribir`, or `--yes`): after the soft commit,
`rewrite_history(sync: GitSync | UserGitSync, paths)` drops from every commit of `main` and from every tag
(`git filter-branch --index-filter ... --tag-name-filter cat`) every path a purge commit ever
deleted in the planned topics (`purged_history_paths(sync, topic_roots)`, which takes the
directories the handle gives and returns that handle's paths, so earlier soft purges are reclaimed
too),
deletes `refs/original/`, force-pushes `main` with `--force-with-lease` against the
remote-tracking branch and then every local tag with `--force-with-lease=refs/tags/<t>:<sha>`,
`<sha>` being what `git ls-remote --tags` gave before the rewrite (empty: the tag must still be
absent), so a tag another PC created or moved meanwhile makes the push fail instead of being
overwritten; a tag only the remote has is left as it is. Then it expires the reflog and runs
`gc --prune=now`; `HistoryRewrite` reports the object store size before and after, and its `paths`
are the handle's, so they read like the plan items they came from. The `paths` given are the
handle's and the pathspecs the filter is written with are the repository's (`users/<id>/...` for a
user's topic), because **the history a `--hard` purge rewrites is the one every user of the vault
shares**: it drops only the paths it was given, but it re-creates every tag on the new commits --
another user's notes tags included -- and force-pushes them under the leases it read. A user's
`--hard` purge is therefore a repository-wide operation, and the "stop the backend and tell every
other PC" consequence below is one for every student of the vault, not only for the one whose topic
was purged. The CLI first
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
- Several users, one vault (ADR-0002): separation of data, not security. There is no password, no
  role and no permission, and anyone who can reach the backend or the repository sees every user's
  folder. What a user handle guarantees is that a writer given one writes inside it, not that
  another user cannot be handed one.
- The profile photo is the only image work in this module: `users.py` decodes, downscales and
  re-encodes it with OpenCV, written there again rather than reused from `sources/captures.py`
  because the dependency runs the other way (`sources` imports `vault`, never the reverse).

## Tests
Against `tmp_vault` fixtures with a local bare repo as "remote"; never GitHub. Setup tests use
`tests/github_fakes.py`: a `LocalHost` over bare repositories under `tmp_path`, a fake `gh` script
put first on `PATH`, `file://` remote bases, and `SA_CONFIG` inside `tmp_path`.

The users of #546 are `tests/vault/test_vault_users.py` (profiles, ids, validation),
`test_user_photo.py` (uploads, downscale, removal), `test_user_handles.py` (`for_user` and the two
handles) and `test_user_isolation.py` (every writer through a user handle), and they share
`user_helpers.py`: `add_user`, which writes a `profile.json` straight to disk for the tests of the
handle itself, and `everything_under`, a before/after of the whole tree. No photo is a committed
fixture -- each one is generated in the test with OpenCV and NumPy, so the repository carries no
image file for them. The isolation test writes the whole layout twice, through two handles whose
subject and topic slugs are the same, and asserts that every path added landed under its own
`users/<id>/`, that the ids handed back are relative to that folder and read back, and that one id
gives each of the two users their own file. Since #547 it also asserts that the only thing a user
handle's writers add outside their folder is inside the repository's own `.git/`, and that all of
it is lock files under `studentassistant-locks/`: the locks are the repository's, so they are not
"a writer that escaped", and a test that counted them as one would pass on its own.

The repository-wide operations of #547 are three new files and four extended ones.
`test_sync_user_view.py`: that git runs in the repository root whichever handle the sync was given,
that a divergence of a user handle's sync is kept in the root's refs, that two users' versions of
one subject and topic count apart, that a view requires slugs too, that `read_file_at` reads a
user's file by its own path and reaches the content of before their folder (the `legacy_root_user`
#548 adds, written by hand here), that `revert_paths` undoes only the paths of the user that asks
and names a refused one as that user gave it, that a view shares the state, commits, push and loop
of its parent, that a sync given a user handle still gives every user's view, that a view needs a
user of the vault, and that the git lock a view holds is the repository's.
`test_index_user_view.py`: that a search over one user's index never answers with another user's
notes (the same word in both), that `user_index_path` names a database beside the configured one and
refuses an id that is not a user id, that a user's index lists only their own notes versions and is
keyed to the repository's HEAD, that a root handle rebuilds one database per user in `list_users`
order while a user handle rebuilds only the one it is given, that a unit a user's index cannot read
is named as theirs in the combined report, and that a vault with no users keeps the one database at
the path. `test_purge_user_view.py`: that a plan reports the paths of the user it was planned for,
that a topic is planned only when its own user accepted its notes, that a soft purge removes one
user's files and commits them as theirs, that generated material is aged by its last commit under a
user, and that a hard purge drops one user's files from the history they share -- reclaiming an
earlier soft purge of the same user. Extended: `test_cross_process_locks.py` (a user handle and the
root name one and the same lock, and a directory lock taken on the root bounds a user handle's
writer, both looking at the same lock file), `test_active_host.py` (a user's claim written at the
root and saying who it is, a claim on the root naming the user it is given, the warning naming the
student the other PC is capturing as, and a record from before there were users still reading and
warning), `test_feedback.py` (the inbox is the repository's and each item says who reported it, a
status change reaches an item whichever handle marks it, a context that already names a user keeps
it, and a line from before there were users still reads and folds) and `test_stats.py` (`categorize`
reads a user's path as the same path inside their folder, a root handle reports each user with their
own subjects, and a user handle measures their folder and the repository's store).

Format 2 and the migration of #548 changed what a test vault is, so the fixtures changed with it.
`tmp_vault` is born with its first user (`STUDENT_USER_ID`, the `slugify` of the `STUDENT` its
`vault.yaml` records, for the tests that become user-scoped in #549-#551) and hands out the ROOT
handle; most of the suite still writes its content through that handle, at the root, because nothing
in it hands out user handles yet -- a format-2 vault with content at its root is a test's, not a
state the product writes. `vault_with_no_users` takes that first user out again (`users/` removed),
for the tests that ask what a vault nobody belongs to says -- `list_users`, the id a first
`create_user` picks, a photo of a user that is not there -- and for the root-handle measurements of
`test_stats.py`, `test_index.py`, `test_index_user_view.py` and `test_user_photo.py`. `legacy_vault`
goes the rest of the way to a format-1 vault: a `vault.yaml` of the three fields that layout had and
no `legacy_root_user`, and the handle `open_for_migration` gives, since `open` refuses it.
`user_helpers.py` gained `write_everything`, which writes the whole layout through the public
writers, with `handle_relative` and `ledger_entry`; `test_user_isolation.py` and the nothing-lost
test share it.

`test_migrate.py`: that the root's content becomes the first user's and `vault.yaml` then says
format 2 naming them, that the whole migration is ONE commit named after the user, that the move is
a rename git's history follows, that every notes version is re-created under the user keeping its
number and its message, that a version tagged before the move still reads through the user's handle,
that a dry run says what would happen and changes nothing, that an already-migrated vault has
nothing to do, that an unended session refuses the migration and names it, that a conflict with the
remote refuses it and names the paths, that the migration pushes its commit and its new tags, that a
push which fails leaves the migration done and says so, that the name and email given are the first
user's and one that is not refuses it, that a vault with nothing at its root still becomes format 2
with its first user, that content written but not committed yet moves too, that a user handle or a
user's view of the sync is refused, and that an entry git cannot move is left where it is and named
in the report.

`test_migrate_nothing_lost.py` is the "nothing is lost" criterion, on a vault `write_everything`
built through the public writers alone -- two subjects, sessions with transcripts and events, every
source kind, notes with two tags, conversations, ledger, study and generated material: that every
file the root held is byte-identical under the user, that `git log --follow` of a moved
`notes/apuntes.md` reaches the commits it had, that every notes version keeps its number, its
message and the text `read_file_at` gives for its pre-migration tag, that the user's index finds the
search hits the root's index found, and that a dry run of a vault this full changes none of it.

Extended: `test_init_open.py` (a vault born with this format names no legacy user of its root and
`open` reads back the one a migration named, `init` creates the first user and stores the email it
was given, a name no user may be called by is refused in Spanish and leaves a vault `users add` can
still put right, `open` refuses a format-1 vault and names the command that migrates it, and
`open_for_migration` opens that one and refuses a vault of the format this backend writes and one it
cannot read at all), `test_models.py` (a meta nobody was moved into names no legacy user, one that
was says which, and a legacy meta reads back so that it can be migrated), `test_setup_clone.py` (a
clone of a format-1 vault succeeds and says how to migrate it, and a format-1 vault already on this
PC warns the same way) and `tests/install/test_doctor.py` (a format-1 vault is an `aviso` naming the
migration and the vault's path, not a `fallo`, and the checks that need a vault this backend may use
stop there). `tests/test_cli_vault_migrate_users.py` drives the command with a `SA_CONFIG` inside
`tmp_path`: that it moves the content, names the user and rebuilds the index; that `--dry-run` lists
the move and changes nothing; that an unended session refuses it in Spanish and exits 1; that
`--name` and `--email` are the first user's; that a vault with a remote is pushed with its new tags;
that a second run says there was nothing to do; that the help says to stop the backend first; and
that a vault that is not there exits 1.
