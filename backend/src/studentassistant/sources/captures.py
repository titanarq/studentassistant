"""Capture processing: one burst of stills becomes one stored page of the topic.

`process_burst` decodes every still of a burst (JPEG, PNG or WebP, EXIF orientation applied),
scores each by the variance of its Laplacian (computed on a grayscale copy scaled to a common long
edge, so stills of different sizes compare fairly) and keeps the sharpest; when several share the
best score the one nearest the middle of the burst wins (a burst's first and last frames are the
likeliest to be shaken by the tap). The kept still is downscaled to `[sources]
capture_long_edge` (2400 px) and re-encoded as JPEG at `capture_jpeg_quality` (85). The page
image is derived from it: the largest convex quadrilateral found in the still (the sheet of
paper) is warped flat by a perspective transform and given a mild contrast boost (CLAHE on
lightness); when no page is found the whole still gets the contrast boost instead.

`store_capture` stores the result through `vault.put_source`, all in one call:

    sources/<kind>/page-NNN.jpg          the kept still, downscaled
    sources/<kind>/page-NNN.page.jpg     the cropped, deskewed page (or the uncropped fallback)
    sources/<kind>/page-NNN.burst<K>.<ext>  each other still of the burst as it came, `K` its
                                         1-based position in the burst (a retention-purge target)
    sources/<kind>/page-NNN.yaml         the sidecar

The sidecar carries what the caller gives (capture id, trigger, source context, session...) plus
the capture's session time `session_t_ms` and its `transcript_window` (`t_start`/`t_end`, session
milliseconds, from `capture_window_before_seconds` before to `capture_window_after_seconds` after
the capture, never below 0), which is the span of `transcript.jsonl` a page transcription reads as
spoken hints, and the processing record: `selected_image` (the kept still's `K`), `sharpness`
(per still, `null` for one that could not be decoded), `page_detected`, and the stored still's
`width_px`/`height_px`.

Everything here is CPU-bound (OpenCV): a server caller runs it in a worker thread.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from studentassistant.config import SourcesSettings
from studentassistant.vault import Vault, put_source

STILL_NAME = "capture.jpg"
PAGE_SUFFIX = "page.jpg"
BURST_SUFFIX = "burst{position}{extension}"

# Sharpness is compared on grayscale copies of this long edge, whatever each still's size.
_ANALYSIS_LONG_EDGE = 1200
# The page is searched for on a copy of this long edge (faster, and less paper texture).
_DETECTION_LONG_EDGE = 800
# A quadrilateral counts as the page only if it covers at least this share of the image...
_MIN_PAGE_AREA = 0.2
# ...and at most this share (above it, it is the image border, not a sheet on a desk).
_MAX_PAGE_AREA = 0.98
_APPROX_EPSILON = 0.02
# A corner this close to the image border (share of the long edge) counts as on it.
_BORDER_MARGIN = 0.01
# The sheet must be at least this much lighter (0-255 grey levels) than what surrounds it.
_MIN_PAGE_CONTRAST = 25.0
_CLAHE_CLIP_LIMIT = 1.5
_CLAHE_TILES = (8, 8)
_SHARPNESS_REL_TOLERANCE = 1e-6

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class CaptureImageError(Exception):
    """No still of the burst is an image OpenCV can decode; the message is Spanish."""


@dataclass(frozen=True)
class BurstStill:
    """One still of a burst as it was uploaded."""

    data: bytes
    content_type: str


@dataclass(frozen=True)
class ProcessedBurst:
    """What `process_burst` makes of a burst.

    `selected` is the 0-based index of the kept still, `sharpness` the score of each still
    (`None` when it could not be decoded), `still` and `page` JPEG bytes, `width_px`/`height_px`
    the size of `still`.
    """

    selected: int
    sharpness: tuple[float | None, ...]
    still: bytes
    page: bytes
    page_detected: bool
    width_px: int
    height_px: int


@dataclass(frozen=True)
class StoredCapture:
    """What `store_capture` wrote: the still's path, the page image's path and the processing."""

    path: Path
    page_path: Path
    processed: ProcessedBurst


def decode_image(data: bytes) -> np.ndarray | None:
    """A BGR image from encoded bytes, EXIF orientation applied; `None` when it is not one."""
    if not data:
        return None
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return None
    return image


def downscale(image: np.ndarray, long_edge: int) -> np.ndarray:
    """`image` shrunk (never enlarged) so its longer side is at most `long_edge` pixels."""
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= long_edge:
        return image
    scale = long_edge / longest
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def sharpness(image: np.ndarray) -> float:
    """The variance of the Laplacian of `image` in grayscale at a common scale (higher: sharper)."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = downscale(gray, _ANALYSIS_LONG_EDGE)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def pick_sharpest(scores: Sequence[float | None]) -> int:
    """The index of the best score; among ties, the one nearest the middle (`len // 2`), the
    earlier one when two are equally near. `None` scores never win.

    Raises:
        ValueError: when every score is `None` (or there is none).
    """
    valid = [(index, score) for index, score in enumerate(scores) if score is not None]
    if not valid:
        raise ValueError("no still of the burst has a sharpness score")
    best = max(score for _, score in valid)
    tied = [
        index
        for index, score in valid
        if math.isclose(score, best, rel_tol=_SHARPNESS_REL_TOLERANCE, abs_tol=1e-9)
    ]
    middle = len(scores) // 2
    return min(tied, key=lambda index: (abs(index - middle), index))


