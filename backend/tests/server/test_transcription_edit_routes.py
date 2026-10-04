"""`PUT /api/sources/{vault_id}/transcription`: the student corrects a transcription (#473)."""

from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient
from read_api_fixtures import JPEG_BYTES, ReadVault
from ws_harness import WsHarness

from studentassistant.vault import (
    put_page_transcription,
    put_source,
    read_topic_events,
)


def _url(path: str) -> str:
    return f"/api/sources/{path}/transcription"


def test_the_correction_is_stored_served_and_committed(
    read_vault: ReadVault, reader: TestClient
) -> None:
    # The machine's transcription, written after the vault opened (and committed what it held):
    # still uncommitted when the student corrects it.
    assert reader.get(f"/api/sources/{read_vault.notes_page}/meta").status_code == 200
    put_page_transcription(read_vault.vault, read_vault.notes_page, "v = dx/dt (máquina)\n")

    response = reader.put(_url(read_vault.notes_page), json={"text": "v = Δx/Δt"})

    assert response.status_code == 200
    md = read_vault.notes_page.replace(".jpg", ".md")
    assert response.json() == {
        "source_path": read_vault.notes_page,
        "transcription_path": md,
        "text": "v = Δx/Δt\n",
    }
    assert reader.get(f"/api/sources/{md}").content.decode() == "v = Δx/Δt\n"
    served = reader.get(f"/api/sources/{read_vault.notes_page}/meta").json()
    assert served["meta"]["transcription_edited"]["by"] == "student"
    # The sidecar's copy is the correction too: the viewer never serves the replaced text.
    assert served["transcription"] == "v = Δx/Δt\n"
    log = _git(read_vault, "log", "-2", "--format=%s").splitlines()
    assert log == [
        "Transcripción de sources/notes/page-001.jpg de fisica/cinematica corregida",
        "Cambios pendientes antes de corregir una transcripción",
    ]
    # The machine's transcription, never committed before, is kept in git history.
    assert _git(read_vault, "show", f"HEAD~1:./{md}") == "v = dx/dt (máquina)\n"


def _git(read_vault: ReadVault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=read_vault.vault.path, capture_output=True, text=True, check=True
    ).stdout


def test_refusals(read_vault: ReadVault, reader: TestClient) -> None:
    blank = reader.put(_url(read_vault.notes_page), json={"text": "   "})
    assert blank.status_code == 422
    assert blank.json()["detail"] == "La transcripción no puede quedar vacía."
    secret = reader.put(_url(read_vault.notes_page), json={"text": "sk-ant-api03-" + "a" * 90})
    assert secret.status_code == 422
    assert "clave o un secreto" in secret.json()["detail"]
    for path in (read_vault.web_page, read_vault.notes_page.replace("page-001", "page-009")):
        response = reader.put(_url(path), json={"text": "x"})
        assert response.status_code == 404
        assert response.json()["detail"] == "No existe esa fuente en la bóveda."
    untranscribed = put_source(
        read_vault.vault, read_vault.subject, read_vault.topic, "notes", "f.jpg", JPEG_BYTES, {}
    )
    rel = untranscribed.relative_to(read_vault.vault.path).as_posix()
    conflict = reader.put(_url(rel), json={"text": "x"})
    assert conflict.status_code == 409
    assert "todavía no está transcrita" in conflict.json()["detail"]


def test_a_lan_client_without_a_token_cannot_edit(
    read_vault: ReadVault, lan_reader: TestClient
) -> None:
    response = lan_reader.put(_url(read_vault.notes_page), json={"text": "x"})
    assert response.status_code in (401, 403)


def test_the_live_session_gets_a_transcription_edited_event(ws: WsHarness) -> None:
    # The harness already has the subject and topic, in the student's folder.
    page = put_source(ws.vault, "fisica", "cinematica", "notes", "f.jpg", JPEG_BYTES, {})
    path = page.relative_to(ws.vault.path).as_posix()
    put_page_transcription(ws.vault, path, "texto\n")

    assert ws.client.put(_url(path), json={"text": "texto corregido"}).status_code == 200

    edited = [
        event
        for session_id, event in read_topic_events(ws.vault, "fisica", "cinematica")
        if session_id == ws.session_id and event.kind == "page.transcription_edited"
    ]
    assert len(edited) == 1
    assert edited[0].origin == "user"
    assert edited[0].payload == {
        "source_id": "sources/notes/page-001.jpg",
        "source_path": path,
        "transcription_path": path.replace(".jpg", ".md"),
    }
