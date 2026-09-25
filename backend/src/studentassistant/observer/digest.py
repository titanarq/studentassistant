"""The topic digest: a compact Spanish summary of a topic, regenerated at every session end.

`state/digest.md` says what a topic holds so far: a one-paragraph summary (the excerpt the phone
and the web show), the outline, what each session covered (sections worked on, new concepts,
pages captured, the source used, doubts raised and settled, the observer's last remarks) and the
open doubts. It is how a topic is resumed another day ("continúa el tema"): the live observer puts
it in its cached prefix (`render_topic`) when it opens a session, and the editor reads it as input.

`render_digest(subject_name, topic_title, events, state, timezone=...)` is pure and deterministic:
it depends only on the topic's event log (and the fold of it) and the timezone, never on the clock
or on Claude, so the digest of the same log in the same zone is always the same text.
`regenerate_topic_digest` renders it from the vault and writes it through
`studentassistant.vault.write_topic_digest` only when it changed; `DigestOnEnd(lookup)` is the
session-end hook (`SessionService.add_before_close`, after `session.ended` is in the log) that
does it for the ending session's topic. `topic_digest` is the reader the observer loop and the
editor are given: the stored digest, `None` before the first.

Dates come from the session ids (`YYYYMMDD-HHMMSS`, UTC), shown in the given zone (the configured
`observer.digest_timezone`, #202), lengths from the events' `t`. A session whose events the vault
purge folded into a compaction (#31) is still described from the state, but without its status,
length or sources, which only its events held.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo

from studentassistant.observer.fold import TopicEvent
from studentassistant.observer.loader import load_observer_snapshot
from studentassistant.observer.ops import STATE_OP_EVENT_KIND, PendingKind
from studentassistant.observer.state import PendingItem, Section, TopicState
from studentassistant.vault import (
    Session,
    Vault,
    get_subject,
    get_topic,
    read_topic_digest,
    read_topic_events,
    write_topic_digest,
)

logger = logging.getLogger(__name__)

SESSION_ENDED_KIND = "session.ended"
SESSION_ID_FORMAT = "%Y%m%d-%H%M%S"
"""A session id is its start in UTC (`vault.session_models.SESSION_ID_FORMAT`)."""
NOTES_PER_SESSION = 5
"""The observer's remarks kept per session (the latest ones)."""
NOTE_CHARS = 240
"""A remark longer than this is cut, so one long note cannot swell the cached prefix."""
EXCERPT_CHARS = 400
"""The longest excerpt `digest_excerpt` returns."""

PENDING_KIND_LABELS: dict[PendingKind, str] = {
    "illegible": "Ilegible",
    "unexplained_concept": "Concepto sin explicar",
    "incomplete": "Información incompleta",
    "possible_error": "Posible error",
    "contradiction": "Contradicción entre fuentes",
}
SOURCE_KIND_LABELS = {"notes": "apuntes", "book": "libro", "pdf": "PDF", "web": "página web"}


@dataclass
class _SessionDigest:
    """What one session contributed, collected from the log and the fold."""

    session_id: str
    logged: bool = False
    """Whether any of its events is still in the log (a purge leaves an earlier session's empty)."""
    ended: bool = False
    last_t: int = 0
    segments: int = 0
    captures: int = 0
    sections: list[str] = field(default_factory=list)
    new_sections: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    pending_added: int = 0
    pending_resolved: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.segments
            or self.captures
            or self.sections
            or self.new_sections
            or self.concepts
            or self.sources
            or self.pending_added
            or self.pending_resolved
            or self.notes
        )


def session_date(session_id: str, timezone: tzinfo = UTC) -> str:
    """`dd/mm/yyyy` in `timezone` of a `YYYYMMDD-HHMMSS` session id (its start, in UTC).

    The id itself if it is not one.
    """
    try:
        naive = datetime.strptime(session_id, SESSION_ID_FORMAT)
    except ValueError:
        return session_id
    start = datetime.combine(naive.date(), naive.time(), UTC)
    return start.astimezone(timezone).strftime("%d/%m/%Y")


