"""Textbook pages shown to the camera (#58): the printed-text prompt, the page number from the image
or from speech, the topic's book title, and how the editor cites a book page."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from capture_images import desk_still, encode

from studentassistant.config import Settings, SourcesSettings
from studentassistant.editor.inputs import assemble_input, page_citation_text
from studentassistant.llm import FakeClaude, load_prompt
from studentassistant.observer import CAPTURE_EVENT_KIND, STATE_OP_EVENT_KIND
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.sources import BurstStill, store_capture
from studentassistant.sources.transcriber import (
    PAGE_TRANSCRIBED_KIND,
    PageTranscriber,
    default_client_factory,
)
from studentassistant.sources.transcription import (
    BOOK_PROMPT_NAME,
    PROMPT_NAME,
    BookPage,
    PageInput,
    UncertainWord,
    pending_ops,
    prompt_name,
    read_page_input,
    render_request_text,
    split_printed_page,
    spoken_page_number,
    transcribe_page,
)
from studentassistant.vault import (
    Session,
    SourceNotFoundError,
    SourcePathError,
    TranscriptSegment,
    Vault,
    create_subject,
    create_topic,
    get_book,
    list_sources,
    put_source,
    read_source,
    set_book,
    sources_directory,
    start_session,
    update_page_meta,
)

WAIT = 10.0
SETTINGS = SourcesSettings()
FAST = SourcesSettings(
    capture_window_before_seconds=20,
    capture_window_after_seconds=0,
    transcription_grace_seconds=0,
    transcription_retry_seconds=0,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología").slug
    return subject, create_topic(tmp_vault, subject, "La célula").slug


def _segment(seq: int, start_s: float, end_s: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        seq=seq, t_start=int(start_s * 1000), t_end=int(end_s * 1000), text=text
    )


def _store_book(
    vault: Vault,
    topic: tuple[str, str],
    t_ms: int = 30_000,
    cid: str = "c-1",
    settings: SourcesSettings = SETTINGS,
) -> str:
    stored = store_capture(
        vault,
        *topic,
        "book",
        [BurstStill(encode(desk_still()), "image/jpeg")],
        {"capture_id": cid, "source_context": "book"},
        t_ms,
        settings,
    )
    return stored.path.relative_to(vault.path).as_posix()


def _book_page(**changes: object) -> PageInput:
    values: dict[str, object] = {
        "page_image": b"page",
        "original_image": None,
        "hints": (),
        "session_t_ms": 30_000,
        "subject": "Biología",
        "topic": "La célula",
        "source_kind": "book",
        "page_number": 2,
    }
    values.update(changes)
    return PageInput(**values)  # type: ignore[arg-type]


# -- the prompt and the request --------------------------------------------------------------------


def test_a_book_page_has_its_own_printed_text_prompt() -> None:
    assert prompt_name("book") == BOOK_PROMPT_NAME
    assert prompt_name("notes") == prompt_name("pdf") == PROMPT_NAME
    prompt = load_prompt(BOOK_PROMPT_NAME)
    for needle in ("printed textbook", "<!-- página impresa: 83 -->", "ninguna", "[[?word]]"):
        assert needle in prompt.content
    assert prompt.hash != load_prompt(PROMPT_NAME).hash


def test_the_book_request_names_the_book_and_asks_for_the_printed_number() -> None:
    text = render_request_text(_book_page(book_title="Biología 2º Bachillerato"))
    assert (
        "Source: a printed textbook page, from the book «Biología 2º Bachillerato» (photo 2)"
        in (text)
    )
    assert text.endswith("Transcribe the page, then give the page number printed on it.")
    untitled = render_request_text(_book_page())
    assert "Source: a printed textbook page (photo 2)" in untitled
    notes = render_request_text(_book_page(source_kind="notes", page_number=3))
    assert "Source: handwritten class notes, page 3" in notes
    assert notes.endswith("Transcribe the page.")


# -- the page number -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "text", "number"),
    [
        ("# Tema 4\n\nTexto.\n\n<!-- página impresa: 83 -->", "# Tema 4\n\nTexto.", 83),
        ("# Tema 4\n<!-- Página impresa:  7 -->  ", "# Tema 4", 7),
        ("# Tema 4\n\n<!-- página impresa: ninguna -->", "# Tema 4", None),
        ("# Tema 4\n\n<!-- página impresa: xii -->", "# Tema 4", None),
        ("# Tema 4\n\n<!-- página impresa: 0 -->", "# Tema 4", None),
        ("# Tema 4\n\nSin línea.", "# Tema 4\n\nSin línea.", None),
        ("[[?]]\n<!-- página impresa: ninguna -->", "[[?]]", None),
    ],
)
def test_the_printed_page_line_is_taken_off_the_answer(
    answer: str, text: str, number: int | None
) -> None:
    assert split_printed_page(answer) == (text, number)


def test_the_page_said_nearest_the_photo_is_the_spoken_page() -> None:
    hints = (
        _segment(1, 10, 12, "vamos a la página 82"),
        _segment(2, 27, 29, "ahora la página ochenta y tres del libro"),
        _segment(3, 36, 38, "luego pág. 84"),
    )
    assert spoken_page_number(hints, 30_000) == 83
    assert spoken_page_number(hints, 35_000) == 84
    assert spoken_page_number(hints[:1], 30_000) == 82
    assert spoken_page_number((_segment(1, 28, 31, "página número 7, no, página 9"),), 30_000) == 9
    assert spoken_page_number((_segment(1, 28, 31, "la pagina ciento doce"),), 30_000) == 112
    assert spoken_page_number((_segment(1, 28, 31, "página doscientos uno"),), 30_000) == 201
    assert spoken_page_number((_segment(1, 28, 31, "esta página es importante"),), 30_000) is None
    assert spoken_page_number((_segment(1, 28, 31, "en la página"),), 30_000) is None
    assert spoken_page_number((), 30_000) is None


def test_the_printed_number_wins_over_the_spoken_one() -> None:
    both = BookPage(printed=83, spoken=84)
    assert (both.number, both.number_from) == (83, "image")
    spoken = BookPage(printed=None, spoken=84)
    assert (spoken.number, spoken.number_from) == (84, "speech")
    neither = BookPage(printed=None, spoken=None)
    assert (neither.number, neither.number_from) == (None, None)
    assert both.meta() == {
        "book_page": 83,
        "book_page_from": "image",
        "book_page_printed": 83,
        "book_page_spoken": 84,
    }


def test_a_pending_item_of_a_book_page_names_the_book_page() -> None:
    mark = [UncertainWord("célula", "La [[?célula]]", 1, 4)]
    [known] = pending_ops(
        mark, session_id="s", capture_id="c", source_kind="book", page_number=2, book_page=83
    )
    assert "en la página 83 del libro, línea 1, columna 4" in known.text
    [unknown] = pending_ops(mark, session_id="s", capture_id="c", source_kind="book", page_number=2)
    assert "en la foto 2 del libro, línea 1" in unknown.text


# -- the vault: book title and sidecar updates -----------------------------------------------------


def test_the_topic_book_title_is_kept_beside_the_book_pages(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    assert get_book(tmp_vault, *topic) is None
    book = set_book(tmp_vault, *topic, "  Biología   2º Bachillerato ")
    assert book.title == "Biología 2º Bachillerato"
    path = sources_directory(tmp_vault, *topic, "book") / "book.yaml"
    assert path.read_text(encoding="utf-8") == "title: Biología 2º Bachillerato\n"
    assert get_book(tmp_vault, *topic) == book
    with pytest.raises(ValueError):
        set_book(tmp_vault, *topic, "   ")
    # Not a source, and the pages are numbered as before.
    source_path = _store_book(tmp_vault, topic)
    assert source_path.endswith("sources/book/page-001.jpg")
    assert [s.path for s in list_sources(tmp_vault, *topic)] == [source_path]


def test_update_page_meta_merges_into_the_sidecar(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    source_path = _store_book(tmp_vault, topic)
    before = read_source(tmp_vault, source_path).meta or {}
    update_page_meta(tmp_vault, source_path.replace(".jpg", ".page.jpg"), {"book_page": 83})
    after = read_source(tmp_vault, source_path).meta or {}
    assert after == {**before, "book_page": 83}
    assert list(after)[: len(before)] == list(before)
    with pytest.raises(SourceNotFoundError):
        update_page_meta(tmp_vault, source_path.replace("001", "009"), {"book_page": 1})
    web = put_source(tmp_vault, *topic, "web", "Una web", "# x", {"url": "https://example.com"})
    with pytest.raises(SourcePathError):
        update_page_meta(tmp_vault, web.relative_to(tmp_vault.path).as_posix(), {"a": 1})


# -- one book page transcribed ---------------------------------------------------------------------


def test_a_book_page_is_transcribed_with_its_printed_number(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    session.append_transcript(28_000, 31_000, "esto es la página 84")
    set_book(tmp_vault, *topic, "Biología 2")
    source_path = _store_book(tmp_vault, topic)
    page_path = source_path.replace(".jpg", ".page.jpg")
    page = read_page_input(tmp_vault, *topic, session.id, source_path, page_path, SETTINGS)
    assert (page.source_kind, page.book_title, page.spoken_page) == ("book", "Biología 2", 84)

    fake = FakeClaude().reply_text("# La membrana\n\nTexto impreso.\n\n<!-- página impresa: 83 -->")
    result = asyncio.run(
        asyncio.wait_for(
            transcribe_page(fake.client("transcriber"), page, tmp_vault, source_path), WAIT
        )
    )

    [request] = fake.requests
    assert request.prompt_hash == load_prompt(BOOK_PROMPT_NAME).hash
    assert "«Biología 2»" in request.messages[0]["content"][-1]["text"]
    assert result.text == "# La membrana\n\nTexto impreso."
    assert (tmp_vault.path / result.path).read_text(encoding="utf-8") == result.text + "\n"
    assert result.book_page == BookPage(printed=83, spoken=84)
    meta = read_source(tmp_vault, source_path).meta or {}
    assert (meta["book_page"], meta["book_page_from"]) == (83, "image")
    assert (meta["book_page_printed"], meta["book_page_spoken"]) == (83, 84)
    assert meta["capture_id"] == "c-1"  # the rest of the sidecar is kept


def test_without_a_printed_number_the_spoken_one_is_kept(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_path = _store_book(tmp_vault, topic)
    page = _book_page(hints=(_segment(1, 28, 31, "página 84"),), spoken_page=84)
    fake = FakeClaude().reply_text("Texto.\n<!-- página impresa: ninguna -->")
    result = asyncio.run(
        asyncio.wait_for(
            transcribe_page(fake.client("transcriber"), page, tmp_vault, source_path), WAIT
        )
    )
    assert result.text == "Texto."
    meta = read_source(tmp_vault, source_path).meta or {}
    assert (meta["book_page"], meta["book_page_from"]) == (84, "speech")


def test_a_notes_page_keeps_its_prompt_and_its_sidecar(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    stored = store_capture(
        tmp_vault,
        *topic,
        "notes",
        [BurstStill(encode(desk_still()), "image/jpeg")],
        {"capture_id": "c-1", "source_context": "notes"},
        30_000,
        SETTINGS,
    )
    source_path = stored.path.relative_to(tmp_vault.path).as_posix()
    before = read_source(tmp_vault, source_path).meta
    fake = FakeClaude().reply_text("# Apuntes\n\n<!-- página impresa: 3 -->")
    page = _book_page(source_kind="notes")
    result = asyncio.run(
        asyncio.wait_for(
            transcribe_page(fake.client("transcriber"), page, tmp_vault, source_path), WAIT
        )
    )
    assert fake.requests[0].prompt_hash == load_prompt(PROMPT_NAME).hash
    assert result.book_page is None
    assert "página impresa" in result.text  # only a book answer has that line taken off
    assert read_source(tmp_vault, source_path).meta == before


# -- on the bus ------------------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def session(tmp_vault: Vault, topic: tuple[str, str]) -> Session:
    return start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
async def transcriber(bus: SessionBus, fake: FakeClaude) -> AsyncIterator[PageTranscriber]:
    worker = PageTranscriber(
        bus,
        bus.attached,
        settings=FAST,
        client_factory=default_client_factory(Settings(), fake),
    )
    worker.start()
    yield worker
    await asyncio.wait_for(worker.stop(), WAIT)


@pytest.mark.anyio
async def test_a_book_capture_on_the_bus_publishes_its_book_page(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text("# Tema\n\nLa [[?célula]].\n\n<!-- página impresa: 83 -->")
    topic = (session.subject_slug, session.topic_slug)
    source_path = await asyncio.to_thread(_store_book, session.vault, topic, 30_000, "cap-1", FAST)
    await bus.publish(
        session.id,
        CAPTURE_EVENT_KIND,
        "phone",
        {
            "capture_id": "cap-1",
            "trigger": "button",
            "image_count": 1,
            "source_path": source_path,
            "page_path": source_path.replace(".jpg", ".page.jpg"),
            "source_context": "book",
        },
        t=30_000,
    )
    await asyncio.wait_for(transcriber.wait_idle(session.id), WAIT)

    [done] = [e for e in session.read_events() if e.kind == PAGE_TRANSCRIBED_KIND]
    assert done.payload["source_context"] == "book"
    assert (done.payload["book_page"], done.payload["book_page_from"]) == (83, "image")
    assert done.payload["text"] == "# Tema\n\nLa [[?célula]]."
    [op] = [e.payload for e in session.read_events() if e.kind == STATE_OP_EVENT_KIND]
    assert "la página 83 del libro" in op["text"]


# -- the editor cites the book page ----------------------------------------------------------------


def test_page_citation_text_uses_the_book_page_and_title() -> None:
    assert page_citation_text("notes", 3, {"book_page": 83}) == "Apuntes, página 3"
    assert page_citation_text("book", 2, None) == "Libro, página 2"
    assert page_citation_text("book", 2, {"book_page": 83}) == "Libro, página 83"
    assert (
        page_citation_text("book", 2, {"book_page": 83}, "Bio [2]") == "Libro «Bio (2)», página 83"
    )


def test_the_editor_catalogue_cites_a_book_page_by_its_number_and_book(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_path = _store_book(tmp_vault, topic)
    update_page_meta(tmp_vault, source_path, {"book_page": 83, "book_page_from": "image"})
    set_book(tmp_vault, *topic, "Biología 2")
    assembled = assemble_input(tmp_vault, *topic, prompt=load_prompt("editor_generate"))
    catalogue = assembled.content[0]["text"]
    assert "[Libro «Biología 2», página 83](../sources/book/page-001.jpg)" in catalogue
    joined = "\n".join(b.get("text", "") for b in assembled.content)
    assert "## Páginas del libro «Biología 2»" in joined
