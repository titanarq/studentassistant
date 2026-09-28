"""`editor.crop.crop_source_image`: read the page, locate, clean up, store with provenance."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import cv2
import numpy as np
import pytest

from studentassistant.config import Settings
from studentassistant.editor.crop import (
    BLURRY_CROP_MESSAGE,
    TOOL_NAME,
    BlurryCropError,
    BoundingBox,
    CropError,
    CroppedImage,
    crop_client,
    crop_source_image,
)
from studentassistant.editor.notes_format import (
    IMAGE_CROP_TEXT,
    IMAGE_TEXT,
    FootnoteDefinition,
    cropped_image_provenance,
    image_provenance,
    parse_provenance,
)
from studentassistant.llm import FakeClaude
from studentassistant.vault import (
    SourceNotFoundError,
    SourcePathError,
    Vault,
    create_subject,
    create_topic,
    put_pasted_image,
    put_source,
    read_source,
    sources_directory,
)

BOX = {"x0": 0.25, "y0": 0.25, "x1": 0.75, "y1": 0.75}
ADDED_AT = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
REGION = "solo el diagrama del centro"


def _diagram() -> np.ndarray:
    """A line drawing on white paper: no rectangular border, so the crop is the plain box."""
    image = np.full((600, 800, 3), 255, np.uint8)
    for i in range(12):
        cv2.line(image, (50 + i * 50, 80), (90 + i * 50, 500), (0, 0, 0), 2)
    cv2.circle(image, (400, 300), 120, (30, 30, 30), 3)
    return image


def _jpeg(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def _topic(vault: Vault) -> tuple[str, str]:
    subject = create_subject(vault, "Física").slug
    return subject, create_topic(vault, subject, "Dinámica").slug


def _page(vault: Vault, subject: str, topic: str, content: bytes) -> str:
    path = put_source(vault, subject, topic, "notes", "foto.jpg", content, {"page": 1})
    return path.relative_to(vault.path).as_posix()


def _crop(
    vault: Vault, subject: str, topic: str, page: str, fake: FakeClaude, region: str = REGION
) -> CroppedImage:
    return asyncio.run(
        crop_source_image(
            vault,
            subject,
            topic,
            page,
            region,
            client=crop_client(transport=fake),
            added_at=ADDED_AT,
        )
    )


def _images(vault: Vault, subject: str, topic: str) -> list[str]:
    directory = sources_directory(vault, subject, topic, "images")
    return sorted(entry.name for entry in directory.iterdir()) if directory.is_dir() else []


def test_crop_is_stored_as_a_new_images_source_with_its_provenance(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    original = _jpeg(_diagram())
    page = _page(tmp_vault, subject, topic, original)
    page_before = read_source(tmp_vault, page)
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)

    result = _crop(tmp_vault, subject, topic, page, fake)

    assert len(fake.requests) == 1
    assert result.source_id == "sources/images/img-001.jpg"
    assert result.path == f"subjects/{subject}/topics/{topic}/sources/images/img-001.jpg"
    assert result.number == 1
    assert result.box == BoundingBox(**BOX)
    stored = read_source(tmp_vault, result.path)
    assert stored.content == result.crop.data
    assert stored.content != original
    assert stored.media_type == "image/jpeg"
    decoded = cv2.imdecode(np.frombuffer(stored.content, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[:2] == (300, 400)  # the box, in pixels of the 800x600 page
    assert stored.meta is not None
    assert stored.meta["origin"] == "cropped"
    assert stored.meta["cropped_from"] == page
    assert stored.meta["bbox"] == [0.25, 0.25, 0.75, 0.75]
    assert stored.meta["requested_region"] == REGION
    assert stored.meta["content_type"] == "image/jpeg"
    assert stored.meta["sha256"] == hashlib.sha256(stored.content).hexdigest()
    assert str(stored.meta["added_at"]).startswith("2026-09-28")
    # The page it came from is untouched, content and sidecar.
    assert read_source(tmp_vault, page) == page_before


def test_footnote_and_image_link_cite_the_crop_distinctly(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    page = _page(tmp_vault, subject, topic, _jpeg(_diagram()))
    result = _crop(tmp_vault, subject, topic, page, FakeClaude().reply_tool(TOOL_NAME, BOX))

    assert result.footnote_label == "img001"
    assert result.footnote == "[^img001]: [Imagen recortada 1](../sources/images/img-001.jpg)"
    assert result.markdown == "![Imagen recortada 1](../sources/images/img-001.jpg)"
    assert result.provenance == cropped_image_provenance(1, "jpg")
    label, _, text = result.footnote.partition(": ")
    parsed = parse_provenance(FootnoteDefinition(label=label[2:-1], text=text))
    assert parsed.kind == "images"
    assert parsed.source_id == result.source_id
    assert parsed.path == result.source_id


def test_cropped_provenance_differs_from_a_pasted_one_only_in_its_text() -> None:
    cropped = cropped_image_provenance(7, ".JPG")
    pasted = image_provenance(7, "jpg")
    assert IMAGE_CROP_TEXT == "Imagen recortada" != IMAGE_TEXT
    assert cropped.text == "Imagen recortada 7"
    assert cropped.source_id == pasted.source_id == "sources/images/img-007.jpg"
    assert cropped.kind == pasted.kind == "images"
    assert cropped.definition("img007") == (
        "[^img007]: [Imagen recortada 7](../sources/images/img-007.jpg)"
    )


def test_crop_numbers_follow_existing_images(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    put_pasted_image(tmp_vault, subject, topic, b"\x89PNG not decoded", "image/png")
    page = _page(tmp_vault, subject, topic, _jpeg(_diagram()))
    result = _crop(tmp_vault, subject, topic, page, FakeClaude().reply_tool(TOOL_NAME, BOX))
    assert result.source_id == "sources/images/img-002.jpg"
    assert result.footnote.startswith("[^img002]: [Imagen recortada 2]")


def test_png_page_gives_a_png_crop(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    ok, encoded = cv2.imencode(".png", _diagram())
    assert ok
    path = put_source(tmp_vault, subject, topic, "book", "p.png", encoded.tobytes(), {})
    page = path.relative_to(tmp_vault.path).as_posix()
    result = _crop(tmp_vault, subject, topic, page, FakeClaude().reply_tool(TOOL_NAME, BOX))
    assert result.source_id == "sources/images/img-001.png"
    assert read_source(tmp_vault, result.path).meta["content_type"] == "image/png"


def test_blurry_crop_stores_nothing(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    blurred = cv2.GaussianBlur(_diagram(), (0, 0), 8)
    page = _page(tmp_vault, subject, topic, _jpeg(blurred))
    with pytest.raises(BlurryCropError) as raised:
        _crop(tmp_vault, subject, topic, page, FakeClaude().reply_tool(TOOL_NAME, BOX))
    assert str(raised.value) == BLURRY_CROP_MESSAGE
    assert _images(tmp_vault, subject, topic) == []


def test_threshold_comes_from_editor_settings(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    page = _page(tmp_vault, subject, topic, _jpeg(_diagram()))
    settings = Settings.model_validate({"editor": {"crop_min_sharpness": 1e12}})
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    with pytest.raises(BlurryCropError):
        asyncio.run(
            crop_source_image(
                tmp_vault,
                subject,
                topic,
                page,
                REGION,
                settings=settings,
                client=crop_client(transport=fake),
            )
        )
    assert _images(tmp_vault, subject, topic) == []


@pytest.mark.parametrize(
    ("page", "error"),
    [
        ("subjects/fisica/topics/dinamica/sources/notes/page-009.jpg", SourceNotFoundError),
        ("subjects/fisica/topics/dinamica/sources/../notes/page-001.jpg", SourcePathError),
        ("/etc/passwd", SourcePathError),
    ],
)
def test_unresolvable_source_bubbles_and_asks_nothing(
    tmp_vault: Vault, page: str, error: type[Exception]
) -> None:
    subject, topic = _topic(tmp_vault)
    _page(tmp_vault, subject, topic, _jpeg(_diagram()))
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    with pytest.raises(error):
        _crop(tmp_vault, subject, topic, page, fake)
    assert fake.requests == []
    assert _images(tmp_vault, subject, topic) == []


def test_non_image_source_is_refused_before_asking(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    path = put_source(tmp_vault, subject, topic, "web", "Una página", "# Hola\n", {})
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    with pytest.raises(CropError, match="no es una imagen"):
        _crop(tmp_vault, subject, topic, path.relative_to(tmp_vault.path).as_posix(), fake)
    assert fake.requests == []
    assert _images(tmp_vault, subject, topic) == []


def test_empty_region_is_refused_before_asking(tmp_vault: Vault) -> None:
    subject, topic = _topic(tmp_vault)
    page = _page(tmp_vault, subject, topic, _jpeg(_diagram()))
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    with pytest.raises(CropError):
        _crop(tmp_vault, subject, topic, page, fake, region="   ")
    assert fake.requests == []