def _sessions(events: Sequence[TopicEvent], state: TopicState) -> list[_SessionDigest]:
    sessions: dict[str, _SessionDigest] = {}

    def of(session_id: str) -> _SessionDigest:
        found = sessions.get(session_id)
        if found is None:
            found = sessions[session_id] = _SessionDigest(session_id)
        return found

    for session_id, event in events:
        digest = of(session_id)
        digest.logged = True
        digest.last_t = max(digest.last_t, event.t)
        if event.kind == SESSION_ENDED_KIND:
            digest.ended = True
        elif event.kind == STATE_OP_EVENT_KIND and event.payload.get("op") == "set_source_context":
            label = _source_label(event.payload.get("kind"), event.payload.get("reference"))
            if label not in digest.sources:
                digest.sources.append(label)
    for ref in state.segments.values():
        of(ref.session_id).segments += 1
    for ref in state.captures.values():
        of(ref.session_id).captures += 1
    for section in state.sections.values():
        of(section.added_at.session_id).new_sections.append(section.title)
    for segment_id, section_id in state.assignments.items():
        ref = state.segments.get(segment_id)
        section = state.sections.get(section_id)
        if ref is None or section is None:
            continue
        worked = of(ref.session_id).sections
        if section.id not in worked:
            worked.append(section.id)
    for concept in state.concepts.values():
        of(concept.added_at.session_id).concepts.append(concept.name)
    for item in state.pending.values():
        of(item.added_at.session_id).pending_added += 1
        if item.resolved_at is not None:
            of(item.resolved_at.session_id).pending_resolved += 1
    for note in state.notes:
        of(note.added_at.session_id).notes.append(_cut(note.text, NOTE_CHARS))
    ordered = [sessions[key] for key in sorted(sessions)]
    for digest in ordered:
        # Worked-on sections in outline order, as titles.
        digest.sections = [s.title for s in state.sections.values() if s.id in digest.sections]
        digest.notes = digest.notes[-NOTES_PER_SESSION:]
    return ordered


def _source_label(kind: object, reference: object) -> str:
    label = SOURCE_KIND_LABELS.get(str(kind), str(kind))
    return f"{label} ({reference})" if isinstance(reference, str) and reference else label


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _outline_lines(state: TopicState, parent_id: str | None, depth: int) -> list[str]:
    lines: list[str] = []
    for section in state.outline(parent_id):
        lines.append(f"{'  ' * depth}- {section.title}{_segments_note(state, section)}")
        lines.extend(_outline_lines(state, section.id, depth + 1))
    return lines


def _segments_note(state: TopicState, section: Section) -> str:
    count = len(state.segments_of(section.id))
    return f" ({_plural(count, 'fragmento', 'fragmentos')})" if count else ""


def _summary(sessions: list[_SessionDigest], state: TopicState, timezone: tzinfo) -> str:
    worked = [s for s in sessions if not s.is_empty]
    open_count = len(state.open_pending())
    doubts = (
        "Sin dudas abiertas."
        if open_count == 0
        else f"{_plural(open_count, 'duda abierta', 'dudas abiertas')}."
    )
    if not worked:
        return f"Todavía no hay contenido registrado. {doubts}"
    last = worked[-1]
    covered = ", ".join(last.sections or last.new_sections or last.concepts)
    text = f"{_plural(len(worked), 'sesión', 'sesiones')} con contenido"
    text += f"; la última, el {session_date(last.session_id, timezone)}"
    text += f": {covered}." if covered else "."
    return f"{text} {doubts}"


def _session_block(number: int, session: _SessionDigest, timezone: tzinfo) -> list[str]:
    heading = f"### Sesión {number} — {session_date(session.session_id, timezone)}"
    if session.logged:
        status = "terminada" if session.ended else "sin terminar"
        heading += f" ({status}, {round(session.last_t / 60000)} min)"
    lines = [heading]
    if session.is_empty:
        return [*lines, "", "Sin contenido registrado."]
    lines.append("")
    if session.sources:
        lines.append(f"- Fuente: {'; '.join(session.sources)}")
    if session.sections:
        lines.append(f"- Apartados trabajados: {', '.join(session.sections)}")
    new_only = [title for title in session.new_sections if title not in session.sections]
    if new_only:
        lines.append(f"- Apartados nuevos sin contenido todavía: {', '.join(new_only)}")
    if session.concepts:
        lines.append(f"- Conceptos nuevos: {', '.join(session.concepts)}")
    material = []
    if session.segments:
        material.append(_plural(session.segments, "fragmento de voz", "fragmentos de voz"))
    if session.captures:
        material.append(_plural(session.captures, "página capturada", "páginas capturadas"))
    if material:
        lines.append(f"- Material: {', '.join(material)}")
    if session.pending_added or session.pending_resolved:
        lines.append(
            f"- Dudas: {session.pending_added} nuevas, {session.pending_resolved} resueltas"
        )
    if session.notes:
        lines.append("- Observaciones:")
        lines.extend(f"  - {note}" for note in session.notes)
    return lines


