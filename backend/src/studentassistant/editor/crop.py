"""Crop one region of a stored page image: Claude locates the region (`locate_region`).

The student asks for only part of a page ("solo el diagrama de la página 3"); Claude (the role
`[editor] crop_locator_role`, `observer` by default -- no model id here) receives the page
as an image content block and the description, and answers through the strict tool `crop_region`
(`llm.structured`) with a `BoundingBox`: four fractions of the image's width/height in `[0, 1]`,
`x0 < x1`, `y0 < y1`. A box out of these bounds fails validation and is re-asked by `structured`
itself, never used raw. The prompt (`prompts/editor_crop.md`) asks for a tight box that leaves out
blank margins, the desk and fingers holding the page.

Reliability (#520, `locate_crop`): that first box is only coarse. The coarse box plus
`[editor] crop_zoom_margin` is cut from the full-resolution decoded page (`zoom_window`) and the
box asked again on that zoomed image; the refined box is mapped back to page fractions by
deterministic code (`map_from_window`). The cut (padded by `[editor] crop_margin`, `pad_box`) is
then shown with the request through the strict tool `check_crop` (`CropVerdict`, prompt
`editor_crop_check`): complete, or which sides to expand or shrink; one correction round
(`correct_box`, `[editor] crop_correction_step`) is applied, never more. A crop redone because the
student said the previous one was wrong uses the same locator role and model at the higher
effort `[editor] crop_retry_effort` (`xhigh` by default), with the student's words as extra
guidance (`crop_source_image(feedback=...)`).

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
# The default locator role; `[editor] crop_locator_role` chooses it (`locator_role`).
ROLE = "observer"
TOOL_NAME = "crop_region"
TOOL_DESCRIPTION = (
    "Give the tight bounding box of the requested region, as fractions of the image's width "
    "and height."
)
MAX_TOKENS = 300
CHECK_PROMPT_NAME = "editor_crop_check"
CHECK_TOOL_NAME = "check_crop"
CHECK_TOOL_DESCRIPTION = (
    "Say whether the crop shows the whole requested region and only it, or which sides to move."
)
CropQuality = Literal["standard", "high"]

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


SideFix = Literal["ok", "expand", "shrink"]


class CropVerdict(BaseModel):
    """The `check_crop` answer: the crop is complete, or how each of its sides should move."""

    model_config = ConfigDict(extra="forbid")

    complete: bool = Field(
        description="True when the crop shows all of the requested region and nothing else of"
        " note; false when a side cuts part of it off or takes in too much."
    )
    left: SideFix = Field(
        description="`expand` when the region is cut off at the left edge, `shrink` when there"
        " is too much unrelated content on the left, else `ok`."
    )
    top: SideFix = Field(description="The same for the top edge.")
    right: SideFix = Field(description="The same for the right edge.")
    bottom: SideFix = Field(description="The same for the bottom edge.")

    @property
    def fixes(self) -> dict[str, SideFix]:
        """The sides to move (none when `complete`)."""
        if self.complete:
            return {}
        sides = {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}
        return {side: fix for side, fix in sides.items() if fix != "ok"}


def locator_role(settings: Settings | None = None, quality: CropQuality = "standard") -> str:
    """The role that locates a crop: `[editor] crop_locator_role`, for a crop redone after the
    student's complaint too (`quality="high"` only raises the effort, `crop_client`)."""
    return (settings or Settings()).editor.crop_locator_role


def crop_client(
    *,
    settings: Settings | None = None,
    transport: Any | None = None,
    ledger: LedgerBinding | None = None,
    quality: CropQuality = "standard",
) -> LLMClient:
    """The client that locates regions: role `locator_role(settings)` (`observer` by default,
    from `[llm.roles.<role>]`); with `quality="high"` (a crop redone after the student's complaint)
    the same role at `[editor] crop_retry_effort`."""
    settings = settings or Settings()
    if quality == "high":
        role = locator_role(settings)
        settings = settings.model_copy(deep=True)
        getattr(settings.llm.roles, role).effort = settings.editor.crop_retry_effort
    return get_client(locator_role(settings), settings=settings, transport=transport, ledger=ledger)


def retry_client(client: LLMClient | None, *, settings: Settings | None = None) -> LLMClient:
    """The locator of a crop redone after the student's complaint: the locator role at
    `[editor] crop_retry_effort`, over `client`'s transport and ledger (the app's own), or the
    default ones without `client`."""
    return crop_client(
        settings=settings,
        transport=None if client is None else client.transport,
        ledger=None if client is None else client.ledger,
        quality="high",
    )


def _image_block(image: bytes, media_type: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image).decode("ascii"),
        },
    }


