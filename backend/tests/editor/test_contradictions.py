"""Every source kind in the editor's input, and contradictions between sources as doubts (#65)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from typing import Any

import pytest
import yaml

from multi_source_topic import (
    BOOK_DEFINITION,
    BOOK_ID,
    BOOK_TRANSCRIPTION,
    NOTES_DEFINITION,
    NOTES_ID,
    NOTES_TRANSCRIPTION,
    PDF_DEFINITION,
    PDF_ID,
    PDF_PAGE_ID,
    WEB_DEFINITION,
    WEB_ID,
    WEB_TEXT,
    MultiSourceTopic,
    contradiction,
    make_multi_source_topic,
    side,
    valid_notes,
    year_contradiction,
)
from studentassistant.editor.contradictions import (
    CONTRADICTIONS_DETECTED_KIND,
    MAX_REASKS,
    TOOL_NAME,
    ContradictionsResult,
    detect_contradictions,
)
from studentassistant.editor.doubts import (
    DECISION_TOOL,
    PENDING_QUESTION_KIND,
    DoubtAnswer,
    OpenSessionError,
    answer_doubt,
    list_doubts,
)
from studentassistant.editor.generate import (
    DETECTION_FAILED_WARNING,
    NOTES_GENERATED_KIND,
    OPEN_SESSION_WARNING,
    GenerationResult,
    generate_notes,
)
from studentassistant.editor.inputs import (
    DISAGREEMENT_RULE,
    STUDENT_LABEL,
    SUPPLEMENTARY_LABEL,
    assemble_input,
)
from studentassistant.llm import FakeClaude, LLMAPIError, LLMRequest, load_prompt
from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.vault import (
    GitSync,
    Vault,
    list_sessions,
    pending_review_path,
    read_conversation,
    read_notes,
    read_topic_events,
    start_session,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> MultiSourceTopic:
    made = make_multi_source_topic(tmp_vault)
    GitSync(tmp_vault).checkpoint("fixture")
    return made


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


def _detect(topic: MultiSourceTopic, sync: GitSync, fake: FakeClaude) -> ContradictionsResult:
    return _run(
        detect_contradictions(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor"),
            sync=sync,
            host="pc",
        )
    )


def _generate(topic: MultiSourceTopic, sync: GitSync, fake: FakeClaude) -> GenerationResult:
    return _run(
        generate_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor"),
            sync=sync,
            host="pc",
        )
    )


def _texts(request: LLMRequest) -> list[str]:
    return [b["text"] for b in request.messages[0]["content"] if b["type"] == "text"]


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def _head(vault: Vault) -> str:
    return _git(vault, "rev-parse", "HEAD").strip()


def _contradiction_items(topic: MultiSourceTopic) -> list[Any]:
    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    return [d for d in queue.items if d.item.kind == "contradiction"]


# -- every source kind in the input -----------------------------------------------------------


def test_every_source_kind_is_catalogued_and_sent(topic: MultiSourceTopic) -> None:
    assembled = assemble_input(
        topic.vault, topic.subject, topic.topic, prompt=load_prompt("editor_generate")
    )

    by_id = {c.source_id: c for c in assembled.catalogue}
    assert by_id[NOTES_ID].definition == NOTES_DEFINITION
    assert by_id[BOOK_ID].definition == BOOK_DEFINITION
    assert by_id[PDF_PAGE_ID].definition == PDF_DEFINITION
    assert by_id[WEB_ID].definition == WEB_DEFINITION
    catalogue = assembled.content[0]["text"]
    for definition in (NOTES_DEFINITION, BOOK_DEFINITION, PDF_DEFINITION, WEB_DEFINITION):
        assert f"`[^etiqueta]: {definition}`" in catalogue
    # Each source is sent as content: pages as their transcription, the PDF as a document, the
    # web page as its text.
    joined = "\n".join(b.get("text", "") for b in assembled.content)
    assert NOTES_TRANSCRIPTION.strip() in joined and BOOK_TRANSCRIPTION.strip() in joined
    assert WEB_TEXT.strip() in joined
    assert assembled.documents == [PDF_ID]
    assert assembled.sessions == [topic.session]


def test_the_input_labels_the_students_notes_and_the_supplementary_sources(
    topic: MultiSourceTopic,
) -> None:
    assembled = assemble_input(
        topic.vault, topic.subject, topic.topic, prompt=load_prompt("editor_generate")
    )

    roles = {c.source_id: c.role for c in assembled.catalogue}
    assert roles == {
        NOTES_ID: "student",
        BOOK_ID: "supplementary",
        "sources/pdf/page-001.pdf#page=1": "supplementary",
        "sources/pdf/page-001.pdf#page=2": "supplementary",
        WEB_ID: "supplementary",
    }
    catalogue = assembled.content[0]["text"]
    student = catalogue.index("### Apuntes del estudiante")
    supplementary = catalogue.index("### Fuentes complementarias")
    assert student < catalogue.index(NOTES_DEFINITION) < supplementary
    assert supplementary < catalogue.index(BOOK_DEFINITION)
    assert supplementary < catalogue.index(WEB_DEFINITION)
    assert DISAGREEMENT_RULE in catalogue
    # Each source in the body carries its label.
    texts = [b.get("text", "") for b in assembled.content]
    notes_heading = next(t for t in texts if t.startswith("## Páginas de los apuntes"))
    book_heading = next(t for t in texts if t.startswith("## Páginas del libro «Historia 2»"))
    assert STUDENT_LABEL in notes_heading and SUPPLEMENTARY_LABEL in book_heading
    assert SUPPLEMENTARY_LABEL in next(t for t in texts if t.startswith("### PDF"))
    assert SUPPLEMENTARY_LABEL in next(t for t in texts if t.startswith("### Web:"))


def test_the_prompts_say_the_students_notes_lead_and_no_disagreement_is_settled_silently() -> None:
    generate = load_prompt("editor_generate").content
    assert "give\n  the structure and the emphasis" in generate
    assert "cited with its own footnote" in generate
    assert "never settle it\n  silently" in generate
    revise = load_prompt("editor_revise").content
    assert "supplementary" in revise and "never settle it silently" in revise


# -- detection --------------------------------------------------------------------------------


def test_a_contradiction_is_raised_as_a_pending_doubt_with_both_sources(
    topic: MultiSourceTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(TOOL_NAME, {"contradictions": [year_contradiction()]})

    result = _detect(topic, sync, fake)

    assert len(result.raised) == 1 and result.warning is None and result.attempts == 1
    [doubt] = _contradiction_items(topic)
    assert doubt.item.id == result.raised[0] and doubt.item.is_open
    assert doubt.item.refs.sources == [NOTES_ID, BOOK_ID]
    assert doubt.item.created_by == "editor"
    assert doubt.question is not None
    assert [(o.source_id, o.says) for o in doubt.question.options] == [
        (NOTES_ID, "1769"),
        (BOOK_ID, "1765"),
    ]
    # Written in a review session, as the doubts are.
    sessions = list_sessions(topic.vault, topic.subject, topic.topic)
    assert sessions[-1].id == result.session_id and sessions[-1].kind == "review"
    assert sessions[-1].ended_at is not None
    events = [
        (e.kind, e.origin, e.payload)
        for sid, e in read_topic_events(topic.vault, topic.subject, topic.topic)
        if sid == result.session_id
    ]
    assert [(kind, origin) for kind, origin, _ in events] == [
        (STATE_OP_EVENT_KIND, "editor"),
        (PENDING_QUESTION_KIND, "editor"),
    ]
    op = events[0][2]
    assert op["op"] == "add_pending" and op["kind"] == "contradiction"
    assert op["source_refs"] == [NOTES_ID, BOOK_ID]
    pending = yaml.safe_load(
        pending_review_path(topic.vault, topic.subject, topic.topic).read_text()
    )
    assert pending["open_count"] == 1
    assert result.commit == _head(topic.vault)
    assert "1 contradicción nueva" in _git(topic.vault, "log", "-1", "--format=%s")

    [request] = fake.requests
    assert request.role == "editor"
    assert request.system[0]["text"] == load_prompt("editor_contradictions").content
    assert [tool["name"] for tool in request.tools] == [TOOL_NAME]
    assert _texts(request)[-1].startswith("## Tarea: buscar contradicciones")
    records = read_conversation(topic.vault, topic.subject, topic.topic, "editor")
    assert [r.kind for r in records] == [
        "context",
        "user",
        "assistant",
        "validation",
        CONTRADICTIONS_DETECTED_KIND,
    ]
    assert records[0].detail is not None and records[0].detail["reason"] == "contradictions"


def test_a_known_contradiction_is_not_raised_again(topic: MultiSourceTopic, sync: GitSync) -> None:
    _detect(
        topic, sync, FakeClaude().reply_tool(TOOL_NAME, {"contradictions": [year_contradiction()]})
    )
    head = _head(topic.vault)
    # The same sources in another order and other words: already known, as is a repeat within
    # one answer.
    again = contradiction(side(BOOK_ID, "1765"), side(NOTES_ID, "1769"), text="Otra redacción.")
    fake = FakeClaude().reply_tool(TOOL_NAME, {"contradictions": [again, again]})

    result = _detect(topic, sync, fake)

    assert result.raised == [] and result.duplicates == 2 and result.commit is None
    assert len(_contradiction_items(topic)) == 1
    assert _head(topic.vault) == head
    # The known contradiction is listed in the task, so the editor does not repeat it.
    assert "Contradicciones ya registradas" in _texts(fake.requests[0])[-1]


def test_an_invalid_answer_is_re_asked_then_dropped_with_a_warning(
    topic: MultiSourceTopic, sync: GitSync
) -> None:
    unknown = contradiction(side(NOTES_ID, "1769"), side("sources/book/page-009.jpg", "1765"))
    single = contradiction(side(NOTES_ID, "1769"), side(NOTES_ID, "1765"))
    fake = FakeClaude()
    for _ in range(MAX_REASKS + 1):
        fake.reply_tool(TOOL_NAME, {"contradictions": [unknown, single]})
    head = _head(topic.vault)

    result = _detect(topic, sync, fake)

    assert len(fake.requests) == MAX_REASKS + 1 and result.attempts == MAX_REASKS + 1
    assert result.raised == [] and result.dropped == 2
    assert result.warning is not None and "descartado 2" in result.warning
    reask = fake.requests[1].messages[-1]["content"]
    assert reask[0]["type"] == "tool_result" and reask[0]["is_error"] is True
    assert "sources/book/page-009.jpg no está en el catálogo" in reask[0]["content"]
    assert "al menos dos fuentes distintas" in reask[0]["content"]
    assert _contradiction_items(topic) == [] and _head(topic.vault) == head


def test_a_corrected_answer_is_accepted(topic: MultiSourceTopic, sync: GitSync) -> None:
    web = contradiction(side(WEB_ID, "fábricas textiles"), side("sources/web/002-x.md", "minas"))
    fake = FakeClaude().reply_tool(TOOL_NAME, {"contradictions": [web]})
    fake.reply_tool(
        TOOL_NAME,
        {
            "contradictions": [
                contradiction(side(PDF_PAGE_ID, "1769"), side(topic.transcript_id(), "1769?")),
            ]
        },
    )

    result = _detect(topic, sync, fake)

    assert result.attempts == 2 and len(result.raised) == 1 and result.warning is None
    [doubt] = _contradiction_items(topic)
    assert doubt.item.refs.sources == [PDF_PAGE_ID, topic.transcript_id()]


def test_no_contradiction_means_no_event_and_no_commit(
    topic: MultiSourceTopic, sync: GitSync
) -> None:
    sessions = list_sessions(topic.vault, topic.subject, topic.topic)
    head = _head(topic.vault)

    result = _detect(topic, sync, FakeClaude().reply_tool(TOOL_NAME, {"contradictions": []}))

    assert result.raised == [] and result.session_id is None and result.commit is None
    assert list_sessions(topic.vault, topic.subject, topic.topic) == sessions
    assert _head(topic.vault) == head


def test_an_open_session_is_refused_before_any_call(topic: MultiSourceTopic, sync: GitSync) -> None:
    start_session(topic.vault, topic.subject, topic.topic, host="pc", protocol_version="1.1")
    fake = FakeClaude()
    with pytest.raises(OpenSessionError):
        _detect(topic, sync, fake)
    assert fake.requests == []


def test_a_raised_contradiction_is_answered_by_the_doubts_flow(
    topic: MultiSourceTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    fake.reply_tool(TOOL_NAME, {"contradictions": [year_contradiction()]})
    generated = _generate(topic, sync, fake)
    [pending_id] = generated.contradictions

    answer = FakeClaude().reply_tool(
        DECISION_TOOL,
        {
            "resolution": "Vale 1769, como en los apuntes.",
            "edits": [
                {
                    "op": "replace_block",
                    "section": "watt",
                    "block": 1,
                    "text": "Watt perfeccionó la máquina de vapor en 1769.[^p1][^t1]",
                }
            ],
        },
    )
    result = _run(
        answer_doubt(
            topic.vault,
            topic.subject,
            topic.topic,
            pending_id,
            DoubtAnswer(source_id=NOTES_ID),
            client=answer.client("editor"),
            sync=sync,
            host="pc",
        )
    )

    assert result.status == "resolved" and result.notes_changed
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None and "(el libro dice 1765)" not in notes
    [doubt] = _contradiction_items(topic)
    assert doubt.item.status == "resolved" and doubt.outcome is not None
    assert doubt.outcome.chosen_source is not None
    assert doubt.outcome.chosen_source.source_id == NOTES_ID
    assert [o.source_id for o in doubt.outcome.discarded] == [BOOK_ID]


# -- in "prepárame el tema" ---------------------------------------------------------------------


def test_generation_detects_contradictions_after_writing_the_notes(
    topic: MultiSourceTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    fake.reply_tool(TOOL_NAME, {"contradictions": [year_contradiction()]})

    result = _generate(topic, sync, fake)

    assert not result.draft and result.version == 1 and result.warning is None
    [pending_id] = result.contradictions
    assert [d.item.id for d in _contradiction_items(topic)] == [pending_id]
    # The detection sees the notes just written.
    assert any("Versión actual de los apuntes" in t for t in _texts(fake.requests[1]))
    records = read_conversation(topic.vault, topic.subject, topic.topic, "editor")
    reasons = [r.detail["reason"] for r in records if r.kind == "context" and r.detail]
    assert reasons == ["generate", "contradictions"]
    assert records[-1].kind == NOTES_GENERATED_KIND
    assert records[-1].detail is not None and records[-1].detail["contradictions"] == [pending_id]


def test_a_failed_detection_keeps_the_notes(topic: MultiSourceTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    fake.fail(LLMAPIError("bad request", status_code=400))

    result = _generate(topic, sync, fake)

    assert not result.draft and result.version == 1 and result.contradictions == []
    assert result.warning == DETECTION_FAILED_WARNING
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)
    assert sync.list_notes_tags(topic.subject, topic.topic)[0].version == 1


def test_an_open_session_skips_the_detection_with_a_warning(
    topic: MultiSourceTopic, sync: GitSync
) -> None:
    start_session(topic.vault, topic.subject, topic.topic, host="pc", protocol_version="1.1")
    fake = FakeClaude().reply_text(valid_notes(topic.session))

    result = _generate(topic, sync, fake)

    assert not result.draft and result.warning == OPEN_SESSION_WARNING
    assert len(fake.requests) == 1
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)


def test_a_draft_is_not_searched_for_contradictions(topic: MultiSourceTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    for _ in range(3):
        fake.reply_text("# La máquina de vapor\n\nSin fuentes.\n")

    result = _generate(topic, sync, fake)

    assert result.draft and result.contradictions == [] and len(fake.requests) == 3
