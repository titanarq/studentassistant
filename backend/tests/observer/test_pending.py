"""The pending-review queue (#55): the item model, deduplication in the fold, the review file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from studentassistant.observer import (
    STATE_VERSION,
    AddPending,
    DuplicateIdError,
    PendingAlreadyResolvedError,
    PendingRefs,
    ResolvePending,
    TopicEvent,
    fold,
    load_observer_snapshot,
    parse_op,
    pending_review,
    text_similarity,
)
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    end_session,
    pending_review_path,
    start_session,
)
from studentassistant.vault.index import VaultIndex

from .script import CAPTURE_1, CAPTURE_2, FIRST_SESSION, ScriptedEvent, capture, op, segment
from .script import to_topic_events as scripted


def events(*items: ScriptedEvent) -> list[TopicEvent]:
    """One session: two segments and two captures registered, then `items`."""
    base = [segment("s-1"), segment("s-2"), capture(CAPTURE_1), capture(CAPTURE_2)]
    return scripted([(FIRST_SESSION, [*base, *items])])


def add(pending_id: str, kind: str, text: str, origin: Any = "observer", **refs: Any) -> Any:
    return op("add_pending", origin=origin, pending_id=pending_id, kind=kind, text=text, **refs)


# -- the model ------------------------------------------------------------------------------------


def test_an_item_has_kind_text_refs_creator_and_status() -> None:
    state = fold(
        events(
            add(
                "p-1",
                "contradiction",
                "Los apuntes dan 46 cromosomas y el libro 23 pares",
                segment_ids=["s-1"],
                capture_ids=[CAPTURE_1],
                source_refs=["libro p. 112"],
            )
        )
    )
    item = state.pending["p-1"]
    assert item.kind == "contradiction"
    assert item.text.startswith("Los apuntes")
    assert item.refs == PendingRefs(pages=[CAPTURE_1], segments=["s-1"], sources=["libro p. 112"])
    assert item.created_by == "observer"
    assert item.status == "open" and item.is_open
    assert item.resolution is None and item.merged_ids == []


def test_legacy_category_and_description_are_read_as_kind_and_text() -> None:
    parsed = parse_op(
        {"op": "add_pending", "pending_id": "p", "category": "illegible", "description": "x"}
    )
    assert isinstance(parsed, AddPending)
    assert (parsed.kind, parsed.text) == ("illegible", "x")
    assert parsed.model_dump()["kind"] == "illegible"


def test_created_by_is_the_origin_of_the_event() -> None:
    state = fold(events(add("p-1", "incomplete", "Falta el final de la lista", origin="user")))
    assert state.pending["p-1"].created_by == "user"


@pytest.mark.parametrize(
    ("origin", "status", "expected"),
    [
        ("observer", None, "auto_resolved"),
        ("user", None, "resolved"),
        ("user", "dismissed", "dismissed"),
        ("observer", "resolved", "resolved"),
    ],
)
def test_resolving_sets_the_status(origin: Any, status: str | None, expected: str) -> None:
    fields: dict[str, Any] = {"pending_id": "p-1"}
    if status is not None:
        fields["status"] = status
    if status != "dismissed":
        fields["resolution"] = "Se aclara después"
    state = fold(
        events(add("p-1", "illegible", "No se lee"), op("resolve_pending", origin=origin, **fields))
    )
    item = state.pending["p-1"]
    assert item.status == expected and not item.is_open
    assert state.open_pending() == [] and state.resolved_pending() == [item]


def test_only_a_dismissal_may_go_without_a_resolution() -> None:
    with pytest.raises(ValidationError):
        ResolvePending(pending_id="p-1")
    assert ResolvePending(pending_id="p-1", status="dismissed").resolution is None


def test_a_closed_item_cannot_be_closed_again() -> None:
    with pytest.raises(PendingAlreadyResolvedError):
        fold(
            events(
                add("p-1", "illegible", "No se lee"),
                op("resolve_pending", pending_id="p-1", status="dismissed"),
                op("resolve_pending", pending_id="p-1", resolution="ya"),
            )
        )


# -- deduplication --------------------------------------------------------------------------------


def test_text_similarity_ignores_case_accents_and_punctuation() -> None:
    assert text_similarity("Fórmula ilegible.", "formula ILEGIBLE") == 1.0
    assert text_similarity("mitosis", "") == 0.0
    assert text_similarity("", "") == 1.0
    assert text_similarity("la palabra tras fase", "fase tras la palabra") == 1.0
    assert text_similarity("No se lee la fórmula", "Falta la definición de límite") < 0.5


def test_the_same_doubt_again_is_merged_into_the_open_item() -> None:
    state = fold(
        events(
            add(
                "p-1",
                "illegible",
                "No se lee la palabra junto a la fórmula",
                capture_ids=[CAPTURE_1],
            ),
            add(
                "p-2",
                "illegible",
                "No se lee la palabra que está junto a la fórmula",
                capture_ids=[CAPTURE_2],
                segment_ids=["s-2"],
            ),
        )
    )
    assert list(state.pending) == ["p-1"]
    item = state.pending["p-1"]
    assert item.merged_ids == ["p-2"]
    assert item.refs.pages == [CAPTURE_1, CAPTURE_2] and item.refs.segments == ["s-2"]
    assert item.text == "No se lee la palabra junto a la fórmula"  # the first text is kept
    assert state.pending_aliases == {"p-2": "p-1"}
    assert state.pending_item("p-2") is item


def test_overlapping_refs_merge_a_looser_match() -> None:
    first = "Se nombra el huso acromático sin explicarlo"
    second = "El huso acromático no se explica"
    assert 0.5 <= text_similarity(first, second) < 0.85
    shared = fold(
        events(
            add("p-1", "unexplained_concept", first, segment_ids=["s-1"]),
            add("p-2", "unexplained_concept", second, segment_ids=["s-1", "s-2"]),
        )
    )
    assert list(shared.pending) == ["p-1"]
    apart = fold(
        events(
            add("p-1", "unexplained_concept", first, segment_ids=["s-1"]),
            add("p-2", "unexplained_concept", second, segment_ids=["s-2"]),
        )
    )
    assert list(apart.pending) == ["p-1", "p-2"]


def test_different_kinds_different_numbers_or_different_doubts_stay_apart() -> None:
    state = fold(
        events(
            add("p-1", "illegible", "No se lee la fórmula 3", capture_ids=[CAPTURE_1]),
            add("p-2", "incomplete", "No se lee la fórmula 3", capture_ids=[CAPTURE_1]),
            add("p-3", "illegible", "No se lee la fórmula 4", capture_ids=[CAPTURE_1]),
            add(
                "p-4", "illegible", "Subíndice borroso en la segunda línea", capture_ids=[CAPTURE_1]
            ),
        )
    )
    assert list(state.pending) == ["p-1", "p-2", "p-3", "p-4"]


def test_a_closed_item_is_never_merged_into() -> None:
    state = fold(
        events(
            add("p-1", "illegible", "No se lee la palabra"),
            op("resolve_pending", pending_id="p-1", resolution="Dice «fase»"),
            add("p-2", "illegible", "No se lee la palabra"),
        )
    )
    assert list(state.pending) == ["p-1", "p-2"]
    assert [item.id for item in state.open_pending()] == ["p-2"]


def test_resolving_a_merged_id_closes_the_item_it_joined() -> None:
    state = fold(
        events(
            add("p-1", "illegible", "No se lee la palabra"),
            add("p-2", "illegible", "No se lee la palabra"),
            op("resolve_pending", origin="user", pending_id="p-2", resolution="Dice «fase»"),
        )
    )
    assert state.pending["p-1"].status == "resolved"
    assert state.open_pending() == []


def test_a_merged_id_cannot_be_reused() -> None:
    with pytest.raises(DuplicateIdError):
        fold(
            events(
                add("p-1", "illegible", "No se lee la palabra"),
                add("p-2", "illegible", "No se lee la palabra"),
                add("p-2", "incomplete", "Falta algo distinto"),
            )
        )


# -- the review file ------------------------------------------------------------------------------


def test_pending_review_lists_open_items_first() -> None:
    state = fold(
        events(
            add("p-1", "illegible", "No se lee la palabra"),
            add("p-2", "incomplete", "Falta el final de la lista"),
            op("resolve_pending", pending_id="p-1", resolution="Dice «fase»"),
        )
    )
    review = pending_review(state)
    assert review.open_count == 1
    assert [item.id for item in review.items] == ["p-2", "p-1"]


def test_the_loader_regenerates_review_pending_yaml(tmp_vault: Vault, tmp_path: Path) -> None:
    subject = create_subject(tmp_vault, "Biología").slug
    topic = create_topic(tmp_vault, subject, "La célula").slug
    session = start_session(tmp_vault, subject, topic, "pc", "1.0")
    session.append_event("transcript.final", "stt", {"segment_id": "s-1", "text": "hola"})
    session.append_event(
        "observer.state_op",
        "observer",
        {"op": "add_pending", "pending_id": "p-1", "kind": "illegible", "text": "No se lee"},
    )
    end_session(session)
    path = pending_review_path(tmp_vault, subject, topic)
    load_observer_snapshot(tmp_vault, subject, topic, write_back=False)
    assert not path.exists()

    snapshot = load_observer_snapshot(tmp_vault, subject, topic)
    assert snapshot.state_version == STATE_VERSION
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["open_count"] == 1
    assert [(item["id"], item["kind"], item["status"]) for item in data["items"]] == [
        ("p-1", "illegible", "open")
    ]
    # The derived index reads the file the observer wrote.
    with VaultIndex.open(tmp_vault, tmp_path / "index.sqlite") as index:
        index.rebuild()
        assert [entry.item["id"] for entry in index.pending(subject, topic)] == ["p-1"]
