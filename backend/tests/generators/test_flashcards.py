"""The flashcards generator: cards, CSV, the Anki package and ids that survive a regeneration."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import zipfile
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.generators import (
    GenerateResult,
    GeneratorRegistry,
    default_registry,
    read_artifact_meta,
    run_generator,
)
from studentassistant.generators.flashcards import (
    ANKI_MODEL_ID,
    APKG_NAME,
    CSV_NAME,
    KIND,
    TOOL_NAME,
    YAML_NAME,
    DraftCard,
    Flashcard,
    FlashcardsGenerator,
    anki_html,
    assign_ids,
    deck_id_for,
    guid_for,
)
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, generated_directory

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)

CARDS: list[dict[str, Any]] = [
    {
        "front": "¿Qué es la derivada de $f$ en $a$?",
        "back": "El límite $$\\lim_{h\\to 0} \\frac{f(a+h)-f(a)}{h}$$",
        "anchors": ["definicion"],
    },
    {
        "front": "¿Qué se verá el próximo día?",
        "back": "La **regla de la cadena**.",
        "anchors": ["#proximo-dia"],
    },
]


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


def _registry() -> GeneratorRegistry:
    registry = GeneratorRegistry()
    registry.register(FlashcardsGenerator)
    return registry


def _generate(
    topic: ReviseTopic, fake: FakeClaude, cards: list[dict[str, Any]], **options: object
) -> GenerateResult:
    fake.reply_tool(TOOL_NAME, {"cards": cards})
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    return _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            KIND,
            client=client,
            sync=GitSync(topic.vault),
            registry=_registry(),
            options=options,
            clock=lambda: NOW,
        )
    )


def _file(topic: ReviseTopic, name: str) -> bytes:
    return (generated_directory(topic.vault, topic.subject, topic.topic) / name).read_bytes()


def _stored(topic: ReviseTopic) -> dict[str, Any]:
    return yaml.safe_load(_file(topic, YAML_NAME).decode("utf-8"))


def _anki(topic: ReviseTopic, tmp_path: Any) -> sqlite3.Connection:
    with zipfile.ZipFile(io.BytesIO(_file(topic, APKG_NAME))) as archive:
        assert set(archive.namelist()) == {"collection.anki2", "media"}
        database = archive.read("collection.anki2")
    path = tmp_path / "collection.anki2"
    path.write_bytes(database)
    return sqlite3.connect(path)


def test_it_is_a_registered_generator() -> None:
    assert KIND in default_registry
    assert default_registry.lookup(KIND) is FlashcardsGenerator


def test_writes_the_cards_the_csv_and_an_anki_deck(
    topic: ReviseTopic, fake: FakeClaude, tmp_path: Any
) -> None:
    result = _generate(topic, fake, CARDS)

    assert result.items == 2 and not result.unresolved
    # The second card's back goes beyond the fixture's notes: reported, kept.
    [ungrounded] = result.ungrounded
    assert ungrounded.anchors == ["definicion"] and ungrounded.support < 0.6
    assert result.warnings == [
        f"1 elemento no se apoya claramente en los apuntes: {ungrounded.item} (25 %, #definicion)."
        " Revísalos antes de estudiar con ellos."
    ]
    names = sorted(path.rsplit("/", 1)[-1] for path in result.files)
    assert names == sorted([APKG_NAME, CSV_NAME, YAML_NAME, f"{KIND}.meta.yaml"])

    stored = _stored(topic)
    assert stored["deck"] == "Matemáticas::Derivadas"
    assert stored["deck_id"] == deck_id_for(topic.subject, topic.topic)
    ids = [card["id"] for card in stored["cards"]]
    assert len(set(ids)) == 2
    assert [card["anchors"] for card in stored["cards"]] == [["definicion"], ["proximo-dia"]]

    rows = list(csv.reader(io.StringIO(_file(topic, CSV_NAME).decode("utf-8"))))
    assert rows[0] == ["id", "anverso", "reverso", "secciones"]
    assert rows[1][:2] == [ids[0], CARDS[0]["front"]]
    assert rows[2][3] == "2. Próximo día"

    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None
    assert [item.item for item in meta.items] == ids

    with _anki(topic, tmp_path) as db:
        notes = db.execute("SELECT guid, mid, flds, tags FROM notes ORDER BY id").fetchall()
        decks = json.loads(db.execute("SELECT decks FROM col").fetchone()[0])
    assert [guid for guid, *_ in notes] == [guid_for(topic.subject, topic.topic, i) for i in ids]
    assert {mid for _, mid, *_ in notes} == {ANKI_MODEL_ID}
    front, back, source = notes[0][2].split("\x1f")
    assert front == r"¿Qué es la derivada de \(f\) en \(a\)?"
    assert back.startswith(r"El límite \[\lim_{h\to 0}")
    assert source == "Apuntes: 1. Definición"
    assert "<b>regla de la cadena</b>" in notes[1][2]
    deck = decks[str(deck_id_for(topic.subject, topic.topic))]
    assert deck["name"] == "Matemáticas::Derivadas"


def test_a_regeneration_keeps_the_ids_so_anki_updates_the_notes(
    topic: ReviseTopic, fake: FakeClaude, tmp_path: Any
) -> None:
    _generate(topic, fake, CARDS)
    first = [card["id"] for card in _stored(topic)["cards"]]

    again = [
        # Claude says it is the same card, reworded.
        {**CARDS[0], "front": "Define la derivada de f en a.", "id": first[0]},
        # Claude forgot the id, but the front reads the same.
        {**CARDS[1], "front": "¿Qué  se verá el PRÓXIMO día", "back": "La regla de la cadena."},
        {"front": "¿Cómo se escribe la derivada?", "back": "$f'(x)$", "anchors": ["definicion"]},
    ]
    _generate(topic, fake, again)

    second = [card["id"] for card in _stored(topic)["cards"]]
    assert second[:2] == first and second[2] not in first
    with _anki(topic, tmp_path) as db:
        guids = [row[0] for row in db.execute("SELECT guid FROM notes ORDER BY id")]
    assert guids[:2] == [guid_for(topic.subject, topic.topic, i) for i in first]
    # The previous cards were shown to Claude with their ids.
    shown = str(fake.requests[-1].messages)
    assert first[0] in shown and first[1] in shown


def test_ids_claude_invents_or_repeats_are_not_trusted() -> None:
    previous = [Flashcard(id="c00000001", front="¿A?", back="a")]
    drafts = [
        DraftCard(front="¿B?", back="b", anchors=["x"], id="c00000001"),
        DraftCard(front="¿C?", back="c", anchors=["x"], id="c00000001"),
        DraftCard(front="¿D?", back="d", anchors=["x"], id="c99999999"),
        DraftCard(front="¿D?", back="d otra vez", anchors=["x"]),
    ]
    ids = [card.id for card in assign_ids(drafts, previous)]
    assert ids[0] == "c00000001"
    assert len(set(ids)) == 4 and "c99999999" not in ids
    assert ids[3] == ids[2] + "-2"


def test_the_size_option_caps_the_cards(topic: ReviseTopic, fake: FakeClaude) -> None:
    result = _generate(topic, fake, CARDS, size=1)
    assert result.items == 1
    assert len(_stored(topic)["cards"]) == 1
    assert any("se guardan las 1 primeras" in warning for warning in result.warnings)
    assert "como mucho 1 tarjetas" in str(fake.requests[-1].messages)


def test_an_unknown_anchor_is_reported(topic: ReviseTopic, fake: FakeClaude) -> None:
    result = _generate(topic, fake, [{**CARDS[0], "anchors": ["no-existe"]}])
    assert [entry.item for entry in result.unresolved] == [_stored(topic)["cards"][0]["id"]]


def test_anki_html_escapes_and_converts_math() -> None:
    assert anki_html("a < b & **c**\nd") == "a &lt; b &amp; <b>c</b><br>d"
    assert anki_html("$x^2$ y $$\\int f$$") == r"\(x^2\) y \[\int f\]"
    assert anki_html("cuesta 5$ o 6$") == "cuesta 5$ o 6$"
