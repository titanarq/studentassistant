"""Messages the capture client sends over `/ws/sessions/{id}`."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from studentassistant.protocol.base import EpochMs, Id, ProtocolModel
from studentassistant.protocol.version import VERSION_PATTERN

# Id of a client recognizer, e.g. `web-speech` or `android-speech`.
ProviderId = Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
# BCP 47 language tag as the recognizer reports it, e.g. `es-ES`.
Language = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]


class AudioFormat(ProtocolModel):
    """The only audio the backend accepts in server STT mode (ADR-0001, ADR-0008)."""

    encoding: Literal["pcm16"]
    sample_rate_hz: Literal[16000]
    channels: Literal[1]


class ClientCapabilities(ProtocolModel):
    """What the client can do; the backend picks the STT mode in `hello.ack`."""

    # The client's preferred STT mode; the backend may still ask for the other one.
    stt: Literal["client", "server"]
    # Id of the client's own recognizer, e.g. `web-speech` or `android-speech`.
    stt_provider: ProviderId
    # Present only when the client can stream audio for server-side STT.
    audio_format: AudioFormat | None = None


class ClientHello(ProtocolModel):
    """First message on the WebSocket: version, capabilities and the clock-sync reading."""

    type: Literal["hello"]
    protocol_version: Annotated[str, Field(pattern=VERSION_PATTERN)]
    capabilities: ClientCapabilities
    # Client clock in Unix epoch ms when the message was sent; the backend derives the offset.
    client_time_ms: Annotated[int, Field(ge=0)]


class _TranscriptSegment(ProtocolModel):
    """A client-recognised stretch of speech; times are on the client's clock (ADR-0008)."""

    # Client-assigned; the partials and the final of one utterance share it.
    segment_id: Id
    client_start_ms: EpochMs
    client_end_ms: EpochMs
    text: str
    provider: ProviderId
    language: Language
    # The recognizer's own score in [0, 1], when it reports one.
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        if self.client_end_ms < self.client_start_ms:
            raise ValueError("client_end_ms is before client_start_ms")
        return self


class TranscriptClientPartial(_TranscriptSegment):
    """Interim text of an utterance still being spoken; a later one replaces it."""

    type: Literal["transcript.client.partial"]


class TranscriptClientFinal(_TranscriptSegment):
    """The settled text of an utterance; it replaces every partial with the same `segment_id`."""

    type: Literal["transcript.client.final"]


# Every ADR-0006 command except capture, which the client performs itself and uploads with
# `trigger: button` (POST /api/sessions/{id}/captures).
ButtonName = Literal[
    "next_page",
    "important",
    "switch_source",
    "pause",
    "resume",
    "end_session",
    "web_search",
]
SourceKind = Literal["book", "notes", "pdf"]


class Button(ProtocolModel):
    """The student pressed the button equivalent of a voice command (ADR-0006)."""

    type: Literal["button"]
    button: ButtonName
    # The source switched to; present exactly when `button` is `switch_source`.
    source: SourceKind | None = None
    client_time_ms: EpochMs

    @model_validator(mode="after")
    def _source_only_on_switch(self) -> Self:
        if (self.button == "switch_source") != (self.source is not None):
            raise ValueError("source is required with switch_source and forbidden otherwise")
        return self


class Marker(ProtocolModel):
    """A point on the session timeline the student flagged, with an optional short label."""

    type: Literal["marker"]
    client_time_ms: EpochMs
    label: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class ClientAck(ProtocolModel):
    """The client received a server `command` and acted on it (ADR-0006)."""

    type: Literal["ack"]
    command_id: Id
    client_time_ms: EpochMs


ClientEvent = Annotated[
    ClientHello | TranscriptClientPartial | TranscriptClientFinal | Button | Marker | ClientAck,
    Field(discriminator="type"),
]
CLIENT_EVENT_ADAPTER: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


def parse_client_event(data: Any) -> ClientEvent:
    """Parses one decoded JSON client message; `ValidationError` on an unknown or missing `type`."""
    return CLIENT_EVENT_ADAPTER.validate_python(data)
