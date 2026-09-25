"""Contradictions between sources: the editor finds them and leaves them as pending doubts.

The notes of a topic come from sources of several kinds -- the student's notes, textbook pages,
PDFs, web pages, the transcript -- and two of them may disagree on a fact (1769 in the notes,
1765 in the book). The editor never settles that silently (ADR-0005, VISION §5): after a version
of the notes is written, `detect_contradictions` asks the `editor` role (tool
`report_contradictions`, prompt `editor_contradictions`) over `assemble_input` with a task
instruction, so the sources are read from the same cached input as every other editor task.

Each reported contradiction has a Spanish `text`, a `question` and at least two `sides` -- each a
citable `source_id` of the catalogue (or a transcript span of one of the topic's sessions) and
what it says. An answer citing an unknown source, with fewer than two distinct sources or with an
empty text is sent back with the Spanish error list at most `MAX_REASKS` times; past that the
invalid contradictions are dropped (the valid ones of the last answer are kept) with a Spanish
`warning`.

Every accepted contradiction becomes, in a **review session** of the topic (as `doubts.py` writes
its events; `OpenSessionError` while the topic has an unended session, checked before any call),
an `observer.state_op` `add_pending` event (origin `editor`, `kind: contradiction`, `source_refs`
with every side's source) followed by a `pending.question` event whose `options` are the sides,
so the doubts flow can answer it at once (`answer_doubt` with `source_id`). A contradiction whose
set of sources equals that of an existing `contradiction` item of the topic (open or closed), or
of an earlier one of the same answer, is not added again. Then `review/pending.yaml` and the
snapshot are regenerated and everything is committed once (`Contradicciones en <s>/<t>: ...`).
No contradiction: no event, no commit.

The call is recorded in `conversations/editor.jsonl` (`context` with `reason` `contradictions`,
`user`, `assistant`, `validation`, then `contradictions.detected` with the result).
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import Field

from studentassistant.editor.doubts import (
    MAX_REASKS,
    PENDING_QUESTION_KIND,
    DoubtQuestion,
    SourceOption,
    _assemble_with,
    _citable,
    _Conversation,
    _Event,
    _require_no_open_session,
    _state,
    _Strict,
    _Task,
    _write,
)
from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    DigestReader,
    EditorInput,
)
from studentassistant.llm import LLMClient
from studentassistant.observer import STATE_OP_EVENT_KIND, AddPending, TopicState, op_payload
from studentassistant.vault import GitSync, Vault

PROMPT_NAME = "editor_contradictions"
TOOL_NAME = "report_contradictions"
REASON = "contradictions"
CONTRADICTIONS_DETECTED_KIND = "contradictions.detected"
MIN_SIDES = 2

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- what the editor answers ---------------------------------------------------------------------


class ContradictionSide(_Strict):
    """One side of a contradiction: a citable source and what it says."""

    source_id: str = Field(
        description="As the catalogue names it, or sessions/<id>#t=HH:MM:SS-HH:MM:SS."
    )
    says: str = Field(description="What this source says, briefly (Spanish).")


class Contradiction(_Strict):
    """One disagreement between sources of the topic."""

    text: str = Field(description="The disagreement, one Spanish sentence for the student.")
    question: str = Field(description="The Spanish question to ask the student.")
    sides: list[ContradictionSide] = Field(description="One per source in conflict, at least two.")


class ContradictionsOutput(_Strict):
    """The input of the `report_contradictions` tool."""

    contradictions: list[Contradiction] = Field(default_factory=list)


class ContradictionsResult(_Strict):
    """What one detection did."""

    subject: str
    topic: str
    raised: list[str] = Field(default_factory=list, description="Pending ids added.")
    duplicates: int = Field(default=0, description="Contradictions already known, not re-added.")
    dropped: int = Field(default=0, description="Invalid contradictions left out.")
    session_id: str | None = None
    commit: str | None = None
    attempts: int = 0
    warning: str | None = None
    model: str | None = None


# -- checks ---------------------------------------------------------------------------------------


def contradiction_errors(
    contradiction: Contradiction, number: int, assembled: EditorInput
) -> list[str]:
    """Spanish errors of one reported contradiction (`number` from 1); empty when it is valid."""
    where = f"Contradicción {number}"
    errors: list[str] = []
    if not contradiction.text.strip():
        errors.append(f"{where}: falta la descripción (text).")
    if not contradiction.question.strip():
        errors.append(f"{where}: falta la pregunta para el estudiante (question).")
    for side in contradiction.sides:
        if not _citable(assembled, side.source_id):
            errors.append(
                f"{where}: la fuente {side.source_id} no está en el catálogo ni es un fragmento"
                " de una sesión del tema."
            )
        if not side.says.strip():
            errors.append(f"{where}: di qué dice la fuente {side.source_id}.")
    if len({side.source_id for side in contradiction.sides}) < MIN_SIDES:
        errors.append(
            f"{where}: una contradicción necesita al menos dos fuentes distintas; si solo hay una,"
            " no es una contradicción."
        )
    return errors


def _check(value: ContradictionsOutput, assembled: EditorInput) -> list[str]:
    return [
        error
        for number, contradiction in enumerate(value.contradictions, start=1)
        for error in contradiction_errors(contradiction, number, assembled)
    ]


def _known_source_sets(state: TopicState) -> list[frozenset[str]]:
    return [
        frozenset(item.refs.sources)
        for item in state.pending.values()
        if item.kind == "contradiction"
    ]


def _instruction(state: TopicState) -> str:
    known = [item for item in state.pending.values() if item.kind == "contradiction"]
    lines = [
        f"## Tarea: buscar contradicciones entre las fuentes (herramienta `{TOOL_NAME}`)",
        "",
        "Compara lo que dicen las fuentes del tema (apuntes, libro, PDF, web y transcripción) y"
        " da cada dato en que dos o más de ellas no coinciden, con las fuentes de cada versión."
        " Si no hay ninguna, llama a la herramienta con la lista vacía.",
    ]
    if known:
        lines += ["", "Contradicciones ya registradas (no las repitas):"]
        lines += [
            f"- {item.id} [{item.status}] {item.text} ({'; '.join(item.refs.sources)})"
            for item in known
        ]
    return "\n".join(lines) + "\n"


# -- the call --------------------------------------------------------------------------------------


async def detect_contradictions(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    client: LLMClient,
    sync: GitSync,
    host: str | None = None,
    digest: DigestReader | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> ContradictionsResult:
    """Ask the editor for the contradictions between the topic's sources and raise them as
    `contradiction` pending doubts (see the module docstring).

    Raises:
        OpenSessionError: the topic has an unended session; nothing sent or written.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing
            written but the conversation records.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    await asyncio.to_thread(_require_no_open_session, vault, subject_slug, topic_slug)
    state = await asyncio.to_thread(_state, vault, subject_slug, topic_slug)
    assembled, prompt_hash = await _assemble_with(
        PROMPT_NAME,
        vault,
        subject_slug,
        topic_slug,
        _instruction(state),
        digest,
        max_page_images,
        max_attachment_bytes,
    )
    task = _Task(
        client,
        assembled,
        _Conversation(vault, subject_slug, topic_slug, clock),
        prompt_hash,
        REASON,
        confirm_over_cap,
    )
    last: list[ContradictionsOutput] = []

    def check(value: ContradictionsOutput) -> list[str]:
        last[:] = [value]
        return _check(value, assembled)

    value, attempts, model, _errors = await task.run(
        ContradictionsOutput,
        TOOL_NAME,
        "Report every contradiction between the topic's sources, each with the conflicting"
        " sources and what each one says; an empty list when there is none.",
        check,
    )

    reported = value.contradictions if value is not None else []
    dropped = 0
    warning = None
    if value is None and last:
        reported = []
        for number, contradiction in enumerate(last[0].contradictions, start=1):
            if contradiction_errors(contradiction, number, assembled):
                dropped += 1
            else:
                reported.append(contradiction)
        warning = (
            "El editor ha señalado contradicciones entre tus fuentes que no ha sabido citar bien;"
            f" se han descartado {dropped}."
        )

    known = _known_source_sets(state)
    events: list[_Event] = []
    raised: list[str] = []
    duplicates = 0
    for contradiction in reported:
        sources = list(dict.fromkeys(side.source_id for side in contradiction.sides))
        if frozenset(sources) in known:
            duplicates += 1
            continue
        known.append(frozenset(sources))
        pending_id = f"contradiccion-{uuid.uuid4().hex[:12]}"
        op = AddPending(
            pending_id=pending_id,
            kind="contradiction",
            text=contradiction.text.strip(),
            source_refs=sources,
        )
        question = DoubtQuestion(
            pending_id=pending_id,
            question=contradiction.question.strip(),
            options=[
                SourceOption(source_id=side.source_id, says=side.says.strip())
                for side in contradiction.sides
            ],
        )
        events.append(_Event(STATE_OP_EVENT_KIND, "editor", op_payload(op)))
        events.append(
            _Event(
                PENDING_QUESTION_KIND,
                "editor",
                question.model_dump(mode="json", exclude={"asked_at"}),
            )
        )
        raised.append(pending_id)

    session_id = commit = None
    if events:
        count = len(raised)
        noun = "contradicción nueva" if count == 1 else "contradicciones nuevas"
        session_id, commit = await asyncio.to_thread(
            _write,
            vault,
            sync,
            subject_slug,
            topic_slug,
            host=host or socket.gethostname(),
            events=events,
            notes=None,
            message=f"Contradicciones en {subject_slug}/{topic_slug}: {count} {noun} entre fuentes",
        )
    result = ContradictionsResult(
        subject=subject_slug,
        topic=topic_slug,
        raised=raised,
        duplicates=duplicates,
        dropped=dropped,
        session_id=session_id,
        commit=commit,
        attempts=attempts,
        warning=warning,
        model=model,
    )
    await task.conversation.record(
        CONTRADICTIONS_DETECTED_KIND,
        model=model,
        prompt_hash=prompt_hash,
        detail=result.model_dump(mode="json"),
    )
    sync.note_change()  # the conversation's last line, committed by the sync loop
    return result


__all__ = [
    "CONTRADICTIONS_DETECTED_KIND",
    "MAX_REASKS",
    "PROMPT_NAME",
    "TOOL_NAME",
    "Contradiction",
    "ContradictionSide",
    "ContradictionsOutput",
    "ContradictionsResult",
    "contradiction_errors",
    "detect_contradictions",
]
