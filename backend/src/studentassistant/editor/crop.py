"""Crop one region of a stored page image: Sonnet locates the region (`locate_region`).

The student asks for only part of a page ("solo el diagrama de la página 3"); Claude (role
`observer`, Sonnet by default -- no model id here) receives the page as an image content block and
the description, and answers through the strict tool `crop_region` (`llm.structured`) with a
`BoundingBox`: four fractions of the image's width/height in `[0, 1]`, `x0 < x1`, `y0 < y1`. A box
out of these bounds fails validation and is re-asked by `structured` itself, never used raw. The
prompt (`prompts/editor_crop.md`) asks for a tight box that leaves out blank margins, the desk and
fingers holding the page: that is the whole reframing step.

The cleanup (`clean_crop`) is deterministic `numpy`/`cv2` code with no LLM call, built from
`studentassistant.sources.captures`' building blocks: the image is cut to the box, the cut's
sharpness (`captures.sharpness`) is measured and a blurry one refused (`BlurryCropError`,
`[editor] crop_min_sharpness`), then `captures.find_page` searches the cut for a quadrilateral
(a sheet, a card, a framed figure photographed slightly askew); when it finds one,
`captures.crop_page` warps it flat, and otherwise the plain axis-aligned cut is kept. A PNG source
gives a PNG crop, anything else a JPEG. `clean_crop_async` runs it in a worker thread.

`crop_source_image` puts the three together: it reads a stored page with `vault.read_source`
(never modifying it), locates the region, cleans the cut up and stores it as a new `images` source
through `vault.put_source`, whose sidecar records `origin: cropped`, `cropped_from` (the page's
vault-relative path), `bbox`, `requested_region`, `content_type`, `sha256` and `added_at`. What it
returns (`CroppedImage`) carries the footnote (`notes_format.cropped_image_provenance`,
«Imagen recortada N») and the image link a caller writes into the notes; nothing is committed.

Claude is reached only through `studentassistant.llm` (ADR-0004).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Self

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from studentassistant.config import DEFAULT_CAPTURE_JPEG_QUALITY, Settings
from studentassistant.editor.edits import EditOp, NewFootnote
from studentassistant.editor.inputs import IMAGE_MEDIA_TYPES
from studentassistant.editor.notes_format import (
    LINK_PREFIX,
    Provenance,
    cropped_image_provenance,
)
from studentassistant.llm import (
    LedgerBinding,
    LLMClient,
    RefusalError,
    StructuredOutputError,
    get_client,
    load_prompt,
    strict_tool,
    structured,
)
from studentassistant.sources.captures import (
    crop_page,
    decode_image,
    encode_jpeg,
    find_page,
    sharpness,
)
from studentassistant.vault import Vault, put_source, read_source, topic_directory
from studentassistant.vault.sources import SIDECAR_SUFFIX

PROMPT_NAME = "editor_crop"
ROLE = "observer"
TOOL_NAME = "crop_region"
TOOL_DESCRIPTION = (
    "Give the tight bounding box of the requested region, as fractions of the image's width "
    "and height."
)
MAX_TOKENS = 300

UNSUPPORTED_IMAGE_MESSAGE = "La fuente indicada no es una imagen que se pueda recortar."
EMPTY_REGION_MESSAGE = "Indica qué parte de la página quieres recortar."
REGION_NOT_FOUND_MESSAGE = (
    "No he podido localizar la parte de la página que pides; prueba a describirla de otra forma."
)
_PIXEL_DECIMALS = 6
CROPPED_ORIGIN = "cropped"
IMAGES_KIND = "images"
BLURRY_CROP_MESSAGE = "El recorte solicitado sale borroso; prueba con otra foto de la página."


class CropError(ValueError):
    """A crop could not be made; its message is Spanish, for the student."""


class RegionNotFoundError(CropError):
    """Claude declined, or gave no valid box even after the re-ask."""


class BlurryCropError(CropError):
    """The cut region is below `[editor] crop_min_sharpness`."""


class BoundingBox(BaseModel):
    """A region as fractions of the image: `x` of its width, `y` of its height, from top-left."""

    model_config = ConfigDict(extra="forbid")

    x0: float = Field(ge=0, le=1, description="Left edge, fraction of the image width.")
    y0: float = Field(ge=0, le=1, description="Top edge, fraction of the image height.")
    x1: float = Field(ge=0, le=1, description="Right edge, fraction of the image width.")
    y1: float = Field(ge=0, le=1, description="Bottom edge, fraction of the image height.")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.x0 < self.x1:
            raise ValueError("x0 must be smaller than x1")
        if not self.y0 < self.y1:
            raise ValueError("y0 must be smaller than y1")
        return self

    def as_list(self) -> list[float]:
        """`[x0, y0, x1, y1]`, the form stored in a crop's sidecar."""
        return [self.x0, self.y0, self.x1, self.y1]


