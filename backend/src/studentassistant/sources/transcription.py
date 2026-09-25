"""Page transcription (role `transcriber`, vision): one stored page becomes Markdown.

`transcribe_page` reads a stored capture through the vault -- its sidecar, its page image
(`page-NNN.page.jpg`) and, when the crop is doubtful, the original still (`page-NNN.jpg`) -- plus
the spoken hints (the session's transcript segments overlapping the sidecar's `transcript_window`)
and the source context (subject, topic, source kind, page number), asks Claude once with the
`page_transcription` prompt, and writes the answer as `page-NNN.md` next to the page
(`vault.put_page_transcription`). The answer keeps the page's structure and marks doubtful words
as `[[?word]]` and illegible ones as `[[?]]`; `find_uncertain` lists those marks, each of which
becomes a pending item of category `illegible` (`pending_ops`).

The crop is doubtful when a page was detected but covers less than `transcription_min_crop_share`
of the still (a box on the sheet may have been taken for the sheet). When no page was detected the
page image is the whole still already, so the original adds nothing and is not sent.

A textbook page (source kind `book`, #58) is read with its own prompt, `page_transcription_book`
(printed text: running headers left out, panels as block quotes), which also ends the answer with
the page number printed on the page (`<!-- página impresa: 83 -->`). That line is taken off the
Markdown (`split_printed_page`); the number the student said around the photo ("página 83",
`spoken_page_number`) stands in when none is printed. The page number found (`book_page`, with
`book_page_from` `image` or `speech`) goes to the page's sidecar (`vault.update_page_meta`), where
the editor reads it to cite the page as the book's page, together with the topic's book title
(`vault.get_book`), which the request names too.

Nothing here retries or schedules: `transcriber.PageTranscriber` does, on the session bus.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import cv2
import numpy as np

from studentassistant.config import SourcesSettings
from studentassistant.llm import LLMClient, LLMError, LLMResponse, RefusalError, load_prompt
from studentassistant.observer import AddPending
from studentassistant.vault import (
    TranscriptSegment,
    Vault,
    get_book,
    get_subject,
    get_topic,
    put_page_transcription,
    read_session_transcript,
    read_source,
    update_page_meta,
)

PROMPT_NAME = "page_transcription"
BOOK_PROMPT_NAME = "page_transcription_book"
BOOK_KIND = "book"

# `[[?palabra]]` (a doubtful reading) or `[[?]]` (illegible); never across lines or brackets.
UNCERTAIN_MARK = re.compile(r"\[\[\?([^\[\]\n]*)\]\]")

# How the source kinds read in the request and, in Spanish, in a pending item.
_KIND_DESCRIPTIONS = {
    "notes": "handwritten class notes",
    "book": "a printed textbook page",
    "pdf": "a page of a PDF document",
}
_KIND_NAMES_ES = {"notes": "apuntes", "book": "libro", "pdf": "PDF", "web": "web"}
_PAGE_STEM = re.compile(r"^page-(\d{3,})\.")
_FENCE = re.compile(r"^```[A-Za-z]*\n(?P<body>.*)\n```$", re.DOTALL)
_LINE_EXCERPT = 160

# The last line of a textbook page's answer: `<!-- página impresa: 83 -->` or `... ninguna -->`.
PRINTED_PAGE_LINE = re.compile(
    r"\n?[ \t]*<!--\s*p[áa]gina\s+impresa\s*:\s*(?P<value>[^\n>]*?)\s*-->[ \t]*$",
    re.IGNORECASE,
)
MAX_BOOK_PAGE = 9999

BookPageFrom = Literal["image", "speech"]

# "página 83", "pág. 83", "la página ochenta y tres", "pagina número 7".
_SPOKEN_PAGE = re.compile(
    r"\bp(?:[áa]gina|[áa]g\.?|g\.)(?:\s+n[úu]mero|\s*n\.?[ºo])?",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciséis": 16, "dieciseis": 16,
    "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21,
    "veintiún": 21, "veintiuna": 21, "veintidós": 22, "veintidos": 22, "veintitrés": 23,
    "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiséis": 26,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30,
    "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80,
    "noventa": 90, "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200,
    "trescientos": 300, "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400,
    "quinientos": 500, "quinientas": 500, "seiscientos": 600, "seiscientas": 600,
    "setecientos": 700, "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
    "novecientos": 900, "novecientas": 900, "mil": 1000,
}  # fmt: skip


class TranscriptionError(LLMError):
    """Claude answered, but not with a transcription (empty text); worth another attempt."""


@dataclass(frozen=True)
class UncertainWord:
    """One `[[?...]]` mark of a transcription: the reading (`None` when illegible), its line and
    where it is (1-based line number among the text's lines, 1-based column of the mark)."""

    word: str | None
    line: str
    line_number: int = 0
    column: int = 0


@dataclass(frozen=True)
class PageInput:
    """What one page transcription sends: the images (JPEG bytes) and the text parts."""

    page_image: bytes
    original_image: bytes | None
    hints: tuple[TranscriptSegment, ...]
    session_t_ms: int
    subject: str
    topic: str
    source_kind: str
    page_number: int | None
    book_title: str | None = None
    spoken_page: int | None = None


@dataclass(frozen=True)
class BookPage:
    """The page number of a textbook page: the one printed on it, the one the student said, and
    the one kept (`number`, from `number_from`): printed first, else spoken."""

    printed: int | None
    spoken: int | None

    @property
    def number(self) -> int | None:
        return self.printed if self.printed is not None else self.spoken

    @property
    def number_from(self) -> BookPageFrom | None:
        if self.printed is not None:
            return "image"
        return "speech" if self.spoken is not None else None

    def meta(self) -> dict[str, Any]:
        """The sidecar keys this page number is recorded under."""
        return {
            "book_page": self.number,
            "book_page_from": self.number_from,
            "book_page_printed": self.printed,
            "book_page_spoken": self.spoken,
        }


@dataclass(frozen=True)
class PageTranscription:
    """A transcribed page: the Markdown, where it was written and its uncertain words; for a
    textbook page, its page number too."""

    text: str
    path: str
    uncertain: tuple[UncertainWord, ...]
    page_input: PageInput
    response: LLMResponse
    prompt_hash: str
    book_page: BookPage | None = None


# -- pure pieces -----------------------------------------------------------------------------------


def find_uncertain(text: str) -> list[UncertainWord]:
    """Every `[[?word]]` / `[[?]]` mark of `text`, in order, with the line it is on."""
    found: list[UncertainWord] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in UNCERTAIN_MARK.finditer(line):
            word = match.group(1).strip()
            found.append(
                UncertainWord(
                    word=word or None,
                    line=line.strip(),
                    line_number=number,
                    column=match.start() + 1,
                )
            )
    return found


def clean_markdown(text: str) -> str:
    """The answer's Markdown, stripped, without a code fence wrapped around the whole of it."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced is not None and not fenced.group(0).lower().startswith("```mermaid"):
        stripped = fenced.group("body").strip()
    return stripped


def prompt_name(source_kind: str) -> str:
    """The prompt a page of `source_kind` is transcribed with: printed text for `book`."""
    return BOOK_PROMPT_NAME if source_kind == BOOK_KIND else PROMPT_NAME


def _page_value(value: int) -> int | None:
    return value if 0 < value <= MAX_BOOK_PAGE else None


def split_printed_page(text: str) -> tuple[str, int | None]:
    """The Markdown without its last `<!-- página impresa: N -->` line, and `N` (`None` for
    `ninguna`, anything that is not a page number, or no such line)."""
    match = PRINTED_PAGE_LINE.search(text)
    if match is None:
        return text, None
    value = match.group("value").strip()
    number = _page_value(int(value)) if value.isdigit() else None
    return text[: match.start()].rstrip(), number


def _spelled_number(words: Sequence[str]) -> int | None:
    """The number the leading Spanish number words spell (`ochenta y tres` -> 83), if any."""
    total, current, seen = 0, 0, False
    for index, word in enumerate(words):
        if word == "y" and seen and index + 1 < len(words) and words[index + 1] in _NUMBER_WORDS:
            continue
        value = _NUMBER_WORDS.get(word)
        if value is None:
            break
        seen = True
        if value == 1000:
            total += max(current, 1) * 1000
            current = 0
        else:
            current += value
    return total + current if seen else None


def _number_after(rest: str) -> int | None:
    digits = re.match(r"\s*(\d{1,4})\b", rest)
    if digits is not None:
        return _page_value(int(digits.group(1)))
    words = re.findall(r"[a-záéíóúüñ]+", rest.lower())[:8]
    spelled = _spelled_number(words)
    return None if spelled is None else _page_value(spelled)


def spoken_page_number(hints: Sequence[TranscriptSegment], session_t_ms: int) -> int | None:
    """The page number the student said around the photo ("página 83", "pág. 83", "la página
    ochenta y tres"): of the segments naming one, the nearest to the capture time (the earlier
    on a tie); within a segment, the last page named. `None` when no segment names a page."""
    best: tuple[int, int, int] | None = None  # (distance, t_start, page)
    for segment in hints:
        found = None
        for match in _SPOKEN_PAGE.finditer(segment.text):
            number = _number_after(segment.text[match.end() :])
            if number is not None:
                found = number
        if found is None:
            continue
        if segment.t_start <= session_t_ms <= segment.t_end:
            distance = 0
        else:
            distance = min(abs(segment.t_start - session_t_ms), abs(segment.t_end - session_t_ms))
        candidate = (distance, segment.t_start, found)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return None if best is None else best[2]


def hint_segments(
    segments: Iterable[TranscriptSegment], window: Mapping[str, Any] | None
) -> tuple[TranscriptSegment, ...]:
    """The segments overlapping the capture's transcript window, in time order (none without
    a window)."""
    if not window:
        return ()
    start, end = int(window.get("t_start", 0)), int(window.get("t_end", 0))
    kept = [s for s in segments if s.t_end >= start and s.t_start <= end and s.text.strip()]
    return tuple(sorted(kept, key=lambda s: (s.t_start, s.seq)))


def page_number(path: str) -> int | None:
    match = _PAGE_STEM.match(PurePosixPath(path).name)
    return None if match is None else int(match.group(1))


def crop_is_doubtful(meta: Mapping[str, Any], page_image: bytes, settings: SourcesSettings) -> bool:
    """Whether the detected page covers less of the still than `transcription_min_crop_share`."""
    if not meta.get("page_detected"):
        return False
    width, height = meta.get("width_px"), meta.get("height_px")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return False
    decoded = cv2.imdecode(np.frombuffer(page_image, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if decoded is None:
        return True
    share = (decoded.shape[0] * decoded.shape[1]) / (width * height)
    return share < settings.transcription_min_crop_share


def _image_block(data: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def render_request_text(page: PageInput) -> str:
    """The text part of the request: the source context, then the spoken hints."""
    kind = _KIND_DESCRIPTIONS.get(page.source_kind, page.source_kind)
    if page.source_kind == BOOK_KIND:
        # A book page's own number is the one printed on it; the stored number only counts photos.
        title = f", from the book «{page.book_title}»" if page.book_title else ""
        number = title + ("" if page.page_number is None else f" (photo {page.page_number})")
    else:
        number = "" if page.page_number is None else f", page {page.page_number}"
    images = (
        "The first image is the cropped page; the second is the original photo (the crop may "
        "have cut part of the page off)."
        if page.original_image is not None
        else "The image is the page."
    )
    lines = [
        f"Subject: {page.subject}",
        f"Topic: {page.topic}",
        f"Source: {kind}{number}",
        images,
        "",
    ]
    if page.hints:
        lines.append(
            "What the student said around the photo (taken at "
            f"{page.session_t_ms / 1000:.1f} s), as hints for words you cannot read:"
        )
        lines.extend(
            f"- [{s.t_start / 1000:.1f}-{s.t_end / 1000:.1f} s] {s.text.strip()}"
            for s in page.hints
        )
    else:
        lines.append("The student said nothing around the photo.")
    if page.source_kind == BOOK_KIND:
        lines += ["", "Transcribe the page, then give the page number printed on it."]
    else:
        lines += ["", "Transcribe the page."]
    return "\n".join(lines)


def request_content(page: PageInput) -> list[dict[str, Any]]:
    """The user turn's blocks: the image(s) first, then the text."""
    blocks = [_image_block(page.page_image)]
    if page.original_image is not None:
        blocks.append(_image_block(page.original_image))
    blocks.append({"type": "text", "text": render_request_text(page)})
    return blocks


