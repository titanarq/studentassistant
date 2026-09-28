# Cloud session report (2026-09-28)

This reports what the Claude Code cloud session did after it took over from `cloud-2026-09-28.md`.
It lives only on the branch `handoff/cloud-2026-09-28`, never on `main`. Read it together with
`AGENTS.md` and the earlier handoff.

## 1. State at the end

- `main` is at `a7ead04`.
- No PR is open.
- Every issue from the queue is closed except **#488** (study progress). The human deferred #488:
  it stays open with label `p3` and must not be dispatched until they revisit it. Feeding the
  tutor through the chat history was rejected, and usage data and metrics need their own design.

## 2. Merged today (all squash, each reviewed by a separate agent)

| PR | Closes | What |
|---|---|---|
| #475 | #472 | Chat reports app bugs and improvements into the vault `feedback/inbox.jsonl`, with the CLI `feedback list/mark` and `GET /api/feedback`. |
| #477 | #473 | The resource detail opens over the document. The transcription can be edited by hand, and the thumbnail controls are translucent. |
| #478 | #474 | Incremental incorporation of overlapping captures. Reviewed blocks are locked, and there is an eval score *unique*. |
| #480 | #479 | Mermaid is rendered in the notes viewer (lazy-loaded, strict security). |
| #489 | #485 | Visual zones: a header band, a desk and cards. The document header is sticky. |
| #490 | #486 | One reusable full-screen confirmation modal, used by the Recursos delete and the unsaved-changes prompt. |
| #492 | #484 | `editor/crop.py`: Sonnet locates a region, and the crop is cleaned up and stored with provenance. |
| #494 | #491 | Restoring a version goes through the modal. Tab stays inside a busy modal. |
| #496 | #493 | The workspace chat can crop a page region and insert it into the notes. |
| #497 | #487 | Estudiar shares the Construir frame, and the study chat has the microphone. |
| #499 | #495 | The modal traps focus when idle, and the flaky Recursos focus test is fixed. |
| #500 | #498 | Undoing a turn retires its crop even when a batch commit took the crop first. |
| #501 | #482 | An npm override pins `lodash-es` 4.18.1 under mermaid, so `npm audit` reports 0 advisories. |
| #503 | #476 | Feedback ids are random (`fb-xxxxxx`), so two PCs never collide. |
| #504 | #481 | Mermaid in the exam PDF and the Marp slides uses the fallback «Diagrama no disponible en la exportación». |
| #506 | #483 | Settled blocks stay settled against later or indirect doubts. |
| #507 | #505 | The same mermaid fallback in Anki cards. The export edge cases are aligned. |
| #508 | #502 | The crop retirement on undo survives crashes and a batch commit that splits the crop's files. |
| #510 | #509 | Estudiar: «Repasos para hoy» is removed, the options are cards two per row showing title and state, images render in every notes view and the study chat, and the chat reads «Chat sobre el tema…» / «Enviar». |

Several PRs were blocked by their first reviewer on a real, reproduced bug and fixed on the same
branch before a second review merged them: #477, #478, #490, #499 and #508 (twice). All
reviewers verified the web PRs in headless Chromium.

## 3. Decisions the human made in this session

- **Feedback flow.** The chat's bug and improvement reports stay in the vault; the backend never
  calls GitHub. The human triages them with the local Claude Code, which runs
  `studentassistant feedback list --status nuevo`, files the issues (title and text only, never
  the private chat excerpt) and marks each item `feedback mark <id> --status triado --issue N`.
  #484 came in through this channel (`fb-1`).
- **Visual design.** The old mockups are lost, and the orchestrator's proposal was accepted: the
  notebook/blackboard tokens with zones (#485).
- **Confirmations.** Every confirmation uses the shared full-screen modal (#486, #491).
- **Estudiar.** It inherits the Construir frame. Every change to Construir applies to Estudiar too
  (#487).
- **Study progress.** Deferred (#488); see section 1.
- **Screenshots.** No screenshots in PRs: the human tests locally, and #473's criterion was
  dropped.
- **Mermaid exports.** The fallback is accepted for now. Rendering the exports in the browser would
  be a separate, bigger task, if it is ever needed.

## 4. How the cloud session ran agent-os (and what to recreate next time)

The container has no `.secrets/`, no systemd, no qwen and no Android SDK, and GitHub GraphQL is
blocked. The following workarounds, all outside the repo, made the mechanism usable. Their files
are in `docs/handoff/cloud-shims/` on this branch, and `setup.sh` there recreates them:

- **`gh`.** The real CLI lives at `/usr/local/lib/gh-real`. `gh-rest-shim.py` sits at
  `/home/linuxbrew/.linuxbrew/bin/gh`, the path `config/agents.yaml` names, and translates the
  `gh issue view|list|edit|comment` and `gh pr list|view|create|diff` calls agent-os makes into
  REST. It prints nothing for a null jq result, as real gh does. Board (Projects) moves still fail
  with a warning, because they need GraphQL, and they are harmless.
- **`claude`.** `claude-wrapper.sh` sits at `/home/titan/.local/bin/claude`. It sets
  `IS_SANDBOX=1`, because the container runs as root and `--dangerously-skip-permissions` is
  otherwise refused. It unsets `CLAUDE_AUTO_BACKGROUND_TASKS` and
  `CLAUDE_CODE_BG_TASKS_REPORT_RUNNING`, which the orchestrator session sets: without that, a
  headless worker ended its turn while its tests ran in the background, and the guard cut it. It
  also refuses the `planner` and `refiner` roles.
- **Planner.** The guard wakes the planner after every run. `sa-worker` wraps
  `scripts/worker_task.sh` with `PLANNER_CLAUDE_BIN` and `PLANNER_QWEN_BIN` pointing at
  `planner-disabled.sh`, so the planner never runs on its own. The cloud orchestrator plans by
  hand.
- **Workers.** Workers are always Claude Opus: the budget class is `complex-claude`, and qwen was
  never used. A second implementation lane ran as a native subagent in its own worktree when the
  agent-os Claude worktree was busy.
- **agent-os interpreter.** `agent_os/.venv` was built with Python 3.12 through `uv`; the
  container's `python3` is 3.11.
- **Commit identity.** The git identity is set to the human's noreply email, per the handoff rules.
- **GitHub access.** The Claude GitHub App had to be installed on the `titanarq` organisation
  before the session could push or write issues. Before that, both answered 403.
- **Merges.** Merges went through REST, squash, pinned to the head SHA. All PRs and merges carry
  the human's account, because there is no separate GitHub identity per agent in the cloud.

Next time, in a fresh cloud session: run `bash docs/handoff/cloud-shims/setup.sh` from a checkout
of this branch (or copy the folder), then `sa-worker claude branch task/<N>-<slug>` and
`sa-worker claude start <N> <extra-brief.md>`. Issues must follow the agent-os template
(`agent_os/templates/issue_template/task.md`) with `<!-- budget: complex-claude -->`.

## 5. For the human on the local PC

After pulling `main`, run the usual steps:

```
git pull --ff-only
cd backend && uv sync --extra whisper
cd ../web && npm ci && npm run build
systemctl --user restart studentassistant.service
```

Then test what each row in section 2 describes. The cloud session listed the checks for each PR
in its chat.