def crop_client(
    *,
    settings: Settings | None = None,
    transport: Any | None = None,
    ledger: LedgerBinding | None = None,
) -> LLMClient:
    """The client that locates regions: role `observer` from `[llm.roles.observer]`."""
    return get_client(ROLE, settings=settings, transport=transport, ledger=ledger)


def region_request(image: bytes, media_type: str, description: str) -> list[dict[str, Any]]:
    """The user message: the page as an image content block, then the requested region."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image).decode("ascii"),
                    },
                },
                {
                    "type": "text",
                    "text": "Locate this region of the page, in the student's words: "
                    + description.strip(),
                },
            ],
        }
    ]


async def locate_region(
    client: LLMClient, image: bytes, media_type: str, description: str
) -> BoundingBox:
    """The tight box Claude gives for `description` on `image`.

    Raises:
        CropError: `media_type` is not an image Claude reads, or `description` is empty.
        RegionNotFoundError: Claude declined, or no valid box came even after the re-ask.
        LLMError: any other failure of the call (a reached cap, exhausted retries).
    """
    if media_type not in IMAGE_MEDIA_TYPES:
        raise CropError(UNSUPPORTED_IMAGE_MESSAGE)
    if not description.strip():
        raise CropError(EMPTY_REGION_MESSAGE)
    prompt = load_prompt(PROMPT_NAME)
    try:
        result = await structured(
            client,
            region_request(image, media_type, description),
            BoundingBox,
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
            system=prompt.content,
            max_tokens=MAX_TOKENS,
            prompt_hash=prompt.hash,
        )
    except (RefusalError, StructuredOutputError) as error:
        raise RegionNotFoundError(REGION_NOT_FOUND_MESSAGE) from error
    return result.value


@dataclass(frozen=True)
class CleanCrop:
    """A cleaned-up crop, encoded: `data` in `content_type` (`extension` without the dot)."""

    data: bytes
    content_type: str
    extension: str
    width: int
    height: int
    # The cut's sharpness (before any warp), as `captures.sharpness` measures it.
    sharpness: float
    # Whether a quadrilateral was found in the cut and warped flat.
    deskewed: bool


def box_pixels(box: BoundingBox, width: int, height: int) -> tuple[int, int, int, int]:
    """`box` in pixels of a `width` x `height` image: `(left, top, right, bottom)`, right and
    bottom exclusive, outward-rounded, clamped to the image and at least one pixel each way."""
    left = min(max(math.floor(_pixel(box.x0, width)), 0), width - 1)
    top = min(max(math.floor(_pixel(box.y0, height)), 0), height - 1)
    right = max(min(math.ceil(_pixel(box.x1, width)), width), left + 1)
    bottom = max(min(math.ceil(_pixel(box.y1, height)), height), top + 1)
    return left, top, right, bottom


def _pixel(fraction: float, size: int) -> float:
    """`fraction` of `size`, with float noise (`0.55 * 800 == 440.00000000000006`) rounded off so
    an edge that falls on a pixel boundary is not pushed a pixel out."""
    return round(fraction * size, _PIXEL_DECIMALS)


def clean_crop(
    image: bytes,
    media_type: str,
    box: BoundingBox,
    *,
    min_sharpness: float,
    jpeg_quality: int = DEFAULT_CAPTURE_JPEG_QUALITY,
) -> CleanCrop:
    """`image` cut to `box`, refused when blurry, deskewed when a quadrilateral is found in it.

    Pure and deterministic: the same bytes and box always give byte-identical output.

    Raises:
        CropError: `image` does not decode as an image.
        BlurryCropError: the cut's sharpness is below `min_sharpness`.
    """
    decoded = decode_image(image)
    if decoded is None:
        raise CropError(UNSUPPORTED_IMAGE_MESSAGE)
    return clean_decoded(
        decoded, media_type, box, min_sharpness=min_sharpness, jpeg_quality=jpeg_quality
    )


def clean_decoded(
    decoded: np.ndarray,
    media_type: str,
    box: BoundingBox,
    *,
    min_sharpness: float,
    jpeg_quality: int = DEFAULT_CAPTURE_JPEG_QUALITY,
) -> CleanCrop:
    """`clean_crop` of an image already decoded (`captures.decode_image`: EXIF orientation
    applied), so the box maps to the pixels the locating call saw.

    Raises:
        BlurryCropError: the cut's sharpness is below `min_sharpness`.
    """
    height, width = decoded.shape[:2]
    left, top, right, bottom = box_pixels(box, width, height)
    region = decoded[top:bottom, left:right]
    score = sharpness(region)
    if score < min_sharpness:
        raise BlurryCropError(BLURRY_CROP_MESSAGE)
    corners = find_page(region)
    deskewed = corners is not None
    if corners is not None:
        region = crop_page(region, corners)
    data, content_type, extension = _encode(region, media_type, jpeg_quality)
    return CleanCrop(
        data=data,
        content_type=content_type,
        extension=extension,
        width=int(region.shape[1]),
        height=int(region.shape[0]),
        sharpness=score,
        deskewed=deskewed,
    )


def oriented_image(decoded: np.ndarray, media_type: str, jpeg_quality: int) -> tuple[bytes, str]:
    """`decoded` re-encoded for the locating call, `(bytes, media type)`: PNG for a PNG source,
    else JPEG. It carries no EXIF orientation, so Claude sees the pixels the box is applied to."""
    return _encode(decoded, media_type, jpeg_quality)[:2]


def _encode(image: np.ndarray, media_type: str, jpeg_quality: int) -> tuple[bytes, str, str]:
    """`(data, content type, extension)`: PNG for a PNG source, anything else a JPEG."""
    if media_type == "image/png":
        ok, encoded = cv2.imencode(".png", image)
        if not ok:  # pragma: no cover - OpenCV encodes any 8-bit BGR image
            raise RuntimeError("OpenCV could not encode the image as PNG")
        return encoded.tobytes(), "image/png", "png"
    return encode_jpeg(image, jpeg_quality), "image/jpeg", "jpg"


async def clean_crop_async(
    image: bytes,
    media_type: str,
    box: BoundingBox,
    *,
    min_sharpness: float,
    jpeg_quality: int = DEFAULT_CAPTURE_JPEG_QUALITY,
) -> CleanCrop:
    """`clean_crop` in a worker thread, off the event loop."""
    return await asyncio.to_thread(
        clean_crop,
        image,
        media_type,
        box,
        min_sharpness=min_sharpness,
        jpeg_quality=jpeg_quality,
    )


@dataclass(frozen=True)
class CroppedImage:
    """A crop stored by `crop_source_image`, with what the notes need to cite and show it."""

    # Vault-relative path of the new file (`subjects/.../sources/images/img-NNN.<ext>`).
    path: str
    # Topic-relative source id (`sources/images/img-NNN.<ext>`), as the notes cite it.
    source_id: str
    number: int
    box: BoundingBox
    crop: CleanCrop
    # The sidecar written next to the file.
    meta: dict[str, Any]
    # `«Imagen recortada N»`, pointing at the new file.
    provenance: Provenance

    @property
    def paths(self) -> list[str]:
        """The vault-relative files stored: the image and its sidecar."""
        directory, _, name = self.path.rpartition("/")
        return [self.path, f"{directory}/{name.split('.', 1)[0]}{SIDECAR_SUFFIX}"]

    @property
    def footnote_label(self) -> str:
        """`imgNNN`, the label a pasted image's footnote uses too."""
        return f"img{self.number:03d}"

    @property
    def footnote(self) -> str:
        """`[^imgNNN]: [Imagen recortada N](../sources/images/img-NNN.<ext>)`."""
        return self.provenance.definition(self.footnote_label)

    @property
    def markdown(self) -> str:
        """The image link: `![Imagen recortada N](../sources/images/img-NNN.<ext>)`."""
        return f"![{self.provenance.text}]({LINK_PREFIX}{self.source_id})"


