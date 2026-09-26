"""The student's document edits: `PUT .../notes` and `POST .../sources/images`, plus the
`revision` of `GET .../notes`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings, SourcesSettings
from studentassistant.editor.notes_format import notes_revision
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.notes_routes import TURN_HOLDER, NotesGenerator
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, read_notes, sources_directory

LOCAL_BASE_URL = "http://localhost:8765"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(32)


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def app(devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_transport=FakeClaude(),
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
        sources=SourcesSettings(max_pasted_image_bytes=1024),
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _notes_url(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes"


def _images_url(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/sources/images"


def _stored(topic: ReviseTopic) -> str:
    return read_notes(topic.vault, topic.subject, topic.topic) or ""


def test_get_notes_carries_the_revision(client: TestClient, topic: ReviseTopic) -> None:
    body = client.get(_notes_url(topic)).json()
    assert body["revision"] == notes_revision(topic.notes)


def test_a_save_answers_the_new_revision(client: TestClient, topic: ReviseTopic) -> None:
    revision = client.get(_notes_url(topic)).json()["revision"]
    edited = topic.notes.replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^t2]\n\nMío.")

    response = client.put(_notes_url(topic), json={"text": edited, "base_revision": revision})

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["normalised"] and "Mío.[^est]" in result["notes"]
    assert result["revision"] == notes_revision(_stored(topic)) and result["commit"]
    assert result["changed_sections"] == ["definicion"]
    assert client.get(_notes_url(topic)).json()["revision"] == result["revision"]


def test_a_stale_base_is_409_notes_changed_with_the_current_notes(
    client: TestClient, topic: ReviseTopic
) -> None:
    response = client.put(_notes_url(topic), json={"text": "# Otra\n", "base_revision": "0" * 64})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "notes_changed"
    assert body["text"] == topic.notes and body["revision"] == notes_revision(topic.notes)
    assert _stored(topic) == topic.notes


def test_a_rewrite_holding_the_notes_is_409_notes_busy_but_a_chat_turn_is_not(
    app: FastAPI, client: TestClient, topic: ReviseTopic
) -> None:
    generator: NotesGenerator = app.state.notes
    revision = notes_revision(topic.notes)
    edited = topic.notes.replace("Se escribe", "Se escribe también")

    assert generator.claim(topic.subject, topic.topic, "generation")
    busy = client.put(_notes_url(topic), json={"text": edited, "base_revision": revision})
    generator.release(topic.subject, topic.topic)
    assert busy.status_code == 409 and busy.json()["code"] == "notes_busy"
    assert _stored(topic) == topic.notes

    assert generator.claim(topic.subject, topic.topic, TURN_HOLDER)
    during_turn = client.put(_notes_url(topic), json={"text": edited, "base_revision": revision})
    generator.release(topic.subject, topic.topic)
    assert during_turn.status_code == 200, during_turn.text
    assert "Se escribe también" in _stored(topic)


def test_notes_that_break_the_format_are_422_with_the_errors(
    client: TestClient, topic: ReviseTopic
) -> None:
    broken = topic.notes.replace("Se escribe $f'(x)$.[^t2]", "Se escribe.[^nada]")

    response = client.put(
        _notes_url(topic), json={"text": broken, "base_revision": notes_revision(topic.notes)}
    )

    assert response.status_code == 422
    body = response.json()
    assert any("[^nada]" in error for error in body["errors"]) and "[^nada]" in body["detail"]


def test_an_unknown_topic_is_404(client: TestClient, topic: ReviseTopic) -> None:
    response = client.put(
        f"/api/subjects/{topic.subject}/topics/no-existe/notes",
        json={"text": "# X\n", "base_revision": None},
    )
    assert response.status_code == 404


def test_a_pasted_image_is_stored_and_answers_its_markdown(
    client: TestClient, topic: ReviseTopic
) -> None:
    response = client.post(_images_url(topic), files={"file": ("pegada.bin", PNG, "text/plain")})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_id"] == "sources/images/img-001.png"
    assert body["markdown"] == "![Imagen pegada 1](../sources/images/img-001.png)"
    directory = sources_directory(topic.vault, topic.subject, topic.topic, "images")
    assert (directory / "img-001.png").read_bytes() == PNG
    assert body["path"].endswith("/sources/images/img-001.png")

    second = client.post(_images_url(topic), files={"file": ("x.webp", WEBP, "image/webp")})
    assert second.json()["source_id"] == "sources/images/img-002.webp"

    # The image can go straight into the notes.
    edited = topic.notes.replace(
        "## 2. Próximo día {#proximo-dia}\n\n",
        f"## 2. Próximo día {{#proximo-dia}}\n\n{body['markdown']}\n\n",
    )
    saved = client.put(
        _notes_url(topic), json={"text": edited, "base_revision": notes_revision(topic.notes)}
    )
    assert saved.status_code == 200, saved.text
    assert "[^img001]: [Imagen pegada 1](../sources/images/img-001.png)" in saved.json()["notes"]


def test_images_that_are_too_large_or_not_images_are_refused(
    client: TestClient, topic: ReviseTopic
) -> None:
    large = client.post(_images_url(topic), files={"file": ("a.png", PNG + bytes(2048))})
    assert large.status_code == 413
    gif = client.post(_images_url(topic), files={"file": ("a.gif", b"GIF89a" + bytes(10))})
    assert gif.status_code == 422
    empty = client.post(_images_url(topic), files={"file": ("a.png", b"")})
    assert empty.status_code == 422
    other = client.post(_images_url(topic), files={"otra": ("a.png", PNG)})
    assert other.status_code == 422
    assert not sources_directory(topic.vault, topic.subject, topic.topic, "images").exists()