def _pending_line(item: PendingItem, numbers: dict[str, int]) -> str:
    label = PENDING_KIND_LABELS.get(item.kind, item.kind)
    number = numbers.get(item.added_at.session_id)
    where = f" (sesión {number})" if number is not None else ""
    return f"- [{label}] {_cut(item.text, NOTE_CHARS)}{where}"


def render_digest(
    subject_name: str,
    topic_title: str,
    events: Sequence[TopicEvent],
    state: TopicState,
    *,
    timezone: tzinfo = UTC,
) -> str:
    """The topic digest (Spanish Markdown) of `events`, whose fold is `state`.

    Session dates are shown in `timezone`. Pure and deterministic: equal arguments give the same
    text, whatever the clock.
    """
    sessions = _sessions(events, state)
    numbers = {session.session_id: index for index, session in enumerate(sessions, start=1)}
    lines = [f"# Resumen del tema: {topic_title}", "", _summary(sessions, state, timezone), ""]
    lines += [f"Asignatura: {subject_name}.", ""]
    lines += ["## Índice", ""]
    lines += _outline_lines(state, None, 0) or ["Sin apartados todavía."]
    lines += ["", "## Sesiones", ""]
    if not sessions:
        lines.append("Ninguna sesión todavía.")
    for number, session in enumerate(sessions, start=1):
        if number > 1:
            lines.append("")
        lines += _session_block(number, session, timezone)
    lines += ["", "## Dudas abiertas", ""]
    open_items = state.open_pending()
    lines += [_pending_line(item, numbers) for item in open_items] or ["Ninguna."]
    return "\n".join(lines) + "\n"


def digest_excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str | None:
    """The digest's summary paragraph (the first one that is not a heading), at most `limit`.

    `None` for no digest or one without such a paragraph.
    """
    if not text:
        return None
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if paragraph and not paragraph.startswith("#"):
            return _cut(paragraph, limit)
    return None


def regenerate_topic_digest(
    vault: Vault, subject_slug: str, topic_slug: str, *, timezone: tzinfo = UTC
) -> bool:
    """Render the topic's digest from its log and write `state/digest.md` if it changed.

    Session dates are shown in `timezone` (the app passes `observer.digest_timezone`).

    Returns whether it was written. Reads the stored snapshot without writing it back.

    Raises:
        what `load_observer_snapshot`, `read_topic_events` and `write_topic_digest` raise.
    """
    snapshot = load_observer_snapshot(vault, subject_slug, topic_slug, write_back=False)
    events = list(read_topic_events(vault, subject_slug, topic_slug))
    subject_name = get_subject(vault, subject_slug).subject.name
    topic_title = get_topic(vault, subject_slug, topic_slug).topic.title
    text = render_digest(subject_name, topic_title, events, snapshot.state, timezone=timezone)
    if read_topic_digest(vault, subject_slug, topic_slug) == text:
        return False
    write_topic_digest(vault, subject_slug, topic_slug, text)
    return True


def topic_digest(vault: Vault, subject_slug: str, topic_slug: str) -> str | None:
    """The stored digest of a topic (`state/digest.md`), `None` before its first session end.

    The `DigestReader` the observer loop and the editor are given.
    """
    return read_topic_digest(vault, subject_slug, topic_slug)


class DigestOnEnd:
    """The session-end hook that regenerates the ending session's topic digest.

    `lookup(session_id)` gives the attached session (`SessionBus.attached`); register it with
    `SessionService.add_before_close`, so `session.ended` is already in the log. A session the
    lookup does not know is logged and skipped. Session dates are shown in `timezone`.
    """

    def __init__(self, lookup: Callable[[str], Session | None], *, timezone: tzinfo = UTC) -> None:
        self.lookup = lookup
        self.timezone = timezone

    async def __call__(self, session_id: str) -> None:
        session = self.lookup(session_id)
        if session is None:
            logger.warning("no open vault session %s; its digest is not regenerated", session_id)
            return
        await asyncio.to_thread(
            regenerate_topic_digest,
            session.vault,
            session.subject_slug,
            session.topic_slug,
            timezone=self.timezone,
        )
