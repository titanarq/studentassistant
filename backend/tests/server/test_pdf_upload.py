"""`POST /api/subjects/{s}/topics/{t}/sources/pdf`: a PDF or a page range of it, from the web.

Bodies are built by hand (`multipart`), so a test controls every part. Every refusal is checked
to store no source.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.config import ObserverSettings, ServerSettings, Settings, SourcesSettings
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_subject, create_topic, list_sources, read_ledger

LOCAL_BASE_URL = "http://localhost:8765"
LAN_BASE_URL = "http://192.168.1.20:8765"
ROUTE = "/api/subjects/historia/topics/revolucion-industrial/sources/pdf"

Part = tuple[str, str | None, str | None, bytes]
"""`(name, filename, content_type, data)`."""


def multipart(parts: list[Part], boundary: str = "sa-pdf-boundary") -> tuple[bytes, str]:
    body = bytearray()
    for name, filename, content_type, data in parts:
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"'.encode()
        if filename is not None:
            body += f'; filename="{filename}"'.encode()
        body += b"\r\n"
        if content_type is not None:
            body += f"Content-Type: {content_type}\r\n".encode()
        body += b"\r\n" + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def make_pdf(pages: int = 12, blank: tuple[int, ...] = (11,)) -> bytes:
    document = pymupdf.open()
    for number in range(1, pages + 1):
        page = document.new_page()
        if number not in blank:
            page.insert_text((72, 72), f"Página {number}")
    return document.tobytes()


def upload(
    client: TestClient,
    content: bytes,
    pages: str | None = None,
    *,
    filename: str | None = "Tema 4.pdf",
    route: str = ROUTE,
) -> Any:
    parts: list[Part] = [("file", filename, "application/pdf", content)]
    if pages is not None:
        parts.append(("pages", None, None, pages.encode()))
    body, content_type = multipart(parts)
    return client.post(route, content=body, headers={"Content-Type": content_type})


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def vault(tmp_vault: Vault) -> Vault:
    subject = create_subject(tmp_vault, "Historia").slug
    create_topic(tmp_vault, subject, "Revolución industrial")
    return tmp_vault


@pytest.fixture
def make_app(devices_path: Path, codes: PairingCodes, tmp_path: Path, vault: Vault) -> AppFactory:
    def make(**limits: Any) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path, public_url=LAN_BASE_URL),
            codes=codes,
            vault=vault,
            sources=SourcesSettings(**limits),
        )

    return make


@pytest.fixture
def client(make_app: AppFactory) -> TestClient:
    return TestClient(make_app(), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


def pdf_sources(vault: Vault) -> list[Any]:
    return [s for s in list_sources(vault, "historia", "revolucion-industrial") if s.kind == "pdf"]


def test_a_page_range_is_imported(client: TestClient, vault: Vault) -> None:
    changes: list[None] = []
    client.app.state.sessions._note_change = lambda: changes.append(None)  # type: ignore[attr-defined]

    response = upload(client, make_pdf(), "páginas 9 a 11")

    assert response.status_code == 201, response.text
    assert response.json() == {
        "subject_id": "historia",
        "topic_id": "revolucion-industrial",
        "source_id": "sources/pdf/page-001.pdf",
        "vault_id": "subjects/historia/topics/revolucion-industrial/sources/pdf/page-001.pdf",
        "original_name": "Tema 4.pdf",
        "original_page_count": 12,
        "first_page": 9,
        "last_page": 11,
        "page_count": 3,
        "pages_without_text": [11],
    }
    [stored] = pdf_sources(vault)
    assert stored.meta["first_page"] == 9
    assert pymupdf.open(stream=(vault.path / stored.path).read_bytes()).page_count == 3
    assert changes == [None]


def test_without_a_range_every_page_is_kept(client: TestClient, vault: Vault) -> None:
    for pages in (None, "  "):
        response = upload(client, make_pdf(pages=3, blank=()), pages, filename="C:\\x\\a.pdf")
        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["first_page"], body["last_page"], body["pages_without_text"]) == (1, 3, [])
        assert body["original_name"] == "a.pdf"
    assert [Path(s.path).name for s in pdf_sources(vault)] == ["page-001.pdf", "page-002.pdf"]


def test_a_file_without_a_name_is_called_documento(client: TestClient) -> None:
    response = upload(client, make_pdf(pages=1), filename=None)

    assert response.status_code == 201, response.text
    assert response.json()["original_name"] == "documento.pdf"


@pytest.mark.parametrize(
    ("pages", "fragment"),
    [
        ("capítulo 3", "no es un rango de páginas"),
        ("9-4", "no puede ir hacia atrás"),
        ("10-13", "tiene 12 páginas"),
    ],
)
def test_a_bad_range_is_422(client: TestClient, vault: Vault, pages: str, fragment: str) -> None:
    response = upload(client, make_pdf(), pages)

    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert pdf_sources(vault) == []


def test_not_a_pdf_is_422(client: TestClient, vault: Vault) -> None:
    response = upload(client, b"%PDF-no really not", filename="falso.pdf")

    assert response.status_code == 422
    assert response.json()["detail"] == "«falso.pdf» no es un PDF que se pueda leer."
    assert pdf_sources(vault) == []


def test_a_password_protected_pdf_is_422(client: TestClient, vault: Vault) -> None:
    document = pymupdf.open()
    document.new_page()
    locked = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")

    response = upload(client, locked)

    assert response.status_code == 422
    assert "protegido con contraseña" in response.json()["detail"]
    assert pdf_sources(vault) == []


def test_a_file_over_max_pdf_bytes_stops_the_read_with_413(
    make_app: AppFactory, vault: Vault
) -> None:
    content = make_pdf()
    client = TestClient(
        make_app(max_pdf_bytes=len(content) - 1), base_url=LOCAL_BASE_URL, client=("::1", 1)
    )

    response = upload(client, content)

    assert response.status_code == 413
    assert "supera el máximo que se importa" in response.json()["detail"]
    assert pdf_sources(vault) == []


def test_a_declared_length_over_the_cap_is_413_before_reading(
    make_app: AppFactory, vault: Vault
) -> None:
    client = TestClient(make_app(max_pdf_bytes=10), base_url=LOCAL_BASE_URL, client=("::1", 1))
    body, content_type = multipart([("file", "a.pdf", "application/pdf", b"x")])

    response = client.post(
        ROUTE,
        content=body,
        headers={"Content-Type": content_type, "Content-Length": str(10**9)},
    )

    assert response.status_code == 413


def test_too_many_pages_is_413_with_the_import_message(make_app: AppFactory, vault: Vault) -> None:
    client = TestClient(make_app(max_pdf_pages=2), base_url=LOCAL_BASE_URL, client=("::1", 1))

    response = upload(client, make_pdf(), "1-3")

    assert response.status_code == 413
    assert "elige un rango más corto" in response.json()["detail"]
    assert pdf_sources(vault) == []


@pytest.mark.parametrize(
    ("parts", "fragment"),
    [
        ([("pages", None, None, b"1-2")], "Falta la parte «file»"),
        ([("file", "a.pdf", "application/pdf", b"")], "El PDF está vacío."),
        ([("otra", None, None, b"x")], "Sobra la parte «otra»"),
        (
            [("file", "a.pdf", None, b"x"), ("file", "b.pdf", None, b"y")],
            "aparece más de una vez",
        ),
        ([("pages", None, None, b"1" * 300)], "demasiado grande"),
    ],
)
def test_a_malformed_form_is_refused(
    client: TestClient, vault: Vault, parts: list[Part], fragment: str
) -> None:
    body, content_type = multipart(parts)

    response = client.post(ROUTE, content=body, headers={"Content-Type": content_type})

    assert response.status_code in (413, 422)
    assert fragment in response.json()["detail"]
    assert pdf_sources(vault) == []


def test_a_body_that_is_not_multipart_is_422(client: TestClient) -> None:
    response = client.post(ROUTE, content=make_pdf(), headers={"Content-Type": "application/pdf"})

    assert response.status_code == 422
    assert "multipart/form-data" in response.json()["detail"]


def test_an_unknown_topic_is_404(client: TestClient) -> None:
    response = upload(client, make_pdf(), route="/api/subjects/historia/topics/nada/sources/pdf")

    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_a_lan_client_without_a_token_is_refused(make_app: AppFactory, vault: Vault) -> None:
    client = TestClient(make_app(), base_url=LAN_BASE_URL, client=("192.168.1.30", 50000))

    response = upload(client, make_pdf())

    assert response.status_code == 401
    assert pdf_sources(vault) == []


class GatedClaude(FakeClaude):
    """A `FakeClaude` whose requests wait until `gate` is set (on the app's event loop)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def send(self, request: Any, on_text: Any = None) -> Any:
        await self.gate.wait()
        return await super().send(request, on_text)


def test_a_scanned_page_is_transcribed_in_the_background_after_the_response(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, vault: Vault
) -> None:
    fake = GatedClaude()
    fake.reply_text("# Tema 4\n\nPágina escaneada.")
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path, public_url=LAN_BASE_URL),
        codes=codes,
        vault=vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        response = upload(client, make_pdf(pages=3, blank=(2,)))
        assert response.status_code == 201, response.text
        assert response.json()["pages_without_text"] == [2]
        # The response came back while Claude had not answered yet.
        topic = vault.path / "subjects" / "historia" / "topics" / "revolucion-industrial"
        md = topic / "sources" / "pdf" / "page-001.p002.md"
        assert not md.exists()

        async def release_and_wait() -> None:
            fake.gate.set()
            await asyncio.wait_for(app.state.pdf_transcriber.wait_idle(), 10)

        client.portal.call(release_and_wait)  # type: ignore[union-attr]

    assert md.read_text(encoding="utf-8") == "# Tema 4\n\nPágina escaneada.\n"
    assert len(fake.requests) == 1  # only the page without text
    assert [e.role for e in read_ledger(vault, "historia", "revolucion-industrial")] == [
        "transcriber"
    ]


def test_without_an_llm_transport_there_is_no_pdf_transcriber(make_app: AppFactory) -> None:
    assert make_app().state.pdf_transcriber is None
