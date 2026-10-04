"""Switching a topic to Estudiar (#335): end the capture, label the "versión de estudio".

The study screen (`/subjects/{s}/topics/{t}/study`, epic #332) has one backend transition and one
read:

- `POST /api/subjects/{s}/topics/{t}/study`, no body -> `StudyState`. `switch_to_study` ends the
  topic's unended capture session if it has one -- the same `SessionService.end` the capture
  page's end runs (observer flush, digest, checkpoint and push) but **without** starting
  "prepárame el tema" -- then `editor.mark_study_version` labels the current notes (the latest
  `apuntes-vN` when the notes are exactly it, else a new tag), recorded in `study/version.yaml`.
  A **label, not a freeze**: nothing locks the notes afterwards. `study.marked` `{version, tag}`
  goes on the topic's workspace stream. It holds the topic's notes lock (`NotesGenerator.claim`,
  as a `TURN_HOLDER`) while it works, so it is refused with `409 notes_busy` while the notes are
  being prepared or an editor turn runs; no notes is a 409 with a Spanish `detail`, an unknown
  topic 404, a vault that cannot be opened 503.
- `GET /api/subjects/{s}/topics/{t}/study` -> `StudyState`: the latest label (`study_version`,
  or null), whether the notes still equal it (`study_current`) and each study option's state
  (`listo`, `desactualizado`, `sin_generar`) from `generators.materials_status`: its existing
  staleness (an edit after the material was built makes it "Desactualizado"). Reads only.

Neither route calls Claude, so both work without an `llm_transport`. The chat intent "ya está,
quiero estudiar" (request kind `study`) runs the same `switch_to_study` from the request consumer
(`assistant_requests.py`), which already holds the notes lock, and answers with a `StudyTurn`
whose `action` is `{"kind": "go_study", "path": ...}`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.revise import ChatRequestRef, TurnOrigin
from studentassistant.editor.versions import (
    NotesMissingError,
    StudyVersion,
    mark_study_version,
    read_study_label,
    study_current,
)
from studentassistant.generators import GeneratorRegistry, default_registry, materials_status
from studentassistant.generators.slides import KIND as SLIDES_KIND
from studentassistant.protocol import ErrorCode
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.errors import ApiError
from studentassistant.server.notes_routes import TURN_HOLDER, NotesGenerator
from studentassistant.server.sessions import (
    SessionConflictError,
    SessionService,
    UnknownSessionError,
    VaultUnavailableError,
)
from studentassistant.server.workspace import STUDY_MARKED, WorkspaceHub
from studentassistant.vault import (
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
    read_notes,
)

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

BASE = "/api/subjects/{subject_id}/topics/{topic_id}/study"
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = (
    "El editor está trabajando en los apuntes de este tema: espera a que termine para pasar a"
    " estudiar."
)

StudyOptionKey = Literal["esquema", "ejercicios", "examen", "quiz", "tarjetas", "diapositivas"]
StudyOptionState = Literal["listo", "desactualizado", "sin_generar"]
OPTION_KINDS: dict[StudyOptionKey, str] = {
    "esquema": "esquema",
    "ejercicios": "examen",
    "examen": "examen",
    "quiz": "quiz",
    "tarjetas": "flashcards",
    "diapositivas": SLIDES_KIND,
}
"""Each study option and the generator kind whose material it shows (ejercicios and examen are
both built from the `examen` material). `diapositivas` is the slides deck: its exported PDF/PPTX
are the `files` of the `diapositivas` artifact in `GET .../topics/{t}/generated`, downloaded from
`GET .../generated/files/{name}` (`generators_routes.py`), as the topic card page does."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StudyLabelRef(_Strict):
    version: int
    tag: str
    marked_at: datetime


class StudyOption(_Strict):
    key: StudyOptionKey
    kind: str
    """The generator kind of its material."""
    state: StudyOptionState
    stale_reason: str | None = None
    """Spanish, for the student; only when `desactualizado`."""
    notes_version: int | None = None
    """The `apuntes-vN` the material was built from (the newest tag then); none if not built."""


class StudyState(_Strict):
    subject: str
    topic: str
    study_version: StudyLabelRef | None = None
    study_current: bool = False
    """The current `apuntes.md` is still exactly the labelled one."""
    options: list[StudyOption]
    created_tag: bool = False
    """`POST` only: a new `apuntes-vN` tag was made for the label."""
    ended_session: str | None = None
    """`POST` only: the capture session it ended, if the topic had one open."""


class GoStudyAction(_Strict):
    kind: Literal["go_study"] = "go_study"
    path: str


class StudyTurn(_Strict):
    """The `turn.result` of a `study` request: what was done and where to go."""

    turn_id: str | None = None
    origin: TurnOrigin = "typed"
    request: ChatRequestRef | None = Field(
        default=None, description="The spoken request it answers (`origin` `voice`)."
    )
    message: str
    reply: str
    action: GoStudyAction
    study: StudyState


def study_path(subject_id: str, topic_id: str) -> str:
    """The web route of the topic's study screen."""
    return f"/subjects/{subject_id}/topics/{topic_id}/study"


