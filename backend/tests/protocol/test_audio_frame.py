"""Binary audio frames: the header round-trips and a foreign or incompatible frame is refused."""

import struct

import pytest

from studentassistant.protocol import (
    HEADER_SIZE,
    MAGIC,
    PROTOCOL_VERSION,
    AudioFrame,
    AudioFrameError,
    IncompatibleAudioFrameVersionError,
    IncompatibleProtocolVersionError,
    WrongMagicError,
    decode_frame,
    encode_frame,
)

# 20 ms of PCM16 16 kHz mono: 320 samples.
SAMPLES = [(i * 97) % 65536 - 32768 for i in range(320)]
PCM = struct.pack(f"<{len(SAMPLES)}h", *SAMPLES)


def _frame() -> AudioFrame:
    return AudioFrame(seq=4_000_000_000, client_time_ms=1_758_700_000_123, pcm=PCM)


def test_frame_round_trips_without_loss() -> None:
    frame = _frame()
    data = encode_frame(frame)
    assert len(data) == HEADER_SIZE + len(PCM)
    decoded = decode_frame(data)
    assert decoded == frame
    assert list(struct.unpack(f"<{decoded.sample_count}h", decoded.pcm)) == SAMPLES
    assert decoded.duration_ms == 20


def test_header_layout_is_the_documented_one() -> None:
    data = encode_frame(_frame())
    major, minor = (int(part) for part in PROTOCOL_VERSION.split("."))
    assert HEADER_SIZE == 18
    assert data[:4] == MAGIC
    assert data[4] == major and data[5] == minor
    assert struct.unpack(">I", data[6:10]) == (4_000_000_000,)
    assert struct.unpack(">Q", data[10:18]) == (1_758_700_000_123,)
    assert data[18:] == PCM


def test_wrong_magic_is_rejected() -> None:
    data = b"RIFF" + encode_frame(_frame())[4:]
    with pytest.raises(WrongMagicError, match="magic"):
        decode_frame(data)


def test_incompatible_major_version_is_rejected() -> None:
    data = bytearray(encode_frame(_frame()))
    data[4] += 1
    with pytest.raises(IncompatibleAudioFrameVersionError, match="incompatible protocol_version"):
        decode_frame(bytes(data))
    with pytest.raises(IncompatibleProtocolVersionError):
        decode_frame(bytes(data))


def test_newer_minor_version_is_accepted() -> None:
    data = bytearray(encode_frame(_frame()))
    data[5] += 1
    assert decode_frame(bytes(data)).pcm == PCM


def test_truncated_header_is_rejected() -> None:
    with pytest.raises(AudioFrameError, match="shorter than its header"):
        decode_frame(encode_frame(_frame())[: HEADER_SIZE - 1])


def test_odd_payload_is_not_pcm16() -> None:
    with pytest.raises(AudioFrameError, match="whole number of samples"):
        decode_frame(encode_frame(_frame()) + b"\x00")


@pytest.mark.parametrize(("seq", "client_time_ms"), [(-1, 0), (2**32, 0), (0, -1), (0, 2**64)])
def test_out_of_range_header_fields_are_rejected(seq: int, client_time_ms: int) -> None:
    with pytest.raises(AudioFrameError, match="does not fit"):
        AudioFrame(seq=seq, client_time_ms=client_time_ms, pcm=b"")
