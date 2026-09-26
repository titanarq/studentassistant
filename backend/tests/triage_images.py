"""Synthetic notebook photos for the capture triage tests, all drawn with NumPy and OpenCV.

A laptop camera at 1280x720 sees a sheet filling the frame (no page is detected, as in the real
sessions): paper texture, a soft shadow, and handwriting-like strokes whose layout depends on the
seed, so two seeds are two different pages and one seed re-shot is the same page.
"""

from __future__ import annotations

import cv2
import numpy as np

FRAME = (1280, 720)  # w x h


def paper(seed: int = 1, shadow: bool = True, size: tuple[int, int] = FRAME) -> np.ndarray:
    """An empty sheet: textured, with a shadow darkening one corner."""
    width, height = size
    rng = np.random.default_rng(seed)
    sheet = rng.normal(232, 7, (height, width)).astype(np.float32)
    sheet = cv2.GaussianBlur(sheet, (3, 3), 0)
    if shadow:
        x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
        y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
        sheet -= 70 * np.clip(1 - (x + y), 0, 1)
    gray = np.clip(sheet, 0, 255).astype(np.uint8)
    return cv2.merge((gray, gray, gray))


def written(
    seed: int = 3,
    lines: int | None = None,
    size: tuple[int, int] = (1400, 800),
    margin: int = 150,
) -> np.ndarray:
    """A sheet with handwriting-like lines (and, for some seeds, a boxed scheme)."""
    rng = np.random.default_rng(seed)
    width, height = size
    page = paper(seed + 100, shadow=False, size=size)
    count = lines if lines is not None else int(rng.integers(5, 10))
    y = margin + int(rng.integers(0, 120))
    for _ in range(count):
        x = margin + int(rng.integers(0, 300))
        end = min(width - margin, x + int(rng.integers(300, width - 2 * margin)))
        points = []
        while x < end:
            points.append((x, y + int(rng.normal(0, 6))))
            x += int(rng.integers(8, 18))
        cv2.polylines(page, [np.array(points, np.int32)], False, (40, 40, 70), 3, cv2.LINE_AA)
        y += int(rng.integers(60, 120))
        if y > height - margin:
            break
    if rng.random() < 0.5:
        x0, y0 = int(rng.integers(margin, width // 2)), int(rng.integers(margin, height // 2))
        cv2.rectangle(page, (x0, y0), (x0 + 300, y0 + 180), (50, 50, 80), 3)
    return page


def framed(
    page: np.ndarray,
    shift: tuple[int, int] = (0, 0),
    scale: float = 1.0,
    exposure: float = 1.0,
    blur: int = 0,
) -> np.ndarray:
    """`page` as the camera frames it: a 1280x720 window of the (scaled) sheet, moved by `shift`,
    brightened or darkened by `exposure` and blurred by `blur` (odd kernel, 0 for none)."""
    width, height = FRAME
    scaled = cv2.resize(page, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cy = (scaled.shape[0] - height) // 2 + shift[1]
    cx = (scaled.shape[1] - width) // 2 + shift[0]
    frame = scaled[cy : cy + height, cx : cx + width].astype(np.float32) * exposure
    frame = np.clip(frame, 0, 255).astype(np.uint8)
    if blur:
        frame = cv2.GaussianBlur(frame, (blur, blur), 0)
    return frame


def cut_by_frame(seed: int = 3) -> np.ndarray:
    """A written sheet whose lines run off the right side of the frame."""
    page = written(seed, lines=8, size=(1600, 1000), margin=40)
    return page[140:860, 520:1800] if page.shape[1] >= 1800 else page[140:860, 320:1600]


def encode(image: np.ndarray) -> bytes:
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return data.tobytes()
