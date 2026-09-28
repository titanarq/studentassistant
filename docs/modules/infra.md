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
- `studentassistant setup` -- the vault part is the vault module's (`docs/modules/vault.md`);
  after it this module adds, in order: the Anthropic API key (`--api-key-stdin`, else a hidden
  Spanish prompt; skipped when already stored or in `ANTHROPIC_API_KEY`, and when unattended;
  never asked with `[llm] backend = "claude-code"`, and skipping it under `auto` says Claude Code
  will be used),
  the STT provider (the faster-whisper model download, only when it is selected) and the systemd
  unit (`--service/--no-service`; asked when interactive, installed when unattended). Any step
  that fails makes it exit 1 after the others ran.
- `studentassistant doctor [--api-call] [--fix]` -- one line per check, `[ok]`/`[aviso]`/`[FALLO]`,
  exit 1 on any `FALLO` (list below). `--api-call` is the opt-in free API call; `--fix` first
  writes the `gh` credential helper to the vault's `.git/config` (#308).
- `studentassistant purge [--topic s/t] [--dry-run] [--hard] [--yes]` -- the vault's retention
  policy (`[vault.purge]`) per accepted topic: `--dry-run` lists what would go and the space it
  frees, a plain run commits the purge (recoverable from history), `--hard` also rewrites history
  and force-pushes with leases after asking for `reescribir` (or `--yes`). Behaviour and the
  consequence for other PCs: `docs/modules/vault.md`, "Purge".
- `studentassistant vault stats [--top N] [--json]` -- the vault's size by category, per subject
  and topic, the N largest files (default 10) and git's object store, as a Spanish table or JSON
  (`vault.stats`, `docs/modules/vault.md`, "Size report").
- Not built yet: `replay` (server).
- `studentassistant eval run [--case NAME]... [--yes]` -- the eval set (below, "Evals"): prints
  the estimated cost per case and role, asks before any Claude call (`--yes` does not), runs
  every case and writes the report, compared with the previous run; exit 1 when a case could
  not be replayed (regressions are reported, never fatal).
- `studentassistant eval compare <run-a> <run-b>` -- prints the comparison ("Evals",
  "Comparison") of two existing runs, each a name under `[eval] path`'s `runs/` or a directory;
  never calls Claude. Exit 1 when either report cannot be read.
- `studentassistant eval import-session <subject> <topic> <session_id> [--out DIR]` -- turns a
  session of the vault into a new eval case ("Evals", "Import") under `[eval] path` (default
  directory `<subject>-<topic>-<session_id>`); only reads the vault. Exit 1 when the session is
  unknown, the output exists, or the output is inside the vault (or holds it) or the checkout.

### Install (`studentassistant/install/`)
The PC-side pieces `setup`, `serve` and `doctor` use. Runbook (Spanish): `docs/runbooks/install.md`.
- `apikey.py` -- the key file `llm.api_key_path()` (`llm.api_key_file`, default `secrets.env`
  next to the config file): `KEY=value` lines, mode `0600`, also a valid systemd
  `EnvironmentFile`. `store_api_key(path, key) -> bool` (keeps other lines, idempotent),
  `read_api_key(path)`, `export_api_key(path, environ=None) -> bool` (sets
  `ANTHROPIC_API_KEY` only when the environment has none; `serve` calls it before uvicorn),
  `file_is_private(path)`. Never in the vault or `config.toml`; nothing prints the key.
- `service.py` -- `studentassistant.service` in `$XDG_CONFIG_HOME/systemd/user`
  (`~/.config/systemd/user`): `ExecStart=<venv>/bin/studentassistant serve`,
  `Environment=SA_CONFIG=<absolute config path>`, `Restart=on-failure`,
  `TimeoutStopSec=300` (`STOP_TIMEOUT_SECONDS`, #408: above the shutdown's bounded waits plus
  the final commit and push, so systemd never SIGKILLs `serve` before it pushed),
  `WantedBy=default.target`. `install_unit(executable, config_path)` writes it when it differs,
  `daemon-reload`, `enable --now`, and `try-restart` when an existing unit changed.
  `service_state()` is `systemctl --user is-active`. Every call goes through `run_systemctl`,
  which `tests/conftest.py` replaces for every test (autouse `systemctl` fixture, a
  `FakeSystemctl`) together with `unit_directory`, so no test touches the machine's systemd.
