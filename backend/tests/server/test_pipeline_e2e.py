"""The whole pipeline, end to end: the sample recording replayed into master notes, pushed.

One in-process app (`create_app` with its lifespan, as `serve` runs it) gets the sample recording
through `replay` exactly as a capture client would send it: the session WebSocket and the client
STT path (the recording's finals), the capture upload and its processing, the page transcriber,
the live observer and, after the session end, "prepárame el tema" over REST. Claude is scripted
per role (observer, transcriber, editor) with `FakeClaude`; the vault's `GitSync` pushes to a
local bare repository (`git_origin`).

Everything is asserted on the vault the remote received -- a fresh clone of `git_origin` -- never
on logs: the transcript, the stored pages and their transcription, the session events, the
pending-review queue and notes that pass the ADR-0005 validator, tagged as version 1.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import yaml
from fastapi import FastAPI

from studentassistant.config import (
    ObserverSettings,
    ServerSettings,
    Settings,
    SttSettings,
    VaultGitSettings,
)
from studentassistant.editor.notes_format import (
    page_provenance,
    topic_source_resolver,
    transcript_provenance,
    validate,
)
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.observer import CAPTURE_EVENT_KIND, STATE_OP_EVENT_KIND
from studentassistant.observer.live import TOOL_NAME
from studentassistant.protocol import TranscriptClientFinal
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import read_recording
from studentassistant.server.replay import AsgiTransport, ReplayResult, replay
from studentassistant.sources.transcriber import PAGE_TRANSCRIBED_KIND
from studentassistant.vault import Event, GitSync, Vault, read_jsonl

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"
SUBJECT, TOPIC = "biologia", "la-celula"
PAGE_TRANSCRIPTION = "# La célula\n\n- membrana, [[?citoplasma]] y núcleo\n"
# Generous bounds: the whole test takes a few seconds; these only keep a regression from hanging.
REPLAY_TIMEOUT_S = 30
GENERATE_TIMEOUT_S = 15


class ClaudeByRole:
    """A transport that hands each request to the `FakeClaude` scripted for its role.

    The observer, the page transcriber and the editor share the app's one transport and run
    concurrently, so a single script would be consumed in whatever order they happen to ask.
    """

    def __init__(self, **fakes: FakeClaude) -> None:
        self.fakes = fakes

    async def send(self, request: LLMRequest, **options: object) -> LLMResponse:
        return await self.fakes[request.role].send(request, **options)  # type: ignore[arg-type]


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


def _notes(session_id: str) -> str:
    """Master notes citing the replayed book page and two spans of the replayed transcript."""
    page = page_provenance("book", 1)
    first = transcript_provenance(session_id, 0, 4)
    second = transcript_provenance(session_id, 4, 8)
    return (
        "# La célula\n"
        "\n"
        "## 1. Definición {#definicion}\n"
        "\n"
        "La célula es la unidad básica de los seres vivos.[^t1]\n"
        "\n"
        "## 2. Partes {#partes}\n"
        "\n"
        "- Membrana, citoplasma y núcleo.[^t2][^p1]\n"
        "\n"
        f"{page.definition('p1')}\n"
        f"{first.definition('t1')}\n"
        f"{second.definition('t2')}\n"
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout


def test_a_replayed_session_becomes_pushed_master_notes(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    git_origin: Path,
) -> None:
    observer = FakeClaude().reply_tool(
        TOOL_NAME,
        {
            "ops": [
                {"op": "add_section", "section_id": "sec-1", "title": "La célula"},
                {
                    "op": "add_pending",
                    "pending_id": "obs-1",
                    "kind": "unexplained_concept",
                    "text": "Se nombra el núcleo sin explicar qué hace.",
                },
            ]
        },
    )
    for _ in range(8):  # more batches than the sample can make
        observer.reply_tool(TOOL_NAME, {"ops": []})
    transcriber = FakeClaude().reply_text(PAGE_TRANSCRIPTION)
    editor = FakeClaude()
    claude = ClaudeByRole(observer=observer, transcriber=transcriber, editor=editor)
    app: FastAPI = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        sync=GitSync(tmp_vault, VaultGitSettings()),
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
        llm_transport=claude,
        llm_settings=Settings(observer=ObserverSettings()),
    )

    async def main() -> tuple[ReplayResult, int, dict[str, object]]:
        async with AsgiTransport(app) as transport:  # runs the lifespan: sync loop, shutdown flush
            result = await asyncio.wait_for(
                replay(read_recording(FIXTURE), transport, speed=10, sleep=_no_wait),
                REPLAY_TIMEOUT_S,
            )
            editor.reply_text(_notes(result.session_id))
            generated = await asyncio.wait_for(
                transport.request(
                    "POST",
                    f"/api/subjects/{SUBJECT}/topics/{TOPIC}/notes/generate",
                    b"{}",
                    "application/json",
                ),
                GENERATE_TIMEOUT_S,
            )
            return result, generated.status, generated.json()

    result, status, generated = asyncio.run(main())

    assert status == 200, generated
    assert generated["version"] == 1 and generated["draft"] is False
    assert observer.requests and len(transcriber.requests) == 1 and len(editor.requests) == 1

    # -- what the remote holds: a fresh clone of the local "GitHub" --------------------------
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(git_origin), str(clone)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    topic = clone / "subjects" / SUBJECT / "topics" / TOPIC
    session = topic / "sessions" / result.session_id
    assert _git(clone, "status", "--porcelain") == ""

    # The transcript: every final the client sent, in order.
    recording = read_recording(FIXTURE)
    finals = [m.text for m in recording.transcript if isinstance(m, TranscriptClientFinal)]
    lines = (session / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert [yaml.safe_load(line)["text"] for line in lines] == finals

    # The page: the processed still, its crop and its transcription, under the book context.
    book = topic / "sources" / "book"
    for name in ("page-001.jpg", "page-001.page.jpg", "page-001.yaml"):
        assert (book / name).is_file(), name
    assert (book / "page-001.md").read_text(encoding="utf-8").startswith("# La célula")

    # The events: capture, transcription and every state op before the end, which closes the log.
    events = list(read_jsonl(session / "events.jsonl", Event))
    kinds = [e.kind for e in events]
    assert kinds[-1] == "session.ended"
    assert kinds.count("transcript.final") == len(finals)
    assert kinds.count(CAPTURE_EVENT_KIND) == 1 and kinds.count(PAGE_TRANSCRIBED_KIND) == 1
    ops = [e.payload for e in events if e.kind == STATE_OP_EVENT_KIND]
    assert {"add_section", "add_pending"} <= {op["op"] for op in ops}
    [transcribed] = [e for e in events if e.kind == PAGE_TRANSCRIBED_KIND]
    page_pending = set(transcribed.payload["pending_ids"])
    assert page_pending and page_pending <= {op.get("pending_id") for op in ops}

    # The pending-review queue: the observer's doubt and the page's unreadable word, both open.
    pending = yaml.safe_load((topic / "review" / "pending.yaml").read_text(encoding="utf-8"))
    texts = [item["text"] for item in pending["items"]]
    assert pending["open_count"] == len(texts) >= 2
    assert "Se nombra el núcleo sin explicar qué hace." in texts
    assert any("citoplasma" in text for text in texts)

    # The notes: what the editor wrote, valid against the pushed topic, and tagged v1.
    notes = (topic / "notes" / "apuntes.md").read_text(encoding="utf-8")
    assert notes == _notes(result.session_id)
    pushed = Vault.open(clone)
    assert validate(notes, source_exists=topic_source_resolver(pushed, SUBJECT, TOPIC)) == []
    assert _git(clone, "ls-remote", "--tags", "origin", f"{SUBJECT}/{TOPIC}/apuntes-v1").strip()
