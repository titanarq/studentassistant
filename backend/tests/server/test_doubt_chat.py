"""Doubts marked in the notes and shown in the workspace chat on demand (#325, #516).

The real app (`FakeClaude`, `tmp_vault`, the FastAPI test client): an editor turn reports doubts,
the editor reviews the unreviewed ones and `doubts.marked` announces the marks -- nothing is asked
one after another --; `POST .../doubts/{id}/ask` shows one in the chat, an answer through the
doubts route is broadcast; during a session the events go to its live log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.editor.doubts import (
    DECISION_TOOL,
    PENDING_QUESTION_KIND,
    REVIEW_TOOL,
    list_doubts,
)
from studentassistant.editor.notes_format import notes_revision
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.llm import FakeClaude
from studentassistant.observer import ASSISTANT_REQUEST_KIND, STATE_OP_EVENT_KIND
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import AssistantRequestConsumer
from studentassistant.server.doubt_chat import DoubtChat
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import WorkspaceEvent, WorkspaceSubscription
from studentassistant.vault import Vault, list_sessions, read_notes, read_topic_events

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
PAGE_1 = "sources/notes/page-001.jpg"
PAGE_2 = "sources/notes/page-002.jpg"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


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
        # The observer stays off, so every scripted reply is the editor's.
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _base(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}"


def _settle(client: TestClient) -> None:
    app: Any = client.app
    requests: AssistantRequestConsumer = app.state.assistant_requests
    chat: DoubtChat = app.state.doubt_chat
    client.portal.call(requests.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]
    client.portal.call(chat.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]


def _subscribe(client: TestClient, topic: ReviseTopic) -> WorkspaceSubscription:
    app: Any = client.app
    return app.state.workspace.subscribe(topic.subject, topic.topic)  # type: ignore[no-any-return]


def _names(events: list[WorkspaceEvent]) -> list[str]:
    return [e.event for e in events if e.event != "reply.delta"]


def _doubt(kind: str, text: str, question: str, ref: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text,
        "question": question,
        "suggestions": ["incremental", "diferencial"],
        "refs": [ref],
    }


FIRST = _doubt(
    "illegible",
    "Palabra dudosa tras «cociente» en la página 1.",
    "¿Qué pone después de «cociente»?",
    PAGE_1,
)
SECOND = _doubt(
    "incomplete",
    "Falta el final de la frase de la página 2.",
    "¿Cómo termina la frase de la página 2?",
    PAGE_2,
)


def _edit_with_doubts(fake: FakeClaude) -> None:
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Aclaro la definición",
            "ops": [
                {
                    "op": "replace_block",
                    "section": "definicion",
                    "block": 1,
                    "text": "**Derivada**: el límite del cociente.[^p1][^t1]",
                }
            ],
            "doubts": [FIRST, SECOND],
        },
        text="Lo aclaro y te pregunto lo que no se lee.",
    )


def _review_p1(fake: FakeClaude, topic: ReviseTopic) -> None:
    """The review before asking: the observer's p-1 is settled by the transcript."""
    fake.reply_tool(
        REVIEW_TOOL,
        {
            "decisions": [
                {
                    "pending_id": "p-1",
                    "action": "auto_resolve",
                    "resolution": "La regla de la cadena se repasa el próximo día.",
                    "evidence": [
                        {
                            "source_id": f"sessions/{topic.session}#t=01:02:05-01:02:10",
                            "quote": "Mañana repasamos la regla de la cadena.",
                        }
                    ],
                }
            ]
        },
    )


def _in_chat(topic: ReviseTopic) -> list[dict[str, Any]]:
    return [
        event.payload
        for _sid, event in read_topic_events(topic.vault, topic.subject, topic.topic)
        if event.kind == PENDING_QUESTION_KIND and event.payload.get("in_chat")
    ]


