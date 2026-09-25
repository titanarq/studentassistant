"""Bodies of the REST endpoints under `/api`.

Every endpoint except `GET /api/health` and `POST /api/pair` needs `Authorization: Bearer <token>`
(ADR-0001). Times are Unix epoch milliseconds: `client_time_ms` on the client's clock,
`*_at_ms` and `server_time_ms` on the backend's.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from studentassistant.protocol.base import UUID_PATTERN, EpochMs, Id, ProtocolModel
from studentassistant.protocol.version import VERSION_PATTERN

ProtocolVersion = Annotated[str, Field(pattern=VERSION_PATTERN)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
# Client-generated UUID (lowercase, hyphenated) that makes a capture upload idempotent.
CaptureId = Annotated[str, Field(pattern=UUID_PATTERN)]


# POST /api/pair


class PairRequest(ProtocolModel):
    """Exchanges the one-time code shown in the pairing QR for a long-lived bearer token."""

    pairing_code: Annotated[str, Field(min_length=1)]
    device_name: Name
    client_kind: Literal["web", "android"]
    protocol_version: ProtocolVersion


class PairResponse(ProtocolModel):
    device_id: Id
    # Sent as `Authorization: Bearer <token>`; never logged by either side.
    token: Annotated[str, Field(min_length=1)]
    protocol_version: ProtocolVersion


# GET /api/health


class HealthResponse(ProtocolModel):
    status: Literal["ok"]
    protocol_version: ProtocolVersion
    server_time_ms: EpochMs


# GET /api/subjects, POST /api/subjects


class Subject(ProtocolModel):
    subject_id: Id
    name: Name


class SubjectsListResponse(ProtocolModel):
    subjects: list[Subject]


class SubjectCreateRequest(ProtocolModel):
    name: Name


# GET /api/subjects/{subject_id}/topics, POST /api/subjects/{subject_id}/topics


class Topic(ProtocolModel):
    topic_id: Id
    subject_id: Id
    name: Name
    # The session still open on this topic, if any; the client resumes it instead of starting one.
    open_session_id: Id | None = None


class TopicsListResponse(ProtocolModel):
    subject_id: Id
    topics: list[Topic]


class TopicCreateRequest(ProtocolModel):
    name: Name


# POST /api/sessions, POST /api/sessions/{id}/resume, POST /api/sessions/{id}/end


class SessionStartRequest(ProtocolModel):
    """A session is about exactly one topic of one subject, fixed here (ADR-0001)."""

    subject_id: Id
    topic_id: Id
    client_time_ms: EpochMs


class Session(ProtocolModel):
    """An open session, returned by start and resume."""

    session_id: Id
    subject_id: Id
    topic_id: Id
    status: Literal["active"]
    started_at_ms: EpochMs
    # Where to open the WebSocket, always `/ws/sessions/{session_id}`.
    ws_path: Annotated[str, Field(pattern=r"^/ws/sessions/[A-Za-z0-9][A-Za-z0-9_-]*$")]
    protocol_version: ProtocolVersion
    # Captures the backend already stored, so a resuming client re-uploads only the rest.
    received_capture_ids: list[CaptureId] = []


class SessionEndRequest(ProtocolModel):
    client_time_ms: EpochMs
    reason: Literal["button", "command"]


class SessionEndResponse(ProtocolModel):
    session_id: Id
    status: Literal["ended"]
    ended_at_ms: EpochMs


# POST /api/sessions/{id}/captures (multipart/form-data)


class CaptureImage(ProtocolModel):
    # Name of the multipart part carrying this image's bytes.
    part: Annotated[str, Field(pattern=r"^image_[0-9]+$")]
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    width_px: Annotated[int, Field(ge=1)]
    height_px: Annotated[int, Field(ge=1)]
    client_time_ms: EpochMs


class CaptureUploadRequest(ProtocolModel):
    """The `metadata` JSON part of a capture burst; the images travel as sibling parts.

    Re-sending a `capture_id` the backend already stored is answered with `duplicate` and stores
    nothing, so a client may retry an upload (or replay its offline spool) freely.
    """

    capture_id: CaptureId
    trigger: Literal["button", "command"]
    # The `command` message that asked for this capture, when `trigger` is `command` (ADR-0006).
    command_id: Id | None = None
    client_time_ms: EpochMs
    images: Annotated[list[CaptureImage], Field(min_length=1)]


class CaptureUploadResponse(ProtocolModel):
    capture_id: CaptureId
    session_id: Id
    status: Literal["stored", "duplicate"]
    image_count: Annotated[int, Field(ge=1)]
    received_at_ms: EpochMs


# GET /api/search


class SearchHit(ProtocolModel):
    """One match of a search over the vault's notes, page transcriptions, web pages, transcripts.

    `path` is the vault-relative file the text is in; `source` the source it belongs to (a page
    transcription's page image, a web page itself; absent for notes and transcripts). A transcript
    hit carries its `session`, the segment's `seq` and its `t_start` in session milliseconds.
    `snippet` marks each matched term between U+0002 and U+0003.
    """

    kind: Literal["notes", "page", "pdf", "web", "transcript"]
    path: Annotated[str, Field(min_length=1)]
    source: Annotated[str, Field(min_length=1)] | None = None
    subject: Id
    topic: Id
    session: Id | None = None
    seq: Annotated[int, Field(ge=0)] | None = None
    t_start: Annotated[int, Field(ge=0)] | None = None
    snippet: str


class SearchResponse(ProtocolModel):
    """The best matches of `query`, best first."""

    query: str
    hits: list[SearchHit]
