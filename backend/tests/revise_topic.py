"""A fixture topic for the revision tests: "Derivadas" with notes and a textbook page.

On top of `make_topic`, a textbook page `sources/book/page-001.jpg` (with its transcription) is
stored and `valid_notes` is written as `notes/apuntes.md` and committed. `BOOK_FOOTNOTE` is the
definition the catalogue gives for that page.
"""

from __future__ import annotations

from dataclasses import dataclass

from generate_topic import GenerateTopic, make_topic, valid_notes
from studentassistant.vault import GitSync, Vault, put_source, sources_directory, write_notes

BOOK_TRANSCRIPTION = (
    "La derivada de f en a es el límite, cuando h tiende a 0, de (f(a+h) - f(a)) / h.\n"
)
BOOK_FOOTNOTE = "[Libro, página 1](../sources/book/page-001.jpg)"
IA_FOOTNOTE = "Ampliado por la IA: no está en tus fuentes"


@dataclass(frozen=True)
class ReviseTopic(GenerateTopic):
    @property
    def notes(self) -> str:
        return valid_notes(self.session)


def make_revise_topic(vault: Vault, *, notes: str | None = None) -> ReviseTopic:
    base = make_topic(vault)
    put_source(vault, base.subject, base.topic, "book", "page.jpg", b"\xff\xd8 book page", {})
    (sources_directory(vault, base.subject, base.topic, "book") / "page-001.md").write_text(
        BOOK_TRANSCRIPTION, encoding="utf-8"
    )
    write_notes(vault, base.subject, base.topic, notes or valid_notes(base.session))
    GitSync(vault).checkpoint("fixture")
    return ReviseTopic(vault=vault, subject=base.subject, topic=base.topic, session=base.session)


def ampliado_notes(session: str) -> str:
    """`valid_notes` with an AI-expanded paragraph in #definicion (valid only in `ampliado`)."""
    return (
        valid_notes(session)
        .replace(
            "Se escribe $f'(x)$.[^t2]\n",
            "Se escribe $f'(x)$.[^t2]\n\nTambién se usa la notación de Leibniz, df/dx.[^ia]\n",
        )
        .replace("[^p1]: [Apuntes", f"[^ia]: {IA_FOOTNOTE}\n[^p1]: [Apuntes")
    )
