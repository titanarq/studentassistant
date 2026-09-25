"""The web read API (`server/read_routes.py`): the fixture vault, session list, transcript spans."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from read_api_fixtures import ReadVault

from studentassistant.config import ServerSettings, VaultSettings
from studentassistant.server.app import create_app
from studentassistant.server.auth import EXEMPT_ROUTES
from studentassistant.server.read_routes import parse_span
from studentassistant.vault import list_sessions, list_sources, read_session_transcript


def test_fixture_vault(read_vault: ReadVault, reader: TestClient) -> None:
    vault, subject, topic = read_vault.vault, read_vault.subject, read_vault.topic
    sessions = list_sessions(vault, subject, topic)
    assert [meta.id for meta in sessions] == [read_vault.ended_session, read_vault.open_session]
    assert sessions[0].ended_at is not None and sessions[1].ended_at is None
    assert len(read_session_transcript(vault, subject, topic, read_vault.ended_session)) == 3
    assert [s.kind for s in list_sources(vault, subject, topic)] == ["notes", "web"]
    assert reader.get("/api/subjects").status_code == 200


def test_no_read_route_is_exempt_from_the_bearer_check() -> None:
    assert not any(path.startswith(("/api/sources", "/api/sessions")) for _, path in EXEMPT_ROUTES)
    assert not any("/topics/" in path for _, path in EXEMPT_ROUTES)


# -- session list -------------------------------------------------------------------------------


def test_sessions_lists_the_topics_sessions_with_their_minutes(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}/sessions")

    assert response.status_code == 200
    body = response.json()
    assert (body["subject_id"], body["topic_id"]) == (read_vault.subject, read_vault.topic)
    ended, open_ = body["sessions"]
    assert ended["session_id"] == read_vault.ended_session
    assert ended["minutes"] == 30.0 and ended["ended_at"] is not None
    assert open_["session_id"] == read_vault.open_session
    assert open_["ended_at"] is None and open_["minutes"] >= 0


def test_sessions_of_an_empty_topic_is_an_empty_list(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(
        f"/api/subjects/{read_vault.subject}/topics/{read_vault.empty_topic}/sessions"
    )
    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_sessions_of_an_unknown_topic_is_404_in_spanish(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(f"/api/subjects/{read_vault.subject}/topics/optica/sessions")
    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_a_path_id_outside_the_protocol_pattern_is_422(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(f"/api/subjects/{read_vault.subject}/topics/.hidden/sessions")
    assert response.status_code == 422


# -- pending review -----------------------------------------------------------------------------


def test_pending_lists_the_queue_open_items_first(
    read_vault: ReadVault, reader: TestClient
) -> None:
    base = f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}/pending"
    response = reader.get(base)

    assert response.status_code == 200
    body = response.json()
    assert (body["subject_id"], body["topic_id"]) == (read_vault.subject, read_vault.topic)
    assert body["open_count"] == 1
    assert [(i["id"], i["kind"], i["status"]) for i in body["items"]] == [
        ("p2", "incomplete", "open"),
        ("p1", "illegible", "auto_resolved"),
    ]
    first = body["items"][1]
    assert first["text"] == "x" and first["resolution"] == "dice «aceleración»"
    assert first["created_by"] == "observer"
    assert first["refs"] == {"pages": [], "segments": [], "sources": []}

    open_only = reader.get(base, params={"status": "open"}).json()
    assert [i["id"] for i in open_only["items"]] == ["p2"] and open_only["open_count"] == 1
    closed = reader.get(base, params={"status": "closed"}).json()
    assert [i["id"] for i in closed["items"]] == ["p1"]
    assert reader.get(base, params={"status": "later"}).status_code == 422


def test_pending_of_an_empty_topic_is_empty_and_of_an_unknown_one_404(
    read_vault: ReadVault, reader: TestClient
) -> None:
    empty = reader.get(
        f"/api/subjects/{read_vault.subject}/topics/{read_vault.empty_topic}/pending"
    )
    assert empty.status_code == 200
    assert empty.json()["open_count"] == 0 and empty.json()["items"] == []
    unknown = reader.get(f"/api/subjects/{read_vault.subject}/topics/optica/pending")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "No existe ese tema en la bóveda."


# -- transcript spans ---------------------------------------------------------------------------


def transcript(reader: TestClient, rv: ReadVault, span: str, session: str | None = None) -> Any:
    return reader.get(
        f"/api/sessions/{session or rv.ended_session}/transcript",
        params={"subject": rv.subject, "topic": rv.topic, "t": span},
    )


def test_the_span_returns_the_segments_overlapping_it(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = transcript(reader, read_vault, "00:00:03-00:00:05")

    assert response.status_code == 200
    body = response.json()
    assert body["ref"] == f"sessions/{read_vault.ended_session}#t=00:00:03-00:00:05"
    assert (body["start_ms"], body["end_ms"]) == (3_000, 5_000)
    assert [s["seq"] for s in body["segments"]] == [1, 2]
    assert body["segments"][1]["text"] == "La velocidad es la derivada de la posición."


def test_the_adr_0005_example_span_selects_the_segment_it_cites(
    read_vault: ReadVault, reader: TestClient
) -> None:
    body = transcript(reader, read_vault, "00:02:34-00:03:10").json()
    assert [s["seq"] for s in body["segments"]] == [3]


def test_a_span_touching_a_segment_only_at_its_edge_leaves_it_out(
    read_vault: ReadVault, reader: TestClient
) -> None:
    body = transcript(reader, read_vault, "00:00:09-00:02:34").json()
    assert body["segments"] == []


def test_a_point_span_takes_the_segments_spanning_it(
    read_vault: ReadVault, reader: TestClient
) -> None:
    body = transcript(reader, read_vault, "00:00:04-00:00:04").json()
    assert [s["seq"] for s in body["segments"]] == [1, 2]


def test_an_unended_sessions_transcript_is_readable(
    read_vault: ReadVault, reader: TestClient
) -> None:
    body = transcript(reader, read_vault, "00:00:00-00:01:00", read_vault.open_session).json()
    assert [s["text"] for s in body["segments"]] == ["Seguimos con la caída libre."]


@pytest.mark.parametrize(
    "span", ["", "00:00:05", "0:00:01-0:00:02", "00:61:00-00:62:00", "00:00:09-00:00:01", "a-b"]
)
def test_a_malformed_span_is_422_in_spanish(
    read_vault: ReadVault, reader: TestClient, span: str
) -> None:
    response = transcript(reader, read_vault, span)
    assert response.status_code == 422
    assert "HH:MM:SS-HH:MM:SS" in response.json()["detail"]


def test_parse_span_reads_hours_minutes_and_seconds() -> None:
    assert parse_span("01:02:03-01:02:04") == (3_723_000, 3_724_000)
    with pytest.raises(ValueError):
        parse_span("01:02:03")


def test_an_unknown_session_is_404(read_vault: ReadVault, reader: TestClient) -> None:
    response = transcript(reader, read_vault, "00:00:00-00:00:01", "20200101-000000")
    assert response.status_code == 404
    assert response.json()["detail"] == "No existe esa sesión en ese tema."


def test_a_session_of_another_topic_is_404(read_vault: ReadVault, reader: TestClient) -> None:
    response = reader.get(
        f"/api/sessions/{read_vault.ended_session}/transcript",
        params={
            "subject": read_vault.subject,
            "topic": read_vault.empty_topic,
            "t": "00:00:00-00:00:01",
        },
    )
    assert response.status_code == 404


def test_the_transcript_needs_subject_and_topic(read_vault: ReadVault, reader: TestClient) -> None:
    response = reader.get(
        f"/api/sessions/{read_vault.ended_session}/transcript", params={"t": "00:00:00-00:00:01"}
    )
    assert response.status_code == 422


# -- auth and vault availability ----------------------------------------------------------------


def test_a_lan_client_without_a_token_gets_401(
    read_vault: ReadVault, lan_reader: TestClient
) -> None:
    rv = read_vault
    for path in (
        f"/api/subjects/{rv.subject}/topics/{rv.topic}/sessions",
        f"/api/subjects/{rv.subject}/topics/{rv.topic}/summary",
        f"/api/subjects/{rv.subject}/topics/{rv.topic}/notes",
        f"/api/sessions/{rv.ended_session}/transcript?subject={rv.subject}&topic={rv.topic}&t=00:00:00-00:00:01",
        f"/api/sources/{rv.notes_page}",
        f"/api/sources/{rv.notes_page}/meta",
    ):
        response = lan_reader.get(path)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_paired_lan_client_reads_with_its_token(
    read_vault: ReadVault, read_app: FastAPI, lan_reader: TestClient
) -> None:
    _, token = read_app.state.devices.issue_token("Móvil")
    response = lan_reader.get(
        f"/api/subjects/{read_vault.subject}/topics/{read_vault.topic}/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_every_read_route_is_503_when_the_vault_cannot_be_opened(
    server: ServerSettings, tmp_path: Path
) -> None:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        vault_settings=VaultSettings(path=tmp_path / "no-vault"),
    )
    client = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))
    for path in (
        "/api/subjects/fisica/topics/cinematica/sessions",
        "/api/subjects/fisica/topics/cinematica/summary",
        "/api/subjects/fisica/topics/cinematica/notes",
        "/api/subjects/fisica/topics/cinematica/pending",
        "/api/sessions/20260924-100000/transcript?subject=fisica&topic=cinematica&t=00:00:00-00:00:01",
        "/api/sources/subjects/fisica/topics/cinematica/sources/notes/page-001.jpg",
        "/api/sources/subjects/fisica/topics/cinematica/sources/notes/page-001.jpg/meta",
    ):
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.json()["detail"] == "No se puede abrir la bóveda."
