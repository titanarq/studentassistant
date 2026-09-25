"""`serve --record`: each session's raw client inputs, written as a recording `replay` reads back.

The WebSocket gateway and the capture endpoint call a `SessionRecorder` (on `app.state.recorder`,
`None` unless `create_app` was given one) with exactly what the client sent and the backend
accepted: the client transcript messages (a resent final or a partial overtaken by its final is
not recorded twice), the audio frames in `seq` order as they are fed to the provider (reassembled
into `audio.wav`), the `button`/`marker` messages, and each newly stored capture burst with its
images. What the backend derives from them (normalised segments, events, sources) is not recorded:
that is what a replay produces again.

Each session gets `<recordings_dir>/<session_id>/`, written through `recording.RecordingWriter`.
The manifest is written when the session's first socket completes `hello`: that is when the client
clock offset is known, so `started_client_time_ms` is the session start on the client's clock (a
capture uploaded before any `hello` assumes the client clock is the backend's). A session resumed
after a backend restart finds its directory taken and records into `<session_id>-2/` (and so on),
so no recording is ever overwritten or mixed with another run's audio.

Everything here is blocking file I/O: the async callers run it in a worker thread. The recordings
directory is never inside the vault (`create_app` refuses one that is).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from studentassistant.protocol import (
    Button,
    CaptureUploadRequest,
    Marker,
    TranscriptClientFinal,
    TranscriptClientPartial,
)
from studentassistant.protocol.audio import SAMPLE_RATE_HZ, SAMPLE_WIDTH_BYTES
from studentassistant.server.recording import FORMAT_VERSION, RecordingManifest, RecordingWriter
from studentassistant.server.sessions import OpenSession

DEFAULT_CLIENT_STT_PROVIDER = "replay"
"""The recognizer a manifest names when the session's client never said which one it runs."""


class _SessionRecording:
    def __init__(self, writer: RecordingWriter) -> None:
        self.writer = writer
        self.audio_bytes = 0


class SessionRecorder:
    """Writes one recording per session under `root` (`[server].recordings_dir`)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._recordings: dict[str, _SessionRecording] = {}
        self._lock = threading.Lock()

    def directory(self, session_id: str) -> Path | None:
        """Where the session is being recorded; `None` before anything of it was recorded."""
        recording = self._recordings.get(session_id)
        return None if recording is None else recording.writer.directory

    def start(
        self,
        session: OpenSession,
        *,
        stt_mode: Literal["client", "server"],
        language: str,
        stt_provider: str,
        clock_offset_ms: int,
    ) -> None:
        """Open the session's recording at its first `hello`; later sockets change nothing."""
        self._open(session, stt_mode, language, stt_provider, clock_offset_ms)

    def transcript(
        self, session_id: str, message: TranscriptClientPartial | TranscriptClientFinal
    ) -> None:
        recording = self._recordings.get(session_id)
        if recording is not None:
            recording.writer.append_transcript(message)

    def event(self, session_id: str, message: Button | Marker) -> None:
        recording = self._recordings.get(session_id)
        if recording is not None:
            recording.writer.append_event(message)

    def audio(self, session_id: str, pcm: bytes, session_start_s: float) -> None:
        """Append one fed audio frame; the first one is preceded by silence back to the session
        start, so a sample's position in `audio.wav` stays its time in the session."""
        recording = self._recordings.get(session_id)
        if recording is None:
            return
        if recording.audio_bytes == 0:
            lead = round(session_start_s * SAMPLE_RATE_HZ) * SAMPLE_WIDTH_BYTES
            if lead > 0:
                recording.writer.append_audio(bytes(lead))
                recording.audio_bytes += lead
        recording.writer.append_audio(pcm)
        recording.audio_bytes += len(pcm)

    def capture(
        self,
        session: OpenSession,
        metadata: CaptureUploadRequest,
        images: dict[str, bytes],
        *,
        stt_mode: Literal["client", "server"],
        language: str,
    ) -> None:
        """Record a newly stored burst: its metadata and every image it names."""
        recording = self._open(session, stt_mode, language, DEFAULT_CLIENT_STT_PROVIDER, 0)
        recording.writer.add_capture(metadata, images)

    def close(self, session_id: str) -> None:
        """Finish the session's recording (`audio.wav`'s header); nothing more is recorded."""
        with self._lock:
            recording = self._recordings.pop(session_id, None)
        if recording is not None:
            recording.writer.close()

    def close_all(self) -> None:
        for session_id in list(self._recordings):
            self.close(session_id)

    def _open(
        self,
        session: OpenSession,
        stt_mode: Literal["client", "server"],
        language: str,
        stt_provider: str,
        clock_offset_ms: int,
    ) -> _SessionRecording:
        with self._lock:
            recording = self._recordings.get(session.session_id)
            if recording is not None:
                return recording
            manifest = RecordingManifest(
                format_version=FORMAT_VERSION,
                subject=session.subject_id,
                topic=session.topic_id,
                language=language,
                stt_mode=stt_mode,
                stt_provider=stt_provider,
                started_client_time_ms=max(0, session.started_at_ms - clock_offset_ms),
            )
            recording = _SessionRecording(
                RecordingWriter(self._free_directory(session.session_id), manifest)
            )
            self._recordings[session.session_id] = recording
            return recording

    def _free_directory(self, session_id: str) -> Path:
        candidate = self.root / session_id
        attempt = 1
        while candidate.exists():
            attempt += 1
            candidate = self.root / f"{session_id}-{attempt}"
        return candidate


__all__ = ["DEFAULT_CLIENT_STT_PROVIDER", "SessionRecorder"]
