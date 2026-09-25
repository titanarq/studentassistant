"""Sessions: `sessions/<session-id>/` under a topic, its `session.yaml` and its two JSONL logs.

A session belongs to exactly one topic (ADR-0003). Starting one creates its directory with
`session.yaml`, an empty `transcript.jsonl` and an empty `events.jsonl`, and records its id in the
topic's `topic.yaml` `sessions` list, so the topic says which sessions fed it. The id is the
UTC start time as `YYYYMMDD-HHMMSS`; a second session started within the same second of the topic
takes the next free second, so the id keeps its pattern and stays unique and sortable.

A `Session` is the handle every append goes through. It assigns each log its own `seq`, from 1
and with no gaps, continuing from the highest `seq` already in the file when the session is
resumed, and computes `t` (milliseconds since `started_at`) for an event that does not bring its
own. Appends on one handle are serialised by a lock, so threads sharing it cannot hand out the
same `seq`; one handle per session is the caller's rule. This module writes files and never runs
git: committing a session is the git-sync task's job.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from yaml import YAMLError

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import read_yaml, write_text_atomic, write_yaml_atomic
from studentassistant.vault.jsonl import append_jsonl, last_seq, read_jsonl
from studentassistant.vault.session_models import (
    EVENT_SCHEMA_VERSION,
    SESSION_ID_FORMAT,
    SESSION_ID_PATTERN,
    Event,
    Origin,
    SessionKind,
    SessionMeta,
    TranscriptSegment,
    TranscriptWord,
)
from studentassistant.vault.topics import (
    TOPIC_FILE_NAME,
    get_topic,
    require_topic,
    topic_directory,
)
from studentassistant.vault.vault import Vault

SESSIONS_DIRNAME = "sessions"
SESSION_FILE_NAME = "session.yaml"
TRANSCRIPT_FILE_NAME = "transcript.jsonl"
EVENTS_FILE_NAME = "events.jsonl"


class SessionError(VaultError):
    """A session this backend cannot start, find, read or append to; the message says why."""


class NoOpenSessionError(SessionError):
    """The topic has no session whose `ended_at` is unset, so there is nothing to resume."""


class SessionEndedError(SessionError):
    """The session has ended: its logs take no more lines and it cannot end twice."""


class SessionNotFoundError(SessionError):
    """The topic lists no session with that id (or the id is not a session id at all)."""


class SessionFileError(SessionError):
    """`session.yaml` is missing, or holds something this backend cannot read as a session."""


@dataclass
class Session:
    """An open session: where it lives, its `session.yaml`, and the next `seq` of each log."""

    vault: Vault
    subject_slug: str
    topic_slug: str
    meta: SessionMeta
    _next_event_seq: int = field(repr=False)
    _next_transcript_seq: int = field(repr=False)
    _last_t: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def directory(self) -> Path:
        return sessions_directory(self.vault, self.subject_slug, self.topic_slug) / self.meta.id

    @property
    def events_path(self) -> Path:
        return self.directory / EVENTS_FILE_NAME

    @property
    def transcript_path(self) -> Path:
        return self.directory / TRANSCRIPT_FILE_NAME

    @property
    def ended(self) -> bool:
        return self.meta.ended_at is not None

    def append_event(
        self,
        kind: str,
        origin: Origin,
        payload: Mapping[str, Any] | None = None,
        t: int | None = None,
        schema_version: int = EVENT_SCHEMA_VERSION,
    ) -> Event:
        """Append one event to `events.jsonl` and return it as written, `seq` included.

        `t` defaults to the milliseconds elapsed since `started_at`, never less than the last `t`
        this handle wrote, so a wall clock stepping back cannot make the log's time go backwards.

        Raises:
            SessionEndedError: when the session has ended.
            SecretRefused: when the event carries something that looks like a key; the log and
                the next `seq` stay as they were.
            ValidationError: when the envelope is not a valid `Event` (a bad `origin`, say).
        """
        with self._lock:
            self._require_open()
            event_t = self._now_t() if t is None else t
            event = Event(
                seq=self._next_event_seq,
                t=event_t,
                origin=origin,
                kind=kind,
                schema_version=schema_version,
                payload=dict(payload or {}),
            )
            append_jsonl(self.events_path, event)
            self._next_event_seq += 1
            self._last_t = max(self._last_t, event_t)
            return event

    def append_transcript(
        self,
        t_start: int,
        t_end: int,
        text: str,
        words: Sequence[TranscriptWord | Mapping[str, Any]] | None = None,
    ) -> TranscriptSegment:
        """Append one final segment to `transcript.jsonl` and return it as written.

        Its `seq` is the transcript's own, numbered independently of the events.

        Raises:
            SessionEndedError: when the session has ended.
            SecretRefused: when the text looks like it carries a key; nothing is appended.
            ValidationError: when the span or the words are not a valid `TranscriptSegment`.
        """
        with self._lock:
            self._require_open()
            segment = TranscriptSegment.model_validate(
                {
                    "seq": self._next_transcript_seq,
                    "t_start": t_start,
                    "t_end": t_end,
                    "text": text,
                    "words": None
                    if words is None
                    else [
                        w.model_dump() if isinstance(w, TranscriptWord) else dict(w) for w in words
                    ],
                }
            )
            append_jsonl(self.transcript_path, segment)
            self._next_transcript_seq += 1
            return segment

    def read_events(self) -> Iterator[Event]:
        """Every complete event of the log, in the order it was written."""
        return read_jsonl(self.events_path, Event)

    def read_transcript(self) -> Iterator[TranscriptSegment]:
        """Every complete segment of the transcript, in the order it was written."""
        return read_jsonl(self.transcript_path, TranscriptSegment)

    def _require_open(self) -> None:
        if self.ended:
            raise SessionEndedError(f"session {self.meta.id} has ended and takes no more lines")

    def _now_t(self) -> int:
        elapsed = datetime.now(UTC) - self.meta.started_at
        return max(self._last_t, int(elapsed / timedelta(milliseconds=1)))


def sessions_directory(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """Where a topic's sessions live, whether or not it has one yet."""
    return topic_directory(vault, subject_slug, topic_slug) / SESSIONS_DIRNAME


