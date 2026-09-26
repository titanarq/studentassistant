"""The restore drill: a vault cloned on a "fresh PC" gives back everything the first PC had.

ADR-0002 promises that a new PC restores the whole state with install -> `studentassistant setup`
(clone) -> index rebuild. This test proves it end to end, with no network and no real Claude:

1. **Build** a vault through the public paths only: the sample recording replayed through
   `create_app` (session WebSocket, capture upload, page transcriber, live observer), "prepárame
   el tema" (`notes/generate`) and one editor chat revision turn (`notes/chat`). Claude is
   `FakeClaude`, scripted per role; the app's `GitSync` pushes to a local bare repository that
   stands in for GitHub, and the app's shutdown flushes the last commits.
2. **Restore**: `clone_vault` -- the code `setup --clone` runs -- clones that repository into a
   fresh directory with `LocalHost` as the GitHub host, and its `post_clone` rebuilds a fresh
   SQLite index, as the CLI's does (`SA_CONFIG` stays inside `tmp_path`: nothing touches
   `~/.config`).
3. **Compare** the original and the restored vault: the study desk (the subjects and topics
   listings, through the REST API and the vault), the observer fold per topic
   (`load_observer_snapshot`), `apuntes.md` and its version tags, the pending doubts, the cost
   ledger totals and full-text search results.

Every wait is bounded; the whole drill takes a few seconds.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from github_fakes import LocalHost, bare_repo
from studentassistant.config import (
    ObserverSettings,
    ServerSettings,
    Settings,
    SttSettings,
    VaultGitSettings,
)
from studentassistant.editor.contradictions import TOOL_NAME as CONTRADICTIONS_TOOL
from studentassistant.editor.doubts import list_doubts
from studentassistant.editor.notes_format import page_provenance, transcript_provenance
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse, Usage
from studentassistant.observer import load_observer_snapshot
from studentassistant.observer.live import TOOL_NAME
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import read_recording
from studentassistant.server.replay import AsgiTransport, ReplayResult, replay
from studentassistant.vault import (
    GitSync,
    Vault,
    list_sessions,
    list_subjects,
    list_topics,
    read_all_ledgers,
    read_notes,
)
from studentassistant.vault.index import VaultIndex, rebuild_index
from studentassistant.vault.setup import clone_vault

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"
SUBJECT, TOPIC = "biologia", "la-celula"
REPO = "estudiante/vault"
LOCAL_BASE_URL = "http://localhost:8765"
PAGE_TRANSCRIPTION = "# La célula\n\n- membrana, [[?citoplasma]] y núcleo\n"
REVISION = "Robert Hooke describió la célula en 1665.[^t1]"
SEARCHES = ["célula", "nucleo", "membrana", "Hooke", "mitocondria"]
# Generous bounds: they only keep a regression from hanging.
REPLAY_TIMEOUT_S = 30
REQUEST_TIMEOUT_S = 15
GIT_TIMEOUT_S = 30


class ClaudeByRole:
    """A transport handing each request to the `FakeClaude` scripted for its role (the observer,
    the transcriber and the editor share the app's transport and run concurrently)."""

    def __init__(self, **fakes: FakeClaude) -> None:
        self.fakes = fakes

    async def send(self, request: LLMRequest, **options: object) -> LLMResponse:
        return await self.fakes[request.role].send(request, **options)  # type: ignore[arg-type]


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


def _usage(tokens: int) -> Usage:
    return Usage(input_tokens=tokens, output_tokens=tokens // 10)


def _notes(session_id: str) -> str:
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
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
    ).stdout


def _scripted_claude() -> tuple[ClaudeByRole, FakeClaude]:
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
        usage=_usage(1200),
    )
    for _ in range(8):  # more batches than the sample can make
        observer.reply_tool(TOOL_NAME, {"ops": []}, usage=_usage(300))
    transcriber = FakeClaude().reply_text(PAGE_TRANSCRIPTION, usage=_usage(2500))
    editor = FakeClaude()
    return ClaudeByRole(observer=observer, transcriber=transcriber, editor=editor), editor


