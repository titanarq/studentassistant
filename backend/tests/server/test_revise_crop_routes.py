"""A crop asked in the workspace chat (#493), end to end through `POST .../notes/chat`: the app's
transport serves both the editor's `crop_image` call and Sonnet's `crop_region` box."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.editor.crop import (
    CHECK_TOOL_NAME,
    CROP_FAILED_PREFIX,
    CROP_TOOL,
    TOOL_NAME,
    UNSUPPORTED_IMAGE_MESSAGE,
)
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import GitSync, Vault, put_source, read_notes, write_notes

LOCAL_BASE_URL = "http://localhost:8765"
BOX = {"x0": 0.25, "y0": 0.25, "x1": 0.75, "y1": 0.75}
REGION = "el diagrama de la página"
CONFIRMATION = "He añadido el recorte del diagrama de la página 2 del libro."
BOOK_PAGE = "sources/book/page-002.jpg"


@pytest.fixture
def topic(user_vault: Vault) -> ReviseTopic:
    return make_revise_topic(user_vault)


COMPLETE = {"complete": True, "left": "ok", "top": "ok", "right": "ok", "bottom": "ok"}


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def client(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault, fake: FakeClaude
) -> Iterator[TestClient]:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _diagram() -> bytes:
    """A sharp line drawing on white paper, as a JPEG."""
    image = np.full((600, 800, 3), 255, np.uint8)
    for i in range(12):
        cv2.line(image, (50 + i * 50, 80), (90 + i * 50, 500), (0, 0, 0), 2)
    cv2.circle(image, (400, 300), 120, (30, 30, 30), 3)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def _cite_book_page(topic: ReviseTopic) -> None:
    """A second textbook page with a real diagram, cited by the notes (the REST chat carries no
    Recursos selection, so the crop's source must be cited)."""
    put_source(topic.vault, topic.subject, topic.topic, "book", "foto.jpg", _diagram(), {})
    notes = topic.notes.replace(
        "Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^t2][^b2]"
    ).replace("[^p1]: [Apuntes", f"[^b2]: [Libro, página 2](../{BOOK_PAGE})\n[^p1]: [Apuntes")
    write_notes(topic.vault, topic.subject, topic.topic, notes)
    GitSync(topic.vault).checkpoint("fixture: cite page 2")


def _crop_call(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "region": REGION,
        "op": "insert_after",
        "section": "definicion",
        "block": 2,
        "summary": "Añado el recorte del diagrama",
    }


def _chat(client: TestClient, topic: ReviseTopic) -> dict[str, Any]:
    """One chat turn; the `result` event of its Server-Sent Events."""
    response = client.post(
        f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/chat",
        json={"message": "pon solo el diagrama de esta página"},
    )
    assert response.status_code == 200, response.text
    chunks = [chunk for chunk in response.text.split("\n\n") if chunk.strip()]
    fields = dict(line.split(": ", 1) for line in chunks[-1].splitlines())
    assert fields["event"] == "result", response.text
    return json.loads(fields["data"])


def _images(client: TestClient, topic: ReviseTopic) -> list[str]:
    body = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/sources").json()
    return [item["vault_id"] for item in body["sources"] if item["kind"] == "images"]


def test_a_crop_turn_stores_the_image_and_the_history_carries_it(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    _cite_book_page(topic)
    # The default crop (#520): a coarse box, a refined one on the zoomed page, then the check.
    fake.reply_tool(CROP_TOOL, _crop_call(BOOK_PAGE), text=CONFIRMATION).reply_tool(TOOL_NAME, BOX)
    fake.reply_tool(TOOL_NAME, {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.9})
    fake.reply_tool(CHECK_TOOL_NAME, COMPLETE)

    result = _chat(client, topic)

    assert result["applied"] and result["reply"] == CONFIRMATION
    crop = result["crop"]
    assert crop["error"] is None and crop["source_id"] == "sources/images/img-001.jpg"
    # Sonnet located and checked the region through the app's own transport.
    assert [request.role for request in fake.requests] == ["editor", *["observer"] * 3]
    assert [request.tools[0]["name"] for request in fake.requests[1:]] == [
        TOOL_NAME,
        TOOL_NAME,
        CHECK_TOOL_NAME,
    ]
    [image] = _images(client, topic)
    assert image == crop["path"] and image.endswith(crop["source_id"])
    meta = client.get(f"/api/sources/{image}/meta").json()["meta"]
    assert meta["origin"] == "cropped" and meta["cropped_from"].endswith(BOOK_PAGE)

    history = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/chat")
    last = history.json()["turns"][-1]
    assert last["applied"] is True and last["crop"]["source_id"] == crop["source_id"]


def test_a_failed_crop_changes_nothing_and_says_why(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    # A cited notes page whose bytes are no decodable image: refused before any Sonnet call.
    fake.reply_tool(CROP_TOOL, _crop_call("sources/notes/page-001.jpg"), text=CONFIRMATION)

    result = _chat(client, topic)

    assert result["applied"] is False
    assert result["crop"]["error"] == UNSUPPORTED_IMAGE_MESSAGE
    assert result["reply"].startswith("No he podido añadir el recorte")
    assert result["reply"] == f"{CROP_FAILED_PREFIX}{UNSUPPORTED_IMAGE_MESSAGE}"
    assert [request.role for request in fake.requests] == ["editor"]
    assert _images(client, topic) == []
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes
