"""Capture processing: sharpest still, downscale, page crop/deskew and storage of a burst."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
import yaml
from capture_images import SHEET_SIZE, desk_still, encode, flat_still, rotated_corners

from studentassistant.config import SourcesSettings
from studentassistant.sources import (
    BurstStill,
    CaptureImageError,
    process_burst,
    store_capture,
    transcript_window,
)
from studentassistant.sources.captures import (
    decode_image,
    downscale,
    find_page,
    pick_sharpest,
    sharpness,
)
from studentassistant.vault import (
    SecretRefused,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    sources_directory,
)

SETTINGS = SourcesSettings()


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología").slug
    return subject, create_topic(tmp_vault, subject, "La célula").slug


def _decoded(data: bytes) -> np.ndarray:
    image = decode_image(data)
    assert image is not None
    return image


# -- sharpness -------------------------------------------------------------------------------------


def test_a_sharp_still_scores_above_its_blurred_copies() -> None:
    sharp, soft, softer = (desk_still(blur=b) for b in (0, 7, 21))
    assert sharpness(sharp) > sharpness(soft) > sharpness(softer)


def test_the_sharpest_still_is_kept_wherever_it_is_in_the_burst() -> None:
    for position in range(3):
        stills = [encode(desk_still(blur=15)) for _ in range(3)]
        stills[position] = encode(desk_still())
        assert process_burst(stills, SETTINGS).selected == position


def test_ties_go_to_the_middle_frame() -> None:
    same = encode(desk_still())
    assert process_burst([same, same, same], SETTINGS).selected == 1
    assert pick_sharpest([5.0, 5.0, 5.0]) == 1
    assert pick_sharpest([5.0, 5.0, 5.0, 5.0, 5.0]) == 2
    assert pick_sharpest([5.0, 2.0, 5.0]) == 0  # equally near the middle: the earlier
    assert pick_sharpest([9.0, 9.0, 1.0]) == 1
    assert pick_sharpest([1.0, 3.0, 9.0]) == 2


def test_undecodable_stills_are_skipped() -> None:
    processed = process_burst([b"not an image", encode(desk_still(blur=9))], SETTINGS)
    assert processed.selected == 1
    assert processed.sharpness[0] is None


def test_a_burst_with_no_decodable_still_is_refused() -> None:
    with pytest.raises(CaptureImageError, match="Ninguna imagen"):
        process_burst([b"junk", b""], SETTINGS)
    with pytest.raises(ValueError):
        pick_sharpest([None])


# -- downscale -------------------------------------------------------------------------------------


def test_the_still_is_downscaled_to_the_long_edge_as_jpeg() -> None:
    big = desk_still(width=4000, height=3000)
    processed = process_burst([encode(big, ".png")], SETTINGS)
    assert (processed.width_px, processed.height_px) == (2400, 1800)
    still = _decoded(processed.still)
    assert still.shape[:2] == (1800, 2400)
    assert processed.still.startswith(b"\xff\xd8")  # re-encoded as JPEG
    page = _decoded(processed.page)
    assert max(page.shape[:2]) <= 2400


def test_a_small_still_is_never_enlarged() -> None:
    image = flat_still(640, 480)
    assert downscale(image, 2400) is image
    processed = process_burst([encode(image)], SETTINGS)
    assert (processed.width_px, processed.height_px) == (640, 480)


def test_the_long_edge_and_quality_are_configurable() -> None:
    settings = SourcesSettings(capture_long_edge=800, capture_jpeg_quality=40)
    low = process_burst([encode(desk_still())], settings)
    assert max(low.width_px, low.height_px) == 800
    high = process_burst([encode(desk_still())], SourcesSettings(capture_long_edge=800))
    assert len(low.still) < len(high.still)


# -- page crop -------------------------------------------------------------------------------------


def test_the_page_is_found_cropped_and_deskewed() -> None:
    still = desk_still()
    corners = find_page(still)
    assert corners is not None
    expected = np.array([[420, 140], [1180, 190], [1230, 1080], [360, 1040]], dtype=np.float32)
    assert np.abs(corners - expected).max() < 12

    assert process_burst([encode(still)], SETTINGS).page_detected


def test_a_skewed_page_is_deskewed_to_its_own_aspect_ratio() -> None:
    still = desk_still(corners=rotated_corners(8))
    processed = process_burst([encode(still)], SETTINGS)
    assert processed.page_detected
    page = _decoded(processed.page)
    height, width = page.shape[:2]
    # Portrait like the sheet, with the sheet's aspect ratio, and no desk left around it.
    assert abs(width / height - SHEET_SIZE[0] / SHEET_SIZE[1]) < 0.03
    assert abs(width - SHEET_SIZE[0]) < 20
    border = np.concatenate([page[5, :], page[-6, :], page[:, 5], page[:, -6]])
    assert border.mean() > 200


def test_no_page_falls_back_to_the_uncropped_still() -> None:
    image = flat_still()
    assert find_page(image) is None
    processed = process_burst([encode(image)], SETTINGS)
    assert not processed.page_detected
    assert _decoded(processed.page).shape == image.shape


# -- transcript window -----------------------------------------------------------------------------


def test_the_transcript_window_is_20s_before_to_10s_after_by_default() -> None:
    assert transcript_window(60_000, SETTINGS) == {"t_start": 40_000, "t_end": 70_000}
    assert transcript_window(5_000, SETTINGS) == {"t_start": 0, "t_end": 15_000}
    narrow = SourcesSettings(capture_window_before_seconds=5, capture_window_after_seconds=2.5)
    assert transcript_window(60_000, narrow) == {"t_start": 55_000, "t_end": 62_500}


# -- storage ---------------------------------------------------------------------------------------


def test_a_burst_is_stored_as_one_page_with_its_page_image_and_originals(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, topic_slug = topic
    blurred = encode(desk_still(blur=15))
    sharp = encode(desk_still())
    also_blurred = encode(desk_still(blur=11), ".png")
    stills = [
        BurstStill(blurred, "image/jpeg"),
        BurstStill(sharp, "image/jpeg"),
        BurstStill(also_blurred, "image/png"),
    ]
    meta = {"capture_id": "c-1", "trigger": "button", "source_context": "notes"}
    stored = store_capture(tmp_vault, subject, topic_slug, "notes", stills, meta, 42_000, SETTINGS)

    directory = sources_directory(tmp_vault, subject, topic_slug, "notes")
    assert stored.path == directory / "page-001.jpg"
    assert stored.page_path == directory / "page-001.page.jpg"
    assert sorted(p.name for p in directory.iterdir()) == [
        "page-001.burst1.jpg",
        "page-001.burst3.png",
        "page-001.jpg",
        "page-001.page.jpg",
        "page-001.yaml",
    ]
    assert (directory / "page-001.burst1.jpg").read_bytes() == blurred
    assert (directory / "page-001.burst3.png").read_bytes() == also_blurred
    assert stored.path.read_bytes() == stored.processed.still
    assert stored.page_path.read_bytes() == stored.processed.page

    sidecar = yaml.safe_load((directory / "page-001.yaml").read_text(encoding="utf-8"))
    assert sidecar["capture_id"] == "c-1"
    assert sidecar["trigger"] == "button"
    assert sidecar["source_context"] == "notes"
    assert sidecar["session_t_ms"] == 42_000
    assert sidecar["transcript_window"] == {"t_start": 22_000, "t_end": 52_000}
    assert sidecar["selected_image"] == 2
    assert sidecar["page_detected"] is True
    assert (sidecar["width_px"], sidecar["height_px"]) == (1600, 1200)
    assert len(sidecar["sharpness"]) == 3
    assert sidecar["sharpness"][1] == max(sidecar["sharpness"])

    # One source: the page image and the burst originals are derived files, not sources.
    [listed] = list_sources(tmp_vault, subject, topic_slug)
    assert listed.path.endswith("sources/notes/page-001.jpg")


def test_a_single_still_burst_stores_no_originals(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, topic_slug = topic
    stored = store_capture(
        tmp_vault,
        subject,
        topic_slug,
        "book",
        [BurstStill(encode(flat_still()), "image/jpeg")],
        {"capture_id": "c-2"},
        0,
        SETTINGS,
    )
    assert sorted(p.name for p in stored.path.parent.iterdir()) == [
        "page-001.jpg",
        "page-001.page.jpg",
        "page-001.yaml",
    ]


def test_an_undecodable_burst_writes_nothing(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, topic_slug = topic
    with pytest.raises(CaptureImageError):
        store_capture(
            tmp_vault,
            subject,
            topic_slug,
            "notes",
            [BurstStill(b"junk", "image/jpeg")],
            {"capture_id": "c-3"},
            0,
            SETTINGS,
        )
    assert not sources_directory(tmp_vault, subject, topic_slug, "notes").exists()


def test_a_secret_in_the_metadata_writes_nothing(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, topic_slug = topic
    with pytest.raises(SecretRefused):
        store_capture(
            tmp_vault,
            subject,
            topic_slug,
            "notes",
            [BurstStill(encode(flat_still()), "image/jpeg")] * 2,
            {"capture_id": "sk-ant-api03-" + "A" * 90},
            0,
            SETTINGS,
        )
    directory = sources_directory(tmp_vault, subject, topic_slug, "notes")
    assert not directory.exists() or not any(directory.iterdir())


def test_contrast_is_enhanced_mildly() -> None:
    low = (flat_still() // 4 + 100).astype(np.uint8)  # a washed-out, low-contrast still
    processed = process_burst([encode(low, ".png")], SETTINGS)
    page = cv2.cvtColor(_decoded(processed.page), cv2.COLOR_BGR2GRAY)
    original = cv2.cvtColor(low, cv2.COLOR_BGR2GRAY)
    assert page.std() >= original.std()
    assert abs(float(page.mean()) - float(original.mean())) < 40
