"""The recording format of `server/recording.py`: what the writer writes, the reader reads back."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest
import yaml

from studentassistant.protocol import (
    Button,
    CaptureImage,
    CaptureUploadRequest,
    Marker,
    TranscriptClientFinal,
    TranscriptClientPartial,
)
from studentassistant.server.recording import (
    AUDIO_FILE_NAME,
    MANIFEST_FILE_NAME,
    RecordingError,
    RecordingManifest,
    RecordingWriter,
    read_manifest,
    read_recording,
    read_wav,
    write_wav,
)

START_MS = 1_760_000_000_000
CAPTURE_ID = "0b9e6f4e-2f2a-4c1e-9a53-3b1d2f0c7e11"


def _manifest(stt_mode: str = "client") -> RecordingManifest:
    return RecordingManifest(
        format_version=1,
        subject="biologia",
        topic="la-celula",
        language="es-ES",
        stt_mode=stt_mode,  # type: ignore[arg-type]
        stt_provider="web-speech",
        started_client_time_ms=START_MS,
    )


def _segment(kind: type, segment_id: str, text: str, offset_ms: int):  # noqa: ANN202
    return kind(
        type="transcript.client.final"
        if kind is TranscriptClientFinal
        else "transcript.client.partial",
        segment_id=segment_id,
        client_start_ms=START_MS + offset_ms,
        client_end_ms=START_MS + offset_ms + 900,
        text=text,
        provider="web-speech",
        language="es-ES",
    )


def _capture() -> CaptureUploadRequest:
    return CaptureUploadRequest(
        capture_id=CAPTURE_ID,
        trigger="button",
        client_time_ms=START_MS + 3000,
        images=[
            CaptureImage(
                part="image_0",
                content_type="image/jpeg",
                width_px=4,
                height_px=3,
                client_time_ms=START_MS + 3000,
            ),
            CaptureImage(
                part="image_1",
                content_type="image/png",
                width_px=4,
                height_px=3,
                client_time_ms=START_MS + 3100,
            ),
        ],
    )


def test_client_mode_recording_round_trips(tmp_path: Path) -> None:
    directory = tmp_path / "rec"
    partial = _segment(TranscriptClientPartial, "s1", "la célula", 0)
    final = _segment(TranscriptClientFinal, "s1", "La célula es la unidad básica.", 0)
    button = Button(
        type="button", button="switch_source", source="book", client_time_ms=START_MS + 2000
    )
    marker = Marker(type="marker", client_time_ms=START_MS + 2500, label="ojo")
    images = {"image_0": b"\xff\xd8jpeg-bytes", "image_1": b"\x89PNGpng-bytes"}

    with RecordingWriter(directory, _manifest()) as writer:
        writer.append_transcript(partial)
        writer.append_transcript(final)
        writer.append_event(button)
        writer.append_event(marker)
        writer.add_capture(_capture(), images)

    recording = read_recording(directory)

    assert recording.manifest == _manifest()
    assert recording.transcript == (partial, final)
    assert recording.events == (button, marker)
    assert recording.audio_path is None
    assert recording.read_audio() == b""
    (capture,) = recording.captures
    assert capture.metadata == _capture()
    assert [path.name for path in capture.image_paths] == [
        f"{CAPTURE_ID}.image_0.jpg",
        f"{CAPTURE_ID}.image_1.png",
    ]
    assert [path.read_bytes() for path in capture.image_paths] == list(images.values())
    # The manifest is plain YAML a human can read and edit.
    assert yaml.safe_load((directory / MANIFEST_FILE_NAME).read_text())["topic"] == "la-celula"


def test_server_mode_audio_round_trips_across_appends(tmp_path: Path) -> None:
    directory = tmp_path / "rec"
    first = struct.pack("<4h", 0, 1000, -1000, 32767)
    second = struct.pack("<2h", -32768, 7)

    with RecordingWriter(directory, _manifest("server")) as writer:
        writer.append_audio(first)
        writer.append_audio(second)

    recording = read_recording(directory)
    assert recording.audio_path == directory / AUDIO_FILE_NAME
    assert recording.read_audio() == first + second
    assert recording.transcript == ()


def test_wav_helpers_round_trip_and_refuse_other_formats(tmp_path: Path) -> None:
    pcm = struct.pack("<3h", 1, 2, 3)
    write_wav(tmp_path / "a.wav", pcm)
    assert read_wav(tmp_path / "a.wav") == pcm

    with wave.open(str(tmp_path / "stereo.wav"), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(pcm + pcm[:2])
    with pytest.raises(RecordingError, match="mono"):
        read_wav(tmp_path / "stereo.wav")

    (tmp_path / "junk.wav").write_bytes(b"not a wav")
    with pytest.raises(RecordingError, match="as WAV"):
        read_wav(tmp_path / "junk.wav")


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param(None, id="missing"),
        pytest.param(":\n  - [unbalanced", id="not-yaml"),
        pytest.param({"format_version": 2}, id="unknown-version"),
        pytest.param(
            {
                "format_version": 1,
                "subject": "biologia",
                "topic": "la-celula",
                "language": "es-ES",
                "stt_mode": "both",
                "started_client_time_ms": START_MS,
            },
            id="bad-stt-mode",
        ),
        pytest.param(
            {
                "format_version": 1,
                "subject": "biologia",
                "topic": "la-celula",
                "language": "es-ES",
                "stt_mode": "client",
                "started_client_time_ms": START_MS,
                "surprise": True,
            },
            id="unknown-field",
        ),
        pytest.param(
            {
                "format_version": 1,
                "subject": "biologia",
                "language": "es-ES",
                "stt_mode": "client",
                "started_client_time_ms": START_MS,
            },
            id="no-topic",
        ),
    ],
)
def test_invalid_manifest_is_refused(tmp_path: Path, manifest: object) -> None:
    directory = tmp_path / "rec"
    directory.mkdir()
    if isinstance(manifest, str):
        (directory / MANIFEST_FILE_NAME).write_text(manifest)
    elif manifest is not None:
        (directory / MANIFEST_FILE_NAME).write_text(yaml.safe_dump(manifest))

    with pytest.raises(RecordingError):
        read_manifest(directory)
    with pytest.raises(RecordingError):
        read_recording(directory)


def test_invalid_lines_and_missing_images_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "rec"
    RecordingWriter(directory, _manifest()).close()
    (directory / "events.jsonl").write_text('{"type": "button", "button": "fly"}\n')
    with pytest.raises(RecordingError, match="events.jsonl:1"):
        read_recording(directory)

    (directory / "events.jsonl").unlink()
    writer = RecordingWriter(directory, _manifest())
    writer.add_capture(_capture(), {"image_0": b"a", "image_1": b"b"})
    next((directory / "captures").glob("*.png")).unlink()
    with pytest.raises(RecordingError, match="misses its image"):
        read_recording(directory)


def test_capture_with_wrong_parts_is_refused_by_the_writer(tmp_path: Path) -> None:
    writer = RecordingWriter(tmp_path / "rec", _manifest())
    with pytest.raises(RecordingError, match="names parts"):
        writer.add_capture(_capture(), {"image_0": b"a"})
    assert not (tmp_path / "rec" / "captures.jsonl").exists()


def test_mode_mismatch_is_refused(tmp_path: Path) -> None:
    client = tmp_path / "client"
    with RecordingWriter(client, _manifest("client")) as writer:
        writer.append_audio(b"\x00\x00")
    with pytest.raises(RecordingError, match="client-mode"):
        read_recording(client)

    server = tmp_path / "server"
    with RecordingWriter(server, _manifest("server")) as writer:
        writer.append_transcript(_segment(TranscriptClientFinal, "s1", "hola", 0))
    with pytest.raises(RecordingError, match="server-mode"):
        read_recording(server)