def _build_original(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, vault: Vault
) -> ReplayResult:
    """Replay, generate and revise through one running app; its shutdown flushes to the remote."""
    claude, editor = _scripted_claude()
    app: FastAPI = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=vault,
        sync=GitSync(vault, VaultGitSettings()),
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
        llm_transport=claude,
        # The request detector (#314) would share the observer fake's script.
        llm_settings=Settings(observer=ObserverSettings(request_detection="off")),
    )
    base = f"/api/subjects/{SUBJECT}/topics/{TOPIC}/notes"

    async def main() -> ReplayResult:
        async with AsgiTransport(app) as transport:  # runs the lifespan: sync loop, shutdown flush
            result = await asyncio.wait_for(
                replay(read_recording(FIXTURE), transport, speed=10, sleep=_no_wait),
                REPLAY_TIMEOUT_S,
            )
            editor.reply_text(_notes(result.session_id), usage=_usage(8000))
            editor.reply_tool(CONTRADICTIONS_TOOL, {"contradictions": []}, usage=_usage(900))
            generated = await asyncio.wait_for(
                transport.request("POST", f"{base}/generate", b"{}", "application/json"),
                REQUEST_TIMEOUT_S,
            )
            assert generated.status == 200, generated.json()
            editor.reply_tool(
                EDIT_TOOL,
                {
                    "summary": "Añado quién describió la célula",
                    "ops": [
                        {
                            "op": "insert_after",
                            "section": "definicion",
                            "block": 1,
                            "text": REVISION,
                        }
                    ],
                },
                text="Añado quién describió la célula.",
                usage=_usage(6000),
            )
            message = json.dumps({"message": "Di quién describió la célula"}).encode()
            revised = await asyncio.wait_for(
                transport.request("POST", f"{base}/chat", message, "application/json"),
                REQUEST_TIMEOUT_S,
            )
            assert revised.status == 200
            return result

    result = asyncio.run(main())
    assert REVISION in (read_notes(vault, SUBJECT, TOPIC) or ""), "the revision turn was applied"
    return result


def _desk(client: TestClient) -> dict[str, Any]:
    """What the study desk reads over REST: the subjects, each one's topics, each topic's view."""
    desk: dict[str, Any] = {}
    subjects = client.get("/api/subjects").json()
    desk["subjects"] = subjects
    for subject in subjects["subjects"]:
        slug = subject["subject_id"]
        topics = client.get(f"/api/subjects/{slug}/topics").json()
        desk[slug] = topics
        for topic in topics["topics"]:
            base = f"/api/subjects/{slug}/topics/{topic['topic_id']}"
            for path in ("/summary", "/sessions", "/doubts", "/notes/versions", "/notes/chat"):
                response = client.get(base + path)
                assert response.status_code == 200, (path, response.text)
                desk[base + path] = response.json()
    return desk


def _read_client(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, vault: Vault
) -> TestClient:
    # No lifespan (no `with`): reading must not pull, commit or push anything.
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=vault,
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


def _per_topic(vault: Vault, read: Callable[[Vault, str, str], Any]) -> dict[str, Any]:
    return {
        f"{subject.slug}/{topic.slug}": read(vault, subject.slug, topic.slug)
        for subject in list_subjects(vault)
        for topic in list_topics(vault, subject.slug)
    }


def _ledger_totals(vault: Vault) -> dict[str, Any]:
    entries = list(read_all_ledgers(vault))
    return {
        "calls": len(entries),
        "roles": sorted({entry.role for entry in entries}),
        "input_tokens": sum(entry.input_tokens for entry in entries),
        "output_tokens": sum(entry.output_tokens for entry in entries),
        "usd": round(sum(entry.estimated_usd or 0.0 for entry in entries), 9),
    }


def _searches(index: VaultIndex) -> dict[str, list[Any]]:
    return {query: index.search(query, limit=50) for query in SEARCHES}


