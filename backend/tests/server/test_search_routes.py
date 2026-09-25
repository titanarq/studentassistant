"""`GET /api/search` (`server/search_routes.py`) over the index the session service opens.

The index always lives under `tmp_path` (`[vault] index_path`), never in `~/.cache`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from read_api_fixtures import ReadVault

from studentassistant.config import ServerSettings, VaultSettings
from studentassistant.protocol import model_for
from studentassistant.server.app import create_app
from studentassistant.server.auth import EXEMPT_ROUTES
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.search_routes import parse_kinds
from studentassistant.server.sessions import SessionService
from studentassistant.vault.index import SNIPPET_END, SNIPPET_START

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
LOCAL_HOST_HEADER = "localhost:8765"

ClientWithHost = Callable[[FastAPI, str], TestClient]


def conforms(body: Any) -> Any:
    """`body` checked against `rest.search.response`: its JSON Schema and the protocol model."""
    schema = json.loads((PROTOCOL_DIR / "rest.search.response.schema.json").read_text("utf-8"))
    Draft202012Validator(schema).validate(body)
    model_for("rest.search.response").model_validate(body)
    return body


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "index.sqlite3"


@pytest.fixture
def app(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    read_vault: ReadVault,
    index_path: Path,
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=read_vault.vault,
        vault_settings=VaultSettings(path=read_vault.vault.path, index_path=index_path),
    )


@pytest.fixture
def searcher(local: TestClient) -> TestClient:
    """A loopback client (trusted without a token) of the app over the populated vault."""
    return local


def search(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/search", params=params)
    assert response.status_code == 200, response.text
    return conforms(response.json())


# -- results -------------------------------------------------------------------------------------


def test_a_transcript_hit_names_its_session_segment_and_time(
    read_vault: ReadVault, searcher: TestClient, index_path: Path
) -> None:
    body = search(searcher, q="coche frena")

    assert body["query"] == "coche frena"
    (hit,) = body["hits"]
    assert hit["kind"] == "transcript"
    assert (hit["subject"], hit["topic"]) == (read_vault.subject, read_vault.topic)
    assert hit["session"] == read_vault.ended_session
    assert (hit["seq"], hit["t_start"]) == (3, 154_000)
    assert hit["path"].endswith(f"sessions/{read_vault.ended_session}/transcript.jsonl")
    assert "source" not in hit  # absent, never null
    assert f"{SNIPPET_START}coche{SNIPPET_END}" in hit["snippet"]
    assert index_path.is_file()


def test_accents_and_case_are_ignored(searcher: TestClient) -> None:
    hits = search(searcher, q="POSICION")["hits"]
    assert [hit["kind"] for hit in hits] == ["transcript"]
    assert "posición" in hits[0]["snippet"]


def test_a_web_hit_names_its_source(read_vault: ReadVault, searcher: TestClient) -> None:
    (hit,) = search(searcher, q="rectilíneo", kinds="web")["hits"]
    assert hit["kind"] == "web"
    assert hit["path"] == hit["source"] == read_vault.web_page
    assert "session" not in hit and "seq" not in hit and "t_start" not in hit


def test_kinds_filter_the_hits(searcher: TestClient) -> None:
    every = {hit["kind"] for hit in search(searcher, q="rectilíneo")["hits"]}
    assert every == {"transcript", "web"}
    only = search(searcher, q="rectilíneo", kinds="transcript, transcript")["hits"]
    assert {hit["kind"] for hit in only} == {"transcript"}
    assert search(searcher, q="rectilíneo", kinds="notes,page")["hits"] == []


def test_subject_and_topic_filter_the_hits(read_vault: ReadVault, searcher: TestClient) -> None:
    subject, topic = read_vault.subject, read_vault.topic
    assert len(search(searcher, q="velocidad", subject=subject, topic=topic)["hits"]) == 1
    assert (
        search(searcher, q="velocidad", subject=subject, topic=read_vault.empty_topic)["hits"] == []
    )
    assert search(searcher, q="velocidad", subject="quimica")["hits"] == []


def test_limit_caps_the_hits(searcher: TestClient) -> None:
    assert len(search(searcher, q="rectilíneo")["hits"]) == 2
    assert len(search(searcher, q="rectilíneo", limit=1)["hits"]) == 1


def test_a_query_without_words_matches_nothing(searcher: TestClient) -> None:
    assert search(searcher, q="  ¿? ")["hits"] == []
    assert search(searcher, q="")["hits"] == []


# -- refusals ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": "x", "limit": 0},
        {"q": "x", "limit": 101},
        {"q": "x" * 501},
        {"q": "x", "subject": "no válido"},
        {"q": "x", "subject": "fisica", "topic": "../x"},
    ],
)
def test_bad_parameters_are_422(searcher: TestClient, params: dict[str, Any]) -> None:
    assert searcher.get("/api/search", params=params).status_code == 422


def test_an_unknown_kind_is_422_in_spanish(searcher: TestClient) -> None:
    response = searcher.get("/api/search", params={"q": "x", "kinds": "notes,audio"})
    assert response.status_code == 422
    assert "audio" in response.json()["detail"] and "Tipo" in response.json()["detail"]


def test_a_topic_without_its_subject_is_422(searcher: TestClient) -> None:
    response = searcher.get("/api/search", params={"q": "x", "topic": "cinematica"})
    assert response.status_code == 422
    assert "asignatura" in response.json()["detail"]


def test_parse_kinds() -> None:
    assert parse_kinds(None) is None
    assert parse_kinds(" , ") is None
    assert parse_kinds("web,notes,web") == ("web", "notes")
    with pytest.raises(ValueError, match="audio"):
        parse_kinds("audio")


def test_search_needs_the_bearer_token(lan: TestClient) -> None:
    assert not any(path.startswith("/api/search") for _, path in EXEMPT_ROUTES)
    assert lan.get("/api/search", params={"q": "velocidad"}).status_code == 401


def test_a_vault_that_cannot_be_opened_is_503(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, client_with_host: ClientWithHost
) -> None:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault_settings=VaultSettings(path=tmp_path / "no-vault", index_path=tmp_path / "i.db"),
    )
    client = client_with_host(app, LOCAL_HOST_HEADER)
    response = client.get("/api/search", params={"q": "x"})
    assert response.status_code == 503
    assert response.json()["detail"] == "No se puede abrir la bóveda."


def test_an_index_that_cannot_be_opened_is_503_and_the_vault_still_works(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    read_vault: ReadVault,
    client_with_host: ClientWithHost,
) -> None:
    unusable = tmp_path / "a-directory"
    unusable.mkdir()
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=read_vault.vault,
        vault_settings=VaultSettings(path=read_vault.vault.path, index_path=unusable),
    )
    client = client_with_host(app, LOCAL_HOST_HEADER)

    response = client.get("/api/search", params={"q": "velocidad"})

    assert response.status_code == 503
    assert response.json()["detail"] == "El índice de búsqueda no está disponible."
    assert client.get("/api/subjects").status_code == 200


# -- the lifespan --------------------------------------------------------------------------------


def test_the_lifespan_runs_the_index_loop_and_closes_it_at_shutdown(
    app: FastAPI, client_with_host: ClientWithHost
) -> None:
    service: SessionService = app.state.sessions
    with client_with_host(app, LOCAL_HOST_HEADER) as client:
        assert not service.index_running  # the vault (and so the index) opens lazily
        assert len(search(client, q="velocidad")["hits"]) == 1
        assert service.index is not None and service.index_running
    assert service.index is None and not service.index_running
