"""The topic digest: rendered from the log, idempotent, written at the session end, read back."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from studentassistant.observer import (
    TopicEvent,
    digest_excerpt,
    fold,
    regenerate_topic_digest,
    render_digest,
    topic_digest,
)
from studentassistant.observer.digest import NOTES_PER_SESSION, session_date
from studentassistant.vault import (
    Session,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    end_session,
    read_topic_digest,
    start_session,
    topic_digest_path,
    write_topic_digest,
)

from .script import FIRST_SESSION, SECOND_SESSION, ScriptedEvent, op, segment, to_topic_events


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def _render(events: list[TopicEvent]) -> str:
    return render_digest("Matemáticas II", "Derivadas", events, fold(events))


def _record(vault: Vault, topic: tuple[str, str], events: list[ScriptedEvent]) -> Session:
    session = start_session(vault, *topic, host="ubuntu-pc", protocol_version="1.0")
    for origin, kind, payload in events:
        session.append_event(kind, origin, payload)
    return session


# -- rendering ------------------------------------------------------------------------------------


def test_the_digest_holds_the_outline_each_session_and_the_open_doubts(
    topic_events: list[TopicEvent],
) -> None:
    text = _render(topic_events)

    assert text.startswith("# Resumen del tema: Derivadas\n\n")
    assert (
        "2 sesiones con contenido; la última, el 25/09/2026: Reglas, Regla de la cadena."
        " 1 duda abierta." in text
    )
    assert "Asignatura: Matemáticas II." in text
    # The outline, nested, with how many segments each section holds.
    assert (
        "## Índice\n\n- Definición de derivada (2 fragmentos)\n- Reglas (2 fragmentos)\n"
        "  - Regla de la cadena (1 fragmento)\n" in text
    )
    first, second = text.split("### Sesión ")[1:]
    assert first.startswith("1 — 24/09/2026 (terminada, 0 min)")
    assert "- Fuente: apuntes" in first
    assert "- Apartados trabajados: Definición de derivada, Reglas" in first  # s1-c moved later
    assert "- Conceptos nuevos: límite del cociente incremental" in first
    assert "- Material: 3 fragmentos de voz, 1 página capturada" in first
    assert "- Dudas: 1 nuevas, 0 resueltas" in first
    assert second.startswith("2 — 25/09/2026 (terminada, 0 min)")
    assert "- Fuente: libro (Stewart, cap. 3, p. 112)" in second
    assert "- Apartados trabajados: Reglas, Regla de la cadena" in second
    assert "- Dudas: 1 nuevas, 1 resueltas" in second
    assert "  - El profesor insiste en la notación de Leibniz" in second
    assert text.endswith(
        "## Dudas abiertas\n\n"
        "- [Concepto sin explicar] Se menciona la regla de L'Hôpital sin explicarla (sesión 2)\n"
    )
    # The resolved doubt is only counted, never listed as open.
    assert "Palabra ilegible" not in text


def test_the_digest_is_idempotent_for_the_same_log(topic_events: list[TopicEvent]) -> None:
    other = to_topic_events([(FIRST_SESSION, []), (SECOND_SESSION, [])])
    assert _render(topic_events) == _render(list(topic_events))
    assert _render(topic_events) != _render(other)


def test_an_empty_topic_and_an_empty_session() -> None:
    assert _render([]) == (
        "# Resumen del tema: Derivadas\n\n"
        "Todavía no hay contenido registrado. Sin dudas abiertas.\n\n"
        "Asignatura: Matemáticas II.\n\n"
        "## Índice\n\nSin apartados todavía.\n\n"
        "## Sesiones\n\nNinguna sesión todavía.\n\n"
        "## Dudas abiertas\n\nNinguna.\n"
    )
    started = to_topic_events([(FIRST_SESSION, [("phone", "session.started", {})])])
    text = _render(started)
    assert "### Sesión 1 — 24/09/2026 (sin terminar, 0 min)\n\nSin contenido registrado." in text


def test_only_the_latest_remarks_of_a_session_are_kept_and_long_ones_are_cut() -> None:
    notes = [op("note", text=f"Observación {n}") for n in range(NOTES_PER_SESSION + 2)]
    notes.append(op("note", text="larga " * 100))
    text = _render(to_topic_events([(FIRST_SESSION, notes)]))

    assert "Observación 0" not in text and "Observación 1\n" not in text
    assert f"Observación {NOTES_PER_SESSION + 1}" in text
    long_line = next(line for line in text.splitlines() if "larga" in line)
    assert long_line.endswith("…") and len(long_line) < 260


def test_the_excerpt_is_the_summary_paragraph(topic_events: list[TopicEvent]) -> None:
    excerpt = digest_excerpt(_render(topic_events))
    assert excerpt is not None and excerpt.startswith("2 sesiones con contenido")
    assert digest_excerpt(None) is None and digest_excerpt("# Solo título\n") is None
    assert digest_excerpt("# T\n\n" + "a" * 1000, limit=50) == "a" * 49 + "…"


def test_session_dates_come_from_the_session_ids() -> None:
    assert session_date("20260924-180000") == "24/09/2026"
    assert session_date("not-an-id") == "not-an-id"


# -- the vault ---------------------------------------------------------------------------------


def test_regenerating_writes_the_digest_once_for_the_same_log(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = _record(
        tmp_vault,
        topic,
        [
            ("phone", "session.started", {}),
            segment("s-1"),
            op("add_section", section_id="sec-1", title="Definición"),
            op("assign_segments", section_id="sec-1", segment_ids=["s-1"]),
        ],
    )
    assert topic_digest(tmp_vault, *topic) is None

    assert regenerate_topic_digest(tmp_vault, *topic) is True
    stored = read_topic_digest(tmp_vault, *topic)
    assert stored is not None and "- Definición (1 fragmento)" in stored
    assert topic_digest(tmp_vault, *topic) == stored
    before = topic_digest_path(tmp_vault, *topic).stat().st_mtime_ns

    assert regenerate_topic_digest(tmp_vault, *topic) is False
    assert topic_digest_path(tmp_vault, *topic).stat().st_mtime_ns == before

    session.append_event("session.ended", "user", {})
    end_session(session, ended_at=datetime(2026, 9, 26, tzinfo=UTC))
    assert regenerate_topic_digest(tmp_vault, *topic) is True
    assert "(terminada," in (read_topic_digest(tmp_vault, *topic) or "")


def test_the_vault_digest_file_round_trips_and_refuses_an_unknown_topic(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    path = write_topic_digest(tmp_vault, *topic, "# Resumen\n\nTexto.\n")
    assert path == topic_digest_path(tmp_vault, *topic)
    assert path.relative_to(tmp_vault.path).parts[-2:] == ("state", "digest.md")
    assert read_topic_digest(tmp_vault, *topic) == "# Resumen\n\nTexto.\n"
    with pytest.raises(TopicNotFoundError):
        write_topic_digest(tmp_vault, topic[0], "no-existe", "x")
    with pytest.raises(TopicNotFoundError):
        read_topic_digest(tmp_vault, topic[0], "no-existe")


def test_a_compacted_session_is_described_from_the_state_without_status(
    topic_events: list[TopicEvent],
) -> None:
    state = fold(topic_events)
    later = [(sid, e) for sid, e in topic_events if sid == SECOND_SESSION]
    text = render_digest("Matemáticas II", "Derivadas", later, state)

    first = text.split("### Sesión ")[1]
    assert first.startswith("1 — 24/09/2026\n")
    assert "- Conceptos nuevos: límite del cociente incremental" in first
