"""Scanned PDF pages transcribed with Claude vision (`sources.pdf_transcription`, #257).

The PDFs are generated here with PyMuPDF: page 1 has a text layer, page 2 is an image only.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pymupdf
import pytest

from studentassistant.config import LlmSettings, Settings, SourcesSettings
from studentassistant.llm import FakeClaude, LedgerBinding, LLMError, load_prompt
from studentassistant.sources import (
    import_pdf,
    read_pdf_page_text,
    render_pdf_page,
    scanned_pages_without_transcription,
)
from studentassistant.sources.pdf_transcription import (
    CONVERSATION_NAME,
    PROMPT_NAME,
    ScannedPdfTranscriber,
    default_client_factory,
    read_pdf_page_input,
    render_request_text,
    transcribe_pdf_page,
)
from studentassistant.vault import (
    SourcePathError,
    Vault,
    create_subject,
    create_topic,
    put_page_transcription,
    read_conversation,
    read_ledger,
)

WAIT = 10.0
FAST = SourcesSettings(transcription_attempts=2, transcription_retry_seconds=0)
TRANSCRIPTION = "# Tema 4\n\nLa derivada de una [[?constante]] es cero."


def scanned_pdf() -> bytes:
    """Page 1 reads «Página 1 del tema»; page 2 is a picture only (a scanned page)."""
    document = pymupdf.open()
    try:
        document.new_page().insert_text((72, 72), "Página 1 del tema", fontsize=14)
        picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        picture.clear_with(200)
        document.new_page().insert_image(pymupdf.Rect(72, 72, 272, 272), pixmap=picture)
        return document.tobytes()
    finally:
        document.close()


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


@pytest.fixture
def pdf_path(tmp_vault: Vault, topic: tuple[str, str]) -> str:
    imported = import_pdf(tmp_vault, *topic, "Tema 4.pdf", scanned_pdf())
    assert [page.has_text for page in imported.pages] == [True, False]
    return imported.path.relative_to(tmp_vault.path).as_posix()


def _run[T](work: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(work, WAIT)

    return asyncio.run(main())


def _md(vault: Vault, pdf_path: str, page: int) -> Path:
    return vault.path / pdf_path.replace(".pdf", f".p{page:03d}.md")


# -- pure and blocking pieces ----------------------------------------------------------------------


def test_only_the_scanned_page_is_owed_a_transcription(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    assert scanned_pages_without_transcription(tmp_vault, *topic) == [(pdf_path, 2)]
    put_page_transcription(tmp_vault, pdf_path, "  \n", page=2)  # an empty one still owes
    assert scanned_pages_without_transcription(tmp_vault, *topic) == [(pdf_path, 2)]
    put_page_transcription(tmp_vault, pdf_path, TRANSCRIPTION + "\n", page=2)
    assert scanned_pages_without_transcription(tmp_vault, *topic) == []


def test_the_page_transcription_is_written_next_to_the_page_files(
    tmp_vault: Vault, pdf_path: str
) -> None:
    written = put_page_transcription(tmp_vault, pdf_path, TRANSCRIPTION + "\n", page=2)
    assert written == _md(tmp_vault, pdf_path, 2)
    assert written.with_suffix(".txt").is_file() and written.with_suffix(".jpg").is_file()
    with pytest.raises(SourcePathError):
        put_page_transcription(tmp_vault, pdf_path, "x", page=0)


def test_a_page_text_is_the_extracted_text_else_the_transcription(
    tmp_vault: Vault, pdf_path: str
) -> None:
    first = read_pdf_page_text(tmp_vault, pdf_path, 1)
    assert first is not None and first.text == "Página 1 del tema" and not first.transcribed
    assert read_pdf_page_text(tmp_vault, pdf_path, 2) is None
    put_page_transcription(tmp_vault, pdf_path, TRANSCRIPTION + "\n", page=2)
    second = read_pdf_page_text(tmp_vault, pdf_path, 2)
    assert second is not None and second.text == TRANSCRIPTION and second.transcribed
    assert read_pdf_page_text(tmp_vault, pdf_path, 3) is None


def test_a_page_renders_at_the_configured_long_edge() -> None:
    image = render_pdf_page(scanned_pdf(), 2, long_edge=500, quality=80)
    decoded = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and max(decoded.shape[:2]) == 500


def test_the_input_names_the_original_page_and_file(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    page = read_pdf_page_input(
        tmp_vault, *topic, pdf_path, 2, SourcesSettings(pdf_transcription_long_edge=300)
    )
    assert page.original_page == 2 and page.original_name == "Tema 4.pdf"
    decoded = cv2.imdecode(np.frombuffer(page.image, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) == 300
    text = render_request_text(page)
    assert "Subject: Matemáticas" in text and "Topic: Derivadas" in text
    assert "page 2 of the PDF «Tema 4.pdf»" in text


# -- one call --------------------------------------------------------------------------------------


def test_one_call_uses_the_transcriber_role_and_writes_the_markdown(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    fake = FakeClaude().reply_text(f"```markdown\n{TRANSCRIPTION}\n```")
    client = fake.client("transcriber", ledger=LedgerBinding(tmp_vault, *topic))
    page = read_pdf_page_input(tmp_vault, *topic, pdf_path, 2, SourcesSettings())

    result = _run(transcribe_pdf_page(client, page, tmp_vault))

    [request] = fake.requests
    assert request.role == "transcriber"
    assert request.prompt_hash == load_prompt(PROMPT_NAME).hash
    image, text = request.messages[0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert base64.standard_b64decode(image["source"]["data"]) == page.image
    assert text["type"] == "text" and "Transcribe the page." in text["text"]
    assert result.text == TRANSCRIPTION
    assert [mark.word for mark in result.uncertain] == ["constante"]
    assert _md(tmp_vault, pdf_path, 2).read_text(encoding="utf-8") == TRANSCRIPTION + "\n"
    [entry] = read_ledger(tmp_vault, *topic)
    assert entry.role == "transcriber" and entry.session is None


# -- background runner -----------------------------------------------------------------------------


def _worker(fake: Any, sources: SourcesSettings = FAST, settings: Settings | None = None) -> Any:
    writes: list[int] = []
    worker = ScannedPdfTranscriber(
        settings=sources,
        client_factory=default_client_factory(settings or Settings(), fake),
        on_write=lambda: writes.append(1),
    )
    worker.writes = writes  # type: ignore[attr-defined]
    return worker


async def _schedule_and_wait(
    worker: ScannedPdfTranscriber, vault: Vault, topic: tuple[str, str], pages: Any
) -> int:
    try:
        queued = worker.schedule(vault, *topic, pages)
        await asyncio.wait_for(worker.wait_idle(), WAIT)
        return queued
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)


def test_a_scheduled_page_is_transcribed_recorded_and_synced(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    fake = FakeClaude().reply_text(TRANSCRIPTION)
    worker = _worker(fake)

    queued = _run(_schedule_and_wait(worker, tmp_vault, topic, [(pdf_path, 2), (pdf_path, 2)]))

    assert queued == 1
    assert _md(tmp_vault, pdf_path, 2).read_text(encoding="utf-8") == TRANSCRIPTION + "\n"
    assert worker.writes
    [entry] = read_ledger(tmp_vault, *topic)
    assert entry.role == "transcriber"
    records = read_conversation(tmp_vault, *topic, CONVERSATION_NAME)
    assert [r.kind for r in records] == ["user", "assistant"]
    assert records[0].message["content"][0] == {  # type: ignore[index]
        "type": "image",
        "source": {"type": "vault", "path": pdf_path, "page": 2},
    }


def test_a_transient_failure_is_retried(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    # Not a transient error, so the client itself does not retry it: the runner's attempt does.
    fake = FakeClaude().fail(LLMError("boom")).reply_text(TRANSCRIPTION)
    _run(_schedule_and_wait(_worker(fake), tmp_vault, topic, [(pdf_path, 2)]))
    assert len(fake.requests) == 2
    assert _md(tmp_vault, pdf_path, 2).is_file()


def test_every_attempt_failing_leaves_the_page_without_markdown(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    fake = FakeClaude().fail(LLMError("uno")).fail(LLMError("dos"))
    _run(_schedule_and_wait(_worker(fake), tmp_vault, topic, [(pdf_path, 2)]))
    assert len(fake.requests) == 2
    assert not _md(tmp_vault, pdf_path, 2).exists()
    assert scanned_pages_without_transcription(tmp_vault, *topic) == [(pdf_path, 2)]
    assert (tmp_vault.path / pdf_path).is_file()  # nothing stored is lost


def test_a_reached_cost_cap_sends_nothing(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    fake = FakeClaude().reply_text(TRANSCRIPTION)
    capped = Settings(llm=LlmSettings(max_usd_per_day=0))
    _run(_schedule_and_wait(_worker(fake, settings=capped), tmp_vault, topic, [(pdf_path, 2)]))
    assert fake.requests == []
    assert not _md(tmp_vault, pdf_path, 2).exists()


def test_the_catch_up_queues_every_scanned_page_left_without_markdown(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    other = create_topic(tmp_vault, topic[0], "Integrales").slug
    second = import_pdf(tmp_vault, topic[0], other, "Tema 5.pdf", scanned_pdf())
    second_path = second.path.relative_to(tmp_vault.path).as_posix()
    fake = FakeClaude().reply_text("# Uno").reply_text("# Dos")
    worker = _worker(fake, FAST.model_copy(update={"transcription_concurrency": 1}))

    async def main() -> None:
        try:
            worker.catch_up_vault(tmp_vault)
            worker.catch_up_vault(tmp_vault)  # once per start
            await asyncio.wait_for(worker.wait_idle(), WAIT)
        finally:
            await asyncio.wait_for(worker.stop(), WAIT)

    _run(main())
    assert len(fake.requests) == 2
    assert _md(tmp_vault, pdf_path, 2).is_file() and _md(tmp_vault, second_path, 2).is_file()
    assert not _md(tmp_vault, pdf_path, 1).exists()
    assert scanned_pages_without_transcription(tmp_vault, *topic) == []


def test_a_stopped_runner_queues_nothing(
    tmp_vault: Vault, topic: tuple[str, str], pdf_path: str
) -> None:
    worker = _worker(FakeClaude())

    async def main() -> int:
        await worker.stop()
        worker.catch_up_vault(tmp_vault)
        return worker.schedule(tmp_vault, *topic, [(pdf_path, 2)])

    assert _run(main()) == 0
