"""Scanned PDF pages: each kept page without a text layer is transcribed with Claude vision.

`import_pdf` stores, per kept page `K` of a PDF, the text PyMuPDF extracts (`page-NNN.pKKK.txt`),
which is empty for a scanned page. Such a page is rendered (`pdf.render_pdf_page`, long edge
`[sources] pdf_transcription_long_edge`, JPEG `pdf_transcription_quality`) and sent once to the
`transcriber` role with the `page_transcription_pdf` prompt (the printed-text variant of the page
transcription prompts: no spoken hints, no printed page number asked for). The answer is written
through the vault as `page-NNN.pKKK.md` next to the `.txt` and `.jpg`
(`vault.put_page_transcription` with `page=K`), and the exchange is appended to the topic's
`conversations/transcriber-pdf.jsonl`
(the image as its vault path, never its bytes). The editor and the "¿por qué pusiste esto?"
explanation read that Markdown where the `.txt` is empty (`pdf.read_pdf_page_text`).

`ScannedPdfTranscriber` runs those calls in the background, never on the request that stored the
PDF: `schedule` queues pages (the web upload does, after its import), `catch_up_vault` queues
every scanned page of the vault still without `.md` (server start, once the vault is open: a
restart cut a job, or the PDF came in through the CLI). At most `transcription_concurrency` pages
go to Claude at once; a failed attempt (an `LLMError` other than a reached cost cap or a refusal,
or a vault error) is retried up to `transcription_attempts` times, `transcription_retry_seconds`
(doubling) apart. A page whose every attempt failed, that reached a cost cap or that Claude
refused is logged and left without `.md` (the next server start tries it again); nothing already
stored is touched. Every call goes through a `transcriber` client bound to the topic's ledger (no
session: a PDF is a topic's source), so it is capped and recorded like every other call.

Uncertain words (`[[?word]]`, `[[?]]`) stay marked in the Markdown; unlike a captured page they
open no pending item, since no session holds the PDF.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from studentassistant.config import Settings, SourcesSettings
from studentassistant.llm import (
    CostCapReachedError,
    LedgerBinding,
    LLMClient,
    LLMError,
    LLMResponse,
    RefusalError,
    get_client,
    load_prompt,
)
from studentassistant.sources.pdf import (
    PdfImportError,
    original_page,
    page_transcription_path,
    read_pdf_page_text,
    render_pdf_page,
    scanned_pages_without_transcription,
)
from studentassistant.sources.transcription import (
    TranscriptionError,
    UncertainWord,
    clean_markdown,
    find_uncertain,
)
from studentassistant.vault import (
    ConversationRecord,
    SecretRefused,
    Vault,
    VaultError,
    append_conversation_record,
    get_subject,
    get_topic,
    list_subjects,
    list_topics,
    put_page_transcription,
    read_source,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "page_transcription_pdf"
CONVERSATION_NAME = "transcriber-pdf"

ClientFactory = Callable[[LedgerBinding], LLMClient]
PageKey = tuple[str, int]
"""`(pdf_path, page)`: a stored PDF's vault-relative path and a page of it, from 1."""


@dataclass(frozen=True)
class PdfPageInput:
    """What one scanned page's transcription sends: the rendered page and its context."""

    image: bytes
    subject: str
    topic: str
    pdf_path: str
    page: int
    original_page: int
    original_name: str


@dataclass(frozen=True)
class PdfPageTranscription:
    """A transcribed scanned page: the Markdown, where it was written, its uncertain words."""

    text: str
    path: str
    uncertain: tuple[UncertainWord, ...]
    page_input: PdfPageInput
    response: LLMResponse
    prompt_hash: str


# -- request ---------------------------------------------------------------------------------------


def render_request_text(page: PdfPageInput) -> str:
    """The text part of the request: where the page comes from."""
    name = page.original_name or PurePosixPath(page.pdf_path).name
    return "\n".join(
        [
            f"Subject: {page.subject}",
            f"Topic: {page.topic}",
            f"Source: page {page.original_page} of the PDF «{name}» (a scanned page: the PDF has"
            " no text for it)",
            "The image is the page.",
            "",
            "Transcribe the page.",
        ]
    )


def request_content(page: PdfPageInput) -> list[dict[str, Any]]:
    """The user turn's blocks: the page image, then the text."""
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(page.image).decode("ascii"),
            },
        },
        {"type": "text", "text": render_request_text(page)},
    ]


