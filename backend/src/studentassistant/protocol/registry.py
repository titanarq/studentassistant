"""Maps each `protocol/<name>.schema.json` name to the model that parses it."""

from __future__ import annotations

from pydantic import BaseModel

from studentassistant.protocol.client import (
    Button,
    ClientAck,
    ClientHello,
    Marker,
    TranscriptClientFinal,
    TranscriptClientPartial,
)
from studentassistant.protocol.rest import (
    CaptureUploadRequest,
    CaptureUploadResponse,
    HealthResponse,
    PairRequest,
    PairResponse,
    Session,
    SessionEndRequest,
    SessionEndResponse,
    SessionStartRequest,
    Subject,
    SubjectCreateRequest,
    SubjectsListResponse,
    Topic,
    TopicCreateRequest,
    TopicsListResponse,
)
from studentassistant.protocol.server import (
    Command,
    HelloAck,
    Notice,
    ServerAck,
    TranscriptFinal,
    TranscriptPartial,
)

MODELS: dict[str, type[BaseModel]] = {
    "client.hello": ClientHello,
    "client.transcript.client.partial": TranscriptClientPartial,
    "client.transcript.client.final": TranscriptClientFinal,
    "client.button": Button,
    "client.marker": Marker,
    "client.ack": ClientAck,
    "server.hello.ack": HelloAck,
    "server.transcript.partial": TranscriptPartial,
    "server.transcript.final": TranscriptFinal,
    "server.command": Command,
    "server.notice": Notice,
    "server.ack": ServerAck,
    "rest.pair.request": PairRequest,
    "rest.pair.response": PairResponse,
    "rest.health.response": HealthResponse,
    "rest.subjects.list.response": SubjectsListResponse,
    "rest.subjects.create.request": SubjectCreateRequest,
    "rest.subjects.create.response": Subject,
    "rest.topics.list.response": TopicsListResponse,
    "rest.topics.create.request": TopicCreateRequest,
    "rest.topics.create.response": Topic,
    "rest.sessions.start.request": SessionStartRequest,
    "rest.sessions.start.response": Session,
    "rest.sessions.resume.response": Session,
    "rest.sessions.end.request": SessionEndRequest,
    "rest.sessions.end.response": SessionEndResponse,
    "rest.sessions.captures.request": CaptureUploadRequest,
    "rest.sessions.captures.response": CaptureUploadResponse,
}


def model_for(name: str) -> type[BaseModel]:
    """The model for a schema name such as `client.hello`; `KeyError` if none is registered."""
    try:
        return MODELS[name]
    except KeyError:
        raise KeyError(f"no model registered for protocol message {name!r}") from None