def find_page(image: np.ndarray) -> np.ndarray | None:
    """The four corners (float32, `image` pixels, ordered top-left, top-right, bottom-right,
    bottom-left) of the largest convex quadrilateral in `image`, or `None` when there is none
    covering a plausible share of it."""
    small = downscale(image, _DETECTION_LONG_EDGE)
    scale = image.shape[1] / small.shape[1]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    area = float(gray.shape[0] * gray.shape[1])
    edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8))
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    best: np.ndarray | None = None
    best_area = 0.0
    for mask in (edges, otsu):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            perimeter = cv2.arcLength(contour, True)
            polygon = cv2.approxPolyDP(contour, _APPROX_EPSILON * perimeter, True)
            if len(polygon) != 4 or not cv2.isContourConvex(polygon):
                continue
            polygon_area = float(cv2.contourArea(polygon))
            share = polygon_area / area
            if (
                _MIN_PAGE_AREA <= share <= _MAX_PAGE_AREA
                and polygon_area > best_area
                and _plausible_page(gray, polygon)
            ):
                best, best_area = polygon, polygon_area
    if best is None:
        return None
    return _order_corners(best.reshape(4, 2).astype(np.float32) * scale)


def _plausible_page(gray: np.ndarray, polygon: np.ndarray) -> bool:
    """Whether `polygon` looks like a sheet: at most one corner on the image border (a region cut
    by the frame is not a whole page) and clearly lighter inside than around it."""
    height, width = gray.shape
    margin = max(2, round(_BORDER_MARGIN * max(height, width)))
    points = polygon.reshape(4, 2)
    on_border = sum(
        1
        for x, y in points
        if x < margin or y < margin or x >= width - margin or y >= height - margin
    )
    if on_border > 1:
        return False
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(mask, points.astype(np.int32), 255)
    inside = gray[mask > 0]
    outside = gray[mask == 0]
    if inside.size == 0 or outside.size == 0:
        return False
    return float(inside.mean()) - float(outside.mean()) >= _MIN_PAGE_CONTRAST


def _order_corners(points: np.ndarray) -> np.ndarray:
    """`points` as top-left, top-right, bottom-right, bottom-left."""
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).ravel()  # y - x
    return np.array(
        [
            points[np.argmin(sums)],
            points[np.argmin(diffs)],
            points[np.argmax(sums)],
            points[np.argmax(diffs)],
        ],
        dtype=np.float32,
    )


def crop_page(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """The quadrilateral `corners` (as `find_page` orders them) of `image`, warped flat."""
    top_left, top_right, bottom_right, bottom_left = corners
    width = round(
        max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left))
    )
    height = round(
        max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right))
    )
    width, height = max(1, width), max(1, height)
    target = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(corners, target)
    return cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_CUBIC)


