"""The flashcards generator (kind `flashcards`): question/answer cards and an Anki deck.

Claude (role `generator`, prompt `generator_flashcards`) turns the master notes into at most
`size` cards, each a Spanish question and its answer with the note anchors it came from. Three
files are written under `generated/`:

- `flashcards.yaml` -- the cards (`FlashcardsFile`): the deck's name and id and every card's
  stable `id`, front, back and anchors. It is also what the next generation reads back.
- `flashcards.csv` -- one row per card (`id,anverso,reverso,secciones`), UTF-8, for a spreadsheet
  or any flashcards app.
- `flashcards.apkg` -- an Anki package (genanki): one deck per topic, one note per card.

Re-exporting updates rather than duplicates: the deck id is derived from the subject and topic,
the note type id is fixed, and every note's Anki GUID is derived from the topic and the card's
`id`. A card keeps its `id` across generations: the previous cards are shown to Claude, which
gives a card asking the same thing the old `id`; failing that, a card whose front reads the same
as an old one (case, spacing and punctuation aside) takes its `id`; a new card gets an `id` from
its front's hash. Importing the new `.apkg` into Anki then updates the existing notes (keeping
their review history) and adds only the new ones.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import io
import itertools
import logging
import re
import sqlite3
import unicodedata
import zipfile
from datetime import UTC, datetime
from typing import Any

import genanki
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.generators.base import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
    NoteSection,
)
from studentassistant.generators.registry import register
from studentassistant.llm import load_prompt
from studentassistant.vault import VaultError, get_subject, read_generated

logger = logging.getLogger(__name__)

KIND = "flashcards"
PROMPT_NAME = "generator_flashcards"
TOOL_NAME = "record_flashcards"
YAML_NAME = f"{KIND}.yaml"
CSV_NAME = f"{KIND}.csv"
APKG_NAME = f"{KIND}.apkg"
CSV_HEADER = ("id", "anverso", "reverso", "secciones")

ANKI_MODEL_ID = 1_718_224_457
"""The Anki note type of every deck; fixed so that re-imports match the same note type."""
ANKI_MODEL_NAME = "Student Assistant (anverso/reverso)"
_ANKI_CSS = """\
.card { font-family: sans-serif; font-size: 20px; text-align: center; color: black;
  background-color: white; }
.apuntes { margin-top: 1em; font-size: 14px; color: #666; }
"""
ANKI_MODEL = genanki.Model(
    ANKI_MODEL_ID,
    ANKI_MODEL_NAME,
    fields=[{"name": "Anverso"}, {"name": "Reverso"}, {"name": "Apuntes"}],
    templates=[
        {
            "name": "Tarjeta",
            "qfmt": "{{Anverso}}",
            "afmt": (
                '{{FrontSide}}<hr id="answer">{{Reverso}}'
                '{{#Apuntes}}<div class="apuntes">{{Apuntes}}</div>{{/Apuntes}}'
            ),
        }
    ],
    css=_ANKI_CSS,
)

_ID_PATTERN = r"^c[0-9a-f]{8}(-[0-9]+)?$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlashcardsOptions(_Strict):
    size: int = Field(default=20, ge=1, le=100, description="Cuántas tarjetas, como mucho.")


class DraftCard(_Strict):
    """One card as Claude records it."""

    front: str = Field(min_length=1, description="The question, in Spanish.")
    back: str = Field(min_length=1, description="The answer, in Spanish.")
    anchors: list[str] = Field(
        default_factory=list, description="Anchors (no `#`) of the note sections it comes from."
    )
    id: str | None = Field(
        default=None,
        description="The `id` of the earlier card that asks the same thing; none for a new card.",
    )


class DraftDeck(_Strict):
    cards: list[DraftCard]


class Flashcard(_Strict):
    """One stored card."""

    id: str = Field(pattern=_ID_PATTERN)
    front: str
    back: str
    anchors: list[str] = Field(default_factory=list)


class FlashcardsFile(_Strict):
    """`generated/flashcards.yaml`."""

    deck: str
    deck_id: int
    cards: list[Flashcard]


def deck_id_for(subject: str, topic: str) -> int:
    """The topic's Anki deck id: stable, in the range Anki's own ids take (`2**30..2**31`)."""
    digest = hashlib.sha256(f"studentassistant/{subject}/{topic}".encode()).digest()
    return (1 << 30) | (int.from_bytes(digest[:4], "big") & ((1 << 30) - 1))


def guid_for(subject: str, topic: str, card_id: str) -> str:
    """The Anki GUID of a card's note: the same for the same topic and card id."""
    return genanki.guid_for("studentassistant", subject, topic, card_id)


def _normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w$\\^_{}+\-*/=<>()]+", " ", letters).split())


