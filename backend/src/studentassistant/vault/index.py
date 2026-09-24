"""The derived SQLite index: listings and FTS5 search over the vault, rebuildable from it alone.

ADR-0002 makes the vault the source of truth and this database a cache under
`~/.cache/studentassistant/` (`[vault] index_path`): everything in it is read from vault files
and can be thrown away at any time. `VaultIndex.rebuild()` recreates it from scratch;
`VaultIndex.update()` brings it up to date incrementally, re-reading only what changed since the
last update; `VaultIndex.open()` rebuilds on its own when the index was built for another vault,
by another schema, or at another git HEAD than the vault's (a clone, a pull), and otherwise
updates.

What is indexed is grouped in *units*, each a small set of vault files read together:

- `subjects/<s>/subject.yaml` -- a subject;
- `.../topics/<t>/topic.yaml` -- a topic;
- `.../sessions/<id>/session.yaml` and `transcript.jsonl` -- a session and its final segments
  (`events.jsonl` is not indexed: nothing listed or searched comes from it);
- `.../sources/<kind>/*` -- the sources of one kind, their sidecars and page transcriptions;
- `.../notes/apuntes.md` -- the master notes;
- `.../review/pending.yaml` -- the pending-review items.

The index remembers a fingerprint (mtime, size, inode) of every file it read; `update()` scans
the tree, and each unit one of whose files appeared, disappeared or changed is deleted from the
index and read again. A file this backend cannot read does not fail the index: its unit is left
out and reported (`IndexReport.skipped`). Notes versions come from the git tags
`<topic-slug>/apuntes-vN` and are re-listed on every update.

Search is FTS5 with the `unicode61` tokenizer removing diacritics, so `fotosintesis` finds
`fotosíntesis`. Every hit names the vault-relative file it came from (and, for a transcript, the
segment's `seq`), which is what the web opens. Nothing here writes a vault file; git is run
read-only (`rev-parse`, `tag --list`) through `GitRunner`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from yaml import YAMLError, safe_load

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import read_yaml
from studentassistant.vault.git import GitIdentity, GitRunner
from studentassistant.vault.jsonl import read_jsonl
from studentassistant.vault.models import Subject, Topic
from studentassistant.vault.notes import NOTES_DIRNAME, NOTES_FILE_NAME
from studentassistant.vault.session_models import SESSION_ID_PATTERN, SessionMeta, TranscriptSegment
from studentassistant.vault.sessions import (
    SESSION_FILE_NAME,
    SESSIONS_DIRNAME,
    TRANSCRIPT_FILE_NAME,
)
from studentassistant.vault.slugs import is_slug
from studentassistant.vault.sources import (
    PAGED_KINDS,
    SOURCE_KINDS,
    SOURCES_DIRNAME,
    _read_sidecar,
    _sidecar_of,
    _source_entries,
)
from studentassistant.vault.subjects import SUBJECT_FILE_NAME, SUBJECTS_DIRNAME
from studentassistant.vault.sync import NOTES_TAG_SUFFIX
from studentassistant.vault.topics import TOPIC_FILE_NAME, TOPICS_DIRNAME
from studentassistant.vault.vault import Vault

logger = logging.getLogger(__name__)

# Bumped whenever the tables change: an index of another schema is rebuilt, never migrated.
INDEX_SCHEMA_VERSION = 1

REVIEW_DIRNAME = "review"
PENDING_FILE_NAME = "pending.yaml"

# What `SearchHit.snippet` puts around each matched term: control characters no vault text holds,
# so the web can split on them and highlight without parsing (or trusting) any markup.
SNIPPET_START = "\x02"
SNIPPET_END = "\x03"
SNIPPET_ELLIPSIS = "…"
SNIPPET_TOKENS = 16

# The kinds of searchable document.
DOC_NOTES = "notes"  # notes/apuntes.md
DOC_PAGE = "page"  # sources/{notes,book,pdf}/page-NNN.md: a page transcription
DOC_WEB = "web"  # sources/web/NNN-<slug>.md
DOC_TRANSCRIPT = "transcript"  # one final segment of sessions/<id>/transcript.jsonl
DOC_KINDS: tuple[str, ...] = (DOC_NOTES, DOC_PAGE, DOC_WEB, DOC_TRANSCRIPT)

_SESSION_ID = re.compile(SESSION_ID_PATTERN)
_PAGE_TRANSCRIPTION = re.compile(r"^page-(\d{3,})\.md$")
_QUERY_TERM = re.compile(r"\w+")
_NOTES_TAG = re.compile(rf"^([a-z0-9]+(?:-[a-z0-9]+)*)/{NOTES_TAG_SUFFIX}([1-9][0-9]*)$")
# The identity `GitRunner` wants; the index only reads, so it never authors anything.
_READER = GitIdentity(name="studentassistant index", email="index@studentassistant.invalid")
_GIT_TIMEOUT_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files (path TEXT PRIMARY KEY, unit TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE INDEX files_unit ON files (unit);
CREATE TABLE subjects (unit TEXT NOT NULL, slug TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE topics (
    unit TEXT NOT NULL, subject TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL,
    fidelity_mode TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (subject, slug)
);
CREATE TABLE sessions (
    unit TEXT NOT NULL, subject TEXT NOT NULL, topic TEXT NOT NULL, id TEXT NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT, host TEXT NOT NULL,
    PRIMARY KEY (subject, topic, id)
);
CREATE TABLE sources (
    unit TEXT NOT NULL, subject TEXT NOT NULL, topic TEXT NOT NULL, kind TEXT NOT NULL,
    number INTEGER NOT NULL, path TEXT PRIMARY KEY, meta TEXT
);
CREATE INDEX sources_unit ON sources (unit);
CREATE TABLE pending (
    unit TEXT NOT NULL, subject TEXT NOT NULL, topic TEXT NOT NULL, position INTEGER NOT NULL,
    item TEXT NOT NULL, PRIMARY KEY (subject, topic, position)
);
CREATE TABLE note_versions (
    topic TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, commit_id TEXT NOT NULL,
    PRIMARY KEY (topic, version)
);
CREATE VIRTUAL TABLE docs USING fts5 (
    body, kind UNINDEXED, path UNINDEXED, source UNINDEXED, subject UNINDEXED, topic UNINDEXED,
    session UNINDEXED, seq UNINDEXED, t_start UNINDEXED, unit UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""
_TABLES = (
    "meta",
    "files",
    "subjects",
    "topics",
    "sessions",
    "sources",
    "pending",
    "note_versions",
    "docs",
)
_UNIT_TABLES = ("subjects", "topics", "sessions", "sources", "pending", "docs")


class VaultIndexError(VaultError):
    """The index database cannot be opened or written; the message says why."""


@dataclass(frozen=True)
class IndexedSubject:
    slug: str
    name: str


@dataclass(frozen=True)
class IndexedTopic:
    subject: str
    slug: str
    title: str
    fidelity_mode: str
    created_at: str


@dataclass(frozen=True)
class IndexedSession:
    subject: str
    topic: str
    id: str
    started_at: str
    ended_at: str | None
    host: str


@dataclass(frozen=True)
class IndexedSource:
    """One stored source, named by its vault-relative path (what `read_source` takes)."""

    subject: str
    topic: str
    kind: str
    path: str
    meta: dict[str, Any] | None


@dataclass(frozen=True)
class PendingItem:
    """One entry of a topic's `review/pending.yaml`, as the file holds it (its schema is the
    observer's)."""

    subject: str
    topic: str
    position: int
    item: Any


