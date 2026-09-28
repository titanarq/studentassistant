"""Overlap-aware merge (#474): the prompts' rules, the per-capture facts the editor decides with,
and the `nothing_new` path of an incorporation and of a revision turn with a Recursos selection,
on the synthetic overlapping captures of `tests/fixtures/overlap/`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from studentassistant.editor.incorporate import incorporate_sources
from studentassistant.editor.overlap import nothing_new_reply
from studentassistant.editor.revise import EDIT_TOOL, NO_CHANGE_WARNING, revise_notes
from studentassistant.llm import FakeClaude, LLMRequest, load_prompt
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    put_source,
    read_notes,
    sources_directory,
    write_notes,
)

OVERLAP = Path(__file__).resolve().parent.parent / "fixtures" / "overlap"
REFERENCE = (OVERLAP / "reference.md").read_text(encoding="utf-8")
P1, P2, P3, P4 = (f"sources/notes/page-00{n}.jpg" for n in range(1, 5))


def _footnote(n: int) -> dict[str, str]:
    return {
        "label": f"p{n}",
        "definition": f"[Apuntes, página {n}](../sources/notes/page-00{n}.jpg)",
    }


# The list as each incorporation leaves it: page 1 without its uncertain fragments, page 2 fills
# them (clearly better: no uncertainty mark), page 3 adds the context.
LIST_1 = (
    "- Emisor: quien envía el mensaje.\n"
    "- Receptor: quien recibe el mensaje.\n"
    "- Mensaje: la información que se transmite.\n"
    "- Canal: el medio físico por el que viaja el mensaje.[^p1]"
)
LIST_2 = (
    "- Emisor: quien envía el mensaje.\n"
    "- Receptor: quien recibe el mensaje.\n"
    "- Mensaje: la información que se transmite.\n"
    "- Canal: el medio físico por el que viaja el mensaje, p. ej. las ondas sonoras.\n"
    "- Código: conjunto de signos y reglas que comparten emisor y receptor.[^p1][^p2]"
)
LIST_3 = LIST_2.removesuffix("[^p1][^p2]") + (
    "\n- Contexto: la situación en la que se produce la comunicación.[^p1][^p2][^p3]"
)
REVIEW = (
    "- Elementos: emisor, receptor, mensaje, canal, código y contexto.\n"
    "- Sin código común no hay comunicación.[^p3]"
)


@dataclass(frozen=True)
class Topic:
    vault: Vault
    subject: str
    topic: str


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


@pytest.fixture
def topic(tmp_vault: Vault) -> Topic:
    """The four overlapping pages of the fixture, with their triage metadata; no notes yet."""
    s = create_subject(tmp_vault, "Lengua").slug
    t = create_topic(tmp_vault, s, "La comunicación").slug
    metadata = json.loads((OVERLAP / "pages.json").read_text(encoding="utf-8"))
    notes_dir = sources_directory(tmp_vault, s, t, "notes")
    for n in range(1, 5):
        stem = f"page-00{n}"
        put_source(
            tmp_vault, s, t, "notes", "page.jpg", f"\xff\xd8 {stem}".encode(), metadata[stem]
        )
        (notes_dir / f"{stem}.md").write_text(
            (OVERLAP / f"{stem}.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
    GitSync(tmp_vault).checkpoint("fixture")
    return Topic(tmp_vault, s, t)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


def _texts(request: LLMRequest) -> str:
    return "\n".join(
        block.get("text", "") or str(block.get("content", ""))
        for message in request.messages
        for block in message["content"]
        if isinstance(block, dict)
    )


def _system(request: LLMRequest) -> str:
    return "\n".join(block.get("text", "") for block in request.system)


def _notes(topic: Topic) -> str:
    text = read_notes(topic.vault, topic.subject, topic.topic)
    assert text is not None
    return text


def _incorporate(topic: Topic, sync: GitSync, fake: FakeClaude, ids: list[str]) -> Any:
    return _run(
        incorporate_sources(
            topic.vault,
            topic.subject,
            topic.topic,
            ids,
            client=fake.client("editor"),
            sync=sync,
            host="pc",
        )
    )


def _edit(fake: FakeClaude, text: str, **tool: Any) -> FakeClaude:
    return fake.reply_tool(EDIT_TOOL, {"footnotes": [], **tool}, text=text)


def test_the_prompts_carry_the_overlap_and_clearly_better_rules() -> None:
    incorporate = load_prompt("editor_incorporate").content
    assert "**Captures overlap**" in incorporate
    assert "**Replace only when clearly better**" in incorporate
    assert "A different wording, or a reading that is\n  just as uncertain, is never better" in (
        incorporate
    )
    assert "«[revisado]» are locked" in incorporate
    revise = load_prompt("editor_revise").content
    assert "**When the student selected sources in Recursos**" in revise
    assert "- `nothing_new`: only when the student selected sources in Recursos" in revise
    assert "a different wording alone is never better" in revise
    generate = load_prompt("editor_generate").content
    assert "never write a page's content once per capture" in generate


def test_overlapping_captures_add_only_what_is_new_and_a_repeat_changes_nothing(
    topic: Topic, sync: GitSync
) -> None:
    fake = FakeClaude()
    _edit(
        fake,
        "He creado la sección de los elementos con la página 1.",
        summary="Incorporo la página 1: elementos de la comunicación",
        ops=[
            {
                "op": "add_section",
                "after": "",
                "level": 2,
                "title": "Elementos de la comunicación",
                "anchor": "elementos",
                "text": LIST_1,
            }
        ],
        footnotes=[_footnote(1)],
    )
    _edit(
        fake,
        "La página 2 es la misma lista más nítida: completo el canal y el código.",
        summary="Completo el canal y el código con la página 2",
        ops=[{"op": "replace_block", "section": "elementos", "block": 1, "text": LIST_2}],
        footnotes=[_footnote(2)],
    )
    _edit(
        fake,
        "Añado el contexto a la lista y el repaso de la página 3.",
        summary="Incorporo la página 3: contexto y repaso",
        ops=[
            {"op": "replace_block", "section": "elementos", "block": 1, "text": LIST_3},
            {
                "op": "add_section",
                "after": "elementos",
                "level": 2,
                "title": "Repaso",
                "anchor": "repaso",
                "text": REVIEW,
            },
        ],
        footnotes=[_footnote(3)],
    )
    # Page 4 only repeats page 3: no op, and no reply of its own.
    _edit(fake, "", summary="Sin cambios", ops=[], nothing_new=[P4])

    for page in (P1, P2, P3):
        result = _incorporate(topic, sync, fake, [page])
        assert result.applied and not result.errors, result.errors
    before = _notes(topic)
    repeat = _incorporate(topic, sync, fake, [P4])

    # Every idea once, the uncertain fragments filled from the sharper page: the reference notes.
    assert before == REFERENCE
    assert not repeat.applied and not repeat.notes_changed and not repeat.errors
    assert repeat.nothing_new == [P4] and repeat.commit is None
    assert repeat.reply == (
        "La página 4 de tus apuntes no aporta nada nuevo: los apuntes ya lo dicen, así que no los"
        " he cambiado."
    )
    assert _notes(topic) == REFERENCE

    # The editor is told the overlap rules, and given the facts to judge "clearly better".
    first, second = fake.requests[0], fake.requests[1]
    for request in fake.requests:
        assert "**Captures overlap**" in _system(request)
        assert "claramente mejor" in _texts(request)
    assert (
        "- Apuntes `sources/notes/page-001.jpg` (se incorpora ahora): 2 lecturas"
        " dudosas (`[[?ondas sonoras]]`, `[[?]]`); nitidez 41;" in _texts(first)
    )
    assert "`sources/notes/page-002.jpg` (se incorpora ahora): ninguna lectura dudosa; nitidez" in (
        _texts(second)
    )
    assert "`sources/notes/page-001.jpg` (ya citada en los apuntes): 2 lecturas dudosas" in (
        _texts(second)
    )


def test_a_reply_of_its_own_is_kept_on_a_repeat(topic: Topic, sync: GitSync) -> None:
    write_notes(topic.vault, topic.subject, topic.topic, REFERENCE)
    GitSync(topic.vault).checkpoint("notes")
    fake = _edit(
        FakeClaude(),
        "La página 4 repite la 3; no he cambiado los apuntes.",
        summary="Sin cambios",
        ops=[],
        nothing_new=[P4],
    )
    result = _incorporate(topic, sync, fake, [P4])
    assert result.reply == "La página 4 repite la 3; no he cambiado los apuntes."
    assert result.nothing_new == [P4] and not result.notes_changed


def _revise(topic: Topic, sync: GitSync, fake: FakeClaude, selected: list[str]) -> Any:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "Añade el esquema de la imagen que he seleccionado",
            client=fake.client("editor"),
            sync=sync,
            selected_sources=selected,
            expects_change=True,
        )
    )


def test_a_selected_capture_that_only_repeats_is_nothing_new_without_a_warning(
    topic: Topic, sync: GitSync
) -> None:
    write_notes(topic.vault, topic.subject, topic.topic, REFERENCE)
    GitSync(topic.vault).checkpoint("notes")
    fake = _edit(FakeClaude(), "", summary="", ops=[], nothing_new=[P4])

    result = _revise(topic, sync, fake, [P4])

    assert result.attempts == 1 and not result.errors
    assert result.warning != NO_CHANGE_WARNING and result.warning is None
    assert result.nothing_new == [P4] and not result.notes_changed and not result.applied
    assert result.reply.startswith("La página 4 de tus apuntes no aporta nada nuevo")
    assert _notes(topic) == REFERENCE
    [request] = fake.requests
    assert "**When the student selected sources in Recursos**" in _system(request)
    turn = request.messages[0]["content"][-1]["text"]
    assert "si no aportan nada nuevo, ponlas en `nothing_new`" in turn
    assert "`sources/notes/page-004.jpg` (se incorpora ahora): ninguna lectura dudosa" in (
        _texts(request)
    )


def test_nothing_new_may_only_name_selected_sources(topic: Topic, sync: GitSync) -> None:
    write_notes(topic.vault, topic.subject, topic.topic, REFERENCE)
    GitSync(topic.vault).checkpoint("notes")
    fake = FakeClaude()
    _edit(fake, "No aportan nada.", summary="", ops=[], nothing_new=[P3, P4])
    _edit(fake, "La página 4 no aporta nada nuevo.", summary="", ops=[], nothing_new=[P4])

    result = _revise(topic, sync, fake, [P4])

    assert result.attempts == 2 and not result.errors and result.nothing_new == [P4]
    assert (
        "`nothing_new` solo puede nombrar fuentes seleccionadas en Recursos;"
        " sources/notes/page-003.jpg no lo es." in _texts(fake.requests[1])
    )
    assert result.reply == "La página 4 no aporta nada nuevo."


def test_nothing_new_reply_names_every_repeated_source() -> None:
    assert nothing_new_reply([P3, P4, "sources/book/page-012.jpg", P3]) == (
        "La página 3 de tus apuntes, la página 4 de tus apuntes y la página 12 del libro no"
        " aportan nada nuevo: los apuntes ya lo dicen, así que no los he cambiado."
    )
    assert nothing_new_reply(["sources/web/001-x.md"]).startswith(
        "`sources/web/001-x.md` no aporta"
    )
    assert nothing_new_reply([]) == ""