def _new_id(front: str, taken: set[str]) -> str:
    base = "c" + hashlib.sha256(_normalized(front).encode()).hexdigest()[:8]
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def assign_ids(drafts: list[DraftCard], previous: list[Flashcard]) -> list[Flashcard]:
    """The drafts as stored cards, each with a stable id (see the module docstring).

    An id Claude gives is kept when it is an earlier card's and not given twice; otherwise a card
    whose normalized front equals an earlier card's takes that id; otherwise a new one.
    """
    previous_ids = {card.id for card in previous}
    by_front = {_normalized(card.front): card.id for card in previous}
    taken: set[str] = set()
    ids: list[str | None] = []
    for draft in drafts:  # first pass: the ids Claude reused, then equal fronts
        chosen = draft.id if draft.id in previous_ids and draft.id not in taken else None
        if chosen is None:
            match = by_front.get(_normalized(draft.front))
            chosen = match if match is not None and match not in taken else None
        if chosen is not None:
            taken.add(chosen)
        ids.append(chosen)
    cards: list[Flashcard] = []
    for draft, chosen in zip(drafts, ids, strict=True):
        if chosen is None:
            chosen = _new_id(draft.front, taken | previous_ids)
            taken.add(chosen)
        cards.append(
            Flashcard(
                id=chosen,
                front=draft.front.strip(),
                back=draft.back.strip(),
                anchors=[anchor.lstrip("#") for anchor in draft.anchors],
            )
        )
    return cards


def parse_flashcards(data: bytes) -> FlashcardsFile:
    """`flashcards.yaml` as stored; `ValueError` when it is not one."""
    try:
        return FlashcardsFile.model_validate(yaml.safe_load(data.decode("utf-8")))
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError) as error:
        raise ValueError(f"not a flashcards file: {error}") from error


def render_yaml(deck: FlashcardsFile) -> str:
    return yaml.safe_dump(
        deck.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=100
    )


def _section_titles(card: Flashcard, sections: dict[str, NoteSection]) -> list[str]:
    return [sections[anchor].title if anchor in sections else anchor for anchor in card.anchors]


