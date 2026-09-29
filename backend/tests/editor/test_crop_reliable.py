"""Reliable crops (#520): two-pass localization, the check, the margin, the configurable locator."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any

import cv2
import numpy as np
import pytest

from studentassistant.config import Settings
from studentassistant.editor.crop import (
    CHECK_PROMPT_NAME,
    CHECK_TOOL_NAME,
    TOOL_NAME,
    BoundingBox,
    CropVerdict,
    RegionNotFoundError,
    box_pixels,
    clean_decoded,
    correct_box,
    crop_client,
    crop_source_image,
    locate_crop,
    locator_role,
    map_from_window,
    pad_box,
    retry_client,
    zoom_window,
)
from studentassistant.llm import FakeClaude, load_prompt
from studentassistant.sources.captures import decode_image
from studentassistant.vault import Vault, create_subject, create_topic, put_source, read_source

COARSE = {"x0": 0.25, "y0": 0.25, "x1": 0.75, "y1": 0.75}
LOCAL = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.9}
REGION = "solo el diagrama del centro"
ADDED_AT = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def _verdict(complete: bool = True, **sides: str) -> dict[str, Any]:
    return {
        "complete": complete,
        **{s: sides.get(s, "ok") for s in ("left", "top", "right", "bottom")},
    }


def _diagram() -> np.ndarray:
    image = np.full((600, 800, 3), 255, np.uint8)
    for i in range(12):
        cv2.line(image, (50 + i * 50, 80), (90 + i * 50, 500), (0, 0, 0), 2)
    cv2.circle(image, (400, 300), 120, (30, 30, 30), 3)
    return image


def _jpeg(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def _shown(request: Any) -> np.ndarray:
    """The image a request carried, decoded."""
    data = base64.b64decode(request.messages[0]["content"][0]["source"]["data"])
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    return decoded


def _text(request: Any) -> str:
    return request.messages[0]["content"][1]["text"]


def _box(x0: float, y0: float, x1: float, y1: float) -> BoundingBox:
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _approx(box: BoundingBox, expected: list[float]) -> None:
    assert box.as_list() == pytest.approx(expected, abs=1e-9)


# -- the geometry ----------------------------------------------------------------------------------


def test_margin_pads_every_side_and_is_clamped_to_the_image() -> None:
    _approx(pad_box(_box(0.3, 0.4, 0.5, 0.6), 0.02), [0.28, 0.38, 0.52, 0.62])
    _approx(pad_box(_box(0.01, 0.5, 0.99, 0.6), 0.02), [0.0, 0.48, 1.0, 0.62])
    _approx(pad_box(_box(0.0, 0.0, 1.0, 1.0), 0.1), [0.0, 0.0, 1.0, 1.0])
    assert pad_box(_box(0.3, 0.4, 0.5, 0.6), 0) == _box(0.3, 0.4, 0.5, 0.6)


def test_zoom_window_is_the_coarse_box_plus_margin_in_page_pixels() -> None:
    assert zoom_window(_box(**COARSE), 0.1, 800, 600) == (120, 90, 680, 510)
    # Clamped at the page's edges.
    assert zoom_window(_box(0.02, 0.0, 0.5, 0.97), 0.1, 800, 600) == (0, 0, 480, 600)


def test_a_box_on_the_zoomed_image_maps_back_to_page_fractions() -> None:
    window = (120, 90, 680, 510)  # 560 x 420 pixels of the 800 x 600 page
    # The whole zoomed image is the window itself.
    _approx(map_from_window(_box(0, 0, 1, 1), window, 800, 600), [0.15, 0.15, 0.85, 0.85])
    # x0 = (120 + 0.5 * 560) / 800, y0 = (90 + 0.25 * 420) / 600, ...
    mapped = map_from_window(_box(0.5, 0.25, 0.75, 1.0), window, 800, 600)
    _approx(mapped, [0.5, 0.325, 0.675, 0.85])
    assert box_pixels(mapped, 800, 600) == (400, 195, 540, 510)
    _approx(map_from_window(_box(**LOCAL), window, 800, 600), [0.22, 0.22, 0.78, 0.78])


def test_correction_moves_the_named_sides_by_a_step_of_the_box() -> None:
    box = _box(0.2, 0.2, 0.6, 0.6)  # 0.4 x 0.4: a step of 0.25 is 0.1
    verdict = CropVerdict.model_validate(_verdict(False, left="expand", bottom="shrink"))
    _approx(correct_box(box, verdict, 0.25), [0.1, 0.2, 0.6, 0.5])
    grown = CropVerdict.model_validate(
        _verdict(False, left="expand", top="expand", right="expand", bottom="expand")
    )
    _approx(correct_box(_box(0.05, 0.3, 0.9, 0.98), grown, 0.25), [0.0, 0.13, 1.0, 1.0])
    # A shrink that would leave nothing keeps that axis; a complete verdict moves nothing.
    both = CropVerdict.model_validate(_verdict(False, left="shrink", right="shrink", top="expand"))
    _approx(correct_box(box, both, 0.6), [0.2, 0.0, 0.6, 0.6])
    complete = CropVerdict.model_validate(_verdict(True, left="expand"))
    assert complete.fixes == {} and correct_box(box, complete, 0.25) == box


# -- the locator role ------------------------------------------------------------------------------


def test_the_locator_role_is_configurable_and_the_retry_raises_its_effort() -> None:
    fake = FakeClaude()
    assert locator_role() == "observer" and locator_role(quality="high") == "observer"
    standard = crop_client(transport=fake)
    assert standard.role == "observer" and standard.effort == "medium"
    editor = Settings.model_validate({"editor": {"crop_locator_role": "editor"}})
    client = crop_client(settings=editor, transport=fake)
    assert client.role == "editor" and client.model == editor.llm.roles.editor.model
    # The retry is the same role and model at `[editor] crop_retry_effort`.
    high = Settings.model_validate({"editor": {"crop_retry_effort": "max"}})
    retry = crop_client(settings=high, transport=fake, quality="high")
    assert (retry.role, retry.model, retry.effort) == ("observer", standard.model, "max")
    assert high.llm.roles.observer.effort == "medium"
    # The retry client keeps the given client's transport (the app's own) and ledger.
    retried = retry_client(crop_client(transport=fake))
    assert retried.role == "observer" and retried.effort == "xhigh"
    assert retried.model == standard.model and retried.transport is fake


# -- the two passes and the check ------------------------------------------------------------------


def _locate(fake: FakeClaude, settings: Settings | None = None, **kwargs: Any):  # noqa: ANN202
    decoded = decode_image(_jpeg(_diagram()))
    assert decoded is not None
    return decoded, asyncio.run(
        locate_crop(
            crop_client(transport=fake),
            decoded,
            "image/jpeg",
            REGION,
            settings=settings or Settings(),
            **kwargs,
        )
    )


def test_two_passes_then_a_complete_check_give_the_padded_refined_box() -> None:
    fake = (
        FakeClaude()
        .reply_tool(TOOL_NAME, COARSE)
        .reply_tool(TOOL_NAME, LOCAL)
        .reply_tool(CHECK_TOOL_NAME, _verdict())
    )
    decoded, located = _locate(fake)

    coarse, zoomed, checked = fake.requests
    # The coarse pass sees the whole page; the second, the coarse box plus 10 % of the page from
    # the full-resolution pixels; the check, the final crop.
    assert _shown(coarse).shape[:2] == (600, 800)
    assert _shown(zoomed).shape[:2] == (420, 560)
    assert "enlarged part of the page" in _text(zoomed) and _text(zoomed).endswith(REGION)
    assert [r.tools[0]["name"] for r in fake.requests] == [TOOL_NAME, TOOL_NAME, CHECK_TOOL_NAME]
    assert checked.tools[0]["strict"] is True
    assert checked.prompt_hash == load_prompt(CHECK_PROMPT_NAME).hash
    assert {r.role for r in fake.requests} == {"observer"}
    _approx(located.coarse, [0.25, 0.25, 0.75, 0.75])
    _approx(located.tight, [0.22, 0.22, 0.78, 0.78])
    _approx(located.box, [0.2, 0.2, 0.8, 0.8])  # padded by 2 % of the page
    assert located.check == "complete"
    assert (located.crop.width, located.crop.height) == (480, 360)
    assert _shown(checked).shape[:2] == (360, 480)
    # Deterministic: the same boxes give byte-identical output.
    again = clean_decoded(decoded, "image/jpeg", located.box, min_sharpness=0)
    assert again.data == located.crop.data


def test_one_correction_round_is_applied_and_never_checked_again() -> None:
    fake = (
        FakeClaude()
        .reply_tool(TOOL_NAME, COARSE)
        .reply_tool(TOOL_NAME, LOCAL)
        .reply_tool(CHECK_TOOL_NAME, _verdict(False, right="expand", top="shrink"))
    )
    _, located = _locate(fake)

    assert len(fake.requests) == 3 and fake.pending == 0
    # tight (0.22, 0.22, 0.78, 0.78): the right side grows and the top shrinks by 0.25 * 0.56.
    _approx(located.tight, [0.22, 0.36, 0.92, 0.78])
    _approx(located.box, [0.2, 0.34, 0.94, 0.8])
    assert located.check == "corrected"
    assert (located.crop.width, located.crop.height) == (592, 276)


def test_no_verdict_keeps_the_cut_and_single_pass_settings_ask_once() -> None:
    fake = (
        FakeClaude()
        .reply_tool(TOOL_NAME, COARSE)
        .reply_tool(TOOL_NAME, LOCAL)
        .reply_text("No puedo.", stop_reason="refusal")
    )
    _, located = _locate(fake)
    assert located.check == "unchecked" and (located.crop.width, located.crop.height) == (480, 360)

    single = Settings.model_validate(
        {"editor": {"crop_refine": False, "crop_verify": False, "crop_margin": 0.05}}
    )
    fake = FakeClaude().reply_tool(TOOL_NAME, COARSE)
    _, located = _locate(fake, single)
    assert len(fake.requests) == 1 and located.check == "unchecked"
    _approx(located.box, [0.2, 0.2, 0.8, 0.8])


def test_a_refined_box_never_found_is_the_usual_domain_error() -> None:
    fake = FakeClaude().reply_tool(TOOL_NAME, COARSE)
    fake.reply_text("No puedo.", stop_reason="refusal")
    with pytest.raises(RegionNotFoundError, match="No he podido localizar"):
        _locate(fake)


# -- stored, and redone with the student's feedback ------------------------------------------------


def test_a_retry_passes_the_feedback_and_the_earlier_box_and_is_recorded(
    tmp_vault: Vault,
) -> None:
    subject = create_subject(tmp_vault, "Física").slug
    topic = create_topic(tmp_vault, subject, "Dinámica").slug
    stored = put_source(tmp_vault, subject, topic, "notes", "foto.jpg", _jpeg(_diagram()), {})
    page = stored.relative_to(tmp_vault.path).as_posix()
    single = Settings.model_validate({"editor": {"crop_refine": False, "crop_verify": False}})
    first = asyncio.run(
        crop_source_image(
            tmp_vault,
            subject,
            topic,
            page,
            REGION,
            settings=single,
            client=crop_client(transport=FakeClaude().reply_tool(TOOL_NAME, COARSE)),
            added_at=ADDED_AT,
        )
    )
    fake = (
        FakeClaude()
        .reply_tool(TOOL_NAME, {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.9})
        .reply_tool(TOOL_NAME, LOCAL)
        .reply_tool(CHECK_TOOL_NAME, _verdict())
    )
    retried = asyncio.run(
        crop_source_image(
            tmp_vault,
            subject,
            topic,
            page,
            REGION,
            client=retry_client(crop_client(transport=fake)),
            feedback="le falta   la flecha de la derecha",
            retry_of=first.path,
            added_at=ADDED_AT,
        )
    )

    assert {(r.role, r.effort) for r in fake.requests} == {("observer", "xhigh")}
    coarse, zoomed, checked = fake.requests
    assert "the student said: le falta la flecha de la derecha" in _text(coarse)
    assert "x0=0.2300 y0=0.2300 x1=0.7700 y1=0.7700" in _text(coarse)  # the first crop's box
    assert "le falta la flecha" in _text(zoomed) and "x0=" not in _text(zoomed)
    assert "le falta la flecha" in _text(checked)
    meta = read_source(tmp_vault, retried.path).meta
    assert meta["retry_of"] == first.path
    assert meta["feedback"] == "le falta la flecha de la derecha"
    assert meta["locator"] == {
        "role": "observer",
        "effort": "xhigh",
        "coarse_bbox": [0.1, 0.1, 0.9, 0.9],
        "check": "complete",
    }
