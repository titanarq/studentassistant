"""The session WebSocket's authentication, active user, session check and `hello` / `hello.ack`
handshake."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from starlette.websockets import WebSocketDisconnect
from ws_harness import WsHarness

from studentassistant.config import SttSettings
from studentassistant.protocol import (
    PROTOCOL_VERSION,
    USER_COOKIE,
    USER_HEADER,
    IncompatibleProtocolVersionError,
    model_for,
    parse_version,
)
from studentassistant.server.sessions import SESSION_STARTED
from studentassistant.server.ws import (
    CLOSE_PROTOCOL_VIOLATION,
    CLOSE_UNKNOWN_SESSION,
    USER_REQUIRED_CLOSE_REASON,
)
from studentassistant.vault import Vault, create_user

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
SERVER_STT = SttSettings(mode="server", provider="fake", language="es")


def conforms(name: str, body: Any) -> Any:
    schema = json.loads((PROTOCOL_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(body)
    model_for(name).model_validate(body)
    return body


def only_lifecycle_published(ws: WsHarness) -> bool:
    return [event.kind for event in ws.events()] == [SESSION_STARTED]


def test_hello_ack_in_client_mode(ws: WsHarness) -> None:
    with ws.connect() as socket:
        # The client's preference (server) does not choose the mode: the config does.
        socket.send_json(ws.hello(1_000_000, capabilities={"stt": "server", "stt_provider": "x"}))
        ack = conforms("server.hello.ack", socket.receive_json())
    assert ack == {
        "type": "hello.ack",
        "protocol_version": "1.0",
        "stt_mode": "client",
        "clock_offset_ms": ws.clock.now - 1_000_000,
        "server_time_ms": ws.clock.now,
    }


@pytest.mark.parametrize("stt_settings", [SERVER_STT])
def test_hello_ack_in_server_mode_names_the_audio_format(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(2_000_000))
        ack = conforms("server.hello.ack", socket.receive_json())
    assert ack["stt_mode"] == "server"
    assert ack["audio_format"] == {"encoding": "pcm16", "sample_rate_hz": 16000, "channels": 1}
    assert ack["clock_offset_ms"] == ws.clock.now - 2_000_000
    assert [p.name for p in ws.providers] == ["fake"]


def test_a_newer_minor_is_negotiated_down(ws: WsHarness) -> None:
    major, minor = parse_version(PROTOCOL_VERSION)
    with ws.connect() as socket:
        socket.send_json(ws.hello(protocol_version=f"{major}.{minor + 1}"))
        assert socket.receive_json()["protocol_version"] == PROTOCOL_VERSION


def test_the_clock_offset_may_be_negative(ws: WsHarness) -> None:
    ahead = ws.clock.now + 3_000
    with ws.connect() as socket:
        socket.send_json(ws.hello(ahead))
        assert socket.receive_json()["clock_offset_ms"] == -3_000


def test_an_incompatible_major_gets_no_hello_ack(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(protocol_version="2.0"))
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert reason == str(IncompatibleProtocolVersionError("2.0"))
    assert only_lifecycle_published(ws)


@pytest.mark.parametrize(
    "first",
    [
        {"type": "marker", "client_time_ms": 1},
        {"type": "nope"},
        {"protocol_version": "1.0"},
        {"type": "hello", "protocol_version": "1.0"},
    ],
    ids=["not-hello", "unknown-type", "missing-type", "invalid-hello"],
)
def test_the_first_message_must_be_a_valid_hello(ws: WsHarness, first: dict[str, Any]) -> None:
    with ws.connect() as socket:
        socket.send_json(first)
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert reason
    assert only_lifecycle_published(ws)


def test_text_that_is_not_json_is_refused(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_text("hola")
        assert ws.receive_close(socket)[0] == CLOSE_PROTOCOL_VIOLATION


def test_a_second_hello_is_refused(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello())
        socket.receive_json()
        socket.send_json(ws.hello())
        assert ws.receive_close(socket)[0] == CLOSE_PROTOCOL_VIOLATION


def test_an_unknown_session_is_closed_with_a_reason(ws: WsHarness) -> None:
    with ws.client.websocket_connect("/ws/sessions/20000101-000000") as socket:
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_UNKNOWN_SESSION
    assert "20000101-000000" in reason
    assert only_lifecycle_published(ws)


def test_an_ended_session_is_closed_with_a_reason(ws: WsHarness) -> None:
    ended = ws.client.post(
        f"/api/sessions/{ws.session_id}/end", json={"client_time_ms": 5, "reason": "button"}
    )
    assert ended.status_code == 200
    with ws.connect() as socket:
        code, _ = ws.receive_close(socket)
    assert code == CLOSE_UNKNOWN_SESSION
    assert [event.kind for event in ws.events()][-1] == "session.ended"


def test_a_lan_client_without_a_token_is_refused_before_accept(ws: WsHarness) -> None:
    with pytest.raises(WebSocketDisconnect) as refused, ws.lan.websocket_connect(ws.path):
        pass
    assert refused.value.code == 1008


def test_a_paired_device_connects_with_its_token(ws: WsHarness) -> None:
    code = ws.client.post("/api/pair/codes").json()["code"]
    paired = ws.lan.post(
        "/api/pair",
        json={
            "pairing_code": code,
            "device_name": "Móvil",
            "client_kind": "android",
            "protocol_version": "1.0",
        },
    ).json()
    with ws.lan.websocket_connect(f"{ws.path}?token={paired['token']}") as socket:
        socket.send_json(ws.hello())
        assert socket.receive_json()["type"] == "hello.ack"


# -- whose the session is (#550) ---------------------------------------------------------------

NEVER_WRITTEN = "20000101-000000"
"""A session id no topic of anybody's lists."""


