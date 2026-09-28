"""Deterministic checks and facts for building the notes from overlapping captures (#474).

The tutor-editor incorporates successive captures of the same page (`incorporate.py`, a revision
turn with a Recursos selection). What the model is told is in its prompts; what must hold whatever
it answers is checked here, and an answer that breaks it is re-asked with the Spanish errors:

- **Settled blocks are locked** (`settled_block_errors`): a block the student reviewed, with no
  `[[?` mark and no open doubt about its sources (`reviewed.settled_blocks`), must still be in the
  edited notes with the same key (`reviewed.block_key`: its text modulo footnote refs). Moving it
  or changing only its footnotes is allowed; changing its text or deleting it is not.
- **Doubts across captures only when they come together** (`same_kind_contradiction_errors`): a
  contradiction between a capture incorporated now and a capture of the same kind (notes/notes,
  book/book) that is not being incorporated now -- one already in the notes -- is not raised: the
  editor keeps the notes' reading or replaces it when the new reading is clearly better. Notes vs
  book stays allowed, as does a contradiction between captures incorporated together.
- **The facts of each capture** (`capture_facts`): for the requested captures and those the notes
  cite, the uncertain marks of its transcription, its sharpness and its triage flags, so that the
  editor can tell a clearly better reading from a merely different one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from studentassistant.editor.inputs import _UNCERTAIN, PAGE_KIND_TEXT, _transcription
from studentassistant.editor.notes_format import ProvenanceError, parse, parse_provenance
from studentassistant.editor.reviewed import block_key, content_blocks, source_key
from studentassistant.sources.triage import REASON_TEXT, triage_of
from studentassistant.vault import Vault, list_sources, topic_directory

if TYPE_CHECKING:  # `doubts` imports this module
    from studentassistant.editor.doubts import EditorDoubt

CAPTURE_KINDS: tuple[str, ...] = ("notes", "book")
"""The kinds whose captures overlap one another: the student's notebook pages and book pages."""
MAX_LISTED_MARKS = 8
"""The most uncertain marks listed per capture (the count is always given)."""
_OPENING = 60


def source_kind(ref: str) -> str | None:
    """The kind of a source id or reference: `sources/notes/page-003.jpg` -> `notes`; `None`
    for a transcript span or anything that is not under `sources/<kind>/`."""
    parts = source_key(ref).split("/")
    if len(parts) >= 3 and parts[0] == "sources":
        return parts[1]
    return None


def _opening(text: str) -> str:
    opening = " ".join(text.split())
    return opening if len(opening) <= _OPENING else opening[:_OPENING].rstrip() + "…"


def _where(document: Any, key: str) -> str:
    for section in document.sections:
        for number, block in enumerate(section.blocks, start=1):
            if block.kind != "footnotes" and block_key(block.text) == key:
                return f"el bloque {number} de #{section.anchor}"
    return "un bloque del principio"


def settled_block_errors(before: str, after: str, settled: Collection[str]) -> list[str]:
    """Spanish errors for every settled block of `before` whose text `after` changed or deleted.

    A settled key must occur in `after` at least as many times as in `before`; its footnote refs
    and its place may change.
    """
    if not settled:
        return []
    old = parse(before)
    wanted = Counter(
        key for key in (block_key(b.text) for b in content_blocks(old)) if key in settled
    )
    if not wanted:
        return []
    have = Counter(block_key(b.text) for b in content_blocks(parse(after)))
    errors: list[str] = []
    for block in content_blocks(old):
        key = block_key(block.text)
        if key not in wanted:
            continue
        if have[key] < wanted[key]:
            errors.append(
                f"{_where(old, key).capitalize()} («{_opening(block.text)}») está [revisado]: el"
                " estudiante ya lo revisó y no tiene dudas abiertas, así que no se cambia ni se"
                " borra. Déjalo con el mismo texto (puedes moverlo o cambiar sus notas al pie);"
                " si la fuente nueva lo repite, no añade nada."
            )
        del wanted[key]
    return errors


