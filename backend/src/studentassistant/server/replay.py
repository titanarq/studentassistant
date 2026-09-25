"""Replay a recorded session through the gateway, acting as a capture client (protocol v1).

`replay` feeds a `Recording` (`server/recording.py`) through exactly the path a live capture
client takes, and nothing else:

1. make sure the recording's subject and topic exist (`GET`/`POST /api/subjects...`, the id as
   the name when one is created), then `POST /api/sessions` with the recording's start time;
2. connect `WS /ws/sessions/{id}`, send `hello` and wait for `hello.ack`, refusing a backend whose
   STT mode is not the recording's;
3. send every transcript message (`transcript.client.*`, due at its `client_end_ms`) and every
   `button`/`marker` (due at its `client_time_ms`), and upload every capture burst through
   `POST /api/sessions/{id}/captures` (due at its `client_time_ms`), in client-time order, each
   at its recorded offset from the session start divided by `speed`; a server-mode recording
   sends `audio.wav` instead of transcript messages, sliced into `AUDIO_FRAME_MS` binary frames
   (the protocol's `encode_frame`, `seq` from 0, each due when its last sample was captured);
4. `POST /api/sessions/{id}/end`, due at the last recorded time, then close the socket.

Every message carries the client times it was recorded with, and `hello` the recording's start
time: the backend's clock offset then maps them to the session times they had when recorded,
whatever the replay speed.

Before each upload and before the end the replay settles: it waits until the backend has
processed every WebSocket message sent so far (a capture is stored under the source context of
the `switch_source` buttons before it, read from the session's events) and has echoed every
final it was sent (up to `confirm_timeout_s`, since a final refused as a secret is never echoed).
In server mode it also waits for the backend's `ack` of the last audio frame sent.

Server-mode audio survives a dropped socket: when the backend closes it (or the connection
drops), the replay reconnects to the same `ws_path` (up to `MAX_RECONNECTS` times), sends `hello`
again with a client time that keeps the first connection's clock offset, and resends every frame
after the highest `audio_seq` the backend acknowledged. The gateway keeps the session's receive
state across sockets and ignores a frame it already had, so nothing is lost or fed twice.

Pacing reads `clock` and waits with `sleep`, both injectable, so a test replays without waiting.
The backend is reached through a `ReplayTransport`: `AsgiTransport` drives an in-process app
(`create_app`) directly through ASGI, `HttpTransport` talks to a running server at a base URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import quote, urlsplit, urlunsplit

import websockets
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection

from studentassistant.protocol import (
    AudioFormat,
    Button,
    CaptureUploadRequest,
    ClientCapabilities,
    ClientHello,
    HelloAck,
    Marker,
    SessionEndRequest,
    SessionStartRequest,
    TranscriptClientFinal,
    TranscriptClientPartial,
)
from studentassistant.protocol import Session as SessionResponse
from studentassistant.protocol.audio import (
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    AudioFrame,
    encode_frame,
)
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server.recording import RecordedCapture, Recording

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

DEFAULT_CONFIRM_TIMEOUT_S = 5.0
"""How long a settle waits for the echo of the finals sent before it goes on without them."""

_HELLO_TIMEOUT_S = 10.0

AUDIO_FRAME_MS = 100
"""Length of the audio slice each binary frame carries in a server-mode replay."""

MAX_RECONNECTS = 3
"""How many times a server-mode replay reconnects a dropped socket before it gives up."""

DEFAULT_ACK_TIMEOUT_S = 10.0
"""How long a settle waits for the backend to acknowledge the last audio frame sent."""


class ReplayError(RuntimeError):
    """The backend refused a step of the replay, or the recording cannot be replayed on it."""


@dataclass(frozen=True)
class Response:
    """An HTTP response as a transport returns it: the status and the raw body."""

    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            return None

    def detail(self) -> str:
        data = self.json()
        if isinstance(data, dict) and "detail" in data:
            return str(data["detail"])
        return self.body.decode("utf-8", errors="replace")[:200]


class ReplaySocket(Protocol):
    """The client side of the session WebSocket."""

    async def send_text(self, text: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def receive_text(self) -> str | None:
        """The next text message from the backend; `None` once the socket is closed."""
        ...

    async def settle(self) -> None:
        """Return once the backend has processed every message sent so far, where the transport
        can know it (in process); a network transport cannot, and returns at once."""
        ...

    async def close(self) -> None: ...


class ReplayTransport(Protocol):
    """How the replay reaches the backend: plain HTTP requests and the session WebSocket."""

    async def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> Response: ...

    async def connect(self, path: str) -> ReplaySocket: ...


@dataclass(frozen=True)
class ReplayResult:
    """What a replay did: the session it ran and how much of the recording it sent."""

    session_id: str
    subject_id: str
    topic_id: str
    partials_sent: int
    finals_sent: int
    events_sent: int
    captures_stored: int
    captures_duplicate: int
    audio_frames_sent: int
    # Frames sent again after a reconnect, from the backend's last acknowledged `seq`.
    audio_frames_resent: int
    reconnects: int
    ended_at_ms: int


# -- the replay ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    client_time_ms: int
    message: (
        TranscriptClientPartial
        | TranscriptClientFinal
        | Button
        | Marker
        | RecordedCapture
        | AudioFrame
    )


def audio_frames(recording: Recording, frame_ms: int = AUDIO_FRAME_MS) -> list[AudioFrame]:
    """`audio.wav` as the binary frames a server-mode client streams: `frame_ms` of samples each
    (the last one shorter), `seq` from 0, stamped with the client time of their first sample."""
    pcm = recording.read_audio()
    step = SAMPLE_RATE_HZ * frame_ms // 1000 * SAMPLE_WIDTH_BYTES
    start_ms = recording.manifest.started_client_time_ms
    return [
        AudioFrame(
            seq=seq,
            client_time_ms=start_ms + offset // SAMPLE_WIDTH_BYTES * 1000 // SAMPLE_RATE_HZ,
            pcm=pcm[offset : offset + step],
        )
        for seq, offset in enumerate(range(0, len(pcm), step))
    ]


def timeline(recording: Recording) -> list[_Step]:
    """Everything the recording sends after `hello`, in client-time order.

    A transcript message is due when the recognizer gave it (`client_end_ms`), an audio frame
    when its last sample was captured. The sort is stable and the input is audio, transcript,
    then events, then captures, so at one instant speech goes before a button, and a button
    before the capture it applies to.
    """
    steps = [
        _Step(frame.client_time_ms + round(frame.duration_ms), frame)
        for frame in audio_frames(recording)
    ]
    steps += [_Step(message.client_end_ms, message) for message in recording.transcript]
    steps += [_Step(message.client_time_ms, message) for message in recording.events]
    steps += [_Step(capture.metadata.client_time_ms, capture) for capture in recording.captures]
    return sorted(steps, key=lambda step: step.client_time_ms)


async def replay(
    recording: Recording,
    transport: ReplayTransport,
    *,
    speed: float = 1.0,
    subject: str | None = None,
    topic: str | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
    ack_timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
) -> ReplayResult:
    """Replay `recording` into the backend `transport` reaches, as the module doc describes.

    `subject`/`topic` override the manifest's (the `--topic` of the CLI). `speed` divides every
    recorded offset: 4 replays a twenty-minute session in five.

    Raises:
        ReplayError: when the backend refuses a request or closes the socket, or the recording's
            STT mode is not the backend's, or a server-mode socket kept dropping.
    """
    if speed <= 0:
        raise ValueError(f"speed must be positive, got {speed}")
    manifest = recording.manifest
    subject_id = subject or manifest.subject
    topic_id = topic or manifest.topic
    await _ensure_topic(transport, subject_id, topic_id)
    start_ms = manifest.started_client_time_ms
    session = SessionResponse.model_validate(
        await _post_json(
            transport,
            "/api/sessions",
            SessionStartRequest(subject_id=subject_id, topic_id=topic_id, client_time_ms=start_ms),
            expect=(201,),
        )
    )
    client = _Client(
        transport,
        session.ws_path,
        stt_mode=manifest.stt_mode,
        stt_provider=manifest.stt_provider,
        confirm_timeout_s=confirm_timeout_s,
        ack_timeout_s=ack_timeout_s,
    )
    try:
        await client.connect(start_ms)
        started = clock()
        partials = finals = events = stored = duplicates = 0
        end_ms = start_ms
        for step in timeline(recording):
            delay = started + (step.client_time_ms - start_ms) / 1000 / speed - clock()
            if delay > 0:
                await sleep(delay)
            end_ms = max(end_ms, step.client_time_ms)
            message = step.message
            if isinstance(message, RecordedCapture):
                await client.settle()
                if await _upload(transport, session.session_id, message):
                    stored += 1
                else:
                    duplicates += 1
                continue
            if isinstance(message, AudioFrame):
                await client.send_audio(message)
                continue
            await client.send(message)
            if isinstance(message, TranscriptClientFinal):
                finals += 1
            elif isinstance(message, TranscriptClientPartial):
                partials += 1
            else:
                events += 1
        await client.settle()
        ended = await _post_json(
            transport,
            f"/api/sessions/{session.session_id}/end",
            SessionEndRequest(client_time_ms=end_ms, reason="button"),
            expect=(200,),
        )
    finally:
        await client.close()
    return ReplayResult(
        session_id=session.session_id,
        subject_id=subject_id,
        topic_id=topic_id,
        partials_sent=partials,
        finals_sent=finals,
        events_sent=events,
        captures_stored=stored,
        captures_duplicate=duplicates,
        audio_frames_sent=len(client.audio_sent),
        audio_frames_resent=client.audio_resent,
        reconnects=client.reconnects,
        ended_at_ms=int(ended["ended_at_ms"]),
    )


class _Client:
    """The WebSocket side of one replay: sends, reads what the backend sends back, and (server
    mode) reconnects a dropped socket and resends the audio the backend did not acknowledge."""

    def __init__(
        self,
        transport: ReplayTransport,
        ws_path: str,
        *,
        stt_mode: str,
        stt_provider: str,
        confirm_timeout_s: float,
        ack_timeout_s: float,
    ) -> None:
        self.transport = transport
        self.ws_path = ws_path
        self.stt_mode = stt_mode
        self.stt_provider = stt_provider
        self.confirm_timeout_s = confirm_timeout_s
        self.ack_timeout_s = ack_timeout_s
        self.finals_sent: set[str] = set()
        self.finals_echoed: set[str] = set()
        # Every audio frame sent so far, by `seq`, and the highest `seq` the backend acknowledged.
        self.audio_sent: list[AudioFrame] = []
        self.audio_acked: int | None = None
        self.audio_resent = 0
        self.reconnects = 0
        self.echoed = asyncio.Condition()
        self.closed = True
        self.socket: ReplaySocket | None = None
        self._reader: asyncio.Task[None] | None = None
        # Client time of the first `hello` and when (real monotonic seconds) it was sent.
        self._first_hello: tuple[int, float] | None = None

    async def connect(self, client_time_ms: int) -> None:
        """Open the session socket and complete the `hello` handshake."""
        socket = await self.transport.connect(self.ws_path)
        self.socket = socket
        hello = ClientHello(
            type="hello",
            protocol_version=PROTOCOL_VERSION,
            capabilities=ClientCapabilities(
                stt=self.stt_mode,  # type: ignore[arg-type]
                stt_provider=self.stt_provider,
                audio_format=(
                    AudioFormat(encoding="pcm16", sample_rate_hz=16000, channels=1)
                    if self.stt_mode == "server"
                    else None
                ),
            ),
            client_time_ms=client_time_ms,
        )
        if self._first_hello is None:
            self._first_hello = (client_time_ms, time.monotonic())
        await socket.send_text(hello.model_dump_json(exclude_none=True))
        try:
            text = await asyncio.wait_for(socket.receive_text(), _HELLO_TIMEOUT_S)
        except TimeoutError:
            raise ReplayError("the backend did not answer hello") from None
        if text is None:
            raise ReplayError(f"the backend closed the socket instead of hello.ack: {socket}")
        try:
            ack = HelloAck.model_validate_json(text)
        except ValidationError:
            raise ReplayError(f"expected hello.ack, got {text[:200]}") from None
        if ack.stt_mode != self.stt_mode:
            raise ReplayError(
                f"the backend runs STT in {ack.stt_mode} mode but the recording is "
                f"{self.stt_mode} mode: set [stt].mode to match it"
            )
        self.closed = False
        self._reader = asyncio.create_task(self._read(socket))

    async def send(self, message: Any) -> None:
        if self.closed:
            raise ReplayError(f"the backend closed the session socket: {self.socket}")
        if isinstance(message, TranscriptClientFinal):
            self.finals_sent.add(message.segment_id)
        assert self.socket is not None
        await self.socket.send_text(message.model_dump_json(exclude_none=True))

    async def send_audio(self, frame: AudioFrame) -> None:
        """Send the next audio frame, reconnecting first when the socket was dropped."""
        assert frame.seq == len(self.audio_sent), "audio frames are sent in `seq` order"
        self.audio_sent.append(frame)
        if self.closed:
            await self._resume()  # resends everything unacknowledged, this frame included
            return
        assert self.socket is not None
        await self.socket.send_bytes(encode_frame(frame))

    async def _resume(self) -> None:
        """Reconnect a dropped server-mode socket and resend from the last acknowledged `seq`."""
        while True:
            if self.reconnects >= MAX_RECONNECTS:
                raise ReplayError(
                    f"the session socket dropped {self.reconnects + 1} times; last {self.socket}"
                )
            self.reconnects += 1
            logger.warning("the session socket dropped (%s); reconnecting", self.socket)
            await self._drop()
            assert self._first_hello is not None
            first_client_ms, first_real_s = self._first_hello
            # Keep the first connection's clock offset: the backend clock has moved on by the
            # real time since the first `hello`, so the client clock "has" too.
            await self.connect(first_client_ms + round((time.monotonic() - first_real_s) * 1000))
            assert self.socket is not None
            # Let the backend's resume `ack` (sent right after `hello.ack`) reach the reader.
            await self.socket.settle()
            first = 0 if self.audio_acked is None else self.audio_acked + 1
            for frame in self.audio_sent[first:]:
                if self.closed:
                    break
                await self.socket.send_bytes(encode_frame(frame))
                self.audio_resent += 1
            if not self.closed:
                return

    async def settle(self) -> None:
        assert self.socket is not None
        await self.socket.settle()
        if self.stt_mode == "server":
            await self._settle_audio()
        if self.closed:
            raise ReplayError(f"the backend closed the session socket: {self.socket}")
        try:
            async with self.echoed:
                await asyncio.wait_for(
                    self.echoed.wait_for(
                        lambda: self.closed or self.finals_sent <= self.finals_echoed
                    ),
                    self.confirm_timeout_s,
                )
        except TimeoutError:
            missing = sorted(self.finals_sent - self.finals_echoed)
            logger.warning("the backend did not echo the final(s) %s; going on", missing)
        if self.closed:
            raise ReplayError(f"the backend closed the session socket: {self.socket}")

    async def _settle_audio(self) -> None:
        """Wait for the `ack` of the last frame sent, resuming a socket dropped meanwhile."""
        last = len(self.audio_sent) - 1
        if last < 0:
            return

        def done() -> bool:
            return self.closed or (self.audio_acked is not None and self.audio_acked >= last)

        while True:
            try:
                async with self.echoed:
                    await asyncio.wait_for(self.echoed.wait_for(done), self.ack_timeout_s)
            except TimeoutError:
                raise ReplayError(
                    f"the backend acknowledged audio up to seq {self.audio_acked}, not {last}"
                ) from None
            if not self.closed:
                return
            await self._resume()

    async def _drop(self) -> None:
        if self.socket is not None:
            await self.socket.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None

    async def close(self) -> None:
        await self._drop()

    async def _read(self, socket: ReplaySocket) -> None:
        while True:
            text = await socket.receive_text()
            async with self.echoed:
                if text is None:
                    self.closed = True
                else:
                    with contextlib.suppress(ValueError):
                        data = json.loads(text)
                        if isinstance(data, dict) and data.get("type") == "transcript.final":
                            self.finals_echoed.add(str(data.get("segment_id")))
                        elif isinstance(data, dict) and data.get("type") == "ack":
                            seq = data.get("audio_seq")
                            if isinstance(seq, int) and (
                                self.audio_acked is None or seq > self.audio_acked
                            ):
                                self.audio_acked = seq
                self.echoed.notify_all()
            if text is None:
                return


async def _ensure_topic(transport: ReplayTransport, subject_id: str, topic_id: str) -> None:
    """Create the subject and topic when the vault has not got them, named by their ids."""
    subjects = await _get_json(transport, "/api/subjects")
    if subject_id not in {subject["subject_id"] for subject in subjects["subjects"]}:
        created = await _post_raw(transport, "/api/subjects", {"name": subject_id}, expect=(201,))
        if created["subject_id"] != subject_id:
            raise ReplayError(f"subject {subject_id!r} cannot be created under that id")
    topics = await _get_json(transport, f"/api/subjects/{quote(subject_id)}/topics")
    if topic_id not in {topic["topic_id"] for topic in topics["topics"]}:
        created = await _post_raw(
            transport, f"/api/subjects/{quote(subject_id)}/topics", {"name": topic_id}, (201,)
        )
        if created["topic_id"] != topic_id:
            raise ReplayError(f"topic {topic_id!r} cannot be created under that id")


async def _upload(transport: ReplayTransport, session_id: str, capture: RecordedCapture) -> bool:
    """Upload one burst; `True` when stored, `False` when the backend already had it."""
    metadata: CaptureUploadRequest = capture.metadata
    parts: list[tuple[str, str | None, str, bytes]] = [
        ("metadata", None, "application/json", metadata.model_dump_json(exclude_none=True).encode())
    ]
    for image, path in zip(metadata.images, capture.image_paths, strict=True):
        parts.append((image.part, path.name, image.content_type, path.read_bytes()))
    body, content_type = encode_multipart(parts)
    response = await transport.request(
        "POST", f"/api/sessions/{session_id}/captures", body, content_type
    )
    if response.status not in (200, 201):
        raise ReplayError(
            f"capture {metadata.capture_id} refused ({response.status}): {response.detail()}"
        )
    return response.status == 201


def encode_multipart(parts: Sequence[tuple[str, str | None, str, bytes]]) -> tuple[bytes, str]:
    """A `multipart/form-data` body of `(name, filename, content_type, data)` parts, and the
    `Content-Type` header that goes with it."""
    boundary = "replay-" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, filename, content_type, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
            f"Content-Type: {content_type}\r\n\r\n".encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


async def _get_json(transport: ReplayTransport, path: str) -> Any:
    response = await transport.request("GET", path)
    if response.status != 200:
        raise ReplayError(f"GET {path} refused ({response.status}): {response.detail()}")
    return response.json()


async def _post_json(
    transport: ReplayTransport, path: str, body: Any, expect: tuple[int, ...]
) -> Any:
    return await _post_raw(transport, path, body.model_dump(mode="json", exclude_none=True), expect)


async def _post_raw(
    transport: ReplayTransport, path: str, body: Mapping[str, Any], expect: tuple[int, ...]
) -> Any:
    response = await transport.request("POST", path, json.dumps(body).encode(), "application/json")
    if response.status not in expect:
        raise ReplayError(f"POST {path} refused ({response.status}): {response.detail()}")
    return response.json()


# -- in process: the app driven through ASGI -------------------------------------------------


class AsgiTransport:
    """Drives an in-process ASGI app (`create_app`) as a trusted loopback client would.

    Use it as an async context manager: with `lifespan` it runs the app's startup on entry and
    its shutdown on exit, as `serve` would (the transcript pipeline, the vault sync loop).
    Requests come from `client` with `Host: host`, which the Host allowlist accepts by default.
    """

    def __init__(
        self,
        app: Any,
        *,
        lifespan: bool = True,
        host: str = "localhost",
        client: tuple[str, int] = ("127.0.0.1", 50000),
    ) -> None:
        self.app = app
        self.lifespan = lifespan
        self.host = host
        self.client = client
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_in: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lifespan_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        if self.lifespan:
            scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
            self._lifespan_task = asyncio.create_task(
                self.app(scope, self._lifespan_in.get, self._lifespan_out.put)
            )
            await self._lifespan_step("startup")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lifespan_task is not None:
            await self._lifespan_step("shutdown")
            await self._lifespan_task
            self._lifespan_task = None

    async def _lifespan_step(self, step: str) -> None:
        await self._lifespan_in.put({"type": f"lifespan.{step}"})
        message = await self._lifespan_out.get()
        if message["type"] != f"lifespan.{step}.complete":
            raise ReplayError(f"the app's lifespan {step} failed: {message.get('message', '')}")

    def _scope(self, kind: str, path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
        return {
            "type": kind,
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "http" if kind == "http" else "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", self.host.encode()), *headers],
            "client": self.client,
            "server": (self.host, 80),
            "state": {},
        }

    async def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> Response:
        headers: list[tuple[bytes, bytes]] = []
        if body is not None:
            headers.append((b"content-length", str(len(body)).encode()))
        if content_type is not None:
            headers.append((b"content-type", content_type.encode()))
        scope = self._scope("http", path, headers)
        scope["method"] = method
        sent = False
        disconnected = asyncio.Event()
        status = 500
        chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body or b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        try:
            await self.app(scope, receive, send)
        finally:
            disconnected.set()
        return Response(status, b"".join(chunks))

    async def connect(self, path: str) -> ReplaySocket:
        socket = _AsgiSocket(self.app, self._scope("websocket", path, []))
        await socket.open()
        return socket


class _AsgiSocket:
    """One in-process WebSocket session; knows when the app is idle, waiting for a message."""

    def __init__(self, app: Any, scope: dict[str, Any]) -> None:
        self.app = app
        self.scope = scope
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbound: asyncio.Queue[str | None] = asyncio.Queue()
        self._idle = asyncio.Event()
        self._accepted = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.close_code: int | None = None
        self.close_reason = ""

    def __str__(self) -> str:
        return f"closed with {self.close_code} {self.close_reason!r}"

    async def open(self) -> None:
        self._put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        waiter = asyncio.create_task(self._accepted.wait())
        await asyncio.wait({waiter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not self._accepted.is_set():
            if self._task.done() and self._task.exception() is not None:
                raise ReplayError(f"the session socket failed: {self._task.exception()!r}")
            raise ReplayError(f"the backend refused the session socket: {self}")

    def _put(self, message: dict[str, Any]) -> None:
        self._idle.clear()
        self._inbound.put_nowait(message)

    async def _receive(self) -> dict[str, Any]:
        if self._inbound.empty():
            self._idle.set()
        return await self._inbound.get()

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            self._accepted.set()
        elif kind == "websocket.send":
            if message.get("text") is not None:
                await self._outbound.put(message["text"])
        elif kind in ("websocket.close", "websocket.http.response.start"):
            self.close_code = message.get("code", message.get("status"))
            self.close_reason = message.get("reason") or ""
            await self._outbound.put(None)

    async def send_text(self, text: str) -> None:
        self._put({"type": "websocket.receive", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        self._put({"type": "websocket.receive", "bytes": data})

    async def receive_text(self) -> str | None:
        if self.close_code is not None and self._outbound.empty():
            return None
        return await self._outbound.get()

    async def settle(self) -> None:
        assert self._task is not None
        idle = asyncio.create_task(self._idle.wait())
        await asyncio.wait({idle, self._task}, return_when=asyncio.FIRST_COMPLETED)
        idle.cancel()
        # Let what the app published reach the socket's forwarder before the caller reads it.
        await asyncio.sleep(0)

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, _HELLO_TIMEOUT_S)
        except TimeoutError:
            self._task.cancel()
        except Exception:
            logger.exception("the in-process session socket failed")
        if self.close_code is None:
            self.close_code = 1000
            await self._outbound.put(None)


# -- over the network: a running server at a base URL -----------------------------------------


class HttpTransport:
    """Talks to a running backend at `base_url` (e.g. `http://localhost:8765`).

    `token` is the device bearer token a LAN address needs; a loopback URL is trusted without
    one. The HTTP requests run in a worker thread (urllib), the WebSocket on `websockets`.
    """

    def __init__(self, base_url: str, *, token: str | None = None, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        return {} if self.token is None else {"Authorization": f"Bearer {self.token}"}

    async def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> Response:
        headers = self._headers()
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )

        def send() -> Response:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as answer:  # noqa: S310
                    return Response(answer.status, answer.read())
            except urllib.error.HTTPError as error:
                return Response(error.code, error.read())
            except urllib.error.URLError as error:
                raise ReplayError(f"cannot reach {self.base_url}: {error.reason}") from None

        return await asyncio.to_thread(send)

    async def connect(self, path: str) -> ReplaySocket:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        url = urlunsplit((scheme, parts.netloc, parts.path + path, "", ""))
        try:
            connection = await websockets.connect(
                url, additional_headers=self._headers(), proxy=None
            )
        except (OSError, websockets.InvalidHandshake) as error:
            raise ReplayError(f"cannot open the session socket {url}: {error}") from None
        return _NetworkSocket(connection)


class _NetworkSocket:
    def __init__(self, connection: ClientConnection) -> None:
        self.connection = connection

    def __str__(self) -> str:
        return f"closed with {self.connection.close_code} {self.connection.close_reason!r}"

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def send_bytes(self, data: bytes) -> None:
        await self._send(data)

    async def _send(self, data: str | bytes) -> None:
        try:
            await self.connection.send(data)
        except websockets.ConnectionClosed:
            raise ReplayError(f"the backend closed the session socket: {self}") from None

    async def receive_text(self) -> str | None:
        while True:
            try:
                message = await self.connection.recv()
            except websockets.ConnectionClosed:
                return None
            if isinstance(message, str):
                return message

    async def settle(self) -> None:
        return None

    async def close(self) -> None:
        await self.connection.close()