def start_session(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    host: str,
    protocol_version: str,
    kind: SessionKind = "study",
) -> Session:
    """Start a new session of a topic and record it in the topic's `sessions` list.

    `kind` is written to `session.yaml`: `review` marks a session the backend opens only to hold
    events written outside a study session (see `SessionMeta`).

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read; nothing is written.
        OSError: when a directory or a file cannot be written.
    """
    stored = get_topic(vault, subject_slug, topic_slug)
    root = sessions_directory(vault, subject_slug, topic_slug)
    root.mkdir(exist_ok=True)
    started_at = datetime.now(UTC)
    candidate = started_at.replace(microsecond=0)
    while True:
        session_id = candidate.strftime(SESSION_ID_FORMAT)
        directory = root / session_id
        try:
            directory.mkdir()
        except FileExistsError:
            candidate += timedelta(seconds=1)
            continue
        break
    meta = SessionMeta(
        id=session_id,
        started_at=started_at,
        host=host,
        protocol_version=protocol_version,
        kind=kind,
    )
    write_yaml_atomic(directory / SESSION_FILE_NAME, meta)
    write_text_atomic(directory / TRANSCRIPT_FILE_NAME, "")
    write_text_atomic(directory / EVENTS_FILE_NAME, "")
    topic = stored.topic
    write_yaml_atomic(
        topic_directory(vault, subject_slug, topic_slug) / TOPIC_FILE_NAME,
        topic.model_copy(update={"sessions": [*topic.sessions, session_id]}),
    )
    return Session(
        vault=vault,
        subject_slug=subject_slug,
        topic_slug=topic_slug,
        meta=meta,
        _next_event_seq=1,
        _next_transcript_seq=1,
    )


def resume_session(vault: Vault, subject_slug: str, topic_slug: str) -> Session:
    """Reopen the latest session of the topic whose `ended_at` is unset.

    The handle continues each log from the highest `seq` already in it, and its default `t` from
    the last event's, so a resumed session never renumbers or rewinds what was written.

    Raises:
        NoOpenSessionError: when every session of the topic has ended, or it has none.
        SessionFileError: when a listed session's `session.yaml` cannot be read.
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read.
    """
    stored = get_topic(vault, subject_slug, topic_slug)
    root = sessions_directory(vault, subject_slug, topic_slug)
    for session_id in sorted(stored.topic.sessions, reverse=True):
        meta = _read_session_file(root / session_id / SESSION_FILE_NAME)
        if meta.ended_at is not None:
            continue
        directory = root / session_id
        events_path = directory / EVENTS_FILE_NAME
        last_t = max((event.t for event in read_jsonl(events_path, Event)), default=0)
        return Session(
            vault=vault,
            subject_slug=subject_slug,
            topic_slug=topic_slug,
            meta=meta,
            _next_event_seq=last_seq(events_path) + 1,
            _next_transcript_seq=last_seq(directory / TRANSCRIPT_FILE_NAME) + 1,
            _last_t=last_t,
        )
    raise NoOpenSessionError(
        f"the topic {topic_slug!r} of the subject {subject_slug!r} has no session to resume:"
        " every one of its sessions has ended, or it has none"
    )