def same_kind_contradiction_errors(
    doubts: Sequence[EditorDoubt], requested: Iterable[str]
) -> list[str]:
    """Spanish errors for every contradiction between a requested capture and a capture of the
    same kind (notes/notes, book/book) that is not requested now."""
    now = {source_key(ref) for ref in requested}
    errors: list[str] = []
    for number, doubt in enumerate(doubts, start=1):
        if doubt.kind != "contradiction":
            continue
        sides = {source_key(option.source_id) for option in doubt.options} | {
            source_key(ref) for ref in doubt.refs
        }
        for kind in CAPTURE_KINDS:
            of_kind = {side for side in sides if source_kind(side) == kind}
            new, old = of_kind & now, of_kind - now
            if new and old:
                errors.append(
                    f"Duda {number} de `doubts`: es una contradicción entre"
                    f" {', '.join(sorted(new))} y {', '.join(sorted(old))}, capturas del mismo"
                    " tipo que no se incorporan juntas. No la preguntes: conserva lo que dicen"
                    " los apuntes, o sustitúyelo por la lectura nueva solo si es claramente mejor"
                    " (antes había un hueco, una lectura dudosa, un corte o algo ilegible, y la"
                    " nueva no tiene marca de duda)."
                )
                break
    return errors


def _cited_captures(notes: str | None) -> list[str]:
    cited: list[str] = []
    for definition in parse(notes or "").footnotes:
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError:
            continue
        if provenance.source_id is not None and source_kind(provenance.source_id) in CAPTURE_KINDS:
            cited.append(source_key(provenance.source_id))
    return list(dict.fromkeys(cited))


def _sharpness(meta: dict[str, Any], metrics: dict[str, Any]) -> float | None:
    score = metrics.get("sharpness")
    if isinstance(score, int | float):
        return float(score)
    scores, selected = meta.get("sharpness"), meta.get("selected_image")
    if isinstance(scores, list) and isinstance(selected, int) and 1 <= selected <= len(scores):
        value = scores[selected - 1]
        return float(value) if isinstance(value, int | float) else None
    return None


def capture_facts(
    vault: Vault, subject_slug: str, topic_slug: str, requested: Sequence[str], notes: str | None
) -> str:
    """The Spanish section giving the facts of each capture (module docstring): the requested
    notes/book pages first, then those the notes cite; empty when there is none. Blocking."""
    prefix = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
    stored = {
        source.path.removeprefix(prefix + "/"): source
        for source in list_sources(vault, subject_slug, topic_slug)
        if source.kind in CAPTURE_KINDS
    }
    asked = [key for key in dict.fromkeys(source_key(ref) for ref in requested) if key in stored]
    cited = [key for key in _cited_captures(notes) if key in stored and key not in asked]
    if not asked and not cited:
        return ""
    lines = [
        "## Datos de cada captura",
        "",
        "Para decidir si una lectura nueva es claramente mejor que la de los apuntes: cuántas"
        " lecturas dudosas (`[[?`) tiene la transcripción de cada captura, su nitidez (varianza"
        " del laplaciano: más es más nítida) y lo que marcó el triaje. Una redacción distinta no"
        " es mejor por sí sola.",
        "",
    ]
    for key in [*asked, *cited]:
        source = stored[key]
        meta = source.meta or {}
        triage = triage_of(meta)
        transcription = _transcription(vault, source)
        marks = _UNCERTAIN.findall(transcription or "")
        role = "se incorpora ahora" if key in asked else "ya citada en los apuntes"
        if transcription is None or not transcription.strip():
            reading = "sin transcripción"
        elif marks:
            listed = ", ".join(f"`{mark}`" for mark in marks[:MAX_LISTED_MARKS])
            more = ", …" if len(marks) > MAX_LISTED_MARKS else ""
            noun = "lectura dudosa" if len(marks) == 1 else "lecturas dudosas"
            reading = f"{len(marks)} {noun} ({listed}{more})"
        else:
            reading = "ninguna lectura dudosa"
        sharpness = _sharpness(meta, triage.metrics)
        sharp = f"nitidez {sharpness:.0f}" if sharpness is not None else "nitidez desconocida"
        reasons = [REASON_TEXT.get(reason, reason) for reason in triage.reasons]
        flags = f"triaje: {', '.join(reasons)}" if reasons else "triaje: sin marcas"
        lines.append(
            f"- {PAGE_KIND_TEXT[source.kind]} `{key}` ({role}): {reading}; {sharp}; {flags}."
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "CAPTURE_KINDS",
    "capture_facts",
    "same_kind_contradiction_errors",
    "settled_block_errors",
    "source_kind",
]
