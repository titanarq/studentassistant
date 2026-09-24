"""Small PDFs built in memory with PyMuPDF, so no test ships a binary fixture."""

from __future__ import annotations

import pymupdf


def make_pdf(pages: int, *, blank: tuple[int, ...] = ()) -> bytes:
    """A PDF of `pages` A4 pages; page `n` reads «Página n del libro», except those in `blank`."""
    document = pymupdf.open()
    try:
        for number in range(1, pages + 1):
            page = document.new_page()
            if number not in blank:
                page.insert_text((72, 72), f"Página {number} del libro", fontsize=14)
        return document.tobytes()
    finally:
        document.close()


def encrypted_pdf() -> bytes:
    document = pymupdf.open()
    try:
        document.new_page().insert_text((72, 72), "secreto")
        return document.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="dueño", user_pw="clave"
        )
    finally:
        document.close()