def test_a_turn_with_doubts_marks_them_and_one_is_shown_when_asked_for(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    subscription = _subscribe(client, topic)
    _edit_with_doubts(fake)
    _review_p1(fake, topic)

    response = client.post(f"{_base(topic)}/notes/chat", json={"message": "Aclara la definición"})
    assert response.status_code == 200, response.text
    _settle(client)

    events = [e for e in subscription.drain() if e.event != "reply.delta"]
    assert _names(events) == [
        "turn.started",
        "turn.result",
        "notes.changed",
        "doubts.auto_resolved",
        "doubts.marked",
    ]
    result = events[1].data
    first_id, second_id = result["doubts"]
    auto = events[3].data
    assert auto["pending_ids"] == ["p-1"] and auto["summary"].startswith("He resuelto")
    assert events[4].data == {"count": 2}
    # Nothing is asked one after another.
    assert _in_chat(topic) == []

    marks = client.get(f"{_base(topic)}/doubts/marks")
    assert marks.status_code == 200, marks.text
    body = marks.json()
    assert body["count"] == 2
    by_id = {mark["pending_id"]: mark for mark in body["marks"]}
    assert by_id[first_id]["level"] == "block"
    assert by_id[first_id]["blocks"] == [{"section": "definicion", "number": 1}]
    assert by_id[second_id]["blocks"] == [{"section": "proximo-dia", "number": 1}]
    assert [mark["pending_id"] for mark in body["marks"]] == [first_id, second_id]

    shown = client.post(f"{_base(topic)}/doubts/{first_id}/ask")
    assert shown.status_code == 200, shown.text
    assert shown.json() == {
        "pending_id": first_id,
        "asked": True,
        "status": "open",
        "summary": None,
    }
    events = subscription.drain()
    assert _names(events) == ["doubt.asked"]
    assert events[0].data == {
        "pending_id": first_id,
        "kind": "illegible",
        "text": FIRST["text"],
        "question": FIRST["question"],
        "suggestions": FIRST["suggestions"],
        "options": [],
        "refs": [PAGE_1],
    }
    # Clicked again while it is the doubt asked: announced again, nothing new written.
    again = client.post(f"{_base(topic)}/doubts/{first_id}/ask")
    assert again.status_code == 200, again.text
    assert _names(subscription.drain()) == ["doubt.asked"]
    assert len(_in_chat(topic)) == 1
    assert client.get(f"{_base(topic)}/doubts/marks").json()["marks"][0]["asked"] is True

    fake.reply_tool(
        DECISION_TOOL,
        {
            "resolution": "Pone «incremental».",
            "edits": [
                {
                    "op": "replace_block",
                    "section": "definicion",
                    "block": 1,
                    "text": "**Derivada**: el límite del cociente incremental.[^p1][^t1]",
                }
            ],
        },
    )
    answered = client.post(f"{_base(topic)}/doubts/{first_id}/answer", json={"suggestion": 1})
    assert answered.status_code == 200, answered.text
    _settle(client)

    events = subscription.drain()
    assert _names(events) == ["doubt.resolved", "notes.changed", "doubts.marked"]
    assert events[0].data == {
        "pending_id": first_id,
        "status": "resolved",
        "resolution": "Pone «incremental».",
        "notes_changed": True,
    }
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    assert events[1].data["origin"] == "editor"
    assert events[1].data["revision"] == notes_revision(notes)
    assert events[2].data == {"count": 1}

    turns = client.get(f"{_base(topic)}/notes/chat").json()["turns"]
    kinds = [turn["kind"] for turn in turns]
    assert kinds == ["revise", "doubts_resolved", "doubt"]
    first = turns[2]
    assert first["pending_id"] == first_id and first["status"] == "resolved"
    assert first["answer"] == "incremental" and first["resolution"] == "Pone «incremental»."
    assert first["doubt_text"] == FIRST["text"]

    closed = client.post(f"{_base(topic)}/doubts/{first_id}/ask")
    assert closed.status_code == 409 and closed.json()["code"] == "doubt_closed"
    unknown = client.post(f"{_base(topic)}/doubts/duda-nope/ask")
    assert unknown.status_code == 404
    assert subscription.drain() == []


def test_during_a_session_the_doubts_go_to_its_live_log(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    started = client.post(
        "/api/sessions",
        json={"subject_id": topic.subject, "topic_id": topic.topic, "client_time_ms": 1_000},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]
    subscription = _subscribe(client, topic)
    _edit_with_doubts(fake)
    _review_p1(fake, topic)

    bus = client.app.state.bus  # type: ignore[attr-defined]

    async def publish() -> None:
        await bus.publish(
            session_id,
            ASSISTANT_REQUEST_KIND,
            "observer",
            {
                "request_id": "req-1",
                "kind": "edit",
                "summary": "Aclara la definición",
                "text": "aclara la definición",
                "segment_ids": ["seg-1"],
                "t_start_ms": 1_000,
                "t_end_ms": 1_800,
                "detector": "observer",
            },
        )

    client.portal.call(publish)  # type: ignore[union-attr]
    _settle(client)

    names = _names(subscription.drain())
    assert names[-2:] == ["doubts.auto_resolved", "doubts.marked"]
    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    shown = client.post(f"{_base(topic)}/doubts/{queue.current}/ask")
    assert shown.status_code == 200, shown.text
    assert _names(subscription.drain()) == ["doubt.asked"]
    live = [
        (event.kind, event.origin, event.payload)
        for sid, event in read_topic_events(topic.vault, topic.subject, topic.topic)
        if sid == session_id
    ]
    doubts = [
        (kind, origin)
        for kind, origin, _p in live
        if kind in (STATE_OP_EVENT_KIND, PENDING_QUESTION_KIND)
    ]
    # Two add_pending + their questions, the review's close of p-1, then the one shown in chat.
    assert doubts.count((STATE_OP_EVENT_KIND, "editor")) == 3
    asked = [p for kind, _o, p in live if kind == PENDING_QUESTION_KIND and p["in_chat"]]
    assert len(asked) == 1
    assert not [
        meta
        for meta in list_sessions(topic.vault, topic.subject, topic.topic)
        if meta.kind == "review"
    ]
    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    assert queue.open_count == 2

    # Dismissing during the session works too (no `session_open`); the marks are announced.
    dismissed = client.post(f"{_base(topic)}/doubts/{asked[0]['pending_id']}/dismiss")
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["session_id"] == session_id
    _settle(client)
    assert _names(subscription.drain()) == ["doubt.resolved", "doubts.marked"]


def test_a_doubt_whose_pages_are_all_set_aside_is_not_marked(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    from studentassistant.vault import put_source

    put_source(
        topic.vault,
        topic.subject,
        topic.topic,
        "book",
        "page.jpg",
        b"\xff\xd8 blank",
        {"triage": {"status": "set_aside", "reasons": ["blank"]}},
    )
    subscription = _subscribe(client, topic)
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Añado una línea",
            "ops": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 2,
                    "text": "Es una tasa de cambio.[^p1]",
                }
            ],
            "doubts": [],
        },
        text="Añado una línea.",
    )
    _review_p1(fake, topic)
    response = client.post(f"{_base(topic)}/notes/chat", json={"message": "Añade una línea"})
    assert response.status_code == 200, response.text
    _settle(client)
    # Only p-1 was open and relevant; it was settled from the sources, so nothing is marked.
    events = subscription.drain()
    names = _names(events)
    assert "doubts.auto_resolved" in names and "doubt.asked" not in names
    assert events[-1].event == "doubts.marked" and events[-1].data == {"count": 0}


