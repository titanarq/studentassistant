"""The harness of the session WebSocket tests (`server/ws.py`); the `ws` fixture builds one."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.stt import (
    ClientSegment,
    InMemoryTranscriptSink,
    NormalisedSegment,
    SpeechToTextProvider,
)
from studentassistant.vault import Event, Vault, read_jsonl


class RecordingSink(InMemoryTranscriptSink):
    """An `InMemoryTranscriptSink` that also keeps every `ClientSegment` it was given."""

    def __init__(self, provider: str, *, clock_offset: float = 0.0) -> None:
        super().__init__(provider, clock_offset=clock_offset)
        self.ingested: list[ClientSegment] = []

    async def ingest(self, segment: ClientSegment) -> NormalisedSegment:
        self.ingested.append(segment)
        return await super().ingest(segment)


class MsClock:
    """Backend epoch milliseconds for the gateway; set `now` to move it."""

    def __init__(self, now: int = 0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class WsHarness:
    """What a WebSocket test works with: the app, a loopback client and the active session."""

    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        lan: TestClient,
        vault: Vault,
        clock: MsClock,
        sinks: list[RecordingSink],
        providers: list[SpeechToTextProvider],
    ) -> None:
        self.app = app
        self.client = client
        # A LAN client (needs a bearer token); `client` is a trusted loopback one.
        self.lan = lan
        self.vault = vault
        self.clock = clock
        self.sinks = sinks
        self.providers = providers
        self.session: dict[str, Any] = {}

    @property
    def session_id(self) -> str:
        return str(self.session["session_id"])

    @property
    def started_at_ms(self) -> int:
        return int(self.session["started_at_ms"])

    @property
    def path(self) -> str:
        return f"/ws/sessions/{self.session_id}"

    def connect(self) -> Any:
        return self.client.websocket_connect(self.path)

    @staticmethod
    def hello(client_time_ms: int = 1_000_000, **overrides: Any) -> dict[str, Any]:
        """A valid `hello` whose clock reading is `client_time_ms`."""
        message: dict[str, Any] = {
            "type": "hello",
            "protocol_version": "1.0",
            "capabilities": {
                "stt": "client",
                "stt_provider": "web-speech",
                "audio_format": {"encoding": "pcm16", "sample_rate_hz": 16000, "channels": 1},
            },
            "client_time_ms": client_time_ms,
        }
        message.update(overrides)
        return message

    @staticmethod
    def receive_close(websocket: Any) -> tuple[int, str]:
        """The (code, reason) the backend closed `websocket` with; fails on any other message."""
        message = websocket.receive()
        assert message["type"] == "websocket.close", message
        return message["code"], message.get("reason", "")

    def events(self) -> list[Event]:
        log = (
            self.vault.path
            / "subjects/fisica/topics/cinematica/sessions"
            / self.session_id
            / "events.jsonl"
        )
        return list(read_jsonl(log, Event))

    def events_of(self, kind: str) -> list[Event]:
        return [event for event in self.events() if event.kind == kind]
