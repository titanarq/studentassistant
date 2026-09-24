"""The capture client's session WebSocket, `/ws/sessions/{session_id}` (protocol v1).

One socket per connected capture client of the active session. The route authenticates before
`accept()` (`authenticate_websocket`), refuses a session that is not the active one, then runs the
protocol v1 flow:

1. The first message must be `hello`. An incompatible MAJOR `protocol_version` closes the socket
   with the `check_compatible` message and no `hello.ack`. Otherwise `hello.ack` carries the
   negotiated version, the STT mode of the `[stt]` config (never the client's preference),
   `audio_format` exactly in `server` mode, and `clock_offset_ms` (backend clock minus
   `hello.client_time_ms`), which maps every later client time to backend time and then to session
   time (ms since the session's `started_at_ms`).
2. `client` mode: `transcript.client.*` become `ClientSegment`s ingested by a `TranscriptSink`;
   `server` mode: binary audio frames become `AudioChunk`s fed to the `SpeechToTextProvider`.
   Either way the resulting segments are published on the `SessionBus`: `transcript.final` as a
   persisted event, `transcript.partial` as a notice.
3. `button`, `marker` and the client `ack` are published as persisted events.
4. Bus events `transcript.partial`, `transcript.final`, `command` and `notice` of the session are
   forwarded to the client as the matching server messages.

Every text message is validated with the backend protocol models; an invalid one, one with an
unknown or missing `type`, or a message the current mode does not allow closes the socket with a
reason (never silently ignored).

Resume: the receive state of a session (finals seen, audio `seq` received, the server-side
provider) lives in the gateway, not in the socket, so a client that reconnects and resends from
the last acknowledged audio `seq`, or resends transcript segments, produces no gap and no
duplicate.

Backpressure: nothing here blocks the event loop (vault writes run in worker threads through the
bus). Outbound messages go through the connection's bounded bus subscription, which drops the
oldest notices (partials) first and never a persisted event; out-of-order audio is buffered up to
`MAX_PENDING_FRAMES` frames, past which the socket is closed so the client resends from its ack.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from studentassistant.config import SttSettings
from studentassistant.protocol import (
    AudioFormat,
    AudioFrameError,
    Button,
    ClientAck,
    ClientHello,
    Command,
    HelloAck,
    IncompatibleProtocolVersionError,
    Marker,
    Notice,
    ServerAck,
    TranscriptClientFinal,
    TranscriptClientPartial,
    TranscriptFinal,
    TranscriptPartial,
    decode_frame,
    negotiate,
    parse_client_event,
)
from studentassistant.protocol.base import ProtocolModel
from studentassistant.server.auth import WS_POLICY_VIOLATION, authenticate_websocket
from studentassistant.server.bus import SessionBus, SessionNotAttachedError, Subscription
from studentassistant.server.sessions import OpenSession, SessionService
from studentassistant.stt import (
    AudioChunk,
    ClientSegment,
    InMemoryTranscriptSink,
    NormalisedSegment,
    SpeechToTextProvider,
    TranscriptSink,
    provider_from_settings,
)
from studentassistant.vault import SecretRefused

logger = logging.getLogger(__name__)

# Bus event kinds this gateway publishes.
TRANSCRIPT_PARTIAL = "transcript.partial"
TRANSCRIPT_FINAL = "transcript.final"
BUTTON = "button"
MARKER = "marker"
COMMAND_ACK = "command.ack"
# Bus event kinds it forwards to the client (besides the transcript ones).
COMMAND = "command"
NOTICE = "notice"
FORWARDED_KINDS = frozenset({TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL, COMMAND, NOTICE})

CLOSE_UNKNOWN_SESSION = 4404
"""Close code for a `session_id` that is not the active session (unknown, ended, not resumed)."""
CLOSE_PROTOCOL_VIOLATION = WS_POLICY_VIOLATION
"""Close code for a message protocol v1 does not allow here (and an incompatible version)."""
CLOSE_INTERNAL_ERROR = 1011
"""Close code when the backend cannot serve the socket (e.g. the STT provider cannot be built)."""

MAX_PENDING_FRAMES = 512
"""Out-of-order audio frames buffered per session while waiting for the missing `seq`."""

SERVER_AUDIO_FORMAT = AudioFormat(encoding="pcm16", sample_rate_hz=16000, channels=1)

_MAX_REASON_BYTES = 123  # RFC 6455: a close frame's reason fits in 123 bytes of UTF-8.

SinkFactory = Callable[[SttSettings, float], TranscriptSink]
"""Builds a connection's sink from the `[stt]` settings and the clock offset in seconds (the
client-clock reading at session start, as `InMemoryTranscriptSink` takes it)."""
ProviderFactory = Callable[[SttSettings], SpeechToTextProvider]


def _default_sink(settings: SttSettings, clock_offset: float) -> TranscriptSink:
    return InMemoryTranscriptSink.from_settings(settings, clock_offset=clock_offset)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class _RefusedError(Exception):
    """End the connection with this close code and reason."""

    def __init__(self, reason: str, code: int = CLOSE_PROTOCOL_VIOLATION) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class ReceiveState:
    """What a session has received so far; it outlives every socket of the session."""

    session_id: str
    finals_seen: set[str] = field(default_factory=set)
    # Next audio `seq` to feed; frames below it were received (and fed) already.
    next_seq: int = 0
    # Frames that arrived ahead of `next_seq`, already converted to session time.
    pending: dict[int, AudioChunk] = field(default_factory=dict)
    provider: SpeechToTextProvider | None = None
    # Server-side segments get backend ids; a partial and its final share one.
    segment_counter: int = 0
    open_segment_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def last_contiguous_seq(self) -> int | None:
        """Highest audio `seq` such that every frame up to it has been received."""
        return self.next_seq - 1 if self.next_seq > 0 else None

    def server_segment_id(self, final: bool) -> str:
        if self.open_segment_id is None:
            self.segment_counter += 1
            self.open_segment_id = f"server-{self.segment_counter}"
        segment_id = self.open_segment_id
        if final:
            self.open_segment_id = None
        return segment_id


class SessionGateway:
    """The WebSocket gateway's shared state (one on `app.state.gateway`).

    `stt` is the `[stt]` config section; `sink_factory` builds a connection's `TranscriptSink`
    (default: `InMemoryTranscriptSink`), `provider_factory` a session's server-side provider
    (default: `provider_from_settings`), `clock` gives backend epoch ms. Tests replace them.
    """

    def __init__(
        self,
        bus: SessionBus,
        sessions: SessionService,
        stt: SttSettings,
        *,
        sink_factory: SinkFactory = _default_sink,
        provider_factory: ProviderFactory = provider_from_settings,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self.bus = bus
        self.sessions = sessions
        self.stt = stt
        self.sink_factory = sink_factory
        self.provider_factory = provider_factory
        self.clock = clock
        self._states: dict[str, ReceiveState] = {}

    def state_for(self, session_id: str) -> ReceiveState:
        """The session's receive state; states of any other session are forgotten (at most one
        session is active at a time, so theirs can no longer be resumed over a socket)."""
        state = self._states.get(session_id)
        if state is None:
            self._states = {}
            state = self._states[session_id] = ReceiveState(session_id)
        return state

    async def serve(self, websocket: WebSocket, session_id: str) -> None:
        """Run one socket to its end; the caller has not accepted it yet."""
        principal = await authenticate_websocket(websocket)
        if principal is None:
            return
        await websocket.accept()
        connection = _Connection(self, websocket, session_id)
        try:
            await connection.run()
        except _RefusedError as refusal:
            logger.info("closing session socket %s: %s", session_id, refusal.reason)
            await connection.close(refusal.code, refusal.reason)
        except WebSocketDisconnect:
            pass


class _Connection:
    """One accepted socket: handshake, receive loop and the bus -> client forwarder."""

    def __init__(self, gateway: SessionGateway, websocket: WebSocket, session_id: str) -> None:
        self.gateway = gateway
        self.websocket = websocket
        self.session_id = session_id
        self.bus = gateway.bus
        self._send_lock = asyncio.Lock()
        self.session: OpenSession | None = None
        self.state: ReceiveState | None = None
        self.mode: Literal["client", "server"] | None = None
        self.clock_offset_ms = 0
        self.sink: TranscriptSink | None = None
        self.language = gateway.stt.language

    # -- time ----------------------------------------------------------------------------------

    def backend_ms(self, client_ms: int) -> int:
        return client_ms + self.clock_offset_ms

    def session_ms(self, client_ms: int) -> int:
        assert self.session is not None
        return max(0, self.backend_ms(client_ms) - self.session.started_at_ms)

    # -- sending -------------------------------------------------------------------------------

    async def send(self, message: ProtocolModel) -> None:
        async with self._send_lock:
            await self.websocket.send_text(message.model_dump_json(exclude_none=True))

    async def close(self, code: int, reason: str) -> None:
        if self.websocket.application_state == WebSocketState.DISCONNECTED:
            return
        encoded = reason.encode("utf-8")[:_MAX_REASON_BYTES]
        with contextlib.suppress(Exception):
            async with self._send_lock:
                await self.websocket.close(code=code, reason=encoded.decode("utf-8", "ignore"))

    # -- flow ----------------------------------------------------------------------------------

    async def run(self) -> None:
        session = self.gateway.sessions.get_active(self.session_id)
        if session is None or not self.bus.is_attached(self.session_id):
            raise _RefusedError(
                f"session {self.session_id} is not active: start or resume it first",
                CLOSE_UNKNOWN_SESSION,
            )
        self.session = session
        self.state = self.gateway.state_for(self.session_id)
        hello = await self._receive_hello()
        subscription = self.bus.subscribe(
            name=f"ws:{self.session_id}", session_id=self.session_id, kinds=FORWARDED_KINDS
        )
        forwarder: asyncio.Task[None] | None = None
        try:
            await self._handshake(hello)
            forwarder = asyncio.create_task(self._forward(subscription))
            await self._receive_loop()
        finally:
            subscription.close()
            if forwarder is not None:
                forwarder.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await forwarder

    async def _receive_hello(self) -> ClientHello:
        message = await self.websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if message.get("text") is None:
            raise _RefusedError("audio frame before hello.ack chose server stt_mode")
        event = self._parse(message["text"])
        if not isinstance(event, ClientHello):
            raise _RefusedError(f"the first message must be hello, not {event.type}")
        return event

    async def _handshake(self, hello: ClientHello) -> None:
        try:
            version = negotiate(hello.protocol_version)
        except IncompatibleProtocolVersionError as error:
            raise _RefusedError(str(error)) from None
        settings = self.gateway.stt
        self.mode = settings.mode
        now = self.gateway.clock()
        self.clock_offset_ms = now - hello.client_time_ms
        assert self.session is not None and self.state is not None
        if self.mode == "server":
            if self.state.provider is None:
                try:
                    self.state.provider = self.gateway.provider_factory(settings)
                except Exception as error:
                    logger.exception("the STT provider %r cannot be built", settings.provider)
                    raise _RefusedError(
                        f"STT provider {settings.provider!r} unavailable: {type(error).__name__}",
                        CLOSE_INTERNAL_ERROR,
                    ) from None
            self.language = self.state.provider.language
        else:
            client_start_s = (self.session.started_at_ms - self.clock_offset_ms) / 1000
            self.sink = self.gateway.sink_factory(settings, client_start_s)
        await self.send(
            HelloAck(
                type="hello.ack",
                protocol_version=version,
                stt_mode=self.mode,
                audio_format=SERVER_AUDIO_FORMAT if self.mode == "server" else None,
                clock_offset_ms=self.clock_offset_ms,
                server_time_ms=now,
            )
        )
        acked = self.state.last_contiguous_seq
        if self.mode == "server" and acked is not None:
            # A reconnecting client learns where to resume its audio.
            await self.send(ServerAck(type="ack", audio_seq=acked, server_time_ms=now))

    async def _receive_loop(self) -> None:
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await self._on_text(message["text"])
            elif message.get("bytes") is not None:
                await self._on_audio(message["bytes"])

    def _parse(self, text: str) -> Any:
        try:
            data = json.loads(text)
        except ValueError:
            raise _RefusedError("a text message must be a JSON object") from None
        try:
            return parse_client_event(data)
        except ValidationError as error:
            kind = data.get("type") if isinstance(data, dict) else None
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise _RefusedError(
                f"invalid message (type {kind!r}): {where}: {first['type']}"
            ) from None

    # -- client messages -----------------------------------------------------------------------

    async def _on_text(self, text: str) -> None:
        event = self._parse(text)
        if isinstance(event, ClientHello):
            raise _RefusedError("hello was already received")
        if isinstance(event, TranscriptClientPartial | TranscriptClientFinal):
            if self.mode != "client":
                raise _RefusedError("transcript.client.* is not accepted in server stt_mode")
            await self._on_client_segment(event)
        elif isinstance(event, Button):
            payload: dict[str, Any] = {"button": event.button}
            if event.source is not None:
                payload["source"] = event.source
            await self._publish_client_event(BUTTON, payload, event.client_time_ms)
        elif isinstance(event, Marker):
            payload = {} if event.label is None else {"label": event.label}
            await self._publish_client_event(MARKER, payload, event.client_time_ms)
        elif isinstance(event, ClientAck):
            await self._publish_client_event(
                COMMAND_ACK, {"command_id": event.command_id}, event.client_time_ms
            )

    async def _publish_client_event(
        self, kind: str, payload: dict[str, Any], client_time_ms: int
    ) -> None:
        payload["client_time_ms"] = client_time_ms
        payload["backend_time_ms"] = self.backend_ms(client_time_ms)
        await self._publish(kind, "phone", payload, t=self.session_ms(client_time_ms))

    async def _on_client_segment(
        self, event: TranscriptClientPartial | TranscriptClientFinal
    ) -> None:
        assert self.state is not None and self.sink is not None
        final = isinstance(event, TranscriptClientFinal)
        async with self.state.lock:
            if event.segment_id in self.state.finals_seen:
                return  # a resent final, or a partial overtaken by its final
            normalised = await self.sink.ingest(
                ClientSegment(
                    client_start=event.client_start_ms / 1000,
                    client_end=event.client_end_ms / 1000,
                    text=event.text,
                    confidence=event.confidence,
                    is_final=final,
                )
            )
            await self._publish_segment(event.segment_id, normalised, event.language)

    # -- server-mode audio ---------------------------------------------------------------------

    async def _on_audio(self, data: bytes) -> None:
        if self.mode != "server":
            raise _RefusedError("audio frames are accepted only in server stt_mode")
        try:
            frame = decode_frame(data)
        except AudioFrameError as error:
            raise _RefusedError(str(error)) from None
        state = self.state
        assert state is not None and state.provider is not None
        async with state.lock:
            if frame.seq >= state.next_seq and frame.seq not in state.pending:
                if frame.seq - state.next_seq >= MAX_PENDING_FRAMES:
                    raise _RefusedError(
                        f"audio frame {frame.seq} is too far ahead of {state.next_seq}: "
                        "resend from the last ack"
                    )
                state.pending[frame.seq] = AudioChunk(
                    start=self.session_ms(frame.client_time_ms) / 1000, data=frame.pcm
                )
                while state.next_seq in state.pending:
                    chunk = state.pending.pop(state.next_seq)
                    state.next_seq += 1
                    for segment in await state.provider.feed(chunk):
                        await self._publish_segment(
                            state.server_segment_id(segment.is_final), segment, self.language
                        )
            acked = state.last_contiguous_seq
        if acked is not None:
            await self.send(
                ServerAck(type="ack", audio_seq=acked, server_time_ms=self.gateway.clock())
            )

    # -- publishing ----------------------------------------------------------------------------

    async def _publish_segment(
        self, segment_id: str, segment: NormalisedSegment, language: str
    ) -> None:
        """Publish one normalised segment (the caller holds the state lock)."""
        assert self.state is not None
        start_ms = round(segment.start * 1000)
        payload: dict[str, Any] = {
            "segment_id": segment_id,
            "session_start_ms": start_ms,
            "session_end_ms": max(start_ms, round(segment.end * 1000)),
            "text": segment.text,
            "language": language,
            "provider": segment.provider,
        }
        if segment.confidence is not None:
            payload["confidence"] = segment.confidence
        if segment.is_final:
            await self._publish(TRANSCRIPT_FINAL, "stt", payload, t=start_ms)
            # Also a refused (secret-looking) final is settled: a resend must not retry it.
            self.state.finals_seen.add(segment_id)
        else:
            await self._publish(TRANSCRIPT_PARTIAL, "stt", payload, t=start_ms, persist=False)

    async def _publish(
        self,
        kind: str,
        origin: Literal["phone", "stt"],
        payload: Mapping[str, Any],
        *,
        t: int,
        persist: bool = True,
    ) -> None:
        try:
            await self.bus.publish(self.session_id, kind, origin, payload, persist=persist, t=t)
        except SessionNotAttachedError:
            raise _RefusedError(
                f"session {self.session_id} is no longer active", CLOSE_UNKNOWN_SESSION
            ) from None
        except SecretRefused:
            logger.warning("a %s event of session %s was refused as secret", kind, self.session_id)

    # -- bus -> client -------------------------------------------------------------------------

    async def _forward(self, subscription: Subscription) -> None:
        async for event in subscription:
            message = _server_message(event.kind, event.payload, self.gateway.clock)
            if message is None:
                continue
            try:
                await self.send(message)
            except Exception:
                return  # the socket is gone; the receive loop ends the connection


def _server_message(
    kind: str, payload: Mapping[str, Any], clock: Callable[[], int]
) -> ProtocolModel | None:
    """The protocol v1 server message for a forwarded bus event; None (logged) if it has none."""
    try:
        if kind in (TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL):
            model = TranscriptPartial if kind == TRANSCRIPT_PARTIAL else TranscriptFinal
            fields = model.model_fields.keys() - {"type"}
            return model.model_validate(
                {"type": kind, **{key: payload[key] for key in fields if key in payload}}
            )
        if kind == COMMAND:
            return Command(
                type="command",
                command_id=payload["command_id"],
                command=payload["command"],
                server_time_ms=payload.get("server_time_ms", clock()),
            )
        if kind == NOTICE:
            return Notice(
                type="notice",
                pending_count=payload["pending_count"],
                server_time_ms=payload.get("server_time_ms", clock()),
            )
    except (KeyError, ValidationError):
        logger.warning("a %s bus event does not make a protocol v1 message; not forwarded", kind)
    return None


def ws_router() -> APIRouter:
    """`/ws/sessions/{session_id}`; the gateway is read from `app.state.gateway`."""
    router = APIRouter(prefix="/ws")

    @router.websocket("/sessions/{session_id}")
    async def session_socket(websocket: WebSocket, session_id: str) -> None:
        gateway: SessionGateway = websocket.app.state.gateway
        await gateway.serve(websocket, session_id)

    return router


__all__ = [
    "BUTTON",
    "CLOSE_INTERNAL_ERROR",
    "CLOSE_PROTOCOL_VIOLATION",
    "CLOSE_UNKNOWN_SESSION",
    "COMMAND",
    "COMMAND_ACK",
    "FORWARDED_KINDS",
    "MARKER",
    "MAX_PENDING_FRAMES",
    "NOTICE",
    "TRANSCRIPT_FINAL",
    "TRANSCRIPT_PARTIAL",
    "ReceiveState",
    "SessionGateway",
    "ws_router",
]
