"""Binary audio frames sent on `/ws/sessions/{id}` when the backend asked for server-side STT.

Layout (header fields big-endian, 18 bytes):

    offset  size  field
    0       4     magic, ASCII `SAAF`
    4       1     protocol_version MAJOR
    5       1     protocol_version MINOR
    6       4     seq, u32, per-session audio frame counter (acked by the server `ack`)
    10      8     client time in ms, u64, capture time of the first sample (client clock)
    18      ...   payload: PCM16 16 kHz mono, little-endian signed 16-bit samples

A frame whose MAJOR differs from ours is refused, exactly like a `protocol_version` in `hello`;
a different MINOR is accepted.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from studentassistant.protocol.version import (
    PROTOCOL_VERSION,
    IncompatibleProtocolVersionError,
    check_compatible,
    parse_version,
)

MAGIC = b"SAAF"
SAMPLE_RATE_HZ = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2

_HEADER = struct.Struct(">4sBBIQ")
HEADER_SIZE = _HEADER.size

_U32_MAX = 2**32 - 1
_U64_MAX = 2**64 - 1
_U8_MAX = 2**8 - 1


class AudioFrameError(ValueError):
    """A binary frame that does not follow the documented layout."""


class WrongMagicError(AudioFrameError):
    """The frame does not start with `MAGIC`."""


class IncompatibleAudioFrameVersionError(AudioFrameError, IncompatibleProtocolVersionError):
    """The frame was written by a peer speaking another protocol MAJOR version."""

    def __init__(self, peer: str, ours: str = PROTOCOL_VERSION) -> None:
        IncompatibleProtocolVersionError.__init__(self, peer, ours)


@dataclass(frozen=True)
class AudioFrame:
    """One chunk of client audio: its sequence number, client timestamp and PCM16 samples."""

    seq: int
    client_time_ms: int
    pcm: bytes
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not 0 <= self.seq <= _U32_MAX:
            raise AudioFrameError(f"seq {self.seq} does not fit in u32")
        if not 0 <= self.client_time_ms <= _U64_MAX:
            raise AudioFrameError(f"client_time_ms {self.client_time_ms} does not fit in u64")
        if len(self.pcm) % SAMPLE_WIDTH_BYTES:
            raise AudioFrameError(
                f"PCM16 payload of {len(self.pcm)} bytes is not a whole number of samples"
            )
        major, minor = parse_version(self.protocol_version)
        if major > _U8_MAX or minor > _U8_MAX:
            raise AudioFrameError(f"protocol_version {self.protocol_version} does not fit in u8.u8")

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // SAMPLE_WIDTH_BYTES

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000 / SAMPLE_RATE_HZ


def encode_frame(frame: AudioFrame) -> bytes:
    """Header followed by the PCM16 payload, ready to send as one binary WebSocket message."""
    major, minor = parse_version(frame.protocol_version)
    return _HEADER.pack(MAGIC, major, minor, frame.seq, frame.client_time_ms) + frame.pcm


def decode_frame(data: bytes, ours: str = PROTOCOL_VERSION) -> AudioFrame:
    """Parse one binary message; a wrong magic or an incompatible MAJOR raises `AudioFrameError`."""
    if len(data) < HEADER_SIZE:
        raise AudioFrameError(f"audio frame of {len(data)} bytes is shorter than its header")
    magic, major, minor, seq, client_time_ms = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise WrongMagicError(f"audio frame magic {magic!r} is not {MAGIC!r}")
    peer = f"{major}.{minor}"
    try:
        check_compatible(peer, ours)
    except IncompatibleProtocolVersionError:
        raise IncompatibleAudioFrameVersionError(peer, ours) from None
    return AudioFrame(
        seq=seq,
        client_time_ms=client_time_ms,
        pcm=bytes(data[HEADER_SIZE:]),
        protocol_version=peer,
    )
