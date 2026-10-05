"""`GET .../captures` and its thumbnail route (#580)."""

from __future__ import annotations

import cv2
import numpy as np
from fastapi.testclient import TestClient
from read_api_fixtures import ReadVault

from studentassistant.vault import put_source


def _jpeg(width: int, height: int) -> bytes:
    done, encoded = cv2.imencode(".jpg", np.full((height, width, 3), 128, np.uint8))
    assert done
    return encoded.tobytes()


def _capture(rv: ReadVault, name: str, capture_id: str, at: str, **meta: object) -> None:
    put_source(
        rv.vault,
        rv.subject,
        rv.topic,
        "notes",
        name,
        _jpeg(1000, 500),
        {"capture_id": capture_id, "session": rv.ended_session, "captured_at": at, **meta},
    )


def test_lists_the_topics_captures_oldest_first_without_set_aside_ones(
    read_vault: ReadVault, reader: TestClient
) -> None:
    rv = read_vault
    _capture(rv, "b.jpg", "cap-b", "2026-09-25T10:00:00Z")
    _capture(rv, "a.jpg", "cap-a", "2026-09-24T10:00:00Z")
    _capture(rv, "c.jpg", "cap-c", "2026-09-26T10:00:00Z", triage={"status": "set_aside"})

    response = reader.get(f"/api/subjects/{rv.subject}/topics/{rv.topic}/captures")

    assert response.status_code == 200
    body = response.json()
    # The fixture's own `cap-1` page has no `captured_at`, so it sorts first.
    ids = [item["capture_id"] for item in body["captures"]]
    assert ids == ["cap-1", "cap-a", "cap-b"]
    item = body["captures"][1]
    assert item["session_id"] == rv.ended_session
    assert item["captured_at_ms"] == 1_790_244_000_000
    assert item["thumbnail_url"].endswith("/captures/cap-a/thumbnail")


def test_the_thumbnail_is_a_downscaled_jpeg(read_vault: ReadVault, reader: TestClient) -> None:
    rv = read_vault
    _capture(rv, "a.jpg", "cap-a", "2026-09-24T10:00:00Z")

    response = reader.get(f"/api/subjects/{rv.subject}/topics/{rv.topic}/captures/cap-a/thumbnail")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (128, 256)


def test_unknown_topic_or_capture_is_404_in_spanish(
    read_vault: ReadVault, reader: TestClient
) -> None:
    rv = read_vault
    assert reader.get(f"/api/subjects/{rv.subject}/topics/optica/captures").status_code == 404
    missing = reader.get(f"/api/subjects/{rv.subject}/topics/{rv.topic}/captures/nope/thumbnail")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "No existe esa captura en este tema."


def test_an_empty_topic_lists_nothing(read_vault: ReadVault, reader: TestClient) -> None:
    rv = read_vault
    response = reader.get(f"/api/subjects/{rv.subject}/topics/{rv.empty_topic}/captures")
    assert response.status_code == 200 and response.json()["captures"] == []
