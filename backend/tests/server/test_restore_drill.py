"""The restore drill: a vault cloned on a "fresh PC" gives back everything the first PC had.

ADR-0002 promises that a new PC restores the whole state with install -> `studentassistant setup`
(clone) -> index rebuild. This test proves it end to end, with no network and no real Claude:

1. **Build** a vault through the public paths only: the sample recording replayed through
   `create_app` (session WebSocket, capture upload, page transcriber, live observer), "prepárame
   el tema" (`notes/generate`) and one editor chat revision turn (`notes/chat`); then the
   Construir and Estudiar state (#370): a pasted image saved into the notes (`sources/images`,
   `PUT notes`), a second session of two blank captures the triage sets aside, one typed
   workspace message that sets the book page aside and restores one blank page, one typed
   message answered by an editor turn, the switch to Estudiar (`POST study`), one written study
   chat question (`POST tutor`), a quiz and flashcards, one quiz result and one practice review,
   and a last save that leaves the materials stale. Claude is `FakeClaude`, scripted per role;
   the app's `GitSync` pushes to a local bare repository that stands in for GitHub, and the
   app's shutdown flushes the last commits.
2. **Restore**: `clone_vault` -- the code `setup --clone` runs -- clones that repository into a
   fresh directory with `LocalHost` as the GitHub host, and its `post_clone` rebuilds a fresh
   SQLite index, as the CLI's does (`SA_CONFIG` stays inside `tmp_path`: nothing touches
   `~/.config`).
3. **Compare** the original and the restored vault: the study desk (the subjects and topics
   listings, through the REST API and the vault), the observer fold per topic
   (`load_observer_snapshot`), `apuntes.md` and its version tags, the pending doubts, the cost
   ledger totals and full-text search results; then, over REST, the workspace chat, the sources
   with their triage state and reason (and every source's meta and bytes, the pasted image
   included), the study label and option states, the study chat history, the materials and their
   staleness, the quiz results, the topic's practice queue and the practice summary.

Every wait is bounded; the whole drill takes a few seconds.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from github_fakes import LocalHost, bare_repo
from studentassistant.config import (
    EditorSettings,
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
from studentassistant.generators.flashcards import TOOL_NAME as FLASHCARDS_TOOL
from studentassistant.generators.quiz import TOOL_NAME as QUIZ_TOOL
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse, Usage
from studentassistant.observer import load_observer_snapshot
from studentassistant.observer.live import TOOL_NAME
from studentassistant.observer.requests import TOOL_NAME as REQUESTS_TOOL
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import read_recording
from studentassistant.server.replay import (
    AsgiTransport,
    ReplayResult,
    Response,
    encode_multipart,
    replay,
)
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
from triage_images import paper

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"
SUBJECT, TOPIC = "biologia", "la-celula"
REPO = "estudiante/vault"
LOCAL_BASE_URL = "http://localhost:8765"
PAGE_TRANSCRIPTION = "# La célula\n\n- membrana, [[?citoplasma]] y núcleo\n"
REVISION = "Robert Hooke describió la célula en 1665.[^t1]"
BASE = f"/api/subjects/{SUBJECT}/topics/{TOPIC}"
BOOK_PAGE = "sources/book/page-001.jpg"  # the sample's one capture, under `switch_source` book
BLANK_PAGE = "sources/notes/page-001.jpg"  # the second session's blank pages
BLANK_ASIDE = "sources/notes/page-002.jpg"
BLANK_CAPTURE_IDS = ["0b7d3c1e-5a2f-4c8d-9e6b-1f2a3b4c5d6e", "4e1a9f2b-7c3d-4b5e-8a6f-9d0c1b2a3e4f"]
CAPTURE_MS = 1_760_000_100_000
LIST_ITEM = "- Membrana, citoplasma y núcleo.[^t2][^p1]"
NUCLEUS = "El núcleo guarda el material genético.[^t2]"
DEFINITION = "La célula es la unidad básica de los seres vivos."
DEFINITION_EDITED = "La célula es la unidad básica de todos los seres vivos."
QUIZ: list[dict[str, Any]] = [
    {
        "type": "multiple_choice",
        "difficulty": "easy",
        "question": "¿Qué envuelve la célula?",
        "options": ["La membrana", "El núcleo", "El citoplasma"],
        "answer": "la membrana",
        "explanation": "La membrana es la parte exterior.",
        "anchors": ["partes"],
    },
    {
        "type": "true_false",
        "difficulty": "medium",
        "question": "La célula es la unidad básica de los seres vivos.",
        "options": [],
        "answer": "verdadero",
        "explanation": "Así empiezan los apuntes.",
        "anchors": ["definicion"],
    },
]
CARDS: list[dict[str, Any]] = [
    {
        "front": "¿Qué es la célula?",
        "back": "La unidad básica de los seres vivos.",
        "anchors": ["definicion"],
    },
    {
        "front": "¿Qué partes tiene?",
        "back": "Membrana, citoplasma y núcleo.",
        "anchors": ["partes"],
    },
]
SEARCHES = ["célula", "nucleo", "membrana", "Hooke", "mitocondria"]
# Generous bounds: they only keep a regression from hanging.
REPLAY_TIMEOUT_S = 30
REQUEST_TIMEOUT_S = 15
GIT_TIMEOUT_S = 30


class ClaudeByRole:
    """A transport handing each request to the `FakeClaude` scripted for its role (the observer,
    the transcriber, the editor and the generators share the app's transport and run
    concurrently). The typed-message classifier runs as the `observer` role too, so a request
    offering its tool goes to the `classifier` fake: the live observer's script stays its own."""

    def __init__(self, **fakes: FakeClaude) -> None:
        self.fakes = fakes

    async def send(self, request: LLMRequest, **options: object) -> LLMResponse:
        tools = {tool.get("name") for tool in request.tools}
        fake = self.fakes["classifier" if REQUESTS_TOOL in tools else request.role]
        return await fake.send(request, **options)  # type: ignore[arg-type]


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