def read_pdf_page_input(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    pdf_path: str,
    page: int,
    settings: SourcesSettings,
) -> PdfPageInput:
    """Render page `page` of the stored PDF `pdf_path` and read its context (blocking).

    Raises:
        SourceError (a `VaultError`): the PDF is not stored there.
        PdfImportError: the stored file is not a readable PDF or has no such page.
    """
    stored = read_source(vault, pdf_path)
    meta = stored.meta or {}
    image = render_pdf_page(
        stored.content,
        page,
        long_edge=settings.pdf_transcription_long_edge,
        quality=settings.pdf_transcription_quality,
    )
    return PdfPageInput(
        image=image,
        subject=get_subject(vault, subject_slug).subject.name,
        topic=get_topic(vault, subject_slug, topic_slug).topic.title,
        pdf_path=pdf_path,
        page=page,
        original_page=original_page(meta, page),
        original_name=str(meta.get("original_name") or ""),
    )


async def transcribe_pdf_page(
    client: LLMClient, page: PdfPageInput, vault: Vault
) -> PdfPageTranscription:
    """Ask Claude for the scanned page's Markdown and store it as `page-NNN.pKKK.md`.

    Raises:
        CostCapReachedError: a cost cap is reached; nothing was sent.
        RefusalError: Claude refused the page.
        TranscriptionError: the answer holds no text.
        LLMError: any other failure of the call; VaultError/OSError from the write.
    """
    prompt = load_prompt(PROMPT_NAME)
    response = await client.create(
        [{"role": "user", "content": request_content(page)}],
        system=prompt.content,
        prompt_hash=prompt.hash,
    )
    if response.stop_reason == "refusal":
        raise RefusalError("Claude refused to transcribe the PDF page")
    text = clean_markdown(response.text)
    if not text:
        raise TranscriptionError("the transcription answer holds no text")
    written = await asyncio.to_thread(
        put_page_transcription, vault, page.pdf_path, text + "\n", page=page.page
    )
    return PdfPageTranscription(
        text=text,
        path=written.relative_to(vault.path).as_posix(),
        uncertain=tuple(find_uncertain(text)),
        page_input=page,
        response=response,
        prompt_hash=prompt.hash,
    )


# -- background runner -----------------------------------------------------------------------------