def pending_ops(
    uncertain: Sequence[UncertainWord],
    *,
    session_id: str,
    capture_id: str,
    source_kind: str,
    page_number: int | None,
    book_page: int | None = None,
) -> list[AddPending]:
    """One `add_pending` (kind `illegible`, the capture as its page ref) per uncertain word.

    The text names the mark's line and column in the transcription: the pending queue merges an
    open item of the same kind whose text is alike, unless the two name different numbers
    (`observer.pending.is_duplicate`), so two marks of one page, even on one line, stay two items.

    A textbook page is named by `book_page`, its printed (or spoken) page number, when known.

    Ids are `ill-<session>-<capture>-<n>` (`n` from 1), unique in the topic because a capture id is
    unique in its session and a capture is transcribed once per `capture.stored`.
    """
    where = _KIND_NAMES_ES.get(source_kind, source_kind)
    if source_kind == BOOK_KIND:
        # The book's own page number when known; the stored number only counts the photos.
        if book_page is not None:
            page = f"la página {book_page} del libro"
        elif page_number is not None:
            page = f"la foto {page_number} del libro"
        else:
            page = "una página del libro"
    else:
        page = f"la página {page_number}" if page_number is not None else "una página"
        page += f" ({where})"
    ops: list[AddPending] = []
    for index, mark in enumerate(uncertain, start=1):
        line = mark.line
        if len(line) > _LINE_EXCERPT:
            line = line[: _LINE_EXCERPT - 1].rstrip() + "…"
        at = f"{page}, línea {mark.line_number}, columna {mark.column}"
        if mark.word is None:
            text = f"Palabra ilegible en {at}: «{line}»"
        else:
            text = f"Palabra dudosa «{mark.word}» en {at}: «{line}»"
        ops.append(
            AddPending(
                pending_id=f"ill-{session_id}-{capture_id}-{index}",
                kind="illegible",
                text=text,
                capture_ids=[capture_id],
            )
        )
    return ops


