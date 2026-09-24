# AGENTS.md -- studentassistant

Rules for every agent (human-driven or headless) working in this repository. Read this first,
then the module doc of the module your issue names (`docs/modules/<module>.md`), then any ADR it
links. The product vision is `docs/VISION.md` (Spanish); the development plan is `docs/PLAN.md`;
decisions are in `docs/adr/`.

## What this is

**Student Assistant** turns handwritten class notes (plus textbook pages, PDFs and web pages)
into faithful digital study notes through a **multimodal conversation**: the student shows pages
to an Android phone and talks about them; an Ubuntu PC backend transcribes the voice locally,
stores every source, lets **Claude Sonnet** (the *observer*) understand and classify the session
live, and lets **Claude Opus** (the *tutor-editor*) build and revise the master notes with the
student. Quizzes, flashcards, exams and slides are generated from those notes later.

All content (sources, transcripts, session events, LLM conversations and state, notes, generated
material) lives in a separate private git repository, **the vault**, synced to GitHub, so the
whole state can be restored on another PC by installing the app and pointing it at the vault.

MVP goal: "I sit with my notebook and phone for twenty minutes and end up with digital notes that
really represent what I meant to write."

## How the work is organized

- The tracker is GitHub issues on `titanarq/studentassistant`, driven by the agent OS in
  `agent_os/` (a `git subtree` of `titanarq/agent-os`; **never edit anything under
  `agent_os/`**). Its configuration is `config/agents.yaml`.
- One issue = one unit of work. The issue body is the brief: acceptance criteria, module label,
  budget class (`<!-- budget: <class> -->`). Its parent epic carries the wider context. Stay
  inside what the issue asks for.
- Workers work on a branch in their own worktree and open a PR. **Nobody merges their own work**.
- A task that uncovers something out of scope files a new issue instead of widening its diff.
- Everything in the repository (code, identifiers, comments, commits, docs other than
  `docs/VISION.md`/`docs/PLAN.md`) is English. **Everything the student sees is Spanish**
  (app UI, web UI, prompts' output language, generated notes).

## Repository layout

```text
backend/            Python 3.12 package `studentassistant` (uv project)
  src/studentassistant/<module>/   one subpackage per backend module below
  src/studentassistant/prompts/    versioned prompt files (*.md)
  tests/                           pytest; tests/fixtures/ holds sample sessions
web/                React + Vite + TypeScript review UI, built into the backend's static dir
android/            Gradle project, `:app` (Kotlin, Jetpack Compose)
protocol/           JSON Schemas + example messages of the phone<->backend contract
docs/               VISION, PLAN, adr/, modules/, runbooks/
scripts/            test.sh and agent-OS shims/workarounds
config/             agents.yaml (agent OS) and agent prompt extras
```

## Modules

Each module maps 1:1 to a `module:<name>` label and `docs/modules/<name>.md`:

| module | lives in | owns |
|---|---|---|
| infra | root, `scripts/`, `.github/` | monorepo tooling, CI, test wrapper, install/setup, agent OS config |
| protocol | `protocol/`, `studentassistant.protocol`, `android/.../protocol` | phone<->backend wire contract (WebSocket + REST) |
| android | `android/` | capture app: pairing, session screens, camera, mic streaming, offline spool |
| server | `studentassistant.server` | FastAPI app: pairing/auth, session lifecycle, WebSocket gateway, REST for the web, replay |
| stt | `studentassistant.stt` | local speech-to-text (faster-whisper), VAD segmentation, voice-command grammar |
| vault | `studentassistant.vault` | git-backed content store: layout, writers, commit/push/pull, setup, SQLite index |
| sources | `studentassistant.sources` | source ingestion: capture processing, page transcription, textbook, PDF, web |
| llm | `studentassistant.llm` | Claude client wrapper: model roles, caching, structured outputs, cost ledger, fakes |
| observer | `studentassistant.observer` | Sonnet observer: event-sourced session state, pending-review queue, topic digest |
| editor | `studentassistant.editor` | Opus tutor-editor: master notes with provenance, edit loop, doubts, "why" |
| web | `web/` | review UI: study desk, notes viewer + sources panel, editor chat, pending panel |
| generators | `studentassistant.generators` | outline, quiz, flashcards, exercises/exam, slides |

Dependency direction inside the backend: `server` -> (`observer`, `editor`, `generators`,
`sources`, `stt`) -> (`llm`, `vault`) -> `protocol`/models. `vault` and `llm` never import a
feature module. Feature modules talk to each other only through the session event bus or public
functions listed in their module doc, never through each other's internals.

## Python conventions (backend)

- Python 3.12, managed with `uv` (`backend/pyproject.toml`, `backend/uv.lock`). Add dependencies
  only with `uv add` in `backend/`, and only when the issue needs them.
- FastAPI + uvicorn, Pydantic v2 models for everything that crosses a boundary (API, vault files,
  LLM tool inputs). Type hints everywhere; `ruff` (lint + format) as configured in `pyproject.toml`.
- Async where I/O-bound (WebSocket, LLM calls); CPU/GPU work (Whisper, OpenCV) in worker threads
  or processes, never blocking the event loop.
- Configuration via `studentassistant.config` (TOML at `~/.config/studentassistant/config.toml`
  + `SA_*` env vars). **No model id, path or secret is hard-coded** outside config defaults.
- Claude is called ONLY through `studentassistant.llm` (ADR-0004). No other module imports
  `anthropic`.
- Vault files are written ONLY through `studentassistant.vault` (ADR-0002). No other module runs
  `git` or writes under the vault root directly.
- Tests: pytest, no network, no GPU, no real Claude calls. Use `FakeClaude` (scripted responses),
  `FakeTranscriber` and a temporary vault fixture (`tmp_vault`). Tests needing a GPU or network
  are marked `@pytest.mark.integration` and are not run by the test command.

## TypeScript conventions (web)

- React + Vite + TypeScript (strict), vitest + Testing Library. `npm` with a committed
  `package-lock.json`. No UI framework beyond what `web/package.json` already declares unless
  the issue asks. All user-facing strings Spanish.

## Kotlin / Android conventions

- Kotlin only, JDK 17 toolchain, Gradle Kotlin DSL with a version catalog
  (`android/gradle/libs.versions.toml`); add dependencies ONLY through the catalog.
- `minSdk` 28, `compileSdk`/`targetSdk` 35. Jetpack Compose (Material 3), CameraX,
  coroutines + `Flow`; manual constructor DI through an `AppContainer`; fakes over mocks.
- The app is a thin client: no business logic that belongs to the backend (ADR-0001).
- JVM unit tests (`src/test`) for all logic; instrumented tests are allowed but never run by the
  test command.

## Test command

`scripts/test.sh` runs every suite that exists: backend (`uv run pytest` + `ruff check`), web
(`npm test`), android (`./gradlew test`). Narrow it with `scripts/test.sh backend|web|android
[extra args]`, e.g. `scripts/test.sh backend -k vault`. Full logs land in
`.cache/test-<suite>-last.log`. A suite whose skeleton does not exist yet is skipped with a
message. Every PR must leave `scripts/test.sh` green.

## Never touch

- `agent_os/` (the mechanism; changes go upstream to `titanarq/agent-os`).
- `docs/adr/*`, `docs/VISION.md`, `AGENTS.md`, `config/agents.yaml`, `config/agent_prompts/*` --
  proposals to change them go in the PR description or a new issue; the human edits them.
- `.secrets/`, `.env`, any real vault, API keys, signing keys; never commit credentials or print
  their contents. A test never touches `~/.config/studentassistant` or a real vault.