def _guidance(feedback: str | None, previous: BoundingBox | None) -> str:
    """The student's complaint about the previous crop, as extra guidance (empty without one)."""
    text = ""
    if feedback is not None and feedback.strip():
        text += "\n\nThe previous crop of this request was wrong; the student said: " + " ".join(
            feedback.split()
        )
    if previous is not None:
        text += (
            "\nThat crop covered the box x0={:.4f} y0={:.4f} x1={:.4f} y1={:.4f} of the whole"
            " page: do not repeat its mistake."
        ).format(*previous.as_list())
    return text


def region_request(
    image: bytes,
    media_type: str,
    description: str,
    *,
    zoomed: bool = False,
    feedback: str | None = None,
    previous: BoundingBox | None = None,
) -> list[dict[str, Any]]:
    """The user message: the page (or, `zoomed`, the part of it around a first box) as an image
    content block, then the requested region and any guidance from the student's complaint."""
    lead = (
        "This image is an enlarged part of the page around the requested region; give the tight"
        " box of the region on THIS image. The region, in the student's words: "
        if zoomed
        else "Locate this region of the page, in the student's words: "
    )
    return [
        {
            "role": "user",
            "content": [
                _image_block(image, media_type),
                {
                    "type": "text",
                    "text": lead
                    + description.strip()
                    + _guidance(feedback, None if zoomed else previous),
                },
            ],
        }
    ]


def check_request(
    image: bytes, media_type: str, description: str, *, feedback: str | None = None
) -> list[dict[str, Any]]:
    """The verification message: the crop as an image content block, then the request."""
    return [
        {
            "role": "user",
            "content": [
                _image_block(image, media_type),
                {
                    "type": "text",
                    "text": "This is the crop made for the student's request: "
                    + description.strip()
                    + _guidance(feedback, None),
                },
            ],
        }
    ]