# -- vault reads (blocking: run in a worker thread) -----------------------------------------------


def read_page_input(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    session_id: str,
    source_path: str,
    page_path: str | None,
    settings: SourcesSettings,
    extra_segments: Iterable[TranscriptSegment] = (),
) -> PageInput:
    """Everything a page transcription sends, read from the vault.

    `extra_segments` are final segments known in memory that may not be in `transcript.jsonl`
    yet; a segment in both (same span and text) counts once.
    """
    still = read_source(vault, source_path)
    meta = still.meta or {}
    page_image = read_source(vault, page_path).content if page_path else still.content
    original = still.content if page_path and crop_is_doubtful(meta, page_image, settings) else None
    segments = list(read_session_transcript(vault, subject_slug, topic_slug, session_id))
    seen = {(s.t_start, s.t_end, s.text) for s in segments}
    for segment in extra_segments:
        if (segment.t_start, segment.t_end, segment.text) not in seen:
            segments.append(segment)
            seen.add((segment.t_start, segment.t_end, segment.text))
    hints = hint_segments(segments, meta.get("transcript_window"))
    session_t_ms = int(meta.get("session_t_ms") or 0)
    kind = str(meta.get("source_context") or PurePosixPath(source_path).parent.name)
    book_title = spoken = None
    if kind == BOOK_KIND:
        book = get_book(vault, subject_slug, topic_slug)
        book_title = None if book is None else book.title
        spoken = spoken_page_number(hints, session_t_ms)
    return PageInput(
        page_image=page_image,
        original_image=original,
        hints=hints,
        session_t_ms=session_t_ms,
        subject=get_subject(vault, subject_slug).subject.name,
        topic=get_topic(vault, subject_slug, topic_slug).topic.title,
        source_kind=kind,
        page_number=page_number(source_path),
        book_title=book_title,
        spoken_page=spoken,
    )


