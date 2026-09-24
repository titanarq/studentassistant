# Module: infra

**Lives in:** repository root, `scripts/`, `.github/workflows/`, `backend/pyproject.toml` tooling
sections, `web/package.json` scripts, `android/` Gradle wrapper and root build.

## Responsibility
- Monorepo skeletons (backend, web, android) and the single test wrapper `scripts/test.sh`.
- CI (`.github/workflows/ci.yml`): one job per suite, path-filtered, plus the agent-OS job; GitHub-hosted
  `runs-on: ubuntu-latest` only, never self-hosted (public repo, see `docs/runbooks/operations.md`).
- Install/operate the app on a PC: `studentassistant setup`/`doctor` wiring, systemd `--user`
  unit for `studentassistant serve`, Whisper model download.

## Boundaries
- `config/agents.yaml`, `config/agent_prompts/*` and `agent_os/` are the human's / mechanism's.
- No product logic here.

## Tests
`scripts/test.sh` itself; CI green on a PR touching each suite.