def _scripted_claude() -> tuple[ClaudeByRole, dict[str, FakeClaude]]:
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
    for _ in range(16):  # more batches than the sample and the second session can make
        observer.reply_tool(TOOL_NAME, {"ops": []}, usage=_usage(300))
    fakes = {
        "observer": observer,
        "transcriber": FakeClaude().reply_text(PAGE_TRANSCRIPTION, usage=_usage(2500)),
        "editor": FakeClaude(),
        "classifier": FakeClaude(),
        "generator": FakeClaude(),
    }
    return ClaudeByRole(**fakes), fakes


def _jpeg(image: Any) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _pasted_png() -> bytes:
    image = np.full((24, 32, 3), 200, np.uint8)
    cv2.rectangle(image, (4, 4), (20, 16), (30, 60, 90), -1)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


class Api:
    """The build's REST calls through the running app, each one bounded."""

    def __init__(self, transport: AsgiTransport) -> None:
        self.transport = transport

    async def call(
        self, method: str, path: str, body: Any = None, *, expect: int = 200
    ) -> Response:
        data = None if body is None else json.dumps(body).encode()
        response = await asyncio.wait_for(
            self.transport.request(
                method, path, data, None if body is None else "application/json"
            ),
            REQUEST_TIMEOUT_S,
        )
        assert response.status == expect, (method, path, response.status, response.body)
        return response

    async def multipart(self, path: str, parts: list[tuple[str, str | None, str, bytes]]) -> Any:
        body, content_type = encode_multipart(parts)
        response = await asyncio.wait_for(
            self.transport.request("POST", path, body, content_type), REQUEST_TIMEOUT_S
        )
        assert response.status == 201, (path, response.status, response.body)
        return response.json()

    async def save_notes(self, edit: Callable[[str], str]) -> None:
        notes = (await self.call("GET", f"{BASE}/notes")).json()
        text = edit(notes["text"])
        assert text != notes["text"]
        body = {"text": text, "base_revision": notes["revision"]}
        saved = (await self.call("PUT", f"{BASE}/notes", body)).json()
        assert saved["notes_changed"] is True, saved


def _classified(fakes: dict[str, FakeClaude], *requests: dict[str, Any]) -> None:
    fakes["classifier"].reply_tool(
        REQUESTS_TOOL,
        {"requests": [{"segment_ids": ["m1"], **request} for request in requests]},
        usage=_usage(700),
    )


