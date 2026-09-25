"""The server-side STT provider's status over the session socket: protocol 1.5 `stt.status`."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from ws_harness import WsHarness

from studentassistant.config import SttSettings
from studentassistant.protocol import AudioFrame, encode_frame, model_for
from studentassistant.server.ws import STT_STATUS
from studentassistant.stt.fakes import FakeProvider

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
HELLO_AT = 1_000_000
CLIENT_T0 = HELLO_AT - 10_000  # the client clock at session start (see `ws`)
FRAME_MS = 100
PCM = b"\0\0" * (16 * FRAME_MS)  # 100 ms of PCM16 16 kHz mono
LOST = "Se ha perdido la conexión; se reintenta en 5 s y el audio de mientras no se transcribe."
GONE = "La transcripción no está disponible; el audio no se transcribe."

pytestmark = pytest.mark.parametrize(
    "stt_settings", [SttSettings(mode="server", provider="fake", language="es")]
)


def conforms(name: str, body: Any) -> Any:
    schema = json.loads((PROTOCOL_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(body)
    model_for(name).model_validate(body)
    return body


def frame(seq: int) -> bytes:
    return encode_frame(AudioFrame(seq=seq, client_time_ms=CLIENT_T0 + seq * FRAME_MS, pcm=PCM))


class Client:
    """A server-mode client that keeps every `stt.status` it is sent."""

    def __init__(self, ws: WsHarness, socket: Any, version: str | None = "1.5") -> None:
        """Says hello in `version`; `None` for a socket that already did."""
        self.ws = ws
        self.socket = socket
        self.statuses: list[dict[str, Any]] = []
        if version is not None:
            socket.send_json(ws.hello(HELLO_AT, protocol_version=version))
            ack = conforms("server.hello.ack", socket.receive_json())
            assert ack["stt_mode"] == "server"

    @property
    def provider(self) -> FakeProvider:
        provider = self.ws.providers[-1]
        assert isinstance(provider, FakeProvider)
        return provider

    def _next(self) -> dict[str, Any]:
        message = self.socket.receive_json()
        if message["type"] == "stt.status":
            self.statuses.append(conforms("server.stt.status", message))
        return message

    def send(self, seq: int) -> None:
        """Send frame `seq` and wait for its ack."""
        self.socket.send_bytes(frame(seq))
        while self._next()["type"] != "ack":
            pass

    def sync(self) -> list[dict[str, Any]]:
        """Every `stt.status` published so far has reached the client: the bus delivers in order,
        so once a notice published now arrives, all of them did."""
        publish = functools.partial(
            self.ws.app.state.bus.publish,
            self.ws.session_id,
            "notice",
            "observer",
            {"pending_count": 0},
            persist=False,
        )
        self.socket.portal.call(publish)
        while self._next()["type"] != "notice":
            pass
        return self.statuses


def test_a_degraded_provider_is_announced_once_per_change_and_recovery_clears_it(
    ws: WsHarness,
) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        client.send(0)
        client.provider.set_status("reconnecting", LOST)
        client.send(1)
        client.send(2)  # still reconnecting: nothing new
        client.provider.set_status("streaming", LOST)  # a new stream opened, no result yet
        client.send(3)
        client.provider.set_status("streaming")
        client.send(4)  # still fine: nothing new
        client.provider.set_status("unavailable", GONE)
        client.send(5)
        statuses = client.sync()

    now = ws.clock.now
    assert statuses == [
        {"type": "stt.status", "state": "reconnecting", "detail": LOST, "server_time_ms": now},
        {"type": "stt.status", "state": "ok", "server_time_ms": now},
        {"type": "stt.status", "state": "unavailable", "detail": GONE, "server_time_ms": now},
    ]
    events = ws.events_of(STT_STATUS)
    assert [(e.origin, e.payload) for e in events] == [
        ("stt", {"state": "reconnecting", "detail": LOST}),
        ("stt", {"state": "ok"}),
        ("stt", {"state": "unavailable", "detail": GONE}),
    ]
    # At the session time of the chunk that showed the change.
    assert [e.t for e in events] == [200, 400, 600]


def test_a_provider_that_starts_streaming_is_never_announced(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        client.send(0)
        client.provider.set_status("streaming")
        client.send(1)
        assert client.sync() == []
    assert ws.events_of(STT_STATUS) == []


def test_an_older_client_is_not_told_but_the_change_is_logged_in_the_session(
    ws: WsHarness,
) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket, "1.4")
        client.send(0)
        client.provider.set_status("unavailable", GONE)
        client.send(1)
        assert client.sync() == []
    assert [e.payload for e in ws.events_of(STT_STATUS)] == [
        {"state": "unavailable", "detail": GONE}
    ]


def test_a_client_joining_a_degraded_session_is_told_after_hello_ack(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket, "1.0")
        client.send(0)
        client.provider.set_status("reconnecting", LOST)
        client.send(1)
        client.sync()
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT, protocol_version="1.5"))
        assert socket.receive_json()["type"] == "hello.ack"
        assert socket.receive_json()["type"] == "ack"  # where to resume the audio
        status = conforms("server.stt.status", socket.receive_json())
        assert status == {
            "type": "stt.status",
            "state": "reconnecting",
            "detail": LOST,
            "server_time_ms": ws.clock.now,
        }
        # Recovery reaches it like any change, and only once.
        rejoined = Client(ws, socket, None)
        rejoined.provider.set_status("idle")
        rejoined.send(2)
        rejoined.send(3)
        assert [s["state"] for s in rejoined.sync()] == ["ok"]


def test_a_detail_past_the_protocol_bound_still_sends_the_state(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        client.send(0)
        client.provider.set_status("unavailable", "x" * 400)
        client.send(1)
        statuses = client.sync()
    assert statuses == [
        {"type": "stt.status", "state": "unavailable", "server_time_ms": ws.clock.now}
    ]
