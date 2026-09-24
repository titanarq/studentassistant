"""The fidelity validator: every ADR-0005 rule, in Spanish, against sources stored in a vault."""

from __future__ import annotations

import pytest
from notes_samples import FIXTURE_SESSION, read_fixture

from studentassistant.editor.notes_format import (
    SourceExists,
    parse,
    serialize,
    topic_source_resolver,
    validate,
)
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    put_source,
    start_session,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Revolución Industrial").slug


@pytest.fixture
def stocked(tmp_vault: Vault, topic: tuple[str, str]) -> tuple[SourceExists, str]:
    """A topic holding every source the fixture cites; returns its resolver and session id."""
    subject, slug = topic
    put_source(tmp_vault, subject, slug, "notes", "foto1.jpg", JPEG, {"capture_id": "c1"})
    put_source(tmp_vault, subject, slug, "notes", "foto2.jpg", JPEG, {"capture_id": "c2"})
    put_source(tmp_vault, subject, slug, "book", "libro.jpg", JPEG, {})
    put_source(tmp_vault, subject, slug, "pdf", "tema.pdf", b"%PDF-1.7\n", {})
    put_source(
        tmp_vault, subject, slug, "web", "Máquina de vapor", "# Máquina de vapor\n", {"url": "x"}
    )
    session = start_session(tmp_vault, subject, slug, host="test", protocol_version="1")
    session.append_transcript(154_000, 190_000, "la fábrica sustituye al taller")
    return topic_source_resolver(tmp_vault, subject, slug), session.meta.id


def _fixture_for(session_id: str) -> str:
    return read_fixture("apuntes.md").replace(FIXTURE_SESSION, session_id)


def test_the_fixture_is_valid_when_every_cited_source_is_in_the_vault(
    stocked: tuple[SourceExists, str],
) -> None:
    source_exists, session_id = stocked

    assert validate(_fixture_for(session_id), "estricto", source_exists) == []


def test_a_cited_source_missing_from_the_vault_is_reported(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic
    put_source(tmp_vault, subject, slug, "notes", "foto1.jpg", JPEG, {})
    text = (
        "## A {#a}\n\nUno.[^p1] Dos.[^p2] Tres.[^t1]\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^p2]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
        "[^t1]: [Transcripción, 00:00:01–00:00:02]"
        "(../sessions/20260101-000000/transcript.jsonl#t=00:00:01-00:00:02)\n"
    )

    errors = validate(text, "estricto", topic_source_resolver(tmp_vault, subject, slug))

    assert errors == [
        "Nota al pie [^p2]: la fuente sources/notes/page-002.jpg no existe en el tema.",
        "Nota al pie [^t1]: la fuente sessions/20260101-000000#t=00:00:01-00:00:02"
        " no existe en el tema.",
    ]


def test_the_resolver_answers_nothing_outside_the_topic_sources(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    source_exists = topic_source_resolver(tmp_vault, *topic)

    assert not source_exists("topic.yaml")
    assert not source_exists("sources/notes/../../topic.yaml")
    assert not source_exists("../../../vault.yaml")


def test_a_block_without_footnote_is_reported_with_its_section_and_opening() -> None:
    text = (
        "# Tema\n\nIntroducción sin fuente.\n\n## 2. Causas {#causas}\n\n"
        "Con fuente.[^p1]\n\n- Una lista\n- sin notas\n\n| a |\n|---|\n\n---\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
    )

    errors = validate(text)

    assert errors == [
        "Antes de la primera sección, bloque 2 (párrafo «Introducción sin fuente.»): no tiene"
        " nota al pie de procedencia; cita la fuente de la que sale o, si no está en tus"
        " fuentes, pregunta al estudiante.",
        "Sección #causas, bloque 2 (lista «- Una lista - sin notas»): no tiene nota al pie de"
        " procedencia; cita la fuente de la que sale o, si no está en tus fuentes, pregunta al"
        " estudiante.",
        "Sección #causas, bloque 3 (tabla «| a | |---|»): no tiene nota al pie de procedencia;"
        " cita la fuente de la que sale o, si no está en tus fuentes, pregunta al estudiante.",
    ]


def test_a_reference_without_definition_is_reported() -> None:
    errors = validate("## A {#a}\n\nTexto.[^p9]\n")

    assert errors == [
        "Sección #a, bloque 1 (párrafo «Texto.»): cita [^p9], que no tiene definición."
    ]


def test_a_definition_that_is_not_a_source_link_is_reported() -> None:
    errors = validate("## A {#a}\n\nTexto.[^p1]\n\n[^p1]: Apuntes, página 1\n")

    assert errors == [
        "Nota al pie [^p1]: la nota [^p1] no es un enlace Markdown a una fuente del tema."
    ]


def test_ia_is_refused_in_estricto_the_default() -> None:
    text = read_fixture("ampliado.md")

    errors = validate(text)

    assert errors == validate(text, "estricto")
    assert errors == [
        "Sección #consecuencias, bloque 2 (párrafo «Las condiciones en las fábricas dieron o…»):"
        " usa [^ia] (contenido que no está en tus fuentes), que el modo de fidelidad estricto"
        " no permite; pregunta al estudiante en su lugar.",
        "Nota al pie [^ia]: el modo de fidelidad estricto no admite contenido ampliado por la IA.",
    ]


def test_ia_is_allowed_in_ampliado_but_must_still_be_defined() -> None:
    assert validate(read_fixture("ampliado.md"), "ampliado") == []
    assert validate("## A {#a}\n\nAmpliado.[^ia]\n", "ampliado") == [
        "Sección #a, bloque 1 (párrafo «Ampliado.»): cita [^ia], que no tiene definición."
    ]


def test_sections_need_a_unique_anchor_and_footnotes_a_unique_definition() -> None:
    text = (
        "## Sin ancla\n\nUno.[^p1]\n\n## B {#b}\n\nDos.[^p1]\n\n## Otra B {#b}\n\nTres.[^p1]\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^p1]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
    )

    assert validate(text) == [
        "La nota al pie [^p1] está definida más de una vez.",
        "La sección «Sin ancla» no tiene ancla estable: escribe su título como"
        " «## Título {#ancla}».",
        "El ancla #b se repite en más de una sección.",
    ]


def test_the_validator_never_changes_the_notes(stocked: tuple[SourceExists, str]) -> None:
    source_exists, _ = stocked
    text = read_fixture("ampliado.md") + "\nSin fuente.\n"
    notes = parse(text)

    assert validate(notes, "estricto", source_exists)
    assert serialize(notes) == text
