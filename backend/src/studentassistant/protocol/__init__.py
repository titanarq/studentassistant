"""Phone<->backend wire contract (WebSocket + REST)."""

from studentassistant.protocol.client import AudioFormat, ClientCapabilities, ClientHello
from studentassistant.protocol.registry import MODELS, model_for
from studentassistant.protocol.version import (
    PROTOCOL_VERSION,
    IncompatibleProtocolVersionError,
    check_compatible,
    negotiate,
    parse_version,
)

__all__ = [
    "MODELS",
    "PROTOCOL_VERSION",
    "AudioFormat",
    "ClientCapabilities",
    "ClientHello",
    "IncompatibleProtocolVersionError",
    "check_compatible",
    "model_for",
    "negotiate",
    "parse_version",
]
