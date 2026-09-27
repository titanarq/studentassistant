"""`studentassistant eval import-session`: turn a session of the vault into an eval case.

The real sessions in the vault have no `serve --record` recording, so `read_case` cannot use
them. This rebuilds one from what the vault kept of the session -- read only through the public
`studentassistant.vault` API, and nothing is ever written into the vault:

    <out>/
      recording/                 a client-mode recording (`server/recording.py`)
        manifest.yaml            subject, topic, language, the recognizer of the session's finals
                                 and the client time the session started
        transcript.jsonl         one `transcript.client.final` per segment of `transcript.jsonl`,
                                 on the session clock (the start time plus its `t_start`/`t_end`)
        events.jsonl             the session's `button` and `marker` events, at their session time
        captures.jsonl           one burst per stored capture of the session (its
        captures/                `capture.stored` events, in order), at the capture's
                                 `session_t_ms`: the kept still, plus every other still of the
                                 burst still in the vault, in burst order
      reference/                 stubs for the student to correct, each marked `DRAFT_MARKER`
        notes.md                 empty but for the topic's title: the notes the student means
        pages/<capture_id>.md    the page transcription the vault stored, to correct
        triage.yaml              every capture with the triage it got, to correct

The session's own segment ids (`transcript.final` events) are kept when they can be matched to a
transcript segment, so a reference written against the vault's events still names them; other
segments are `seg-<seq>`. A capture whose still is gone or not an image is left out and reported.
Only `notes`/`book` pages are captures; other sources of the session are not in the recording.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from studentassistant.evals.cases import (
    CASE_RECORDING_DIR,
    CASE_REFERENCE_DIR,
    DRAFT_MARKER,
    REFERENCE_NOTES_FILE,
    REFERENCE_PAGES_DIR,
    REFERENCE_TRIAGE_FILE,
)
from studentassistant.protocol import (
    Button,
    CaptureImage,
    CaptureUploadRequest,
    Marker,
    TranscriptClientFinal,
)
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.recording import RecordingManifest, RecordingWriter
from studentassistant.sources.captures import decode_image
from studentassistant.sources.triage import CAPTURE_KINDS, triage_of
from studentassistant.vault import (
    Event,
    SessionMeta,
    SourceError,
    TranscriptSegment,
    Vault,
    VaultError,
    get_topic,
    list_sessions,
    read_session_transcript,
    read_source,
    read_topic_events,
)

SEGMENT_KIND = "transcript.final"
CAPTURE_KIND = "capture.stored"
SESSION_STARTED_KIND = "session.started"
BUTTON_KIND = "button"
MARKER_KIND = "marker"
DEFAULT_PROVIDER = "replay"

_ID = re.compile(ID_PATTERN)
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_BURST_EXTENSIONS = (".jpg", ".png", ".webp")


class SessionImportError(ValueError):
    """A session that cannot be imported: unknown, unreadable, or an output already there."""


@dataclass
class ImportedCase:
    """What `import_session` wrote: the case directory and what went into it."""

    directory: Path
    finals: int = 0
    events: int = 0
    captures: int = 0
    # Capture ids whose stored page transcription prefilled `reference/pages/`.
    pages: list[str] = field(default_factory=list)
    # Captures left out, each with why (Spanish, for the CLI).
    skipped: list[str] = field(default_factory=list)


def default_case_name(subject: str, topic: str, session_id: str) -> str:
    """The case directory name `eval import-session` uses without `--out`."""
    return f"{subject}-{topic}-{session_id}"


def import_session(
    vault: Vault,
    subject: str,
    topic: str,
    session_id: str,
    out: Path,
    *,
    language: str,
) -> ImportedCase:
    """Write the eval case of session `session_id` of `subject`/`topic` into `out`.

    `language` is the manifest's language when no final of the session names one.

    Raises:
        SessionImportError: when `out` already exists, the topic has no such session, or the
            session's files cannot be read.
    """
    if out.exists():
        raise SessionImportError(f"{out} already exists")
    try:
        meta = _session(vault, subject, topic, session_id)
        segments = read_session_transcript(vault, subject, topic, session_id)
        events = [e for sid, e in read_topic_events(vault, subject, topic) if sid == session_id]
        title = get_topic(vault, subject, topic).topic.title
    except (VaultError, OSError) as error:
        raise SessionImportError(str(error)) from error
    start = _started_client_ms(meta, events)
    finals = _finals(segments, events, start, language)
    manifest = RecordingManifest(
        format_version=1,
        subject=subject,
        topic=topic,
        language=finals[0].language if finals else language,
        stt_mode="client",
        stt_provider=finals[0].provider if finals else DEFAULT_PROVIDER,
        started_client_time_ms=start,
    )
    result = ImportedCase(directory=out)
    reference = out / CASE_REFERENCE_DIR
    (reference / REFERENCE_PAGES_DIR).mkdir(parents=True)
    triage_entries: list[str] = []
    with RecordingWriter(out / CASE_RECORDING_DIR, manifest) as writer:
        for final in finals:
            writer.append_transcript(final)
            result.finals += 1
        for message in _client_events(events, start):
            writer.append_event(message)
            result.events += 1
        for event in events:
            if event.kind != CAPTURE_KIND:
                continue
            capture = _capture(vault, event, start)
            if isinstance(capture, str):
                result.skipped.append(capture)
                continue
            metadata, images, stored = capture
            writer.add_capture(metadata, images)
            result.captures += 1
            page = _page_transcription(vault, stored.source_path)
            if page is not None:
                _write_page_stub(reference, metadata.capture_id, stored.source_id, page)
                result.pages.append(metadata.capture_id)
            triage_entries.append(_triage_entry(metadata, stored))
    _write_notes_stub(reference, title)
    _write_triage_stub(reference, triage_entries)
    return result


def _session(vault: Vault, subject: str, topic: str, session_id: str) -> SessionMeta:
    for meta in list_sessions(vault, subject, topic):
        if meta.id == session_id:
            return meta
    raise SessionImportError(f"the topic {subject}/{topic} has no session {session_id!r}")


def _started_client_ms(meta: SessionMeta, events: list[Event]) -> int:
    """The client time the session started: `session.started`'s, else its `started_at`."""
    for event in events:
        if event.kind == SESSION_STARTED_KIND:
            value = event.payload.get("client_time_ms")
            if isinstance(value, int) and value >= 0:
                return value
            break
    return int(meta.started_at.timestamp() * 1000)


