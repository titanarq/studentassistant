"""Phone<->backend wire contract (WebSocket + REST)."""

from studentassistant.protocol.base import ProtocolModel
from studentassistant.protocol.client import AudioFormat, ClientCapabilities, ClientHello
from studentassistant.protocol.registry import MODELS, model_for
from studentassistant.protocol.rest import (
    CaptureImage,
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
    "CaptureImage",
    "CaptureUploadRequest",
    "CaptureUploadResponse",
    "ClientCapabilities",
    "ClientHello",
    "HealthResponse",
    "IncompatibleProtocolVersionError",
    "PairRequest",
    "PairResponse",
    "ProtocolModel",
    "Session",
    "SessionEndRequest",
    "SessionEndResponse",
    "SessionStartRequest",
    "Subject",
    "SubjectCreateRequest",
    "SubjectsListResponse",
    "Topic",
    "TopicCreateRequest",
    "TopicsListResponse",
    "check_compatible",
    "model_for",
    "negotiate",
    "parse_version",
]
