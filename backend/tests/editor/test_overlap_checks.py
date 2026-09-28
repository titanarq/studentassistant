"""Lock and contradiction checks for overlapping captures (`editor.overlap`, #474): settled blocks
are locked, a contradiction against an already-incorporated capture of the same kind is re-asked,
the doubts review keeps settled blocks and auto-resolves what they already say, and the editor
input gives the facts of each capture."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from doubts_topic import DoubtsTopic, make_doubts_topic, review_reply, transcript_id
from studentassistant.editor.doubts import (
    REVIEW_TOOL,
    SETTLED_REVIEW_RULE,
    EditorDoubt,
    SourceOption,
    dismiss_doubt,
    list_doubts,
    review_doubts,
)
from studentassistant.editor.incorporate import incorporate_sources
from studentassistant.editor.overlap import (
    capture_facts,
    same_kind_contradiction_errors,
    settled_block_errors,
    source_kind,
)
from studentassistant.editor.reviewed import block_key, record_reviewed, settled_blocks
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.llm import FakeClaude, LLMRequest
from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.vault import (
    GitSync,
    Vault,
    end_session,
    put_source,
    read_conversation,
    read_notes,
    sources_directory,
    start_session,
)

NOTATION = "Se escribe $f'(x)$.[^t2]"
P1 = "sources/notes/page-001.jpg"
P2 = "sources/notes/page-002.jpg"
P3 = "sources/notes/page-003.jpg"
P4 = "sources/notes/page-004.jpg"
B1 = "sources/book/page-001.jpg"
P3_FOOTNOTE = "[Apuntes, página 3](../sources/notes/page-003.jpg)"
P4_FOOTNOTE = "[Apuntes, página 4](../sources/notes/page-004.jpg)"
B1_FOOTNOTE = "[Libro, página 1](../sources/book/page-001.jpg)"
PAGE_3 = "La derivada de $x^2$ es $2x$, como vimos en la [[?revista]].\n"
PAGE_4 = "La derivada de $x^3$ es $3x^2$.\n"


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


@pytest.fixture
def topic(tmp_vault: Vault, sync: GitSync) -> DoubtsTopic:
    """The doubts topic with p-1 dismissed -- so `Se escribe $f'(x)$` is settled (reviewed; p-4
    and p-5 still name pages 1 and 2) -- plus page 3 (flagged as cut, one uncertain word), page 4
    and a book page, none of them incorporated yet."""
    topic = make_doubts_topic(tmp_vault)
    s, t = topic.subject, topic.topic
    _run(dismiss_doubt(tmp_vault, s, t, "p-1", sync=sync))
    triage = {"status": "flagged", "reasons": ["partial"], "metrics": {"sharpness": 142.7}}
    put_source(tmp_vault, s, t, "notes", "page.jpg", b"\xff\xd8 three", {"triage": triage})
    put_source(tmp_vault, s, t, "notes", "page.jpg", b"\xff\xd8 four", {})
    put_source(tmp_vault, s, t, "book", "page.jpg", b"\xff\xd8 book", {})
    notes_dir = sources_directory(tmp_vault, s, t, "notes")
    (notes_dir / "page-003.md").write_text(PAGE_3, encoding="utf-8")
    (notes_dir / "page-004.md").write_text(PAGE_4, encoding="utf-8")
    (sources_directory(tmp_vault, s, t, "book") / "page-001.md").write_text(
        "La derivada de $x^2$ es $2x$.\n", encoding="utf-8"
    )
    GitSync(tmp_vault).checkpoint("fixture")
    notes = read_notes(tmp_vault, s, t)
    assert settled_blocks(tmp_vault, s, t, notes) == {block_key(NOTATION)}
    return topic


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


def _notes(topic: DoubtsTopic) -> str:
    text = read_notes(topic.vault, topic.subject, topic.topic)
    assert text is not None
    return text


def _incorporate(topic: DoubtsTopic, sync: GitSync, fake: FakeClaude, ids: list[str]) -> Any:
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


def _add_x2(fake: FakeClaude, **extra: Any) -> FakeClaude:
    """A valid incorporation of page 3: a new paragraph after the settled block."""
    return fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Añado la derivada de x² de la página 3",
            "ops": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 2,
                    "text": "La derivada de $x^2$ es $2x$.[^p3]",
                }
            ],
            "footnotes": [{"label": "p3", "definition": P3_FOOTNOTE}],
            **extra,
        },
        text="He añadido la derivada de x².",
    )


def _contradiction(new: str, old: str) -> dict[str, Any]:
    return {
        "kind": "contradiction",
        "text": "Las dos capturas dan derivadas distintas.",
        "question": "¿Cuál es la correcta?",
        "options": [
            {"source_id": new, "says": "2x"},
            {"source_id": old, "says": "x"},
        ],
    }


# -- pure checks ---------------------------------------------------------------------------------


def test_a_settled_block_may_move_or_change_its_footnotes_but_not_its_text() -> None:
    before = (
        "# T\n\n## A {#a}\n\nUno.[^p1]\n\nDos.[^p1]\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
    )
    settled = {block_key("Uno.")}
    reordered = before.replace("Uno.[^p1]\n\nDos.[^p1]", "Dos.[^p1]\n\nUno.")
    assert settled_block_errors(before, reordered, settled) == []
    assert settled_block_errors(before, before.replace("Dos.", "Tres."), settled) == []

    [changed] = settled_block_errors(before, before.replace("Uno.", "Una."), settled)
    assert changed.startswith("El bloque 1 de #a («Uno.[^p1]») está [revisado]")
    [deleted] = settled_block_errors(before, before.replace("Uno.[^p1]\n\n", ""), settled)
    assert "no se cambia ni se borra" in deleted
    assert settled_block_errors(before, before.replace("Uno.", "Una."), set()) == []


def test_a_settled_block_may_not_come_to_cite_a_source_with_open_doubts() -> None:
    before = (
        "# T\n\n## A {#a}\n\nUno.[^p1]\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
    )
    cited = before.replace("Uno.[^p1]", "Uno.[^p1][^p3]") + f"[^p3]: {P3_FOOTNOTE}\n"
    settled = {block_key("Uno.")}

    [error] = settled_block_errors(before, cited, settled, {P3})
    assert error.startswith("El bloque 1 de #a («Uno.[^p1]») está [revisado]")
    assert f"no le añadas una nota al pie de {P3}, que tiene dudas abiertas" in error
    assert settled_block_errors(before, cited, settled, {P4}) == []
    assert settled_block_errors(before, cited, settled) == []
    # A source it already cited may have doubts: only a new citation unlocks the block.
    assert settled_block_errors(before, cited, settled, {P1}) == []


def test_only_a_same_kind_contradiction_against_a_capture_not_requested_is_refused() -> None:
    def doubt(*sides: str) -> EditorDoubt:
        return EditorDoubt(
            kind="contradiction",
            text="t",
            question="q",
            options=[SourceOption(source_id=side, says="x") for side in sides],
        )

    [error] = same_kind_contradiction_errors([doubt(P3, P1)], [P3])
    assert error.startswith("Duda 1 de `doubts`: es una contradicción entre")
    assert P3 in error and P1 in error
    assert same_kind_contradiction_errors([doubt(P3, P4)], [P3, P4]) == []
    assert same_kind_contradiction_errors([doubt(P3, B1)], [P3]) == []
    assert same_kind_contradiction_errors([doubt(B1, "sources/book/page-002.jpg")], [B1])
    illegible = EditorDoubt(kind="illegible", text="t", question="q", suggestions=["a"], refs=[P1])
    assert same_kind_contradiction_errors([illegible], [P3]) == []
    assert source_kind("sources/pdf/page-001.pdf#page=2") == "pdf"
    assert source_kind("sessions/abc#t=00:00:01-00:00:02") is None


def test_the_facts_list_the_requested_captures_then_the_cited_ones(topic: DoubtsTopic) -> None:
    facts = capture_facts(topic.vault, topic.subject, topic.topic, [P3], _notes(topic))

    lines = facts.splitlines()
    assert lines[0] == "## Datos de cada captura"
    [p3, p1, p2] = [line for line in lines if line.startswith("- ")]
    assert p3 == (
        f"- Apuntes `{P3}` (se incorpora ahora): 1 lectura dudosa (`[[?revista]]`);"
        " nitidez 143; triaje: cortada."
    )
    assert p1.startswith(f"- Apuntes `{P1}` (ya citada en los apuntes): ninguna lectura dudosa;")
    assert p2.startswith(f"- Apuntes `{P2}` (ya citada en los apuntes): sin transcripción;")
    assert capture_facts(topic.vault, topic.subject, topic.topic, [], None) == ""


# -- incorporation -------------------------------------------------------------------------------


def test_the_incorporation_input_gives_the_facts_of_each_capture(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = _add_x2(FakeClaude())

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied and result.attempts == 1
    text = _texts(fake.requests[0])
    assert "## Datos de cada captura" in text
    assert f"`{P3}` (se incorpora ahora): 1 lectura dudosa (`[[?revista]]`)" in text
    assert f"`{P1}` (ya citada en los apuntes)" in text
    assert "bloque 2 (paragraph) [revisado]: Se escribe" in text


def test_changing_a_settled_block_is_re_asked_and_the_block_kept(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        {
            "summary": "Reescribo la notación con la página 3",
            "ops": [
                {
                    "op": "replace_block",
                    "section": "definicion",
                    "block": 2,
                    "text": "Se escribe $f'(x)$ o $\\frac{df}{dx}$.[^p3]",
                }
            ],
            "footnotes": [{"label": "p3", "definition": P3_FOOTNOTE}],
        },
        text="He reescrito la notación.",
    )
    _add_x2(fake)

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied and result.attempts == 2 and not result.errors
    reask = _texts(fake.requests[1])
    assert "El bloque 2 de #definicion («Se escribe $f'(x)$.[^t2]») está [revisado]" in reask
    notes = _notes(topic)
    assert NOTATION in notes and "La derivada de $x^2$ es $2x$.[^p3]" in notes


def test_deleting_a_settled_block_every_time_leaves_the_notes_unchanged(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    before = _notes(topic)
    delete = {
        "summary": "Quito la notación, repetida en la página 3",
        "ops": [
            {"op": "delete_block", "section": "definicion", "block": 2},
            {
                "op": "insert_after",
                "section": "definicion",
                "block": 1,
                "text": "La derivada de $x^2$ es $2x$.[^p3]",
            },
        ],
        "footnotes": [{"label": "p3", "definition": P3_FOOTNOTE}],
    }
    fake = FakeClaude()
    for _ in range(3):
        fake.reply_tool(EDIT_TOOL, delete, text="Quito lo repetido.")

    result = _incorporate(topic, sync, fake, [P3])

    assert not result.applied and result.errors and result.warning
    assert any("está [revisado]" in error for error in result.errors)
    assert _notes(topic) == before


def test_a_contradiction_with_an_incorporated_page_of_the_same_kind_is_re_asked(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = _add_x2(FakeClaude(), doubts=[_contradiction(P3, P1)])
    _add_x2(fake)
    open_before = list_doubts(topic.vault, topic.subject, topic.topic).open_count

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied and result.attempts == 2 and result.doubts == []
    assert "capturas del mismo tipo que no se incorporan juntas" in _texts(fake.requests[1])
    assert list_doubts(topic.vault, topic.subject, topic.topic).open_count == open_before


def test_contradictions_between_pages_incorporated_together_or_notes_and_book_are_raised(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        {
            "summary": "Añado las derivadas de las páginas 3 y 4 y del libro",
            "ops": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 2,
                    "text": "La derivada de $x^2$ es $2x$.[^p3][^b1] La de $x^3$ es $3x^2$.[^p4]",
                }
            ],
            "footnotes": [
                {"label": "p3", "definition": P3_FOOTNOTE},
                {"label": "p4", "definition": P4_FOOTNOTE},
                {"label": "b1", "definition": B1_FOOTNOTE},
            ],
            "doubts": [_contradiction(P3, P4), _contradiction(B1, P1)],
        },
        text="He añadido las derivadas; hay dos puntos que no coinciden.",
    )

    result = _incorporate(topic, sync, fake, [P3, P4, B1])

    assert result.applied and result.attempts == 1 and not result.errors
    assert len(result.doubts) == 2


def _doubt_on_page_3(topic: DoubtsTopic) -> None:
    """An open transcriber doubt, `p-6`, on page 3 -- the re-capture of the settled line."""
    session = start_session(
        topic.vault, topic.subject, topic.topic, host="pc", protocol_version="1.1"
    )
    session.append_event("session.started", "user", {})
    session.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {
            "op": "add_pending",
            "pending_id": "p-6",
            "kind": "illegible",
            "text": "Lectura dudosa «revista» en la página 3.",
            "source_refs": [P3],
        },
    )
    session.append_event("session.ended", "user", {})
    end_session(session)
    GitSync(topic.vault).checkpoint("doubt on page 3")


def _cite_on_notation(label: str, definition: str) -> dict[str, Any]:
    """An incorporation that only adds a footnote ref to the settled block, as corroboration."""
    return {
        "summary": "La página repite la notación",
        "ops": [
            {
                "op": "replace_block",
                "section": "definicion",
                "block": 2,
                "text": f"Se escribe $f'(x)$.[^t2][^{label}]",
            }
        ],
        "footnotes": [{"label": label, "definition": definition}],
    }


def test_citing_a_doubted_capture_on_a_settled_block_is_re_asked(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _doubt_on_page_3(topic)
    fake = FakeClaude().reply_tool(
        EDIT_TOOL, _cite_on_notation("p3", P3_FOOTNOTE), text="La página 3 repite la notación."
    )
    _add_x2(fake)

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied and result.attempts == 2 and not result.errors
    reask = _texts(fake.requests[1])
    assert "El bloque 2 de #definicion («Se escribe $f'(x)$.[^t2]») está [revisado]" in reask
    assert f"no le añadas una nota al pie de {P3}, que tiene dudas abiertas" in reask
    notes = _notes(topic)
    assert NOTATION in notes and "La derivada de $x^2$ es $2x$.[^p3]" in notes
    assert block_key(NOTATION) in settled_blocks(topic.vault, topic.subject, topic.topic, notes)


def test_citing_a_clean_capture_on_a_settled_block_keeps_it_settled(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _doubt_on_page_3(topic)
    fake = FakeClaude().reply_tool(
        EDIT_TOOL, _cite_on_notation("p4", P4_FOOTNOTE), text="La página 4 repite la notación."
    )

    result = _incorporate(topic, sync, fake, [P4])

    assert result.applied and result.attempts == 1 and not result.errors
    notes = _notes(topic)
    assert "Se escribe $f'(x)$.[^t2][^p4]" in notes
    assert block_key(NOTATION) in settled_blocks(topic.vault, topic.subject, topic.topic, notes)


def test_the_review_of_the_repeated_capture_still_sees_the_block_settled(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _doubt_on_page_3(topic)
    fake = FakeClaude().reply_tool(
        EDIT_TOOL, _cite_on_notation("p3", P3_FOOTNOTE), text="La página 3 repite la notación."
    )
    _add_x2(fake)
    _incorporate(topic, sync, fake, [P3])
    notes = _notes(topic)
    # Had the ref been accepted, the block would have lost «[revisado]» (design case (c)).
    unlocked = notes.replace(NOTATION, "Se escribe $f'(x)$.[^t2][^p3]")
    assert block_key(NOTATION) not in settled_blocks(
        topic.vault, topic.subject, topic.topic, unlocked
    )
    resolve = {
        "pending_id": "p-6",
        "action": "auto_resolve",
        "resolution": "Los apuntes ya dicen la notación; la página 3 solo la repite.",
        "evidence": [
            {"source_id": transcript_id(topic, "00:00:10-00:00:15"), "quote": "Se escribe f'(x)"}
        ],
        "edits": [],
    }
    review = FakeClaude().reply_tool(REVIEW_TOOL, {"decisions": [resolve]})

    result = _run(
        review_doubts(
            topic.vault,
            topic.subject,
            topic.topic,
            client=review.client("editor"),
            sync=sync,
            pending_ids=["p-6"],
        )
    )

    text = _texts(review.requests[0])
    assert "bloque 2 (paragraph) [revisado]: Se escribe" in text
    assert SETTLED_REVIEW_RULE in text
    assert result.attempts == 1 and result.auto_resolved == ["p-6"] and result.asked == []
    assert _notes(topic) == notes


# -- the doubts review ---------------------------------------------------------------------------


def _review(topic: DoubtsTopic, sync: GitSync, fake: FakeClaude) -> Any:
    return _run(
        review_doubts(
            topic.vault, topic.subject, topic.topic, client=fake.client("editor"), sync=sync
        )
    )


def _asks(topic: DoubtsTopic) -> list[dict[str, Any]]:
    return [d for d in review_reply(topic)["decisions"] if d["pending_id"] != "p-1"]


def test_the_review_keeps_settled_blocks_and_is_told_to_auto_resolve_what_they_say(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    before = _notes(topic)
    [p4, p5] = _asks(topic)
    rewrite = {
        **p4,
        "action": "auto_resolve",
        "question": None,
        "suggestions": [],
        "resolution": "Pone «incremental».",
        "evidence": [{"source_id": P1, "quote": "cociente incremental"}],
        "edits": [
            {
                "op": "replace_block",
                "section": "definicion",
                "block": 2,
                "text": "Se escribe $f'(x)$ (derivada).[^t2]",
            }
        ],
    }
    keep = {**rewrite, "edits": []}
    fake = (
        FakeClaude()
        .reply_tool(REVIEW_TOOL, {"decisions": [rewrite, p5]})
        .reply_tool(REVIEW_TOOL, {"decisions": [keep, p5]})
    )

    result = _review(topic, sync, fake)

    first = _texts(fake.requests[0])
    assert SETTLED_REVIEW_RULE in first
    assert "está [revisado]" in _texts(fake.requests[1])
    assert result.attempts == 2 and result.auto_resolved == ["p-4"] and result.asked == ["p-5"]
    assert _notes(topic) == before


def test_without_settled_blocks_the_review_is_not_given_the_rule(
    tmp_vault: Vault, sync: GitSync
) -> None:
    plain = make_doubts_topic(tmp_vault)
    fake = FakeClaude().reply_tool(REVIEW_TOOL, review_reply(plain))

    _review(plain, sync, fake)

    assert SETTLED_REVIEW_RULE not in _texts(fake.requests[0])


# -- doubts the review did not see (#483) --------------------------------------------------------


def _later_doubt(topic: DoubtsTopic, kind: str, refs: list[str]) -> None:
    """An open doubt, `p-7`, added after the settled block was reviewed."""
    session = start_session(
        topic.vault, topic.subject, topic.topic, host="pc", protocol_version="1.1"
    )
    session.append_event("session.started", "user", {})
    session.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {
            "op": "add_pending",
            "pending_id": "p-7",
            "kind": kind,
            "text": "Lectura dudosa en la grabación.",
            "source_refs": refs,
        },
    )
    session.append_event("session.ended", "user", {})
    end_session(session)
    GitSync(topic.vault).checkpoint("later doubt")


def _settled(topic: DoubtsTopic) -> set[str]:
    return settled_blocks(topic.vault, topic.subject, topic.topic, _notes(topic))


def test_the_review_record_stores_the_cited_sources_and_the_doubts_open_then(
    topic: DoubtsTopic,
) -> None:
    [record] = [
        r.detail
        for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")
        if r.kind == "notes.reviewed"
    ]
    assert record is not None
    assert record["sources"][block_key(NOTATION)] == [f"sessions/{topic.session}"]
    assert record["open_doubts"] == ["p-4", "p-5"]


def test_a_later_doubt_on_a_source_the_block_already_cited_keeps_it_settled(
    topic: DoubtsTopic,
) -> None:
    _later_doubt(topic, "illegible", [transcript_id(topic, "00:00:10-00:00:15")])

    assert block_key(NOTATION) in _settled(topic)


def test_a_later_contradiction_on_a_source_the_block_cites_unsettles_it(
    topic: DoubtsTopic,
) -> None:
    _later_doubt(topic, "contradiction", [transcript_id(topic, "00:00:10-00:00:15"), B1])

    assert block_key(NOTATION) not in _settled(topic)


def test_a_later_doubt_on_a_source_cited_only_after_the_review_unsettles_it(
    topic: DoubtsTopic,
) -> None:
    notes = _notes(topic)
    cited = notes.replace(NOTATION, "Se escribe $f'(x)$.[^t2][^p4]") + f"[^p4]: {P4_FOOTNOTE}\n"
    s, t = topic.subject, topic.topic
    assert block_key(NOTATION) in settled_blocks(topic.vault, s, t, cited)

    _later_doubt(topic, "illegible", [P4])

    assert block_key(NOTATION) not in settled_blocks(topic.vault, s, t, cited)


def test_a_review_record_without_what_it_saw_counts_every_open_doubt(
    topic: DoubtsTopic,
) -> None:
    _later_doubt(topic, "illegible", [transcript_id(topic, "00:00:10-00:00:15")])
    # A record from before #483: no `sources`, no `open_doubts`; the latest review wins.
    record_reviewed(topic.vault, topic.subject, topic.topic, "student_edit", [block_key(NOTATION)])

    assert block_key(NOTATION) not in _settled(topic)


def _raising(doubt: dict[str, Any]) -> dict[str, Any]:
    """A valid incorporation of page 3 that also raises `doubt`."""
    return {
        "summary": "Añado la derivada de x² de la página 3",
        "ops": [
            {
                "op": "insert_after",
                "section": "definicion",
                "block": 2,
                "text": "La derivada de $x^2$ es $2x$.[^p3]",
            }
        ],
        "footnotes": [{"label": "p3", "definition": P3_FOOTNOTE}],
        "doubts": [doubt],
    }


def test_a_doubt_an_incorporation_raises_on_a_cited_source_keeps_the_block_settled(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    doubt = {
        "kind": "illegible",
        "text": "En la grabación no se oye bien la notación.",
        "question": "¿Qué dijo el profesor?",
        "suggestions": ["f'(x)"],
        "refs": [transcript_id(topic, "00:00:10-00:00:15")],
    }
    fake = FakeClaude().reply_tool(EDIT_TOOL, _raising(doubt), text="He añadido la derivada.")

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied and not result.errors and len(result.doubts) == 1
    assert block_key(NOTATION) in _settled(topic)


def test_a_contradiction_an_incorporation_raises_on_a_cited_source_unsettles_the_block(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    doubt = _contradiction(P3, transcript_id(topic, "00:00:10-00:00:15"))
    fake = FakeClaude().reply_tool(EDIT_TOOL, _raising(doubt), text="He añadido la derivada.")

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied and not result.errors and len(result.doubts) == 1
    assert block_key(NOTATION) not in _settled(topic)


def test_the_review_may_not_make_a_settled_block_cite_a_doubted_source(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    before = _notes(topic)
    [p4, p5] = _asks(topic)
    cite = {
        **p4,
        "action": "auto_resolve",
        "question": None,
        "suggestions": [],
        "resolution": "Pone «incremental».",
        "evidence": [{"source_id": P1, "quote": "cociente incremental"}],
        "edits": [
            {
                "op": "replace_block",
                "section": "definicion",
                "block": 2,
                "text": "Se escribe $f'(x)$.[^t2][^p1]",
            }
        ],
    }
    footnotes = [
        {"label": "p1", "definition": "[Apuntes, página 1](../sources/notes/page-001.jpg)"}
    ]
    fake = (
        FakeClaude()
        .reply_tool(REVIEW_TOOL, {"decisions": [cite, p5], "footnotes": footnotes})
        .reply_tool(REVIEW_TOOL, {"decisions": [{**cite, "edits": []}, p5]})
    )

    result = _review(topic, sync, fake)

    reask = _texts(fake.requests[1])
    assert f"no le añadas una nota al pie de {P1}, que tiene dudas abiertas" in reask
    assert result.attempts == 2 and result.auto_resolved == ["p-4"]
    assert _notes(topic) == before
