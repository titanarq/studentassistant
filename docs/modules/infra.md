# Module: infra

**Lives in:** repository root, `scripts/`, `.github/workflows/`, `backend/pyproject.toml` tooling
sections, `web/package.json` scripts, `android/` Gradle wrapper and root build.

## Responsibility
- Monorepo skeletons (backend, web, android) and the single test wrapper `scripts/test.sh`.
- CI (`.github/workflows/ci.yml`): one job per suite, path-filtered, plus the agent-OS job; GitHub-hosted
  `runs-on: ubuntu-latest` only, never self-hosted (public repo, see `docs/runbooks/operations.md`).
- Install/operate the app on a PC: `studentassistant setup`/`doctor` wiring, systemd `--user`
  unit for `studentassistant serve`, Whisper model download.

## Public surface (backend skeleton)
What `backend/` (ADR-0007) exposes today; feature modules add to it, they do not replace it.
Source layout `backend/src/studentassistant/`, one subpackage per module in AGENTS.md with a
one-line docstring; `__version__` in `studentassistant/__init__.py` is the single source of the
package version (`[tool.hatch.version]`). Dependencies go in with `uv add` inside `backend/`, and
`backend/uv.lock` is committed.

### `studentassistant` CLI
`studentassistant/cli.py` (typer), exposed by `[project.scripts]` in `backend/pyproject.toml` as
the `studentassistant` console script.
- `studentassistant serve` -- runs `create_app()` under uvicorn on the configured host and port,
  until interrupted.
- `studentassistant version` -- prints `studentassistant.__version__`.
- A flag never duplicates a configuration value: where the server listens comes from `Settings()`,
  so there is one way to configure the backend and not two.
- Not built yet: `setup` and `doctor` (this module), `replay` (server), `purge` (vault).

### `create_app()`
`studentassistant/server/app.py`. A factory, not a module-level `app`: uvicorn, the CLI and every
test get an instance of their own, and nothing is imported -- therefore nothing registered or
connected -- until somebody asks for an app. It serves `GET /api/health`, which answers
the protocol v1 `rest.health.response` (`status`, `protocol_version`, `server_time_ms`); every
other route of the phone<->backend contract lands there later.

### Configuration
`studentassistant/config.py`: `Settings()` (pydantic-settings) reads the TOML at
`~/.config/studentassistant/config.toml`, and `SA_CONFIG` points it at another file -- which is
what the tests do, so no test ever touches `~/.config/studentassistant` or a real vault. On top of
the file, every field is also an environment variable with the `SA_*` prefix, nesting levels
separated by `__` (`SA_SERVER__PORT=9000`, `SA_LLM__ROLES__EDITOR__MODEL=claude-opus-5-5`). The
environment always wins over the file.

The defaults live here and nowhere else:

| setting | default |
|---|---|
| `server.host` | `0.0.0.0`: binds the LAN interfaces so the phone and the web page can reach it (ADR-0001) |
| `server.port` | `8765` |
| `vault.path` | `~/StudentAssistant/vault`, `~` expanded (ADR-0002) |
| `llm.roles.observer.model`, `llm.roles.transcriber.model` | `claude-sonnet-5` (ADR-0004) |
| `llm.roles.editor.model`, `llm.roles.generator.model` | `claude-opus-5-5` (ADR-0004) |

## CI (`.github/workflows/ci.yml`)
Triggered on every `pull_request` with no `paths` filter, top-level `permissions: contents: read`.
Every job is `runs-on: ubuntu-latest` (GitHub-hosted); never `self-hosted` under any label -- the
repo is public (`docs/runbooks/operations.md`, "CI runners: GitHub-hosted only").

| job | runs when | setup | command |
|---|---|---|---|
| `changes` | always | `actions/checkout` with `fetch-depth: 0` | `git diff --name-only <PR base sha> <github.sha>` -> outputs `backend`, `web`, `android` |
| `backend` | `backend` output is `true` | `astral-sh/setup-uv`, Python 3.12 | `bash scripts/test.sh backend` |
| `web` | `web` output is `true` | `actions/setup-node`, Node 22 | `bash scripts/test.sh web`, then `npm run build` in `web/` if `web/package.json` exists |
| `android` | `android` output is `true` | `actions/setup-java` JDK 17 (temurin); the image's preinstalled SDK (`$ANDROID_HOME`) | `bash scripts/test.sh android test -Dorg.gradle.workers.max=2` |
| `ci` | always (`if: always()`), needs all four | -- | fails if any needed job is `failure`/`cancelled`; `skipped` is fine |

Path filters (plain shell in the `changes` job, no third-party action): `backend/**` -> backend;
`web/**` -> web; `android/**` -> android; `protocol/**` -> backend, web and android;
`scripts/test.sh` and `.github/workflows/ci.yml` -> all three. Each suite job ends with an
`if: failure()` step that prints `.cache/test-<suite>-last.log`. A suite whose skeleton does not
exist yet is skipped by `scripts/test.sh` and its job ends green. `ci` is the one check every PR
always gets, including PRs that touch no suite. `.github/workflows/ci-agent-os.yml` is the
mechanism's own, separate, path-filtered workflow.

## Boundaries
- `config/agents.yaml`, `config/agent_prompts/*` and `agent_os/` are the human's / mechanism's.
- No product logic here.

## Tests
`scripts/test.sh` itself; CI green on a PR touching each suite.
- backend: inside `backend/`, everything through `uv run --frozen` -- `ruff check .`,
  `ruff format --check .`, then `pytest -q -m "not integration"`. The `integration` marker is
  registered in `backend/pyproject.toml` and the wrapper never runs it (GPU, network, real Claude).
- A suite whose skeleton does not exist yet is skipped with a message; full logs land in
  `.cache/test-<suite>-last.log`, so read that instead of re-running.
