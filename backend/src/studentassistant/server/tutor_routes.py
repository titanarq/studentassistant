"""The voice tutor API: ask the tutor about a topic, and read the questions asked so far.

Thin: the work is `studentassistant.editor.tutor`. The vault is opened through the
`SessionService`, like the editor chat routes (`revise_routes.py`), whose stream shape it shares:

- `POST /api/subjects/{s}/topics/{t}/tutor` (`TutorRequest`: `question`, `style`,
  `confirm_over_cap`) answers with a Server-Sent Events stream: the answer as it is written
  (`reply.delta`), then one `result` event with the `TutorAnswer` (`style`, `question`, `reply`,
  `refs`, `sections`, `warning`) or one `error`
  event (`status`, `detail`, and `code` when the failure has one and the caller speaks it). The
  question runs as its own task, so a client that goes away does not lose the recorded answer.
  It only reads the notes, so it does not take the notes lock of "prepárame el tema", the chat and
  the doubts; one question per topic runs at a time (its own lock). `style` `spoken` (the default:
  the capture page's and the Android app's voice tutor) uses the `editor` role; `written` (the
  study screen's question chat, #334) the role `[editor] study_chat_role` names (`editor` by
  default, `observer` to compare), which the ledger records.
  A `written` question that `study_requests.match_generation` recognises as a generation request
  ("hazme un quiz", #366) is not asked to the tutor: it runs `generate_material` (the code path
  of `POST .../generated/{kind}`) and streams `generation.started` `{kind, option, text}`, then
  `result` `{kind: "generation", option, material_kind, question, reply, items, warnings,
  study}` or `error`; every failure of it, no notes and a busy material included, is an `error`
  event. The turn is recorded (`editor.tutor.record_generation`) unless it failed.
  A **bare** match (`study_requests.needs_parameters`: "hazme un quiz", no count nor difficulty;
  #383) generates nothing yet: it streams one `result` `{kind: "clarification", option, style,
  question, reply, refs: [], sections: [], warning: null, defaults}` -- answer-shaped, `reply` the
  Spanish question back -- and records it (`editor.tutor.record_clarification`). While it is the
  topic's latest tutor turn, the next `written` message that is not itself a request is read by
  `study_requests.complete_parameters` ("5 difíciles", "vale"): a completion runs the generation
  stream above; anything else goes to the tutor, which drops the clarification.
- `GET /api/subjects/{s}/topics/{t}/tutor` -> `TutorHistory` (answer and generation turns).

Errors before the stream are HTTP errors, `{"detail": "..."}` in Spanish: no `llm_transport` 503,
a vault that cannot be opened 503, an unknown topic 404, no notes yet or another question of the
topic running 409, an empty question 422. In the stream, the `error` event carries the status the
same failure would have had: a reached cost cap 409 `cost_cap_reached` (until the body says
`confirm_over_cap`), a Claude refusal or failure 502.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from studentassistant.editor.revise import InvalidMessageError, RevisionError
from studentassistant.editor.tutor import (
    MAX_QUESTION_CHARS,
    TutorClarification,
    TutorGeneration,
    TutorHistory,
    TutorStyle,
    ask_tutor,
    pending_clarification,
    record_clarification,
    record_generation,
    tutor_history,
)
from studentassistant.generators import (
    GenerateResult,
    GenerationError,
    GeneratorRegistry,
    default_registry,
    read_artifact_meta,
)
from studentassistant.generators.exam import KIND as EXAM_KIND
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.protocol import ErrorCode
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.errors import ApiError, caller_speaks_error_codes, cost_cap_error
from studentassistant.server.generators_routes import MaterialGenerators, generate_material
from studentassistant.server.notes_routes import NotesGenerator
from studentassistant.server.revise_routes import sse
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.server.study_requests import (
    OPTION_TITLES,
    Clarification,
    GenerationOption,
    GenerationRequest,
    complete_parameters,
    match_generation,
    needs_parameters,
    result_reply,
    started_text,
)
from studentassistant.server.study_routes import study_state
from studentassistant.vault import (
    GitSync,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    VaultError,
    get_topic,
    read_notes,
)

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

UNAVAILABLE_DETAIL = "El tutor no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "El tutor ya está contestando otra pregunta de este tema."
NO_NOTES_DETAIL = "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos."
EMPTY_DETAIL = "Dime qué quieres preguntar."
REFUSED_DETAIL = "Claude se ha negado a contestar esa pregunta."
FAILED_DETAIL = "El tutor no ha podido contestar: Claude no ha respondido. Prueba más tarde."
INTERNAL_DETAIL = "El tutor no ha podido contestar por un error del servidor."
CAP_THEN = "Confirma para continuar igualmente."


class TutorRequest(BaseModel):
    """One question of the student; `confirm_over_cap` proceeds past a reached cost cap."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    style: TutorStyle = "spoken"
    confirm_over_cap: bool = False


