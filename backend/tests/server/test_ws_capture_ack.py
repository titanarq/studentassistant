"""Capture upload x session WebSocket: the connected client gets an `ack` for every stored capture,
and a `switch_source` button sent over the socket decides where the next capture is stored.

The upload and the socket share one TestClient portal (`with ws.client`), so both run in the
event loop the bus delivers in, as they do under uvicorn.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Iterator
from typing import Any

import anyio
import cv2
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from ws_harness import WsHarness

from studentassistant.protocol import parse_server_event
from studentassistant.vault import sources_directory

CAPTURE_ID = "0b6f3c2e-9a41-4d8e-8f7a-2c5d1e3b4a60"
OTHER_CAPTURE_ID = "5d2a7e10-3c4b-4f9a-a1d2-7e6f5c4b3a21"
# A real (tiny) JPEG: capture processing decodes every still it stores.
JPEG = cv2.imencode(".jpg", np.full((120, 160, 3), 200, dtype=np.uint8))[1].tobytes()
BOUNDARY = "sa-test-boundary"
RECEIVE_TIMEOUT_S = 5.0
"""How long a test waits for a server message before it fails (instead of hanging CI)."""


def upload(client: TestClient, session_id: str, capture_id: str = CAPTURE_ID) -> Any:
    """Upload a one-image burst `capture_id` to the session."""
    meta = {
        "capture_id": capture_id,
        "trigger": "button",
        "client_time_ms": 1_790_000_000_000,
        "images": [
            {
                "part": "image_0",
                "content_type": "image/jpeg",
                "width_px": 3000,
                "height_px": 4000,
                "client_time_ms": 1_790_000_000_100,
            }
        ],
    }
    body = (
        (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="image_0"; filename="image_0"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + JPEG
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    return client.post(
        f"/api/sessions/{session_id}/captures",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
    )


@pytest.fixture
def client(ws: WsHarness) -> Iterator[TestClient]:
    with ws.client as client:
        yield client


def connect(ws: WsHarness, client: TestClient) -> Any:
    return client.websocket_connect(ws.path)


def receive_json(socket: Any, timeout: float = RECEIVE_TIMEOUT_S) -> Any:
    """The next JSON message of `socket`, failing the test if none arrives within `timeout`.

    Starlette's `WebSocketTestSession.receive_json` waits forever; this reads the same in-portal
    stream it reads (`_send_rx`) under `anyio.fail_after`, so a missing message fails fast and a
    cancelled read loses nothing.
    """

    async def receive() -> Any:
        with anyio.fail_after(timeout):
            return await socket._send_rx.receive()

    try:
        message = socket.portal.call(receive)
    except TimeoutError:
        pytest.fail(f"no server message within {timeout} s")
    socket._raise_on_close(message)
    return json.loads(message["text"])


def handshake(ws: WsHarness, socket: Any) -> None:
    socket.send_json(ws.hello())
    assert receive_json(socket)["type"] == "hello.ack"


def publish_notice(ws: WsHarness, socket: Any, pending_count: int) -> None:
    """A notice sent after the uploads: the next message the socket reads if nothing else came."""
    publish = functools.partial(
        ws.app.state.bus.publish,
        ws.session_id,
        "notice",
        "observer",
        {"pending_count": pending_count},
        persist=False,
    )
    socket.portal.call(publish)


def wait_for_buttons(ws: WsHarness, count: int) -> None:
    """Wait until the gateway has persisted `count` button events (it answers them nothing)."""
    deadline = time.monotonic() + 5
    while len(ws.events_of("button")) < count:
        assert time.monotonic() < deadline, "the button was never persisted"
        time.sleep(0.01)


def test_a_stored_capture_is_acknowledged_once_on_the_socket(
    ws: WsHarness, client: TestClient
) -> None:
    with connect(ws, client) as socket:
        handshake(ws, socket)
        assert upload(client, ws.session_id).status_code == 201
        ack = receive_json(socket)
        publish_notice(ws, socket, 1)
        after = receive_json(socket)

    parse_server_event(ack)
    assert ack == {"type": "ack", "capture_ids": [CAPTURE_ID], "server_time_ms": ws.clock.now}
    assert after["type"] == "notice", "one capture, one ack"


def test_a_duplicate_upload_is_not_acknowledged_again(ws: WsHarness, client: TestClient) -> None:
    with connect(ws, client) as socket:
        handshake(ws, socket)
        assert upload(client, ws.session_id).status_code == 201
        assert receive_json(socket)["capture_ids"] == [CAPTURE_ID]
        duplicate = upload(client, ws.session_id)
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "duplicate"
        publish_notice(ws, socket, 2)
        assert receive_json(socket) == {
            "type": "notice",
            "pending_count": 2,
            "server_time_ms": ws.clock.now,
        }


def test_each_capture_gets_its_own_ack(ws: WsHarness, client: TestClient) -> None:
    with connect(ws, client) as socket:
        handshake(ws, socket)
        assert upload(client, ws.session_id).status_code == 201
        assert upload(client, ws.session_id, OTHER_CAPTURE_ID).status_code == 201
        acks = [receive_json(socket), receive_json(socket)]
    assert [ack["capture_ids"] for ack in acks] == [[CAPTURE_ID], [OTHER_CAPTURE_ID]]


def test_a_reconnecting_client_is_not_replayed_past_acks(ws: WsHarness, client: TestClient) -> None:
    assert upload(client, ws.session_id).status_code == 201
    resumed = client.post(f"/api/sessions/{ws.session_id}/resume")
    assert resumed.json()["received_capture_ids"] == [CAPTURE_ID]
    with connect(ws, client) as socket:
        handshake(ws, socket)
        publish_notice(ws, socket, 0)
        assert receive_json(socket)["type"] == "notice"


def test_switch_source_over_the_socket_stores_the_next_capture_under_that_source(
    ws: WsHarness, client: TestClient
) -> None:
    with connect(ws, client) as socket:
        handshake(ws, socket)
        socket.send_json(
            {
                "type": "button",
                "button": "switch_source",
                "source": "book",
                "client_time_ms": 1_000_500,
            }
        )
        wait_for_buttons(ws, 1)
        assert upload(client, ws.session_id).status_code == 201
        assert receive_json(socket)["capture_ids"] == [CAPTURE_ID]

    book = sources_directory(ws.vault, "fisica", "cinematica", "book")
    assert (book / "page-001.jpg").is_file()
    assert (book / "page-001.page.jpg").is_file()
    sidecar = yaml.safe_load((book / "page-001.yaml").read_text(encoding="utf-8"))
    assert sidecar["source_context"] == "book"
    assert not sources_directory(ws.vault, "fisica", "cinematica", "notes").exists()
    (event,) = ws.events_of("capture.stored")
    assert event.payload["source_context"] == "book"


def test_without_a_switch_the_capture_stays_notes(ws: WsHarness, client: TestClient) -> None:
    with connect(ws, client) as socket:
        handshake(ws, socket)
        assert upload(client, ws.session_id).status_code == 201
        assert receive_json(socket)["type"] == "ack"
    notes = sources_directory(ws.vault, "fisica", "cinematica", "notes")
    assert (notes / "page-001.jpg").is_file()
    (event,) = ws.events_of("capture.stored")
    assert event.payload["source_context"] == "notes"
