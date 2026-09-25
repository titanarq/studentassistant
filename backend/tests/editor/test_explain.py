""" "¿Por qué pusiste esto?" (`editor.explain`): finding the block, its sources, the answer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from generate_topic import make_pdf
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.explain import (
    BlockAnchor,
    BlockNotFoundError,
    explain_block,
    find_block,
)
from studentassistant.editor.notes_format import parse
from studentassistant.editor.revise import NotesMissingError, _history_text, chat_history
from studentassistant.llm import FakeClaude, LLMRequest, RefusalError, load_prompt
from studentassistant.sources import PageRange, import_pdf
from studentassistant.vault import (
    GitSync,
    Vault,
    create_topic,
    read_conversation,
    read_notes,
    write_notes,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))


def _explain(topic: ReviseTopic, fake: FakeClaude, anchor: BlockAnchor, **kwargs: Any) -> Any:
    return _run(
        explain_block(
            topic.vault,
            topic.subject,
            topic.topic,
            anchor,
            client=fake.client("editor"),
            **kwargs,
        )
    )


def _texts(request: LLMRequest) -> str:
    content = request.messages[0]["content"]
    return "\n".join(b["text"] for b in content if b["type"] == "text")


# -- finding the block ----------------------------------------------------------------------------


def test_find_block_by_number_and_by_quote(topic: ReviseTopic) -> None:
    document = parse(topic.notes)
    section, number, block = find_block(document, BlockAnchor(section="definicion", block=2))
    assert (section, number) == ("definicion", 2)
    assert block.text.startswith("Se escribe")

    # A quote as the web shows it (no footnotes, no Markdown) confirms the numbered block...
    found = find_block(
        document,
        BlockAnchor(section="definicion", block=1, quote="Derivada: el límite del cociente…"),
    )
    assert found[:2] == ("definicion", 1)
    # ...and finds it when the number is off, even in another section.
    found = find_block(
        document, BlockAnchor(section="definicion", block=1, quote="La regla de la cadena")
    )
    assert found[:2] == ("proximo-dia", 1)
    found = find_block(document, BlockAnchor(section="definicion", quote="Se escribe $f'(x)$."))
    assert found[:2] == ("definicion", 2)


def test_find_block_refuses_what_is_not_there(topic: ReviseTopic) -> None:
    document = parse(topic.notes)
    with pytest.raises(BlockNotFoundError):
        find_block(document, BlockAnchor(section="definicion", block=9))
    with pytest.raises(BlockNotFoundError):
        find_block(document, BlockAnchor(section="nada", block=1))
    with pytest.raises(BlockNotFoundError):
        find_block(document, BlockAnchor(section="definicion", quote="texto que no existe"))
    # The title of the preamble is not a block to explain.
    with pytest.raises(BlockNotFoundError, match="párrafo"):
        find_block(document, BlockAnchor(section=None, block=1))


# -- the explanation ------------------------------------------------------------------------------


def test_explains_a_block_from_its_sources(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("Lo pusiste en tu página 1 y lo dijiste en clase.")
    replies = Sink()
    result = _explain(topic, fake, BlockAnchor(section="definicion", block=1), on_reply=replies)

    assert result.reply == "Lo pusiste en tu página 1 y lo dijiste en clase."
    assert result.section == "definicion" and result.block == 1
    assert result.question == (
        "¿Por qué pusiste esto? (en la sección #definicion)"
        " «**Derivada**: el límite del cociente incremental.»"
    )
    assert [(r.label, r.kind, r.source_id) for r in result.refs] == [
        ("p1", "notes", "sources/notes/page-001.jpg"),
        ("t1", "transcript", f"sessions/{topic.session}#t=00:00:02-00:00:10"),
    ]
    assert result.refs[0].text == "Apuntes, página 1"
    # Streamed as it was written.
    assert "".join(p["text"] for k, p in replies.items if k == "reply.delta") == result.reply

    [request] = fake.requests
    assert request.system[0]["text"] == load_prompt("editor_explain").content
    assert request.tools in (None, [])
    text = _texts(request)
    # The block, its footnotes, the page transcription and the transcript span around it.
    assert "**Derivada**: el límite del cociente incremental.[^p1][^t1]" in text
    assert "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)" in text
    assert "Derivada: límite del cociente incremental." in text  # page-001.md
    assert "La derivada es el límite del cociente incremental.  <- citado" in text
    assert "Se escribe f prima de x." in text  # context around the span, not marked
    assert "Se escribe f prima de x.  <- citado" not in text
    assert "Mañana repasamos" not in text  # an hour later: out of the span's context
    # The page image is sent even though its transcription is plain (the cropped page).
    images = [b for b in request.messages[0]["content"] if b["type"] == "image"]
    assert len(images) == 1
    assert result.images == ["sources/notes/page-001.jpg"]
    # Not the other sources of the topic.
    assert "[^p2]" not in text and "Libro" not in text


def test_explanation_is_appended_to_the_conversation(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("Sale del libro.")
    _explain(topic, fake, BlockAnchor(section="proximo-dia", block=1), sync=GitSync(topic.vault))

    records = read_conversation(topic.vault, topic.subject, topic.topic, "editor")
    assert [r.kind for r in records] == ["context", "user", "assistant", "explanation"]
    assert records[0].detail is not None and records[0].detail["reason"] == "explain"
    user = records[1].message
    assert user is not None
    assert {"type": "image_ref", "source_id": "sources/notes/page-002.jpg"} in user["content"]

    history = chat_history(topic.vault, topic.subject, topic.topic)
    [turn] = history.turns
    assert turn.kind == "explain" and turn.reply == "Sale del libro."
    assert turn.message.startswith("¿Por qué pusiste esto? (en la sección #proximo-dia)")
    assert [ref.label for ref in turn.refs] == ["t3", "p2"]
    assert not turn.applied and not history.can_undo
    # The revision conversation that follows sees it, with no "not applied" remark.
    text = _history_text(history.turns)
    assert "Editor: Sale del libro." in text and "validación" not in text
    # The notes are untouched.
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes


def test_ia_blocks_and_missing_sources_are_said(tmp_vault: Vault) -> None:
    base = make_revise_topic(tmp_vault)
    notes = base.notes.replace(
        "Se escribe $f'(x)$.[^t2]\n",
        "Se escribe $f'(x)$.[^t2]\n\nTambién df/dx.[^ia][^w1][^zz]\n",
    ).replace(
        "[^p1]: [Apuntes",
        "[^ia]: Ampliado por la IA: no está en tus fuentes\n"
        "[^w1]: [Web: Leibniz](../sources/web/001-leibniz.md)\n[^p1]: [Apuntes",
    )
    write_notes(tmp_vault, base.subject, base.topic, notes)
    fake = FakeClaude().reply_text("Lo añadí yo.")
    result = _explain(base, fake, BlockAnchor(section="definicion", quote="También df/dx."))
    assert result.block == 3
    assert [ref.kind for ref in result.refs] == ["ia", "web"]
    text = _texts(fake.requests[0])
    assert "no sale de las fuentes" in text
    assert "ya no está en el tema" in text
    assert "[^zz]" in text  # undefined: named as citing nothing


def test_refusal_and_missing_notes(tmp_vault: Vault, topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("No.", stop_reason="refusal")
    with pytest.raises(RefusalError):
        _explain(topic, fake, BlockAnchor(section="definicion", block=1))
    records = read_conversation(topic.vault, topic.subject, topic.topic, "editor")
    assert "explanation" not in [r.kind for r in records]

    empty = create_topic(tmp_vault, topic.subject, "Integrales").slug
    with pytest.raises(NotesMissingError):
        _run(
            explain_block(
                tmp_vault,
                topic.subject,
                empty,
                BlockAnchor(section="definicion", block=1),
                client=FakeClaude().client("editor"),
            )
        )


def test_an_empty_answer_carries_a_warning(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("  ")
    result = _explain(topic, fake, BlockAnchor(section="definicion", block=2))
    assert result.reply == "" and result.warning is not None


def test_a_pdf_page_is_read_as_its_text(tmp_vault: Vault) -> None:
    base = make_revise_topic(tmp_vault)
    import_pdf(
        tmp_vault, base.subject, base.topic, "libro.pdf", make_pdf(90), pages=PageRange(84, 85)
    )
    notes = base.notes.replace(
        "Se escribe $f'(x)$.[^t2]\n", "Se escribe $f'(x)$.[^t2]\n\nDel libro.[^pdf1]\n"
    ).replace(
        "[^p1]: [Apuntes",
        "[^pdf1]: [PDF, página 85](../sources/pdf/page-001.pdf#page=2)\n[^p1]: [Apuntes",
    )
    write_notes(tmp_vault, base.subject, base.topic, notes)
    fake = FakeClaude().reply_text("Del PDF.")
    result = _explain(base, fake, BlockAnchor(section="definicion", block=3))
    assert [(r.kind, r.source_id) for r in result.refs] == [
        ("pdf", "sources/pdf/page-001.pdf#page=2")
    ]
    text = _texts(fake.requests[0])
    assert "Página 85 del libro" in text and "página 85 del original" in text
    assert "Página 84 del libro" not in text
    assert not [b for b in fake.requests[0].messages[0]["content"] if b["type"] == "document"]
