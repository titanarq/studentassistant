"""The fixture vault of the web read API tests (`server/read_routes.py`), built through the
vault's public functions only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    start_session,
)

JPEG_BYTES = b"\xff\xd8\xff\xe0 fake jpeg bytes \xff\xd9"
WEB_MARKDOWN = "# Movimiento rectilíneo\n\n<script>alert(1)</script> texto de la web.\n"


@dataclass(frozen=True)
class ReadVault:
    """What `read_vault` put in `tmp_vault`: the ids and paths the read-route tests ask for."""

    vault: Vault
    subject: str
    topic: str
    empty_topic: str
    ended_session: str
    open_session: str
    notes_page: str
    web_page: str


def populate(tmp_vault: Vault) -> ReadVault:
    """`tmp_vault` populated through the vault's public functions only: subject `fisica` with
    topic `cinematica` (an ended 30-minute session and an unended one, both with transcript lines;
    two pending items, one resolved; a `notes` page with a transcription in its sidecar and a
    `web` page) and an empty topic `dinamica`."""
    subject = create_subject(tmp_vault, "Física").slug
    topic = create_topic(tmp_vault, subject, "Cinemática").slug
    empty = create_topic(tmp_vault, subject, "Dinámica").slug

    first = start_session(tmp_vault, subject, topic, host="ubuntu-pc", protocol_version="1.0")
    first.append_transcript(0, 4_000, "Hoy vemos el movimiento rectilíneo.")
    first.append_transcript(4_000, 9_000, "La velocidad es la derivada de la posición.")
    first.append_transcript(154_000, 190_000, "Un ejemplo con un coche que frena.")
    first.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "add_pending", "pending_id": "p1", "category": "illegible", "description": "x"},
    )
    first.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "add_pending", "pending_id": "p2", "category": "incomplete", "description": "y"},
    )
    first.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "resolve_pending", "pending_id": "p1", "resolution": "dice «aceleración»"},
    )
    end_session(first, ended_at=first.meta.started_at + timedelta(minutes=30))

    second = start_session(tmp_vault, subject, topic, host="ubuntu-pc", protocol_version="1.0")
    second.append_transcript(0, 3_000, "Seguimos con la caída libre.")

    notes_page = put_source(
        tmp_vault,
        subject,
        topic,
        "notes",
        "foto.jpg",
        JPEG_BYTES,
        {"capture_id": "cap-1", "session": first.id, "transcription": "v = dx/dt"},
    )
    web_page = put_source(
        tmp_vault,
        subject,
        topic,
        "web",
        "Movimiento rectilíneo",
        WEB_MARKDOWN,
        {"url": "https://example.org/mru", "fetched_at": "2026-09-24T10:00:00Z"},
    )
    return ReadVault(
        vault=tmp_vault,
        subject=subject,
        topic=topic,
        empty_topic=empty,
        ended_session=first.id,
        open_session=second.id,
        notes_page=notes_page.relative_to(tmp_vault.path).as_posix(),
        web_page=web_page.relative_to(tmp_vault.path).as_posix(),
    )
