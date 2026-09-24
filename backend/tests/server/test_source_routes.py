"""`GET /api/sources/{vault_id}` and `/meta`: bytes and sidecars only through `read_source`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from read_api_fixtures import JPEG_BYTES, WEB_MARKDOWN, ReadVault


def test_an_image_is_served_with_its_media_type(read_vault: ReadVault, reader: TestClient) -> None:
    response = reader.get(f"/api/sources/{read_vault.notes_page}")

    assert response.status_code == 200
    assert response.content == JPEG_BYTES
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_markdown_is_served_as_markdown_text_never_html(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(f"/api/sources/{read_vault.web_page}")

    assert response.status_code == 200
    assert response.text == WEB_MARKDOWN
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_a_sidecar_read_directly_is_never_rendered(
    read_vault: ReadVault, reader: TestClient
) -> None:
    path = read_vault.notes_page.replace(".jpg", ".yaml")
    response = reader.get(f"/api/sources/{path}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert b"cap-1" in response.content


def test_meta_returns_the_sidecar_and_its_transcription(
    read_vault: ReadVault, reader: TestClient
) -> None:
    response = reader.get(f"/api/sources/{read_vault.notes_page}/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["vault_id"] == read_vault.notes_page
    assert body["kind"] == "notes"
    assert body["media_type"] == "image/jpeg"
    assert body["size"] == len(JPEG_BYTES)
    assert body["meta"]["capture_id"] == "cap-1"
    assert body["transcription"] == "v = dx/dt"


def test_meta_without_a_transcription_says_null(read_vault: ReadVault, reader: TestClient) -> None:
    body = reader.get(f"/api/sources/{read_vault.web_page}/meta").json()
    assert body["kind"] == "web"
    assert body["meta"]["url"] == "https://example.org/mru"
    assert body["transcription"] is None


def test_a_missing_source_is_404_in_spanish(read_vault: ReadVault, reader: TestClient) -> None:
    path = read_vault.notes_page.replace("page-001", "page-099")
    for suffix in ("", "/meta"):
        response = reader.get(f"/api/sources/{path}{suffix}")
        assert response.status_code == 404
        assert response.json()["detail"] == "No existe esa fuente en la bóveda."


def refused(reader: TestClient, path: str) -> None:
    for suffix in ("", "/meta"):
        response = reader.get(f"/api/sources/{path}{suffix}")
        assert response.status_code in (400, 404), (path, response.status_code)
        assert b"student" not in response.content  # nothing of vault.yaml
        assert b"root:" not in response.content  # nothing of /etc/passwd


@pytest.mark.parametrize(
    "path",
    [
        "vault.yaml",
        "subjects/fisica/subject.yaml",
        "subjects/fisica/topics/cinematica/topic.yaml",
        "subjects/fisica/topics/cinematica/sources/notes/%2e%2e/%2e%2e/topic.yaml",
        "subjects/fisica/topics/cinematica/sources/notes/%2E%2E/%2E%2E/%2E%2E/%2E%2E/%2E%2E/vault.yaml",
        "subjects/fisica/topics/cinematica/sources/notes/..%2f..%2ftopic.yaml",
        "%2fetc%2fpasswd",
        "subjects/fisica/topics/cinematica/sources/notes/%2fetc%2fpasswd",
        "subjects/fisica/topics/cinematica/sources/secrets/page-001.jpg",
        "subjects/fisica/topics/cinematica/sessions/x/session.yaml",
        "subjects/fisica/topics/cinematica/sources/notes/.%2fpage-001.jpg",
        "subjects/fisica/topics/cinematica/sources/notes%5cpage-001.jpg",
        "subjects/fisica/topics/cinematica/sources/notes/page-001.jpg%00.md",
    ],
)
def test_a_path_outside_a_topics_sources_is_refused(
    read_vault: ReadVault, reader: TestClient, path: str
) -> None:
    refused(reader, path)


def test_literal_dot_segments_never_reach_outside(
    read_vault: ReadVault, reader: TestClient
) -> None:
    # httpx may normalise `..` away before sending; either way nothing outside a source is served.
    refused(reader, "subjects/fisica/topics/cinematica/sources/notes/../../topic.yaml")
    refused(reader, "subjects/fisica/topics/cinematica/sources/notes/../../../../../../vault.yaml")


def test_a_symlink_out_of_the_vault_is_refused(read_vault: ReadVault, reader: TestClient) -> None:
    outside = read_vault.vault.path.parent / "outside.jpg"
    outside.write_bytes(b"secret bytes outside the vault")
    notes_dir = (read_vault.vault.path / read_vault.notes_page).parent
    (notes_dir / "page-050.jpg").symlink_to(outside)

    for suffix in ("", "/meta"):
        response = reader.get(
            f"/api/sources/subjects/fisica/topics/cinematica/sources/notes/page-050.jpg{suffix}"
        )
        assert response.status_code in (400, 404)
        assert b"secret bytes" not in response.content


def test_a_symlinked_sources_directory_is_refused(
    read_vault: ReadVault, reader: TestClient
) -> None:
    outside = read_vault.vault.path.parent / "elsewhere"
    outside.mkdir()
    (outside / "page-001.jpg").write_bytes(b"secret bytes outside the vault")
    sources = (read_vault.vault.path / read_vault.notes_page).parent.parent
    (sources / "book").symlink_to(outside, target_is_directory=True)

    response = reader.get(
        "/api/sources/subjects/fisica/topics/cinematica/sources/book/page-001.jpg"
    )
    assert response.status_code in (400, 404)
    assert b"secret bytes" not in response.content