# -- one call --------------------------------------------------------------------------------------


async def transcribe_page(
    client: LLMClient,
    page: PageInput,
    vault: Vault,
    source_path: str,
) -> PageTranscription:
    """Ask Claude for the page's Markdown and store it as `page-NNN.md` (in a worker thread).

    A textbook page (`source_kind` `book`) uses the printed-text prompt; its printed page number
    is taken off the answer and, with the spoken one, written to the page's sidecar.

    Raises:
        CostCapReachedError: a cost cap is reached; nothing was sent.
        RefusalError: Claude refused the page.
        TranscriptionError: the answer holds no text.
        LLMError: any other failure of the call; VaultError/OSError from the write.
    """
    prompt = load_prompt(prompt_name(page.source_kind))
    response = await client.create(
        [{"role": "user", "content": request_content(page)}],
        system=prompt.content,
        prompt_hash=prompt.hash,
    )
    if response.stop_reason == "refusal":
        raise RefusalError("Claude refused to transcribe the page")
    text = clean_markdown(response.text)
    book_page = None
    if page.source_kind == BOOK_KIND:
        text, printed = split_printed_page(text)
        text = clean_markdown(text)
        book_page = BookPage(printed=printed, spoken=page.spoken_page)
    if not text:
        raise TranscriptionError("the transcription answer holds no text")
    written = await asyncio.to_thread(put_page_transcription, vault, source_path, text + "\n")
    if book_page is not None:
        await asyncio.to_thread(update_page_meta, vault, source_path, book_page.meta())
    return PageTranscription(
        text=text,
        path=written.relative_to(vault.path).as_posix(),
        uncertain=tuple(find_uncertain(text)),
        page_input=page,
        response=response,
        prompt_hash=prompt.hash,
        book_page=book_page,
    )


__all__ = [
    "BOOK_KIND",
    "BOOK_PROMPT_NAME",
    "PRINTED_PAGE_LINE",
    "PROMPT_NAME",
    "UNCERTAIN_MARK",
    "BookPage",
    "PageInput",
    "PageTranscription",
    "TranscriptionError",
    "UncertainWord",
    "clean_markdown",
    "crop_is_doubtful",
    "find_uncertain",
    "hint_segments",
    "page_number",
    "pending_ops",
    "prompt_name",
    "read_page_input",
    "render_request_text",
    "request_content",
    "split_printed_page",
    "spoken_page_number",
    "transcribe_page",
]