async def _blank_captures(api: Api) -> None:
    """A second, short capture session of two blank pages: the triage sets both aside."""
    started = await api.call(
        "POST",
        "/api/sessions",
        {"subject_id": SUBJECT, "topic_id": TOPIC, "client_time_ms": CAPTURE_MS},
        expect=201,
    )
    session_id = str(started.json()["session_id"])
    for n, capture_id in enumerate(BLANK_CAPTURE_IDS, start=1):
        at = CAPTURE_MS + 1_000 * n
        image = {
            "part": "image_0",
            "content_type": "image/jpeg",
            "width_px": 1280,
            "height_px": 720,
            "client_time_ms": at,
        }
        metadata = {
            "capture_id": capture_id,
            "trigger": "button",
            "client_time_ms": at,
            "images": [image],
        }
        await api.multipart(
            f"/api/sessions/{session_id}/captures",
            [
                ("metadata", None, "application/json", json.dumps(metadata).encode()),
                ("image_0", "page.jpg", "image/jpeg", _jpeg(paper(seed=6 + n))),
            ],
        )
    ended = {"client_time_ms": CAPTURE_MS + 5_000, "reason": "button"}
    await api.call("POST", f"/api/sessions/{session_id}/end", ended)


async def _build_workspace_and_study(app: FastAPI, api: Api, fakes: dict[str, FakeClaude]) -> None:
    """The Construir and Estudiar state on top of the replayed session, all through REST."""
    editor, consumer = fakes["editor"], app.state.assistant_requests

    # A pasted image, saved into the notes.
    pasted = await api.multipart(
        f"{BASE}/sources/images", [("file", "pegada.png", "image/png", _pasted_png())]
    )
    await api.save_notes(
        lambda text: text.replace(LIST_ITEM, f"{LIST_ITEM}\n{pasted['markdown']}\n")
    )

    # Triage: two blank pages set aside at capture time; then one typed message sets the book
    # page aside and restores the first blank one (the second stays aside, with its reason).
    await _blank_captures(api)
    _classified(
        fakes,
        {"kind": "set_aside", "summary": "Apartar la página del libro", "targets": [BOOK_PAGE]},
        {"kind": "restore", "summary": "Recuperar la página en blanco", "targets": [BLANK_PAGE]},
    )
    typed = {"text": "aparta la del libro y recupera la página en blanco"}
    await api.call("POST", f"{BASE}/workspace/messages", typed, expect=202)
    await asyncio.wait_for(consumer.wait_idle(REQUEST_TIMEOUT_S), REQUEST_TIMEOUT_S)

    # One typed message the editor answers with an edit turn.
    _classified(fakes, {"kind": "edit", "summary": "Añadir qué hace el núcleo"})
    editor.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Añado qué hace el núcleo",
            "ops": [{"op": "insert_after", "section": "partes", "block": 1, "text": NUCLEUS}],
        },
        text="Añado qué hace el núcleo.",
        usage=_usage(5000),
    )
    edit = {"text": "añade que el núcleo guarda el material genético"}
    await api.call("POST", f"{BASE}/workspace/messages", edit, expect=202)
    await asyncio.wait_for(consumer.wait_idle(REQUEST_TIMEOUT_S), REQUEST_TIMEOUT_S)

    # Estudiar: the study version, one written question, a quiz and flashcards, their use.
    await api.call("POST", f"{BASE}/study")
    editor.reply_text("La membrana envuelve la célula [§partes].", usage=_usage(3000))
    question = {"question": "¿Qué hace la membrana?", "style": "written"}
    tutor = (await api.call("POST", f"{BASE}/tutor", question)).body.decode()
    assert "event: result" in tutor, tutor
    fakes["generator"].reply_tool(QUIZ_TOOL, {"questions": QUIZ}, usage=_usage(4000))
    await api.call("POST", f"{BASE}/generated/quiz", {"options": {"size": 2}})
    fakes["generator"].reply_tool(FLASHCARDS_TOOL, {"cards": CARDS}, usage=_usage(3500))
    await api.call("POST", f"{BASE}/generated/flashcards", {})
    quiz = (await api.call("GET", f"{BASE}/quiz")).json()
    attempt = {
        "built_at": quiz["built_at"],
        "answers": [{"question": "q1", "given": "La membrana"}],
        "duration_seconds": 20,
    }
    await api.call("POST", f"{BASE}/quiz/results", attempt)
    queue = (await api.call("GET", f"{BASE}/practice")).json()["queue"]
    review = {"item": queue[0]["item"]["key"], "rating": "good"}
    await api.call("POST", f"{BASE}/practice/reviews", review)

    # One more edit by hand: the materials are now stale.
    await api.save_notes(lambda text: text.replace(DEFINITION, DEFINITION_EDITED))


