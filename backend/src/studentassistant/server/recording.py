"""The on-disk format of a recorded session, which `replay` feeds back through the gateway.

A recording is a directory holding what one capture client sent during one session, and nothing
the backend derived from it:

    manifest.yaml      `RecordingManifest`: format version, subject, topic, language, `stt_mode`
                       and the client-clock time the session started
    transcript.jsonl   client mode: one `transcript.client.partial`/`.final` message per line
    audio.wav          server mode: PCM16, 16000 Hz, mono, starting at the session's start
    events.jsonl       one `button` or `marker` client message per line
    captures.jsonl     one `rest.sessions.captures.request` metadata object per line, in the
                       order the bursts were uploaded
    captures/          the images of those bursts, `<capture_id>.<part>.<ext>` (`ext` from the
                       image's `content_type`, e.g. `.jpg`)

Every line and the manifest are the protocol's own models, so a recording holds exactly what
the wire carries, with the client times the client gave it. The directory is read and written
only through this module: `RecordingWriter` appends as a session runs (the recorder) or all at
once (a fixture), `read_recording` validates the whole directory up front and refuses anything
it cannot read as a `RecordingError`, so replay never starts on a half-valid recording.

A recording is never a vault file: it lives under `[server].recordings_dir`, outside the vault.
"""

from __future__ import annotations

import json
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from studentassistant.protocol import (
    Button,
    CaptureUploadRequest,
    Marker,
    ProtocolModel,
    TranscriptClientFinal,
    TranscriptClientPartial,
)
from studentassistant.protocol.audio import CHANNELS, SAMPLE_RATE_HZ, SAMPLE_WIDTH_BYTES
from studentassistant.protocol.base import EpochMs, Id
from studentassistant.protocol.client import Language, ProviderId

FORMAT_VERSION = 1

MANIFEST_FILE_NAME = "manifest.yaml"
TRANSCRIPT_FILE_NAME = "transcript.jsonl"
AUDIO_FILE_NAME = "audio.wav"
EVENTS_FILE_NAME = "events.jsonl"
CAPTURES_FILE_NAME = "captures.jsonl"
CAPTURES_DIR_NAME = "captures"

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

TranscriptMessage = Annotated[
    TranscriptClientPartial | TranscriptClientFinal, Field(discriminator="type")
]
EventMessage = Annotated[Button | Marker, Field(discriminator="type")]

_TRANSCRIPT_ADAPTER: TypeAdapter[TranscriptMessage] = TypeAdapter(TranscriptMessage)
_EVENT_ADAPTER: TypeAdapter[EventMessage] = TypeAdapter(EventMessage)


class RecordingError(ValueError):
    """A recording directory this backend cannot read: missing, malformed or inconsistent."""


class RecordingManifest(ProtocolModel):
    """`manifest.yaml`: what a replay needs to start the session the recording came from."""

    format_version: Literal[1]
    # Subject and topic ids as the REST API names them; `replay --topic` overrides both.
    subject: Id
    topic: Id
    language: Language
    stt_mode: Literal["client", "server"]
    # The client recognizer to announce in `hello`; only meaningful in client mode.
    stt_provider: ProviderId = "replay"
    # Client-clock time the session started; every other client time is relative to it.
    started_client_time_ms: EpochMs


@dataclass(frozen=True)
class RecordedCapture:
    """One uploaded burst: its metadata and the path of each image, in `metadata.images` order."""

    metadata: CaptureUploadRequest
    image_paths: tuple[Path, ...]


@dataclass(frozen=True)
class Recording:
    """A whole recording, validated: what `read_recording` returns and replay consumes."""

    directory: Path
    manifest: RecordingManifest
    transcript: tuple[TranscriptClientPartial | TranscriptClientFinal, ...]
    events: tuple[Button | Marker, ...]
    captures: tuple[RecordedCapture, ...]
    # `None` when the recording has no `audio.wav`.
    audio_path: Path | None

    def read_audio(self) -> bytes:
        """The PCM16 samples of `audio.wav`; empty when the recording has no audio."""
        return b"" if self.audio_path is None else read_wav(self.audio_path)


def capture_image_path(directory: Path, capture_id: str, part: str, content_type: str) -> Path:
    """Where the image `part` of burst `capture_id` lives inside the recording `directory`."""
    return directory / CAPTURES_DIR_NAME / f"{capture_id}.{part}{_EXTENSIONS[content_type]}"


