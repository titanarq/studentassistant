"""Page transcription pieces and one `transcribe_page` call with `FakeClaude` over `tmp_vault`."""

from __future__ import annotations

import asyncio
import base64

import cv2
import numpy as np
import pytest
from capture_images import desk_still, encode, flat_still

from studentassistant.config import SourcesSettings
from studentassistant.llm import FakeClaude, RefusalError, load_prompt
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    STATE_OP_EVENT_KIND,
    AddPending,
    TopicState,
    fold,
    op_payload,
)
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.sources import BurstStill, store_capture
from studentassistant.sources.transcription import (
    PageInput,
    TranscriptionError,
    UncertainWord,
    clean_markdown,
    crop_is_doubtful,
    find_uncertain,
    hint_segments,
    pending_ops,
    read_page_input,
    render_request_text,
    request_content,
    transcribe_page,
)
from studentassistant.vault import (
    Event,
    SourceNotFoundError,
    SourcePathError,
    TranscriptSegment,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    put_page_transcription,
    read_source,
    start_session,
)

SETTINGS = SourcesSettings()
WAIT = 10.0


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología").slug
    return subject, create_topic(tmp_vault, subject, "La célula").slug


def _store(vault: Vault, topic: tuple[str, str], image: np.ndarray, t_ms: int = 30_000) -> str:
    stored = store_capture(
        vault,
        *topic,
        "notes",
        [BurstStill(encode(image), "image/jpeg")],
        {"capture_id": "c-1", "source_context": "notes"},
        t_ms,
        SETTINGS,
    )
    return stored.path.relative_to(vault.path).as_posix()


def _segment(seq: int, start_s: float, end_s: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        seq=seq, t_start=int(start_s * 1000), t_end=int(end_s * 1000), text=text
    )


def _page(**changes: object) -> PageInput:
    values: dict[str, object] = {
        "page_image": b"page",
        "original_image": None,
        "hints": (),
        "session_t_ms": 30_000,
        "subject": "Biología",
        "topic": "La célula",
        "source_kind": "notes",
        "page_number": 3,
    }
    values.update(changes)
    return PageInput(**values)  # type: ignore[arg-type]


# -- the prompt ------------------------------------------------------------------------------------


def test_the_prompt_asks_for_spanish_markdown_with_the_marks() -> None:
    prompt = load_prompt("page_transcription")
    for needle in ("Markdown", "Spanish", "[[?word]]", "[[?]]", "mermaid", "nested lists"):
        assert needle in prompt.content
    assert prompt.hash.startswith("sha256:")


# -- pure pieces -----------------------------------------------------------------------------------


def test_uncertain_marks_are_found_in_order_with_their_line() -> None:
    text = "# Título\n\nLa [[?mitocondria]] produce [[?]] ATP.\n- otra [[? ribosoma ]]\n[[no]]"
    assert find_uncertain(text) == [
        UncertainWord("mitocondria", "La [[?mitocondria]] produce [[?]] ATP.", 3, 4),
        UncertainWord(None, "La [[?mitocondria]] produce [[?]] ATP.", 3, 29),
        UncertainWord("ribosoma", "- otra [[? ribosoma ]]", 4, 8),
    ]
    assert find_uncertain("sin dudas") == []


def test_a_fence_around_the_whole_answer_is_removed_but_a_mermaid_block_is_kept() -> None:
    assert clean_markdown("```markdown\n# Hola\n\n- a\n```\n") == "# Hola\n\n- a"
    assert clean_markdown("  # Hola  \n") == "# Hola"
    mermaid = "```mermaid\nflowchart TD\nA --> B\n```"
    assert clean_markdown(mermaid) == mermaid


def test_hints_are_the_segments_overlapping_the_window() -> None:
    segments = [
        _segment(1, 0, 5, "antes del todo"),
        _segment(2, 8, 12, "solapa el inicio"),
        _segment(3, 20, 25, "dentro"),
        _segment(4, 39, 45, "solapa el final"),
        _segment(5, 41, 50, "después"),
        _segment(6, 22, 23, "   "),
    ]
    window = {"t_start": 10_000, "t_end": 40_000}
    assert [s.seq for s in hint_segments(reversed(segments), window)] == [2, 3, 4]
    assert hint_segments(segments, None) == ()


