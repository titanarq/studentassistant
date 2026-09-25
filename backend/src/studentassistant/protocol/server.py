"""Messages the backend sends over `/ws/sessions/{id}`.

Times are on the backend's side: `server_time_ms` is the backend clock in Unix epoch ms, and
`session_*_ms` count milliseconds since the session's `started_at_ms` (ADR-0008 session time).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from studentassistant.protocol.base import EpochMs, Id, ProtocolModel
from studentassistant.protocol.client import AudioFormat, Language
from studentassistant.protocol.rest import CaptureId, ProtocolVersion

# Milliseconds since the session started, on the backend's clock.
SessionMs = Annotated[int, Field(ge=0)]
# Sequence number of a binary audio frame (u32).
AudioSeq = Annotated[int, Field(ge=0, le=2**32 - 1)]

# Since 1.4: the most terms `vocabulary_hints` carries, and the longest term.
VOCABULARY_HINTS_MAX_ITEMS = 50
VOCABULARY_HINT_MAX_CHARS = 100
VOCABULARY_HINTS_SINCE = (1, 4)
"""The protocol version that added `vocabulary_hints` to `hello.ack` and `notice`."""
# Domain terms (subject, topic, concepts) a client-side recognizer may bias towards; the whole
# list, most important first. Spanish text, no duplicates enforced by the sender.
VocabularyHints = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=VOCABULARY_HINT_MAX_CHARS)]],
    Field(min_length=1, max_length=VOCABULARY_HINTS_MAX_ITEMS),
]

STT_STATUS_SINCE = (1, 5)
"""The protocol version that added the `stt.status` server message."""
STT_STATUS_DETAIL_MAX_CHARS = 300
# A Spanish sentence for the student about the server-side STT provider.
SttStatusDetail = Annotated[str, Field(min_length=1, max_length=STT_STATUS_DETAIL_MAX_CHARS)]


class HelloAck(ProtocolModel):
    """Reply to the client `hello`: negotiated version, chosen STT mode and clock offset."""

    type: Literal["hello.ack"]
    # The version both sides speak (shared MAJOR, lower MINOR); an incompatible MAJOR gets no
    # `hello.ack`, the socket is closed with a message naming both versions instead.
    protocol_version: ProtocolVersion
    # `server`: the client must stream binary audio frames instead of its own transcripts.
    stt_mode: Literal["client", "server"]
    # The audio to stream; present exactly when `stt_mode` is `server`.
    audio_format: AudioFormat | None = None
    # Backend clock minus the client clock read in `hello`; add it to a client time to get
    # backend time. May be negative.
    clock_offset_ms: int
    server_time_ms: EpochMs
    # Since 1.4: the session's vocabulary hints, left out when there are none.
    vocabulary_hints: VocabularyHints | None = None

    @model_validator(mode="after")
    def _audio_format_only_in_server_mode(self) -> Self:
        if (self.stt_mode == "server") != (self.audio_format is not None):
            raise ValueError("audio_format is required in server stt_mode and forbidden otherwise")
        return self


class _NormalisedSegment(ProtocolModel):
    """Transcript text as the backend normalised it, whichever STT path produced it (ADR-0008)."""

    # The partials and the final of one utterance share it.
    segment_id: Id
    session_start_ms: SessionMs
    session_end_ms: SessionMs
    text: str
    language: Language
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        if self.session_end_ms < self.session_start_ms:
            raise ValueError("session_end_ms is before session_start_ms")
        return self


class TranscriptPartial(_NormalisedSegment):
    """Interim text of an utterance still being spoken; a later one replaces it."""

    type: Literal["transcript.partial"]


class TranscriptFinal(_NormalisedSegment):
    """The settled text of an utterance; it replaces every partial with the same `segment_id`."""

    type: Literal["transcript.final"]


class Command(ProtocolModel):
    """A deterministic voice command asks the client to act (ADR-0006); answered by client `ack`."""

    type: Literal["command"]
    command_id: Id
    # `capture_now`: take a burst of stills and upload them with `trigger: command`.
    command: Literal["capture_now"]
    server_time_ms: EpochMs


class Notice(ProtocolModel):
    """Status the client shows the student, such as how many doubts await review.

    Since 1.4 it may also carry the session's new `vocabulary_hints`.
    """

    type: Literal["notice"]
    pending_count: Annotated[int, Field(ge=0)]
    server_time_ms: EpochMs
    # Since 1.4: present when the session's vocabulary hints changed; replaces the whole list.
    vocabulary_hints: VocabularyHints | None = None


class SttStatus(ProtocolModel):
    """Since 1.5: the server-side STT provider changed between working and degraded.

    `ok`: speech is transcribed (the client clears its warning); `reconnecting`: the provider lost
    its service and retries, the audio meanwhile is not transcribed; `unavailable`: it cannot work
    this session at all. `detail` is a Spanish sentence for the student, sent when not `ok`.
    """

    type: Literal["stt.status"]
    state: Literal["ok", "reconnecting", "unavailable"]
    detail: SttStatusDetail | None = None
    server_time_ms: EpochMs


class ServerAck(ProtocolModel):
    """The backend stored audio up to a frame `seq` and/or the listed captures."""

    type: Literal["ack"]
    # Highest audio frame `seq` received so far (server STT mode only).
    audio_seq: AudioSeq | None = None
    capture_ids: Annotated[list[CaptureId], Field(min_length=1)] | None = None
    server_time_ms: EpochMs

    @model_validator(mode="after")
    def _acknowledges_something(self) -> Self:
        if self.audio_seq is None and self.capture_ids is None:
            raise ValueError("an ack needs audio_seq, capture_ids or both")
        return self


ServerEvent = Annotated[
    HelloAck | TranscriptPartial | TranscriptFinal | Command | Notice | SttStatus | ServerAck,
    Field(discriminator="type"),
]
SERVER_EVENT_ADAPTER: TypeAdapter[ServerEvent] = TypeAdapter(ServerEvent)


def parse_server_event(data: Any) -> ServerEvent:
    """Parses one decoded JSON server message; `ValidationError` on an unknown or missing `type`."""
    return SERVER_EVENT_ADAPTER.validate_python(data)
