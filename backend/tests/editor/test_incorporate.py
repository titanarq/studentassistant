"""Incremental incorporation (`editor.incorporate`, #326): a few sources per editor request, the
state of each source, and "prepárame el tema" as sequential small batches."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

import pytest

from generate_topic import PAGE_1_TRANSCRIPTION, make_topic, valid_notes
from studentassistant.editor.direct_edit import save_student_edit
from studentassistant.editor.incorporate import (
    INCORPORATION_PROGRESS_KIND,
    NOTES_INCORPORATED_KIND,
    IncorporationResult,
    SourceSetAsideError,
    TooManySourcesError,
    UnknownSourceError,
    incorporate_pending,
    incorporate_sources,
    pending_batches,
    source_status,
)
from studentassistant.editor.notes_format import notes_revision, topic_source_resolver, validate
from studentassistant.editor.revise import (
    EDIT_TOOL,
    NOTES_CHANGED_NOTE,
    REPLY_DELTA,
    chat_history,
    undo_last_revision,
)
from studentassistant.llm import CostConfirmationRequiredError, FakeClaude, LLMRequest
from studentassistant.vault import (
    GitSync,
    Vault,
    put_source,
    read_notes,
    sources_directory,
    write_notes,
)

PAGE_3_TRANSCRIPTION = "Regla de la cadena: (f∘g)' = f'(g) · g'.\n"
BOOK_TRANSCRIPTION = "La derivada de un producto: (fg)' = f'g + fg'.\n"
P3 = "sources/notes/page-003.jpg"
P4 = "sources/notes/page-004.jpg"
B1 = "sources/book/page-001.jpg"
P3_FOOTNOTE = "[Apuntes, página 3](../sources/notes/page-003.jpg)"
B1_FOOTNOTE = "[Libro, página 1](../sources/book/page-001.jpg)"


@dataclass(frozen=True)
class Topic:
    vault: Vault
    subject: str
    topic: str
    session: str


def _make(vault: Vault, *, notes: bool = True) -> Topic:
    """`make_topic` plus page 3 (spoken about at 10-15 s), page 4 set aside as blurry and a book
    page; with `notes`, `valid_notes` (citing pages 1 and 2) is the current `apuntes.md`."""
    base = make_topic(vault)
    s, t = base.subject, base.topic
    notes_dir = sources_directory(vault, s, t, "notes")
    put_source(
        vault,
        s,
        t,
        "notes",
        "page.jpg",
        b"\xff\xd8 page three",
        {"session": base.session, "transcript_window": {"t_start": 10_000, "t_end": 15_000}},
    )
    (notes_dir / "page-003.md").write_text(PAGE_3_TRANSCRIPTION, encoding="utf-8")
    put_source(
        vault,
        s,
        t,
        "notes",
        "page.jpg",
        b"\xff\xd8 page four",
        {"session": base.session, "triage": {"status": "set_aside", "reasons": ["blurry"]}},
    )
    put_source(vault, s, t, "book", "page.jpg", b"\xff\xd8 book page", {})
    (sources_directory(vault, s, t, "book") / "page-001.md").write_text(
        BOOK_TRANSCRIPTION, encoding="utf-8"
    )
    if notes:
        write_notes(vault, s, t, valid_notes(base.session))
    GitSync(vault).checkpoint("fixture")
    return Topic(vault, s, t, base.session)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


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


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def _notes(topic: Topic) -> str:
    text = read_notes(topic.vault, topic.subject, topic.topic)
    assert text is not None
    return text


def _add_chain_rule(fake: FakeClaude, *, after: str = "definicion") -> FakeClaude:
    return fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Incorporo la regla de la cadena de la página 3",
            "ops": [
                {
                    "op": "add_section",
                    "after": after,
                    "level": 2,
                    "title": "Regla de la cadena",
                    "anchor": "cadena",
                    "text": "La regla de la cadena: $(f∘g)' = f'(g) · g'$.[^p3]",
                }
            ],
            "footnotes": [{"label": "p3", "definition": P3_FOOTNOTE}],
        },
        text="He añadido la regla de la cadena de la página 3.",
    )


def _incorporate(topic: Topic, sync: GitSync, fake: FakeClaude, ids: list[str], **kw: Any):
    return _run(
        incorporate_sources(
            topic.vault,
            topic.subject,
            topic.topic,
            ids,
            client=fake.client("editor"),
            sync=sync,
            **kw,
        )
    )


def _valid(topic: Topic, text: str) -> list[str]:
    resolver = topic_source_resolver(topic.vault, topic.subject, topic.topic)
    return validate(text, "estricto", resolver)


# -- the states -----------------------------------------------------------------------------------


def test_source_status_reads_each_state_in_catalogue_order(tmp_vault: Vault) -> None:
    topic = _make(tmp_vault)

    rows = source_status(topic.vault, topic.subject, topic.topic)

    assert [(r.source_id, r.state) for r in rows] == [
        ("sources/notes/page-001.jpg", "incorporada"),
        ("sources/notes/page-002.jpg", "incorporada"),
        (P3, "pendiente"),
        (P4, "apartada"),
        (B1, "pendiente"),
    ]
    assert rows[3].reason == "borrosa" and rows[3].number == 4
    assert rows[2].label == "la página 3" and rows[4].label == "la página 1 del libro"
    assert all(r.reason is None for r in rows if r.state != "apartada")


def test_without_notes_every_kept_source_is_pending(tmp_vault: Vault) -> None:
    topic = _make(tmp_vault, notes=False)
    states = [r.state for r in source_status(topic.vault, topic.subject, topic.topic)]
    assert states == ["pendiente", "pendiente", "pendiente", "apartada", "pendiente"]


# -- one incorporation ----------------------------------------------------------------------------


def test_one_page_into_existing_notes_sends_only_that_page(tmp_vault: Vault, sync: GitSync) -> None:
    topic = _make(tmp_vault)
    fake = _add_chain_rule(FakeClaude())
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    result = _incorporate(topic, sync, fake, [P3], on_event=on_event)

    assert result.applied and result.notes_changed and result.attempts == 1
    assert result.source_ids == [P3] and result.changed_sections == ["cadena"]
    notes = _notes(topic)
    assert "## Regla de la cadena {#cadena}" in notes and f"[^p3]: {P3_FOOTNOTE}" in notes
    assert "Se escribe $f'(x)$.[^t2]" in notes  # the rest is kept
    assert _valid(topic, notes) == []
    assert result.revision == notes_revision(notes)
    assert _git(topic.vault, "log", "-1", "--format=%s").strip() == (
        f"Apuntes de {topic.subject}/{topic.topic}: incorporada la página 3"
    )
    # Only the requested page: its transcription, what was said around it, the current notes.
    request = fake.requests[0]
    sent = _texts(request)
    assert PAGE_3_TRANSCRIPTION.strip() in sent
    assert PAGE_1_TRANSCRIPTION.strip() not in sent  # another page's transcription
    assert BOOK_TRANSCRIPTION.strip() not in sent
    assert "Se escribe f prima de x." in sent  # s-2, inside page 3's window
    assert "La derivada es el límite del cociente incremental." not in sent  # s-1, outside it
    assert "Mañana repasamos" not in sent  # no whole-topic transcript
    assert "Se escribe $f'(x)$.[^t2]" in sent and "#definicion" in sent  # notes + block map
    assert f"`{P3}`" in sent and "page-002.jpg`:" not in sent  # the catalogue of just page 3
    assert request.system[0]["text"].startswith("You are the tutor-editor")
    assert [kind for kind, _ in events] == [NOTES_INCORPORATED_KIND]
    assert "notes" not in events[0][1]


def test_one_page_into_empty_notes_starts_from_the_title(tmp_vault: Vault, sync: GitSync) -> None:
    topic = _make(tmp_vault, notes=False)
    fake = _add_chain_rule(FakeClaude(), after="")

    result = _incorporate(topic, sync, fake, [P3])

    assert result.applied
    notes = _notes(topic)
    assert notes.startswith("# Derivadas\n") and "{#cadena}" in notes
    assert _valid(topic, notes) == []
    assert "## Apuntes actuales" in _texts(fake.requests[0])
    assert "add_section" in _texts(fake.requests[0])  # the block map says how to grow it


def test_two_pages_in_one_request(tmp_vault: Vault, sync: GitSync) -> None:
    topic = _make(tmp_vault)
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        {
            "summary": "Incorporo la página 3 y el libro",
            "ops": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 2,
                    "text": "Regla de la cadena.[^p3]\n\nDerivada del producto.[^b1]",
                }
            ],
            "footnotes": [
                {"label": "p3", "definition": P3_FOOTNOTE},
                {"label": "b1", "definition": B1_FOOTNOTE},
            ],
        },
        text="Incorporo las dos.",
    )

    result = _incorporate(topic, sync, fake, [P3, B1])

    assert result.applied and result.source_ids == [P3, B1]
    sent = _texts(fake.requests[0])
    assert PAGE_3_TRANSCRIPTION.strip() in sent and BOOK_TRANSCRIPTION.strip() in sent
    assert "incorporadas la página 3 y la página 1 del libro" in _git(
        topic.vault, "log", "-1", "--format=%s"
    )
    states = {r.source_id: r.state for r in source_status(topic.vault, topic.subject, topic.topic)}
    assert states[P3] == states[B1] == "incorporada"


def test_an_uncited_source_is_sent_back_and_nothing_new_is_accepted(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = _make(tmp_vault)
    fake = (
        FakeClaude()
        .reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="No cambio nada.")
        .reply_tool(
            EDIT_TOOL,
            {"summary": "La página 3 no aporta nada nuevo", "ops": [], "nothing_new": [P3]},
            text="La página 3 ya está en tus apuntes.",
        )
    )

    result = _incorporate(topic, sync, fake, [P3])

    assert result.attempts == 2 and not result.errors
    assert not result.applied and not result.notes_changed and result.nothing_new == [P3]
    assert "no citan sources/notes/page-003.jpg" in _texts(fake.requests[1])
    assert _notes(topic) == valid_notes(topic.session)


def test_the_limit_and_set_aside_sources_are_refused_before_any_call(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = _make(tmp_vault)
    fake = FakeClaude()

    with pytest.raises(TooManySourcesError, match="pasos más pequeños"):
        _incorporate(
            topic,
            sync,
            fake,
            ["sources/notes/page-001.jpg", "sources/notes/page-002.jpg", P3, B1],
        )
    with pytest.raises(SourceSetAsideError) as refused:
        _incorporate(topic, sync, fake, [P4])
    assert str(refused.value) == (
        "La página 4 está apartada (borrosa); recupérala antes si quieres incorporarla."
    )
    with pytest.raises(UnknownSourceError):
        _incorporate(topic, sync, fake, ["sources/notes/page-009.jpg"])
    assert fake.requests == []


def test_a_student_save_mid_turn_is_kept_and_the_incorporation_redone(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = _make(tmp_vault)
    fake = _add_chain_rule(_add_chain_rule(FakeClaude()))
    line = "Lo que añado yo."
    saved: list[str] = []

    async def on_reply(kind: str, data: dict[str, Any]) -> None:
        if kind == REPLY_DELTA and data["attempt"] == 1 and not saved:
            current = await asyncio.to_thread(read_notes, topic.vault, topic.subject, topic.topic)
            assert current is not None
            edited = current.replace(
                "## 2. Próximo día {#proximo-dia}\n\n",
                f"## 2. Próximo día {{#proximo-dia}}\n\n{line}\n\n",
            )
            done = await save_student_edit(
                topic.vault, topic.subject, topic.topic, edited, notes_revision(current), sync=sync
            )
            saved.append(done.notes)

    result = _incorporate(topic, sync, fake, [P3], on_reply=on_reply)

    assert saved and result.applied and result.attempts == 2
    final = _notes(topic)
    assert f"{line}[^est]" in final and "{#cadena}" in final
    reask = _texts(fake.requests[1])
    assert NOTES_CHANGED_NOTE in reask and line in reask


def test_an_incorporation_is_a_chat_turn_and_can_be_undone(tmp_vault: Vault, sync: GitSync) -> None:
    topic = _make(tmp_vault)
    before = _notes(topic)
    result = _incorporate(topic, sync, _add_chain_rule(FakeClaude()), [P3])

    history = chat_history(topic.vault, topic.subject, topic.topic)
    turn = history.turns[-1]
    assert turn.kind == "incorporate" and turn.source_ids == [P3]
    assert turn.summary == result.summary and turn.commit == result.commit and turn.diff
    assert turn.message == "Incorpora la página 3." and history.can_undo

    undone = _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))

    assert undone.undone_commit == result.commit and _notes(topic) == before
    assert chat_history(topic.vault, topic.subject, topic.topic).turns[-1].undone


# -- the whole topic in small batches ------------------------------------------------------------


def _cite_all(fake: FakeClaude, labels: list[tuple[str, str, str]]) -> FakeClaude:
    """One incorporation answer adding one section citing each `(anchor, label, definition)`."""
    ops = []
    after = ""
    for anchor, label, _definition in labels:
        ops.append(
            {
                "op": "add_section",
                "after": after,
                "level": 2,
                "title": f"Sección {anchor}",
                "anchor": anchor,
                "text": f"Contenido de {anchor}.[^{label}]",
            }
        )
        after = anchor
    return fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": f"Incorporo {', '.join(a for a, _, _ in labels)}",
            "ops": ops,
            "footnotes": [{"label": lb, "definition": d} for _, lb, d in labels],
        },
        text="Hecho.",
    )


PAGE = "[Apuntes, página {n}](../sources/notes/page-00{n}.jpg)"


def test_pending_batches_follow_the_catalogue_order(tmp_vault: Vault) -> None:
    topic = _make(tmp_vault, notes=False)
    assert pending_batches(topic.vault, topic.subject, topic.topic, 2) == [
        ["sources/notes/page-001.jpg", "sources/notes/page-002.jpg"],
        [P3, B1],
    ]


class _Turns:
    def __init__(self) -> None:
        self.begun: list[list[str]] = []
        self.ended: list[IncorporationResult | BaseException | None] = []

    def begin(self, source_ids: list[str]) -> tuple[str | None, Any]:
        self.begun.append(source_ids)
        return f"turn-{len(self.begun)}", None

    def end(self, result: IncorporationResult | None, error: BaseException | None) -> None:
        self.ended.append(result if result is not None else error)


def test_the_whole_topic_runs_in_small_batches_then_is_tagged(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = _make(tmp_vault, notes=False)
    fake = _cite_all(
        _cite_all(
            FakeClaude(),
            [("uno", "p1", PAGE.format(n=1)), ("dos", "p2", PAGE.format(n=2))],
        ),
        [("tres", "p3", P3_FOOTNOTE), ("libro", "b1", B1_FOOTNOTE)],
    )
    progress: list[dict[str, Any]] = []
    turns = _Turns()

    result = _run(
        incorporate_pending(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor"),
            sync=sync,
            batch_size=2,
            on_progress=progress.append,
            turns=turns,
            detect_contradictions=False,
        )
    )

    assert len(fake.requests) == 2 and result.total == 4 and not result.stopped
    assert result.done == ["sources/notes/page-001.jpg", "sources/notes/page-002.jpg", P3, B1]
    assert turns.begun == [result.done[:2], result.done[2:]]
    assert [b.turn_id for b in result.batches] == ["turn-1", "turn-2"]
    first = _texts(fake.requests[0])
    assert PAGE_1_TRANSCRIPTION.strip() in first and PAGE_3_TRANSCRIPTION.strip() not in first
    assert progress == [
        {"done": 2, "total": 4, "source_ids": result.done[:2]},
        {"done": 4, "total": 4, "source_ids": result.done[2:]},
    ]
    assert INCORPORATION_PROGRESS_KIND == "incorporation.progress"
    log = [m for m in _git(topic.vault, "log", "--format=%s").splitlines() if "incorporada" in m]
    assert log == [
        f"Apuntes de {topic.subject}/{topic.topic}: incorporadas la página 3 y la página 1 del"
        " libro",
        f"Apuntes de {topic.subject}/{topic.topic}: incorporadas la página 1 y la página 2",
    ]
    assert result.version == 1 and result.tag == f"{topic.subject}/{topic.topic}/apuntes-v1"
    notes = _notes(topic)
    assert _valid(topic, notes) == [] and result.revision == notes_revision(notes)
    assert all(
        r.state != "pendiente" for r in source_status(topic.vault, topic.subject, topic.topic)
    )


def test_a_cost_cap_stops_the_batches_keeping_the_done_ones_and_a_new_run_resumes(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = _make(tmp_vault, notes=False)
    fake = _cite_all(
        FakeClaude(), [("uno", "p1", PAGE.format(n=1)), ("dos", "p2", PAGE.format(n=2))]
    ).fail(CostConfirmationRequiredError("over", cap="day", limit_usd=5.0, total_usd=5.1))
    turns = _Turns()

    stopped = _run(
        incorporate_pending(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor"),
            sync=sync,
            batch_size=2,
            turns=turns,
            detect_contradictions=False,
        )
    )

    assert stopped.stopped and stopped.done == [
        "sources/notes/page-001.jpg",
        "sources/notes/page-002.jpg",
    ]
    assert stopped.remaining == [P3, B1]
    assert "límite de gasto" in (stopped.warning or "")
    assert isinstance(turns.ended[-1], CostConfirmationRequiredError)
    assert stopped.version == 1  # what was done is a version

    again = _cite_all(FakeClaude(), [("tres", "p3", P3_FOOTNOTE), ("libro", "b1", B1_FOOTNOTE)])
    resumed = _run(
        incorporate_pending(
            topic.vault,
            topic.subject,
            topic.topic,
            client=again.client("editor"),
            sync=sync,
            batch_size=2,
            detect_contradictions=False,
        )
    )

    assert resumed.total == 2 and resumed.done == [P3, B1] and not resumed.stopped
    assert resumed.version == 2
    sent = _texts(again.requests[0])
    assert "Contenido de uno.[^p1]" in sent  # built on the kept batches


def test_a_cap_on_the_first_batch_is_raised(tmp_vault: Vault, sync: GitSync) -> None:
    topic = _make(tmp_vault, notes=False)
    fake = FakeClaude().fail(
        CostConfirmationRequiredError("over", cap="day", limit_usd=5.0, total_usd=5.1)
    )
    with pytest.raises(CostConfirmationRequiredError):
        _run(
            incorporate_pending(
                topic.vault,
                topic.subject,
                topic.topic,
                client=fake.client("editor"),
                sync=sync,
                detect_contradictions=False,
            )
        )
    assert read_notes(topic.vault, topic.subject, topic.topic) is None


def test_nothing_pending_makes_no_call(tmp_vault: Vault, sync: GitSync) -> None:
    topic = _make(tmp_vault, notes=False)
    fake = _cite_all(
        _cite_all(FakeClaude(), [("uno", "p1", PAGE.format(n=1)), ("dos", "p2", PAGE.format(n=2))]),
        [("tres", "p3", P3_FOOTNOTE), ("libro", "b1", B1_FOOTNOTE)],
    )
    kwargs: dict[str, Any] = {"sync": sync, "batch_size": 2, "detect_contradictions": False}
    _run(
        incorporate_pending(
            topic.vault, topic.subject, topic.topic, client=fake.client("editor"), **kwargs
        )
    )
    idle = FakeClaude()

    result = _run(
        incorporate_pending(
            topic.vault, topic.subject, topic.topic, client=idle.client("editor"), **kwargs
        )
    )

    assert result.total == 0 and idle.requests == [] and result.warning and result.version is None
