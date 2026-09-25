"""Revising the notes in conversation (`editor.revise`): reply, edit ops, validation, undo."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from typing import Any

import pytest

from revise_topic import BOOK_FOOTNOTE, ReviseTopic, ampliado_notes, make_revise_topic
from studentassistant.editor.notes_format import parse, topic_source_resolver, validate
from studentassistant.editor.revise import (
    EDIT_TOOL,
    NOTES_EDITED_KIND,
    NOTES_UNDONE_KIND,
    REPLY_DELTA,
    REPLY_RESTART,
    InvalidMessageError,
    NotesMissingError,
    NothingToUndoError,
    RevisionResult,
    UndoConflictError,
    chat_history,
    revise_notes,
    undo_last_revision,
)
from studentassistant.llm import FakeClaude, LLMRequest, load_prompt
from studentassistant.vault import (
    GitSync,
    Vault,
    create_topic,
    get_subject,
    get_topic,
    read_conversation,
    read_notes,
    set_fidelity_mode,
    write_notes,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.items]


def _revise(
    topic: ReviseTopic,
    sync: GitSync,
    fake: FakeClaude,
    message: str,
    *,
    replies: Sink | None = None,
    events: Sink | None = None,
) -> RevisionResult:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            message,
            client=fake.client("editor"),
            sync=sync,
            on_reply=replies,
            on_event=events,
        )
    )


def _edit(fake: FakeClaude, reply: str, **change: Any) -> FakeClaude:
    return fake.reply_tool(EDIT_TOOL, {"summary": "Cambio", **change}, text=reply)


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def _notes(topic: ReviseTopic) -> str:
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    return notes


def _errors(topic: ReviseTopic, mode: str = "estricto") -> list[str]:
    resolver = topic_source_resolver(topic.vault, topic.subject, topic.topic)
    return validate(_notes(topic), mode, resolver)  # type: ignore[arg-type]


def _last_text(request: LLMRequest) -> str:
    content = request.messages[0]["content"]
    return [b["text"] for b in content if b["type"] == "text"][-1]


# -- the example instructions -------------------------------------------------------------------


def test_demasiado_resumido_expands_a_section_and_commits_it(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = _edit(
        FakeClaude(),
        "Amplío la definición con lo que dice el libro.",
        summary="Amplío la definición con el libro",
        ops=[
            {
                "op": "replace_section",
                "section": "definicion",
                "text": "**Derivada**: el límite del cociente incremental.[^p1][^t1]\n\n"
                "Es el límite, cuando h tiende a 0, de (f(a+h) - f(a)) / h.[^b1]\n\n"
                "Se escribe $f'(x)$.[^t2]",
            }
        ],
        footnotes=[{"label": "b1", "definition": BOOK_FOOTNOTE}],
    )
    events = Sink()

    result = _revise(topic, sync, fake, "Esto está demasiado resumido", events=events)

    assert result.applied and result.notes_changed and result.attempts == 1
    assert result.reply == "Amplío la definición con lo que dice el libro."
    assert result.changed_sections == ["definicion"]
    assert "+Es el límite, cuando h tiende a 0" in result.diff
    assert result.notes == _notes(topic) and _errors(topic) == []
    assert f"[^b1]: {BOOK_FOOTNOTE}" in _notes(topic)
    assert result.paths == [f"subjects/{topic.subject}/topics/{topic.topic}/notes/apuntes.md"]
    assert result.commit is not None
    assert _git(topic.vault, "log", "-1", "--format=%s", result.commit).strip() == (
        f"Apuntes de {topic.subject}/{topic.topic} revisados: Amplío la definición con el libro"
    )
    assert _git(topic.vault, "show", "--name-only", "--format=", result.commit).count("apuntes.md")
    assert events.kinds() == [NOTES_EDITED_KIND]
    assert events.items[0][1]["commit"] == result.commit and "notes" not in events.items[0][1]

    # What the editor was given: the whole topic, the block map and the message; the tool.
    (request,) = fake.requests
    assert request.role == "editor" and request.tool_choice == {"type": "auto"}
    assert [tool["name"] for tool in request.tools] == [EDIT_TOOL]
    assert request.tools[0]["strict"] is True
    assert request.system[0]["text"] == load_prompt("editor_revise").content
    text = _last_text(request)
    assert "Es el primer mensaje" in text and "#definicion -- ## 1. Definición" in text
    assert text.rstrip().endswith("Esto está demasiado resumido")
    assert "cache_control" not in request.messages[0]["content"][-1]
    assert any(
        "## Catálogo de fuentes citables" in block.get("text", "")
        for block in request.messages[0]["content"]
    )

    kinds = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")]
    assert kinds == ["context", "user", "assistant", "validation", "revision"]


def test_pon_un_ejemplo_inserts_a_cited_block(topic: ReviseTopic, sync: GitSync) -> None:
    fake = _edit(
        FakeClaude(),
        "Añado un ejemplo del libro.",
        ops=[
            {
                "op": "insert_after",
                "section": "definicion",
                "block": 2,
                "text": "Ejemplo: la derivada de f en a es el límite de (f(a+h) - f(a)) / h.[^b1]",
            }
        ],
        footnotes=[{"label": "b1", "definition": BOOK_FOOTNOTE}],
    )

    result = _revise(topic, sync, fake, "Pon un ejemplo")

    blocks = parse(_notes(topic)).section("definicion").blocks  # type: ignore[union-attr]
    assert [b.text[:8] for b in blocks] == ["**Deriva", "Se escri", "Ejemplo:"]
    assert result.applied and _errors(topic) == []


def test_usa_la_explicacion_del_libro_replaces_a_block(topic: ReviseTopic, sync: GitSync) -> None:
    fake = _edit(
        FakeClaude(),
        "La cambio por la del libro.",
        ops=[
            {
                "op": "replace_block",
                "section": "definicion",
                "block": 1,
                "text": "**Derivada**: el límite, cuando h tiende a 0, de (f(a+h) - f(a)) / h."
                "[^b1]",
            }
        ],
        footnotes=[{"label": "b1", "definition": BOOK_FOOTNOTE}],
    )

    result = _revise(topic, sync, fake, "Usa la explicación del libro")

    assert result.applied and _errors(topic) == []
    notes = _notes(topic)
    assert "cociente incremental.[^p1][^t1]" not in notes
    # [^p1] and [^t1] were cited only there: their definitions went with it.
    assert "[^t1]:" not in notes and "[^p1]:" not in notes


def test_no_inventes_sets_the_strict_mode_and_removes_the_ai_block(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = make_revise_topic_ampliado(make_revise_topic(tmp_vault))
    assert "[^ia]" in _notes(topic)
    fake = _edit(
        FakeClaude(),
        "Entendido: quito lo que no está en tus fuentes y no volveré a añadir nada.",
        summary="Quito el contenido ampliado por la IA",
        fidelity_mode="estricto",
        ops=[{"op": "delete_block", "section": "definicion", "block": 3}],
    )

    result = _revise(topic, sync, fake, "No inventes nada")

    assert result.applied and result.fidelity_mode == "estricto"
    assert get_topic(topic.vault, topic.subject, topic.topic).topic.fidelity_mode == "estricto"
    assert "[^ia]" not in _notes(topic) and _errors(topic, "estricto") == []
    assert result.paths == [
        f"subjects/{topic.subject}/topics/{topic.topic}/notes/apuntes.md",
        f"subjects/{topic.subject}/topics/{topic.topic}/topic.yaml",
    ]
    # The next turn is told the new mode.
    fake.reply_text("Vale.")
    _revise(topic, sync, fake, "Gracias")
    assert "modo de fidelidad «estricto»" in _last_text(fake.requests[-1])


def make_revise_topic_ampliado(topic: ReviseTopic) -> ReviseTopic:
    set_fidelity_mode(topic.vault, topic.subject, topic.topic, "ampliado")
    write_notes(topic.vault, topic.subject, topic.topic, ampliado_notes(topic.session))
    GitSync(topic.vault).checkpoint("ampliado")
    return topic


def test_no_inventes_in_strict_mode_keeping_an_ai_block_is_sent_back(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = make_revise_topic_ampliado(make_revise_topic(tmp_vault))
    fake = _edit(FakeClaude(), "Paso a modo estricto.", fidelity_mode="estricto")
    _edit(
        fake,
        "Paso a modo estricto y quito el párrafo ampliado.",
        fidelity_mode="estricto",
        ops=[{"op": "delete_block", "section": "definicion", "block": 3}],
    )
    replies = Sink()

    result = _revise(topic, sync, fake, "No inventes", replies=replies)

    assert result.applied and result.attempts == 2
    assert result.reply == "Paso a modo estricto y quito el párrafo ampliado."
    reask = fake.requests[1].messages[-1]["content"]
    assert reask[0]["type"] == "tool_result" and reask[0]["is_error"] is True
    assert "estricto no admite" in reask[0]["content"]
    assert (REPLY_RESTART, {"attempt": 2}) in replies.items


def test_a_general_preference_goes_to_the_subject_style_guide(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = _edit(
        FakeClaude(),
        "Apuntado para toda la asignatura.",
        summary="Nueva regla de estilo de la asignatura",
        style_rules=["Pon los ejemplos en cursiva."],
    )

    result = _revise(
        topic, sync, fake, "A partir de ahora, en toda la asignatura, pon los ejemplos en cursiva"
    )

    assert result.applied and not result.notes_changed and result.diff == ""
    assert result.style_rules == ["Pon los ejemplos en cursiva."]
    guide = get_subject(topic.vault, topic.subject).subject.style_guide
    assert guide == "Pon las definiciones en negrita.\n- Pon los ejemplos en cursiva.\n"
    assert result.paths == [f"subjects/{topic.subject}/subject.yaml"]
    # The same rule again changes nothing.
    fake = _edit(FakeClaude(), "Ya lo tenía.", style_rules=["Pon los ejemplos en cursiva."])
    again = _revise(topic, sync, fake, "Recuerda: ejemplos en cursiva en toda la asignatura")
    assert not again.applied and again.commit is None


# -- the loop ------------------------------------------------------------------------------------


def test_the_reply_streams_before_the_change(topic: ReviseTopic, sync: GitSync) -> None:
    fake = _edit(
        FakeClaude(),
        "Muevo el próximo día al principio.",
        ops=[{"op": "move_section", "section": "proximo-dia", "after": ""}],
    )
    replies = Sink()

    result = _revise(topic, sync, fake, "Pon lo del próximo día primero", replies=replies)

    deltas = [p for k, p in replies.items if k == REPLY_DELTA]
    assert len(deltas) > 1 and "".join(d["text"] for d in deltas) == result.reply
    assert all(d["attempt"] == 1 for d in deltas)
    assert [s.anchor for s in parse(_notes(topic)).sections] == ["proximo-dia", "definicion"]
    assert _errors(topic) == [] and result.changed_sections == ["proximo-dia"]


def test_a_chat_only_answer_changes_nothing(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text("Lo puse porque lo dijiste en clase, en el minuto 1.")
    events = Sink()

    result = _revise(topic, sync, fake, "¿Por qué pusiste esto?", events=events)

    assert not result.applied and result.commit is None and result.warning is None
    assert result.reply.startswith("Lo puse porque")
    assert _notes(topic) == topic.notes and events.items == []


def test_a_change_that_never_validates_is_not_applied(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    for _ in range(3):
        _edit(
            fake,
            "Añado una frase.",
            ops=[{"op": "insert_after", "section": "definicion", "block": 0, "text": "Sin cita."}],
        )
    events = Sink()

    result = _revise(topic, sync, fake, "Añade una introducción", events=events)

    assert not result.applied and result.attempts == 3 and result.warning
    assert any("no tiene nota al pie" in error for error in result.errors)
    assert _notes(topic) == topic.notes and events.items == [] and fake.pending == 0


def test_an_op_that_does_not_apply_is_sent_back(topic: ReviseTopic, sync: GitSync) -> None:
    fake = _edit(
        FakeClaude(), "Borro.", ops=[{"op": "delete_block", "section": "nada", "block": 1}]
    )
    _edit(
        fake, "Borro el bloque.", ops=[{"op": "delete_block", "section": "definicion", "block": 2}]
    )

    result = _revise(topic, sync, fake, "Quita la notación")

    assert result.applied and result.attempts == 2
    assert "#nada" in fake.requests[1].messages[-1]["content"][0]["content"]


def test_the_conversation_so_far_is_given_to_the_editor(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text("Claro, dime qué parte.")
    _revise(topic, sync, fake, "Quiero cambiar algo")
    fake.reply_text("Hecho.")
    _revise(topic, sync, fake, "La definición")

    text = _last_text(fake.requests[-1])
    assert "Estudiante: Quiero cambiar algo\nEditor: Claro, dime qué parte." in text
    history = chat_history(topic.vault, topic.subject, topic.topic)
    assert [turn.message for turn in history.turns] == ["Quiero cambiar algo", "La definición"]
    assert history.can_undo is False


def test_bad_requests_send_nothing(tmp_vault: Vault, topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    with pytest.raises(InvalidMessageError):
        _revise(topic, sync, fake, "   ")
    with pytest.raises(InvalidMessageError):
        _revise(topic, sync, fake, "x" * 5000)
    bare = create_topic(tmp_vault, topic.subject, "Integrales").slug
    with pytest.raises(NotesMissingError):
        _run(
            revise_notes(
                tmp_vault, topic.subject, bare, "Hola", client=fake.client("editor"), sync=sync
            )
        )
    assert fake.requests == []


# -- undo -----------------------------------------------------------------------------------------


def _delete_block(fake: FakeClaude, section: str, block: int, summary: str) -> FakeClaude:
    return _edit(
        fake,
        "Hecho.",
        summary=summary,
        ops=[{"op": "delete_block", "section": section, "block": block}],
    )


def test_undo_reverts_the_last_turn_then_the_one_before(topic: ReviseTopic, sync: GitSync) -> None:
    fake = _delete_block(FakeClaude(), "definicion", 2, "Quito la notación")
    first = _revise(topic, sync, fake, "Quita la notación")
    after_first = _notes(topic)
    _edit(
        fake,
        "Y paso a modo ampliado.",
        summary="Modo ampliado",
        fidelity_mode="ampliado",
    )
    second = _revise(topic, sync, fake, "Puedes completar con lo que sepas")
    assert second.fidelity_mode == "ampliado" and second.paths[-1].endswith("topic.yaml")
    events = Sink()

    undone = _run(
        undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync, on_event=events)
    )

    assert undone.undone_commit == second.commit and not undone.notes_changed
    assert get_topic(topic.vault, topic.subject, topic.topic).topic.fidelity_mode == "estricto"
    assert _notes(topic) == after_first
    assert events.kinds() == [NOTES_UNDONE_KIND]
    assert _git(topic.vault, "log", "-1", "--format=%s").strip() == (
        f"Deshecho en {topic.subject}/{topic.topic}: Modo ampliado"
    )

    again = _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    assert again.undone_commit == first.commit and again.notes_changed
    assert _notes(topic) == topic.notes and "+Se escribe" in again.diff
    history = chat_history(topic.vault, topic.subject, topic.topic)
    assert [turn.undone for turn in history.turns] == [True, True] and not history.can_undo
    with pytest.raises(NothingToUndoError):
        _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    # The conversation lines the undone commit carried are kept.
    assert "editor.jsonl" in _git(topic.vault, "show", "--name-only", "--format=", first.commit)
    kinds = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")]
    assert kinds.count("revision") == 2 and kinds.count(NOTES_UNDONE_KIND) == 2


def test_undo_is_refused_when_the_notes_changed_afterwards(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = _delete_block(FakeClaude(), "definicion", 2, "Quito la notación")
    _revise(topic, sync, fake, "Quita la notación")
    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\n")
    sync.checkpoint("a mano")

    with pytest.raises(UndoConflictError):
        _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    assert _notes(topic) == topic.notes + "\n"
