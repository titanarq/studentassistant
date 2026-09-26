"""'Prepárame el tema': the `editor` role (Opus) writes the first version of a topic's notes.

`generate_notes` assembles the topic (`inputs.assemble_input`), asks the editor for the whole of
`notes/apuntes.md` and checks the answer with the provenance validator (ADR-0005). A document that
fails it is sent back with the list of errors, at most `MAX_REASKS` times; the answer is never
patched. Then:

- **valid**: written as `notes/apuntes.md` (`vault.write_notes`), committed and tagged as the
  topic's next version (`GitSync.create_notes_tag`: `<subject>/<topic>/apuntes-vN`, `N` one past
  the highest, so a regeneration makes the next one);
- **still invalid**: written as the draft `notes/borrador.md` (`vault.write_notes_draft`) and
  committed without a tag, with the validator's errors as the warning; the last valid
  `apuntes.md`, if any, is left as it was.

After a valid version, the editor looks for contradictions between the topic's sources
(`contradictions.detect_contradictions`) and raises them as `contradiction` pending doubts; that
second call failing, or an unended session of the topic, is only a warning of the result.

Either way a `notes.generated` event is emitted (`on_event`, which the server publishes on the bus
when a session of the topic is attached) and recorded in the editor's conversation
(`conversations/editor.jsonl`), which keeps every call: a `context` record (model, prompt hash,
what the input held), each `user` turn (with images and PDFs as references to their sources) and
`assistant` answer (with usage), each `validation` result and the final `notes.generated`.

Every call streams (the llm client always does, which is what allows the editor's long answers)
and is capped and recorded in the topic's cost ledger through the client's `LedgerBinding`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from studentassistant.editor.contradictions import detect_contradictions as _detect
from studentassistant.editor.doubts import OpenSessionError
from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    DigestReader,
    EditorInput,
    assemble_input,
)
from studentassistant.editor.notes_format import notes_revision, topic_source_resolver, validate
from studentassistant.llm import LLMClient, LLMResponse, RefusalError, load_prompt
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Vault,
    append_conversation_record,
    read_notes,
    write_notes,
    write_notes_draft,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "editor_generate"
CONVERSATION_NAME = "editor"
NOTES_GENERATED_KIND = "notes.generated"
MAX_REASKS = 2
"""How many times a document that fails the validator is sent back before it is kept as a draft."""

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
Clock = Callable[[], datetime]

_FENCE = re.compile(r"\A\s*```(?:markdown|md)?[ \t]*\n(?P<body>.*?)\n```\s*\Z", re.DOTALL)
TRUNCATED_ERROR = (
    "El documento quedó cortado antes de terminar (se alcanzó el límite de longitud de la"
    " respuesta): escribe el documento completo."
)


class GenerationResult(BaseModel):
    """What one 'prepárame el tema' produced; also the `notes.generated` payload."""

    subject: str
    topic: str
    draft: bool = Field(description="True when the notes did not pass the validator.")
    path: str = Field(description="The vault-relative file written.")
    version: int | None = Field(default=None, description="`N` of `apuntes-vN`; none for a draft.")
    tag: str | None = None
    commit: str | None = None
    attempts: int = Field(description="Editor calls made (1 + re-asks).")
    errors: list[str] = Field(
        default_factory=list, description="The validator's errors of the kept draft (Spanish)."
    )
    warning: str | None = Field(default=None, description="Spanish, for the student.")
    contradictions: list[str] = Field(
        default_factory=list,
        description="Ids of the `contradiction` pending doubts raised after writing the notes.",
    )
    model: str
    revision: str | None = Field(
        default=None,
        description="The revision (`notes_revision`) of `apuntes.md` after the generation; for a"
        " draft, of the notes left as they were (`None` when there are none).",
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def notes_text(response: LLMResponse) -> str:
    """The document of an answer: its text, without a code fence around it, ending in a newline."""
    text = response.text.strip()
    fenced = _FENCE.match(text)
    if fenced is not None:
        text = fenced["body"].strip()
    return text + "\n" if text else ""


def _usage(response: LLMResponse) -> dict[str, int]:
    return response.usage.model_dump()


def _reask(errors: list[str]) -> str:
    listing = "\n".join(f"- {error}" for error in errors)
    return (
        "El validador de procedencia ha rechazado el documento por estos motivos:\n\n"
        f"{listing}\n\nCorrígelos y responde otra vez con el documento completo de"
        " `notes/apuntes.md` y nada más. No borres contenido de los apuntes para evitar un error:"
        " cítalo."
    )


async def generate_notes(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    client: LLMClient,
    sync: GitSync,
    digest: DigestReader | None = None,
    on_event: EventSink | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    detect_contradictions: bool = True,
    host: str | None = None,
) -> GenerationResult:
    """Write the topic's notes with the editor, validate, save, commit and tag them.

    `client` is an `editor` client (`get_client("editor", ledger=LedgerBinding(...))`; tests use
    `FakeClaude`); `sync` the vault's `GitSync`; `digest` reads the topic digest (#56);
    `on_event(kind, payload)` receives the `notes.generated` event.

    After a valid version is written, and when `detect_contradictions` is true and the topic has
    at least two citable sources (sessions included), the editor looks for contradictions between
    the sources (`contradictions.detect_contradictions`, a second call); the ids it raises are in
    `contradictions`. A failure there, or an unended session of the topic, never loses the notes:
    it is a Spanish `warning` of the result.

    Raises:
        CostConfirmationRequiredError: a cost cap is reached and `confirm_over_cap` is false;
            nothing was sent or written.
        RefusalError: the editor declined.
        LLMError: any other Claude failure once the client's retries are spent; nothing written.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    prompt = load_prompt(PROMPT_NAME)
    assembled: EditorInput = await asyncio.to_thread(
        assemble_input,
        vault,
        subject_slug,
        topic_slug,
        prompt=prompt,
        digest=digest,
        max_page_images=max_page_images,
        max_attachment_bytes=max_attachment_bytes,
    )
    source_exists = topic_source_resolver(vault, subject_slug, topic_slug)

    async def record(kind: str, **fields: Any) -> None:
        entry = ConversationRecord(time=clock(), kind=kind, **fields)
        try:
            await asyncio.to_thread(
                append_conversation_record,
                vault,
                subject_slug,
                topic_slug,
                CONVERSATION_NAME,
                entry,
            )
        except Exception:
            # The conversation is a record, not the product: losing a line must not lose the notes.
            logger.exception(
                "could not record the editor conversation of %s/%s", subject_slug, topic_slug
            )

    messages: list[dict[str, Any]] = [{"role": "user", "content": assembled.content}]

    text = ""
    errors: list[str] = []
    model = client.model
    attempts = 0
    for attempt in range(1, MAX_REASKS + 2):
        attempts = attempt
        response = await client.create(
            messages,
            system=assembled.system,
            prompt_hash=prompt.hash,
            confirm_over_cap=confirm_over_cap,
        )
        model = response.model or model
        if attempt == 1:
            # Only once the first call went through (a cost cap refuses before sending anything).
            await record(
                "context",
                model=client.model,
                prompt_hash=prompt.hash,
                detail={"reason": "generate", **assembled.summary()},
            )
            await record(
                "user",
                message={"role": "user", "content": assembled.record_content},
                model=client.model,
            )
        await record(
            "assistant",
            message=response.assistant_turn(),
            model=model,
            prompt_hash=prompt.hash,
            usage=_usage(response),
        )
        if response.stop_reason == "refusal":
            raise RefusalError("the editor declined to write the notes")
        text = notes_text(response)
        errors = await asyncio.to_thread(validate, text, assembled.fidelity_mode, source_exists)
        if response.stop_reason == "max_tokens":
            errors = [TRUNCATED_ERROR, *errors]
        if not text:
            errors = ["La respuesta no contiene ningún documento.", *errors]
        await record("validation", detail={"attempt": attempt, "errors": errors})
        if not errors:
            break
        if attempt > MAX_REASKS:
            break
        reask = {"role": "user", "content": [{"type": "text", "text": _reask(errors)}]}
        messages = [*messages, response.assistant_turn(), reask]
        await record("user", message=reask, model=model)

    result = await asyncio.to_thread(
        _save, vault, sync, subject_slug, topic_slug, assembled.topic_title, text, errors
    )
    result = result.model_copy(update={"attempts": attempts, "model": model})
    if (
        detect_contradictions
        and not result.draft
        and len(assembled.catalogue) + len(assembled.sessions) >= 2
    ):
        result = await _with_contradictions(
            result,
            vault,
            subject_slug,
            topic_slug,
            client=client,
            sync=sync,
            host=host,
            digest=digest,
            confirm_over_cap=confirm_over_cap,
            clock=clock,
            max_page_images=max_page_images,
            max_attachment_bytes=max_attachment_bytes,
        )
    payload = result.model_dump(mode="json")
    await record(NOTES_GENERATED_KIND, model=model, prompt_hash=prompt.hash, detail=payload)
    sync.note_change()
    if on_event is not None:
        try:
            await on_event(NOTES_GENERATED_KIND, payload)
        except Exception:
            logger.exception(
                "could not publish notes.generated for %s/%s", subject_slug, topic_slug
            )
    return result