def study_reply(state: StudyState) -> str:
    """The chat line of a switch to Estudiar, Spanish."""
    version = state.study_version.version if state.study_version is not None else None
    what = (
        f"marcado los apuntes v{version} como versión de estudio"
        if version is not None
        else "marcado los apuntes como versión de estudio"
    )
    if state.ended_session is not None:
        return f"He cerrado la captura y {what}."
    return f"He {what}."


def study_state(
    vault: Vault, subject_id: str, topic_id: str, *, registry: GeneratorRegistry = default_registry
) -> StudyState:
    """The topic's study label and the state of each study option (blocking; reads only).

    Raises the vault's errors for an unknown topic.
    """
    materials = materials_status(vault, subject_id, topic_id, registry=registry)
    label_file = read_study_label(vault, subject_id, topic_id)
    label = label_file.latest if label_file is not None else None
    notes = read_notes(vault, subject_id, topic_id)
    by_kind = {artifact.kind: artifact for artifact in materials.artifacts}
    options = []
    for key, kind in OPTION_KINDS.items():
        artifact = by_kind.get(kind)
        if artifact is None or not artifact.generated:
            options.append(StudyOption(key=key, kind=kind, state="sin_generar"))
            continue
        options.append(
            StudyOption(
                key=key,
                kind=kind,
                state="desactualizado" if artifact.stale else "listo",
                stale_reason=artifact.stale_reason if artifact.stale else None,
                notes_version=artifact.meta.notes.version if artifact.meta is not None else None,
            )
        )
    return StudyState(
        subject=subject_id,
        topic=topic_id,
        study_version=None
        if label is None
        else StudyLabelRef(version=label.version, tag=label.tag, marked_at=label.marked_at),
        study_current=study_current(label, notes),
        options=options,
    )


async def switch_to_study(
    sessions: SessionService,
    hub: WorkspaceHub,
    subject_id: str,
    topic_id: str,
    *,
    registry: GeneratorRegistry = default_registry,
    reason: Literal["button", "command"] = "button",
) -> StudyState:
    """End the topic's capture session (no notes generation) and label the study version.

    The caller holds the topic's notes lock. Publishes `study.marked` on the workspace stream.

    Raises:
        VaultUnavailableError: the vault cannot be opened.
        NotesMissingError: the topic has no notes (nothing is ended then).
        The vault's errors for an unknown topic.
    """
    vault = await sessions.open_vault()
    sync = sessions.sync
    if sync is None:  # pragma: no cover - the vault opens with its sync
        raise VaultUnavailableError("the vault has no sync")
    await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
    notes = await asyncio.to_thread(read_notes, vault, subject_id, topic_id)
    if notes is None or not notes.strip():
        raise NotesMissingError
    ended: str | None = None
    session_id = await sessions.open_session_of(None, subject_id, topic_id)
    if session_id is not None:
        try:
            await sessions.end(
                session_id, client_time_ms=time.time_ns() // 1_000_000, reason=reason
            )
            ended = session_id
        except (UnknownSessionError, SessionConflictError) as error:
            # Ended meanwhile (another client, a restart): nothing left to close.
            logger.info(
                "study switch of %s/%s: session %s: %s", subject_id, topic_id, session_id, error
            )
    marked: StudyVersion = await asyncio.to_thread(
        mark_study_version, vault, subject_id, topic_id, sync=sync
    )
    hub.publish(subject_id, topic_id, STUDY_MARKED, {"version": marked.version, "tag": marked.tag})
    state = await asyncio.to_thread(study_state, vault, subject_id, topic_id, registry=registry)
    return state.model_copy(update={"created_tag": marked.created_tag, "ended_session": ended})


def study_router() -> APIRouter:
    router = APIRouter()

    def registry_of(request: Request) -> GeneratorRegistry:
        return getattr(request.app.state, "generators", None) or default_registry

    async def open_topic(request: Request, subject_id: str, topic_id: str) -> Vault:
        sessions: SessionService = request.app.state.sessions
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        return vault

    @router.get(BASE)
    async def state(request: Request, subject_id: SubjectId, topic_id: TopicId) -> StudyState:
        vault = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(
            study_state, vault, subject_id, topic_id, registry=registry_of(request)
        )

    @router.post(
        BASE,
        responses={
            404: {"description": "Unknown topic."},
            409: {"description": "`notes_busy`, or the topic has no notes."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def switch(request: Request, subject_id: SubjectId, topic_id: TopicId) -> StudyState:
        await open_topic(request, subject_id, topic_id)
        generator: NotesGenerator | None = request.app.state.notes
        if generator is not None and not generator.claim(subject_id, topic_id, TURN_HOLDER):
            raise ApiError(409, BUSY_DETAIL, ErrorCode.NOTES_BUSY)
        try:
            return await switch_to_study(
                request.app.state.sessions,
                request.app.state.workspace,
                subject_id,
                topic_id,
                registry=registry_of(request),
            )
        except NotesMissingError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        finally:
            if generator is not None:
                generator.release(subject_id, topic_id)

    return router


__all__ = [
    "OPTION_KINDS",
    "GoStudyAction",
    "StudyLabelRef",
    "StudyOption",
    "StudyState",
    "StudyTurn",
    "study_path",
    "study_reply",
    "study_router",
    "study_state",
    "switch_to_study",
]
