"""Pasted images: the `images` source kind, `img-NNN.<ext>` with its sidecar."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from yaml import safe_load

from studentassistant.vault import (
    SOURCE_KINDS,
    SourceError,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    put_pasted_image,
    put_source,
    read_source,
    sources_directory,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(40))


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Química").slug
    return subject, create_topic(tmp_vault, subject, "Enlace químico").slug


def test_pasted_images_are_numbered_with_a_sidecar(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    moment = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    first = put_pasted_image(tmp_vault, *topic, PNG, "image/png", added_at=moment)
    second = put_pasted_image(tmp_vault, *topic, JPEG, "image/jpeg")

    directory = sources_directory(tmp_vault, *topic, "images")
    assert "images" in SOURCE_KINDS
    assert (first, second) == (directory / "img-001.png", directory / "img-002.jpg")
    assert first.read_bytes() == PNG
    meta = safe_load((directory / "img-001.yaml").read_text(encoding="utf-8"))
    assert meta == {
        "origin": "pasted",
        "content_type": "image/png",
        "sha256": sha256(PNG).hexdigest(),
        "added_at": "2026-09-26T10:00:00Z",
    }
    listed = [s for s in list_sources(tmp_vault, *topic) if s.kind == "images"]
    assert [s.path.rsplit("/", 1)[-1] for s in listed] == ["img-001.png", "img-002.jpg"]
    read = read_source(tmp_vault, listed[0].path)
    assert read.content == PNG and read.media_type == "image/png"
    assert read.meta is not None and read.meta["origin"] == "pasted"


def test_only_png_jpeg_and_webp_are_kept(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    with pytest.raises(SourceError):
        put_pasted_image(tmp_vault, *topic, b"GIF89a", "image/gif")
    with pytest.raises(SourceError):
        put_pasted_image(tmp_vault, *topic, b"", "image/png")
    with pytest.raises(SourceError):
        put_source(tmp_vault, *topic, "images", "dibujo.gif", b"GIF89a", {})
    assert put_source(tmp_vault, *topic, "images", "foto.JPEG", JPEG, {}).name == "img-001.jpg"
