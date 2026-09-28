"""`editor.crop.clean_crop`: cut to the box, sharpness filter, deskew or plain crop, encode."""

from __future__ import annotations

import asyncio

import cv2
import numpy as np
import pytest

from studentassistant.config import DEFAULT_TRIAGE_MIN_SHARPNESS, Settings
from studentassistant.editor.crop import (
    BLURRY_CROP_MESSAGE,
    BlurryCropError,
    BoundingBox,
    CropError,
    box_pixels,
    clean_crop,
    clean_crop_async,
)

MIN_SHARPNESS = DEFAULT_TRIAGE_MIN_SHARPNESS
CARD = (200, 150, 600, 450)  # left, top, right, bottom of the light card in `_card_page`
CARD_BOX = BoundingBox(x0=0.18, y0=0.18, x1=0.82, y1=0.82)


def _diagram() -> np.ndarray:
    """A line drawing on white paper: no rectangular border for `find_page` to take."""
    image = np.full((600, 800, 3), 255, np.uint8)
    for i in range(12):
        cv2.line(image, (50 + i * 50, 80), (90 + i * 50, 500), (0, 0, 0), 2)
    cv2.circle(image, (400, 300), 120, (30, 30, 30), 3)
    cv2.putText(image, "F = m a", (120, 560), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    return image


def _card_page(angle: float) -> np.ndarray:
    """A light ruled card on a dark noisy background, rotated `angle` degrees about the centre."""
    rng = np.random.default_rng(7)
    image = rng.integers(20, 90, (600, 800, 3), dtype=np.uint8)
    left, top, right, bottom = CARD
    image[top:bottom, left:right] = 230
    for y in range(top + 40, bottom - 20, 30):
        cv2.line(image, (left + 30, y), (right - 30, y), (0, 0, 0), 2)
    if angle:
        matrix = cv2.getRotationMatrix2D((400, 300), angle, 1.0)
        image = cv2.warpAffine(image, matrix, (800, 600), borderMode=cv2.BORDER_REFLECT)
    return image


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _jpeg(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def _decode(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def test_default_threshold_matches_capture_triage() -> None:
    assert Settings().editor.crop_min_sharpness == DEFAULT_TRIAGE_MIN_SHARPNESS
    with pytest.raises(ValueError):
        Settings.model_validate({"editor": {"crop_min_sharpness": -1}})


def test_box_pixels_rounds_outward_and_clamps() -> None:
    box = BoundingBox(x0=0.1, y0=0.25, x1=0.55, y1=0.75)
    assert box_pixels(box, 800, 600) == (80, 150, 440, 450)
    assert box_pixels(BoundingBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0), 801, 599) == (0, 0, 801, 599)
    # A sliver thinner than a pixel still gives one pixel, inside the image.
    thin = BoundingBox(x0=0.9999, y0=0.9999, x1=1.0, y1=1.0)
    assert box_pixels(thin, 800, 600) == (799, 599, 800, 600)


def test_plain_crop_is_exactly_the_box_when_no_quadrilateral() -> None:
    image = _diagram()
    box = BoundingBox(x0=0.05, y0=0.1, x1=0.9, y1=0.95)
    crop = clean_crop(_png(image), "image/png", box, min_sharpness=MIN_SHARPNESS)
    assert not crop.deskewed
    assert (crop.content_type, crop.extension) == ("image/png", "png")
    left, top, right, bottom = box_pixels(box, 800, 600)
    assert (crop.width, crop.height) == (right - left, bottom - top)
    np.testing.assert_array_equal(_decode(crop.data), image[top:bottom, left:right])


def test_sharp_crop_is_accepted_and_blurred_one_refused() -> None:
    sharp = _diagram()
    blurred = cv2.GaussianBlur(sharp, (0, 0), 8)
    box = BoundingBox(x0=0.05, y0=0.1, x1=0.9, y1=0.95)
    crop = clean_crop(_jpeg(sharp), "image/jpeg", box, min_sharpness=MIN_SHARPNESS)
    assert crop.sharpness >= MIN_SHARPNESS
    with pytest.raises(BlurryCropError) as refused:
        clean_crop(_jpeg(blurred), "image/jpeg", box, min_sharpness=MIN_SHARPNESS)
    assert str(refused.value) == BLURRY_CROP_MESSAGE
    assert isinstance(refused.value, CropError)


def test_threshold_is_the_setting_passed() -> None:
    image = _png(_diagram())
    box = BoundingBox(x0=0.05, y0=0.1, x1=0.9, y1=0.95)
    score = clean_crop(image, "image/png", box, min_sharpness=0).sharpness
    clean_crop(image, "image/png", box, min_sharpness=score)
    with pytest.raises(BlurryCropError):
        clean_crop(image, "image/png", box, min_sharpness=score * 1.01)


def test_rotated_card_is_warped_flat() -> None:
    crop = clean_crop(_png(_card_page(5)), "image/png", CARD_BOX, min_sharpness=MIN_SHARPNESS)
    assert crop.deskewed
    # The card's own size, not the box's (512 x 384): the dark background is cut away.
    assert abs(crop.width - 400) <= 6 and abs(crop.height - 300) <= 6
    out = _decode(crop.data)
    for y, x in (
        (4, 4),
        (4, crop.width - 5),
        (crop.height - 5, 4),
        (crop.height - 5, crop.width - 5),
    ):
        assert out[y - 3 : y + 3, x - 3 : x + 3].mean() > 150, (y, x)
    # The ruled lines are horizontal again: in each, the darkest row is the same across the width.
    gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
    for line_y in range(40, 280, 30):
        band = gray[line_y - 12 : line_y + 12]
        rows = [int(np.argmin(band[:, x])) for x in range(60, crop.width - 60, 20)]
        assert max(rows) - min(rows) <= 2, (line_y, rows)


def test_axis_aligned_card_matches_the_plain_crop_of_the_card() -> None:
    image = _card_page(0)
    crop = clean_crop(_png(image), "image/png", CARD_BOX, min_sharpness=MIN_SHARPNESS)
    left, top, right, bottom = CARD
    plain = image[top:bottom, left:right]
    out = _decode(crop.data)
    assert abs(out.shape[0] - plain.shape[0]) <= 2 and abs(out.shape[1] - plain.shape[1]) <= 2
    resized = cv2.resize(out, (plain.shape[1], plain.shape[0]), interpolation=cv2.INTER_AREA)
    assert float(np.abs(resized.astype(int) - plain.astype(int)).mean()) < 10


def test_output_is_byte_identical_for_the_same_input() -> None:
    for data, media_type in ((_jpeg(_card_page(5)), "image/jpeg"), (_png(_diagram()), "image/png")):
        first = clean_crop(data, media_type, CARD_BOX, min_sharpness=MIN_SHARPNESS)
        again = clean_crop(data, media_type, CARD_BOX, min_sharpness=MIN_SHARPNESS)
        threaded = asyncio.run(
            clean_crop_async(data, media_type, CARD_BOX, min_sharpness=MIN_SHARPNESS)
        )
        assert first.data == again.data == threaded.data
        assert first == threaded


def test_non_png_source_gives_a_jpeg() -> None:
    crop = clean_crop(_jpeg(_diagram()), "image/webp", CARD_BOX, min_sharpness=MIN_SHARPNESS)
    assert (crop.content_type, crop.extension) == ("image/jpeg", "jpg")
    assert crop.data[:3] == b"\xff\xd8\xff"


def test_undecodable_bytes_are_refused() -> None:
    with pytest.raises(CropError) as refused:
        clean_crop(b"not an image", "image/jpeg", CARD_BOX, min_sharpness=MIN_SHARPNESS)
    assert not isinstance(refused.value, BlurryCropError)


def test_async_runs_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    original = asyncio.to_thread

    async def spy(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(func)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    asyncio.run(
        clean_crop_async(_png(_diagram()), "image/png", CARD_BOX, min_sharpness=MIN_SHARPNESS)
    )
    assert calls == [clean_crop]