def test_a_cloned_vault_restores_the_desk_state_notes_and_search(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
) -> None:
    github = LocalHost(tmp_path / "github")
    bare_repo(github.root, REPO)
    _git(tmp_vault.path, "remote", "add", "origin", github.remote_url(REPO))

    # -- 1. the first PC: a replayed session, notes generation and one revision, pushed --------
    result = _build_original(server, codes, tmp_path, tmp_vault)
    assert _git(tmp_vault.path, "status", "--porcelain") == "", "everything was committed"
    assert _git(tmp_vault.path, "rev-list", "--count", "origin/main..main").strip() == "0"

    # -- 2. the fresh PC: `setup --clone`'s path, then the index rebuild it runs ---------------
    fresh = tmp_path / "fresh-pc"
    restored_index_path = fresh / "cache" / "index.sqlite3"
    rebuilt: list[Vault] = []

    def post_clone(vault: Vault) -> None:
        rebuild_index(vault, restored_index_path)
        rebuilt.append(vault)

    setup = clone_vault(fresh / "vault", REPO, github, post_clone=post_clone, timeout=GIT_TIMEOUT_S)
    assert setup.action == "cloned" and len(rebuilt) == 1
    restored = setup.vault
    assert restored.path != tmp_vault.path
    assert _git(restored.path, "rev-parse", "HEAD") == _git(tmp_vault.path, "rev-parse", "HEAD")

    # -- 3. the same desk, state, notes, doubts, costs and search -----------------------------
    original_desk = _desk(_read_client(server, codes, tmp_path, tmp_vault))
    restored_desk = _desk(_read_client(server, codes, tmp_path, restored))
    assert [s["subject_id"] for s in original_desk["subjects"]["subjects"]] == [SUBJECT]
    assert restored_desk == original_desk

    assert list_subjects(restored) == list_subjects(tmp_vault)
    assert _per_topic(restored, lambda v, s, _t: list_topics(v, s)) == _per_topic(
        tmp_vault, lambda v, s, _t: list_topics(v, s)
    )

    def fold(vault: Vault, subject: str, topic: str) -> Any:
        return load_observer_snapshot(vault, subject, topic, write_back=False)

    original_fold = _per_topic(tmp_vault, fold)
    assert original_fold[f"{SUBJECT}/{TOPIC}"].state.pending, "the drill has pending items"
    assert _per_topic(restored, fold) == original_fold

    original_notes = read_notes(tmp_vault, SUBJECT, TOPIC)
    assert original_notes is not None and REVISION in original_notes
    assert read_notes(restored, SUBJECT, TOPIC) == original_notes
    original_tags = GitSync(tmp_vault).list_notes_tags(SUBJECT, TOPIC)
    assert [tag.version for tag in original_tags][:1] == [1]
    assert GitSync(restored).list_notes_tags(SUBJECT, TOPIC) == original_tags

    original_doubts = list_doubts(tmp_vault, SUBJECT, TOPIC)
    assert original_doubts.open_count >= 2
    assert list_doubts(restored, SUBJECT, TOPIC) == original_doubts

    original_costs = _ledger_totals(tmp_vault)
    assert original_costs["calls"] >= 4 and original_costs["input_tokens"] > 0
    assert _ledger_totals(restored) == original_costs

    with (
        VaultIndex.open(tmp_vault, tmp_path / "first-pc-index.sqlite3") as original_index,
        VaultIndex.open(restored, restored_index_path) as restored_index,
    ):
        assert restored_index.is_current(), "the clone's rebuilt index is used as it is"
        original_hits = _searches(original_index)
        assert all(original_hits[query] for query in ("célula", "nucleo", "Hooke"))
        assert any(hit.kind == "transcript" for hit in original_hits["célula"])
        assert _searches(restored_index) == original_hits
        assert restored_index.note_versions() == original_index.note_versions()
        assert restored_index.pending() == original_index.pending()
    [session] = list_sessions(restored, SUBJECT, TOPIC)
    assert session.id == result.session_id and session.ended_at is not None
