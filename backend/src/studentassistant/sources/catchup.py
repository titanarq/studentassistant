"""Catch-up: the stored pages of a topic whose transcription never reached the topic's event log.

A page is *owed* when a session of the topic stored it (`capture.stored`) but no
`page.transcribed` names it (by `capture_session_id` -- the session that stored the capture --
and `capture_id`), and no `page.transcription_failed` with reason `refused` does (a refusal would
only be refused again). An owed page is one of two things:

- *to transcribe*: it has no `page-NNN.md` (a backend restart or stop cut its job, or it was
  stored before a transcriber ran);
- *to record*: its `page-NNN.md` is there but its events are not (the job outlived its session's
  end hook, or the backend stopped between the write and the publish). Its events are published
  again from the stored Markdown.

`owed_pages` is pure; `read_owed` reads the topic through the vault (blocking: call it from a
worker thread). The transcriber (`transcriber.py`) runs both at every session start or resume and
once at server start.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    CAPTURE_ID_KEY,
    STATE_OP_EVENT_KIND,
    EventRef,
    TopicEvent,
)
from studentassistant.vault import (
    SourceError,
    SourceNotFoundError,
    Vault,
    read_source,
    read_topic_events,
)

PAGE_TRANSCRIBED_KIND = "page.transcribed"
PAGE_TRANSCRIPTION_FAILED_KIND = "page.transcription_failed"
CAPTURE_SESSION_KEY = "capture_session_id"
"""Payload key of `page.transcribed` / `page.transcription_failed`: the session of the capture."""

TRANSCRIPTION_SUFFIX = ".md"


@dataclass(frozen=True)
class PageRef:
    """One stored capture: the session that stored it and its `capture.stored` payload."""

    session_id: str
    capture_id: str
    source_path: str
    page_path: str | None = None
    t: int = 0
    source_kind: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.session_id, self.capture_id


@dataclass(frozen=True)
class RecordedPage:
    """An owed page whose transcription is stored: its Markdown and where it is."""

    page: PageRef
    text: str
    path: str


@dataclass
class Owed:
    """What `read_owed` found: pages to transcribe, pages to record, and the topic's pending ids
    (every `add_pending` id in the log, which must never be added twice)."""

    to_transcribe: list[PageRef] = field(default_factory=list)
    to_record: list[RecordedPage] = field(default_factory=list)
    pending_ids: set[str] = field(default_factory=set)


def transcription_path(source_path: str) -> str:
    """The `page-NNN.md` next to a stored page (`.../page-007.jpg` -> `.../page-007.md`)."""
    path = PurePosixPath(source_path)
    return str(path.with_name(path.name.split(".", 1)[0] + TRANSCRIPTION_SUFFIX))


def owed_pages(
    events: Iterable[TopicEvent],
    *,
    before: EventRef | None = None,
    sessions: Collection[str] | None = None,
) -> tuple[list[PageRef], set[str]]:
    """The captures of the topic without a recorded transcription, and the topic's pending ids.

    Captures are those of `sessions` (every session when `None`) stored before `before` (the
    event that opened the session: what comes after it reaches the transcriber on the bus); a
    repeated `capture_id` in one session counts once. Recorded transcriptions count wherever they
    are in the log.
    """
    captures: dict[tuple[str, str], PageRef] = {}
    recorded: set[tuple[str, str]] = set()
    pending_ids: set[str] = set()
    for session_id, event in events:
        payload = event.payload
        if event.kind == STATE_OP_EVENT_KIND:
            if payload.get("op") == "add_pending" and isinstance(payload.get("pending_id"), str):
                pending_ids.add(payload["pending_id"])
            continue
        capture_id = payload.get(CAPTURE_ID_KEY)
        if not isinstance(capture_id, str):
            continue
        if event.kind in (PAGE_TRANSCRIBED_KIND, PAGE_TRANSCRIPTION_FAILED_KIND):
            if event.kind == PAGE_TRANSCRIPTION_FAILED_KIND and payload.get("reason") != "refused":
                continue
            owner = payload.get(CAPTURE_SESSION_KEY)
            recorded.add((owner if isinstance(owner, str) else session_id, capture_id))
            continue
        if event.kind != CAPTURE_EVENT_KIND:
            continue
        if sessions is not None and session_id not in sessions:
            continue
        if before is not None and session_id == before.session_id and event.seq >= before.seq:
            continue
        source_path = payload.get("source_path")
        if not isinstance(source_path, str) or (session_id, capture_id) in captures:
            continue
        page_path = payload.get("page_path")
        kind = payload.get("source_context")
        captures[(session_id, capture_id)] = PageRef(
            session_id=session_id,
            capture_id=capture_id,
            source_path=source_path,
            page_path=page_path if isinstance(page_path, str) else None,
            t=event.t,
            source_kind=kind if isinstance(kind, str) else None,
        )
    owed = [page for key, page in captures.items() if key not in recorded]
    return owed, pending_ids


def read_owed(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    before: EventRef | None = None,
    sessions: Collection[str] | None = None,
) -> Owed:
    """`owed_pages` of the topic's log, split by whether `page-NNN.md` is stored (blocking).

    Raises what `read_topic_events` raises for a topic or a session it cannot read.
    """
    pages, pending_ids = owed_pages(
        read_topic_events(vault, subject_slug, topic_slug), before=before, sessions=sessions
    )
    owed = Owed(pending_ids=pending_ids)
    for page in pages:
        path = transcription_path(page.source_path)
        try:
            content = read_source(vault, path).content
        except SourceNotFoundError:
            owed.to_transcribe.append(page)
            continue
        except SourceError:  # a path that is not a stored page: nothing to transcribe
            continue
        text = content.decode("utf-8", errors="replace").strip()
        if text:
            owed.to_record.append(RecordedPage(page=page, text=text, path=path))
        else:
            owed.to_transcribe.append(page)
    return owed


__all__ = [
    "CAPTURE_SESSION_KEY",
    "PAGE_TRANSCRIBED_KIND",
    "PAGE_TRANSCRIPTION_FAILED_KIND",
    "Owed",
    "PageRef",
    "RecordedPage",
    "owed_pages",
    "read_owed",
    "transcription_path",
]
