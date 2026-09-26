"""End to end: client segments -> bus -> `CommandDetector` -> `command` `capture_now` on the socket.

The TestClient is entered (`with ws.client`) so the app's lifespan runs the detector and every
socket and request shares its event loop; `drain()` runs on that loop through the client's portal.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from ws_harness import WsHarness

from studentassistant.config import ServerSettings, SttSettings
from studentassistant.server.app import create_app
from studentassistant.server.ws import COMMAND, TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL
from studentassistant.stt import GrammarError
from studentassistant.vault import Vault

HELLO_AT = 1_000_000
# `ws` sets the backend clock 10 s after the session started: session time = client - CLIENT_T0.
CLIENT_T0 = HELLO_AT - 10_000


def segment(segment_id: str, text: str, *, final: bool) -> dict[str, Any]:
    return {
        "type": "transcript.client.final" if final else "transcript.client.partial",
        "segment_id": segment_id,
        "client_start_ms": CLIENT_T0 + 2_000,
        "client_end_ms": CLIENT_T0 + 3_000,
        "text": text,
        "provider": "web-speech",
        "language": "es-ES",
    }


def utter(ws: WsHarness, partials: list[str], final: str) -> list[dict[str, Any]]:
    """Send one utterance (`seg-1`), wait for the detector, and return what the socket got.

    Once the final is echoed every segment is on the bus, and once the detector has then drained
    every command it will publish for them is in the log: the socket owes exactly one echo per
    segment plus one message per `command` event.
    """
    sent = [segment("seg-1", text, final=False) for text in partials]
    sent.append(segment("seg-1", final, final=True))
    with ws.client, ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        assert socket.receive_json()["type"] == "hello.ack"
        for message in sent:
            socket.send_json(message)
        received: list[dict[str, Any]] = []
        while not any(m["type"] == TRANSCRIPT_FINAL for m in received):
            received.append(socket.receive_json())  # up to the final echo: all on the bus
        ws.client.portal.call(ws.app.state.commands.drain)
        owed = len(sent) + len(ws.events_of(COMMAND)) - len(received)
        received += [socket.receive_json() for _ in range(owed)]
    return received


def test_a_growing_mira_aqui_sends_one_capture_now(ws: WsHarness) -> None:
    received = utter(ws, ["mira", "mira aquí"], "mira aquí")

    assert Counter(m["type"] for m in received) == {
        TRANSCRIPT_PARTIAL: 2,
        TRANSCRIPT_FINAL: 1,
        COMMAND: 1,
    }
    [command] = [m for m in received if m["type"] == COMMAND]
    assert command["command"] == "capture_now"
    [voice] = ws.events_of("voice.command")
    assert voice.payload == {"command": "capture", "segment_id": "seg-1", "text": "mira aquí"}
    [event] = ws.events_of(COMMAND)
    assert event.payload["command_id"] == command["command_id"]


def test_mira_aqui_no_sends_no_command(ws: WsHarness) -> None:
    received = utter(ws, ["mira", "mira aquí"], "mira, aquí no")

    assert Counter(m["type"] for m in received) == {TRANSCRIPT_PARTIAL: 2, TRANSCRIPT_FINAL: 1}
    assert ws.events_of("voice.command") == []
    assert ws.events_of(COMMAND) == []


def test_an_invalid_grammar_fails_building_the_app(
    server: ServerSettings, tmp_path: Path, tmp_vault: Vault
) -> None:
    grammar = tmp_path / "commands.yaml"
    grammar.write_text("capture: [not, a, mapping]\n", encoding="utf-8")
    stt = SttSettings(mode="client", provider="web-speech", commands_path=grammar)

    with pytest.raises(GrammarError, match=str(grammar)):
        create_app(static_dir=tmp_path / "no-web-build", server=server, vault=tmp_vault, stt=stt)