async def locate_region(
    client: LLMClient,
    image: bytes,
    media_type: str,
    description: str,
    *,
    zoomed: bool = False,
    feedback: str | None = None,
    previous: BoundingBox | None = None,
) -> BoundingBox:
    """The tight box Claude gives for `description` on `image` (`zoomed`: an enlarged part of
    the page around a first box). `feedback`/`previous` are the student's complaint about an
    earlier crop and that crop's box, passed as guidance.

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
            region_request(
                image, media_type, description, zoomed=zoomed, feedback=feedback, previous=previous
            ),
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


async def check_crop(
    client: LLMClient,
    image: bytes,
    media_type: str,
    description: str,
    *,
    feedback: str | None = None,
) -> CropVerdict | None:
    """Claude's verdict on a cut (`check_crop`), or `None` when it gave none (a refusal, no
    valid verdict after the re-ask): the cut is then kept as it is.

    Raises:
        LLMError: any other failure of the call (a reached cap, exhausted retries).
    """
    prompt = load_prompt(CHECK_PROMPT_NAME)
    try:
        result = await structured(
            client,
            check_request(image, media_type, description, feedback=feedback),
            CropVerdict,
            tool_name=CHECK_TOOL_NAME,
            tool_description=CHECK_TOOL_DESCRIPTION,
            system=prompt.content,
            max_tokens=MAX_TOKENS,
            prompt_hash=prompt.hash,
        )
    except (RefusalError, StructuredOutputError):
        return None
    return result.value


# -- the deterministic box geometry (#520) ---------------------------------------------------------


def _box(x0: float, y0: float, x1: float, y1: float) -> BoundingBox:
    """A box from edges already ordered, clamped to `[0, 1]`."""
    return BoundingBox(
        x0=min(max(x0, 0.0), 1.0),
        y0=min(max(y0, 0.0), 1.0),
        x1=min(max(x1, 0.0), 1.0),
        y1=min(max(y1, 0.0), 1.0),
    )


def pad_box(box: BoundingBox, margin: float) -> BoundingBox:
    """`box` grown by `margin` (a fraction of the image's width/height) on every side, clamped
    to the image."""
    return _box(box.x0 - margin, box.y0 - margin, box.x1 + margin, box.y1 + margin)


def zoom_window(
    box: BoundingBox, margin: float, width: int, height: int
) -> tuple[int, int, int, int]:
    """The pixels of a `width` x `height` page cut for the refining pass: `box` padded by
    `margin` (`pad_box`), as `box_pixels` maps it: `(left, top, right, bottom)`."""
    return box_pixels(pad_box(box, margin), width, height)


def map_from_window(
    local: BoundingBox, window: tuple[int, int, int, int], width: int, height: int
) -> BoundingBox:
    """A box given on the zoomed image `window` (`zoom_window`) as fractions of the whole
    `width` x `height` page: each edge is the window's offset plus the fraction of its size,
    divided by the page's size. Pure arithmetic, no rounding beyond the floats'."""
    left, top, right, bottom = window
    span_x, span_y = right - left, bottom - top
    return _box(
        (left + local.x0 * span_x) / width,
        (top + local.y0 * span_y) / height,
        (left + local.x1 * span_x) / width,
        (top + local.y1 * span_y) / height,
    )


def correct_box(box: BoundingBox, verdict: CropVerdict, step: float) -> BoundingBox:
    """`box` with each side the verdict names moved by `step` of the box's width (left, right)
    or height (top, bottom): outwards to `expand`, inwards to `shrink`, clamped to the image. A
    shrink that would leave no box keeps both sides of that axis as they were."""
    fixes = verdict.fixes
    dx, dy = (box.x1 - box.x0) * step, (box.y1 - box.y0) * step

    def moved(side: str, value: float, delta: float, outwards: float) -> float:
        fix = fixes.get(side)
        if fix == "expand":
            return value + outwards * delta
        if fix == "shrink":
            return value - outwards * delta
        return value

    x0, x1 = moved("left", box.x0, dx, -1), moved("right", box.x1, dx, 1)
    y0, y1 = moved("top", box.y0, dy, -1), moved("bottom", box.y1, dy, 1)
    if not min(max(x0, 0.0), 1.0) < min(max(x1, 0.0), 1.0):
        x0, x1 = box.x0, box.x1
    if not min(max(y0, 0.0), 1.0) < min(max(y1, 0.0), 1.0):
        y0, y1 = box.y0, box.y1
    return _box(x0, y0, x1, y1)


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
class LocatedCrop:
    """What `locate_crop` settled on: the final box (padded), its cleaned-up crop and how."""

    box: BoundingBox
    crop: CleanCrop
    # The first box on the whole page, and the box before the margin (refined, corrected).
    coarse: BoundingBox
    tight: BoundingBox
    # `complete`, `corrected` (one correction round applied) or `unchecked` (no verdict).
    check: Literal["complete", "corrected", "unchecked"]


async def locate_crop(
    client: LLMClient,
    decoded: np.ndarray,
    media_type: str,
    description: str,
    *,
    settings: Settings,
    feedback: str | None = None,
    previous: BoundingBox | None = None,
) -> LocatedCrop:
    """Locate `description` on the decoded page and cut it out (#520).

    (1) A coarse box on the whole page (`locate_region`). (2) With `[editor] crop_refine`, the
    coarse box plus `crop_zoom_margin` is cut from the full-resolution page (`zoom_window`), the
    box asked again on it (`zoomed=True`) and mapped back (`map_from_window`). (3) The box padded
    by `crop_margin` (`pad_box`) is cut and cleaned up (`clean_decoded`). (4) With `crop_verify`,
    that crop is shown with the request (`check_crop`); sides it says to move are moved once
    (`correct_box`, `crop_correction_step`) and the crop is cut again -- one round, never
    checked again. Everything but the Claude calls is deterministic.

    Raises:
        CropError, RegionNotFoundError: as `locate_region`, for either box.
        BlurryCropError: a cut is too blurry to keep.
        LLMError: any other failure of a Claude call.
    """
    editor = settings.editor
    quality = settings.sources.capture_jpeg_quality
    height, width = decoded.shape[:2]
    shown, shown_type = await asyncio.to_thread(oriented_image, decoded, media_type, quality)
    coarse = await locate_region(
        client, shown, shown_type, description, feedback=feedback, previous=previous
    )
    tight = coarse
    if editor.crop_refine:
        left, top, right, bottom = zoom_window(coarse, editor.crop_zoom_margin, width, height)
        zoomed, zoomed_type = await asyncio.to_thread(
            oriented_image, decoded[top:bottom, left:right], media_type, quality
        )
        local = await locate_region(
            client, zoomed, zoomed_type, description, zoomed=True, feedback=feedback
        )
        tight = map_from_window(local, (left, top, right, bottom), width, height)

    def cut(box: BoundingBox) -> CleanCrop:
        return clean_decoded(
            decoded,
            media_type,
            box,
            min_sharpness=editor.crop_min_sharpness,
            jpeg_quality=quality,
        )

    box = pad_box(tight, editor.crop_margin)
    crop = await asyncio.to_thread(cut, box)
    if not editor.crop_verify:
        return LocatedCrop(box=box, crop=crop, coarse=coarse, tight=tight, check="unchecked")
    verdict = await check_crop(client, crop.data, crop.content_type, description, feedback=feedback)
    if verdict is None:
        return LocatedCrop(box=box, crop=crop, coarse=coarse, tight=tight, check="unchecked")
    if not verdict.fixes:
        return LocatedCrop(box=box, crop=crop, coarse=coarse, tight=tight, check="complete")
    tight = correct_box(tight, verdict, editor.crop_correction_step)
    corrected = pad_box(tight, editor.crop_margin)
    if corrected != box:
        box, crop = corrected, await asyncio.to_thread(cut, corrected)
    return LocatedCrop(box=box, crop=crop, coarse=coarse, tight=tight, check="corrected")


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
    feedback: str | None = None,
    retry_of: str | None = None,
) -> CroppedImage:
    """Crop `description` out of the stored page image at `vault_relative_path` and store it.

    The page is read with `vault.read_source` and never modified; the region is located, cut and
    cleaned up by `locate_crop` (`client`, else `crop_client(settings=settings)`;
    `[editor] crop_*`, `[sources] capture_jpeg_quality`) and stored as a new `images` source of
    the topic with a `cropped` sidecar. Nothing is committed.

    A crop redone because the student said an earlier one was wrong (#520) passes `retry_of`
    (that crop's vault-relative path) and `feedback` (the student's words): both guide the
    locator, the earlier crop's box too when it was cut from this same page, and without
    `client` the locator is `crop_client(quality="high")` (`[editor] crop_retry_effort`).

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
    # Decoded once, before any Claude call: a file OpenCV cannot read (a GIF) is refused here,
    # and the box is located on the same oriented pixels it is applied to.
    decoded = await asyncio.to_thread(decode_image, source.content)
    if decoded is None:
        raise CropError(UNSUPPORTED_IMAGE_MESSAGE)
    retried = retry_of is not None
    client = client or crop_client(settings=settings, quality="high" if retried else "standard")
    previous = (
        await asyncio.to_thread(_previous_box, vault, retry_of, vault_relative_path)
        if retry_of is not None
        else None
    )
    located = await locate_crop(
        client,
        decoded,
        source.media_type,
        description,
        settings=settings,
        feedback=feedback,
        previous=previous,
    )
    box, crop = located.box, located.crop
    meta: dict[str, Any] = {
        "origin": CROPPED_ORIGIN,
        "cropped_from": vault_relative_path,
        "bbox": box.as_list(),
        "requested_region": description.strip(),
        "content_type": crop.content_type,
        "sha256": hashlib.sha256(crop.data).hexdigest(),
        "added_at": added_at or datetime.now(UTC),
        "locator": {
            "role": client.role,
            "effort": client.effort,
            "coarse_bbox": located.coarse.as_list(),
            "check": located.check,
        },
    }
    if retry_of is not None:
        meta["retry_of"] = retry_of
        if feedback is not None and feedback.strip():
            meta["feedback"] = " ".join(feedback.split())
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


def _previous_box(vault: Vault, retry_of: str, page: str) -> BoundingBox | None:
    """The box of the earlier crop at `retry_of` when it was cut from `page`; blocking, `None`
    when it cannot be read or was cut from another page."""
    try:
        meta = read_source(vault, retry_of).meta
        if meta.get("cropped_from") != page:
            return None
        x0, y0, x1, y1 = meta["bbox"]
        return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)
    except Exception:
        return None


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
    retry_of: str | None = Field(
        default=None,
        description="Only to redo a crop the student just said is wrong («el recorte ha salido"
        " mal», «le falta un trozo», «no es esa zona»): the source_id of that crop"
        " (`sources/images/img-NNN.<ext>`, the image the previous turn cropped). The crop is"
        " then redone with more care and the wrong one is replaced and retired. Otherwise null.",
    )
    feedback: str | None = Field(
        default=None,
        description="With `retry_of`: what the student said is wrong with the previous crop, in"
        " their words, to guide the new one. Otherwise null.",
    )


class CropRef(BaseModel):
    """What a turn's crop was: the new source (`source_id`, `None` when it failed) and the page.

    A diagram the turn drew (`draw_diagram`, `editor.diagram`, #511) is recorded the same way, as
    `kind: diagram` with an empty `source` and its title as `region`: it is stored, committed,
    retired and undone exactly like a crop.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    region: str
    kind: Literal["crop", "diagram"] = "crop"
    source_id: str | None = None
    path: str | None = None
    error: str | None = None
    # The stored crop's identity, as its sidecar records it (#502): an undo retires the file at
    # `path` only while it is still this one, never a later crop that reused the number.
    sha256: str | None = None
    added_at: datetime | None = None
    # A crop redone after the student's complaint (#520): the source_id of the crop it replaced,
    # retired in the same commit.
    retry_of: str | None = None


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
    "CHECK_PROMPT_NAME",
    "CHECK_TOOL_NAME",
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
    "CropQuality",
    "CropRef",
    "CropVerdict",
    "LocatedCrop",
    "CroppedImage",
    "RegionNotFoundError",
    "box_pixels",
    "check_crop",
    "check_request",
    "correct_box",
    "locate_crop",
    "locator_role",
    "map_from_window",
    "pad_box",
    "retry_client",
    "zoom_window",
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
