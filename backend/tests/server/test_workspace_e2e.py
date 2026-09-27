"""The workspace path, end to end: spoken requests build the notes, then Estudiar makes a quiz.

One in-process app (`create_app` with its lifespan, as `serve` runs it) gets a small client-mode
recording through `replay` (the session WebSocket and the capture upload) while a client reads the
topic's `GET .../workspace/stream`. The student photographs a page of their notes and says
"incorpora esta página", "haz una tabla con las tres causas" and, after typing a question in the
workspace chat, "ya está, quiero estudiar". Then, on the study screen, "hazme un quiz de 3
preguntas" in the study chat, and a save of their own that leaves the quiz stale.

Claude is `FakeClaude`, scripted per channel (`ClaudeByRole`): the live observer loop, the
request detector and the typed-message classifier (role `observer`, the `report_requests` tool),
the page transcriber, the editor and the generator. No wall clock paces anything: the replay runs
on a virtual clock whose `sleep` (`Pace`) is where the backend settles between recorded steps --
the finals sent so far reached the bus, the pages were transcribed, the request detector examined
what it had not (`RequestDetector.flush`, as a session end does) and the turns it found ran -- so
every detector call sees exactly one new final and the scripts are consumed in a fixed order. The
detector's own timers are set far beyond the test's length.

The capture client ends the session at the end of the recording; here the spoken "ya está, quiero
estudiar" has already ended it (the `study` turn) while the student keeps going (a marker after
it), so the replay stops there and reports the session as ended by the backend (#387), as the web
capture page stops when the chat's `go_study` turn arrives.

The vault's `GitSync` pushes to a local bare repository (`git_origin`) that stands in for GitHub;
after the app's shutdown it holds every commit. Every wait is bounded; the test takes a few
seconds.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from fastapi import FastAPI

from studentassistant.config import (
    EditorSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
    SttSettings,
    VaultGitSettings,
)
from studentassistant.editor.notes_format import notes_revision
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.generators.quiz import TOOL_NAME as QUIZ_TOOL
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.observer.live import TOOL_NAME as OBSERVER_TOOL
from studentassistant.observer.requests import TOOL_NAME as REQUESTS_TOOL
from studentassistant.protocol import (
    CaptureImage,
    CaptureUploadRequest,
    Marker,
    TranscriptClientFinal,
)
from studentassistant.server.app import create_app
from studentassistant.server.bus import Subscription
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import (
    Recording,
    RecordingManifest,
    RecordingWriter,
    read_recording,
)
from studentassistant.server.replay import (
    AsgiTransport,
    ReplayResult,
    replay,
)
from studentassistant.vault import GitSync, Vault, list_sessions

SUBJECT, TOPIC = "historia", "revolucion-francesa"
BASE = f"/api/subjects/{SUBJECT}/topics/{TOPIC}"
STARTED_CLIENT_TIME_MS = 1_770_000_000_000
PROVIDER, LANGUAGE = "web-speech", "es-ES"
CAPTURE_ID = "7a1c9e2b-3d4f-4a5b-8c6d-0e1f2a3b4c5d"
PAGE = "sources/notes/page-001.jpg"
PAGE_FOOTNOTE = "[Apuntes, página 1](../sources/notes/page-001.jpg)"
PAGE_LINES = (
    "Revolución Francesa",
    "",
    "Tres causas:",
    "- crisis económica",
    "- desigualdad social",
    "- ideas ilustradas",
)
PAGE_TRANSCRIPTION = (
    "# Revolución Francesa\n\nTres causas:\n\n"
    "- crisis económica\n- desigualdad social\n- ideas ilustradas\n"
)
# (segment id, offset of its start, offset of its end, final text); a final is sent at its end.
SEGMENTS = (
    ("seg-1", 500, 3_500, "Hoy empezamos la Revolución Francesa, que tuvo tres causas."),
    ("seg-2", 5_000, 7_000, "Incorpora esta página a los apuntes."),
    ("seg-3", 8_000, 10_000, "Haz una tabla con las tres causas."),
    ("seg-4", 12_000, 14_000, "Ya está, quiero estudiar."),
)
CAPTURE_OFFSET_MS = 4_500
TYPED_AT_MS = 14_000
"""The workspace message is typed while the replay waits to send the last final."""
AFTER_STUDY_MS = 16_000
"""A marker the student sets after saying "ya está, quiero estudiar": the session has ended."""
TYPED_QUESTION = "¿Cuál de las tres causas fue la más importante?"
TYPED_ANSWER = "Tus apuntes no las ordenan; la crisis económica fue el detonante."
INCORPORATED = (
    "Tuvo tres causas: la crisis económica, la desigualdad social y las ideas ilustradas.[^p1]"
)
TABLE = (
    "| Causa | Tipo |\n"
    "| --- | --- |\n"
    "| Crisis económica[^p1] | Económica |\n"
    "| Desigualdad social | Social |\n"
    "| Ideas ilustradas | Ideológica |"
)
QUIZ: list[dict[str, Any]] = [
    {
        "type": "multiple_choice",
        "difficulty": "easy",
        "question": "¿Cuántas causas tuvo la Revolución Francesa según los apuntes?",
        "options": ["Tres", "Dos", "Cinco"],
        "answer": "tres",
        "explanation": "Los apuntes nombran tres causas.",
        "anchors": ["causas"],
    },
    {
        "type": "true_false",
        "difficulty": "medium",
        "question": "La desigualdad social es una causa de tipo social.",
        "options": [],
        "answer": "verdadero",
        "explanation": "Así la clasifica la tabla.",
        "anchors": ["tabla-causas"],
    },
    {
        "type": "short_answer",
        "difficulty": "hard",
        "question": "¿Qué causa es de tipo ideológico?",
        "options": [],
        "answer": "Las ideas ilustradas",
        "explanation": "Lo dice la tabla de las causas.",
        "anchors": ["tabla-causas"],
    },
]
SAVED_SENTENCE = "Fue en 1789."
WAIT_S = 15.0
"""Generous bound of every wait: it only keeps a regression from hanging."""
GIT_TIMEOUT_S = 30
CHECKPOINT = re.compile(r"\d+ archivos? cambiados?")
"""The subject of a sync loop's periodic checkpoint."""


