"""Vocabulary hints over the session socket (#54): `hello.ack`, `notice` and the server provider."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from ws_harness import WsHarness

from studentassistant.config import SttSettings
from studentassistant.protocol import model_for
from studentassistant.server.sessions import OpenSession
from studentassistant.server.vocabulary import SessionVocabulary, TopicTerms, load_topic_terms

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
SERVER_STT = SttSettings(mode="server", provider="fake", language="es")
NAMES = ["Física", "Cinemática"]


def conforms(name: str, body: Any) -> Any:
    schema = json.loads((PROTOCOL_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(body)
    model_for(name).model_validate(body)
    return body


def publish(socket: Any, ws: WsHarness, *args: Any, **kwargs: Any) -> None:
    socket.portal.call(functools.partial(ws.app.state.bus.publish, ws.session_id, *args, **kwargs))


def add_concept(socket: Any, ws: WsHarness, concept_id: str, name: str) -> None:
    op = {"op": "add_concept", "concept_id": concept_id, "name": name, "segment_ids": []}
    publish(socket, ws, "observer.state_op", "observer", op)


def observer_notice(socket: Any, ws: WsHarness, pending_count: int) -> None:
    publish(socket, ws, "notice", "observer", {"pending_count": pending_count}, persist=False)


def hello_ack(ws: WsHarness, socket: Any, version: str = "1.4") -> dict[str, Any]:
    socket.send_json(ws.hello(protocol_version=version))
    return conforms("server.hello.ack", socket.receive_json())


def test_a_1_4_client_gets_the_subject_and_topic_in_hello_ack(ws: WsHarness) -> None:
    with ws.connect() as socket:
        ack = hello_ack(ws, socket)
    assert ack["protocol_version"] == "1.4"
    assert ack["vocabulary_hints"] == NAMES


def test_an_older_client_gets_no_hints(ws: WsHarness) -> None:
    with ws.connect() as socket:
        ack = hello_ack(ws, socket, "1.3")
        add_concept(socket, ws, "c1", "velocidad media")
        observer_notice(socket, ws, 1)
        notice = conforms("server.notice", socket.receive_json())
    assert "vocabulary_hints" not in ack
    assert notice == {"type": "notice", "pending_count": 1, "server_time_ms": ws.clock.now}


def test_a_new_concept_is_sent_as_a_notice_with_the_pending_count(ws: WsHarness) -> None:
    with ws.connect() as socket:
        hello_ack(ws, socket)
        add_concept(socket, ws, "c1", "velocidad media")
        first = conforms("server.notice", socket.receive_json())
        add_concept(socket, ws, "c1", "velocidad media")  # already known: nothing new
        add_concept(socket, ws, "c2", "FÍSICA")  # the subject again: the hints do not change
        observer_notice(socket, ws, 2)
        second = conforms("server.notice", socket.receive_json())
        add_concept(socket, ws, "c3", "aceleración")
        third = conforms("server.notice", socket.receive_json())

    assert first == {
        "type": "notice",
        "pending_count": 0,
        "server_time_ms": ws.clock.now,
        "vocabulary_hints": [*NAMES, "velocidad media"],
    }
    # The observer's own notice goes through untouched: its hints were sent already.
    assert second == {"type": "notice", "pending_count": 2, "server_time_ms": ws.clock.now}
    # The newest concept comes first after the names; the count repeats the last one sent.
    assert third["pending_count"] == 2
    assert third["vocabulary_hints"] == [*NAMES, "aceleración", "velocidad media"]


def test_a_reconnecting_client_gets_the_concepts_already_folded(ws: WsHarness) -> None:
    with ws.connect() as socket:
        hello_ack(ws, socket)
        add_concept(socket, ws, "c1", "sufragio censitario")
        add_concept(socket, ws, "c2", "turno pacífico")
        socket.receive_json()
        socket.receive_json()
    with ws.connect() as socket:
        ack = hello_ack(ws, socket)
    assert ack["vocabulary_hints"] == [*NAMES, "turno pacífico", "sufragio censitario"]


@pytest.mark.parametrize("stt_settings", [SERVER_STT])
def test_the_server_provider_gets_the_hints_whatever_the_client_version(ws: WsHarness) -> None:
    with ws.connect() as socket:
        ack = hello_ack(ws, socket, "1.0")
        (provider,) = ws.providers
        assert provider.vocabulary == tuple(NAMES)
        add_concept(socket, ws, "c1", "caída libre")
        observer_notice(socket, ws, 0)  # delivered after the op: the op was handled by then
        socket.receive_json()
    assert "vocabulary_hints" not in ack
    assert provider.vocabulary == (*NAMES, "caída libre")


@pytest.mark.parametrize(
    "stt_settings", [SttSettings(mode="client", provider="web-speech", vocabulary_max_terms=0)]
)
def test_hints_turned_off_in_the_config_are_never_sent(ws: WsHarness) -> None:
    with ws.connect() as socket:
        ack = hello_ack(ws, socket)
        add_concept(socket, ws, "c1", "velocidad media")
        observer_notice(socket, ws, 1)
        notice = socket.receive_json()
    assert "vocabulary_hints" not in ack
    assert "vocabulary_hints" not in notice


def test_with_the_count_unknown_new_hints_ride_on_the_next_observer_notice(ws: WsHarness) -> None:
    async def no_observer_state(session: OpenSession) -> TopicTerms:
        return TopicTerms(subject="Física", topic="Cinemática")

    ws.app.state.gateway.terms_loader = no_observer_state
    with ws.connect() as socket:
        hello_ack(ws, socket)
        add_concept(socket, ws, "c1", "velocidad media")
        observer_notice(socket, ws, 3)
        notice = conforms("server.notice", socket.receive_json())
    assert notice["pending_count"] == 3
    assert notice["vocabulary_hints"] == [*NAMES, "velocidad media"]


def test_unreadable_terms_cost_only_the_hints(ws: WsHarness) -> None:
    async def broken(session: OpenSession) -> TopicTerms:
        return await ws.app.state.gateway._load_terms(
            OpenSession("missing", "nope", "nada", session.started_at)
        )

    ws.app.state.gateway.terms_loader = broken
    with ws.connect() as socket:
        ack = hello_ack(ws, socket)
    assert ack["stt_mode"] == "client"
    assert "vocabulary_hints" not in ack


# --- the terms --------------------------------------------------------------------------------


def test_load_topic_terms_reads_names_and_leaves_out_what_is_missing(ws: WsHarness) -> None:
    terms = load_topic_terms(ws.vault, "fisica", "cinematica")
    assert (terms.subject, terms.topic, terms.concepts, terms.pending_count) == (
        "Física",
        "Cinemática",
        {},
        0,
    )
    # Each part is read on its own: the subject's name survives a missing topic.
    missing = load_topic_terms(ws.vault, "fisica", "nada")
    assert (missing.subject, missing.topic, missing.concepts) == ("Física", None, {})
    assert missing.pending_count is None


def test_session_vocabulary_ignores_other_ops_and_bad_payloads() -> None:
    vocabulary = SessionVocabulary(SttSettings(), TopicTerms(subject="S", topic="T"))
    assert vocabulary.hints == ["S", "T"]
    for payload in (
        {"op": "note", "text": "x"},
        {"op": "add_concept", "concept_id": "c", "name": "  "},
        {"op": "add_concept", "concept_id": 3, "name": "x"},
        {"op": "add_concept", "name": "x"},
    ):
        assert not vocabulary.apply_state_op(payload)
    assert vocabulary.apply_state_op({"op": "add_concept", "concept_id": "c", "name": "límite"})
    assert vocabulary.hints == ["S", "T", "límite"]