def default_client_factory(
    settings: Settings | None = None, transport: Any | None = None
) -> ClientFactory:
    """Transcriber clients from `[llm.roles.transcriber]`, capped and recorded on the ledger."""

    def build(binding: LedgerBinding) -> LLMClient:
        return get_client("transcriber", settings=settings, transport=transport, ledger=binding)

    return build


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ScannedPdfTranscriber:
    """Transcribes scanned PDF pages in the background (see the module docstring)."""

    def __init__(
        self,
        *,
        settings: SourcesSettings | None = None,
        client_factory: ClientFactory | None = None,
        on_write: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.settings = settings or SourcesSettings()
        self.client_factory = client_factory or default_client_factory()
        self.on_write = on_write
        self.clock = clock
        self.sleep = sleep
        self._jobs: dict[PageKey, asyncio.Task[None]] = {}
        self._startup: asyncio.Task[None] | None = None
        self._slots: asyncio.Semaphore | None = None
        self._stopped = False

    def schedule(
        self, vault: Vault, subject_slug: str, topic_slug: str, pages: Iterable[PageKey]
    ) -> int:
        """Queue the scanned `pages` of a topic (on the event loop); returns how many were new.

        A page already queued or being transcribed is not queued twice.
        """
        if self._stopped:
            return 0
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.settings.transcription_concurrency)
        queued = 0
        for key in pages:
            if key in self._jobs:
                continue
            task = asyncio.create_task(
                self._transcribe(vault, subject_slug, topic_slug, key),
                name=f"pdf-transcriber:{key[0]}#page={key[1]}",
            )
            self._jobs[key] = task
            task.add_done_callback(lambda done, key=key: self._forget(key, done))
            queued += 1
        return queued

    def catch_up_vault(self, vault: Vault) -> None:
        """Server start: queue every scanned page of the vault still without `.md`, once."""
        if self._stopped or self._startup is not None:
            return
        self._startup = asyncio.create_task(self._catch_up(vault), name="pdf-transcriber:startup")

    async def wait_idle(self) -> None:
        """Wait until the start-up catch-up and every queued page are done (tests)."""
        if self._startup is not None:
            await asyncio.gather(self._startup, return_exceptions=True)
        while pending := [job for job in self._jobs.values() if not job.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self) -> None:
        """Cancel the catch-up and every job still waiting or calling (shutdown)."""
        self._stopped = True
        tasks = [*self._jobs.values(), *([self._startup] if self._startup else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()

    # -- internals -----------------------------------------------------------------------------

    def _forget(self, key: PageKey, done: asyncio.Task[None]) -> None:
        if self._jobs.get(key) is done:
            del self._jobs[key]

    async def _catch_up(self, vault: Vault) -> None:
        try:
            plan = await asyncio.to_thread(_owed_in_vault, vault)
        except (VaultError, OSError):
            logger.exception("the PDF transcriber cannot read the vault to catch up at start")
            return
        for subject, topic, pages in plan:
            queued = self.schedule(vault, subject, topic, pages)
            if queued:
                logger.info(
                    "PDF transcriber of %s/%s: %d scanned pages queued", subject, topic, queued
                )

    async def _transcribe(
        self, vault: Vault, subject_slug: str, topic_slug: str, key: PageKey
    ) -> None:
        pdf_path, page_number = key
        assert self._slots is not None
        client = self.client_factory(LedgerBinding(vault, subject_slug, topic_slug))
        attempts = 0
        async with self._slots:
            # Queued twice in a row (the upload, then the start-up catch-up): done already.
            if await asyncio.to_thread(_transcribed, vault, pdf_path, page_number):
                return
            while True:
                attempts += 1
                try:
                    page = await asyncio.to_thread(
                        read_pdf_page_input,
                        vault,
                        subject_slug,
                        topic_slug,
                        pdf_path,
                        page_number,
                        self.settings,
                    )
                    result = await transcribe_pdf_page(client, page, vault)
                    break
                except CostCapReachedError as error:
                    self._give_up(key, "cost_cap", error, attempts)
                    return
                except RefusalError as error:
                    self._give_up(key, "refused", error, attempts)
                    return
                except PdfImportError as error:  # the stored file is not a readable PDF page
                    self._give_up(key, "unreadable", error, attempts)
                    return
                except (LLMError, VaultError, OSError) as error:
                    if attempts >= self.settings.transcription_attempts:
                        self._give_up(key, "error", error, attempts)
                        return
                    logger.warning(
                        "transcription of %s page %d, attempt %d failed: %s",
                        pdf_path,
                        page_number,
                        attempts,
                        error,
                    )
                    await self.sleep(
                        self.settings.transcription_retry_seconds * 2 ** (attempts - 1)
                    )
        await self._record(vault, subject_slug, topic_slug, result)
        logger.info("scanned page %d of %s transcribed as %s", page_number, pdf_path, result.path)

    def _give_up(self, key: PageKey, reason: str, error: Exception, attempts: int) -> None:
        logger.warning(
            "scanned page %d of %s left without transcription (%s after %d attempts): %s",
            key[1],
            key[0],
            reason,
            attempts,
            error,
        )

    async def _record(
        self, vault: Vault, subject_slug: str, topic_slug: str, result: PdfPageTranscription
    ) -> None:
        response = result.response
        page = result.page_input
        content = [  # the request as the conversation file keeps it: the image by vault path
            {
                "type": "image",
                "source": {"type": "vault", "path": page.pdf_path, "page": page.page},
            },
            {"type": "text", "text": render_request_text(page)},
        ]
        records = [
            ConversationRecord(
                time=self.clock(), kind="user", message={"role": "user", "content": content}
            ),
            ConversationRecord(
                time=self.clock(),
                kind="assistant",
                message=copy.deepcopy(response.assistant_turn()),
                model=response.model,
                prompt_hash=result.prompt_hash,
                usage=response.usage.model_dump(),
            ),
        ]
        try:
            for record in records:
                await asyncio.to_thread(
                    append_conversation_record,
                    vault,
                    subject_slug,
                    topic_slug,
                    CONVERSATION_NAME,
                    record,
                )
        except SecretRefused:
            logger.warning("a PDF transcriber record of %s looks like a secret", page.pdf_path)
        except (VaultError, OSError):
            logger.exception(
                "the PDF transcriber conversation of %s cannot be written", page.pdf_path
            )
        if self.on_write is not None:
            self.on_write()


def _transcribed(vault: Vault, pdf_path: str, page: int) -> bool:
    text = read_pdf_page_text(vault, pdf_path, page)
    return text is not None and text.transcribed


def _owed_in_vault(vault: Vault) -> list[tuple[str, str, list[PageKey]]]:
    """Per topic of the vault, its scanned PDF pages without transcription (blocking)."""
    plan: list[tuple[str, str, list[PageKey]]] = []
    for subject in list_subjects(vault):
        for topic in list_topics(vault, subject.slug):
            try:
                pages = scanned_pages_without_transcription(vault, subject.slug, topic.slug)
            except VaultError:
                logger.exception("cannot list the PDFs of %s/%s", subject.slug, topic.slug)
                continue
            if pages:
                plan.append((subject.slug, topic.slug, pages))
    return plan


__all__ = [
    "CONVERSATION_NAME",
    "PROMPT_NAME",
    "PdfPageInput",
    "PdfPageTranscription",
    "ScannedPdfTranscriber",
    "default_client_factory",
    "page_transcription_path",
    "read_pdf_page_input",
    "render_request_text",
    "request_content",
    "transcribe_pdf_page",
]
