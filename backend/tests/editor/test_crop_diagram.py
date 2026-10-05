"""A diagram without a clean border is cut whole, never warped to an inner shape (#588)."""

from __future__ import annotations

import asyncio

import cv2
import numpy as np

from studentassistant.config import Settings
from studentassistant.editor.crop import (
    CHECK_TOOL_NAME,
    TOOL_NAME,
    BoundingBox,
    clean_decoded,
    crop_client,
    locate_crop,
)
from studentassistant.llm import FakeClaude
from studentassistant.sources.captures import decode_image, find_page

WIDTH, HEIGHT = 1000, 800
# Each element of the diagram, as (left, top, right, bottom) pixels on the page.
ELEMENTS = {
    "inner_box": (280, 230, 720, 570),
    "top_label": (120, 110, 330, 180),
    "left_circle": (70, 330, 230, 490),
    "right_arrow": (770, 380, 920, 420),
    "bottom_caption": (350, 610, 650, 660),
}


def _noisy_page() -> np.ndarray:
    """A grey, noisy page: a light box in the middle and loose elements all around it, with no
    border enclosing the whole figure."""
    rng = np.random.default_rng(588)
    page = np.full((HEIGHT, WIDTH, 3), 190, np.int16)
    page += rng.integers(-12, 13, size=(HEIGHT, WIDTH, 1), dtype=np.int16)
    page = np.clip(page, 0, 255).astype(np.uint8)
    left, top, right, bottom = ELEMENTS["inner_box"]
    page[top:bottom, left:right] = 255  # the light inner box
    cv2.rectangle(page, (left, top), (right, bottom), (20, 20, 20), 3)
    for name, (x0, y0, x1, y1) in ELEMENTS.items():
        if name == "inner_box":
            continue
        cv2.rectangle(page, (x0, y0), (x1, y1), (20, 20, 20), -1)
    return page


def _inside(box: BoundingBox, element: tuple[int, int, int, int]) -> bool:
    x0, y0, x1, y1 = element
    return (
        box.x0 <= x0 / WIDTH
        and box.y0 <= y0 / HEIGHT
        and box.x1 >= x1 / WIDTH
        and box.y1 >= y1 / HEIGHT
    )


def test_find_page_picks_the_inner_box_so_the_cut_must_not_be_warped_to_it() -> None:
    page = _noisy_page()
    assert find_page(page[80:680, 40:970]) is not None  # the trap: an inner quadrilateral exists


def test_the_stored_crop_keeps_every_element_of_the_diagram() -> None:
    page = _noisy_page()
    ok, encoded = cv2.imencode(".png", page)
    assert ok
    decoded = decode_image(encoded.tobytes())
    assert decoded is not None
    whole = BoundingBox(x0=0.04, y0=0.1, x1=0.97, y1=0.85)
    cleaned = clean_decoded(decoded, "image/png", whole, min_sharpness=0, located=whole)
    assert not cleaned.deskewed
    assert (cleaned.width, cleaned.height) == (930, 600)


def test_locate_crop_pipeline_keeps_the_whole_figure() -> None:
    page = _noisy_page()
    ok, encoded = cv2.imencode(".png", page)
    assert ok
    decoded = decode_image(encoded.tobytes())
    assert decoded is not None
    box = {"x0": 0.05, "y0": 0.14, "x1": 0.94, "y1": 0.85}
    fake = (
        FakeClaude()
        .reply_tool(TOOL_NAME, box)
        .reply_tool(TOOL_NAME, {"x0": 0.03, "y0": 0.03, "x1": 0.97, "y1": 0.97})
        .reply_tool(
            CHECK_TOOL_NAME,
            {"complete": True, "left": "ok", "top": "ok", "right": "ok", "bottom": "ok"},
        )
    )
    located = asyncio.run(
        locate_crop(
            crop_client(transport=fake),
            decoded,
            "image/png",
            "el esquema",
            settings=Settings(),
        )
    )
    assert not located.crop.deskewed
    for element in ELEMENTS.values():
        assert _inside(located.box, element)