GENERATION_STARTED = "generation.started"


def _error_of(error: BaseException) -> tuple[int, str, ErrorCode | None]:
    if isinstance(error, HTTPException):  # from `generate_material`
        code = error.code if isinstance(error, ApiError) else None
        return error.status_code, str(error.detail), code
    if isinstance(error, InvalidMessageError):
        return 422, str(error), None
    if isinstance(error, RevisionError):
        return 409, str(error), None
    if isinstance(error, CostConfirmationRequiredError):
        refused = cost_cap_error(error, CAP_THEN)
        return refused.status_code, refused.detail, refused.code
    if isinstance(error, RefusalError):
        return 502, REFUSED_DETAIL, None
    if isinstance(error, LLMError):
        return 502, FAILED_DETAIL, None
    return 500, INTERNAL_DETAIL, None


def _latest_notes_version(sync: GitSync, subject_id: str, topic_id: str) -> int | None:
    try:
        tags = sync.list_notes_tags(subject_id, topic_id)
    except Exception:  # only for the progress line
        logger.warning("could not list the notes tags of %s/%s", subject_id, topic_id)
        return None
    return tags[-1].version if tags else None


def _exam_counts(vault: Vault, subject_id: str, topic_id: str, kind: str) -> dict[str, int]:
    """An exam material's practice exercises (`e<n>`) and exam questions (`p<n>`), when read."""
    if kind != EXAM_KIND:
        return {}
    try:
        meta = read_artifact_meta(vault, subject_id, topic_id, kind)
    except (GenerationError, VaultError):
        return {}
    if meta is None:
        return {}
    return {
        "exercises": sum(1 for item in meta.items if item.item.startswith("e")),
        "questions": sum(1 for item in meta.items if item.item.startswith("p")),
    }


