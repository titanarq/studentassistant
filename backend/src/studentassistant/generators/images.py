"""Images of the notes inside generated material: exam PDFs and the Anki package (#511).

The notes show a topic's images -- pasted, cropped, and the SVG diagrams the editor draws -- as
`![Diagrama 1](../sources/images/img-001.svg)`. A generator may copy such a link into an item (an
exam question needing the figure, a flashcard asking about it; the prompts say so), and the
Markdown outputs keep it as it is: from `generated/` the link resolves to the topic's
`sources/images/` just as it does from `notes/`. The printed exports cannot follow a link, so:

- `note_images(context, texts)` reads, through `vault.read_source`, every image a link in `texts`
  names (only `sources/images/img-NNN.<png|jpg|webp|svg>` of the same topic; a missing or
  unreadable file, or an SVG that is not sanitized, is left out and the link stays text);
- `pdf_image` gives what the exam PDF embeds: the bytes as they are, an SVG rasterized to PNG
  (`svg_to_png`, PyMuPDF, no browser);
- `split_images(text)` splits a text around its image links, for each export to render its own way.

A Mermaid fence is not an image and keeps its behaviour (see `diagrams`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

import pymupdf

from studentassistant.vault import (
    SVG_MEDIA_TYPE,
    SourceNotFoundError,
    SourcePathError,
    Vault,
    is_safe_svg,
    read_source,
    topic_directory,
)

logger = logging.getLogger(__name__)

IMAGE_LINK = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\.\./(?P<source>sources/images/img-\d{3,}\.(?:png|jpg|webp|svg))\)"
)
"""An image link of the notes, as the editor and the notes editor write it."""
_SVG_DPI = 200


@dataclass(frozen=True)
class NoteImage:
    """One image of the topic a generated text links: its source id, bytes and media type."""

    source_id: str
    data: bytes
    media_type: str

    @property
    def name(self) -> str:
        """The file name (`img-001.svg`)."""
        return PurePosixPath(self.source_id).name


def split_images(text: str) -> Iterator[str | tuple[str, str]]:
    """`text` as its pieces: plain strings and, for each image link, `(alt, source id)`."""
    done = 0
    for match in IMAGE_LINK.finditer(text):
        if match.start() > done:
            yield text[done : match.start()]
        yield match["alt"], match["source"]
        done = match.end()
    if done < len(text):
        yield text[done:]


def note_images(
    vault: Vault, subject: str, topic: str, texts: Iterable[str]
) -> dict[str, NoteImage]:
    """The images the links in `texts` name, by source id; what cannot be read is left out."""
    wanted = {match["source"] for text in texts for match in IMAGE_LINK.finditer(text)}
    if not wanted:
        return {}
    prefix = topic_directory(vault, subject, topic).relative_to(vault.path).as_posix()
    found: dict[str, NoteImage] = {}
    for source_id in sorted(wanted):
        try:
            source = read_source(vault, f"{prefix}/{source_id}")
        except (SourcePathError, SourceNotFoundError):
            continue
        if source.media_type == SVG_MEDIA_TYPE and not is_safe_svg(source.content):
            logger.warning("left out the unsanitized SVG %s", source_id)
            continue
        found[source_id] = NoteImage(source_id, source.content, source.media_type)
    return found


def svg_to_png(data: bytes, *, dpi: int = _SVG_DPI) -> bytes:
    """A sanitized SVG drawn as PNG by PyMuPDF (no browser, no network)."""
    with pymupdf.open(stream=data, filetype="svg") as document:
        return document[0].get_pixmap(dpi=dpi).tobytes("png")


def pdf_image(image: NoteImage) -> tuple[str, bytes] | None:
    """`(file name, bytes)` the exam PDF embeds for `image` (an SVG as PNG), `None` if it
    cannot be drawn."""
    if image.media_type != SVG_MEDIA_TYPE:
        return image.name, image.data
    try:
        return f"{PurePosixPath(image.name).stem}.png", svg_to_png(image.data)
    except Exception:
        logger.exception("could not draw the SVG %s", image.source_id)
        return None


__all__ = [
    "IMAGE_LINK",
    "NoteImage",
    "note_images",
    "pdf_image",
    "split_images",
    "svg_to_png",
]