# -- the recording ---------------------------------------------------------------------------------


def _page_jpeg() -> bytes:
    """A small JPEG of a white page with `PAGE_LINES` typed on it (never a photo)."""
    document = pymupdf.open()
    try:
        page = document.new_page(width=240, height=320)
        for number, line in enumerate(PAGE_LINES):
            page.insert_text((16, 32 + 22 * number), line, fontsize=14)
        return page.get_pixmap(alpha=False).tobytes("jpeg", jpg_quality=70)
    finally:
        document.close()


@pytest.fixture
def recording(tmp_path: Path) -> Recording:
    """The session: a sentence, a photographed notes page, then three spoken requests."""
    start = STARTED_CLIENT_TIME_MS
    manifest = RecordingManifest(
        format_version=1,
        subject=SUBJECT,
        topic=TOPIC,
        language=LANGUAGE,
        stt_mode="client",
        stt_provider=PROVIDER,
        started_client_time_ms=start,
    )
    with RecordingWriter(tmp_path / "recording", manifest) as writer:
        for segment_id, start_ms, end_ms, text in SEGMENTS:
            writer.append_transcript(
                TranscriptClientFinal(
                    type="transcript.client.final",
                    segment_id=segment_id,
                    client_start_ms=start + start_ms,
                    client_end_ms=start + end_ms,
                    text=text,
                    provider=PROVIDER,
                    language=LANGUAGE,
                )
            )
        writer.append_event(Marker(type="marker", client_time_ms=start + AFTER_STUDY_MS))
        at = start + CAPTURE_OFFSET_MS
        writer.add_capture(
            CaptureUploadRequest(
                capture_id=CAPTURE_ID,
                trigger="button",
                client_time_ms=at,
                images=[
                    CaptureImage(
                        part="image_0",
                        content_type="image/jpeg",
                        width_px=240,
                        height_px=320,
                        client_time_ms=at,
                    )
                ],
            ),
            {"image_0": _page_jpeg()},
        )
    return read_recording(tmp_path / "recording")


# -- Claude ----------------------------------------------------------------------------------------


