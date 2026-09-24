# Student Assistant — Plan de desarrollo

Visión: [`VISION.md`](VISION.md) · Decisiones: [`adr/`](adr/) · Reglas para agentes: [`../AGENTS.md`](../AGENTS.md) · Tablero: https://github.com/orgs/titanarq/projects/3

Revisado con el estudiante el 2026-09-24: una sesión = un tema de una asignatura + purga de contexto y de bóveda; Opus 5.5 como editor; transcripción enchufable, por defecto en el cliente con Google; desarrollo con una **página web de captura en el portátil** (cámara + micro) y la app Android en paralelo, fuera del camino crítico.

## Cómo lo desarrolla agent-os

- Cada tarea es una issue `type:task`, sub-issue de su épica, con la plantilla de agent-os y una clase de presupuesto (`mechanical-qwen` / `complex-qwen`). Las dependencias van como `Blocked by #N` (una tarea no se despacha hasta que sus bloqueantes están cerradas).
- *Stages* está vacía a propósito: el **refiner** la escribe (y divide la tarea si es grande). `refiner_unattended: true`.
- Todas las tareas están en `status:refine` (columna Backlog). Con `auto-ready` en una épica, sus tareas refinadas pasan solas a *Ready for AI*.
- Un worker a la vez (Qwen); Claude valida la PR contra los criterios de aceptación; el humano fusiona.
- Arranque, pausa y monitorización: [`runbooks/operations.md`](runbooks/operations.md). Seguimiento: #1.

## Camino crítico del MVP

M0 (esqueletos backend/web/android, CI, protocolo, bóveda) → M1 (emparejado, API de sesión, gateway WebSocket, subida de capturas, **web de captura**, transcripción, comandos, replay, setup de la bóveda) → M2 (capa Claude, procesado y transcripción de páginas, observador, dudas, resumen, purga de contexto) → M3 (formato de apuntes, generación, conversación de edición, dudas, web de revisión). La app Android (p2) avanza en paralelo cuando hay hueco.

## Hitos

- **M0 Fundaciones** (8 tareas): Monorepo skeletons, CI, phone<->PC contract, vault library.
- **M1 Esqueleto andante** (10 tareas): Phone paired, session, audio -> live transcript, stills stored in the vault and pushed to GitHub. No AI yet.
- **M2 Observador** (9 tareas): Sonnet keeps the session state live, transcribes pages, accumulates doubts.
- **M3 MVP: apuntes maestros** (21 tareas): Opus generates and edits the master notes with provenance; review web. The 20-minute goal.
- **M4 Más fuentes** (12 tareas): Textbook, PDF and web search as sources.
- **M5 Generadores** (7 tareas): Outline, quiz, flashcards, exercises/exam, slides.
- **M6 Modo estudio** (3 tareas): Practice, spaced repetition, voice tutor.

## Épicas y tareas

### #2 Foundations: monorepo, toolchains and CI  ·  `p1` · M0 Fundaciones

Create the three skeletons (backend Python, web React, Android Kotlin) the rest of the backlog builds on, the single test wrapper and CI, so every later task starts from a green, tested tree.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #14 | Backend skeleton: uv project, FastAPI app, config and CLI | infra | p1 | M0 | — |
| #15 | Web skeleton: Vite + React + TypeScript served by the backend | web | p1 | M0 | #14 |
| #16 | Android skeleton: Gradle project with an empty Compose app | android | p1 | M0 | — |
| #17 | CI: backend, web and android jobs with path filters | infra | p1 | M0 | #14 #15 #16 |

### #3 Study vault: all content in a private GitHub repo  ·  `p1` · M0 Fundaciones

Everything the product produces (sources, transcripts, session events, LLM conversations and state, notes, generated material, cost ledger) lives in a separate private git repository synced to GitHub; a new PC restores everything with `studentassistant setup` + clone + index rebuild.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #19 | Vault layout v1: models, init/open and subject/topic files | vault | p1 | M0 | #14 |
| #20 | Vault session store: sessions, append-only JSONL logs and sources | vault | p1 | M0 | #19 |
| #21 | Vault git sync: batched commits, checkpoints, push and pull | vault | p1 | M0 | #20 |
| #22 | `studentassistant setup`: create or clone the vault from GitHub on a new PC | vault | p1 | M1 | #21 |
| #23 | Derived SQLite index with full-text search, rebuildable from the vault | vault | p2 | M3 | #21 |
| #24 | Single active writer between PCs and divergence reporting | vault | p3 | M4 | #21 |
| #31 | Vault purge: retention policy for raw and derived session data | vault | p2 | M3 | #21 #29 |

### #4 Study session and capture clients (web capture page first, Android in parallel)  ·  `p1` · M1 Esqueleto andante