def _build_original(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, vault: Vault
) -> ReplayResult:
    """Replay, generate, revise, then build the workspace and study state through one running
    app; its shutdown flushes to the remote."""
    claude, fakes = _scripted_claude()
    editor = fakes["editor"]
    app: FastAPI = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=vault,
        sync=GitSync(vault, VaultGitSettings()),
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
        llm_transport=claude,
        # The request detector (#314) would share the observer fake's script.
        # The doubts chat (#325) is left out: it would review the observer's doubts too.
        llm_settings=Settings(
            observer=ObserverSettings(request_detection="off"),
            editor=EditorSettings(doubts_in_chat=False, prepare_mode="single"),
        ),
    )
    base = f"{BASE}/notes"

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
            await _build_workspace_and_study(app, Api(transport), fakes)
            return result

    result = asyncio.run(main())
    notes = read_notes(vault, SUBJECT, TOPIC) or ""
    assert REVISION in notes, "the revision turn was applied"
    assert NUCLEUS in notes and DEFINITION_EDITED in notes, "the workspace turn and the save"
    assert all(fake.pending == 0 for fake in fakes.values() if fake is not fakes["observer"])
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


def _study(client: TestClient) -> dict[str, Any]:
    """The Construir and Estudiar state over REST: the workspace chat, the sources and their
    triage, the pasted image's bytes, the study label, the study chat, the materials and their
    staleness, the quiz results and the spaced-repetition history."""
    state: dict[str, Any] = {}
    for path in (
        "/notes/chat",
        "/sources",
        "/sources/status",
        "/study",
        "/tutor",
        "/generated",
        "/quiz/results",
        "/practice",
    ):
        response = client.get(BASE + path)
        assert response.status_code == 200, (path, response.text)
        state[path] = response.json()
    summary = client.get("/api/practice/summary")
    assert summary.status_code == 200, summary.text
    state["/api/practice/summary"] = summary.json()
    for key in ("/practice", "/api/practice/summary"):
        state[key].pop("now", None)  # the reading's clock, not vault content
    for source in state["/sources"]["sources"]:
        vault_id = source["vault_id"]
        meta = client.get(f"/api/sources/{vault_id}/meta")
        content = client.get(f"/api/sources/{vault_id}")
        assert meta.status_code == content.status_code == 200, vault_id
        state[f"meta:{vault_id}"] = meta.json()
        state[f"bytes:{vault_id}"] = content.content
    return state


def _check_built(state: dict[str, Any]) -> None:
    """The original really holds what the build phase made, so the comparison means something."""
    status = {row["source_id"]: row for row in state["/sources/status"]["sources"]}
    assert status[BOOK_PAGE]["state"] == "apartada", status
    assert status[BLANK_PAGE]["state"] != "apartada", status
    assert status[BLANK_ASIDE]["state"] == "apartada" and status[BLANK_ASIDE]["reason"], status
    images = [key for key in state if key.startswith("bytes:") and "/images/" in key]
    assert [state[key] for key in images] == [_pasted_png()]
    turns = state["/notes/chat"]
    assert "núcleo" in json.dumps(turns, ensure_ascii=False), turns
    study = state["/study"]
    assert study["study_version"] is not None and study["study_current"] is False, study
    assert state["/tutor"]["turns"], state["/tutor"]
    generated = json.dumps(state["/generated"], ensure_ascii=False)
    assert "quiz" in generated and "flashcards" in generated, generated
    assert [result["correct"] for result in state["/quiz/results"]] == [1]
    assert state["/practice"]["counts"]["learned"] == 1, state["/practice"]
    assert state["/api/practice/summary"], state["/api/practice/summary"]


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


def test_a_cloned_vault_restores_the_desk_notes_workspace_and_study_state(
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
    sessions = list_sessions(restored, SUBJECT, TOPIC)
    assert sessions == list_sessions(tmp_vault, SUBJECT, TOPIC)
    replayed = next(session for session in sessions if session.id == result.session_id)
    assert replayed.ended_at is not None

    # -- 4. the same workspace and study state -------------------------------------------------
    original_study = _study(_read_client(server, codes, tmp_path, tmp_vault))
    restored_study = _study(_read_client(server, codes, tmp_path, restored))
    _check_built(original_study)
    for key, value in original_study.items():
        assert restored_study[key] == value, key