async def crop_source_image(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    vault_relative_path: str,
    description: str,
    *,
    settings: Settings | None = None,
    client: LLMClient | None = None,
    added_at: datetime | None = None,
) -> CroppedImage:
    """Crop `description` out of the stored page image at `vault_relative_path` and store it.

    The page is read with `vault.read_source` and never modified; the region is located by
    Claude (`locate_region`, `client` or `crop_client(settings=settings)`), cleaned up
    (`clean_crop_async`, `[editor] crop_min_sharpness`, `[sources] capture_jpeg_quality`) and
    stored as a new `images` source of the topic with a `cropped` sidecar. Nothing is committed.

    Raises:
        SourcePathError, SourceNotFoundError: from `vault.read_source`; nothing is written.
        CropError: the source is not an image Claude reads, or `description` is empty.
        RegionNotFoundError: Claude found no region.
        BlurryCropError: the cut is too blurry to keep; nothing is written.
        LLMError: any other failure of the Claude call; nothing is written.
        Whatever `vault.put_source` raises for the new file.
    """
    settings = settings or Settings()
    source = read_source(vault, vault_relative_path)
    if source.media_type not in IMAGE_MEDIA_TYPES:
        raise CropError(UNSUPPORTED_IMAGE_MESSAGE)
    if not description.strip():
        raise CropError(EMPTY_REGION_MESSAGE)
    jpeg_quality = settings.sources.capture_jpeg_quality
    # Decoded once, before any Claude call: a file OpenCV cannot read (a GIF) is refused here,
    # and the box is located on the same oriented pixels it is applied to.
    decoded = await asyncio.to_thread(decode_image, source.content)
    if decoded is None:
        raise CropError(UNSUPPORTED_IMAGE_MESSAGE)
    shown, shown_type = await asyncio.to_thread(
        oriented_image, decoded, source.media_type, jpeg_quality
    )
    client = client or crop_client(settings=settings)
    box = await locate_region(client, shown, shown_type, description)
    crop = await asyncio.to_thread(
        partial(
            clean_decoded,
            decoded,
            source.media_type,
            box,
            min_sharpness=settings.editor.crop_min_sharpness,
            jpeg_quality=jpeg_quality,
        )
    )
    meta: dict[str, Any] = {
        "origin": CROPPED_ORIGIN,
        "cropped_from": vault_relative_path,
        "bbox": box.as_list(),
        "requested_region": description.strip(),
        "content_type": crop.content_type,
        "sha256": hashlib.sha256(crop.data).hexdigest(),
        "added_at": added_at or datetime.now(UTC),
    }
    stored = await asyncio.to_thread(
        put_source,
        vault,
        subject_slug,
        topic_slug,
        IMAGES_KIND,
        f"cropped.{crop.extension}",
        crop.data,
        meta,
    )
    number = int(stored.name.split(".", 1)[0].removeprefix("img-"))
    source_id = stored.relative_to(topic_directory(vault, subject_slug, topic_slug)).as_posix()
    return CroppedImage(
        path=stored.relative_to(vault.path).as_posix(),
        source_id=source_id,
        number=number,
        box=box,
        crop=crop,
        meta=meta,
        provenance=cropped_image_provenance(number, crop.extension),
    )