A study session is always about ONE topic of ONE subject (minimal context for the models). Capture clients are thin: during development the laptop's own camera and microphone through a web capture page served by the backend; the Android app follows in parallel over the same protocol. A client picks subject/topic, starts or continues a session, sends transcript segments (or audio, when a server-side STT provider is configured), takes high-resolution stills on demand and shows the live transcript. The backend owns the session lifecycle and a replay harness.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #18 | Capture protocol v1: schemas, examples and models (web, Python, Kotlin) | protocol | p1 | M0 | #14 #15 #16 |
| #27 | Pairing and bearer auth: QR with one-time code, LAN-only | server | p1 | M1 | #18 |
| #32 | Session lifecycle API and the in-process session event bus | server | p1 | M1 | #27 #21 |
| #35 | Session WebSocket gateway: transcript segments or audio in, events both ways, resumable | server | p1 | M1 | #32 |
| #36 | Capture upload endpoint: idempotent bursts stored as sources | server | p1 | M1 | #32 |
| #40 | Web capture page: laptop camera and microphone as the development capture client | web | p1 | M1 | #18 #35 #36 |
| #41 | `studentassistant replay` and `serve --record`: sessions without a phone | server | p1 | M1 | #35 #36 |
| #33 | Android: backend client, pairing by QR and paired-backend settings | android | p2 | M3 | #18 #27 |
| #37 | Android: subjects/topics home and start or continue a session | android | p2 | M3 | #33 #32 |
| #42 | Android capture screen: camera preview, mic streaming and live transcript | android | p2 | M3 | #37 #35 |
| #46 | Android still capture: burst on button or `capture_now`, upload with retries | android | p2 | M3 | #42 #36 |
| #53 | Android offline resilience: disk spool of audio and photos | android | p2 | M3 | #46 |

### #5 Pluggable speech-to-text and voice commands  ·  `p1` · M1 Esqueleto andante

Speech-to-text is a pluggable provider (ADR-0008). Default: transcription on the client with Google's services (browser Web Speech API on the laptop's web capture page, Android SpeechRecognizer on the phone), sent to the backend as timed transcript segments. Server-side providers (local faster-whisper, cloud speech-to-text APIs) plug into the same interface and receive the audio stream instead. Whatever the provider, the backend normalises segments to session time, stores them and detects voice commands deterministically ("mira aquí", "siguiente", "ahora el libro", "ya está, prepárame el tema").

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #25 | STT provider interface: client-side segments and server-side providers | stt | p1 | M1 | #14 |
| #43 | Transcript pipeline: normalise client segments or run a server provider | stt | p1 | M1 | #25 #35 |
| #47 | Voice-command grammar: capture, next page, important, source switch, end | stt | p1 | M1 | #43 |
| #48 | Server-side STT provider: local faster-whisper on the PC GPU | stt | p3 | M4 | #43 |
| #49 | Server-side STT provider: one cloud speech-to-text API | stt | p3 | M4 | #43 |
| #54 | Vocabulary hints for STT from the topic and observer concepts | stt | p3 | M4 | #43 #51 |

### #6 Claude integration layer  ·  `p1` · M2 Observador

One place to talk to Claude: model per role from config (observer/transcriber = Sonnet, editor/generators = Opus), streaming, prompt caching, validated structured outputs, versioned prompts, a cost ledger in the vault and a `FakeClaude` every other module tests against.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #26 | LLM client per role, prompt registry and FakeClaude | llm | p1 | M2 | #14 |
| #28 | Cost ledger per topic and cost caps | llm | p1 | M2 | #26 #20 |

### #7 Observer: Sonnet understands the session live  ·  `p1` · M2 Observador

While the student shows pages and talks, Claude Sonnet acts as fast memory: picks the sharpest photo and transcribes each page, assigns what is said to sections and concepts, links photos with what was said about them, tracks the source context (notes/book) and accumulates a pending-doubts list without interrupting. At the end it writes a topic digest so the topic can be resumed another day.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #44 | Capture processing: sharpest still, downscale, page crop and deskew | sources | p1 | M2 | #36 #20 |
| #50 | Page transcription with Claude vision, using the spoken hints | sources | p1 | M2 | #44 #26 |
| #29 | Observer knowledge state: state ops and a pure fold over session events | observer | p1 | M2 | #20 |
| #51 | Live observer loop with Sonnet: batches in, validated state ops out | observer | p1 | M2 | #29 #26 #43 #28 |
| #55 | Pending-doubts queue: detect, deduplicate and surface without interrupting | observer | p1 | M2 | #51 |
| #56 | Topic digest at session end and "continúa el tema" | observer | p1 | M2 | #51 |
| #60 | Context purge: roll the observer conversation over to snapshot + digest | observer | p1 | M2 | #56 #29 |

### #8 Tutor-editor: Opus builds the master notes  ·  `p1` · M3 MVP: apuntes maestros