class ClaudeByRole:
    """Hands each request to the `FakeClaude` of its channel: a request offering the
    `report_requests` tool (the detector's and the typed classifier's calls) to `requests`, any
    other to the fake of its role. The channels run concurrently; each script is consumed in
    order."""

    def __init__(self, **fakes: FakeClaude) -> None:
        self.fakes = fakes

    async def send(self, request: LLMRequest, **options: object) -> LLMResponse:
        tools = {tool.get("name") for tool in request.tools}
        fake = self.fakes["requests" if REQUESTS_TOOL in tools else request.role]
        return await fake.send(request, **options)  # type: ignore[arg-type]


def _reported(*requests: dict[str, Any]) -> dict[str, Any]:
    return {"requests": list(requests)}


def _scripted_claude() -> dict[str, FakeClaude]:
    observer = FakeClaude()
    for _ in range(12):  # more batches than the session can make
        observer.reply_tool(OBSERVER_TOOL, {"ops": []})
    requests = (
        FakeClaude()
        .reply_tool(REQUESTS_TOOL, _reported())  # seg-1: no request
        .reply_tool(
            REQUESTS_TOOL,
            _reported(
                {
                    "kind": "incorporate",
                    "summary": "Incorporar la página 1",
                    "segment_ids": ["seg-2"],
                    "targets": [PAGE],
                }
            ),
        )
        .reply_tool(
            REQUESTS_TOOL,
            _reported(
                {
                    "kind": "edit",
                    "summary": "Hacer una tabla con las tres causas",
                    "segment_ids": ["seg-3"],
                }
            ),
        )
        .reply_tool(  # the typed message, shown as the one segment `m1`
            REQUESTS_TOOL,
            _reported(
                {
                    "kind": "question",
                    "summary": "Qué causa fue la más importante",
                    "segment_ids": ["m1"],
                }
            ),
        )
        .reply_tool(
            REQUESTS_TOOL,
            _reported({"kind": "study", "summary": "Pasar a estudiar", "segment_ids": ["seg-4"]}),
        )
    )
    editor = (
        FakeClaude()
        .reply_tool(
            EDIT_TOOL,
            {
                "summary": "Incorporo la página 1",
                "ops": [
                    {
                        "op": "add_section",
                        "after": "",
                        "level": 2,
                        "title": "Causas",
                        "anchor": "causas",
                        "text": INCORPORATED,
                    }
                ],
                "footnotes": [{"label": "p1", "definition": PAGE_FOOTNOTE}],
            },
            text="He incorporado la página 1.",
        )
        .reply_tool(
            EDIT_TOOL,
            {
                "summary": "Añado una tabla con las tres causas",
                "ops": [
                    {
                        "op": "add_section",
                        "after": "causas",
                        "level": 2,
                        "title": "Tabla de las causas",
                        "anchor": "tabla-causas",
                        "text": TABLE,
                    }
                ],
                "footnotes": [],
            },
            text="He añadido la tabla.",
        )
        .reply_text(TYPED_ANSWER)
    )
    return {
        "observer": observer,
        "requests": requests,
        "transcriber": FakeClaude().reply_text(PAGE_TRANSCRIPTION),
        "editor": editor,
        "generator": FakeClaude().reply_tool(QUIZ_TOOL, {"questions": QUIZ}),
    }


# -- the workspace stream --------------------------------------------------------------------------


class WorkspaceStream:
    """`GET .../workspace/stream` read in the background, as the workspace page reads it."""

    def __init__(self, app: FastAPI, path: str) -> None:
        self.app, self.path = app, path
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._buffer = b""
        self._connected = False
        self._gone = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 50000),
            "server": ("localhost", 80),
            "state": {},
        }

        async def receive() -> dict[str, Any]:
            await self._gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                assert message["status"] == 200, message
            elif message["type"] == "http.response.body":
                self._feed(message.get("body", b""))

        self._task = asyncio.create_task(self.app(scope, receive, send))
        await self.until(lambda: self._connected)

    def _feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n\n", 1)
            text = raw.decode()
            if text.startswith(":"):
                self._connected = True
                continue
            fields = dict(line.split(": ", 1) for line in text.splitlines())
            self.events.append((fields["event"], json.loads(fields["data"])))

    async def until(self, check: Callable[[], bool]) -> None:
        try:
            async with asyncio.timeout(WAIT_S):
                while not check():
                    await asyncio.sleep(0.01)
        except TimeoutError:
            print("the stream so far:", *(e for e in self.events if e[0] != "reply.delta"))
            raise

    def of(self, event: str) -> list[dict[str, Any]]:
        return [data for name, data in self.events if name == event]

    def turns_done(self) -> int:
        return len(self.of("turn.result")) + len(self.of("turn.error"))

    async def close(self) -> None:
        self._gone.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, WAIT_S)