def enhance_contrast(image: np.ndarray) -> np.ndarray:
    """A mild local contrast boost (CLAHE on the lightness channel); colours are kept."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=_CLAHE_CLIP_LIMIT, tileGridSize=_CLAHE_TILES)
    return cv2.cvtColor(cv2.merge((clahe.apply(lightness), a, b)), cv2.COLOR_LAB2BGR)


def encode_jpeg(image: np.ndarray, quality: int) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:  # pragma: no cover - OpenCV encodes any 8-bit BGR image
        raise RuntimeError("OpenCV could not encode the image as JPEG")
    return encoded.tobytes()


def process_burst(stills: Sequence[bytes], settings: SourcesSettings) -> ProcessedBurst:
    """Pick the sharpest still of the burst, downscale it and derive its page image.

    Stills that cannot be decoded are skipped (scored `None`).

    Raises:
        CaptureImageError: when no still can be decoded (or there is none).
    """
    images = [decode_image(data) for data in stills]
    scores = tuple(None if image is None else sharpness(image) for image in images)
    try:
        selected = pick_sharpest(scores)
    except ValueError as error:
        raise CaptureImageError(
            "Ninguna imagen de la captura se puede leer como JPEG, PNG o WebP."
        ) from error
    kept = images[selected]
    assert kept is not None
    still = downscale(kept, settings.capture_long_edge)
    corners = find_page(still)
    page = enhance_contrast(still if corners is None else crop_page(still, corners))
    page = downscale(page, settings.capture_long_edge)
    return ProcessedBurst(
        selected=selected,
        sharpness=scores,
        still=encode_jpeg(still, settings.capture_jpeg_quality),
        page=encode_jpeg(page, settings.capture_jpeg_quality),
        page_detected=corners is not None,
        width_px=int(still.shape[1]),
        height_px=int(still.shape[0]),
    )


def transcript_window(session_t_ms: int, settings: SourcesSettings) -> dict[str, int]:
    """The span of session time (ms) whose speech belongs with a capture taken at `session_t_ms`."""
    before = round(settings.capture_window_before_seconds * 1000)
    after = round(settings.capture_window_after_seconds * 1000)
    return {"t_start": max(0, session_t_ms - before), "t_end": max(0, session_t_ms + after)}


def store_capture(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    kind: str,
    stills: Sequence[BurstStill],
    meta: Mapping[str, Any],
    session_t_ms: int,
    settings: SourcesSettings,
) -> StoredCapture:
    """Process a burst and store it as one page of the topic's `kind` sources (see the module
    docstring for the files and the sidecar). `meta` is what the caller records about the capture;
    the processing record and the transcript window are added to it.

    Raises:
        CaptureImageError: when no still of the burst can be decoded; nothing is written.
        Whatever `vault.put_source` raises (a refused secret, an unknown topic...); nothing is
            written.
    """
    processed = process_burst([still.data for still in stills], settings)
    derived: dict[str, bytes] = {PAGE_SUFFIX: processed.page}
    for index, still in enumerate(stills):
        if index != processed.selected:
            extension = _EXTENSIONS.get(still.content_type, ".bin")
            derived[BURST_SUFFIX.format(position=index + 1, extension=extension)] = still.data
    sidecar: dict[str, Any] = dict(meta)
    sidecar |= {
        "width_px": processed.width_px,
        "height_px": processed.height_px,
        "session_t_ms": max(0, session_t_ms),
        "transcript_window": transcript_window(max(0, session_t_ms), settings),
        "selected_image": processed.selected + 1,
        "sharpness": [None if s is None else round(s, 3) for s in processed.sharpness],
        "page_detected": processed.page_detected,
    }
    path = put_source(
        vault, subject_slug, topic_slug, kind, STILL_NAME, processed.still, sidecar, derived
    )
    stem = path.name.split(".", 1)[0]
    return StoredCapture(
        path=path, page_path=path.with_name(f"{stem}.{PAGE_SUFFIX}"), processed=processed
    )


__all__ = [
    "BurstStill",
    "CaptureImageError",
    "ProcessedBurst",
    "StoredCapture",
    "crop_page",
    "decode_image",
    "downscale",
    "enhance_contrast",
    "find_page",
    "pick_sharpest",
    "process_burst",
    "sharpness",
    "store_capture",
    "transcript_window",
]