@pytest.fixture
def two_users(ws: WsHarness, tmp_vault: Vault) -> tuple[str, str]:
    """The ids of the vault's two students: the one `ws`'s session is theirs, and a new one.

    The second is added to the vault under the app, which holds the same repository: a handshake
    resolving the user reads `users/` as it is then, so the new student exists for the socket that
    names them.
    """
    owner = ws.vault.user_id
    assert owner is not None, "the harness's session is a student's"
    return owner, create_user(tmp_vault, "Lucía Martín").id


def _close_of(ws: WsHarness, path: str, **kwargs: Any) -> tuple[int, str]:
    """The (code, reason) a handshake to `path` ends with, before any message is exchanged."""
    with ws.client.websocket_connect(path, **kwargs) as socket:
        return ws.receive_close(socket)


def test_a_handshake_naming_the_session_owner_is_served(ws: WsHarness, two_users: tuple[str, str]):
    owner, _ = two_users
    with ws.client.websocket_connect(ws.path, headers={USER_HEADER: owner}) as socket:
        socket.send_json(ws.hello())
        assert socket.receive_json()["type"] == "hello.ack"


def test_the_cookie_names_the_user_of_a_handshake(ws: WsHarness, two_users: tuple[str, str]):
    owner, _ = two_users
    with ws.client.websocket_connect(ws.path, headers={"cookie": f"{USER_COOKIE}={owner}"}) as s:
        s.send_json(ws.hello())
        assert s.receive_json()["type"] == "hello.ack"


def test_a_session_of_another_user_is_unknown_not_theirs(
    ws: WsHarness, two_users: tuple[str, str]
) -> None:
    _, other = two_users

    code, reason = _close_of(ws, ws.path, headers={USER_HEADER: other})
    nobody, nobody_reason = _close_of(
        ws, f"/ws/sessions/{NEVER_WRITTEN}", headers={USER_HEADER: other}
    )

    assert code == CLOSE_UNKNOWN_SESSION
    # The answer for a session that is somebody else's is the answer for an id nobody ever wrote,
    # with the id the caller itself supplied in the path swapped in: it cannot be used to probe
    # for the sessions of the student sharing the backend.
    assert (code, reason.replace(ws.session_id, "<id>")) == (
        nobody,
        nobody_reason.replace(NEVER_WRITTEN, "<id>"),
    )
    assert only_lifecycle_published(ws)


def test_a_handshake_naming_no_user_on_a_shared_vault_is_refused(
    ws: WsHarness, two_users: tuple[str, str]
) -> None:
    code, reason = _close_of(ws, ws.path)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert reason == USER_REQUIRED_CLOSE_REASON
    assert only_lifecycle_published(ws)


def test_a_handshake_naming_a_user_the_vault_does_not_have_is_refused(
    ws: WsHarness, two_users: tuple[str, str]
) -> None:
    code, reason = _close_of(ws, ws.path, headers={USER_HEADER: "nadie"})
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert reason.startswith("user_not_found:")
    assert "nadie" in reason
    assert only_lifecycle_published(ws)