# -- the `crop_image` tool of the revise turn (#493) -----------------------------------------------

CROP_TOOL = "crop_image"
CROP_TOOL_DESCRIPTION = (
    "Crop one region of a page image the notes cite or the student selected in Recursos, and"
    " insert the cropped image, cited, where `op`/`section`/`block` say."
)
CROP_FAILED_PREFIX = "No he podido añadir el recorte: "
CROP_SOURCE_NOT_FOUND_MESSAGE = "No encuentro esa página entre las fuentes del tema."
_PDF_PAGE = re.compile(r"^(?P<file>sources/pdf/(?P<stem>[A-Za-z0-9_-]+)\.pdf)#page=(?P<page>\d+)$")


class CropImageRequest(BaseModel):
    """The input of the `crop_image` tool: which page, which region, where it goes."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        description="The source_id of the page, as the catalogue gives it (a PDF page as"
        " `sources/pdf/<file>.pdf#page=K`): one the notes cite or the student selected."
    )
    region: str = Field(
        description="The region to crop, in the student's words («el diagrama de la página 3»)."
    )
    op: Literal["insert_after", "replace_block"] = Field(
        description="`insert_after`: the image goes after block `block` (0 = first);"
        " `replace_block`: it replaces block `block`."
    )
    section: str = Field(description="The anchor of the section, without `#`.")
    block: int = Field(description="The block number within the section, as the block map shows.")
    summary: str = Field(description="What this turn changes, one short Spanish sentence.")


class CropRef(BaseModel):
    """What a turn's crop was: the new source (`source_id`, `None` when it failed) and the page."""

    model_config = ConfigDict(extra="forbid")

    source: str
    region: str
    source_id: str | None = None
    path: str | None = None
    error: str | None = None