def _finals(
    segments: list[TranscriptSegment], events: list[Event], start: int, language: str
) -> list[TranscriptClientFinal]:
    """One client final per transcript segment, under the session's own segment id if known."""
    published: dict[tuple[int, str], Mapping[str, Any]] = {}
    for event in events:
        if event.kind == SEGMENT_KIND:
            text, begin = event.payload.get("text"), event.payload.get("session_start_ms")
            if isinstance(text, str) and isinstance(begin, int):
                published.setdefault((begin, text), event.payload)
    finals: list[TranscriptClientFinal] = []
    used: set[str] = set()
    for segment in segments:
        payload = published.get((segment.t_start, segment.text), {})
        segment_id = payload.get("segment_id")
        if not isinstance(segment_id, str) or not _ID.match(segment_id) or segment_id in used:
            segment_id = f"seg-{segment.seq}"
        used.add(segment_id)
        fields = {
            "type": "transcript.client.final",
            "segment_id": segment_id,
            "client_start_ms": start + segment.t_start,
            "client_end_ms": start + max(segment.t_start, segment.t_end),
            "text": segment.text,
        }
        try:
            final = TranscriptClientFinal.model_validate(
                fields
                | {
                    "provider": payload.get("provider") or DEFAULT_PROVIDER,
                    "language": payload.get("language") or language,
                }
            )
        except ValidationError:
            final = TranscriptClientFinal.model_validate(
                fields | {"provider": DEFAULT_PROVIDER, "language": language}
            )
        finals.append(final)
    return finals


def _client_events(events: list[Event], start: int) -> list[Button | Marker]:
    """The session's buttons and markers as the client sent them, at their session time."""
    messages: list[Button | Marker] = []
    for event in events:
        try:
            if event.kind == BUTTON_KIND:
                messages.append(
                    Button.model_validate(
                        {
                            "type": "button",
                            "button": event.payload.get("button"),
                            "source": event.payload.get("source"),
                            "client_time_ms": start + event.t,
                        }
                    )
                )
            elif event.kind == MARKER_KIND:
                messages.append(
                    Marker.model_validate(
                        {
                            "type": "marker",
                            "label": event.payload.get("label"),
                            "client_time_ms": start + event.t,
                        }
                    )
                )
        except ValidationError:
            continue  # a button this protocol no longer knows: nothing a replay could send
    return messages


@dataclass(frozen=True)
class _StoredCapture:
    source_path: str
    source_id: str
    meta: Mapping[str, Any]
    session_t_ms: int


