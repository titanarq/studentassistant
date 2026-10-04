"""Every content route is scoped to the active user (#551, epic #544).

Two students share one vault and one backend, with the SAME subject and topic slugs. For each
family of content routes the tests here check that a read shows only the caller's data and that a
write lands only under the caller's `users/<id>/`, with `FakeClaude` standing in for every LLM
role. Which user a request acts for is `X-SA-User`; what a request that names none, or an unknown
one, is told is walked once for the whole list of routes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import studentassistant.server as server_package
from material_generators import DEFAULT_POINTS, KIND, points_registry, reply_points
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.llm import FakeClaude
from studentassistant.protocol import USER_HEADER
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import (
    FeedbackContext,
    GitSync,
    LedgerEntry,
    Vault,
    add_feedback,
    append_ledger_entry,
    create_user,
    put_source,
    read_notes,
    write_notes,
)

LOCAL_BASE_URL = "http://localhost:8765"
ANA = "ana-garcia"
LUCIA = "lucia-fernandez"
CONTENT_MODULES = (
    "read_routes",
    "notes_routes",
    "notes_edit_routes",
    "revise_routes",
    "versions_routes",
    "source_routes",
    "pdf_upload",
    "book_routes",
    "web_search_routes",
    "style_guide_routes",
    "study_routes",
    "study_requests",
    "quiz_routes",
    "exam_routes",
    "practice_routes",
    "practice_summary_routes",
    "doubts_routes",
    "tutor_routes",
    "generators_routes",
    "workspace_routes",
    "feedback_routes",
    "cost",
)


def as_user(user_id: str) -> dict[str, str]:
    return {USER_HEADER: user_id}


@pytest.fixture
def lucia_vault(tmp_vault: Vault) -> Vault:
    """The second student of the vault; Ana is the one `tmp_vault` is born with."""
    profile = create_user(tmp_vault, "Lucía Fernández")
    assert profile.id == LUCIA
    return tmp_vault.for_user(LUCIA)


@pytest.fixture
def ana(tmp_vault: Vault, lucia_vault: Vault) -> ReviseTopic:
    topic = make_revise_topic(tmp_vault.for_user(ANA))
    GitSync(tmp_vault).for_user(ANA).create_notes_tag(topic.subject, topic.topic, "Apuntes v1")
    return topic


@pytest.fixture
def lucia(tmp_vault: Vault, lucia_vault: Vault, ana: ReviseTopic) -> ReviseTopic:
    """Lucía's topic has Ana's slugs and her own notes (one more sentence), and no style rule."""
    topic = make_revise_topic(lucia_vault)
    assert (topic.subject, topic.topic) == (ana.subject, ana.topic)
    write_notes(lucia_vault, topic.subject, topic.topic, topic.notes + "\nSolo de Lucía.\n")
    GitSync(tmp_vault).for_user(LUCIA).create_notes_tag(
        topic.subject, topic.topic, "Apuntes de Lucía v1"
    )
    return topic


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault, fake: FakeClaude
) -> FastAPI:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    app.state.generators = points_registry()
    return app


@pytest.fixture
def client(app: FastAPI, ana: ReviseTopic, lucia: ReviseTopic) -> Iterator[TestClient]:
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _source_ids(client: TestClient, topic: ReviseTopic, user: str) -> set[str]:
    listed = client.get(f"{_topic(topic)}/sources", headers=as_user(user)).json()["sources"]
    return {source["vault_id"] for source in listed}


def _topic(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}"


# -- the module list ------------------------------------------------------------------------------


def test_no_content_route_opens_the_root_handle() -> None:
    """The routes get their vault and sync from `active_user_vault`, never from the root."""
    folder = Path(server_package.__file__).parent
    offenders = [
        name
        for name in CONTENT_MODULES
        if re.search(r"\.open_vault\(|\.sync\b", (folder / f"{name}.py").read_text("utf-8"))
    ]
    assert offenders == []


# -- who is asking --------------------------------------------------------------------------------


_BODIES = {
    ("PUT", "/notes"): {"text": "# Notas", "base_revision": "0" * 64},
    ("POST", "s/chat"): {"message": "Añade algo."},
}
"""The bodies of the writes in `_routes`, keyed by method and the last characters of the path."""


def _routes(topic: ReviseTopic) -> list[tuple[str, str]]:
    base = _topic(topic)
    subject = f"/api/subjects/{topic.subject}"
    return [
        ("GET", f"{base}/summary"),
        ("GET", f"{base}/sources"),
        ("GET", f"{base}/notes"),
        ("PUT", f"{base}/notes"),
        ("POST", f"{base}/notes/chat"),
        ("GET", f"{base}/notes/chat"),
        ("GET", f"{base}/notes/versions"),
        ("GET", f"{base}/book"),
        ("GET", f"{base}/study"),
        ("GET", f"{base}/quiz"),
        ("GET", f"{base}/exam"),
        ("GET", f"{base}/practice"),
        ("GET", "/api/practice/summary"),
        ("GET", f"{base}/doubts"),
        ("GET", f"{base}/tutor"),
        ("GET", f"{base}/generated"),
        ("GET", f"{base}/web-searches"),
        ("GET", f"{subject}/style-guide"),
        ("GET", f"{base}/workspace/stream"),
        ("GET", "/api/feedback"),
        ("GET", "/api/cost"),
        ("GET", f"{base}/cost"),
    ]


def test_a_missing_user_with_several_users_is_user_required(
    client: TestClient, ana: ReviseTopic
) -> None:
    for method, path in _routes(ana):
        response = client.request(method, path, json=_BODIES.get((method, path[-6:])))
        assert response.status_code == 400, (method, path, response.text)
        assert response.json()["code"] == "user_required", (method, path)


def test_an_unknown_user_is_not_found(client: TestClient, ana: ReviseTopic) -> None:
    for method, path in _routes(ana):
        response = client.request(
            method, path, headers=as_user("nadie"), json=_BODIES.get((method, path[-6:]))
        )
        assert response.status_code == 404, (method, path, response.text)
        assert response.json()["code"] == "user_not_found", (method, path)


# -- reads ----------------------------------------------------------------------------------------


def test_reads_show_only_the_callers_data(
    client: TestClient, ana: ReviseTopic, lucia: ReviseTopic
) -> None:
    base = _topic(ana)

    mine = client.get(f"{base}/notes", headers=as_user(LUCIA)).json()
    theirs = client.get(f"{base}/notes", headers=as_user(ANA)).json()
    assert "Solo de Lucía." in mine["text"]
    assert "Solo de Lucía." not in theirs["text"]

    for user in (ANA, LUCIA):
        summary = client.get(f"{base}/summary", headers=as_user(user)).json()
        assert summary["sources"]["notes"] == 2 and summary["sources"]["book"] == 1
        assert summary["notes_version"] == 1  # the user's own tag only, never the other's


def test_the_sources_are_the_callers(
    client: TestClient, tmp_vault: Vault, ana: ReviseTopic
) -> None:
    ana_vault = tmp_vault.for_user(ANA)
    page = put_source(ana_vault, ana.subject, ana.topic, "book", "page.jpg", b"\xff\xd8 only", {})
    only_ana = page.relative_to(ana_vault.path).as_posix()
    base = _topic(ana)

    ana_ids = {
        s["vault_id"] for s in client.get(f"{base}/sources", headers=as_user(ANA)).json()["sources"]
    }
    lucia_ids = {
        s["vault_id"]
        for s in client.get(f"{base}/sources", headers=as_user(LUCIA)).json()["sources"]
    }

    assert only_ana in ana_ids
    assert only_ana not in lucia_ids
    assert ana_ids - {only_ana} == lucia_ids

    # A source path of Ana's, asked as Lucía, does not exist for her; as Ana it is removed.
    assert client.delete(f"/api/sources/{only_ana}", headers=as_user(LUCIA)).status_code == 404
    assert page.exists()
    assert only_ana in _source_ids(client, ana, ANA)
    assert client.delete(f"/api/sources/{only_ana}", headers=as_user(ANA)).status_code == 204
    assert only_ana not in _source_ids(client, ana, ANA)


def test_the_versions_are_the_callers(client: TestClient, ana: ReviseTopic) -> None:
    base = _topic(ana)
    for user in (ANA, LUCIA):
        versions = client.get(f"{base}/notes/versions", headers=as_user(user)).json()["versions"]
        assert [v["version"] for v in versions] == [1]
    ana_message = client.get(f"{base}/notes/versions", headers=as_user(ANA)).json()["versions"][0]
    lucia_message = client.get(f"{base}/notes/versions", headers=as_user(LUCIA)).json()["versions"][
        0
    ]
    assert ana_message["message"] == "Apuntes v1"
    assert lucia_message["message"] == "Apuntes de Lucía v1"
    text = client.get(f"{base}/notes/versions/1", headers=as_user(LUCIA)).json()["text"]
    assert "Solo de Lucía." in text  # her own version, read from her own tag


def test_cost_and_practice_aggregate_only_the_callers(
    client: TestClient, tmp_vault: Vault, ana: ReviseTopic
) -> None:
    entry = LedgerEntry(
        time=datetime.now(UTC),
        role="editor",
        model="claude-sonnet-5-5",
        input_tokens=10,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        estimated_usd=1.25,
        subject=ana.subject,
        topic=ana.topic,
        session=None,
    )
    append_ledger_entry(tmp_vault.for_user(ANA), ana.subject, ana.topic, entry)

    assert client.get("/api/cost", headers=as_user(ANA)).json()["day_usd"] == pytest.approx(1.25)
    assert client.get("/api/cost", headers=as_user(LUCIA)).json()["day_usd"] == 0
    topic_cost = client.get(f"{_topic(ana)}/cost", headers=as_user(LUCIA)).json()
    assert topic_cost["total"]["usd"] == 0
    assert client.get(f"{_topic(ana)}/cost", headers=as_user(ANA)).json()["total"]["usd"] == (
        pytest.approx(1.25)
    )

    summaries = {
        user: client.get("/api/practice/summary", headers=as_user(user)) for user in (ANA, LUCIA)
    }
    assert all(response.status_code == 200 for response in summaries.values())


def test_the_feedback_inbox_lists_only_the_callers_reports(
    client: TestClient, tmp_vault: Vault, ana: ReviseTopic
) -> None:
    add_feedback(
        tmp_vault.for_user(ANA), "bug", "De Ana", "El botón no responde.", FeedbackContext()
    )
    add_feedback(
        tmp_vault.for_user(LUCIA), "mejora", "De Lucía", "Un modo oscuro.", FeedbackContext()
    )

    ana_items = client.get("/api/feedback", headers=as_user(ANA)).json()["items"]
    lucia_items = client.get("/api/feedback", headers=as_user(LUCIA)).json()["items"]

    assert [(i["title"], i["context"]["user"]) for i in ana_items] == [("De Ana", ANA)]
    assert [(i["title"], i["context"]["user"]) for i in lucia_items] == [("De Lucía", LUCIA)]


# -- writes ---------------------------------------------------------------------------------------


def test_a_save_lands_in_the_callers_folder(
    client: TestClient, tmp_vault: Vault, ana: ReviseTopic, lucia: ReviseTopic
) -> None:
    base = _topic(ana)
    current = client.get(f"{base}/notes", headers=as_user(LUCIA)).json()
    edited = current["text"].replace("Solo de Lucía.", "Solo de Lucía, corregido.")

    saved = client.put(
        f"{base}/notes",
        headers=as_user(LUCIA),
        json={"text": edited, "base_revision": current["revision"]},
    )

    assert saved.status_code == 200, saved.text
    assert "corregido" in (read_notes(tmp_vault.for_user(LUCIA), lucia.subject, lucia.topic) or "")
    assert read_notes(tmp_vault.for_user(ANA), ana.subject, ana.topic) == ana.notes
    # The notes version is Lucía's tag; Ana's list is untouched.
    ana_versions = client.get(f"{base}/notes/versions", headers=as_user(ANA)).json()["versions"]
    assert [v["version"] for v in ana_versions] == [1]


def test_a_restore_writes_only_the_callers_notes(
    client: TestClient, tmp_vault: Vault, ana: ReviseTopic, lucia: ReviseTopic
) -> None:
    base = _topic(ana)
    write_notes(tmp_vault.for_user(LUCIA), lucia.subject, lucia.topic, lucia.notes + "\nOtra.\n")

    restored = client.post(f"{base}/notes/versions/1/restore", headers=as_user(LUCIA))

    assert restored.status_code == 200, restored.text
    assert "Solo de Lucía." in (
        read_notes(tmp_vault.for_user(LUCIA), lucia.subject, lucia.topic) or ""
    )
    assert read_notes(tmp_vault.for_user(ANA), ana.subject, ana.topic) == ana.notes


def test_the_book_title_and_the_style_guide_are_per_user(
    client: TestClient, ana: ReviseTopic
) -> None:
    base = _topic(ana)
    subject = f"/api/subjects/{ana.subject}"

    assert (
        client.put(
            f"{base}/book", headers=as_user(LUCIA), json={"title": "El libro de Lucía"}
        ).status_code
        == 200
    )
    assert client.get(f"{base}/book", headers=as_user(LUCIA)).json()["title"] == "El libro de Lucía"
    assert client.get(f"{base}/book", headers=as_user(ANA)).json()["title"] is None

    added = client.post(
        f"{subject}/style-guide/rules", headers=as_user(LUCIA), json={"rules": ["Usa tablas."]}
    )
    assert added.status_code == 200, added.text
    rules = {
        user: client.get(f"{subject}/style-guide", headers=as_user(user)).json()
        for user in (ANA, LUCIA)
    }
    assert "Usa tablas." in str(rules[LUCIA])
    assert "Usa tablas." not in str(rules[ANA])


def test_a_generation_writes_the_callers_folder_with_the_fake_model(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault, ana: ReviseTopic
) -> None:
    reply_points(fake, *DEFAULT_POINTS)

    response = client.post(
        f"{_topic(ana)}/generated/{KIND}", headers=as_user(LUCIA), json={"options": {"size": 2}}
    )

    assert response.status_code == 200, response.text
    assert fake.requests[0].role == "generator"
    generated = {
        user: sorted(
            path.name
            for path in (tmp_vault.root / "users" / user).rglob("generated/*")
            if path.is_file()
        )
        for user in (ANA, LUCIA)
    }
    assert any(name.startswith(KIND) for name in generated[LUCIA])
    assert not any(name.startswith(KIND) for name in generated[ANA])
    status = {
        user: client.get(f"{_topic(ana)}/generated", headers=as_user(user)).json()
        for user in (ANA, LUCIA)
    }
    assert status[LUCIA]["artifacts"][0]["generated"] is True
    assert status[ANA]["artifacts"][0]["generated"] is False


def test_the_tutor_and_the_editor_chat_work_in_the_callers_folder(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault, ana: ReviseTopic
) -> None:
    base = _topic(ana)
    fake.reply_text("Es el límite del cociente incremental.")

    asked = client.post(
        f"{base}/tutor", headers=as_user(LUCIA), json={"question": "¿Qué es la derivada?"}
    )

    assert asked.status_code == 200, asked.text
    assert len(client.get(f"{base}/tutor", headers=as_user(LUCIA)).json()["turns"]) == 1
    assert client.get(f"{base}/tutor", headers=as_user(ANA)).json()["turns"] == []
    assert client.get(f"{base}/notes/chat", headers=as_user(ANA)).json()["turns"] == []


def test_the_study_switch_labels_the_callers_version_only(
    client: TestClient, ana: ReviseTopic
) -> None:
    base = _topic(ana)

    switched = client.post(f"{base}/study", headers=as_user(LUCIA))

    assert switched.status_code == 200, switched.text
    assert switched.json()["study_version"]["tag"].startswith(f"{LUCIA}/")
    ana_versions = client.get(f"{base}/notes/versions", headers=as_user(ANA)).json()["versions"]
    assert [v["version"] for v in ana_versions] == [1]