@dataclass(frozen=True)
class NoteVersion:
    """One notes version tag `<topic-slug>/apuntes-v<version>`."""

    topic: str
    version: int
    name: str
    commit: str


@dataclass(frozen=True)
class SearchHit:
    """One search result.

    `path` is the vault-relative file the text is in; `source` is the source that file belongs
    to (for a page transcription, the page image `read_source` serves; for a web page, itself;
    `None` for notes and transcripts). A transcript hit also names its `session`, the segment's
    `seq` and its `t_start` in session milliseconds. `snippet` marks every matched term between
    `SNIPPET_START` and `SNIPPET_END`.
    """

    kind: str
    path: str
    source: str | None
    subject: str
    topic: str
    session: str | None
    seq: int | None
    t_start: int | None
    snippet: str


@dataclass(frozen=True)
class IndexReport:
    """What a `rebuild()` or `update()` did: units re-read, and those left out with the reason."""

    rebuilt: bool
    units_indexed: int
    units_removed: int
    documents: int
    skipped: tuple[tuple[str, str], ...] = ()


@dataclass
class _Rows:
    """Rows one unit contributes, collected before anything is written."""

    subjects: list[tuple[Any, ...]] = field(default_factory=list)
    topics: list[tuple[Any, ...]] = field(default_factory=list)
    sessions: list[tuple[Any, ...]] = field(default_factory=list)
    sources: list[tuple[Any, ...]] = field(default_factory=list)
    pending: list[tuple[Any, ...]] = field(default_factory=list)
    docs: list[tuple[Any, ...]] = field(default_factory=list)


