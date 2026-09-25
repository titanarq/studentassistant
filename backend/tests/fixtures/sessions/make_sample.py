"""Generate the synthetic client-mode recording `sessions/sample/` that replay tests feed back.

Run from `backend/` with `uv run python tests/fixtures/sessions/make_sample.py`; it rewrites
`sample/` from scratch. The page image is rendered here (a few typed Spanish lines on a small
white page), never a photo of real notes, so the fixture holds nothing personal and stays tiny.

The session: the student reads their notes about the cell, switches to the textbook with the
`switch_source` button, photographs the book page (one-image burst, button trigger) and talks
about it. Every client time is an offset from `STARTED_CLIENT_TIME_MS`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf

from studentassistant.protocol import (
    Button,
    CaptureImage,
    CaptureUploadRequest,
    TranscriptClientFinal,
    TranscriptClientPartial,
)
from studentassistant.server.recording import RecordingManifest, RecordingWriter

SAMPLE_DIR = Path(__file__).parent / "sample"

STARTED_CLIENT_TIME_MS = 1_760_000_000_000
PROVIDER = "web-speech"
LANGUAGE = "es-ES"
CAPTURE_ID = "5f0c2a7e-8d4b-4e61-9b3a-2c7d1e0f4a58"
PAGE_WIDTH_PX = 240
PAGE_HEIGHT_PX = 320

PAGE_LINES = (
    "Tema 3. La célula",
    "",
    "La célula es la unidad",
    "básica de los seres vivos.",
    "Partes: membrana,",
    "citoplasma y núcleo.",
)

# (segment id, offset of its start, offset of its end, partial texts, final text)
SEGMENTS = (
    (
        "seg-1",
        500,
        3500,
        ("la célula", "la célula es la unidad"),
        "La célula es la unidad básica de los seres vivos.",
    ),
    (
        "seg-2",
        4000,
        7000,
        ("tiene tres partes",),
        "Tiene tres partes: membrana, citoplasma y núcleo.",
    ),
    (
        "seg-3",
        11000,
        14500,
        ("en el libro",),
        "En el libro pone que el núcleo guarda el material genético.",
    ),
)
SWITCH_SOURCE_OFFSET_MS = 8000
CAPTURE_OFFSET_MS = 9500


def render_page_jpeg() -> bytes:
    """A small JPEG of a white page with `PAGE_LINES` typed on it."""
    document = pymupdf.open()
    try:
        page = document.new_page(width=PAGE_WIDTH_PX, height=PAGE_HEIGHT_PX)
        for number, line in enumerate(PAGE_LINES):
            page.insert_text((16, 32 + 22 * number), line, fontsize=14)
        pixmap = page.get_pixmap(alpha=False)
        return pixmap.tobytes("jpeg", jpg_quality=70)
    finally:
        document.close()


def manifest() -> RecordingManifest:
    return RecordingManifest(
        format_version=1,
        subject="biologia",
        topic="la-celula",
        language=LANGUAGE,
        stt_mode="client",
        stt_provider=PROVIDER,
        started_client_time_ms=STARTED_CLIENT_TIME_MS,
    )


def write_sample(directory: Path = SAMPLE_DIR) -> None:
    """Write the whole recording into `directory`, replacing whatever it held."""
    shutil.rmtree(directory, ignore_errors=True)
    start = STARTED_CLIENT_TIME_MS
    with RecordingWriter(directory, manifest()) as writer:
        for segment_id, start_ms, end_ms, partials, final in SEGMENTS:
            span = end_ms - start_ms
            for index, text in enumerate(partials, start=1):
                writer.append_transcript(
                    TranscriptClientPartial(
                        type="transcript.client.partial",
                        segment_id=segment_id,
                        client_start_ms=start + start_ms,
                        client_end_ms=start + start_ms + span * index // (len(partials) + 1),
                        text=text,
                        provider=PROVIDER,
                        language=LANGUAGE,
                    )
                )
            writer.append_transcript(
                TranscriptClientFinal(
                    type="transcript.client.final",
                    segment_id=segment_id,
                    client_start_ms=start + start_ms,
                    client_end_ms=start + end_ms,
                    text=final,
                    provider=PROVIDER,
                    language=LANGUAGE,
                )
            )
        writer.append_event(
            Button(
                type="button",
                button="switch_source",
                source="book",
                client_time_ms=start + SWITCH_SOURCE_OFFSET_MS,
            )
        )
        writer.add_capture(
            CaptureUploadRequest(
                capture_id=CAPTURE_ID,
                trigger="button",
                client_time_ms=start + CAPTURE_OFFSET_MS,
                images=[
                    CaptureImage(
                        part="image_0",
                        content_type="image/jpeg",
                        width_px=PAGE_WIDTH_PX,
                        height_px=PAGE_HEIGHT_PX,
                        client_time_ms=start + CAPTURE_OFFSET_MS,
                    )
                ],
            ),
            {"image_0": render_page_jpeg()},
        )


if __name__ == "__main__":
    write_sample()