def _capture(
    vault: Vault, event: Event, start: int
) -> tuple[CaptureUploadRequest, dict[str, bytes], _StoredCapture] | str:
    """The burst of one `capture.stored` event, or why it cannot be rebuilt (Spanish)."""
    capture_id = str(event.payload.get("capture_id") or "?")
    source_path = event.payload.get("source_path")
    if not isinstance(source_path, str):
        return f"{capture_id}: el evento no dice dónde se guardó"
    parts = PurePosixPath(source_path).parts
    if len(parts) < 3 or parts[-2] not in CAPTURE_KINDS:
        return f"{capture_id}: {source_path} no es una página de apuntes ni de libro"
    try:
        still = read_source(vault, source_path)
    except (SourceError, OSError) as error:
        return f"{capture_id}: no se puede leer {source_path} ({error})"
    meta = still.meta or {}
    t = meta.get("session_t_ms")
    session_t = t if isinstance(t, int) and t >= 0 else event.t
    stills = _burst_stills(vault, source_path, still.content, meta)
    client_time = start + session_t
    images: dict[str, bytes] = {}
    described: list[CaptureImage] = []
    for data, content_type in stills:
        decoded = decode_image(data)
        if decoded is None:
            continue
        part = f"image_{len(described)}"
        images[part] = data
        described.append(
            CaptureImage(
                part=part,
                content_type=content_type,  # type: ignore[arg-type]
                width_px=int(decoded.shape[1]),
                height_px=int(decoded.shape[0]),
                client_time_ms=client_time,
            )
        )
    if not described:
        return f"{capture_id}: {source_path} no es una imagen legible"
    try:
        metadata = CaptureUploadRequest(
            capture_id=capture_id,
            trigger="button",
            client_time_ms=client_time,
            images=described,
        )
    except ValidationError as error:
        return f"{capture_id}: no es una captura válida ({error.errors()[0]['msg']})"
    source_id = "/".join(parts[-3:])
    stored = _StoredCapture(source_path, source_id, meta, session_t)
    return metadata, images, stored


def _burst_stills(
    vault: Vault, source_path: str, kept: bytes, meta: Mapping[str, Any]
) -> list[tuple[bytes, str]]:
    """The burst in its order: the kept still at `selected_image`, each other still still kept."""
    path = PurePosixPath(source_path)
    kept_type = _CONTENT_TYPES.get(path.suffix.lower(), "image/jpeg")
    count, selected = meta.get("image_count"), meta.get("selected_image")
    if not isinstance(count, int) or not isinstance(selected, int) or not 1 <= selected <= count:
        return [(kept, kept_type)]
    stem = path.name.split(".", 1)[0]
    stills: list[tuple[bytes, str]] = []
    for position in range(1, count + 1):
        if position == selected:
            stills.append((kept, kept_type))
            continue
        for extension in _BURST_EXTENSIONS:
            name = path.with_name(f"{stem}.burst{position}{extension}").as_posix()
            try:
                stills.append((read_source(vault, name).content, _CONTENT_TYPES[extension]))
            except (SourceError, OSError):
                continue
            break
    return stills


def _page_transcription(vault: Vault, source_path: str) -> str | None:
    path = PurePosixPath(source_path)
    page = path.with_name(f"{path.name.split('.', 1)[0]}.md").as_posix()
    try:
        return read_source(vault, page).content.decode("utf-8")
    except (SourceError, OSError, UnicodeDecodeError):
        return None


def _clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _write_notes_stub(reference: Path, title: str) -> None:
    (reference / REFERENCE_NOTES_FILE).write_text(
        f"<!-- {DRAFT_MARKER}: escribe aquí, en Markdown, los apuntes que de verdad querías"
        " sacar de esta sesión y borra esta línea. -->\n"
        "\n"
        f"# {title}\n",
        encoding="utf-8",
    )


def _write_page_stub(reference: Path, capture_id: str, source_id: str, text: str) -> None:
    (reference / REFERENCE_PAGES_DIR / f"{capture_id}.md").write_text(
        f"<!-- {DRAFT_MARKER}: esta es la transcripción que guardó la bóveda de {source_id};"
        " corrígela para que diga lo que pone de verdad la página y borra esta línea. -->\n"
        "\n"
        f"{text.rstrip()}\n",
        encoding="utf-8",
    )


def _triage_entry(metadata: CaptureUploadRequest, stored: _StoredCapture) -> str:
    decided = triage_of(stored.meta)
    reasons = ", ".join(decided.reasons)
    lines = [
        f"  # {stored.source_id}, a los {_clock(stored.session_t_ms)} de la sesión;"
        f" la decidió: {decided.decided_by}",
        f"  - capture_id: {json.dumps(metadata.capture_id)}",
        f"    status: {decided.status}",
        f"    reasons: [{reasons}]",
    ]
    return "\n".join(lines)


def _write_triage_stub(reference: Path, entries: list[str]) -> None:
    header = [
        f"# {DRAFT_MARKER}: el triaje que recibió cada captura en la sesión. Corrige cada una",
        "# a lo que debería haber sido y borra esta línea.",
        "# status: kept (se queda), flagged (se queda con aviso) o set_aside (apartada).",
        "# reasons: blank (en blanco), duplicate (repetida), blurry (borrosa), partial (cortada),",
        "#          same_content (mismo contenido que otra).",
    ]
    body = ["captures:", *entries] if entries else ["captures: []"]
    (reference / REFERENCE_TRIAGE_FILE).write_text(
        "\n".join([*header, *body]) + "\n", encoding="utf-8"
    )
