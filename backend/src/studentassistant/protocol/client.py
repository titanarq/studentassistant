"""Messages the capture client sends over `/ws/sessions/{id}`."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from studentassistant.protocol.base import ProtocolModel
from studentassistant.protocol.version import VERSION_PATTERN


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
    stt_provider: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
    # Present only when the client can stream audio for server-side STT.
    audio_format: AudioFormat | None = None


class ClientHello(ProtocolModel):
    """First message on the WebSocket: version, capabilities and the clock-sync reading."""

    type: Literal["hello"]
    protocol_version: Annotated[str, Field(pattern=VERSION_PATTERN)]
    capabilities: ClientCapabilities
    # Client clock in Unix epoch ms when the message was sent; the backend derives the offset.
    client_time_ms: Annotated[int, Field(ge=0)]