# -- pacing ----------------------------------------------------------------------------------------


class Pace:
    """The replay's virtual clock; its `sleep` lets the backend settle before the next step.

    Settling, before the step due at offset `ms`: every final due before it reached the bus, the
    pages were transcribed, the request detector examined what it had not, and every turn
    expected by then (`turns`: offset -> turns done) has its result on the stream. `actions` run
    after that, before the step (offset -> coroutine factory).
    """

    def __init__(
        self,
        app: FastAPI,
        stream: WorkspaceStream,
        *,
        turns: dict[int, int],
        actions: dict[int, Callable[[], Awaitable[None]]],
    ) -> None:
        self.app, self.stream = app, stream
        self.turns, self.actions = turns, actions
        self.now = 1_000.0
        self.origin: float | None = None
        self.finals: Subscription | None = None
        self.finals_seen = 0

    def clock(self) -> float:
        if self.origin is None:
            self.origin = self.now  # the replay's `started`
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        assert self.origin is not None
        await self.settle(round((self.now - self.origin) * 1000))

    async def settle(self, ms: int) -> None:
        state = self.app.state
        active = state.sessions.active
        if active is not None:
            session_id = active.session_id
            if self.finals is None:
                self.finals = state.bus.subscribe(
                    name="test-finals", session_id=session_id, kinds={"transcript.final"}
                )
            due = sum(1 for _id, _start, end, _text in SEGMENTS if end < ms)
            while self.finals_seen < due:
                await asyncio.wait_for(self.finals.get(), WAIT_S)
                self.finals_seen += 1
            await asyncio.wait_for(state.transcriber.flush(session_id), WAIT_S)
            await asyncio.wait_for(state.requests.flush(session_id), WAIT_S)
        expected = max((n for at, n in self.turns.items() if at <= ms), default=0)
        await self.stream.until(lambda: self.stream.turns_done() >= expected)
        await state.assistant_requests.wait_idle(WAIT_S)
        action = self.actions.pop(ms, None)
        if action is not None:
            await action()


# -- helpers ---------------------------------------------------------------------------------------


async def _call(
    transport: AsgiTransport, method: str, path: str, body: Any = None, *, expect: int = 200
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    response = await asyncio.wait_for(
        transport.request(method, path, data, None if body is None else "application/json"),
        WAIT_S,
    )
    assert response.status == expect, (method, path, response.status, response.body)
    return response


def _sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in body.decode().split("\n\n"):
        if chunk.strip() and not chunk.startswith(":"):
            fields = dict(line.split(": ", 1) for line in chunk.splitlines())
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def _option(study: dict[str, Any], key: str) -> str:
    return next(option["state"] for option in study["options"] if option["key"] == key)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
    ).stdout


def _turn_sequence(events: list[tuple[str, dict[str, Any]]], turn_id: str) -> list[str]:
    """The events of one turn (its `reply.delta`s folded into one), from `turn.started` on."""
    names: list[str] = []
    for name, data in events:
        if data.get("turn_id") != turn_id:
            continue
        if name == "reply.delta" and names and names[-1] == "reply.delta":
            continue
        names.append(name)
    return names


# -- the test --------------------------------------------------------------------------------------


