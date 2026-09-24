"""The study desk's topic summary and the notes routes, plus the read routes in the OpenAPI."""

from __future__ import annotations

from fastapi.testclient import TestClient
from read_api_fixtures import ReadVault

from studentassistant.vault import GitSync, notes_path
from studentassistant.vault.files import write_text_atomic

NOTES = "# Cinemática\n\nLa velocidad es la derivada de la posición.[^1]\n"


def summary_of(reader: TestClient, rv: ReadVault, topic: str | None = None) -> dict:
    response = reader.get(f"/api/subjects/{rv.subject}/topics/{topic or rv.topic}/summary")
    assert response.status_code == 200
    return response.json()


def write_notes(rv: ReadVault, text: str = NOTES) -> None:
    # No vault writer of the notes exists yet (the editor's task): the vault's atomic writer.
    path = notes_path(rv.vault, rv.subject, rv.topic)
    path.parent.mkdir(exist_ok=True)
    write_text_atomic(path, text)


def test_the_summary_counts_sources_sessions_and_open_pending(
    read_vault: ReadVault, reader: TestClient
) -> None:
    body = summary_of(reader, read_vault)

    assert (body["subject_id"], body["topic_id"]) == (read_vault.subject, read_vault.topic)
    assert body["sources"] == {"notes": 1, "book": 0, "pdf": 0, "web": 1}
    assert body["sessions"] == 2
    # The ended session is 30 minutes; the unended one counts up to now (a moment ago).
    assert 30.0 <= body["session_minutes"] < 31.0
    assert body["open_pending"] == 1
    assert body["notes_version"] is None
    assert body["generated"] == []


def test_an_empty_topic_summarises_as_zeros(read_vault: ReadVault, reader: TestClient) -> None:
    body = summary_of(reader, read_vault, read_vault.empty_topic)

    assert body["sources"] == {"notes": 0, "book": 0, "pdf": 0, "web": 0}
    assert (body["sessions"], body["session_minutes"], body["open_pending"]) == (0, 0.0, 0)
    assert body["notes_version"] is None
    assert body["generated"] == []


def test_the_summary_reports_the_highest_notes_tag(
    read_vault: ReadVault, reader: TestClient
) -> None:
    write_notes(read_vault)
    sync = GitSync(read_vault.vault)
    sync.create_notes_tag(read_vault.topic)
    write_notes(read_vault, NOTES + "\nMás.\n")
    sync.create_notes_tag(read_vault.topic)

    assert summary_of(reader, read_vault)["notes_version"] == 2
    assert summary_of(reader, read_vault, read_vault.empty_topic)["notes_version"] is None


def test_the_summary_of_an_unknown_topic_is_404(read_vault: ReadVault, reader: TestClient) -> None:
    for path in (
        "/api/subjects/fisica/topics/optica/summary",
        "/api/subjects/quimica/topics/x/summary",
    ):
        response = reader.get(path)
        assert response.status_code == 404
        assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_notes_not_written_yet_are_404_in_spanish(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}/notes")
    assert response.status_code == 404
    assert response.json()["detail"] == "Todavía no hay apuntes de este tema."


def test_notes_return_the_text_and_version(read_vault: ReadVault, reader: TestClient) -> None:
    write_notes(read_vault)
    path = f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}/notes"

    untagged = reader.get(path).json()
    assert untagged == {
        "subject_id": read_vault.subject,
        "topic_id": read_vault.topic,
        "text": NOTES,
        "version": None,
    }

    GitSync(read_vault.vault).create_notes_tag(read_vault.topic)
    assert reader.get(path).json()["version"] == 1


def test_notes_of_an_unknown_topic_is_404(read_vault: ReadVault, reader: TestClient) -> None:
    response = reader.get(f"/api/subjects/{read_vault.subject}/topics/optica/notes")
    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_the_openapi_schema_lists_the_read_routes_and_bodies(reader: TestClient) -> None:
    schema = reader.get("/openapi.json").json()

    paths = schema["paths"]
    for path in (
        "/api/subjects/{subject_id}/topics/{topic_id}/summary",
        "/api/subjects/{subject_id}/topics/{topic_id}/notes",
        "/api/subjects/{subject_id}/topics/{topic_id}/sessions",
        "/api/sessions/{session_id}/transcript",
        "/api/sources/{vault_id}",
        "/api/sources/{vault_id}/meta",
    ):
        assert "get" in paths[path], path
    for model in ("TopicSummary", "TopicNotes", "TopicSessions", "TranscriptSpan", "SourceMeta"):
        assert model in schema["components"]["schemas"], model