class VaultIndex:
    """The SQLite index of one vault. Thread-safe: every call holds one lock over one connection."""

    def __init__(self, vault: Vault, path: Path, connection: sqlite3.Connection) -> None:
        self.vault = vault
        self.path = path
        self._connection = connection
        self._lock = threading.RLock()
        self._git = GitRunner(vault.path, _READER, timeout=_GIT_TIMEOUT_SECONDS)

    # -- opening ---------------------------------------------------------------------------------

    @classmethod
    def open(cls, vault: Vault, path: Path) -> VaultIndex:
        """Open (creating it and its parents if needed) the index at `path` and make it current.

        It is rebuilt from scratch when it is new, unreadable as SQLite, of another schema, built
        for another vault or at another HEAD than the vault's; otherwise it is updated.

        Raises:
            VaultIndexError: when the database cannot be created at `path`.
        """
        index = cls(vault, path, _connect(path))
        try:
            index.refresh()
        except sqlite3.DatabaseError:
            # Not a database this SQLite can use: it is a cache, so start it again.
            index.close()
            _discard(path)
            index = cls(vault, path, _connect(path))
            index.rebuild()
        return index

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> VaultIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- keeping it current ----------------------------------------------------------------------

    def is_current(self) -> bool:
        """Whether the index was built by this schema, for this vault, at the vault's HEAD."""
        with self._lock:
            meta = self._meta()
        return (
            meta.get("schema_version") == str(INDEX_SCHEMA_VERSION)
            and meta.get("vault_path") == str(self.vault.path.resolve())
            and meta.get("head", "") == (self._head() or "")
        )

    def refresh(self) -> IndexReport:
        """`rebuild()` when `is_current()` is false, `update()` otherwise."""
        if self.is_current():
            return self.update()
        return self.rebuild()

    def rebuild(self) -> IndexReport:
        """Throw every table away and index the whole vault again."""
        with self._lock:
            connection = self._connection
            with connection:
                for table in _TABLES:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in _statements(_SCHEMA):
                    connection.execute(statement)
                connection.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(INDEX_SCHEMA_VERSION)),
                        ("vault_path", str(self.vault.path.resolve())),
                    ],
                )
                report = self._update_locked(rebuilt=True)
            return report

    def update(self) -> IndexReport:
        """Re-read every unit one of whose files changed since the last update, and the tags.

        Cheap when nothing changed: one walk of `subjects/` comparing fingerprints and one
        `git tag --list`. Meant to be called after vault writes (or on a timer, `run()`).
        """
        with self._lock:
            if not self._meta():
                return self.rebuild()
            with self._connection:
                return self._update_locked(rebuilt=False)

    def _update_locked(self, *, rebuilt: bool) -> IndexReport:
        connection = self._connection
        on_disk = dict(_scan(self.vault.path))
        stored = {
            path: (unit, fingerprint)
            for path, unit, fingerprint in connection.execute(
                "SELECT path, unit, fingerprint FROM files"
            )
        }
        dirty: set[str] = set()
        for path, (unit, fingerprint) in on_disk.items():
            if stored.get(path) != (unit, fingerprint):
                dirty.add(unit)
        for path, (unit, _) in stored.items():
            if path not in on_disk:
                dirty.add(unit)

        by_unit: dict[str, list[str]] = {}
        for path, (unit, _) in on_disk.items():
            by_unit.setdefault(unit, []).append(path)

        skipped: list[tuple[str, str]] = []
        indexed = removed = 0
        for unit in sorted(dirty):
            for table in (*_UNIT_TABLES, "files"):
                connection.execute(f"DELETE FROM {table} WHERE unit = ?", (unit,))
            paths = sorted(by_unit.get(unit, []))
            if not paths:
                removed += 1
                continue
            try:
                rows = _index_unit(self.vault.path, unit)
            except (VaultError, OSError, UnicodeDecodeError, ValidationError, YAMLError) as error:
                reason = str(error).splitlines()[0] if str(error) else type(error).__name__
                logger.warning("index: %s left out: %s", unit, reason)
                skipped.append((unit, reason))
                rows = _Rows()
            self._insert(unit, rows)
            connection.executemany(
                "INSERT INTO files (path, unit, fingerprint) VALUES (?, ?, ?)",
                [(path, unit, on_disk[path][1]) for path in paths],
            )
            indexed += 1

        connection.execute("DELETE FROM note_versions")
        connection.executemany(
            "INSERT OR REPLACE INTO note_versions (topic, version, name, commit_id)"
            " VALUES (?, ?, ?, ?)",
            [(tag.topic, tag.version, tag.name, tag.commit) for tag in self._tags()],
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('head', ?)", (self._head() or "",)
        )
        documents = connection.execute("SELECT count(*) FROM docs").fetchone()[0]
        return IndexReport(
            rebuilt=rebuilt,
            units_indexed=indexed,
            units_removed=removed,
            documents=documents,
            skipped=tuple(skipped),
        )

    def _insert(self, unit: str, rows: _Rows) -> None:
        connection = self._connection
        connection.executemany(
            "INSERT OR REPLACE INTO subjects VALUES (?, ?, ?)", [(unit, *r) for r in rows.subjects]
        )
        connection.executemany(
            "INSERT OR REPLACE INTO topics VALUES (?, ?, ?, ?, ?, ?)",
            [(unit, *r) for r in rows.topics],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(unit, *r) for r in rows.sessions],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(unit, *r) for r in rows.sources],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?, ?)",
            [(unit, *r) for r in rows.pending],
        )
        connection.executemany(
            "INSERT INTO docs (body, kind, path, source, subject, topic, session, seq, t_start,"
            " unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r, unit) for r in rows.docs],
        )

    async def run(self, interval: float = 5.0) -> None:
        """`update()` in a worker thread every `interval` seconds, until cancelled."""
        while True:
            try:
                await asyncio.to_thread(self.update)
            except (sqlite3.Error, VaultError, OSError) as error:
                logger.warning("index update failed: %s", error)
            await asyncio.sleep(interval)

    # -- git -------------------------------------------------------------------------------------

    def _head(self) -> str | None:
        result = self._git.run("rev-parse", "--verify", "--quiet", "HEAD")
        return result.stdout.strip() or None if result.ok else None

    def _tags(self) -> list[NoteVersion]:
        result = self._git.run(
            "tag",
            "--list",
            f"*/{NOTES_TAG_SUFFIX}*",
            "--format=%(refname:short)%09%(*objectname)%09%(objectname)",
        )
        tags = []
        for line in result.stdout.splitlines() if result.ok else []:
            name, peeled, target = (line.split("\t") + ["", ""])[:3]
            match = _NOTES_TAG.match(name)
            if match:
                tags.append(
                    NoteVersion(
                        topic=match[1], version=int(match[2]), name=name, commit=peeled or target
                    )
                )
        return tags

    def _meta(self) -> dict[str, str]:
        try:
            return dict(self._connection.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.OperationalError:  # no such table: a new database
            return {}

    # -- listings --------------------------------------------------------------------------------

    def _query(self, sql: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchall()

    def subjects(self) -> list[IndexedSubject]:
        """Every subject, by slug."""
        return [
            IndexedSubject(slug=r["slug"], name=r["name"])
            for r in self._query("SELECT slug, name FROM subjects ORDER BY slug")
        ]

    def topics(self, subject: str | None = None) -> list[IndexedTopic]:
        """Every topic (of `subject` when given), by subject then slug."""
        where, parameters = _filters(subject=subject)
        return [
            IndexedTopic(
                subject=r["subject"],
                slug=r["slug"],
                title=r["title"],
                fidelity_mode=r["fidelity_mode"],
                created_at=r["created_at"],
            )
            for r in self._query(
                "SELECT subject, slug, title, fidelity_mode, created_at FROM topics"
                f"{where} ORDER BY subject, slug",
                parameters,
            )
        ]

    def sessions(
        self, subject: str | None = None, topic: str | None = None
    ) -> list[IndexedSession]:
        """Every session with a readable `session.yaml`, by subject, topic then id."""
        where, parameters = _filters(subject=subject, topic=topic)
        return [
            IndexedSession(
                subject=r["subject"],
                topic=r["topic"],
                id=r["id"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                host=r["host"],
            )
            for r in self._query(
                "SELECT subject, topic, id, started_at, ended_at, host FROM sessions"
                f"{where} ORDER BY subject, topic, id",
                parameters,
            )
        ]

    def sources(self, subject: str | None = None, topic: str | None = None) -> list[IndexedSource]:
        """Every source, as `list_sources` lists them: by subject, topic, kind then number."""
        where, parameters = _filters(subject=subject, topic=topic)
        rows = self._query(
            f"SELECT subject, topic, kind, number, path, meta FROM sources{where}", parameters
        )
        kind_order = {kind: position for position, kind in enumerate(SOURCE_KINDS)}
        rows.sort(
            key=lambda r: (r["subject"], r["topic"], kind_order[r["kind"]], r["number"], r["path"])
        )
        return [
            IndexedSource(
                subject=r["subject"],
                topic=r["topic"],
                kind=r["kind"],
                path=r["path"],
                meta=None if r["meta"] is None else json.loads(r["meta"]),
            )
            for r in rows
        ]

    def pending(self, subject: str | None = None, topic: str | None = None) -> list[PendingItem]:
        """Every pending-review entry, by subject, topic then position in the file."""
        where, parameters = _filters(subject=subject, topic=topic)
        return [
            PendingItem(
                subject=r["subject"],
                topic=r["topic"],
                position=r["position"],
                item=json.loads(r["item"]),
            )
            for r in self._query(
                f"SELECT subject, topic, position, item FROM pending{where}"
                " ORDER BY subject, topic, position",
                parameters,
            )
        ]

    def note_versions(self, topic: str | None = None) -> list[NoteVersion]:
        """Every notes version tag (of the topic slug when given), by topic then version."""
        where, parameters = _filters(topic=topic)
        return [
            NoteVersion(
                topic=r["topic"], version=r["version"], name=r["name"], commit=r["commit_id"]
            )
            for r in self._query(
                f"SELECT topic, version, name, commit_id FROM note_versions{where}"
                " ORDER BY topic, version",
                parameters,
            )
        ]

    # -- search ----------------------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        subject: str | None = None,
        topic: str | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """The best `limit` matches of `query` in notes, transcriptions, web pages, transcripts.

        `query` is plain text, never FTS5 syntax: each word must appear (as a prefix, accents
        and case ignored). A query without a single word matches nothing. Hits are ranked by
        BM25, ties broken by path then `seq`, so the same vault always answers the same list.

        Raises:
            ValueError: when a kind is not one of `DOC_KINDS` or `limit` is not positive.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        chosen = tuple(kinds) if kinds is not None else DOC_KINDS
        unknown = [kind for kind in chosen if kind not in DOC_KINDS]
        if unknown:
            raise ValueError(f"unknown document kinds {unknown}; they are {', '.join(DOC_KINDS)}")
        terms = _QUERY_TERM.findall(query)
        if not terms or not chosen:
            return []
        match = " ".join('"' + term.replace('"', "") + '"*' for term in terms)
        where, parameters = _filters(subject=subject, topic=topic)
        conditions = where.replace(" WHERE ", " AND ")
        placeholders = ", ".join("?" for _ in chosen)
        rows = self._query(
            "SELECT kind, path, source, subject, topic, session, seq, t_start,"
            f" snippet(docs, 0, ?, ?, ?, {SNIPPET_TOKENS}) AS snippet"
            f" FROM docs WHERE docs MATCH ? AND kind IN ({placeholders}){conditions}"
            " ORDER BY bm25(docs), path, seq LIMIT ?",
            [SNIPPET_START, SNIPPET_END, SNIPPET_ELLIPSIS, match, *chosen, *parameters, limit],
        )
        return [
            SearchHit(
                kind=r["kind"],
                path=r["path"],
                source=r["source"],
                subject=r["subject"],
                topic=r["topic"],
                session=r["session"],
                seq=r["seq"],
                t_start=r["t_start"],
                snippet=r["snippet"],
            )
            for r in rows
        ]


def rebuild_index(vault: Vault, path: Path) -> IndexReport:
    """Recreate the index at `path` from `vault` alone (what `studentassistant index rebuild`
    runs); a file at `path` that is not a usable database is replaced."""
    try:
        index = VaultIndex(vault, path, _connect(path))
        with index:
            return index.rebuild()
    except sqlite3.DatabaseError:
        _discard(path)
        with VaultIndex(vault, path, _connect(path)) as index:
            return index.rebuild()


# -- the database ----------------------------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    except (OSError, sqlite3.Error) as error:
        raise VaultIndexError(f"cannot open the index at {path}: {error}") from error
    connection.row_factory = sqlite3.Row
    # Python's own transaction handling is off (isolation_level=None): `with connection` below
    # would not open one, so do it explicitly through this adapter.
    return _Transactional(connection)  # type: ignore[return-value]


class _Transactional:
    """A connection whose `with` block is one explicit transaction (BEGIN ... COMMIT/ROLLBACK)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._depth = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self) -> sqlite3.Connection:
        if self._depth == 0:
            self._connection.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self._connection

    def __exit__(self, exc_type: object, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._connection.execute("ROLLBACK" if exc_type is not None else "COMMIT")


def _discard(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def _filters(**columns: str | None) -> tuple[str, list[str]]:
    given = [(column, value) for column, value in columns.items() if value is not None]
    if not given:
        return "", []
    return " WHERE " + " AND ".join(f"{column} = ?" for column, _ in given), [
        value for _, value in given
    ]


# -- which files, which units --------------------------------------------------------------------


def _scan(root: Path) -> Iterator[tuple[str, tuple[str, str]]]:
    """`(vault-relative path, (unit, fingerprint))` of every file the index reads."""
    subjects = root / SUBJECTS_DIRNAME
    if subjects.is_symlink() or not subjects.is_dir():
        return
    for directory, subdirectories, files in subjects.walk():
        subdirectories[:] = sorted(d for d in subdirectories if not (directory / d).is_symlink())
        for name in sorted(files):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            unit = _unit_of(relative)
            if unit is None or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            yield relative, (unit, f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}")


def _unit_of(relative: str) -> str | None:
    """The unit a vault-relative path belongs to, or `None` when the index does not read it."""
    parts = relative.split("/")
    if len(parts) < 3 or parts[0] != SUBJECTS_DIRNAME or not is_slug(parts[1]):
        return None
    subject = parts[1]
    if len(parts) == 3:
        return f"subject/{subject}" if parts[2] == SUBJECT_FILE_NAME else None
    if parts[2] != TOPICS_DIRNAME or len(parts) < 5 or not is_slug(parts[3]):
        return None
    topic = parts[3]
    rest = parts[4:]
    if rest == [TOPIC_FILE_NAME]:
        return f"topic/{subject}/{topic}"
    if (
        len(rest) == 3
        and rest[0] == SESSIONS_DIRNAME
        and _SESSION_ID.fullmatch(rest[1])
        and rest[2] in (SESSION_FILE_NAME, TRANSCRIPT_FILE_NAME)
    ):
        return f"session/{subject}/{topic}/{rest[1]}"
    if len(rest) == 3 and rest[0] == SOURCES_DIRNAME and rest[1] in SOURCE_KINDS:
        return f"sources/{subject}/{topic}/{rest[1]}"
    if rest == [NOTES_DIRNAME, NOTES_FILE_NAME]:
        return f"notes/{subject}/{topic}"
    if rest == [REVIEW_DIRNAME, PENDING_FILE_NAME]:
        return f"pending/{subject}/{topic}"
    return None


def _index_unit(root: Path, unit: str) -> _Rows:
    """Read one unit's files into rows. Raises what reading them raises."""
    kind, _, rest = unit.partition("/")
    names = rest.split("/")
    subject = names[0]
    subject_dir = root / SUBJECTS_DIRNAME / subject
    rows = _Rows()
    if kind == "subject":
        stored = read_yaml(subject_dir / SUBJECT_FILE_NAME, Subject)
        rows.subjects.append((subject, stored.name))
        return rows
    topic = names[1]
    topic_dir = subject_dir / TOPICS_DIRNAME / topic
    if kind == "topic":
        stored_topic = read_yaml(topic_dir / TOPIC_FILE_NAME, Topic)
        rows.topics.append(
            (
                subject,
                topic,
                stored_topic.title,
                stored_topic.fidelity_mode,
                stored_topic.created_at.isoformat(),
            )
        )
    elif kind == "session":
        _index_session(root, topic_dir / SESSIONS_DIRNAME / names[2], subject, topic, rows)
    elif kind == "sources":
        _index_sources(root, topic_dir / SOURCES_DIRNAME / names[2], subject, topic, rows)
    elif kind == "notes":
        path = topic_dir / NOTES_DIRNAME / NOTES_FILE_NAME
        rows.docs.append(
            (_text(path), DOC_NOTES, _rel(root, path), None, subject, topic, None, None, None)
        )
    elif kind == "pending":
        path = topic_dir / REVIEW_DIRNAME / PENDING_FILE_NAME
        for position, item in enumerate(_pending_items(path)):
            rows.pending.append((subject, topic, position, _json(item)))
    return rows


def _index_session(root: Path, directory: Path, subject: str, topic: str, rows: _Rows) -> None:
    session_id = directory.name
    session_file = directory / SESSION_FILE_NAME
    if session_file.is_file():
        meta = read_yaml(session_file, SessionMeta)
        rows.sessions.append(
            (
                subject,
                topic,
                session_id,
                meta.started_at.isoformat(),
                None if meta.ended_at is None else meta.ended_at.isoformat(),
                meta.host,
            )
        )
    transcript = directory / TRANSCRIPT_FILE_NAME
    if transcript.is_file():
        relative = _rel(root, transcript)
        for segment in sorted(read_jsonl(transcript, TranscriptSegment), key=lambda s: s.seq):
            rows.docs.append(
                (
                    segment.text,
                    DOC_TRANSCRIPT,
                    relative,
                    None,
                    subject,
                    topic,
                    session_id,
                    segment.seq,
                    segment.t_start,
                )
            )


def _index_sources(root: Path, directory: Path, subject: str, topic: str, rows: _Rows) -> None:
    kind = directory.name
    if directory.is_symlink() or not directory.is_dir():
        return
    originals: dict[str, str] = {}
    for number, entry in sorted(_source_entries(directory, kind), key=lambda e: (e[0], e[1].name)):
        relative = _rel(root, entry)
        meta = _read_sidecar(_sidecar_of(entry))
        rows.sources.append(
            (subject, topic, kind, number, relative, None if meta is None else _json(meta))
        )
        originals.setdefault(entry.name.split(".", 1)[0], relative)
        if kind not in PAGED_KINDS:
            rows.docs.append(
                (_text(entry), DOC_WEB, relative, relative, subject, topic, None, None, None)
            )
    if kind not in PAGED_KINDS:
        return
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_file() or not _PAGE_TRANSCRIPTION.match(entry.name):
            continue
        relative = _rel(root, entry)
        source = originals.get(entry.name.split(".", 1)[0], relative)
        rows.docs.append(
            (_text(entry), DOC_PAGE, relative, source, subject, topic, None, None, None)
        )


def _pending_items(path: Path) -> list[Any]:
    """The entries of `review/pending.yaml`: a top-level list, or the `items` list of a mapping."""
    data = safe_load(_text(path))
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        raise VaultIndexError(f"{path} holds neither a list nor a mapping with an `items` list")
    return data


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = [
    "DOC_KINDS",
    "DOC_NOTES",
    "DOC_PAGE",
    "DOC_TRANSCRIPT",
    "DOC_WEB",
    "INDEX_SCHEMA_VERSION",
    "SNIPPET_END",
    "SNIPPET_START",
    "IndexReport",
    "IndexedSession",
    "IndexedSource",
    "IndexedSubject",
    "IndexedTopic",
    "NoteVersion",
    "PendingItem",
    "SearchHit",
    "VaultIndex",
    "VaultIndexError",
    "rebuild_index",
]