def test_the_request_holds_the_images_first_then_the_context_and_hints() -> None:
    page = _page(hints=(_segment(1, 25, 28, "esto es la mitocondria"),))
    blocks = request_content(page)
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert blocks[0]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": base64.standard_b64encode(b"page").decode(),
    }
    text = blocks[1]["text"]
    assert "Subject: Biología" in text and "Topic: La célula" in text
    assert "handwritten class notes, page 3" in text
    assert "(taken at 30.0 s)" in text
    assert "- [25.0-28.0 s] esto es la mitocondria" in text

    both = request_content(_page(original_image=b"original"))
    assert [b["type"] for b in both] == ["image", "image", "text"]
    assert "second is the original photo" in both[2]["text"]
    assert "said nothing" in render_request_text(_page())


def test_each_uncertain_word_becomes_an_illegible_pending_item_of_the_capture() -> None:
    marks = [
        UncertainWord("mitocondria", "La [[?mitocondria]] produce", 2, 4),
        UncertainWord(None, "x", 5, 1),
    ]
    ops = pending_ops(
        marks, session_id="20260925-101010", capture_id="cap-1", source_kind="notes", page_number=3
    )
    assert [op.pending_id for op in ops] == [
        "ill-20260925-101010-cap-1-1",
        "ill-20260925-101010-cap-1-2",
    ]
    assert {op.kind for op in ops} == {"illegible"}
    assert all(op.capture_ids == ["cap-1"] for op in ops)
    assert ops[0].text == (
        "Palabra dudosa «mitocondria» en la página 3 (apuntes), línea 2, columna 4:"
        " «La [[?mitocondria]] produce»"
    )
    assert ops[1].text == "Palabra ilegible en la página 3 (apuntes), línea 5, columna 1: «x»"
    long = pending_ops(
        [UncertainWord(None, "a" * 500)],
        session_id="s",
        capture_id="c",
        source_kind="book",
        page_number=None,
    )
    assert "una página del libro" in long[0].text
    assert len(long[0].text) < 250


def _fold_pages(*batches: tuple[str, str, list[AddPending]]) -> TopicState:
    """The observer's fold over, per batch, a stored capture and its pending ops."""
    events: list[tuple[str, Event]] = []
    for session_id, capture_id, ops in batches:
        payloads = [(CAPTURE_EVENT_KIND, {"capture_id": capture_id})]
        payloads += [(STATE_OP_EVENT_KIND, op_payload(op)) for op in ops]
        events += [
            (session_id, Event(seq=seq, t=seq, origin="observer", kind=kind, payload=payload))
            for seq, (kind, payload) in enumerate(payloads, start=1)
        ]
    return fold(events)


def test_distinct_marks_of_one_page_stay_distinct_pending_items() -> None:
    # Marks of one page share the capture ref and have near-identical texts; the pending queue
    # (#178's merging) must still keep one item per mark, even for two marks on one line.
    text = "# Tema\n\nLa [[?]] y la [[?]] del núcleo.\n\nOtra [[?]] del núcleo.\n- [[?mitosis]]"
    marks = find_uncertain(text)
    ops = pending_ops(
        marks, session_id="20260925-101010", capture_id="cap-1", source_kind="notes", page_number=3
    )
    state = _fold_pages(("20260925-101010", "cap-1", ops))
    assert sorted(item.id for item in state.open_pending()) == sorted(op.pending_id for op in ops)
    assert len(ops) == 4
    # The same page photographed and transcribed again later is the same doubts: merged.
    again = pending_ops(
        marks, session_id="20260925-111111", capture_id="cap-9", source_kind="notes", page_number=3
    )
    state = _fold_pages(("20260925-101010", "cap-1", ops), ("20260925-111111", "cap-9", again))
    assert len(state.open_pending()) == 4


# -- reading a stored page -------------------------------------------------------------------------


