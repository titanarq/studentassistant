"""`DELETE /api/sources/{vault_id}`: the Recursos trash button retires a source (#451)."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient
from read_api_fixtures import JPEG_BYTES, ReadVault
from ws_harness import WsHarness

from studentassistant.server.assistant_requests import SelectionError, check_selection
from studentassistant.vault import (
    put_source,
    read_topic_events,
)

NOT_FOUND = "No existe esa fuente en la bóveda."


def _listed(reader: TestClient, read_vault: ReadVault) -> list[str]:
    response = reader.get(f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}/sources")
    assert response.status_code == 200
    return [item["vault_id"] for item in response.json()["sources"]]


def test_a_removed_source_leaves_the_lists_and_is_still_served(
    read_vault: ReadVault, reader: TestClient
) -> None:
    assert read_vault.notes_page in _listed(reader, read_vault)

    response = reader.delete(f"/api/sources/{read_vault.notes_page}")

    assert response.status_code == 204
    assert response.content == b""
    assert _listed(reader, read_vault) == [read_vault.web_page]
    base = f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}"
    status = reader.get(f"{base}/sources/status").json()["sources"]
    assert [row["source_id"] for row in status] == ["sources/web/001-movimiento-rectilineo.md"]
    assert reader.get(f"{base}/summary").json()["sources"]["notes"] == 0
    # A citation of it keeps resolving: the bytes and the sidecar are still served.
    content = reader.get(f"/api/sources/{read_vault.notes_page}")
    assert content.status_code == 200
    assert content.content == JPEG_BYTES
    meta = reader.get(f"/api/sources/{read_vault.notes_page}/meta").json()
    assert meta["removed"] is True
    assert meta["meta"]["capture_id"] == "cap-1"
    assert reader.get(f"/api/sources/{read_vault.web_page}/meta").json()["removed"] is False


def test_the_removal_is_committed(read_vault: ReadVault, reader: TestClient) -> None:
    assert reader.delete(f"/api/sources/{read_vault.web_page}").status_code == 204
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=read_vault.vault.path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert log == "Fuente sources/web/001-movimiento-rectilineo.md de fisica/cinematica retirada"


def test_a_second_delete_and_an_unknown_source_are_404(
    read_vault: ReadVault, reader: TestClient
) -> None:
    assert reader.delete(f"/api/sources/{read_vault.web_page}").status_code == 204
    for path in (read_vault.web_page, read_vault.notes_page.replace("page-001", "page-099")):
        response = reader.delete(f"/api/sources/{path}")
        assert response.status_code == 404
        assert response.json()["detail"] == NOT_FOUND


@pytest.mark.parametrize(
    "path",
    [
        "vault.yaml",
        "subjects/fisica/topics/cinematica/sources/notes/page-001.yaml",
        "subjects/fisica/topics/cinematica/sources/notes/%2e%2e/%2e%2e/topic.yaml",
        "subjects/fisica/topics/cinematica/sources/../topic.yaml",
    ],
)
def test_a_path_that_is_no_listed_source_is_404_and_changes_nothing(
    read_vault: ReadVault, reader: TestClient, path: str
) -> None:
    response = reader.delete(f"/api/sources/{path}")
    assert response.status_code == 404
    assert _listed(reader, read_vault) == [read_vault.notes_page, read_vault.web_page]


def test_a_lan_client_without_a_token_cannot_remove(
    read_vault: ReadVault, lan_reader: TestClient, reader: TestClient
) -> None:
    response = lan_reader.delete(f"/api/sources/{read_vault.notes_page}")
    assert response.status_code in (401, 403)
    assert read_vault.notes_page in _listed(reader, read_vault)


def test_the_live_session_of_the_topic_gets_a_source_removed_event(ws: WsHarness) -> None:
    # The harness already has the subject and topic, in the student's folder.
    page = put_source(ws.vault, "fisica", "cinematica", "notes", "f.jpg", JPEG_BYTES, {})
    path = page.relative_to(ws.vault.path).as_posix()

    assert ws.client.delete(f"/api/sources/{path}").status_code == 204

    removed = [
        event
        for session_id, event in read_topic_events(ws.vault, "fisica", "cinematica")
        if session_id == ws.session_id and event.kind == "source.removed"
    ]
    assert len(removed) == 1
    assert removed[0].origin == "user"
    assert removed[0].payload == {"source_id": "sources/notes/page-001.jpg", "source_path": path}


def test_a_removed_source_cannot_be_selected_as_a_referent(
    read_vault: ReadVault, reader: TestClient
) -> None:
    source_id = "sources/notes/page-001.jpg"
    vault, s, t = read_vault.vault, read_vault.subject, read_vault.topic
    assert check_selection(vault, s, t, [source_id], 20) == [source_id]
    assert reader.delete(f"/api/sources/{read_vault.notes_page}").status_code == 204
    with pytest.raises(SelectionError):
        check_selection(vault, s, t, [source_id], 20)