OPEN_SESSION_WARNING = (
    "Los apuntes están guardados, pero no se han buscado contradicciones entre tus fuentes porque"
    " el tema tiene una sesión sin terminar: termínala y vuelve a preparar el tema."
)
DETECTION_FAILED_WARNING = (
    "Los apuntes están guardados, pero no se han podido buscar contradicciones entre tus fuentes."
)


async def _with_contradictions(
    result: GenerationResult,
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    **options: Any,
) -> GenerationResult:
    """`result` with the contradictions raised after it, or a warning when that failed."""
    try:
        found = await _detect(vault, subject_slug, topic_slug, **options)
    except OpenSessionError:
        return result.model_copy(update={"warning": OPEN_SESSION_WARNING})
    except Exception:
        # The notes are written and committed: nothing of the detection may lose them.
        logger.exception("contradiction detection failed for %s/%s", subject_slug, topic_slug)
        return result.model_copy(update={"warning": DETECTION_FAILED_WARNING})
    return result.model_copy(update={"contradictions": found.raised, "warning": found.warning})


def _save(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    title: str,
    text: str,
    errors: list[str],
) -> GenerationResult:
    """Write the notes or the draft and commit (and tag) them; blocking, in a worker thread."""
    root = vault.path
    if not errors:
        path = write_notes(vault, subject_slug, topic_slug, text)
        sync.note_change()
        existing = sync.list_notes_tags(subject_slug, topic_slug)
        version = (existing[-1].version if existing else 0) + 1
        message = f"Apuntes v{version} de {subject_slug}/{topic_slug}: {title}"
        sync.checkpoint(message)
        tag = sync.create_notes_tag(subject_slug, topic_slug, message)
        return GenerationResult(
            subject=subject_slug,
            topic=topic_slug,
            draft=False,
            path=path.relative_to(root).as_posix(),
            version=tag.version,
            tag=tag.name,
            commit=tag.commit,
            attempts=0,
            model="",
            revision=notes_revision(text),
        )
    path = write_notes_draft(vault, subject_slug, topic_slug, text)
    current = read_notes(vault, subject_slug, topic_slug)
    sync.note_change()
    commit = sync.checkpoint(
        f"Borrador de apuntes de {subject_slug}/{topic_slug} (no pasa la validación)"
    )
    return GenerationResult(
        subject=subject_slug,
        topic=topic_slug,
        draft=True,
        path=path.relative_to(root).as_posix(),
        commit=commit,
        attempts=0,
        errors=errors,
        warning=(
            "Los apuntes generados no cumplen todas las reglas de procedencia; se han guardado"
            " como borrador en notes/borrador.md, con los avisos del validador para revisarlos."
        ),
        model="",
        revision=None if current is None else notes_revision(current),
    )