On "ya está, prepárame el tema", Claude Opus writes the master notes from everything captured, with provenance on every paragraph; then the student revises them in conversation ("demasiado resumido", "pon un ejemplo", "no inventes", "usa la explicación del libro"), resolves the pending doubts and can ask "¿por qué pusiste esto?" months later.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #30 | Master notes format: anchors, provenance footnotes, fidelity validator | editor | p1 | M3 | #20 |
| #61 | "Prepárame el tema": Opus generates the first version of the notes | editor | p1 | M3 | #30 #56 #50 #55 |
| #63 | Revise the notes in conversation: chat reply plus section edit ops | editor | p1 | M3 | #61 |
| #68 | Resolve pending doubts: auto-resolve with evidence, ask the rest | editor | p1 | M3 | #63 |
| #69 | "¿Por qué pusiste esto?": explain a block from its cited sources | editor | p2 | M3 | #63 |
| #70 | Per-subject style guide learned from revisions | editor | p2 | M4 | #63 |
| #64 | Notes versions: list, diff and restore (git tags) | editor | p2 | M3 | #61 #21 |

### #9 Review web: the study desk  ·  `p1` · M3 MVP: apuntes maestros

A Spanish web UI served by the backend where the student sees the study desk (subjects, topics, sources, sessions, pending doubts, materials), reads the notes with their sources side by side, chats with the editor, resolves doubts and browses versions.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #38 | Read API for the web: study desk, topic, notes, sources, sessions | server | p1 | M3 | #32 #30 |
| #45 | Web study desk: subjects, topics and the topic card | web | p1 | M3 | #15 #38 |
| #52 | Web notes viewer with the sources side panel | web | p1 | M3 | #45 |
| #71 | Web editor chat with streamed replies and applied-edit diffs | web | p1 | M3 | #52 #63 |
| #80 | Web pending-doubts panel and resolution flow | web | p1 | M3 | #52 #68 |
| #72 | Web notes version history and diff view | web | p2 | M3 | #52 #64 |
| #57 | Web live session view | web | p3 | M4 | #45 #51 |

### #10 More sources: textbook, PDF and web search  ·  `p2` · M4 Más fuentes

Beyond handwritten notes: textbook pages shown to the camera, PDFs from the teacher, and "busca esto en Internet" during a session, all stored as first-class sources with provenance, and used by the editor (including contradiction detection between sources).

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #58 | Textbook pages shown to the camera as a separate source | sources | p2 | M4 | #50 #47 |
| #34 | PDF import with page ranges | sources | p2 | M4 | #20 #30 |
| #59 | "Busca esto en Internet": web search results as sources | sources | p2 | M4 | #26 #47 #20 |
| #62 | Add a web page by URL (web UI and Android share) | sources | p3 | M4 | #59 #37 |
| #65 | Editor uses book, PDF and web sources and flags contradictions | editor | p2 | M4 | #61 #58 #34 #59 |

### #11 Quality: end-to-end tests, evals and operations  ·  `p2` · M3 MVP: apuntes maestros

Keep the product trustworthy as it grows: an end-to-end replay test in CI, an eval set from real sessions to measure transcription, observer and editor fidelity, cost controls, and running the backend as a service with a doctor command.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #66 | End-to-end test: replayed session to master notes, in CI | server | p2 | M3 | #41 #61 |
| #39 | Install on a PC: API key, Whisper model, service unit and `doctor` | infra | p2 | M3 | #22 #25 #26 |
| #73 | Eval set from real sessions: transcription, observer and editor fidelity | infra | p3 | M4 | #66 |

### #12 Study materials generators  ·  `p3` · M5 Generadores

From good master notes, generating study material is almost free: outline/mind map, quiz, flashcards (Anki), exercises and mock exams, slides. Every artifact keeps provenance, records the notes version it came from, and is marked stale when the notes change.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #67 | Generator framework: versioned, provenance-keeping artifacts | generators | p3 | M5 | #61 #28 |
| #74 | Outline and mind map generator | generators | p3 | M5 | #67 |
| #75 | Quiz generator and taking the quiz in the web | generators | p3 | M5 | #67 #52 |
| #76 | Flashcards generator with Anki export | generators | p3 | M5 | #67 |
| #77 | Exercises and mock exam generator, printable | generators | p3 | M5 | #67 |
| #78 | Slides generator (Marp) with PDF/PPTX export | generators | p3 | M5 | #67 |
| #79 | Web: generate, preview and download study materials | web | p3 | M5 | #67 #45 |

### #13 Study mode and voice tutor  ·  `p4` · M6 Modo estudio

Study with the system: practice quizzes and flashcards with spaced repetition (history in the vault), ask the tutor questions grounded in the notes by voice from the phone, read the notes on the phone.

| # | tarea | módulo | prio | hito | bloqueada por |
|---|---|---|---|---|---|
| #81 | Practice with spaced repetition over quizzes and flashcards | generators | p4 | M6 | #75 #76 |
| #82 | Voice tutor on the phone grounded in the notes | editor | p4 | M6 | #69 #40 |
| #83 | Read the notes and chat with the editor from the phone | android | p4 | M6 | #71 #37 |