- `whisper.py` -- only when `stt.mode = "server"` and `stt.provider = "faster-whisper"`: reads
  `[stt.options.faster-whisper]` `model` (default `DEFAULT_WHISPER_MODEL`, `large-v3-turbo`),
  `device` (`auto`|`cuda`|`cpu`, default `auto`) and `download_root` (default: the Hugging Face
  cache); `download` / `cached_model` via `faster_whisper.download_model`, `cuda_devices()` via
  `ctranslate2.get_cuda_device_count()`, `cuda_libraries_error()` via `stt.cuda.check()` (Spanish
  reason, with `CUDA_LIBRARIES_HINT` as the fix). Both packages, and the CUDA 12 cuBLAS/cuDNN 9
  wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, Linux), come with the optional `whisper`
  extra (`uv sync --extra whisper`; CI and `scripts/test.sh` never install it) and are imported
  or loaded lazily; the future faster-whisper provider (stt module) should read the same option keys.
- `doctor.py` -- `run_doctor(settings, *, api_call=False, probes=None, fix=False) -> list[Check]`; every
  outside reach (GitHub host, the key check, the port, the running backend, the environment) is
  a `DoctorProbes` field. Checks, in order: Python dependencies (the distribution's
  requirements installed); STT mode/provider (server mode: the provider resolves in the
  registry; with faster-whisper also installed, CUDA unless `device = "cpu"` -- a visible GPU
  plus `whisper.cuda_libraries_error()` (`stt.cuda.check`: cuBLAS/cuDNN load and create a handle);
  `aviso` for `auto` without a GPU or with a broken library, `fallo` for `cuda` -- and the model
  cached); how Claude is reached (`llm.resolve_backend` with the probes' `environ` and
  `ant_profile`): for `claude-code`, the `Claude Code` line from the `claude_code` probe
  (`llm.check_claude_code`: the executable on PATH and `claude auth status --json` saying
  `loggedIn`, one subprocess, no model call), else the API key (in the environment, in a `0600` key
  file or, failing both, an `ant auth` profile found by `llm.find_ant_profile` through the
  `ant_profile` probe; the line names the source; with `api_call`, `llm.check_api_key` with the
  key, or with none so the SDK resolves the profile); the vault opens; `origin` is `vault.repo`
  and `git push --dry-run` succeeds (`vault.setup.check_remote_access`; no GitHub credentials
  means git's own); `git ls-remote origin` works the way the service runs git (`Acceso del
  servicio a GitHub`: `vault.credentials.probe_unattended_access`, no host environment, no prompt
  or askpass, minimal `PATH`; skipped when `origin` is not `vault.repo`), preceded with `fix` by
  `Credenciales del vault` (`vault.setup.ensure_credential_helper`; `aviso` without `gh`); the vault's size (`check_vault_size`: working tree plus git object store from
  `vault.stats.vault_stats`; `aviso` over `vault.size_warning_mb`, naming the three largest
  categories and pointing at `studentassistant purge` and the Git LFS question); the server port is free or answered by our `/api/health`; the service is
  `active`; Marp CLI (`check_marp`: `generators.marp_command[0]` found on the `environ` probe's
  `PATH`, `<command> --version` within 30 s gives the version; missing or failing is an `aviso`
  with the `npm install -g @marp-team/marp-cli` hint, since only the slides' PDF/PPTX export
  needs it). The CLI adds a first `Configuración` line (an invalid config is a `FALLO`).

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
| `vault.size_warning_mb` | `1024`: `doctor` warns once the vault (files + git objects) is bigger |
| `llm.roles.observer.model`, `llm.roles.transcriber.model` | `claude-sonnet-5` (ADR-0004) |
| `llm.roles.editor.model`, `llm.roles.generator.model` | `claude-opus-5-5` (ADR-0004) |
| `llm.roles.observer.turn_timeout_seconds`, `llm.roles.observer.max_attempts` | `90` s, `2`: a hung observer / request-detection call cannot stall a live session (#409) |
| `llm.roles.<transcriber\|editor\|generator>.turn_timeout_seconds`, `.max_attempts` | unset: the backend's turn timeout (`llm.claude_code.turn_timeout_seconds`, `600`) and `llm.max_attempts` (`4`); see `docs/modules/llm.md` |
| `eval.path` | `~/StudentAssistant/evals`: the eval set, outside the code repo and the vault |
| `eval.speed` | `4.0`: how many times faster than recorded `eval run` replays each session |
| `eval.regression_margin` | `0.05`: a score dropping more than this against the previous run is a regression |
| `eval.request_detection` | unset: the request detector under test, replacing `observer.request_detection` for the run (`observer`, `wake_word`, `off`) |
| `eval.notes_path` | `generate`: the notes scored come from "prepárame el tema" after the replay; `chat`: from the session's own requests |

## Evals (`studentassistant/evals/`)
A small set of the student's real recorded sessions with reference notes, scored whenever prompts
or models change. Student-facing guide (Spanish): `docs/runbooks/evaluacion.md`.

- **Layout** (`cases.py`, `read_eval_set` / `read_case`, `EvalSetError`): `[eval] path` holds one
  directory per case -- `recording/` (a `serve --record` recording, `server/recording.py`) and
  `reference/notes.md` (required), `reference/pages/<capture_id>.md` and
  `reference/sections.yaml` (`sections: [{title, segments: [<segment_id>]}]`) and
  `reference/requests.yaml` (`requests: [{kind, segments: [<segment_id>], note?}]`, `kind` one of
  `observer.REQUEST_KINDS`) and `reference/triage.yaml` (`captures: [{capture_id, status,
  reasons?, note?}]`, `status` kept/flagged/set_aside, `reasons` among
  `sources.triage.TRIAGE_REASONS`; a capture the recording lacks or one listed twice is an
  `EvalSetError`), all optional -- plus `runs/`. HTML comments are removed from every Markdown
  reference on read; a reference whose first line carries `cases.DRAFT_MARKER` ("borrador sin
  corregir", an uncorrected import stub) is listed in `EvalCase.drafts` and the report warns about it. Every reference is checked against its
  recording before anything runs (an unknown segment or kind is an `EvalSetError`). `eval run`
  refuses a path inside the vault (or holding it) or inside the source checkout it runs from.
- **Import** (`import_session.py`, `import_session` / `SessionImportError`): rebuilds a
  client-mode recording from a vault session through the public vault API only
  (`list_sessions`, `read_session_transcript`, `read_topic_events`, `read_source`, `get_topic`;
  nothing written into the vault). Start time: `session.started`'s `client_time_ms`, else
  `started_at`. One `transcript.client.final` per `transcript.jsonl` segment (start + `t_start` /
  `t_end`), with the session's own `segment_id` when a `transcript.final` event matches its
  `t_start` and text (else `seg-<seq>`), and that event's provider and language (the manifest's
  too; else `replay` / `[stt] language`). `button`/`marker` events at start + `t`. One burst per
  `capture.stored` event of a `notes`/`book` page, at start + the sidecar's `session_t_ms`: the
  kept still at `selected_image` plus every `page-NNN.burst<K>.*` still left, in burst order; a
  capture that cannot be rebuilt is skipped and reported. Reference stubs, each marked
  `DRAFT_MARKER`: `notes.md` (only the topic title), `pages/<capture_id>.md` (the stored page
  transcription, when there is one) and `triage.yaml` (every capture with the triage its sidecar
  holds, a comment naming its page and session time).
- **Cost first** (`estimate.py`, `estimate_case`): calls and tokens per role from the recording
  alone (constants in the module), priced with `[llm.prices]` at the uncached input price; a
  model with no price is named and left out of the total. The request detector (`observer-requests`,
  only when the run's detector is `observer`) is one call per final; with `notes_path = "chat"` the
  editor is one incorporation/edit turn per editor-kind request of `requests.yaml` (without it,
  one per `[editor] incorporate_batch_size` pages) instead of the one generation. The real cost is summed from the
  run vault's ledgers and reported next to it.
- **Run** (`run.py`, `run_eval` / `run_case`): each case is wired as
  `tests/server/test_pipeline_e2e.py` wires the pipeline -- `create_app` with its lifespan over a
  fresh vault under `runs/<UTC time>/<case>/vault` (no remote; index, devices, recordings also
  under the run directory), `replay` at `[eval] speed`, then `POST .../notes/generate` with
  `confirm_over_cap` (the estimate was already confirmed) -- or, with `[eval] notes_path =
  "chat"`, no generation: the notes scored are those the session's requests built, once the
  `AssistantRequestConsumer` queue has drained (`CHAT_TURNS_TIMEOUT_S`) -- with the real Claude transport
  (`evals.cli._eval_transport`, replaced in tests). What came out is read back through the vault
  and the observer's loader and scored. `estimate.run_settings` applies `[eval]
  request_detection` over `[observer]` for the estimate and the app. `write_report` writes
  `report.md` (Spanish) and `report.json` (`EvalReport`, computed scores included, plus the run's
  `request_detection` and `notes_path`).
- **Rubric** (`scoring.py`; pure, deterministic, every score in [0, 1], higher is better). Text is
  compared normalized: `[[?x]]` becomes `x`; footnote references, anchors, link targets, Markdown
  markup and punctuation are dropped; accents folded; lower case; whitespace collapsed. *Content
  words*: normalized words of 3+ letters that are not Spanish stop words, and numbers.
  - *Page transcription*, per reference page: character accuracy `1 - CER` and word accuracy
    `1 - WER` (Levenshtein distance / reference length, floored at 0). A page never transcribed
    scores 0.
  - *Observer sections*, when `sections.yaml` exists: pairwise agreement (Rand index) over the
    reference's segments -- a pair agrees when the reference and the observer both put it in one
    section or both keep it apart; an unassigned segment is in no section -- plus coverage (share
    of those segments assigned). Section ids and titles never need to match.
  - *Editor fidelity*: both notes are cut into units (list items and sentences; headings,
    footnote definitions and units with fewer than 2 content words left out). *Kept* = share of
    reference units with at least 50 % of their content words in one generated unit (dropped
    content lowers it). *Supported* = share of generated units with at least 80 % of their
    content words in the session's transcript, its page transcriptions or the reference notes
    (content from nowhere lowers it). The report lists the dropped and unsupported units, whether
    the notes stayed a draft, and the validator's errors.
  - *Editor repetition* (`score_unique`, also `NotesFidelity.unique`/`repeated`; #474): *unique*
    = 1 - the share of generated units repeating an earlier generated unit (the two share at
    least 50 % of their combined content words, `REPEATED_SHARE`: the same idea written again,
    even read slightly differently). It catches overlapping captures incorporated twice, which
    kept and supported cannot see. The report shows it beside the other scores and lists the
    repeated units; it is compared between runs but NOT part of the global score, so earlier
    baselines stay comparable (`None` in reports written before it). Its scoring test uses the
    synthetic overlapping captures of `backend/tests/fixtures/overlap/`.
  - *Request detection*, when `requests.yaml` exists: the session's `assistant.request` events
    (not typed ones) against the reference; a detected request matches a reference one of the
    same kind sharing at least one segment, each matched once. Precision, recall and F1 overall
    and per kind; the report lists the missed and spurious requests (summary and text).
  - *Capture triage*, when `triage.yaml` exists (`score_triage`): the triage each listed capture's
    page ended the run with -- the real `sources` triage the replay ran at capture time
    (deterministic unless `[sources] triage_llm_check`, then through the run's transport),
    `same_content` included, read from the run vault's sidecars (no `triage` block reads as
    kept; a capture never stored as `missing`). "Set aside" is the positive class: precision,
    recall and F1. Per reason (blank, duplicate, blurry, partial, same_content): accuracy, the
    share of listed captures on which run and reference agree about that reason, and its mean
    (`reason_accuracy`). The report lists the captures triaged otherwise than the reference.
  - *Global* per case: the mean of page character accuracy, section agreement, request F1,
    triage F1, kept and supported (those that exist; no notes counts kept and supported as 0);
    *unique* is not in it.
- **Comparison** (`compare.py`, `compare_reports` / `previous_report` / `render_comparison`;
  pure, no Claude). After a run, `run_eval` loads the most recent readable `report.json` among the
  sibling `runs/<UTC time>/` directories whose name sorts before its own; an unreadable one is
  named in a warning (log, CLI, `EvalReport.comparison_warnings`, the report) and skipped. Per
  case present in both runs and per score (page character/word accuracy, section
  agreement/coverage, request precision/recall/F1, triage precision/recall/F1/reason accuracy,
  kept, supported, unique, global): previous value, new value, delta (`None` when
  either is missing); a drop larger than `[eval] regression_margin` is a regression. Cases only in
  one run are listed as added/removed. Each run's request detector and notes path are shown
  (`None` for older reports, whose new fields are all optional). It is stored as `EvalReport.comparison` (`RunComparison`,
  optional so older reports still load) and rendered as the "Comparación con la ejecución
  anterior" section of `report.md`.

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
- End-to-end coverage, in that same run (no network, `FakeClaude`, a local bare remote as GitHub,
  a few seconds each): `tests/server/test_pipeline_e2e.py` (a replayed session -> "prepárame el
  tema" -> pushed notes), `tests/server/test_workspace_e2e.py` (a replayed session whose spoken
  requests build the notes through the workspace -> "ya está, quiero estudiar" -> a quiz from the
  study chat -> a save that leaves it stale, #369) and `tests/server/test_restore_drill.py` (the
  resulting state survives a clone onto a fresh PC).
- A suite whose skeleton does not exist yet is skipped with a message; full logs land in
  `.cache/test-<suite>-last.log`, so read that instead of re-running.
