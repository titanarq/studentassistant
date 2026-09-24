# A question for the human is written in their language and in functional terms

- Date: 2026-09-15
- Status: accepted
- Modules: workers

## Context
The refiner's first doubt, on #104, reached the human in English and in code terms -- issue
sections, config paths, a class name -- because that is the shape every role's RULES already asks
for everywhere else (AGENTS.md's own language rule: code, commits and repository docs are English
regardless of what language the human's own conversation is in). The human had to ask for it to be
translated and re-explained before they could answer it. A `## Doubts` block, a `blocked-on-human`
question, a worker's BLOCKED comment and the refiner's summary comment are the one place the
mechanism speaks TO the human rather than about the work, and that one place was following the
wrong rule.

## Decision
- **`project.human_language`** (`config/agents.yaml`, roedor: `Spanish`) names the language
  everything addressed to the human is written in. `agent_lib.ProjectConfig.human_language`
  defaults to `English` when the key is absent, so a project that never sets it keeps today's
  behaviour. Code, issue bodies and repository docs stay whatever AGENTS.md sets regardless --
  this field is only about what an agent *says to* the human, never about what it *writes into*
  the tracker.
- **The rule lives once**: `scripts/agent_lib.py human_message_rules(project=None) -> str`, plus
  the CLI subcommand `human-message-rules` that prints it with the language substituted. It states
  what is written in that language, that each doubt is explained in functional terms for a reader
  who knows the product and how it is operated but is not reading the code (what is at stake and
  why it matters now, the options and what each means in practice for the product, the operation,
  cost, dates and risk, and the agent's own recommendation, ending in a concrete question), that
  code identifiers/paths/labels/issue numbers are a reference after the explanation and never the
  explanation itself, and that what the mechanism or another agent parses -- the `@<login>` first
  line, `## Doubts` and the other section headings, the `<!-- refiner-summary -->` marker, the
  `BLOCKED reason=` line, issue bodies, the validator's checklist -- stays exactly as specified
  elsewhere, in its own spelling.
- **One placeholder, `__HUMAN_MESSAGE_RULES__`**, injected as its own paragraph next to the
  existing `__HUMAN_LOGIN__` substitution in every role's RULES block: the validator's and the
  refiner's in `scripts/agent_task.sh`, the planner's in `scripts/planner_task.sh`, the worker's
  in `scripts/worker_task.sh`. No driver carries its own copy of the wording, and no project
  literal (a language, a login, a repo name) appears in any of the four scripts
  (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
- **The refiner's summary comment is the one case that needed an explicit note beyond the shared
  paragraph**: it posts more than doubts, so its rules say plainly that the whole comment -- not
  only a `## Doubts` block -- is written in the configured language, right after the fixed
  `<!-- refiner-summary -->` marker line, which keeps its own spelling; the issue bodies it writes
  or rewrites (including every sub-issue) stay English, per AGENTS.md, because that rule is about
  what it writes into the tracker, not what it says to the human.

## Consequences
- A doubt reaches the human already in their own language and already framed as a decision they
  can make from the product and the operation, not from the code -- the translation and
  re-explanation #104 needed is now the agent's own job, not the human's.
- A second project adopts this by setting one YAML key; a project that sets nothing keeps writing
  to its human in English, same as before this issue.
- `tests/test_agent_lib.py` tests the loaded language and the wording's own shape (pure, no
  network); `tests/test_agent_task.py` and `tests/test_worker_task.py` test that every role's
  resolved RULES actually carries the paragraph and the configured language with no leftover
  placeholder -- the same `--dry-run` / `rules` pattern already used for `__HUMAN_LOGIN__` and
  `__MAIN_CHECKOUT__`.
