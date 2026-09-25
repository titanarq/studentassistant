"""The study materials API: which generators exist, a topic's materials, and generating one.

Thin: the work is `studentassistant.generators`. The routes open the vault through the
`SessionService` (the pulled vault and its `GitSync`), like the notes routes; the registry is
`app.state.generators` (`generators.default_registry` unless a test puts another).

- `GET /api/generators` -> `[GeneratorInfo]`: every registered kind, its Spanish title, version
  and the JSON Schema of its options.
- `GET .../topics/{topic_id}/generated` -> `MaterialsStatus`: every kind, generated or not, stale
  or not (never calls Claude, so it works without an `llm_transport`).
- `POST .../topics/{topic_id}/generated/{kind}`, optional body `{"options": {...},
  "confirm_over_cap": false}` -> `GenerateResult`. One generation per topic and kind at a time;
  needs an `llm_transport` (503 otherwise), a `generator` client bound to the topic's ledger.
- `GET .../topics/{topic_id}/generated/files/{name}` -> the bytes of `generated/<name>` as a
  download (`Content-Disposition: attachment; filename="<topic>-<file>"`), for the web's links to
  the Anki deck, the CSV...; 404 when there is no such file.

Errors, as `{"detail": "...", "code"?: "..."}` in Spanish: an unknown topic or kind 404, invalid
options 422, no notes yet or the same generation running 409, a reached cost cap 409
`cost_cap_reached` until the request says `confirm_over_cap`, a Claude failure or refusal 502, a
vault that cannot be opened 503.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from studentassistant.config import Settings
from studentassistant.generators import (
    GenerateResult,
    GeneratorRegistry,
    InvalidOptionsError,
    MaterialsStatus,
    NoNotesError,
    UnknownGeneratorError,
    materials_status,
    run_generator,
)
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    Transport,
    get_client,
)
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.errors import cost_cap_error
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    GitSync,
    NotesError,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
    read_generated,
)

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]
Kind = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]

UNAVAILABLE_DETAIL = "La generación de material no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "Ya se está generando ese material para este tema."
REFUSED_DETAIL = "Claude se ha negado a generar este material."
FAILED_DETAIL = "No se ha podido generar el material: Claude no ha respondido. Prueba más tarde."
CONFIRM_SENTENCE = "Confirma para generar el material igualmente."
NO_FILE_DETAIL = "No existe ese archivo en el material generado del tema."
MEDIA_TYPES = {
    ".apkg": "application/octet-stream",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".yaml": "application/yaml; charset=utf-8",
}

BASE = "/api/subjects/{subject_id}/topics/{topic_id}/generated"


class GeneratorInfo(BaseModel):
    kind: str
    title: str
    description: str = ""
    version: int
    options_schema: dict[str, Any]


class GenerateMaterialRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)
    confirm_over_cap: bool = False


class MaterialGenerators:
    """What the generate route needs: settings, transport and one lock per topic and kind."""

    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings = settings
        self.transport = transport
        self._running: set[tuple[str, str, str]] = set()

    def claim(self, subject_id: str, topic_id: str, kind: str) -> bool:
        key = (subject_id, topic_id, kind)
        if key in self._running:
            return False
        self._running.add(key)
        return True

    def release(self, subject_id: str, topic_id: str, kind: str) -> None:
        self._running.discard((subject_id, topic_id, kind))


def generators_router() -> APIRouter:
    router = APIRouter()

    def registry_of(request: Request) -> GeneratorRegistry:
        return request.app.state.generators

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

    @router.get("/api/generators")
    async def generators(request: Request) -> list[GeneratorInfo]:
        return [
            GeneratorInfo(
                kind=cls.kind,
                title=cls.title,
                description=cls.description,
                version=cls.version,
                options_schema=cls.options_model.model_json_schema(),
            )
            for cls in registry_of(request).classes()
        ]

    @router.get(BASE)
    async def materials(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> MaterialsStatus:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(
            materials_status, vault, subject_id, topic_id, registry=registry_of(request)
        )

    @router.post(BASE + "/{kind}")
    async def generate(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        kind: Kind,
        body: GenerateMaterialRequest | None = None,
    ) -> GenerateResult:
        service: MaterialGenerators | None = request.app.state.materials
        if service is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        registry = registry_of(request)
        if kind not in registry:
            raise HTTPException(
                status_code=404, detail=str(UnknownGeneratorError(kind, registry.kinds()))
            )
        vault, sync = await open_topic(request, subject_id, topic_id)
        if not service.claim(subject_id, topic_id, kind):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        body = body or GenerateMaterialRequest()
        try:
            client = get_client(
                "generator",
                settings=service.settings,
                transport=service.transport,
                ledger=LedgerBinding(vault, subject_id, topic_id),
            )
            return await run_generator(
                vault,
                subject_id,
                topic_id,
                kind,
                client=client,
                sync=sync,
                registry=registry,
                options=body.options,
                confirm_over_cap=body.confirm_over_cap,
                grounding_min_support=service.settings.generators.grounding_min_support,
            )
        except InvalidOptionsError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except NoNotesError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except CostConfirmationRequiredError as error:
            raise cost_cap_error(error, CONFIRM_SENTENCE) from error
        except RefusalError as error:
            raise HTTPException(status_code=502, detail=REFUSED_DETAIL) from error
        except LLMError as error:
            logger.warning(
                "generation of %s for %s/%s failed: %s", kind, subject_id, topic_id, error
            )
            raise HTTPException(status_code=502, detail=FAILED_DETAIL) from error
        finally:
            service.release(subject_id, topic_id, kind)

    @router.get(BASE + "/files/{name:path}")
    async def generated_file(
        request: Request, subject_id: SubjectId, topic_id: TopicId, name: str
    ) -> Response:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        try:
            data = await asyncio.to_thread(read_generated, vault, subject_id, topic_id, name)
        except NotesError as error:
            raise HTTPException(status_code=404, detail=NO_FILE_DETAIL) from error
        if data is None:
            raise HTTPException(status_code=404, detail=NO_FILE_DETAIL)
        file_name = name.rsplit("/", 1)[-1]
        suffix = "." + file_name.rsplit(".", 1)[-1] if "." in file_name else ""
        media_type = MEDIA_TYPES.get(suffix) or mimetypes.guess_type(file_name)[0]
        return Response(
            content=data,
            media_type=media_type or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{topic_id}-{file_name}"'},
        )

    return router