def test_a_replayed_session_builds_the_notes_and_studies_them(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    git_origin: Path,
    recording: Recording,
) -> None:
    fakes = _scripted_claude()
    app: FastAPI = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        sync=GitSync(tmp_vault, VaultGitSettings()),
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
        llm_transport=ClaudeByRole(**fakes),
        llm_settings=Settings(
            # Only `Pace` (and the session end) makes the detector call.
            observer=ObserverSettings(
                request_debounce_seconds=3_600, request_max_wait_seconds=3_600
            ),
            # The doubts chat (#325) would review the topic's doubts after each turn.
            editor=EditorSettings(doubts_in_chat=False),
        ),
    )
    typed: dict[str, Any] = {}

    async def main() -> tuple[ReplayResult, dict[str, Any]]:
        seen: dict[str, Any] = {}
        async with AsgiTransport(app) as transport:  # the lifespan: sync loop, shutdown flush
            await _call(transport, "POST", "/api/subjects", {"name": SUBJECT}, expect=201)
            await _call(
                transport, "POST", f"/api/subjects/{SUBJECT}/topics", {"name": TOPIC}, expect=201
            )
            stream = WorkspaceStream(app, f"{BASE}/workspace/stream")
            await stream.open()

            async def type_message() -> None:
                response = await _call(
                    transport,
                    "POST",
                    f"{BASE}/workspace/messages",
                    {"text": TYPED_QUESTION},
                    expect=202,
                )
                typed.update(response.json())
                await stream.until(lambda: stream.turns_done() >= 3)

            pace = Pace(
                app,
                stream,
                turns={10_000: 1, TYPED_AT_MS: 2, AFTER_STUDY_MS: 4},
                actions={TYPED_AT_MS: type_message},
            )
            result = await asyncio.wait_for(
                replay(recording, transport, sleep=pace.sleep, clock=pace.clock), WAIT_S * 2
            )
            assert result.ended_by_backend, "the spoken study request did not end the session"
            seen["capture"] = list(stream.events)

            # -- the Construir document, the sources and the study screen ------------------------
            seen["notes"] = (await _call(transport, "GET", f"{BASE}/notes")).json()
            seen["status"] = (await _call(transport, "GET", f"{BASE}/sources/status")).json()
            seen["study_after_switch"] = (await _call(transport, "GET", f"{BASE}/study")).json()

            # -- Estudiar: "hazme un quiz de 3 preguntas" in the study chat ---------------------
            tutor = await _call(
                transport,
                "POST",
                f"{BASE}/tutor",
                {"question": "Hazme un quiz de 3 preguntas", "style": "written"},
            )
            seen["tutor"] = _sse(tutor.body)
            seen["study_after_quiz"] = (await _call(transport, "GET", f"{BASE}/study")).json()

            # -- the student edits the notes afterwards -----------------------------------------
            notes = seen["notes"]
            text = notes["text"].replace(INCORPORATED, f"{INCORPORATED} {SAVED_SENTENCE}")
            assert text != notes["text"]
            saved = await _call(
                transport,
                "PUT",
                f"{BASE}/notes",
                {"text": text, "base_revision": notes["revision"]},
            )
            seen["saved"] = saved.json()
            seen["study_after_save"] = (await _call(transport, "GET", f"{BASE}/study")).json()
            await stream.until(lambda: len(stream.of("notes.changed")) >= 3)
            seen["events"] = list(stream.events)
            await stream.close()
        return result, seen

    result, seen = asyncio.run(main())
    session_id = result.session_id
    events: list[tuple[str, dict[str, Any]]] = seen["events"]

    # -- 1-2. the spoken requests on the stream, in order ------------------------------------------
    detected = [data for name, data in seen["capture"] if name == "request.detected"]
    assert [(d["kind"], d["origin"]) for d in detected] == [
        ("incorporate", "voice"),
        ("edit", "voice"),
        ("question", "typed"),
        ("study", "voice"),
    ]
    assert detected[0]["targets"] == [PAGE]
    assert detected[0]["transcript"]["session_id"] == session_id
    assert detected[0]["transcript"]["segment_ids"] == ["seg-2"]
    assert detected[1]["transcript"]["text"] == "Haz una tabla con las tres causas."
    started = [data for name, data in seen["capture"] if name == "turn.started"]
    assert [(s["kind"], s["request_id"]) for s in started] == [
        ("incorporate", detected[0]["request_id"]),
        ("revise", detected[1]["request_id"]),
        ("revise", detected[2]["request_id"]),
        ("study", detected[3]["request_id"]),
    ]
    incorporate, table, question, study = (s["turn_id"] for s in started)
    for turn_id in (incorporate, table):
        assert _turn_sequence(events, turn_id) == [
            "turn.started",
            "reply.delta",
            "turn.result",
            "notes.changed",
        ]
    results = {data["turn_id"]: data for name, data in events if name == "turn.result"}
    assert results[incorporate]["applied"] and results[table]["applied"]
    changed = [data for name, data in events if name == "notes.changed"]
    assert [(c["origin"], c.get("turn_id")) for c in changed[:2]] == [
        ("editor", incorporate),
        ("editor", table),
    ]

    # The document: the incorporated page, cited, and the table section.
    notes = seen["notes"]
    assert INCORPORATED in notes["text"] and PAGE_FOOTNOTE in notes["text"]
    assert "{#tabla-causas}" in notes["text"] and TABLE in notes["text"]
    assert notes["revision"] == changed[1]["revision"] == notes_revision(notes["text"])
    [page] = seen["status"]["sources"]
    assert (page["source_id"], page["state"]) == (PAGE, "incorporada")

    # -- 3. the typed message: classified, answered on the stream, notes untouched -----------------
    [typed_request] = typed["requests"]
    assert typed_request["kind"] == "question" and typed_request["text"] == TYPED_QUESTION
    assert started[2]["origin"] == "typed"
    assert _turn_sequence(events, question) == ["turn.started", "reply.delta", "turn.result"]
    assert results[question]["reply"] == TYPED_ANSWER
    assert results[question]["applied"] is False

    # -- 4. "ya está, quiero estudiar": the session ended, the study version labelled --------------
    go_study = results[study]
    assert go_study["action"] == {
        "kind": "go_study",
        "path": f"/subjects/{SUBJECT}/topics/{TOPIC}/study",
    }
    assert go_study["study"]["ended_session"] == session_id
    assert go_study["reply"].startswith("He cerrado la captura")
    [marked] = [data for name, data in events if name == "study.marked"]
    [meta] = list_sessions(tmp_vault, SUBJECT, TOPIC)
    assert meta.id == session_id and meta.ended_at is not None
    after_switch = seen["study_after_switch"]
    assert after_switch["study_version"]["version"] == marked["version"]
    assert after_switch["study_version"]["tag"] == marked["tag"]
    assert after_switch["study_current"] is True
    assert {option["state"] for option in after_switch["options"]} == {"sin_generar"}

    # -- 5. the study chat generates the quiz ------------------------------------------------------
    assert [name for name, _ in seen["tutor"]] == ["generation.started", "result"]
    generated = seen["tutor"][1][1]
    assert generated["option"] == "quiz" and generated["items"] == 3
    after_quiz = seen["study_after_quiz"]
    assert _option(after_quiz, "quiz") == "listo" and after_quiz["study_current"] is True
    assert after_quiz == generated["study"]

    # -- 6. the student's save: the notes changed, the study version and the quiz stale ------------
    assert seen["saved"]["notes_changed"] is True
    assert changed[-1]["origin"] == "user"
    after_save = seen["study_after_save"]
    assert after_save["study_current"] is False
    assert _option(after_save, "quiz") == "desactualizado"
    assert after_save["study_version"] == after_switch["study_version"]

    # Every script was consumed: nothing asked Claude more (or less) than the path needs.
    for name in ("requests", "transcriber", "editor", "generator"):
        assert fakes[name].pending == 0, name

    # -- 7. the vault: one commit per write, all of them on the "GitHub" after the shutdown --------
    # The sync loop's periodic checkpoints ("2 archivos cambiados") carry what no step commits
    # itself (the subject and topic files, conversation records); every step's write is its own.
    entries = _git(tmp_vault.path, "log", "--reverse", "--format=%H %s", "main").splitlines()
    commits = [tuple(entry.split(" ", 1)) for entry in entries]
    named = [(sha, subject) for sha, subject in commits if not CHECKPOINT.fullmatch(subject)]
    topic = f"{SUBJECT}/{TOPIC}"
    assert [subject for _sha, subject in named] == [
        f"sesión {session_id} iniciada en {app.state.sessions.host}",
        f"Apuntes de {topic}: incorporada la página 1",
        f"Apuntes de {topic} revisados: Añado una tabla con las tres causas",
        f"sesión {session_id} terminada",
        f"Apuntes v1 de {topic}: marcada como versión de estudio",
        f"Generar quiz de {topic} (apuntes v1)",
        f"Apuntes de {topic} editados por el estudiante",
    ]
    # The page was stored by the time it was incorporated.
    incorporation = named[1][0]
    tree = _git(tmp_vault.path, "ls-tree", "-r", "--name-only", incorporation)
    assert f"subjects/{SUBJECT}/topics/{TOPIC}/{PAGE}" in tree.splitlines()
    assert _git(tmp_vault.path, "status", "--porcelain") == ""
    assert _git(git_origin, "rev-parse", "main") == _git(tmp_vault.path, "rev-parse", "HEAD")
    assert _git(git_origin, "tag").split() == [marked["tag"]]
