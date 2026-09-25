"""Synthetic capture stills: a white sheet with typed lines on a darker desk, sharp or blurred.

Everything is drawn here, never a photo, so the fixtures hold nothing personal.
"""

from __future__ import annotations

import cv2
import numpy as np

# The sheet's corners in a 1600x1200 (w x h) still: a tilted, perspective-distorted A-ish page.
SHEET_CORNERS = np.array([[420, 140], [1180, 190], [1230, 1080], [360, 1040]], dtype=np.float32)
SHEET_SIZE = (700, 900)  # the flat sheet drawn before warping, w x h


def rotated_corners(degrees: float, center: tuple[float, float] = (800, 600)) -> np.ndarray:
    """The corners of the flat sheet rotated by `degrees` about `center`: a skewed but
    undistorted page, whose true aspect ratio a deskew must recover."""
    w, h = SHEET_SIZE
    corners = np.array([[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]])
    angle = np.deg2rad(degrees)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    return (corners @ rotation.T + np.array(center)).astype(np.float32)


def sheet(width: int = SHEET_SIZE[0], height: int = SHEET_SIZE[1]) -> np.ndarray:
    """A flat white sheet with dark typed lines."""
    page = np.full((height, width, 3), 245, dtype=np.uint8)
    for row, y in enumerate(range(80, height - 60, 55)):
        cv2.putText(
            page,
            f"Linea {row}: la celula es la unidad",
            (40, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (20, 20, 20),
            2,
            cv2.LINE_AA,
        )
    return page


def desk_still(
    width: int = 1600,
    height: int = 1200,
    corners: np.ndarray = SHEET_CORNERS,
    blur: int = 0,
    seed: int = 7,
) -> np.ndarray:
    """A still of the sheet lying on a mid-grey desk, optionally Gaussian-blurred (`blur` odd)."""
    rng = np.random.default_rng(seed)
    desk = rng.normal(90, 6, (height, width, 3)).clip(0, 255).astype(np.uint8)
    page = sheet()
    source = np.array(
        [[0, 0], [page.shape[1], 0], [page.shape[1], page.shape[0]], [0, page.shape[0]]],
        dtype=np.float32,
    )
    scaled = corners * np.float32([width / 1600, height / 1200])
    matrix = cv2.getPerspectiveTransform(source, scaled)
    warped = cv2.warpPerspective(page, matrix, (width, height))
    mask = cv2.warpPerspective(np.full(page.shape[:2], 255, np.uint8), matrix, (width, height))
    still = desk.copy()
    still[mask > 0] = warped[mask > 0]
    if blur:
        still = cv2.GaussianBlur(still, (blur, blur), 0)
    return still


def flat_still(width: int = 800, height: int = 600) -> np.ndarray:
    """A still with no page in it: a smooth diagonal gradient."""
    x = np.linspace(40, 200, width, dtype=np.float32)
    y = np.linspace(0, 40, height, dtype=np.float32)[:, None]
    gray = (x[None, :] + y).clip(0, 255).astype(np.uint8)
    return cv2.merge((gray, gray, gray))


def encode(image: np.ndarray, extension: str = ".jpg") -> bytes:
    ok, data = cv2.imencode(extension, image)
    assert ok
    return data.tobytes()