def test_off_nothing_is_reviewed_and_showing_one_reviews_it_first(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, topic: ReviseTopic
) -> None:
    from studentassistant.config import EditorSettings

    fake = FakeClaude()
    app: Callable[..., FastAPI] = create_app
    with TestClient(
        app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=topic.vault,
            llm_transport=fake,
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False),
                editor=EditorSettings(doubts_in_chat=False),
            ),
        ),
        base_url=LOCAL_BASE_URL,
        client=("127.0.0.1", 50000),
    ) as client:
        subscription = _subscribe(client, topic)
        _edit_with_doubts(fake)
        response = client.post(f"{_base(topic)}/notes/chat", json={"message": "Aclara"})
        assert response.status_code == 200, response.text
        _settle(client)
        assert "doubt.asked" not in _names(subscription.drain())
        assert len(fake.requests) == 1

        # p-1 has no question yet: showing it reviews it first, and the sources settle it.
        _review_p1(fake, topic)
        shown = client.post(f"{_base(topic)}/doubts/p-1/ask")
        assert shown.status_code == 200, shown.text
        body = shown.json()
        assert body["asked"] is False and body["status"] == "auto_resolved"
        assert body["summary"].startswith("He resuelto")
        names = _names(subscription.drain())
        assert names == ["doubts.auto_resolved", "doubts.marked"]
        assert len(fake.requests) == 2