def test_the_page_input_reads_the_crop_the_context_and_the_hints(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    session.append_transcript(1_000, 4_000, "muy pronto")  # window is 10 s .. 40 s
    session.append_transcript(28_000, 31_000, "aquí pone mitocondria")
    source_path = _store(tmp_vault, topic, desk_still())
    page_path = source_path.replace(".jpg", ".page.jpg")
    extra = [_segment(9, 35, 38, "y aún no escrito"), _segment(9, 28, 31, "aquí pone mitocondria")]

    page = read_page_input(tmp_vault, *topic, session.id, source_path, page_path, SETTINGS, extra)

    assert page.page_image == read_source(tmp_vault, page_path).content
    assert page.original_image is None  # the sheet covers most of the still
    assert [s.text for s in page.hints] == ["aquí pone mitocondria", "y aún no escrito"]
    assert (page.subject, page.topic, page.source_kind, page.page_number) == (
        "Biología",
        "La célula",
        "notes",
        1,
    )
    assert page.session_t_ms == 30_000


def test_a_small_crop_sends_the_original_too_and_no_page_sends_only_the_still(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_path = _store(tmp_vault, topic, desk_still())
    meta = read_source(tmp_vault, source_path).meta or {}
    page = read_source(tmp_vault, source_path.replace(".jpg", ".page.jpg")).content
    assert not crop_is_doubtful(meta, page, SETTINGS)
    assert crop_is_doubtful(meta, page, SourcesSettings(transcription_min_crop_share=0.99))
    assert crop_is_doubtful(meta, b"not an image", SETTINGS)
    assert not crop_is_doubtful({**meta, "page_detected": False}, page, SETTINGS)

    flat = _store(tmp_vault, topic, flat_still())
    flat_meta = read_source(tmp_vault, flat).meta or {}
    assert flat_meta["page_detected"] is False
    tiny = cv2.imencode(".jpg", np.zeros((10, 10, 3), np.uint8))[1].tobytes()
    assert not crop_is_doubtful(flat_meta, tiny, SETTINGS)


# -- one call --------------------------------------------------------------------------------------


def test_a_page_is_transcribed_and_stored_next_to_it(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_path = _store(tmp_vault, topic, desk_still())
    fake = FakeClaude().reply_text("```markdown\n# La célula\n\n- La [[?mitocondria]]\n```")
    client = fake.client("transcriber")
    page = _page(page_image=b"img")

    result = asyncio.run(
        asyncio.wait_for(transcribe_page(client, page, tmp_vault, source_path), WAIT)
    )

    assert result.text == "# La célula\n\n- La [[?mitocondria]]"
    assert result.path == source_path.replace(".jpg", ".md")
    assert (tmp_vault.path / result.path).read_text(encoding="utf-8") == result.text + "\n"
    assert result.uncertain == (UncertainWord("mitocondria", "- La [[?mitocondria]]", 3, 6),)
    [request] = fake.requests
    prompt = load_prompt("page_transcription")
    assert request.role == "transcriber"
    assert request.prompt_hash == prompt.hash
    assert request.system[0]["text"] == prompt.content
    assert request.messages == [{"role": "user", "content": request_content(page)}]
    assert not request.tools
    # The transcription is not a source of its own.
    assert [s.path for s in list_sources(tmp_vault, *topic)] == [source_path]


def test_an_empty_answer_or_a_refusal_is_an_error_and_writes_nothing(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_path = _store(tmp_vault, topic, desk_still())
    fake = FakeClaude().reply_text("   ").reply_text("no", stop_reason="refusal")
    client = fake.client("transcriber")
    with pytest.raises(TranscriptionError):
        asyncio.run(transcribe_page(client, _page(), tmp_vault, source_path))
    with pytest.raises(RefusalError):
        asyncio.run(transcribe_page(client, _page(), tmp_vault, source_path))
    assert not (tmp_vault.path / source_path.replace(".jpg", ".md")).exists()


# -- the vault writer ------------------------------------------------------------------------------


def test_put_page_transcription_accepts_the_page_or_its_derived_files_only(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_path = _store(tmp_vault, topic, desk_still())
    page_path = source_path.replace(".jpg", ".page.jpg")
    written = put_page_transcription(tmp_vault, page_path, "# Uno\n")
    assert written == tmp_vault.path / source_path.replace(".jpg", ".md")
    assert put_page_transcription(tmp_vault, source_path, "# Dos\n") == written
    assert written.read_text(encoding="utf-8") == "# Dos\n"

    base = source_path.rsplit("/", 1)[0]
    with pytest.raises(SourceNotFoundError):
        put_page_transcription(tmp_vault, f"{base}/page-009.jpg", "x")
    with pytest.raises(SourcePathError):
        put_page_transcription(tmp_vault, f"{base}/otra.jpg", "x")
    with pytest.raises(SourcePathError):
        put_page_transcription(tmp_vault, base.replace("/notes", "/web") + "/page-001.jpg", "x")
    with pytest.raises(SourcePathError):
        put_page_transcription(tmp_vault, "../page-001.jpg", "x")
