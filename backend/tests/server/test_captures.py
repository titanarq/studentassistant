"""`POST /api/sessions/{id}/captures` of protocol v1, through FastAPI's TestClient over `tmp_vault`.

Bodies are built by hand (`multipart`), so a test controls every part's name and `Content-Type`.
Every refusal is checked to store no source and publish no event.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from studentassistant.config import ServerSettings, Settings, VaultSettings
from studentassistant.protocol import model_for
from studentassistant.server import captures as captures_module
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Event, Vault, read_jsonl, sources_directory

LOCAL_BASE_URL = "http://localhost:8765"
LAN_BASE_URL = "http://192.168.1.20:8765"
PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
CAPTURE_ID = "0b6f3c2e-9a41-4d8e-8f7a-2c5d1e3b4a60"
OTHER_CAPTURE_ID = "5d2a7e10-3c4b-4f9a-a1d2-7e6f5c4b3a21"
STILL_SIZE = (320, 240)  # w x h of the synthetic stills


def _encode(image: np.ndarray, extension: str, *params: int) -> bytes:
    ok, data = cv2.imencode(extension, image, list(params))
    assert ok
    return data.tobytes()


def _sharp_still() -> np.ndarray:
    """Sharp, busy detail (the still capture processing must keep)."""
    rng = np.random.default_rng(3)
    return rng.integers(0, 256, (STILL_SIZE[1], STILL_SIZE[0], 3), dtype=np.uint8)


def _soft_still() -> np.ndarray:
    """A shaken frame: the same detail heavily blurred."""
    return cv2.GaussianBlur(_sharp_still(), (31, 31), 0)


# The first still of the default burst is the sharp one (and the larger file), the second a
# blurred PNG, stored as the burst original `page-NNN.burst2.png`.
JPEG = _encode(_sharp_still(), ".jpg", cv2.IMWRITE_JPEG_QUALITY, 95)
PNG = _encode(_soft_still(), ".png", cv2.IMWRITE_PNG_COMPRESSION, 9)
assert len(JPEG) > len(PNG)
PAGE_FILES = ["page-001.burst2.png", "page-001.jpg", "page-001.page.jpg", "page-001.yaml"]


def decoded_size(path: Path) -> tuple[int, int]:
    """The (width, height) of the image file at `path`."""
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image.shape[1], image.shape[0]


Part = tuple[str, str | None, bytes]


def multipart(parts: list[Part], boundary: str = "sa-test-boundary") -> tuple[bytes, str]:
    """A `multipart/form-data` body of `(name, content_type, bytes)` parts, and its header."""
    body = bytearray()
    for name, content_type, data in parts:
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"'.encode()
        if name != "metadata":
            body += f'; filename="{name}"'.encode()
        body += b"\r\n"
        if content_type is not None:
            body += f"Content-Type: {content_type}\r\n".encode()
        body += b"\r\n" + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def metadata(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "capture_id": CAPTURE_ID,
        "trigger": "button",
        "client_time_ms": 1_790_000_000_000,
        "images": [
            {
                "part": "image_0",
                "content_type": "image/jpeg",
                "width_px": 3000,
                "height_px": 4000,
                "client_time_ms": 1_790_000_000_100,
            },
            {
                "part": "image_1",
                "content_type": "image/png",
                "width_px": 3000,
                "height_px": 4000,
                "client_time_ms": 1_790_000_000_300,
            },
        ],
    }
    body.update(overrides)
    return body


def burst(meta: dict[str, Any] | None = None, images: list[Part] | None = None) -> list[Part]:
    """The metadata part followed by the images (default: a JPEG and a PNG)."""
    meta = metadata() if meta is None else meta
    if images is None:
        images = [("image_0", "image/jpeg", JPEG), ("image_1", "image/png", PNG)]
    return [("metadata", "application/json", json.dumps(meta).encode()), *images]


def conforms(name: str, body: Any) -> Any:
    schema = json.loads((PROTOCOL_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(body)
    model_for(name).model_validate(body)
    return body


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(vault: Vault | None = tmp_vault, **limits: Any) -> FastAPI:
        server = ServerSettings(devices_path=devices_path, public_url=LAN_BASE_URL)
        server = server.model_copy(update=limits)
        return create_app(
            static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=vault
        )

    return make


@pytest.fixture
def app(make_app: AppFactory) -> FastAPI:
    return make_app()


def client_of(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


def start_session(client: TestClient) -> str:
    assert client.post("/api/subjects", json={"name": "Física"}).status_code == 201
    assert (
        client.post("/api/subjects/fisica/topics", json={"name": "Cinemática"}).status_code == 201
    )
    started = client.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1_000},
    )
    assert started.status_code == 201
    assert started.json()["received_capture_ids"] == []
    return started.json()["session_id"]


def upload(client: TestClient, session_id: str, parts: list[Part] | None = None) -> Any:
    body, content_type = multipart(burst() if parts is None else parts)
    return client.post(
        f"/api/sessions/{session_id}/captures",
        content=body,
        headers={"Content-Type": content_type},
    )


def notes_dir(vault: Vault) -> Path:
    return sources_directory(vault, "fisica", "cinematica", "notes")


def capture_events(vault: Vault, session_id: str) -> list[Event]:
    path = vault.path / "subjects/fisica/topics/cinematica/sessions" / session_id / "events.jsonl"
    return [e for e in read_jsonl(path, Event) if e.kind == "capture.stored"]


def stored_files(vault: Vault) -> list[Path]:
    directory = notes_dir(vault)
    return sorted(directory.iterdir()) if directory.exists() else []


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return client_of(app)


@pytest.fixture
def session_id(client: TestClient) -> str:
    return start_session(client)


def assert_nothing_stored(vault: Vault, session_id: str) -> None:
    assert stored_files(vault) == []
    assert capture_events(vault, session_id) == []


# -- config ------------------------------------------------------------------------------------


def test_the_capture_limits_have_defaults_and_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings().server
    assert defaults.max_capture_image_bytes == 15 * 1024 * 1024
    assert defaults.max_capture_images == 5
    monkeypatch.setenv("SA_SERVER__MAX_CAPTURE_IMAGE_BYTES", "1024")
    monkeypatch.setenv("SA_SERVER__MAX_CAPTURE_IMAGES", "2")
    overridden = Settings().server
    assert overridden.max_capture_image_bytes == 1024
    assert overridden.max_capture_images == 2


# -- storing -----------------------------------------------------------------------------------


def test_a_burst_stores_its_sharpest_still_as_a_notes_page_and_publishes_the_event(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    notes: list[int] = []
    service = app.state.sessions
    original = service.note_change
    service.note_change = lambda: (notes.append(1), original())[1]
    subscription = app.state.bus.subscribe(kinds={"capture.stored"})
    before = time.time_ns() // 1_000_000

    response = upload(client, session_id)

    assert response.status_code == 201
    body = conforms("rest.sessions.captures.response", response.json())
    assert body["capture_id"] == CAPTURE_ID
    assert body["session_id"] == session_id
    assert body["status"] == "stored"
    assert body["image_count"] == 2
    assert body["received_at_ms"] >= before

    files = stored_files(tmp_vault)
    assert [f.name for f in files] == PAGE_FILES
    burst_original, still, page, sidecar_path = files
    assert burst_original.read_bytes() == PNG  # the blurred still, kept as it came
    assert still.read_bytes().startswith(b"\xff\xd8")
    assert decoded_size(still) == STILL_SIZE
    assert decoded_size(page) == STILL_SIZE  # no sheet in a noise still: uncropped fallback
    sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
    sharpness = sidecar.pop("sharpness")
    assert len(sharpness) == 2 and sharpness[0] > sharpness[1]
    started = app.state.sessions.get_active(session_id).started_at_ms
    session_t_ms = max(0, 1_790_000_000_000 - started)
    assert sidecar == {
        "capture_id": CAPTURE_ID,
        "session": session_id,
        "captured_at": "2026-09-21T14:13:20.100000Z",  # images[0].client_time_ms in UTC
        "trigger": "button",
        "image_count": 2,
        "source_context": "notes",
        "width_px": STILL_SIZE[0],
        "height_px": STILL_SIZE[1],
        "session_t_ms": session_t_ms,
        "transcript_window": {
            "t_start": max(0, session_t_ms - 20_000),
            "t_end": session_t_ms + 10_000,
        },
        "selected_image": 1,
        "page_detected": False,
    }
    assert notes, "the vault write must be noted to GitSync"

    (event,) = capture_events(tmp_vault, session_id)
    assert event.origin == "phone"
    assert event.payload == {
        "capture_id": CAPTURE_ID,
        "trigger": "button",
        "image_count": 2,
        "client_time_ms": 1_790_000_000_000,
        "source_path": "subjects/fisica/topics/cinematica/sources/notes/page-001.jpg",
        "page_path": "subjects/fisica/topics/cinematica/sources/notes/page-001.page.jpg",
        "source_context": "notes",
    }
    assert (tmp_vault.path / event.payload["source_path"]).read_bytes() == still.read_bytes()
    delivered = subscription.get_nowait()
    assert delivered.session_id == session_id
    assert delivered.seq == event.seq
    assert dict(delivered.payload) == event.payload


def test_a_command_capture_carries_its_command_id(
    client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    meta = metadata(trigger="command", command_id="cmd-7")
    response = upload(client, session_id, burst(meta))
    assert response.status_code == 201
    sidecar = yaml.safe_load((notes_dir(tmp_vault) / "page-001.yaml").read_text(encoding="utf-8"))
    assert sidecar["trigger"] == "command"
    assert sidecar["command_id"] == "cmd-7"
    (event,) = capture_events(tmp_vault, session_id)
    assert event.payload["command_id"] == "cmd-7"
    assert event.payload["trigger"] == "command"


def test_a_single_png_still_is_stored_as_a_jpeg_page_without_originals(
    client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    meta = metadata()
    meta["images"] = [dict(meta["images"][1], part="image_0")]
    response = upload(client, session_id, burst(meta, [("image_0", "image/png", PNG)]))
    assert response.status_code == 201
    assert response.json()["image_count"] == 1
    assert [f.name for f in stored_files(tmp_vault)] == [
        "page-001.jpg",
        "page-001.page.jpg",
        "page-001.yaml",
    ]


def test_the_capture_session_time_uses_the_hello_clock_offset(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    started = app.state.sessions.get_active(session_id).started_at_ms
    # The client clock runs 5 s behind the backend's: its hello set the offset to +5000 ms.
    app.state.gateway.state_for(session_id).clock_offset_ms = 5_000
    captured_client_ms = started + 30_000 - 5_000
    response = upload(client, session_id, burst(metadata(client_time_ms=captured_client_ms)))
    assert response.status_code == 201
    sidecar = yaml.safe_load((notes_dir(tmp_vault) / "page-001.yaml").read_text(encoding="utf-8"))
    assert sidecar["session_t_ms"] == 30_000
    assert sidecar["transcript_window"] == {"t_start": 10_000, "t_end": 40_000}


def test_without_a_hello_the_client_time_is_taken_as_backend_time(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    started = app.state.sessions.get_active(session_id).started_at_ms
    response = upload(client, session_id, burst(metadata(client_time_ms=started + 12_000)))
    assert response.status_code == 201
    sidecar = yaml.safe_load((notes_dir(tmp_vault) / "page-001.yaml").read_text(encoding="utf-8"))
    assert sidecar["session_t_ms"] == 12_000
    assert sidecar["transcript_window"] == {"t_start": 0, "t_end": 22_000}


# -- idempotency -------------------------------------------------------------------------------


def test_a_repeated_capture_id_is_a_duplicate_that_stores_nothing(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    assert upload(client, session_id).status_code == 201
    subscription = app.state.bus.subscribe(kinds={"capture.stored"})

    again = upload(client, session_id)

    assert again.status_code == 200
    body = conforms("rest.sessions.captures.response", again.json())
    assert body["status"] == "duplicate"
    assert body["image_count"] == 2
    assert body["capture_id"] == CAPTURE_ID
    assert len(stored_files(tmp_vault)) == len(PAGE_FILES)
    assert len(capture_events(tmp_vault, session_id)) == 1
    assert len(subscription) == 0


def test_a_duplicate_answers_the_stored_image_count(
    client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    assert upload(client, session_id).status_code == 201
    meta = metadata()
    meta["images"] = meta["images"][:1]
    again = upload(client, session_id, burst(meta, [("image_0", "image/jpeg", JPEG)]))
    assert again.status_code == 200
    assert again.json()["image_count"] == 2


def test_another_capture_id_is_stored_as_the_next_page(
    client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    assert upload(client, session_id).status_code == 201
    assert upload(client, session_id, burst(metadata(capture_id=OTHER_CAPTURE_ID))).status_code == (
        201
    )
    names = [f.name for f in stored_files(tmp_vault)]
    assert names == PAGE_FILES + [name.replace("001", "002") for name in PAGE_FILES]
    assert [e.payload["capture_id"] for e in capture_events(tmp_vault, session_id)] == [
        CAPTURE_ID,
        OTHER_CAPTURE_ID,
    ]


def test_two_concurrent_uploads_of_one_capture_store_it_once(
    app: FastAPI, session_id: str, tmp_vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_store_capture = captures_module.store_capture
    entered = threading.Event()

    def slow_store_capture(*args: Any) -> Any:
        entered.set()
        time.sleep(0.3)  # a worker thread: the other request gets every chance to interleave
        return real_store_capture(*args)

    monkeypatch.setattr(captures_module, "store_capture", slow_store_capture)
    with client_of(app) as client, ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: upload(client, session_id), range(2)))

    assert entered.is_set()
    assert sorted(r.status_code for r in results) == [200, 201]
    assert sorted(r.json()["status"] for r in results) == ["duplicate", "stored"]
    assert len(stored_files(tmp_vault)) == len(PAGE_FILES)
    assert len(capture_events(tmp_vault, session_id)) == 1


# -- source context ----------------------------------------------------------------------------


def press(app: FastAPI, session_id: str, button: str, source: str | None = None) -> None:
    """Publish a `button` event as the WebSocket gateway does (#35) on the app's bus."""
    payload: dict[str, Any] = {"button": button, "client_time_ms": 1, "backend_time_ms": 1}
    if source is not None:
        payload["source"] = source
    asyncio.run(app.state.bus.publish(session_id, "button", "phone", payload))


def sidecar_of(vault: Vault, kind: str) -> dict[str, Any]:
    path = sources_directory(vault, "fisica", "cinematica", kind) / "page-001.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("source", ["book", "pdf", "notes"])
def test_a_capture_after_switch_source_is_stored_under_that_source(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault, source: str
) -> None:
    press(app, session_id, "switch_source", source)
    assert upload(client, session_id).status_code == 201

    stored = sources_directory(tmp_vault, "fisica", "cinematica", source) / "page-001.jpg"
    assert decoded_size(stored) == STILL_SIZE
    assert sidecar_of(tmp_vault, source)["source_context"] == source
    (event,) = capture_events(tmp_vault, session_id)
    assert event.payload["source_context"] == source
    assert event.payload["source_path"] == (
        f"subjects/fisica/topics/cinematica/sources/{source}/page-001.jpg"
    )
    if source != "notes":
        assert stored_files(tmp_vault) == []


def test_the_latest_switch_source_wins_and_other_buttons_do_not_count(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    press(app, session_id, "switch_source", "book")
    press(app, session_id, "switch_source", "pdf")
    press(app, session_id, "pause")
    assert upload(client, session_id).status_code == 201
    assert sidecar_of(tmp_vault, "pdf")["source_context"] == "pdf"

    press(app, session_id, "switch_source", "notes")
    assert upload(client, session_id, burst(metadata(capture_id=OTHER_CAPTURE_ID))).status_code == (
        201
    )
    assert sidecar_of(tmp_vault, "notes")["source_context"] == "notes"
    contexts = [e.payload["source_context"] for e in capture_events(tmp_vault, session_id)]
    assert contexts == ["pdf", "notes"]


def test_without_a_switch_source_captures_stay_notes(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    press(app, session_id, "pause")
    assert upload(client, session_id).status_code == 201
    assert sidecar_of(tmp_vault, "notes")["source_context"] == "notes"
    assert not sources_directory(tmp_vault, "fisica", "cinematica", "book").exists()


def test_the_source_context_survives_a_backend_restart(
    app: FastAPI, client: TestClient, session_id: str, make_app: AppFactory, tmp_vault: Vault
) -> None:
    press(app, session_id, "switch_source", "book")

    restarted = client_of(make_app())
    assert restarted.post(f"/api/sessions/{session_id}/resume").status_code == 200
    assert upload(restarted, session_id).status_code == 201
    assert sidecar_of(tmp_vault, "book")["source_context"] == "book"
    (event,) = capture_events(tmp_vault, session_id)
    assert event.payload["source_context"] == "book"


# -- received_capture_ids ----------------------------------------------------------------------


def test_resume_reports_the_captures_already_stored(client: TestClient, session_id: str) -> None:
    assert upload(client, session_id).status_code == 201
    assert upload(client, session_id, burst(metadata(capture_id=OTHER_CAPTURE_ID))).status_code == (
        201
    )
    resumed = client.post(f"/api/sessions/{session_id}/resume")
    assert resumed.status_code == 200
    body = conforms("rest.sessions.resume.response", resumed.json())
    assert body["received_capture_ids"] == [CAPTURE_ID, OTHER_CAPTURE_ID]


def test_the_stored_captures_survive_a_backend_restart(
    client: TestClient, session_id: str, make_app: AppFactory, tmp_vault: Vault
) -> None:
    assert upload(client, session_id).status_code == 201

    restarted = client_of(make_app())
    resumed = restarted.post(f"/api/sessions/{session_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["received_capture_ids"] == [CAPTURE_ID]
    again = upload(restarted, session_id)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert len(capture_events(tmp_vault, session_id)) == 1


# -- refusals ----------------------------------------------------------------------------------


def _without_command() -> list[Part]:
    return burst(metadata(trigger="command"))


def _button_with_command() -> list[Part]:
    return burst(metadata(command_id="cmd-7"))


def _voice_trigger() -> list[Part]:
    return burst(metadata(trigger="voice"))


def _extra_field() -> list[Part]:
    return burst(metadata(source_context="book"))


def _missing_image() -> list[Part]:
    return burst(images=[("image_0", "image/jpeg", JPEG)])


def _extra_image() -> list[Part]:
    return burst(
        images=[
            ("image_0", "image/jpeg", JPEG),
            ("image_1", "image/png", PNG),
            ("image_2", "image/png", PNG),
        ]
    )


def _wrong_content_type() -> list[Part]:
    return burst(images=[("image_0", "image/png", JPEG), ("image_1", "image/png", PNG)])


def _no_content_type() -> list[Part]:
    return burst(images=[("image_0", None, JPEG), ("image_1", "image/png", PNG)])


def _empty_image() -> list[Part]:
    return burst(images=[("image_0", "image/jpeg", b""), ("image_1", "image/png", PNG)])


def _repeated_part() -> list[Part]:
    meta = metadata()
    meta["images"][1]["part"] = "image_0"
    meta["images"][1]["content_type"] = "image/jpeg"
    return burst(meta, [("image_0", "image/jpeg", JPEG)])


REFUSED_BODIES: dict[str, Callable[[], list[Part]]] = {
    "no metadata part": lambda: burst()[1:],
    "metadata is not json": lambda: [("metadata", "application/json", b"{not json"), *burst()[1:]],
    "metadata misses a field": lambda: burst(
        {k: v for k, v in metadata().items() if k != "capture_id"}
    ),
    "capture id is not a uuid": lambda: burst(metadata(capture_id="not-a-uuid")),
    "voice is not a trigger": _voice_trigger,
    "a field the protocol lacks": _extra_field,
    "command without command_id": _without_command,
    "button with command_id": _button_with_command,
    "a named image is absent": _missing_image,
    "an image part not named": _extra_image,
    "content type differs": _wrong_content_type,
    "content type missing": _no_content_type,
    "an empty image": _empty_image,
    "one part named twice": _repeated_part,
    "no image decodes": lambda: burst(
        images=[
            ("image_0", "image/jpeg", b"\xff\xd8 not a jpeg"),
            ("image_1", "image/png", b"png?"),
        ]
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED_BODIES))
def test_an_invalid_burst_is_422_and_stores_nothing(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault, case: str
) -> None:
    subscription = app.state.bus.subscribe(kinds={"capture.stored"})
    response = upload(client, session_id, REFUSED_BODIES[case]())
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert response.json()["detail"]
    assert_nothing_stored(tmp_vault, session_id)
    assert len(subscription) == 0


def test_a_body_that_is_not_multipart_is_422(
    client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    response = client.post(f"/api/sessions/{session_id}/captures", json=metadata())
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert_nothing_stored(tmp_vault, session_id)


def test_a_truncated_body_is_422(client: TestClient, session_id: str, tmp_vault: Vault) -> None:
    body, content_type = multipart(burst())
    response = client.post(
        f"/api/sessions/{session_id}/captures",
        content=body[: len(body) // 2],
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 422
    assert_nothing_stored(tmp_vault, session_id)


def test_an_image_over_the_byte_limit_is_413(make_app: AppFactory, tmp_vault: Vault) -> None:
    client = client_of(make_app(max_capture_image_bytes=len(PNG)))
    session_id = start_session(client)
    response = upload(client, session_id)  # the JPEG is larger than the PNG
    assert response.status_code == 413
    assert "tamaño máximo" in response.json()["detail"]
    assert_nothing_stored(tmp_vault, session_id)


def test_an_image_at_the_byte_limit_is_accepted(make_app: AppFactory) -> None:
    client = client_of(make_app(max_capture_image_bytes=len(JPEG)))
    session_id = start_session(client)
    assert upload(client, session_id).status_code == 201


def test_more_images_than_the_count_limit_is_413(make_app: AppFactory, tmp_vault: Vault) -> None:
    client = client_of(make_app(max_capture_images=1))
    session_id = start_session(client)
    response = upload(client, session_id)
    assert response.status_code == 413
    assert "imágenes" in response.json()["detail"]
    assert_nothing_stored(tmp_vault, session_id)


def test_metadata_declaring_more_images_than_the_limit_is_413(
    make_app: AppFactory, tmp_vault: Vault
) -> None:
    client = client_of(make_app(max_capture_images=1))
    session_id = start_session(client)
    response = upload(client, session_id, burst(images=[("image_0", "image/jpeg", JPEG)]))
    assert response.status_code == 413
    assert_nothing_stored(tmp_vault, session_id)


def test_a_body_larger_than_the_limits_allow_is_413_from_its_length(
    make_app: AppFactory, tmp_vault: Vault
) -> None:
    client = client_of(make_app(max_capture_image_bytes=16, max_capture_images=1))
    session_id = start_session(client)
    big = b"\xff" * (1024 * 1024)
    response = upload(client, session_id, burst(images=[("image_0", "image/jpeg", big)]))
    assert response.status_code == 413
    assert_nothing_stored(tmp_vault, session_id)


def test_an_unknown_session_is_404(client: TestClient, session_id: str, tmp_vault: Vault) -> None:
    response = upload(client, "20000101-000000")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
    assert_nothing_stored(tmp_vault, session_id)


def test_an_ended_session_is_409(client: TestClient, session_id: str, tmp_vault: Vault) -> None:
    ended = client.post(
        f"/api/sessions/{session_id}/end", json={"client_time_ms": 2_000, "reason": "button"}
    )
    assert ended.status_code == 200
    response = upload(client, session_id)
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert_nothing_stored(tmp_vault, session_id)


def test_an_unended_session_that_is_not_resumed_is_409(
    client: TestClient, session_id: str, make_app: AppFactory, tmp_vault: Vault
) -> None:
    restarted = client_of(make_app())
    response = upload(restarted, session_id)
    assert response.status_code == 409
    assert_nothing_stored(tmp_vault, session_id)


def test_a_vault_that_cannot_be_opened_is_503(
    devices_path: Path, codes: PairingCodes, tmp_path: Path
) -> None:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault_settings=VaultSettings(path=tmp_path / "no-vault-here"),
    )
    response = upload(client_of(app), "20000101-000000")
    assert response.status_code == 503
    assert isinstance(response.json()["detail"], str)


def test_the_route_needs_the_bearer_token_off_loopback(
    app: FastAPI, client: TestClient, session_id: str, tmp_vault: Vault
) -> None:
    lan = TestClient(app, base_url=LAN_BASE_URL, client=("192.168.1.30", 5000))
    response = upload(lan, session_id)
    assert response.status_code == 401
    assert_nothing_stored(tmp_vault, session_id)


def test_a_paired_device_uploads_with_its_token(
    app: FastAPI,
    client: TestClient,
    session_id: str,
    pair_device: Callable[[], dict[str, Any]],
) -> None:
    token = pair_device()["token"]
    lan = TestClient(app, base_url=LAN_BASE_URL, client=("192.168.1.30", 5000))
    body, content_type = multipart(burst())
    response = lan.post(
        f"/api/sessions/{session_id}/captures",
        content=body,
        headers={"Content-Type": content_type, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201


def test_a_foreign_host_header_is_refused(
    app: FastAPI,
    session_id: str,
    tmp_vault: Vault,
    client_with_host: Callable[[FastAPI, str], TestClient],
) -> None:
    response = upload(client_with_host(app, "evil.example"), session_id)
    assert response.status_code in (400, 403, 421)
    assert_nothing_stored(tmp_vault, session_id)