def crop_image_tool() -> dict[str, Any]:
    """The strict `crop_image` tool definition offered by a revise turn."""
    return strict_tool(CROP_TOOL, CROP_TOOL_DESCRIPTION, CropImageRequest)


def crop_source_path(vault: Vault, subject_slug: str, topic_slug: str, source_id: str) -> str:
    """The vault-relative image of a topic-relative `source_id`: the file itself, or for a PDF
    page (`sources/pdf/page-001.pdf#page=3`) its rendered page (`sources/pdf/page-001.p003.jpg`).
    """
    ref = source_id.strip()
    match = _PDF_PAGE.match(ref)
    if match is not None:
        ref = f"sources/pdf/{match['stem']}.p{int(match['page']):03d}.jpg"
    topic = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path)
    return f"{topic.as_posix()}/{ref}"


def crop_edit(request: CropImageRequest, cropped: CroppedImage) -> tuple[EditOp, NewFootnote]:
    """The ordinary edit that shows and cites a crop: the image link with its footnote reference
    as the block's text, and the «Imagen recortada N» footnote definition."""
    label = cropped.footnote_label
    op = EditOp(
        op=request.op,
        section=request.section,
        block=request.block,
        text=f"{cropped.markdown}[^{label}]",
    )
    definition = cropped.footnote.removeprefix(f"[^{label}]: ")
    return op, NewFootnote(label=label, definition=definition)


def placeholder_edit(request: CropImageRequest) -> EditOp:
    """The request's edit with a stand-in text: checks the anchor before anything is cropped."""
    return EditOp(op=request.op, section=request.section, block=request.block, text="-")


__all__ = [
    "BLURRY_CROP_MESSAGE",
    "CROP_FAILED_PREFIX",
    "CROP_SOURCE_NOT_FOUND_MESSAGE",
    "CROP_TOOL",
    "CROPPED_ORIGIN",
    "PROMPT_NAME",
    "ROLE",
    "TOOL_NAME",
    "BlurryCropError",
    "BoundingBox",
    "CleanCrop",
    "CropError",
    "CropImageRequest",
    "CropRef",
    "CroppedImage",
    "RegionNotFoundError",
    "box_pixels",
    "clean_crop",
    "clean_crop_async",
    "clean_decoded",
    "oriented_image",
    "placeholder_edit",
    "crop_client",
    "crop_edit",
    "crop_image_tool",
    "crop_source_path",
    "crop_source_image",
    "locate_region",
    "region_request",
]