def render_csv(deck: FlashcardsFile, sections: dict[str, NoteSection]) -> str:
    """One row per card after a header row; the sections joined with `; `."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for card in deck.cards:
        titles = "; ".join(_section_titles(card, sections))
        writer.writerow([card.id, card.front, card.back, titles])
    return buffer.getvalue()


_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def anki_html(text: str) -> str:
    """A card's text as the HTML of an Anki field: escaped, `$$..$$`/`$..$` as MathJax `\\[..\\]`
    and `\\(..\\)`, `**bold**` as `<b>` and line breaks as `<br>`."""
    escaped = html.escape(text.strip(), quote=False)
    escaped = _DISPLAY_MATH.sub(lambda m: r"\[" + m.group(1).strip() + r"\]", escaped)
    escaped = _INLINE_MATH.sub(lambda m: r"\(" + m.group(1) + r"\)", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    return escaped.replace("\n", "<br>")


def _anki_tag(value: str) -> str:
    return re.sub(r"\s+", "_", value.strip()) or "tema"


def render_apkg(
    deck: FlashcardsFile,
    subject: str,
    topic: str,
    sections: dict[str, NoteSection],
    *,
    timestamp: float,
) -> bytes:
    """The Anki package of the deck (`collection.anki2` built in memory, zipped)."""
    anki_deck = genanki.Deck(deck.deck_id, deck.deck)
    tags = [_anki_tag(subject), _anki_tag(topic)]
    for card in deck.cards:
        sources = ", ".join(_section_titles(card, sections))
        anki_deck.add_note(
            genanki.Note(
                model=ANKI_MODEL,
                fields=[
                    anki_html(card.front),
                    anki_html(card.back),
                    html.escape(f"Apuntes: {sources}", quote=False) if sources else "",
                ],
                tags=tags,
                guid=guid_for(subject, topic, card.id),
            )
        )
    package = genanki.Package(anki_deck)
    connection = sqlite3.connect(":memory:")
    try:
        cursor = connection.cursor()
        package.write_to_db(cursor, timestamp, _ids_from(timestamp))
        connection.commit()
        database = connection.serialize()
    finally:
        connection.close()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("collection.anki2", database)
        archive.writestr("media", "{}")
    return buffer.getvalue()


def _ids_from(timestamp: float) -> itertools.count[int]:
    return itertools.count(int(timestamp * 1000))


def _previous_block(previous: list[Flashcard]) -> dict[str, Any]:
    lines = [f"- id `{card.id}`: {card.front}" for card in previous]
    heading = "Tarjetas de la versión anterior (reutiliza su `id` si preguntas lo mismo):"
    text = heading + "\n" + "\n".join(lines)
    return {"type": "text", "text": text}


@register
class FlashcardsGenerator(Generator):
    kind = KIND
    title = "Flashcards"
    description = "Tarjetas de pregunta y respuesta para estudiar, con un mazo de Anki y un CSV."
    version = 1
    options_model = FlashcardsOptions

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        options = context.options
        assert isinstance(options, FlashcardsOptions)
        prompt = load_prompt(PROMPT_NAME)
        previous = await asyncio.to_thread(_read_previous, context)
        content: list[dict[str, Any]] = [context.notes_block()]
        if previous:
            content.append(_previous_block(previous))
        content.append(
            {"type": "text", "text": f"Haz como mucho {options.size} tarjetas de estos apuntes."}
        )
        result = await context.structured(
            [{"role": "user", "content": content}],
            DraftDeck,
            tool_name=TOOL_NAME,
            tool_description="Record the flashcards made from the notes.",
            system=prompt.content,
            prompt_hash=prompt.hash,
        )
        drafts = result.value.cards
        warnings: list[str] = []
        if len(drafts) > options.size:
            warnings.append(
                f"Claude ha propuesto {len(drafts)} tarjetas; "
                f"se guardan las {options.size} primeras."
            )
            drafts = drafts[: options.size]
        if not drafts:
            warnings.append("Claude no ha propuesto ninguna tarjeta: el mazo está vacío.")
        cards = assign_ids(drafts, previous)
        deck = FlashcardsFile(
            deck=await asyncio.to_thread(_deck_name, context),
            deck_id=deck_id_for(context.subject, context.topic),
            cards=cards,
        )
        sections = {section.anchor: section for section in context.sections}
        now = context.clock() if context.clock is not None else datetime.now(UTC)
        return GeneratorOutput(
            files={
                YAML_NAME: render_yaml(deck),
                CSV_NAME: render_csv(deck, sections),
                APKG_NAME: render_apkg(
                    deck, context.subject, context.topic, sections, timestamp=now.timestamp()
                ),
            },
            items=[ItemProvenance(item=card.id, anchors=card.anchors) for card in cards],
            warnings=warnings,
            model=result.responses[-1].model,
            prompt_hash=prompt.hash,
        )


def _read_previous(context: GeneratorContext) -> list[Flashcard]:
    """The cards of the topic's previous `flashcards.yaml`; none when missing or unreadable."""
    try:
        data = read_generated(context.vault, context.subject, context.topic, YAML_NAME)
    except VaultError:
        logger.exception("could not read the previous flashcards of %s", context.topic)
        return []
    if data is None:
        return []
    try:
        return parse_flashcards(data).cards
    except ValueError:
        logger.warning(
            "the previous flashcards of %s/%s are unreadable; ignored",
            context.subject,
            context.topic,
        )
        return []


def _deck_name(context: GeneratorContext) -> str:
    """`<subject name>::<topic title>` (an Anki subdeck of the subject)."""
    try:
        subject = get_subject(context.vault, context.subject).subject.name
    except VaultError:
        subject = context.subject
    return f"{subject.replace('::', ':')}::{context.topic_title.replace('::', ':')}"