class RecordingWriter:
    """Writes one recording directory, appending as the session runs.

    The manifest is written when the writer is created, so a directory that exists is always
    one `read_recording` can at least open. Nothing here is fsynced: a recording is a
    development aid, not content, and losing its last lines to a crash loses no study material.
    """

    def __init__(self, directory: Path, manifest: RecordingManifest) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        (directory / MANIFEST_FILE_NAME).write_text(
            yaml.safe_dump(manifest.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self._audio: WavWriter | None = None

    def append_transcript(self, message: TranscriptClientPartial | TranscriptClientFinal) -> None:
        _append_line(self.directory / TRANSCRIPT_FILE_NAME, message)

    def append_event(self, message: Button | Marker) -> None:
        _append_line(self.directory / EVENTS_FILE_NAME, message)

    def add_capture(self, metadata: CaptureUploadRequest, images: dict[str, bytes]) -> None:
        """Store a burst: every image its metadata names (by `part`), then the metadata line.

        Raises:
            RecordingError: when `images` does not hold exactly the parts `metadata` names.
        """
        named = {image.part for image in metadata.images}
        if named != set(images):
            raise RecordingError(
                f"capture {metadata.capture_id} names parts {sorted(named)} "
                f"but was given {sorted(images)}"
            )
        (self.directory / CAPTURES_DIR_NAME).mkdir(exist_ok=True)
        for image in metadata.images:
            path = capture_image_path(
                self.directory, metadata.capture_id, image.part, image.content_type
            )
            path.write_bytes(images[image.part])
        _append_line(self.directory / CAPTURES_FILE_NAME, metadata)

    def append_audio(self, pcm: bytes) -> None:
        """Append PCM16 16 kHz mono samples to `audio.wav`, creating it on the first call."""
        if self._audio is None:
            self._audio = WavWriter(self.directory / AUDIO_FILE_NAME)
        self._audio.write(pcm)

    def close(self) -> None:
        """Finish `audio.wav`'s header; the JSONL files are complete after every append."""
        if self._audio is not None:
            self._audio.close()
            self._audio = None

    def __enter__(self) -> RecordingWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_recording(directory: Path) -> Recording:
    """Read and validate the whole recording in `directory`.

    Raises:
        RecordingError: when the directory or its manifest is missing, any file does not hold
            what this format says it holds, a capture image is missing, or a recording in
            client mode carries audio (or one in server mode carries a transcript).
    """
    manifest = read_manifest(directory)
    transcript = tuple(
        _read_lines(directory / TRANSCRIPT_FILE_NAME, _TRANSCRIPT_ADAPTER.validate_python)
    )
    events = tuple(_read_lines(directory / EVENTS_FILE_NAME, _EVENT_ADAPTER.validate_python))
    captures = tuple(_read_captures(directory))
    audio_path = directory / AUDIO_FILE_NAME
    has_audio = audio_path.is_file()
    if manifest.stt_mode == "client" and has_audio:
        raise RecordingError(f"{directory} is a client-mode recording but holds {AUDIO_FILE_NAME}")
    if manifest.stt_mode == "server" and transcript:
        raise RecordingError(
            f"{directory} is a server-mode recording but holds {TRANSCRIPT_FILE_NAME}"
        )
    if has_audio:
        read_wav(audio_path)  # validates the format before replay starts
    return Recording(
        directory=directory,
        manifest=manifest,
        transcript=transcript,
        events=events,
        captures=captures,
        audio_path=audio_path if has_audio else None,
    )


def read_manifest(directory: Path) -> RecordingManifest:
    """Read `manifest.yaml`, refusing a missing, malformed or unknown-version one."""
    path = directory / MANIFEST_FILE_NAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecordingError(f"{directory} is not a recording: {path} is missing") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RecordingError(f"cannot read {path}: {exc}") from exc
    try:
        return RecordingManifest.model_validate(data)
    except ValidationError as exc:
        raise RecordingError(f"{path} is not a valid recording manifest: {exc}") from exc


class WavWriter:
    """Streams PCM16 16 kHz mono samples into a WAV file; the header is final on `close`."""

    def __init__(self, path: Path) -> None:
        self._wave = wave.open(str(path), "wb")  # noqa: SIM115 -- closed by `close`
        self._wave.setnchannels(CHANNELS)
        self._wave.setsampwidth(SAMPLE_WIDTH_BYTES)
        self._wave.setframerate(SAMPLE_RATE_HZ)

    def write(self, pcm: bytes) -> None:
        if len(pcm) % SAMPLE_WIDTH_BYTES:
            raise ValueError(f"PCM16 needs an even number of bytes, got {len(pcm)}")
        self._wave.writeframesraw(pcm)

    def close(self) -> None:
        self._wave.close()


def write_wav(path: Path, pcm: bytes) -> None:
    """Write `pcm` (PCM16 16 kHz mono) as a whole WAV file."""
    writer = WavWriter(path)
    try:
        writer.write(pcm)
    finally:
        writer.close()


def read_wav(path: Path) -> bytes:
    """The samples of a WAV file, refusing any format but PCM16 16 kHz mono.

    Raises:
        RecordingError: when the file is not a WAV file or has another format.
    """
    try:
        with wave.open(str(path), "rb") as reader:
            fmt = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
            if fmt != (CHANNELS, SAMPLE_WIDTH_BYTES, SAMPLE_RATE_HZ):
                raise RecordingError(
                    f"{path} is {fmt[0]} channel(s), {fmt[1] * 8}-bit, {fmt[2]} Hz; "
                    f"a recording's audio must be mono, 16-bit, {SAMPLE_RATE_HZ} Hz"
                )
            return reader.readframes(reader.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise RecordingError(f"cannot read {path} as WAV: {exc}") from exc


def _append_line(path: Path, message: BaseModel) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(message.model_dump_json(exclude_none=True) + "\n")


def _read_lines[T](path: Path, validate: Callable[[object], T]) -> Iterator[T]:
    """Validate each non-blank line of a JSONL file; a file that is absent holds nothing."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordingError(f"cannot read {path}: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            yield validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RecordingError(f"{path}:{number} is not a valid line: {exc}") from exc


def _read_captures(directory: Path) -> Iterator[RecordedCapture]:
    for metadata in _read_lines(
        directory / CAPTURES_FILE_NAME, CaptureUploadRequest.model_validate
    ):
        paths = tuple(
            capture_image_path(directory, metadata.capture_id, image.part, image.content_type)
            for image in metadata.images
        )
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise RecordingError(
                f"capture {metadata.capture_id} of {directory} misses its image(s) {missing}"
            )
        yield RecordedCapture(metadata=metadata, image_paths=paths)