def tutor_router() -> APIRouter:
    router = APIRouter()
    running: set[tuple[str, str]] = set()
    # The running questions, so one whose client went away is not garbage-collected.
    tasks: set[asyncio.Task[None]] = set()

    async def open_topic(request: Request, subject_id: str, topic_id: str) -> tuple[Vault, GitSync]:
        sessions: SessionService = request.app.state.sessions
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        sync = sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL)
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        return vault, sync

    def respond(queue: asyncio.Queue[bytes | None]) -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def error_event(
        error: BaseException, subject_id: str, topic_id: str, speaks_codes: bool
    ) -> bytes:
        status, detail, code = _error_of(error)
        if status == 500:
            logger.exception("the tutor of %s/%s failed", subject_id, topic_id)
        elif status == 502:
            logger.warning("the tutor of %s/%s failed: %s", subject_id, topic_id, error)
        data: dict[str, Any] = {"status": status, "detail": detail}
        if code is not None and speaks_codes:
            data["code"] = code.value
        return sse("error", data)

    def generation_stream(
        request: Request,
        vault: Vault,
        sync: GitSync,
        subject_id: str,
        topic_id: str,
        body: TutorRequest,
        matched: GenerationRequest,
        materials: MaterialGenerators,
        registry: GeneratorRegistry,
    ) -> StreamingResponse:
        """A study chat generation request (#366): `generation.started`, then `result` (kind
        `generation`) or `error`. It takes the material's claim, not the tutor's lock."""
        speaks_codes = caller_speaks_error_codes(request)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        question = " ".join(body.question.split())

        async def run() -> None:
            try:
                version = await asyncio.to_thread(_latest_notes_version, sync, subject_id, topic_id)
                text = started_text(matched, version, registry=registry)
                queue.put_nowait(
                    sse(
                        GENERATION_STARTED,
                        {"kind": matched.kind, "option": matched.option, "text": text},
                    )
                )
                result: GenerateResult = await generate_material(
                    materials,
                    vault,
                    sync,
                    subject_id,
                    topic_id,
                    matched.kind,
                    registry=registry,
                    options=matched.options,
                    confirm_over_cap=body.confirm_over_cap,
                )
                counts = await asyncio.to_thread(
                    _exam_counts, vault, subject_id, topic_id, matched.kind
                )
                reply = result_reply(matched, counts, result.items)
                study = await asyncio.to_thread(
                    study_state, vault, subject_id, topic_id, registry=registry
                )
                await record_generation(
                    vault,
                    subject_id,
                    topic_id,
                    TutorGeneration(
                        subject=subject_id,
                        topic=topic_id,
                        question=question,
                        reply=reply,
                        option=matched.option,
                        material_kind=result.kind,
                        items=result.items,
                        warnings=result.warnings,
                        model=result.model,
                    ),
                    sync=sync,
                )
                queue.put_nowait(
                    sse(
                        "result",
                        {
                            "kind": "generation",
                            "option": matched.option,
                            "material_kind": result.kind,
                            "question": question,
                            "reply": reply,
                            "items": result.items,
                            "warnings": result.warnings,
                            "study": study.model_dump(mode="json"),
                        },
                    )
                )
            except Exception as error:
                queue.put_nowait(error_event(error, subject_id, topic_id, speaks_codes))
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return respond(queue)

    async def clarification_stream(
        vault: Vault,
        sync: GitSync,
        subject_id: str,
        topic_id: str,
        body: TutorRequest,
        clarification: Clarification,
    ) -> StreamingResponse:
        """A bare study chat generation request (#383): one `result` (kind `clarification`)."""
        question = " ".join(body.question.split())
        await record_clarification(
            vault,
            subject_id,
            topic_id,
            TutorClarification(
                subject=subject_id,
                topic=topic_id,
                question=question,
                reply=clarification.reply,
                option=clarification.option,
                material_kind=clarification.kind,
                defaults=clarification.defaults,
            ),
            sync=sync,
        )
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        queue.put_nowait(
            sse(
                "result",
                {
                    "kind": "clarification",
                    "option": clarification.option,
                    "style": "written",
                    "question": question,
                    "reply": clarification.reply,
                    "refs": [],
                    "sections": [],
                    "warning": None,
                    "defaults": clarification.defaults,
                },
            )
        )
        queue.put_nowait(None)
        return respond(queue)

    async def study_request(
        vault: Vault, subject_id: str, topic_id: str, text: str, registry: GeneratorRegistry
    ) -> tuple[GenerationRequest | None, Clarification | None]:
        """What a written message asks for: a generation to run, a question to ask back, or
        neither (the tutor answers it)."""
        matched = match_generation(text, registry=registry)
        if matched is not None:
            clarification = needs_parameters(matched, registry=registry)
            if clarification is None:
                return matched, None
            notes = await asyncio.to_thread(read_notes, vault, subject_id, topic_id)
            if not notes or not notes.strip():
                return matched, None  # nothing to ask about: the generation reports no notes
            return None, clarification
        pending = await asyncio.to_thread(pending_clarification, vault, subject_id, topic_id)
        if (
            pending is None
            or pending.option not in OPTION_TITLES
            or pending.material_kind not in registry
        ):
            return None, None
        asked = needs_parameters(
            GenerationRequest(
                kind=pending.material_kind, option=cast(GenerationOption, pending.option)
            ),
            registry=registry,
        )
        if asked is None:
            return None, None
        return complete_parameters(text, asked, registry=registry), None

    @router.get("/api/subjects/{subject_id}/topics/{topic_id}/tutor")
    async def history(request: Request, subject_id: SubjectId, topic_id: TopicId) -> TutorHistory:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(tutor_history, vault, subject_id, topic_id)

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/tutor")
    async def ask(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: TutorRequest
    ) -> StreamingResponse:
        generator: NotesGenerator | None = request.app.state.notes
        if generator is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        vault, sync = await open_topic(request, subject_id, topic_id)
        if not body.question.strip():
            raise HTTPException(status_code=422, detail=EMPTY_DETAIL)
        if body.style == "written":
            registry: GeneratorRegistry = (
                getattr(request.app.state, "generators", None) or default_registry
            )
            materials: MaterialGenerators | None = request.app.state.materials
            if materials is not None:
                matched, clarification = await study_request(
                    vault, subject_id, topic_id, body.question, registry
                )
                if clarification is not None:
                    return await clarification_stream(
                        vault, sync, subject_id, topic_id, body, clarification
                    )
                if matched is not None:
                    return generation_stream(
                        request,
                        vault,
                        sync,
                        subject_id,
                        topic_id,
                        body,
                        matched,
                        materials,
                        registry,
                    )
        notes = await asyncio.to_thread(read_notes, vault, subject_id, topic_id)
        if not notes or not notes.strip():
            raise HTTPException(status_code=409, detail=NO_NOTES_DETAIL)
        key = (subject_id, topic_id)
        if key in running:
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        running.add(key)

        speaks_codes = caller_speaks_error_codes(request)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def on_reply(kind: str, data: dict[str, Any]) -> None:
            queue.put_nowait(sse(kind, data))

        async def run() -> None:
            try:
                role = (
                    generator.settings.editor.study_chat_role
                    if body.style == "written"
                    else "editor"
                )
                client = get_client(
                    role,
                    settings=generator.settings,
                    transport=generator.transport,
                    ledger=LedgerBinding(vault, subject_id, topic_id),
                )
                answer = await ask_tutor(
                    vault,
                    subject_id,
                    topic_id,
                    body.question,
                    client=client,
                    style=body.style,
                    sync=sync,
                    on_reply=on_reply,
                    confirm_over_cap=body.confirm_over_cap,
                )
                queue.put_nowait(sse("result", answer.model_dump(mode="json")))
            except Exception as error:
                queue.put_nowait(error_event(error, subject_id, topic_id, speaks_codes))
            finally:
                running.discard(key)
                queue.put_nowait(None)

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return respond(queue)

    return router
