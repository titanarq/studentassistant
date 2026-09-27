"""Set-aside captures downstream: the transcriber and the catch-up skip them, a restore makes one
owed again, content duplicates are set aside after transcription, the editor's input leaves them
out, the observer's fold ignores `capture.triaged`, the `triage` CLI and the optional Sonnet stage.

Every wait is bounded (`asyncio.wait_for`), so a job that never ends fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from generate_topic import CAPTURE_ID, make_topic
from studentassistant.cli import cli
from studentassistant.config import Settings, SourcesSettings
from studentassistant.editor.inputs import assemble_input
from studentassistant.llm import FakeClaude, load_prompt
from studentassistant.observer import CAPTURE_EVENT_KIND, STATE_OP_EVENT_KIND, fold
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.sources import (
    CAPTURE_TRIAGED_KIND,
    BurstStill,
    process_burst,
    set_capture_triage,
    store_capture,
    triage_status,
    triaged_payload,
)
from studentassistant.sources.catchup import PageRef, owed_pages, read_owed
from studentassistant.sources.transcriber import (
    PAGE_TRANSCRIBED_KIND,
    PageTranscriber,
    default_client_factory,
)
from studentassistant.sources.triage import change_for, decide
from studentassistant.sources.triage_llm import TOOL_NAME, refine_triage
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    read_topic_events,
    sources_directory,
    start_session,
)
from triage_images import encode, framed, paper, written

pytestmark = pytest.mark.anyio

WAIT = 10.0
FAST = SourcesSettings(
    capture_window_before_seconds=20,
    capture_window_after_seconds=0,
    transcription_grace_seconds=0,
    transcription_retry_seconds=0,
)
TEXT = (
    "# La comunicación\n\n- Emisor: quien envía el mensaje.\n- Receptor: quien lo recibe.\n"
    "- Canal: el medio físico por el que circula el mensaje.\n"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Lengua").slug
    return subject, create_topic(tmp_vault, subject, "La comunicación").slug


@pytest.fixture
def session(tmp_vault: Vault, topic: tuple[str, str]) -> Session:
    return start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
async def transcriber(bus: SessionBus, fake: FakeClaude) -> AsyncIterator[PageTranscriber]:
    worker = PageTranscriber(
        bus,
        bus.attached,
        settings=FAST,
        client_factory=default_client_factory(Settings(), fake),
    )
    worker.start()
    yield worker
    await asyncio.wait_for(worker.stop(), WAIT)


async def capture(bus: SessionBus, session: Session, capture_id: str, image: Any) -> str:
    """Store `image` as a capture of the session (triaged) and publish `capture.stored`."""
    stored = await asyncio.to_thread(
        store_capture,
        session.vault,
        session.subject_slug,
        session.topic_slug,
        "notes",
        [BurstStill(encode(image), "image/jpeg")],
        {"capture_id": capture_id, "session": session.id, "source_context": "notes"},
        30_000,
        FAST,
    )
    root = session.vault.path
    source_path = stored.path.relative_to(root).as_posix()
    await bus.publish(
        session.id,
        CAPTURE_EVENT_KIND,
        "phone",
        {
            "capture_id": capture_id,
            "trigger": "button",
            "image_count": 1,
            "source_path": source_path,
            "page_path": stored.page_path.relative_to(root).as_posix(),
            "source_context": "notes",
        },
        t=30_000,
    )
    return source_path


async def idle(worker: PageTranscriber, session: Session) -> None:
    await asyncio.wait_for(worker.wait_idle(session.id), WAIT)


def events(session: Session, kind: str) -> list[Any]:
    return [e for e in session.read_events() if e.kind == kind]


# -- the transcriber -------------------------------------------------------------------------------


async def test_a_set_aside_capture_is_never_transcribed(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    await capture(bus, session, "cap-blank", paper())
    await idle(transcriber, session)
    assert fake.requests == []
    assert events(session, PAGE_TRANSCRIBED_KIND) == []
    assert events(session, STATE_OP_EVENT_KIND) == []


async def test_a_restored_capture_is_transcribed_at_once(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    source_path = await capture(bus, session, "cap-blank", paper())
    await idle(transcriber, session)
    assert fake.requests == []

    fake.reply_text("# Página casi en blanco\n\nUna fecha: 26 de septiembre.")
    restored = await asyncio.to_thread(
        set_capture_triage,
        session.vault,
        session.subject_slug,
        session.topic_slug,
        source_path,
        "restore",
        sync=None,
    )
    change = change_for(
        session.vault, session.subject_slug, session.topic_slug, source_path, restored
    )
    await bus.publish(session.id, CAPTURE_TRIAGED_KIND, "user", triaged_payload(change))
    await transcriber.drain()
    await idle(transcriber, session)

    assert len(fake.requests) == 1
    [done] = events(session, PAGE_TRANSCRIBED_KIND)
    assert done.payload["capture_id"] == "cap-blank"
    assert done.payload["capture_session_id"] == session.id


async def test_an_automatic_triage_event_starts_no_transcription(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    source_path = await capture(bus, session, "cap-blank", paper())
    await idle(transcriber, session)
    payload = {"capture_id": "cap-blank", "source_path": source_path, "status": "kept"}
    await bus.publish(
        session.id, CAPTURE_TRIAGED_KIND, "observer", payload | {"decided_by": "auto"}
    )
    await transcriber.drain()
    await idle(transcriber, session)
    assert fake.requests == []


async def test_a_transcription_like_another_page_sets_the_worse_one_aside(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text(TEXT)
    fake.reply_text(TEXT.replace("Receptor", "[[?Receptor]]"))
    await capture(bus, session, "cap-1", framed(written(3)))
    await idle(transcriber, session)
    await capture(bus, session, "cap-2", framed(written(8)))  # another framing of the page
    await idle(transcriber, session)

    assert len(events(session, PAGE_TRANSCRIBED_KIND)) == 2
    assert [e.origin for e in events(session, CAPTURE_TRIAGED_KIND)] == ["sources"]
    triaged = [e.payload for e in events(session, CAPTURE_TRIAGED_KIND)]
    assert len(triaged) == 1
    assert triaged[0]["capture_id"] == "cap-2"
    assert triaged[0]["status"] == "set_aside"
    assert triaged[0]["reasons"] == ["same_content"]
    assert triaged[0]["duplicate_of"] == "sources/notes/page-001.jpg"
    status = triage_status(session.vault, session.subject_slug, session.topic_slug)
    assert status["sources/notes/page-002.jpg"].set_aside


# -- the catch-up ----------------------------------------------------------------------------------


def test_owed_pages_skip_set_aside_captures(
    tmp_vault: Vault, topic: tuple[str, str], session: Session
) -> None:
    for capture_id, image in (("cap-1", framed(written(3))), ("cap-2", paper())):
        stored = store_capture(
            tmp_vault,
            *topic,
            "notes",
            [BurstStill(encode(image), "image/jpeg")],
            {"capture_id": capture_id, "session": session.id},
            1_000,
            FAST,
        )
        session.append_event(
            CAPTURE_EVENT_KIND,
            "phone",
            {
                "capture_id": capture_id,
                "source_path": stored.path.relative_to(tmp_vault.path).as_posix(),
            },
        )
    owed = read_owed(tmp_vault, *topic)
    assert [page.capture_id for page in owed.to_transcribe] == ["cap-1"]

    events_ = list(read_topic_events(tmp_vault, *topic))
    everything, _ = owed_pages(events_)
    assert [page.capture_id for page in everything] == ["cap-1", "cap-2"]
    skipped, _ = owed_pages(events_, set_aside={everything[0].source_path})
    assert [page.capture_id for page in skipped] == ["cap-2"]
    assert isinstance(skipped[0], PageRef)

    # Restored, the blank page is owed again.
    set_capture_triage(tmp_vault, *topic, "sources/notes/page-002.jpg", "restore", sync=None)
    owed = read_owed(tmp_vault, *topic)
    assert [page.capture_id for page in owed.to_transcribe] == ["cap-1", "cap-2"]


# -- the editor's input ----------------------------------------------------------------------------


def test_the_editor_input_leaves_set_aside_pages_and_their_open_doubts_out(
    tmp_vault: Vault,
) -> None:
    generate = make_topic(tmp_vault)
    later = start_session(tmp_vault, generate.subject, generate.topic, "pc", PROTOCOL_VERSION)
    later.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {
            "op": "add_pending",
            "pending_id": "p-page",
            "kind": "illegible",
            "text": "Palabra ilegible solo en la página 1.",
            "capture_ids": [CAPTURE_ID],
        },
    )
    later.append_event(CAPTURE_TRIAGED_KIND, "observer", {"capture_id": CAPTURE_ID})
    end_session(later)

    def assembled_text() -> str:
        assembled = assemble_input(
            tmp_vault, generate.subject, generate.topic, prompt=load_prompt("editor_generate")
        )
        ids = [source.source_id for source in assembled.catalogue]
        text = "\n".join(b.get("text", "") for b in assembled.content)
        return "|".join(ids) + "\n" + text

    before = assembled_text()
    assert "sources/notes/page-001.jpg" in before.split("\n", 1)[0]
    assert "p-page" in before

    set_capture_triage(
        tmp_vault, generate.subject, generate.topic, "sources/notes/page-001.jpg", "set_aside",
        sync=None,
    )  # fmt: skip
    after = assembled_text()
    ids, text = after.split("\n", 1)
    assert ids.split("|") == ["sources/notes/page-002.jpg"]
    assert "Derivada: límite del cociente incremental." not in text  # page 1's transcription
    assert "p-page" not in text
    assert "p-1" in text  # a doubt about the transcript stays


def test_the_observer_fold_ignores_capture_triaged(
    tmp_vault: Vault, topic: tuple[str, str], session: Session
) -> None:
    session.append_event(CAPTURE_TRIAGED_KIND, "observer", {"capture_id": "cap-1"})
    before = fold([])
    after = fold(list(read_topic_events(tmp_vault, *topic)))
    assert after.open_pending() == before.open_pending()
    assert after.sections == before.sections


# -- the Sonnet stage ------------------------------------------------------------------------------


async def test_the_sonnet_verdict_replaces_only_an_ambiguous_check(fake: FakeClaude) -> None:
    settings = SourcesSettings(triage_llm_check=True)
    processed = process_burst([encode(framed(written(3)))], settings)
    result = decide(
        {"ink_ratio": 0.05, "border_ink": 0.0, "sharpness": 24.0, "dhash": "0" * 16},
        [],
        settings,
    )
    assert result.status == "set_aside"
    fake.reply_tool(
        TOOL_NAME, {"blank": False, "blurry": False, "partial": False, "reason": "Se lee bien."}
    )
    client = default_client_factory(Settings(), fake)(None)  # type: ignore[arg-type]
    refined = await refine_triage(client, processed.page, result, settings)
    assert refined.status == "kept"
    assert refined.metrics["llm_verdict"] == {"blurry": False}
    assert refined.metrics["llm_reason"] == "Se lee bien."
    [request] = fake.requests
    assert request.role == "transcriber"
    assert request.messages[0]["content"][0]["type"] == "image"

    clear = decide({**result.metrics, "sharpness": 300.0}, [], settings)
    assert await refine_triage(client, processed.page, clear, settings) is clear
    assert len(fake.requests) == 1  # nothing ambiguous: nothing sent


async def test_a_failed_sonnet_check_keeps_the_metrics_decision(fake: FakeClaude) -> None:
    settings = SourcesSettings(triage_llm_check=True)
    result = decide(
        {"ink_ratio": 0.05, "border_ink": 0.0, "sharpness": 24.0, "dhash": "0" * 16},
        [],
        settings,
    )
    fake.reply_text("no sé")
    fake.reply_text("tampoco")
    client = default_client_factory(Settings(), fake)(None)  # type: ignore[arg-type]
    assert await refine_triage(client, b"", result, settings) == result


# -- the CLI ---------------------------------------------------------------------------------------


@pytest.fixture
def configured(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    return tmp_vault


def _legacy_capture(vault: Vault, topic: tuple[str, str], image: Any, number: int) -> None:
    """A capture stored before triage existed: no `triage` block in its sidecar."""
    processed = process_burst([encode(image)], FAST)
    put_source(
        vault,
        *topic,
        "notes",
        "capture.jpg",
        processed.still,
        {
            "capture_id": f"old-{number}",
            "selected_image": 1,
            "sharpness": [processed.sharpness[0]],
            "page_detected": processed.page_detected,
        },
        {"page.jpg": processed.page},
    )


def test_the_triage_cli_prints_decisions_and_applies_them(
    configured: Vault, topic: tuple[str, str]
) -> None:
    for number, image in enumerate(
        (framed(written(3)), framed(written(3), shift=(5, 3)), paper(), framed(written(8))), 1
    ):
        _legacy_capture(configured, topic, image, number)
    runner = CliRunner()

    dry = runner.invoke(cli, ["triage", *topic])
    assert dry.exit_code == 0, dry.output
    lines = dry.output.strip().splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("sources/notes/page-001.jpg: tinta=")
    assert "-> se queda" in lines[0]
    assert "apartada (repetida de sources/notes/page-001.jpg)" in lines[1]
    assert "apartada (página en blanco)" in lines[2]
    assert "-> se queda" in lines[3]
    directory = sources_directory(configured, *topic, "notes")
    assert "triage" not in yaml.safe_load((directory / "page-002.yaml").read_text("utf-8"))

    applied = runner.invoke(cli, ["triage", *topic, "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "Guardado el triaje de 4 capturas." in applied.output
    status = triage_status(configured, *topic)
    assert [status[f"sources/notes/page-{n:03d}.jpg"].status for n in (1, 2, 3, 4)] == [
        "kept",
        "set_aside",
        "set_aside",
        "kept",
    ]
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=configured.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log == f"Triaje de 4 capturas de {topic[0]}/{topic[1]}"

    again = runner.invoke(cli, ["triage", *topic, "--apply"])
    assert "nada que guardar" in again.output
    assert "[guardado: apartada" in again.output


def test_the_triage_cli_refuses_an_unknown_topic(configured: Vault) -> None:
    result = CliRunner().invoke(cli, ["triage", "nada", "nada"])
    assert result.exit_code == 1
    assert "No existe el tema" in result.output
