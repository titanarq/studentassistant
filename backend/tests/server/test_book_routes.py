"""`GET`/`PUT /api/subjects/{s}/topics/{t}/book`: the topic's textbook title (#58)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from studentassistant.config import ServerSettings
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_subject, create_topic, get_book


def test_the_book_route_reads_and_sets_the_title(
    tmp_vault: Vault, devices_path: Path, codes: PairingCodes, tmp_path: Path
) -> None:
    subject = create_subject(tmp_vault, "Biología").slug
    topic = (subject, create_topic(tmp_vault, subject, "La célula").slug)
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path, public_url="http://192.168.1.20:8765"),
        codes=codes,
        vault=tmp_vault,
    )
    client = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))
    changes: list[None] = []
    app.state.sessions._note_change = lambda: changes.append(None)
    route = f"/api/subjects/{topic[0]}/topics/{topic[1]}/book"

    assert client.get(route).json() == {"subject_id": topic[0], "topic_id": topic[1], "title": None}
    put = client.put(route, json={"title": "Biología 2"})
    assert put.status_code == 200, put.text
    assert put.json()["title"] == "Biología 2"
    assert client.get(route).json()["title"] == "Biología 2"
    assert get_book(tmp_vault, *topic) is not None
    assert changes == [None]

    empty = client.put(route, json={"title": "  "})
    assert empty.status_code == 422
    assert empty.json()["detail"] == "El título del libro no puede estar vacío."
    assert client.get(f"/api/subjects/{topic[0]}/topics/nope/book").status_code == 404
    assert client.put("/api/subjects/nope/topics/nope/book", json={"title": "x"}).status_code == 404
