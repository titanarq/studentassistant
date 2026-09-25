"""Checking a generated item against the note sections it cites, deterministically, without Claude.

A generator gives, besides each item's provenance, the text of the item that must come from the
notes (`GeneratorOutput.item_texts`: a quiz question's answer and explanation, a flashcard's back,
an exam solution, a slide's bullets, an outline node's gloss). `support(text, sections)` is the
share of that text's *content words* found in the text of the sections it cites; the framework
(`run.py`) reports every item under `[generators] grounding_min_support` as `ungrounded` in the
manifest and in one Spanish warning. Items are never dropped.

Words are compared as the eval rubric compares them (`evals/scoring.py`, same idea, kept apart so
the generators do not import the eval harness): unreadable-word marks become their word, footnote
references, heading anchors, link targets and punctuation go, accents are folded, everything is
lower-cased; content words are the words of three letters or more that are not Spanish stop words
(numbers always count). A few words every generated item uses about itself ("respuesta",
"correcta", "apuntes"...) or to ask for work ("calcula", "justifica"...) are not content either.

A cited section's text is its heading and blocks plus those of its subsections (the deeper
headings that follow it), since citing `#tema` cites what is under it; footnote definitions (the
provenance links) are left out.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.config import DEFAULT_GROUNDING_MIN_SUPPORT
from studentassistant.editor.notes_format import NotesDocument, Section

__all__ = [
    "DEFAULT_GROUNDING_MIN_SUPPORT",
    "UngroundedItem",
    "content_words",
    "find_ungrounded",
    "section_text",
    "support",
    "ungrounded_warning",
]

_UNREADABLE = re.compile(r"\[\[\?([^\]]*)\]\]")
_FOOTNOTE_REF = re.compile(r"\[\^[^\]\s]+\]")
_ANCHOR = re.compile(r"\{#[^}]*\}")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_NOT_WORD = re.compile(r"[^\w\s]|_")
_WORD = re.compile(r"\w+")

_STOPWORDS_TEXT = """
ante bajo cabe con contra desde durante entre hacia hasta mediante para por segun sin
sobre tras los las del una unos unas uno que como cuando donde quien cual cuales cuyo pero
sino porque pues aunque mas muy tan tanto tambien tampoco solo ya aun asi este esta estos
estas ese esa esos esas aquel aquella esto eso ello ella ellos ellas nos vos usted ustedes
mis tus sus nuestro nuestra vuestro vuestra ser son era eran sera fue fueron sido estar
estan estaba hay haber has han habia tiene tienen tener hace hacen puede pueden todo toda
todos todas otro otra otros otras mismo misma algo nada cada vez veces bien mal aqui ahi
alli entonces luego
"""
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())
"""The eval rubric's Spanish stop words (accents folded)."""

_META_WORDS_TEXT = """
respuesta respuestas correcta correcto correctas correctos incorrecta incorrecto
incorrectas incorrectos verdadero verdadera falso falsa opcion opciones apuntes
seccion secciones pregunta preguntas enunciado ejercicio ejercicios paso pasos resultado
calcula calcular define definir explica explicar demuestra demostrar justifica justificar
razona razonar plantea plantear escribe escribir indica indicar halla hallar obten obtener
resuelve resolver nombra nombrar toma tomar aplica aplicar identifica identificar describe
describir compara comparar comprueba comprobar
"""
META_WORDS = frozenset(_META_WORDS_TEXT.split())
"""Words a generated item says about itself or asks the student to do (an exam statement's or
rubric's "calcula", "justifica"), not about the topic: never content."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UngroundedItem(_Strict):
    """An item whose text the note sections it cites do not clearly hold (manifest `ungrounded`)."""

    item: str = Field(min_length=1, description="The generator's id of the item.")
    support: float = Field(ge=0, le=1, description="Share of its content words in the sections.")
    anchors: list[str] = Field(
        default_factory=list, description="The cited anchors the notes have, no `#`."
    )


def _normalize(text: str) -> str:
    text = _UNREADABLE.sub(r"\1", text)
    text = _FOOTNOTE_REF.sub(" ", text)
    text = _ANCHOR.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(_NOT_WORD.sub(" ", folded.lower()).split())


def content_words(text: str) -> set[str]:
    """The words of `text` that carry content (see the module docstring)."""
    return {
        word
        for word in _WORD.findall(_normalize(text))
        if word.isdigit() or (len(word) >= 3 and word not in STOPWORDS and word not in META_WORDS)
    }


def support(text: str, sections: Iterable[str]) -> float:
    """The share of `text`'s content words found in `sections` (in [0, 1], rounded to 4).

    A text with no content word (empty, or only stop words) claims nothing: its support is 1.
    """
    wanted = content_words(text)
    if not wanted:
        return 1.0
    found: set[str] = set()
    for section in sections:
        found |= content_words(section)
    return round(len(wanted & found) / len(wanted), 4)


def _content(section: Section) -> str:
    """A section's heading title and blocks, without footnote definitions (provenance links)."""
    blocks = "".join(block.render() for block in section.blocks if block.kind != "footnotes")
    return f"{section.heading.title}\n{blocks}"


def section_text(notes: NotesDocument, anchor: str) -> str | None:
    """The text of the section `{#anchor}` and its subsections; `None` when the notes lack it."""
    for index, section in enumerate(notes.sections):
        if section.anchor != anchor:
            continue
        parts = [_content(section)]
        for deeper in notes.sections[index + 1 :]:
            if deeper.heading.level <= section.heading.level:
                break
            parts.append(_content(deeper))
        return "\n".join(parts)
    return None


def find_ungrounded(
    notes: NotesDocument,
    items: Iterable[tuple[str, list[str]]],
    texts: Mapping[str, str],
    min_support: float = DEFAULT_GROUNDING_MIN_SUPPORT,
) -> list[UngroundedItem]:
    """The items (`(id, anchors)`, in order) whose text in `texts` is under `min_support`.

    An item with no text in `texts` is not checked; one with no anchor the notes have is left to
    the framework's `unresolved` report and not scored (its missing anchors do not count against
    the others either: only the cited sections the notes have are read).
    """
    found: list[UngroundedItem] = []
    cache: dict[str, str | None] = {}
    for item, anchors in items:
        text = texts.get(item)
        if text is None:
            continue
        resolved: list[str] = []
        bodies: list[str] = []
        for anchor in dict.fromkeys(anchors):
            if anchor not in cache:
                cache[anchor] = section_text(notes, anchor)
            body = cache[anchor]
            if body is not None:
                resolved.append(anchor)
                bodies.append(body)
        if not resolved:
            continue
        score = support(text, bodies)
        if score < min_support:
            found.append(UngroundedItem(item=item, support=score, anchors=resolved))
    return found


def ungrounded_warning(ungrounded: list[UngroundedItem]) -> str:
    """One Spanish warning listing (the first ten of) `ungrounded`."""
    listing = ", ".join(
        f"{entry.item} ({round(entry.support * 100)} %, "
        f"{', '.join('#' + anchor for anchor in entry.anchors)})"
        for entry in ungrounded[:10]
    )
    more = f" y {len(ungrounded) - 10} más" if len(ungrounded) > 10 else ""
    count = len(ungrounded)
    subject = (
        "1 elemento no se apoya claramente"
        if count == 1
        else f"{count} elementos no se apoyan claramente"
    )
    return f"{subject} en los apuntes: {listing}{more}. Revísalos antes de estudiar con ellos."
