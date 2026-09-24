"""The built web app at `/`: static files, SPA fallback, and the JSON hint when it is not built.

No test depends on a real `npm run build`: the built case writes an `index.html` and one asset
under `tmp_path` and hands that directory to `create_app(static_dir=...)`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studentassistant import __version__
from studentassistant.server.app import STATIC_DIR, create_app

INDEX_HTML = "<!doctype html><title>Student Assistant</title><div id='root'></div>"
APP_JS = "console.log('hola');\n"


@pytest.fixture
def built_dir(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (static / "assets" / "app.js").write_text(APP_JS, encoding="utf-8")
    return static


@pytest.fixture
def built(built_dir: Path) -> TestClient:
    return TestClient(create_app(static_dir=built_dir))


def test_default_static_dir_is_the_package_static_directory() -> None:
    import studentassistant.server.app as app_module

    assert Path(app_module.__file__).parent / "static" == STATIC_DIR


def test_hint_when_not_built(tmp_path: Path) -> None:
    client = TestClient(create_app(static_dir=tmp_path / "missing"))

    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["hint"], str) and body["hint"]
    assert "npm run build" in body["hint"]

    assert client.get("/api/health").json() == {"status": "ok", "version": __version__}


def test_root_serves_index_html(built: TestClient) -> None:
    response = built.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == INDEX_HTML


def test_assets_are_served(built: TestClient) -> None:
    response = built.get("/assets/app.js")
    assert response.status_code == 200
    assert response.content == APP_JS.encode()


def test_unknown_path_falls_back_to_index_html(built: TestClient) -> None:
    response = built.get("/mesa/alguna-ruta")
    assert response.status_code == 200
    assert response.text == INDEX_HTML


@pytest.mark.parametrize("path", ["/api/does-not-exist", "/api", "/ws", "/ws/session"])
def test_backend_prefixes_never_get_index_html(built: TestClient, path: str) -> None:
    response = built.get(path)
    assert response.status_code == 404
    assert INDEX_HTML not in response.text


def test_path_outside_the_static_dir_is_not_served(built: TestClient, built_dir: Path) -> None:
    (built_dir.parent / "secret.txt").write_text("secreto", encoding="utf-8")

    response = built.get("/%2e%2e/secret.txt")
    assert "secreto" not in response.text


def test_health_still_answers_when_built(built: TestClient) -> None:
    assert built.get("/api/health").json() == {"status": "ok", "version": __version__}
