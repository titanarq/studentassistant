"""`eval import-session`: a session of a temporary vault becomes a case `read_case` and `run_eval`
accept, with reference drafts prefilled from what the vault stored; the vault is never written."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval_fixtures import PipelineClaude
from studentassistant.cli import cli
from studentassistant.config import EditorSettings, ObserverSettings, Settings, SourcesSettings
from studentassistant.evals import read_case, render_report, run_eval
from studentassistant.evals.cases import DRAFT_MARKER
from studentassistant.evals.import_session import (
    SessionImportError,
    default_case_name,
    import_session,
)
from studentassistant.sources import BurstStill, store_capture
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_page_transcription,
    start_session,
)
from triage_images import encode, framed, paper, written

START_MS = 1_760_000_000_000
RUN_TIMEOUT_S = 60
PAGE_TEXT = "# La célula\n\n- membrana, citoplasma y núcleo\n"
FINALS = (
    ("seg-1", 500, 3500, "La célula es la unidad básica de los seres vivos."),
    ("seg-2", 4000, 7000, "Tiene tres partes: membrana, citoplasma y núcleo."),
    (None, 11000, 14500, "En el libro pone que el núcleo guarda el material genético."),
)


@dataclass(frozen=True)
class StoredSession:
    subject: str
    topic: str
    session_id: str
    # The capture ids, in order: a written page (a burst of two), a blank sheet, the page again.
    captures: tuple[str, str, str]


def _capture(vault: Vault, session, subject: str, topic: str, t: int, images) -> str:  # noqa: ANN001
    capture_id = str(uuid.uuid4())
    meta = {
        "capture_id": capture_id,
        "session": session.id,
        "captured_at": datetime.fromtimestamp((START_MS + t) / 1000, tz=UTC),
        "trigger": "button",
        "image_count": len(images),
        "source_context": "book",
    }
    stills = [BurstStill(encode(image), "image/jpeg") for image in images]
    stored = store_capture(vault, subject, topic, "book", stills, meta, t, SourcesSettings())
    relative = stored.path.relative_to(vault.path).as_posix()
    session.append_event(
        "capture.stored",
        "phone",
        {
            "capture_id": capture_id,
            "trigger": "button",
            "image_count": len(images),
            "client_time_ms": START_MS + t,
            "source_path": relative,
            "page_path": stored.page_path.relative_to(vault.path).as_posix(),
            "source_context": "book",
        },
        t=t,
    )
    return capture_id


def _session(vault: Vault) -> StoredSession:
    """A study session as the backend leaves it: finals, a source switch, a marker, captures."""
    subject = create_subject(vault, "Biología").slug
    topic = create_topic(vault, subject, "La célula").slug
    session = start_session(vault, subject, topic, host="test", protocol_version="1.6")
    session.append_event(
        "session.started",
        "user",
        {"subject_id": subject, "topic_id": topic, "client_time_ms": START_MS},
        t=0,
    )
    for segment_id, start, end, text in FINALS:
        session.append_transcript(start, end, text)
        if segment_id is not None:  # the last final's event is missing: it becomes seg-<seq>
            session.append_event(
                "transcript.final",
                "stt",
                {
                    "segment_id": segment_id,
                    "session_start_ms": start,
                    "session_end_ms": end,
                    "text": text,
                    "language": "es-ES",
                    "provider": "web-speech",
                },
                t=start,
            )
    session.append_event("button", "phone", {"button": "switch_source", "source": "book"}, t=7500)
    session.append_event("marker", "phone", {"label": "importante"}, t=7600)
    page = framed(written(3))
    first = _capture(vault, session, subject, topic, 8000, [framed(written(3), blur=9), page])
    blank = _capture(vault, session, subject, topic, 9000, [paper()])
    again = _capture(vault, session, subject, topic, 10000, [framed(written(3), shift=(25, -15))])
    put_page_transcription(
        vault, f"subjects/{subject}/topics/{topic}/sources/book/page-001.jpg", PAGE_TEXT
    )
    end_session(session)
    return StoredSession(subject, topic, session.id, (first, blank, again))


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


def test_a_vault_session_becomes_a_case_that_reads_and_replays(
    tmp_vault: Vault, tmp_path: Path
) -> None:
    stored = _session(tmp_vault)
    before = _snapshot(tmp_vault.path)
    out = tmp_path / "evals" / "celula"

    imported = import_session(
        tmp_vault, stored.subject, stored.topic, stored.session_id, out, language="es-ES"
    )

    assert _snapshot(tmp_vault.path) == before  # nothing written into the vault
    assert (imported.finals, imported.events, imported.captures) == (3, 2, 3)
    assert imported.pages == [stored.captures[0]] and imported.skipped == []
    case = read_case(out)
    manifest = case.recording.manifest
    assert (manifest.subject, manifest.topic, manifest.stt_mode) == (
        stored.subject,
        stored.topic,
        "client",
    )
    assert manifest.stt_provider == "web-speech" and manifest.started_client_time_ms == START_MS
    # The session's own segment ids where the events name them, `seg-<seq>` otherwise.
    assert [f.segment_id for f in case.finals] == ["seg-1", "seg-2", "seg-3"]
    assert [(f.client_start_ms, f.client_end_ms) for f in case.finals][0] == (
        START_MS + 500,
        START_MS + 3500,
    )
    assert [e.type for e in case.recording.events] == ["button", "marker"]
    # Every capture on the session clock; the first one with both stills of its burst.
    captures = case.recording.captures
    assert [c.metadata.capture_id for c in captures] == list(stored.captures)
    assert [c.metadata.client_time_ms for c in captures] == [
        START_MS + 8000,
        START_MS + 9000,
        START_MS + 10000,
    ]
    assert [len(c.image_paths) for c in captures] == [2, 1, 1]

    # The reference drafts: the stored transcription, the triage each capture got, no notes.
    assert case.reference_pages == {stored.captures[0]: "\n\n" + PAGE_TEXT}
    assert case.reference_notes.strip() == "# La célula"
    assert case.reference_triage is not None
    assert [(t.capture_id, t.status, t.reasons) for t in case.reference_triage] == [
        (stored.captures[0], "kept", ()),
        (stored.captures[1], "set_aside", ("blank",)),
        (stored.captures[2], "set_aside", ("duplicate",)),
    ]
    assert set(case.drafts) == {"notes.md", f"pages/{stored.captures[0]}.md", "triage.yaml"}
    assert DRAFT_MARKER in (out / "reference" / "triage.yaml").read_text(encoding="utf-8")

    # The replay accepts it, and the run's triage matches the one the session got.
    runs = tmp_path / "evals" / "runs" / "now"
    settings = Settings(observer=ObserverSettings(), editor=EditorSettings(prepare_mode="single"))
    report = asyncio.run(
        asyncio.wait_for(
            run_eval(
                [case], runs, settings=settings, transport=PipelineClaude(runs), sleep=_no_wait
            ),
            RUN_TIMEOUT_S,
        )
    )
    [result] = report.cases
    assert result.error is None, result
    assert result.triage is not None and result.triage.mismatches == []
    assert (result.triage.precision, result.triage.recall, result.triage.f1) == (1.0, 1.0, 1.0)
    assert len(result.pages) == 1 and result.pages[0].transcribed
    markdown = render_report(report)
    assert "referencia en borrador sin corregir" in markdown and "Triaje de capturas" in markdown


def test_an_existing_output_or_an_unknown_session_is_refused(
    tmp_vault: Vault, tmp_path: Path
) -> None:
    stored = _session(tmp_vault)
    taken = tmp_path / "taken"
    taken.mkdir()
    with pytest.raises(SessionImportError, match="already exists"):
        import_session(
            tmp_vault, stored.subject, stored.topic, stored.session_id, taken, language="es-ES"
        )
    with pytest.raises(SessionImportError, match="no session"):
        import_session(
            tmp_vault,
            stored.subject,
            stored.topic,
            "20000101-000000",
            tmp_path / "x",
            language="es-ES",
        )
    assert not (tmp_path / "x").exists()


@pytest.fixture
def cli_env(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    path = tmp_path / "evals"
    monkeypatch.setenv("SA_EVAL__PATH", str(path))
    return path


def test_the_cli_imports_into_the_eval_set_and_refuses_the_vault(
    cli_env: Path, tmp_vault: Vault
) -> None:
    stored = _session(tmp_vault)
    arguments = ["eval", "import-session", stored.subject, stored.topic, stored.session_id]

    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == 0, result.output
    case = cli_env / default_case_name(stored.subject, stored.topic, stored.session_id)
    assert f"Caso creado en {case}" in result.output and "3 capturas" in result.output
    assert "reference/triage.yaml" in result.output
    assert read_case(case).reference_triage is not None

    again = CliRunner().invoke(cli, arguments)
    assert again.exit_code == 1 and "No se puede importar" in again.output

    inside = CliRunner().invoke(cli, [*arguments, "--out", str(tmp_vault.path / "case")])
    assert inside.exit_code == 1 and "bóveda" in inside.output
    assert not (tmp_vault.path / "case").exists()