def end_session(session: Session, ended_at: datetime | None = None) -> SessionMeta:
    """Record the end of `session` in its `session.yaml` and return the updated meta.

    After this the handle refuses appends, and `resume_session` no longer returns the session.

    Raises:
        SessionEndedError: when the session has ended already.
    """
    with session._lock:
        session._require_open()
        meta = session.meta.model_copy(update={"ended_at": ended_at or datetime.now(UTC)})
        write_yaml_atomic(session.directory / SESSION_FILE_NAME, meta)
        session.meta = meta
        return meta


def list_sessions(vault: Vault, subject_slug: str, topic_slug: str) -> list[SessionMeta]:
    """Every session of the topic, open or ended, ordered by session id; nothing is written.

    The sessions are the ones the topic's `sessions` list names, each read from its
    `session.yaml`; ids are UTC start times, so id order is start order.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read.
        SessionFileError: when a listed session's `session.yaml` cannot be read.
    """
    stored = get_topic(vault, subject_slug, topic_slug)
    root = sessions_directory(vault, subject_slug, topic_slug)
    return [
        _read_session_file(root / session_id / SESSION_FILE_NAME)
        for session_id in sorted(set(stored.topic.sessions))
    ]


def read_topic_events(
    vault: Vault, subject_slug: str, topic_slug: str
) -> Iterator[tuple[str, Event]]:
    """Yield `(session_id, event)` for every event of every session of the topic.

    Sessions come in id order and, within each one, events in `seq` order -- sorted rather than
    taken in file order, because a `merge=union` of `events.jsonl` may have reordered its lines.
    The topic and its sessions are read when iteration starts; a torn last line is left out, as
    `read_jsonl` does.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read.
        SessionFileError: when a listed session's `session.yaml` or `events.jsonl` is missing or
            cannot be read.
        JsonlError: when a complete line of an event log is not an `Event`.
    """
    root = sessions_directory(vault, subject_slug, topic_slug)
    for meta in list_sessions(vault, subject_slug, topic_slug):
        events_path = root / meta.id / EVENTS_FILE_NAME
        try:
            events = sorted(read_jsonl(events_path, Event), key=lambda event: event.seq)
        except FileNotFoundError as error:
            raise SessionFileError(
                f"{events_path} is missing, so the session {meta.id} has no event log"
            ) from error
        except OSError as error:
            raise SessionFileError(f"{events_path} cannot be read: {error}") from error
        for event in events:
            yield meta.id, event


def read_session_transcript(
    vault: Vault, subject_slug: str, topic_slug: str, session_id: str
) -> list[TranscriptSegment]:
    """Every complete segment of one listed session's transcript, open or ended, sorted by `seq`.

    Reads the files directly, without a `Session` handle, so nothing is opened for writing and
    nothing is written. A torn last line is left out, as `read_jsonl` does; the sort undoes any
    reordering a `merge=union` made.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read (a value that is not a slug is not found).
        SessionNotFoundError: when `session_id` is not a session id or the topic does not list it.
        SessionFileError: when the session's `session.yaml` or `transcript.jsonl` is missing or
            cannot be read.
        JsonlError: when a complete line of the transcript is not a `TranscriptSegment`.
    """
    stored = require_topic(vault, subject_slug, topic_slug)
    if _SESSION_ID.fullmatch(session_id) is None or session_id not in stored.topic.sessions:
        raise SessionNotFoundError(
            f"the topic {topic_slug!r} of the subject {subject_slug!r} has no session"
            f" {session_id!r}"
        )
    directory = sessions_directory(vault, subject_slug, topic_slug) / session_id
    _read_session_file(directory / SESSION_FILE_NAME)
    transcript_path = directory / TRANSCRIPT_FILE_NAME
    try:
        return sorted(
            read_jsonl(transcript_path, TranscriptSegment), key=lambda segment: segment.seq
        )
    except FileNotFoundError as error:
        raise SessionFileError(
            f"{transcript_path} is missing, so the session {session_id} has no transcript"
        ) from error
    except OSError as error:
        raise SessionFileError(f"{transcript_path} cannot be read: {error}") from error


_SESSION_ID = re.compile(SESSION_ID_PATTERN)


def _read_session_file(session_path: Path) -> SessionMeta:
    """Read `session.yaml`, naming in the error which of the ways it can be wrong it is."""
    try:
        return read_yaml(session_path, SessionMeta)
    except FileNotFoundError as error:
        raise SessionFileError(
            f"{session_path} is missing, so {session_path.parent} does not hold a session"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise SessionFileError(f"{session_path} cannot be read: {error}") from error
    except YAMLError as error:
        raise SessionFileError(
            f"{session_path} is not YAML this backend can parse: {error}"
        ) from error
    except ValidationError as error:
        raise SessionFileError(f"{session_path} does not hold a session: {error}") from error
