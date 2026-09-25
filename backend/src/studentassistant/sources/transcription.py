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

Nothing here retries or schedules: `transcriber.PageTranscriber` does, on the session bus.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import cv2
import numpy as np

from studentassistant.config import SourcesSettings
from studentassistant.llm import LLMClient, LLMError, LLMResponse, RefusalError, load_prompt
from studentassistant.observer import AddPending
from studentassistant.vault import (
    TranscriptSegment,
    Vault,
    get_subject,
    get_topic,
    put_page_transcription,
    read_session_transcript,
    read_source,
)

PROMPT_NAME = "page_transcription"

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


@dataclass(frozen=True)
class PageTranscription:
    """A transcribed page: the Markdown, where it was written and its uncertain words."""

    text: str
    path: str
    uncertain: tuple[UncertainWord, ...]
    page_input: PageInput
    response: LLMResponse
    prompt_hash: str


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
) -> list[AddPending]:
    """One `add_pending` (kind `illegible`, the capture as its page ref) per uncertain word.

    The text names the mark's line and column in the transcription: the pending queue merges an
    open item of the same kind whose text is alike, unless the two name different numbers
    (`observer.pending.is_duplicate`), so two marks of one page, even on one line, stay two items.

    Ids are `ill-<session>-<capture>-<n>` (`n` from 1), unique in the topic because a capture id is
    unique in its session and a capture is transcribed once per `capture.stored`.
    """
    where = _KIND_NAMES_ES.get(source_kind, source_kind)
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
    return PageInput(
        page_image=page_image,
        original_image=original,
        hints=hint_segments(segments, meta.get("transcript_window")),
        session_t_ms=int(meta.get("session_t_ms") or 0),
        subject=get_subject(vault, subject_slug).subject.name,
        topic=get_topic(vault, subject_slug, topic_slug).topic.title,
        source_kind=str(meta.get("source_context") or PurePosixPath(source_path).parent.name),
        page_number=page_number(source_path),
    )


# -- one call --------------------------------------------------------------------------------------


async def transcribe_page(
    client: LLMClient,
    page: PageInput,
    vault: Vault,
    source_path: str,
) -> PageTranscription:
    """Ask Claude for the page's Markdown and store it as `page-NNN.md` (in a worker thread).

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
        raise RefusalError("Claude refused to transcribe the page")
    text = clean_markdown(response.text)
    if not text:
        raise TranscriptionError("the transcription answer holds no text")
    written = await asyncio.to_thread(put_page_transcription, vault, source_path, text + "\n")
    return PageTranscription(
        text=text,
        path=written.relative_to(vault.path).as_posix(),
        uncertain=tuple(find_uncertain(text)),
        page_input=page,
        response=response,
        prompt_hash=prompt.hash,
    )


__all__ = [
    "PROMPT_NAME",
    "UNCERTAIN_MARK",
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
    "read_page_input",
    "render_request_text",
    "request_content",
    "transcribe_page",
]
