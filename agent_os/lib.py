"""Shared library for the agent guard and the driver: `config/agents.yaml` task classes, an
issue body's `<!-- budget: --> ` block, and the jsonl event stream both backends emit.

Not a script an agent runs directly for its own sake -- `worker_task.sh`'s `usage_report()` calls
into this via `usage-report` below so there is exactly one implementation of "how big is a turn"
and "did the run fail", shared between the driver's `status`/`collect` output and the guard's
budget check (`agent_os/guard.py`). Read-only: never writes anything.

    python -m agent_os.lib usage-report <events.jsonl> [<issue-body-file>]
        # the context ceiling comes from the issue body's own `<!-- budget: --> ` class (#483) --
        # omitted or unreadable, the report says its budget is unresolved instead of guessing one
    echo "$issue_body" | python -m agent_os.lib resolve-budget [--field F]
        # prints the class name on stdout and exits 0, or one line on stderr and exits 1 --
        # `worker_task.sh start` refuses the dispatch on a non-zero exit. `--field max_cost_usd`
        # prints that field of the resolved class instead of its name, and so does `--field
        # max_total_tokens`, the token ceiling a Qwen class is actually cut on (#387).
    python -m agent_os.lib planner-run-row <log> --ts T --context C --model M
        # one `.cache/planner/runs.tsv` line, read off the log's last `result` event.
    python -m agent_os.lib mark-backend-exited <log>
        # `<log>.exited`: the moment the role's backend process returned, written by both role
        # drivers right after the backend call and before anything else of the run -- the time the
        # guard dates that run's quota observation by (#429), never the log's own mtime.
    python -m agent_os.lib role-class validator [--field model|name|max_context]
    python -m agent_os.lib role-app validator
        # the one class carrying `role: validator`, and the App slug that role signs as --
        # what `agent_task.sh <role>` resolves before it runs anything.
    python -m agent_os.lib role-backend validator [--cache-dir D]
        # the launch gate (#425), one TAB-separated line: backend, model, `yes`/`no` for "this is
        # a substituted run", the ceilings that bind it, and the sentence saying why. The verdict
        # it decides on is the guard's own persisted one
        # (`.cache/agent_guard_<backend>.json`, `last_quota_status`), never an agent's claim, and
        # one older than `mechanism.quota_verdict_ttl_minutes` reads as unknown -- which launches
        # the class's own backend, because a start the quota refuses costs one page while a
        # substitution on a stale verdict costs a review the merge gate rests on.
    python -m agent_os.lib project-value notify_topic_file
    python -m agent_os.lib project-value --path secrets_dir
        # one field of config/agents.yaml's `project:` section, for the shell drivers; `a.b`
        # reaches into a mapping and `--path` resolves it against the repository root.
    python -m agent_os.lib backend-value <backend> command|worktree|app|stream|quota [--path]
        # one capability of one `project.backends` entry (#514) -- what a driver reads instead of
        # branching on a backend's name. Exits 2 for a name that is not a configured backend, 1
        # for a config that does not load.
    python -m agent_os.lib backend-model <backend>
        # the model a worker on that backend runs when the dispatch names none: the first worker
        # class on it, else the first class of any role on it.
    python -m agent_os.lib planner-value relaunch_cap
        # one field of config/agents.yaml's `planner:` section, for the shell drivers.
    python -m agent_os.lib human-message-rules
        # the WRITING TO THE HUMAN rule, `project.human_language` filled in -- injected via
        # __HUMAN_MESSAGE_RULES__ into every role's RULES block, so none of them carries its own
        # copy of the wording.
    python -m agent_os.lib forbidden-paths-rules
    python -m agent_os.lib forbidden-paths-regex
        # the two halves of the ownership rule's FIRST list, both rendered from
        # `project.forbidden_paths` and nothing else: the "FILES YOU MUST NOT TOUCH" paragraph
        # worker_task.sh injects via __FORBIDDEN_PATHS_RULES__, and the `grep -E` pattern its
        # `collect` audits a run's changed paths with. Both print nothing when the list is empty.
    python -m agent_os.lib mechanism-paths-rules
    python -m agent_os.lib mechanism-paths-regex
        # the same two halves over the SECOND list, `mechanism.own_paths` -- the mechanism's own
        # files, which a worker's diff may touch only when the issue body names the path. The
        # paragraph is the second of the two the ownership rule is told in and points back at the
        # first, so worker_task.sh injects the pair together or not at all. Both print nothing
        # when the list is empty, exactly as the two above do.
    python -m agent_os.lib never-run-rules
        # the "COMMANDS YOU MUST NEVER RUN" paragraph, rendered from `project.never_run` with each
        # command's own reason -- ONE list behind the worker's, the validator's and the refiner's
        # RULES, injected via __NEVER_RUN_RULES__. Prints nothing when the list is empty.
    python -m agent_os.lib render-prompt worker [--set NAME=VALUE ...]
        # one role's whole prompt: `agent_os/prompts/<role>.md`, the host's own paragraphs from the
        # file `project.prompt_extras` names for that role, and every placeholder filled -- the
        # config-derived ones by itself, the ones only the run knows (MAIN_CHECKOUT, WORKTREE,
        # REVIEW_BACKEND_LINE) from `--set`. Refuses an extras file the config names and the
        # filesystem lacks, and a placeholder nothing supplied a value for.
    python -m agent_os.lib worker-environment
        # one `KEY<TAB>VALUE` line per `project.worker_environment` entry -- worker_task.sh
        # exports these into the backend process before launching it (a common use is a
        # read-only database URL).
    python -m agent_os.lib worker-environment-rules
        # the environment paragraph worker_task.sh injects via __WORKER_ENVIRONMENT_RULES__,
        # rendered from the same `project.worker_environment` the export loop reads -- names, never
        # values. Prints nothing when the project exports nothing.
    python -m agent_os.lib worktree-backends
        # one backend name per line, every `project.backends` entry with a worktree -- `worker_task.sh start` counts
        # alive workers across every one of them for `planner.max_parallel_issues` (#374).
    python -m agent_os.lib worktree-path <backend>
        # that backend's worktree, resolved against the repository root -- `worktree_path` below,
        # for a shell caller that only has the backend's name (`worker_progress.sh`, #510).
    echo "$commit_subjects" | python -m agent_os.lib stages-completed
        # one commit subject per line on stdin -- prints the highest completed stage N, 0 if none
        # of them is a `stage N/M: <title>` commit (#375).
    python -m agent_os.lib stage-titles <issue-body-file>
        # one stage title per line, in order, from the issue's `## Stages` checklist -- empty
        # output if the section is absent or has no checklist line (#375).
    python -m agent_os.lib cumulative-cost <events.jsonl>...
        # sum of `total_cost_usd` across an issue's stage jsonl logs (archived plus the live one),
        # 4 decimals -- what `max_cost_usd` is checked against for the whole issue (#375).
    python -m agent_os.lib cumulative-tokens <events.jsonl>...
        # the same sum in tokens, as a plain integer the shell can compare arithmetically: what
        # `max_total_tokens` is checked against for the whole issue, and the only one of the two
        # ceilings a Qwen class can cross, because its `result` event reports no cost (#387).
    python -m agent_os.lib quota-status <events.jsonl>
        # `allowed` or `exhausted` for that run's own stream -- the gate `worker_task.sh` checks
        # before chaining the next stage, the same verdict the guard's tick compares (#375).

Verified against real recorded runs (`.cache/worker_claude.jsonl`, `.cache/worker_qwen.jsonl`,
2026-09-14): Claude's `assistant`/`user` events carry a `timestamp` field and its `rate_limit_event`
events carry `rate_limit_info.status` (`"allowed"`/`"rejected"`) per `unifiedWindows.<window>`; a
quota-refused turn surfaces as a `result` event with `is_error: true` and `api_error_status: 429`
(`subtype` stays misleadingly `"success"`). Qwen's events carry none of that -- no timestamp field,
no `rate_limit_event`, no `api_error_status` -- so quota detection below is Claude-only by the data
actually available, not by design choice.

    agent_os/docs/adr/2026-09-14-agent-spend-is-tokens-not-time-and-needs-a-written-budget.md
    agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import string
import sys
import textwrap
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from agent_os.cli import AGENT_OS_DIR, host_root
from agent_os.streams import (
    DEFAULT_STREAM_PARSER,
    QUOTA_DETECTOR_NONE,
    STREAM_PARSERS,
    StreamParser,
    UsageSummary,
    check_quota_detector,
    get_stream_parser,
)
from agent_os.streams.claude_jsonl import (  # noqa: F401 -- re-exported: guard and tests import them here
    result_total_tokens,
    turn_context_tokens,
)

# The HOST project's root, resolved rather than assumed: `$AGENT_OS_HOST_ROOT`, else the git
# checkout the call is made from. Everything a project owns hangs off it -- `config/agents.yaml`,
# `.cache/`, `.secrets/`, `.github/ISSUE_TEMPLATE/`, the `project.worktrees` entries -- and none of
# it is derived from this package's own location, which is what made the mechanism able to run
# exactly one project (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-
# never-modified.md).
HOST_ROOT = host_root()
# `AGENTS_CONFIG_PATH` isolates a test from the repository's real config/agents.yaml, the same
# way `WORKER_CACHE_DIR` isolates `worker_task.sh` from the real `.cache/` -- unset in every real
# run, so production reads the one file everyone else in this module already points at. Needed
# because `tests/test_worker_task.py` exercises `planner.max_parallel_issues` at more than one
# value (#374), which a single real file can only ever pin to one.
DEFAULT_AGENTS_CONFIG = pathlib.Path(
    os.environ.get("AGENTS_CONFIG_PATH") or (HOST_ROOT / "config" / "agents.yaml")
)

# Same convention as `issues.py`'s `<!-- key: --> ` line (KEY_LINE_RE there).
BUDGET_LINE_RE = re.compile(r"<!--\s*budget:\s*([a-z0-9][a-z0-9_-]*)\s*-->")


# The mechanical state an issue is in, in the order it moves through them. `blocked-on-human`
# is a detour from any of the others and `done` is terminal.
STATES = ("refine", "ready", "doing", "blocked-on-human", "ai-completed", "review", "done")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The three ceilings a class declares, named so a fallback can say which of them bind the
# substituted run instead of leaving it to whoever reads the config next.
CeilingName = Literal["max_context", "max_cost_usd", "max_total_tokens"]
CEILING_NAMES: tuple[str, ...] = ("max_context", "max_cost_usd", "max_total_tokens")


class RoleFallback(Strict):
    """The backend a role runs on when its own backend's quota reads exhausted (#425).

    Until this existed, the only route off an exhausted Claude quota was the planner redispatching
    a WORKER whose class set `qwen_fallback_eligible: true` -- and the planner is itself a Claude
    role, so on 2026-09-18 the mechanism stopped with seven `status:ready` issues and a free Qwen
    allowance: the run rejected at 08:48:46Z in 497 ms was the thing that would have redispatched.
    A fallback declared here is read by the LAUNCH of the role itself, before a turn is spent, so
    an exhausted quota stops nothing that names a way round it.

    `ceilings` says which of the class's own three ceilings bind the substituted run, because they
    are not all backend-independent: `max_context` and `max_total_tokens` describe what the role
    reads and how much of it there may be, which does not change with the backend that reads it,
    while `max_cost_usd` sums a field Qwen's `result` event does not carry at all (#387) -- kept on
    a Qwen run it is a ceiling nothing can cross, and a report built on it reads `0.00 USD` for a
    run that cost tokens. Nothing gates a one-shot role's spend today, so this list is a
    declaration for the day one does (and for the control plane's deviation report), not a limit
    this file enforces."""

    # A key of `project.backends`, checked at config load by `AgentsConfig`'s own validator below
    # rather than pinned to a two-name `Literal` (#510) -- a third backend needs no code change,
    # only a `backends:` entry and a class that names it (#514).
    backend: str
    model: str
    ceilings: list[CeilingName] = list(CEILING_NAMES)

    @field_validator("ceilings")
    @classmethod
    def non_empty_and_unrepeated(cls, value: list[str]) -> list[str]:
        # An empty list would mean "no ceiling binds the substituted run", which is not a thing a
        # project declares by accident; a repeated name is a config written by hand twice over.
        if not value:
            raise ValueError("must name at least one ceiling")
        if len(set(value)) != len(value):
            raise ValueError(f"names a ceiling twice: {value}")
        return value


class TaskClass(Strict):
    # Which of the four headless roles this class configures (agent_os/docs/adr/2026-09-14-a-pr-is-
    # validated-by-a-validator-agent-against-the-issues-acceptance-criteria.md). A worker's class
    # is picked by the issue's `<!-- budget: <class> -->` line and there may be many of them; the
    # other three roles have exactly one class each, named after the role, which is how
    # `agent_task.sh <role>` resolves its model without a second mapping.
    role: Literal["worker", "validator", "refiner", "planner"] = "worker"
    # See `RoleFallback.backend` above: a key of `project.backends`, validated once the whole
    # config is loaded, when `project` is there to validate it against.
    backend: str
    model: str
    max_context: int
    max_cost_usd: float
    # The issue-wide ceiling that always bites: Qwen's terminal `result` event reports
    # `usage.total_tokens` and no `total_cost_usd`, so on a Qwen class the dollar ceiling above
    # cannot be crossed and this one is the only live spend backstop (#387). Same scope as the
    # dollars -- the whole issue, summed over its stage processes' archived plus live jsonl logs.
    max_total_tokens: int
    commit_warn_turns: int
    commit_cut_turns: int
    qwen_fallback_eligible: bool = False
    # The backend this class's role runs on when its own backend's quota reads exhausted, or None
    # for a class that declares no way round it -- which is every worker class today, and is what
    # keeps the guard's `quota_exhausted_no_fallback` page meaningful (#425). Absent means the
    # launch behaves exactly as it did before this field existed.
    fallback: RoleFallback | None = None

    @model_validator(mode="after")
    def fallback_names_another_backend(self) -> TaskClass:
        # A fallback to the backend whose quota is already exhausted is not a fallback: it would
        # read as one in the config and substitute nothing, so the launch would spend a turn
        # discovering what the declaration should have said.
        if self.fallback is not None and self.fallback.backend == self.backend:
            raise ValueError(
                f"fallback names '{self.fallback.backend}', the class's own backend; "
                "a fallback has to be a different one"
            )
        return self

    @property
    def allows_backend_fallback(self) -> bool:
        """Whether anything authorises running this class on a backend other than its own: the
        planner's redispatch of a worker (`qwen_fallback_eligible`, the 2026-09-14 ADR) or a role's
        own declared fallback (#425). ONE predicate behind the guard's page and the launch gate, so
        "could this have run somewhere else?" is never answered two ways: the
        `quota_exhausted_no_fallback` page exists for the case where nothing can proceed without a
        human, and a class with a way round it is not that case."""
        return self.qwen_fallback_eligible or self.fallback is not None


class LabelVocabulary(Strict):
    """The mechanical state labels. Named here so a project that spells them differently changes
    one file instead of the guard (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-
    and-configured-not-coded.md).

    The first six are the states an issue moves through and exactly one of them is ever set
    (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
    state.md). `agents_paused` is NOT one of them: it is the human-only full stop, lives on the
    tracking epic, and `move` never adds or removes it."""

    refine: str = "status:refine"
    ready: str = "status:ready"
    doing: str = "status:doing"
    blocked_on_human: str = "status:blocked-on-human"
    ai_completed: str = "status:ai-completed"
    review: str = "status:review"
    agents_paused: str = "status:agents-paused"
    # NOT a state: a marker a human puts on a FEATURE (never a task/bug) to say its refined
    # children may be promoted to `status:ready` mechanically, without a human looking at each one.
    # `issues.py move` never adds or removes it, the same way it never touches `agents_paused`
    # (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md).
    auto_ready: str = "auto-ready"
    # NOT a state either: the human (or the control-plane agent, which authenticates `gh` as the
    # human) puts it on any open issue to wake the planner NOW, and the guard's tick removes it the
    # same tick it reads it (#413). Only a label set by the human wakes anything -- set by one of
    # the mechanism's own identities it is removed and ignored, so the planner cannot wake itself.
    wake_planner: str = "wake:planner"
    # The rest of the vocabulary `issues.py` used to spell as literals (`agent_os/docs/AGENT_OS.md` §7 row
    # (c)'s sibling: the module names moved to `project.modules` there, `type:*`/`p<n>`/`module:`
    # stayed hardcoded). Defaults match what was hardcoded, so no existing config changes
    # behaviour -- a project only writes these to add a type, change its priority scale, or spell
    # the module prefix differently.
    #
    # The four backlog-YAML concepts (`type_label` in `issues.py` decides which of them an entry
    # is: `Epic`/`Task`/tagged-`bug`/else-`feature`), each combined with the mechanism's own
    # `type:` prefix -- adding a fifth entry here creates its label but not a way to reach it from
    # the YAML loader, which stays fixed to those four concepts.
    types: list[str] = ["epic", "feature", "task", "bug"]
    # One label per priority, in order -- `desired_labels` indexes into this by the YAML's own
    # `priority: n` (1-based), and `fixed_labels` creates every one of them up front.
    priorities: list[str] = ["p1", "p2", "p3", "p4"]
    # The prefix `fixed_labels` joins with each of `project.modules` to create that half of the
    # fixed label set.
    module_prefix: str = "module:"

    def label_for_state(self, state: str) -> str | None:
        """`done` is the one state with no label of its own: it removes every state label and
        closes the issue, so the closed state *is* the record."""
        return None if state == "done" else getattr(self, state.replace("-", "_"))

    @property
    def state_labels(self) -> list[str]:
        """The six mutually exclusive state labels, in state order. `move` strips every one of
        them before adding the one it was asked for."""
        return [self.label_for_state(state) for state in STATES if state != "done"]


class NeverRunCommand(Strict):
    """One command an agent must never run, and the reason why -- the reason travels with the
    command because a prohibition nobody can justify is the first one a task talks itself out of
    (`agent_os/docs/AGENT_OS.md` §7 row (b): the same verb list was baked into three RULES blocks and could
    drift between them). Rendered into every role's RULES from `project.never_run`; a project with
    an empty list gets no such paragraph at all."""

    command: str
    reason: str

    @field_validator("command", "reason")
    @classmethod
    def one_non_blank_line(cls, value: str) -> str:
        # One line each: the rendering is a bullet per item, so a reason carrying a newline would
        # break the paragraph it is injected into rather than fail loudly anywhere else.
        if not value.strip() or "\n" in value:
            raise ValueError("must be one non-blank line")
        return value.strip()


def _check_placeholders(key: str, template: str) -> None:
    """Every way a template can be malformed, caught when the config LOADS instead of on the page.
    A literal `{` in a Spanish sentence, or a `{}` someone copied from another language's
    formatting, raises out of `str.format` -- and the only place that would have shown up is the
    moment something needed to page, which is the worst moment to discover a typo (issue #366
    review). `string.Formatter().parse` is exactly the parser `format` itself uses, so what it
    accepts here is what will render there."""
    try:
        fields = [
            field for _, field, _, _ in string.Formatter().parse(template) if field is not None
        ]
    except ValueError as malformed:
        raise ValueError(f"message {key!r} is not a valid template: {malformed}") from malformed
    for field in fields:
        if not field:
            raise ValueError(
                f"message {key!r} uses a positional placeholder; pages are rendered by name, "
                "so every placeholder must be spelled out (`{issue}`, not `{}`)"
            )
        if not field.isidentifier():
            raise ValueError(
                f"message {key!r} uses a placeholder {field!r} that is not a plain name"
            )


class HumanMessageError(Exception):
    """A page the mechanism cannot write: `project.messages` has no such key, the template asks
    for a field the call site does not have, or the template itself is malformed. One type for all
    three, because every caller does the same thing with it -- says so and carries on, since by
    the time anything pages, the state the page announces is already written."""


class BackendConfig(Strict):
    """One backend CLI a worker or a role can run on, and everything the mechanism needs to know
    about it -- `project.backends.<name>` (#514). The NAME is the key and nothing in the mechanism
    compares it against a literal: what a backend can do is read off these fields, so adding a CLI
    is a config entry, plus one parser module under `agent_os/streams/` only when its event stream
    is a shape no registered parser reads.

    - `command`: what the drivers run -- an absolute path, or a bare name resolved through PATH.
      Empty means "the `project.executables` entry of the same name, else the backend's own name",
      which is exactly how a backend resolved before this section existed (#380). An absolute
      command is also added to `project.executables`, the PATH set the generated units carry.
    - `worktree`: the backend's worktree, relative to the host's root. Empty for a backend that
      only ever runs one-shot roles, which work in a throwaway worktree of their own.
    - `app`: the GitHub App slug a worker on this backend signs as. Empty signs as nobody's and
      degrades to the ambient identity, exactly as a missing `worker_apps` entry did.
    - `stream`: the registered parser its jsonl events are read with (`agent_os.streams`); it also
      picks the command-line dialect the drivers launch it with, because the flags that produce a
      stream-json log belong to the CLI that writes that shape. An unregistered name fails here.
    - `quota`: the detector whose `exhausted` verdict cuts a live run of this backend
      (`agent_os.streams.QUOTA_DETECTORS`); `none` records the verdict and never cuts on it."""

    command: str = ""
    worktree: str = ""
    app: str = ""
    stream: str
    quota: str = QUOTA_DETECTOR_NONE

    @field_validator("stream")
    @classmethod
    def a_registered_stream_parser(cls, value: str) -> str:
        get_stream_parser(value)
        return value

    @field_validator("quota")
    @classmethod
    def a_registered_quota_detector(cls, value: str) -> str:
        return check_quota_detector(value)


class DeprecatedBackendMapsWarning(UserWarning):
    """`project.worktrees` / `project.worker_apps` / `project.executables` used as the description
    of a backend, which `project.backends` replaced (#514). One release of grace, then an error."""


# The three parallel maps `project.backends` replaced. `executables` stays a live key with its own
# meaning -- every executable the units need on PATH -- and only stops being where a backend's
# command is read from when that backend declares a `command:` of its own.
DEPRECATED_BACKEND_MAPS = ("worktrees", "worker_apps", "executables")

# The deprecated alias's reading of `quota:`: a backend built from the old maps is cut on its
# quota exactly when the guard cut it before `backends:` existed -- a backend whose own name picks
# the `claude_jsonl` parser. Nothing outside `_backends_from_deprecated_maps` reads this.
DEPRECATED_ALIAS_QUOTA_BY_STREAM = {"claude_jsonl": "claude_rate_limit"}


# The deprecation messages this process has already emitted. `doctor`, the guard's tick and every
# `python -m agent_os.lib` call of a driver each load the config several times, and one line per
# process is what says so; the warnings module's own "once" bookkeeping is reset by anything that
# touches the filters, so it is not relied on.
_EMITTED_DEPRECATION_WARNINGS: set[str] = set()


def _warn_deprecated_backend_maps(message: str) -> None:
    if message in _EMITTED_DEPRECATION_WARNINGS:
        return
    _EMITTED_DEPRECATION_WARNINGS.add(message)
    warnings.warn(message, DeprecatedBackendMapsWarning, stacklevel=2)


def _backends_from_deprecated_maps(data: dict) -> dict:
    """`data` (a raw `project:` mapping) with `backends` filled in from the three old maps when it
    declares none -- the deprecated alias, for one release -- or unchanged, with a warning naming
    what is ignored, when it declares both."""
    worktrees = data.get("worktrees") or {}
    apps = data.get("worker_apps") or {}
    executables = data.get("executables") or {}
    if not all(isinstance(value, dict) for value in (worktrees, apps, executables)):
        return data  # the field validators say what is wrong with the shape
    declared = data.get("backends")
    if declared is not None:
        ignored = [
            f"project.{key}"
            for key, value in (("worktrees", worktrees), ("worker_apps", apps))
            if value
        ]
        if isinstance(declared, dict):
            ignored += [
                f"project.executables.{name}"
                for name in executables
                if isinstance(declared.get(name), dict) and declared[name].get("command")
            ]
        if ignored:
            _warn_deprecated_backend_maps(
                f"config: project.backends is declared, so {', '.join(ignored)} "
                "is ignored as the description of a backend -- delete it "
                "(agent_os/docs/AGENT_OS.md §4.2)"
            )
        return data
    names = list(dict.fromkeys([*worktrees, *apps]))
    if not names:
        return data
    _warn_deprecated_backend_maps(
        "config: project.worktrees, project.worker_apps and project.executables are deprecated as "
        "the description of a backend; project.backends was built from them for this release -- "
        "declare project.backends.<name>: {command, worktree, app, stream, quota} instead "
        "(project.executables keeps its own meaning; agent_os/docs/AGENT_OS.md §4.2)"
    )
    backends = {}
    for name in names:
        # The derivation the parsers were picked by before they were configured (#514 stage 1):
        # the parser named after the backend when one is registered, the default shape otherwise.
        named_after_backend = f"{name}_jsonl"
        stream = (
            named_after_backend if named_after_backend in STREAM_PARSERS else DEFAULT_STREAM_PARSER
        )
        quota = QUOTA_DETECTOR_NONE
        if stream == named_after_backend:
            quota = DEPRECATED_ALIAS_QUOTA_BY_STREAM.get(stream, QUOTA_DETECTOR_NONE)
        backends[name] = {
            "worktree": worktrees.get(name, ""),
            "app": apps.get(name, ""),
            "stream": stream,
            "quota": quota,
        }
    return {**data, "backends": backends}


class ProjectConfig(Strict):
    """Everything that belongs to *this* project rather than to the mechanism: the repository,
    the board, the tracking epic, where the identities and the notify topic live, and one
    worktree per backend. No mechanism script may read any of it as a literal."""

    repo: str
    tracking_epic: int
    board_number: int
    secrets_dir: str = ".secrets/gh_apps"
    planner_app: str = "planner"
    # The base name of the systemd --user units that run the tick: `<this>.service`,
    # `<this>.timer` and `<this>.service.d/override.conf` under `~/.config/systemd/user/`.
    # `agent_os.install` needs it to know what to write and `agent_os.doctor` to know what to
    # check; empty means neither has been told a name yet, so both refuse loudly rather than
    # guessing one from the repository's name (agent_os/docs/AGENT_OS.md §7 row (h)).
    guard_unit: str = ""
    # Where this project's module docs live, one per `project.modules` entry -- read by the
    # `__MODULE_DOCS__` token the `.claude/agents/*.md` templates carry (#510), so the rendered
    # prompt points at the real directory instead of a literal `docs/modules/workers.md`.
    module_docs_dir: str = "docs/modules"
    # The GitHub login of the one human. A question addressed to them starts with `@<login>` so it
    # reaches their mentions; the drivers substitute it into the RULES they inject, which is why no
    # script under the mechanism spells a person's name.
    human_login: str = ""
    # The language everything addressed to the human is written in (`human_message_rules` below).
    # Code, issue bodies and repository docs stay whatever AGENTS.md sets, regardless of this --
    # this field is only about what an agent says TO the human, never about what it writes INTO
    # the tracker.
    human_language: str = "English"
    notify_topic_file: str = ".secrets/ntfy_topic"
    # One template per ntfy page, written in `human_language` above and rendered by
    # `render_human_message` below. Empty by default, and a project that leaves it empty simply
    # cannot page: the renderer refuses an unknown key rather than inventing a wording of its own
    # (agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md).
    messages: dict[str, str] = {}
    # The project's own module names, one per `docs/modules/*.md`. `issues.py` turns them into the
    # `module:<name>` half of the fixed label set it creates on the tracker; they used to be a list
    # in that script (`agent_os/docs/AGENT_OS.md` §7 row (c)). Empty by default: a project with no module
    # vocabulary creates no `module:` label, it does not fall back to anyone else's.
    modules: list[str] = []
    # The project's own test runner, injected into the worker RULES as __TEST_COMMAND__ rather
    # than a literal `scripts/test.sh` in the mechanism: the one door back to the owner role a
    # read-only-by-default worker has (agent_os/docs/adr/2026-09-15-workers-connect-read-only-by-default-
    # and-reach-the-owner-only-through-the-test-runner.md).
    test_command: str = "scripts/test.sh"
    # One host-owned file per role whose text is appended at that role's `__PROJECT_EXTRAS__`
    # extension point, as a path relative to the HOST project's root. Every key is optional, and a
    # role with no entry renders nothing there: this is where a sentence only the host can write
    # goes -- how its own package resolves under test, which class a worker task belongs in -- so
    # the mechanism's own templates carry no literal of any project (`agent_os/docs/AGENT_OS.md` §7 row
    # (t), #509). A file this names and the filesystem does not have stops the render rather than
    # silently dropping the paragraph.
    prompt_extras: dict[str, str] = {}
    # Environment exported into every worker's own backend process before it starts (never the
    # mechanism's own process) -- the first host uses this for a read-only database URL so a
    # worker connects to its shared database read-only by default, without a project literal in
    # worker_task.sh (agent_os/docs/adr/2026-09-15-workers-connect-read-only-by-default-and-reach-the-
    # owner-only-through-the-test-runner.md).
    worker_environment: dict[str, str] = {}
    # The HOST PROJECT's protected paths: what an agent must never write in this project, as glob
    # patterns relative to the repository root, and the ONE list behind both halves of this rule:
    # `worker_task.sh` builds the audit regex that fails a run which touched one AND the "FILES
    # YOU MUST NOT TOUCH" paragraph injected into the worker's prompt, so the two can no longer
    # drift apart the way the prose list and the regex did (`agent_os/docs/AGENT_OS.md` §7 rows (a) and
    # (s)). A pattern is matched with fnmatch semantics, where `*` crosses `/`: `docs/adr/*`
    # protects `docs/adr/2026-09-16-a-decision.md` too, as the prefix regex it replaces did, and
    # matching it any narrower would silently unprotect every nested file under a protected
    # directory. These hold regardless of what a brief says -- a brief naming one of them is a
    # defect in the brief -- which is what separates them from the mechanism's own files
    # (`MechanismConfig.own_paths` below), where the brief is what authorizes the edit. Empty by
    # default: a project that protects nothing forbids nothing, and no mechanism script carries a
    # path of its own
    # (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
    forbidden_paths: list[str] = []
    # The subset of `forbidden_paths` that is a DELIVERY DIRECTORY rather than live configuration
    # -- a place a PR is expected to add a file to, whose diff changes nothing any stamp or freeze
    # protects (`config/proposals/*`, `docs/adr/*`) -- and so is exempt from condition 3 of the
    # merge gate while staying on `forbidden_paths` itself, unconditional for a worker's brief
    # (`agent_os/docs/AGENT_OS.md` §2.4, `.claude/agents/control-plane.md` Duty 4, issue #476).
    # `forbidden_paths_merge_audit_regex` reads this as an EXCLUSION from `forbidden_paths`, never
    # as a second list of what to audit, so a new `forbidden_paths` entry is audited at merge time
    # by default and needs an entry here only for the rare delivery-directory case. Empty by
    # default: a project with no such split audits its whole `forbidden_paths` at merge time,
    # exactly as before this key existed.
    # An entry here naming a path NOT on `forbidden_paths` is a silent no-op, deliberately not a
    # load-time error (#516 review): `load_project()` backs every role this mechanism runs --
    # the guard, the planner, every worker -- and a raise here would take all of them down over a
    # typo or a stale entry in the one merge-time carve-out condition 3 reads. The real config's
    # own entries are checked against drift by a test instead
    # (`test_real_config_merge_audit_exempts_exactly_the_two_delivery_directories`), and a project
    # assembling a synthetic config (a test fixture patching `forbidden_paths` down without also
    # trimming this list) gets a config that still loads, with the stale entry simply excluded from
    # nothing -- `forbidden_paths_merge_audit_regex` below only ever SUBTRACTS this set from
    # `forbidden_paths`, so a member this list shares with nothing has no effect either way.
    merge_audit_exempt_paths: list[str] = []

    # Commands no role may run, each with the reason it is forbidden, rendered into the worker's,
    # the validator's and the refiner's RULES from this one list (`agent_os/docs/AGENT_OS.md` §7 row (b)).
    # Empty renders no such paragraph, which is what a project whose database no agent can write
    # wants.
    never_run: list[NeverRunCommand] = []
    # Every backend a worker or a role can run on, by name (`BackendConfig` above, #514). Absent,
    # it is built from the three deprecated maps below with one warning; declared, it wins and the
    # old maps are ignored as a description of a backend, with a warning naming them.
    backends: dict[str, BackendConfig] = {}
    # DEPRECATED (#514): one worktree per backend, relative to the repository root -- now
    # `backends.<name>.worktree`. After load it always MIRRORS `backends`, so a reader that has not
    # moved yet sees the configured worktrees whichever form the file uses.
    worktrees: dict[str, str] = {}
    # The absolute path of every external executable the mechanism's units call by bare name, by
    # COMMAND NAME: the backend CLIs (`qwen`, `claude` -- and `claude` again for the role drivers,
    # which run the same binary under the same key) AND the tracker CLI (`gh`) alike. Empty by
    # default, and a name with no entry resolves to itself, which is the bare-name PATH lookup
    # every driver did before: a project that sets nothing behaves exactly as it did.
    # Why it exists: a dispatch from an unattended unit and a dispatch from a shell must resolve
    # the SAME binary. On 2026-09-16 they did not -- `qwen` lives under nvm, the PATH the systemd
    # user manager hands the guard's own unit does not carry that directory, and the stage
    # process died in under a second while the mechanical state called it a stage cut for not
    # committing (#380, #381, #363). `agent-os-install` turns every value here into a directory on
    # the generated unit's `Environment=PATH=` (`executables_path_prefix()` in
    # `agent_os/agent_os/install.py`), which is why a command that is not a backend -- `gh`, called
    # by bare name dozens of times per tick -- belongs here too. An absolute
    # `backends.<name>.command` is added to this mapping at load, so the PATH set always carries
    # the backends' own directories (#514).
    executables: dict[str, str] = {}
    # DEPRECATED (#514): one GitHub App slug per backend -- now `backends.<name>.app`. Mirrors
    # `backends` after load, exactly as `worktrees` above does.
    worker_apps: dict[str, str] = {}
    # One GitHub App slug per non-worker role. A role with no entry here signs as `planner_app`:
    # the validator and the refiner do exactly that until they have Apps of their own, which is a
    # browser step for the human, so the day one exists is a line in this file and no code change
    # (agent_os/docs/adr/2026-09-14-a-pr-is-validated-by-a-validator-agent-against-the-issues-acceptance-
    # criteria.md, "Identities").
    role_apps: dict[str, str] = {}
    labels: LabelVocabulary = LabelVocabulary()
    # state -> the board column that mirrors it. A state mapped to null (blocked-on-human) keeps
    # whatever column the item is in: being blocked says nothing about how far the work got.
    board_columns: dict[str, str | None] = {}

    @model_validator(mode="before")
    @classmethod
    def backends_or_their_deprecated_alias(cls, data: object) -> object:
        return _backends_from_deprecated_maps(data) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def deprecated_maps_mirror_the_backends(self) -> ProjectConfig:
        self.worktrees = {name: b.worktree for name, b in self.backends.items() if b.worktree}
        self.worker_apps = {name: b.app for name, b in self.backends.items() if b.app}
        # A bare-name command is already a PATH lookup and names no directory to add.
        absolute_commands = {
            name: b.command for name, b in self.backends.items() if os.path.isabs(b.command)
        }
        # Backends FIRST: `executables_path_prefix()` walks this dict in declaration order to
        # build the generated unit's `Environment=PATH=`, and a backend CLI (often under a version
        # manager's own directory, e.g. nvm) is what the two dispatch paths most need to agree on
        # (#380) -- putting the plain tracker entries (`gh`) ahead of it would silently reorder the
        # generated PATH against the one armed by hand.
        self.executables = {**absolute_commands, **self.executables}
        return self

    @field_validator("messages")
    @classmethod
    def one_non_blank_line_per_message(cls, value: dict[str, str]) -> dict[str, str]:
        # A page is a phone notification, not a letter: `notify.sh` posts one argument and ntfy
        # renders it on one line, so a template carrying a newline would arrive mangled rather
        # than fail anywhere a person would see it.
        for key, template in value.items():
            if not template.strip() or "\n" in template:
                raise ValueError(f"message {key!r} must be one non-blank line")
            _check_placeholders(key, template)
        return {key: template.strip() for key, template in value.items()}


class MechanismConfig(Strict):
    """The mechanism's own configuration, which is NOT the host project's to write: `project:`
    above holds what one project supplies, this holds what the mechanism is made of, so a second
    project adopts the drivers and copies this section with them instead of inventing it
    (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md)."""

    # The mechanism's own files -- the drivers, the shared library, the role prompts -- as globs
    # relative to the repository root, matched with the same fnmatch semantics as
    # `forbidden_paths`. The second of the two lists a worker's diff is audited against, and the
    # conditional one: a worker may write these IN ITS WORKTREE when the issue body names the path
    # as the target of the work, and never otherwise, because the tracking epic exists to change
    # them and one unconditional list left the mechanism unable to develop itself (#363 did the
    # work its brief named and then failed the merge gate on the rule the same driver enforces).
    # Writing them in the main checkout stays forbidden by the rule that forbids writing there at
    # all -- and that is also why editing them from a worktree cannot break the run: a stage runs
    # the main checkout's copy of the driver
    # (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md).
    # Empty by default, like every list in this config: a mechanism that names none of its own
    # files audits none, and the paragraph a driver renders from an empty list is absent rather
    # than empty.
    own_paths: list[str] = []
    # How long the guard's persisted verdict on a backend's quota stays believable, in minutes.
    # Past it the launch of a role reads the verdict as UNKNOWN and runs the role's own backend:
    # a start that the quota then refuses costs one refused run and the page that follows it,
    # while a substitution made on a stale verdict costs a review the independence the merge gate
    # rests on (#425). The bias is deliberate, and so is the number: the guard re-observes the
    # verdict on every tick, five minutes apart, while a run of that backend is alive, so an hour
    # means twelve ticks saw nothing to look at.
    quota_verdict_ttl_minutes: int = 60


class PlannerConfig(Strict):
    """How often the planner may be woken (agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-
    and-an-idle-wake-is-rate-limited.md)."""

    idle_wake_minutes: int = 120
    max_runs_per_day: int = 12
    # Stays false until the human has reviewed the refiner's dry run on three real issues (#349
    # criterion 3). Flipping it is a human decision, never something a checkpoint does on its own
    # (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md).
    refiner_unattended: bool = False
    # `WIP: cut by guard` commits since a branch's fork point at or above which `worker_task.sh
    # resume` refuses to relaunch, read via `planner-value` below (#362).
    relaunch_cap: int = 2
    # How many issues may run at once across every backend, read via `planner-value` below and
    # enforced by `worker_task.sh start`, not counted by the planner (#374).
    max_parallel_issues: int = 1
    # How far back the guard's FIRST reconciliation of closed-but-still-labeled issues looks, in
    # days (#365). Afterwards it asks only for issues closed since its own last pass, so the tick
    # never pages through the whole closed backlog: `gh issue list --state closed` is capped at
    # one page, and past that cap an old issue closed today would silently never be reconciled.
    # A machine that has been off for longer than this loses nothing a human cannot fix with one
    # `issues.py move N done`.
    reconcile_closed_lookback_days: int = 30


class AgentsConfig(Strict):
    project: ProjectConfig
    # The mechanism's own section, optional exactly as `planner:` is: a config that predates it
    # still loads, and an absent mechanism list audits nothing rather than failing the dispatch.
    mechanism: MechanismConfig = MechanismConfig()
    planner: PlannerConfig = PlannerConfig()
    classes: dict[str, TaskClass]

    @model_validator(mode="after")
    def backends_are_configured_backends(self) -> AgentsConfig:
        """`TaskClass.backend` and `RoleFallback.backend` used to be pinned to a two-name
        `Literal`; freed into a plain `str` (#510) they would silently accept a typo with no
        backend behind it, so this checks each one against `project.backends`'s own keys instead,
        once, here, rather than at whichever dispatch first tries the unknown name.

        A project that configures no backend at all (`project.backends` empty, and none of the
        deprecated maps it is built from either) declares nothing to check a backend name against,
        so it is left alone -- the same "empty means unchecked" reading `forbidden_paths`/
        `never_run` already use elsewhere in this file."""
        known = set(self.project.backends)
        if not known:
            return self
        for name, task_class in self.classes.items():
            if task_class.backend not in known:
                raise ValueError(
                    f"class '{name}' names backend '{task_class.backend}', which is not a key of "
                    f"project.backends ({sorted(known)})"
                )
            if task_class.fallback is not None and task_class.fallback.backend not in known:
                raise ValueError(
                    f"class '{name}' fallback names backend '{task_class.fallback.backend}', "
                    f"which is not a key of project.backends ({sorted(known)})"
                )
        return self


def load_agents_config(path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG) -> AgentsConfig:
    raw = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    return AgentsConfig.model_validate(raw)


def load_project(path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG) -> ProjectConfig:
    return load_agents_config(path).project


def load_mechanism(path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG) -> MechanismConfig:
    return load_agents_config(path).mechanism


def load_planner_config(path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG) -> PlannerConfig:
    return load_agents_config(path).planner


def worktree_path(
    backend: str, *, main: pathlib.Path = HOST_ROOT, project: ProjectConfig | None = None
) -> pathlib.Path:
    """The backend's worktree, resolved against the repository root -- `config/agents.yaml` keeps
    it relative (`../your-repo-qwen`) so a clone under a different path needs no edit. A KeyError
    for a name that is not a configured backend, or one configured with no worktree."""
    project = project or load_project()
    relative = project.backends[backend].worktree
    if not relative:
        raise KeyError(backend)
    return (main / relative).resolve()


def backend_executable(name: str, *, project: ProjectConfig | None = None) -> str:
    """The command a driver actually runs for `name`: the backend's own `command:`, else the path
    `project.executables` configures under that name, else the bare name -- resolved through PATH
    exactly as before -- when neither says. ONE resolver for the worker backends and for the role
    drivers' own backend, so an unattended dispatch and a shell dispatch can never run a different
    binary (#380).

    The bare name is the fallback for a MISSING KEY and for nothing else. A config that does not
    load raises out of here, and its CLI below turns that into a one-line stop rather than an
    empty string: a driver that read an empty command would run `"" --model ...`, which the shell
    answers 127, which then reads as a missing executable and points whoever is debugging at the
    very key the broken file made unreadable."""
    project = project or load_project()
    backend = project.backends.get(name)
    if backend is not None and backend.command:
        return backend.command
    return project.executables.get(name, name)


def backend_config(name: str, *, project: ProjectConfig | None = None) -> BackendConfig:
    """`project.backends[name]`, or a KeyError naming the configured ones."""
    project = project or load_project()
    try:
        return project.backends[name]
    except KeyError:
        raise KeyError(
            f"'{name}' is not a configured backend -- project.backends has {sorted(project.backends)}"
        ) from None


def backend_quota_cuts(name: str, *, project: ProjectConfig | None = None) -> bool:
    """Whether an `exhausted` quota verdict read off this backend's stream cuts its live run: its
    `quota:` names a detector rather than `none` (#514). A name that is not a configured backend
    cuts on nothing, which is what the guard did for any backend it had no rule for."""
    project = project or load_project()
    backend = project.backends.get(name)
    return backend is not None and backend.quota != QUOTA_DETECTOR_NONE


def backend_default_model(name: str, path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG) -> str:
    """The model a worker on `name` runs when the dispatch names none (`WORKER_MODEL`): the model
    of the first `role: worker` class on that backend, else of the first class of any role on it --
    read off the classes rather than a per-backend literal in the driver, which is what it was
    before #514. A backend no class runs on has no model to default to, and says so."""
    classes = load_task_classes(path)
    on_backend = [task_class for task_class in classes.values() if task_class.backend == name]
    workers = [task_class for task_class in on_backend if task_class.role == "worker"]
    chosen = (workers or on_backend or [None])[0]
    if chosen is None:
        raise KeyError(f"no class in {path} runs on backend '{name}', so it has no default model")
    return chosen.model


def load_task_classes(path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG) -> dict[str, TaskClass]:
    """Closed by pydantic's `extra="forbid"` on both levels: an unknown top-level key or an
    unknown field inside a class fails to load rather than being silently ignored, the same
    "closed partition" discipline the host project's own stamped config uses (`config/
    AGENTS.md`) -- `config/agents.yaml` itself sits outside the m2/s2 stamps, like `backtest.yaml`.
    """
    return load_agents_config(path).classes


def load_role_class(
    role: str, path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG
) -> tuple[str, TaskClass]:
    """The single class that configures a non-worker role, as `(class name, class)`.

    A worker's class is chosen per issue and there are many of them; the validator, the refiner
    and the planner have exactly one each, so "which model does this role run" is answered by
    `config/agents.yaml` alone and never by a second mapping inside a driver
    (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
    Two classes claiming one role is a configuration error, not something to resolve by picking
    the first."""
    matching = sorted(
        (name, task_class)
        for name, task_class in load_task_classes(path).items()
        if task_class.role == role
    )
    if not matching:
        raise KeyError(f"no class in {path} carries `role: {role}`")
    if len(matching) > 1:
        raise KeyError(
            f"{len(matching)} classes carry `role: {role}` ({', '.join(n for n, _ in matching)}); "
            "a role has exactly one"
        )
    return matching[0]


def role_app_slug(role: str, project: ProjectConfig | None = None) -> str:
    """The GitHub App a role signs as. `project.role_apps` names it; a role with no entry falls
    back to the planner's App, which is what the validator and the refiner do until Apps of their
    own exist (agent_os/docs/adr/2026-09-14-a-pr-is-validated-by-a-validator-agent-against-the-issues-
    acceptance-criteria.md)."""
    project = project or load_project()
    return project.role_apps.get(role) or project.planner_app


def human_message_rules(project: ProjectConfig | None = None) -> str:
    """The one wording of the rule for anything an agent addresses to the human, with
    `project.human_language` filled in -- injected via the `__HUMAN_MESSAGE_RULES__` placeholder
    into every role's RULES block (the validator's and the refiner's in `agent_task.sh`, the
    planner's in `planner_task.sh`, the worker's in `worker_task.sh`) so none of them carries its
    own copy of the wording
    (agent_os/docs/adr/2026-09-15-a-question-for-the-human-is-written-in-their-language-and-in-functional-
    terms.md)."""
    language = (project or load_project()).human_language
    return f"""WRITING TO THE HUMAN
Everything addressed to the human -- a `## Doubts` block, a question posted with
`blocked-on-human`, a worker's BLOCKED question comment, the refiner's summary comment -- is
written in {language}.
Explain each doubt in functional language, for a reader who knows the product and how it is
operated but is not reading the code: what has to be decided and why it matters now; the options,
and what each one means in practice -- for the product, the operation, cost, dates, risk; and your
own recommendation. End with the concrete question to answer, preferably one they can answer by
picking an option.
Code identifiers, file paths, labels and issue numbers appear only as a reference after the
explanation, never as the explanation itself.
What the mechanism or another agent parses stays exactly as specified elsewhere in these rules,
in its own spelling: the `@<login>` first line, `## Doubts` and the other section headings, the
`<!-- refiner-summary -->` marker, the `BLOCKED reason=` line in progress.log, issue bodies
written from the template, and the validator's criterion-by-criterion checklist, which is the
worker's next brief."""


def render_human_message(key: str, project: ProjectConfig | None = None, **fields: object) -> str:
    """The one ntfy page wording, rendered from `project.messages[key]` in the human's own
    language. Every caller goes through here and no script holds a message of its own: the
    quota-exhaustion page was a Spanish literal in `agent_guard.py`, so a project configuring
    `human_language: English` still got that one page in Spanish (`agent_os/docs/AGENT_OS.md` §7 row (g)).

    Raises `HumanMessageError` on every way this can fail -- an unknown key, a field the call site
    did not pass, a template `str.format` cannot parse -- because a page nobody receives is
    indistinguishable from a situation that never arose, and none of the three is worth guessing
    past. ONE exception type, so a caller that must not die over a config typo (both of the
    guard's pages, and `issues.py move N review`) catches one thing and says so. The malformed
    case should already have been refused when the config loaded (`_check_placeholders`); it is
    caught here too because a caller can pass a `ProjectConfig` built in code, and `format` raises
    `IndexError` and `ValueError` there, not `KeyError`."""
    project = project or load_project()
    template = project.messages.get(key)
    if template is None:
        raise HumanMessageError(
            f"no project.messages[{key!r}] in config/agents.yaml "
            f"(configured: {sorted(project.messages) or 'none'})"
        )
    try:
        return template.format(**fields)
    except KeyError as missing:
        raise HumanMessageError(
            f"project.messages[{key!r}] needs a field {missing.args[0]!r} nobody passed"
        ) from missing
    except (IndexError, ValueError) as malformed:
        raise HumanMessageError(
            f"project.messages[{key!r}] is not a valid template: {malformed}"
        ) from malformed


# Both path lists -- `project.forbidden_paths` and `mechanism.own_paths` -- hold globs and
# `worker_task.sh collect` audits with `grep -E`, so something has to translate one into the other
# -- and it has to be the same something that renders the paragraph the worker reads, or the two
# halves of the ownership rule describe two different sets again (`agent_os/docs/AGENT_OS.md` §7 rows (a)
# and (s), issue #363).
_ERE_METACHARACTERS = frozenset(r".\[]{}()*+?^$|")


def _escape_ere(text: str) -> str:
    # Escapes exactly the POSIX ERE metacharacters and nothing else: Python's own `re.escape`
    # would also backslash a space, an `&` or a `#`, which `grep -E` leaves undefined.
    return "".join(
        f"\\{character}" if character in _ERE_METACHARACTERS else character for character in text
    )


def _glob_to_ere(pattern: str) -> str:
    r"""One forbidden-path glob as a POSIX ERE fragment, keeping fnmatch's semantics rather than
    glob's: `*` crosses `/`, so `.claude/*` protects `.claude/agents/worker-runner.md` exactly as
    the prefix regex this list replaced did. Translated by hand because `fnmatch.translate` emits
    `(?s:...)\Z`, which is not a pattern `grep -E` accepts."""
    fragment = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            fragment.append(".*")
            index += 1
        elif character == "?":
            fragment.append(".")
            index += 1
        elif character == "[":
            # `index + 2`: `[]]` is a class holding `]`, as it is in fnmatch, not an empty class
            # followed by a stray bracket.
            closing = pattern.find("]", index + 2)
            if closing == -1:
                fragment.append(_escape_ere("["))  # an unclosed `[` is a literal one
                index += 1
            else:
                body = pattern[index + 1 : closing]
                # glob negates a class with a leading `!`, POSIX ERE with a leading `^`
                fragment.append(("[^" + body[1:] if body.startswith("!") else "[" + body) + "]")
                index = closing + 1
        else:
            fragment.append(_escape_ere(character))
            index += 1
    return "".join(fragment)


def _anchored_ere(paths: list[str]) -> str:
    """One `grep -E` pattern over a list of path globs, prefix-anchored and never end-anchored: an
    entry naming a directory protects everything under it, and an entry naming a file protects any
    path starting with that name. Shared by the two lists so they cannot acquire two different
    readings of the same glob.

    Empty when the list is. That is the caller's signal to skip the audit, never a pattern to audit
    with -- `grep -E ''` matches every line, so an empty list would report every file a run touched
    as a violation."""
    patterns = [_glob_to_ere(path) for path in paths]
    return f"^({'|'.join(patterns)})" if patterns else ""


def forbidden_paths_regex(project: ProjectConfig | None = None) -> str:
    """The `grep -E` pattern `worker_task.sh collect` runs over the paths a run changed, built from
    `project.forbidden_paths` and from nothing else -- the host project's list, which a brief can
    never authorize. `mechanism_paths_regex` below renders the second list the same way."""
    return _anchored_ere((project or load_project()).forbidden_paths)


def forbidden_paths_merge_audit_regex(project: ProjectConfig | None = None) -> str:
    """The `grep -E` pattern condition 3 of the merge gate audits a pull request's diff with
    (`agent_os/docs/AGENT_OS.md` §2.4, `.claude/agents/control-plane.md` Duty 4) -- the SUBSET of
    `forbidden_paths` that is not a delivery directory (`project.merge_audit_exempt_paths`, issue
    #476). Derived from `forbidden_paths_regex`'s own list minus the exemption, never from a second
    copy of either: a path added to `forbidden_paths` is audited here the moment it exists, with no
    second edit, unless it is also named in the exemption.

    Empty exactly when `forbidden_paths_regex` would be -- an empty `forbidden_paths`, or one
    entirely exempted -- and the same reading applies: empty is the caller's signal to skip the
    audit, never a pattern that matches every path. `merge_audit_exempt_paths` is intersected with
    `forbidden_paths`, never checked against it: an exempted path this project's `forbidden_paths`
    does not currently carry (a stale entry, or a synthetic config a test built without it) simply
    has nothing to subtract and changes this function's output not at all."""
    project = project or load_project()
    exempt = set(project.merge_audit_exempt_paths)
    return _anchored_ere([path for path in project.forbidden_paths if path not in exempt])


def forbidden_paths_merge_audit_violations(
    changed_paths: list[str], project: ProjectConfig | None = None
) -> list[str]:
    """Which of `changed_paths` (a pull request's own `gh pr diff N --name-only`) condition 3
    refuses a merge for -- the paths matching `forbidden_paths_merge_audit_regex`, in the order
    they were given. Returns `[]` both when nothing matched and when the audit pattern is empty:
    `re.match("", anything)` matches at position 0, unlike an empty `grep -E` pattern the shell
    audit relies on being told to skip, so this function makes the same empty-means-skip reading
    explicit instead of inheriting Python's opposite default."""
    pattern = forbidden_paths_merge_audit_regex(project)
    if not pattern:
        return []
    return [path for path in changed_paths if re.match(pattern, path)]


def mechanism_paths_regex(mechanism: MechanismConfig | None = None) -> str:
    """The `grep -E` pattern over `mechanism.own_paths`, rendered exactly as
    `forbidden_paths_regex` renders the host project's list -- same globs, same anchoring, same
    empty-means-no-audit reading -- because what separates the two lists is the RULE, not the
    matching: a path in this one is a violation only when the issue body does not name it, which is
    the caller's decision to make and not this function's
    (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md)."""
    return _anchored_ere((mechanism or load_mechanism()).own_paths)


def forbidden_paths_rules(project: ProjectConfig | None = None) -> str:
    """The worker's "FILES YOU MUST NOT TOUCH" paragraph, rendered from the same
    `project.forbidden_paths` `forbidden_paths_regex` is built from and injected via the
    `__FORBIDDEN_PATHS_RULES__` placeholder -- the list the worker is told about and the list its
    run is audited against are one list, so neither can drift
    (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).

    The FIRST of the two paragraphs the ownership rule is told in, and it points at the second
    (`mechanism_paths_rules` below): "no brief can authorize one of these" is worth saying because
    a brief CAN authorize one of the mechanism's own files, and one paragraph without the other
    leaves the worker guessing which side of that line its brief fell on. The driver injects the
    pair together or not at all
    (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md).

    Empty when the project forbids nothing, which is why the driver deletes the placeholder's own
    line instead of substituting an empty string into it: a heading with nothing under it would
    read as a rule the worker cannot see."""
    paths = (project or load_project()).forbidden_paths
    if not paths:
        return ""
    listing = textwrap.fill(
        ", ".join(paths) + ".", width=100, break_long_words=False, break_on_hyphens=False
    )
    whose = textwrap.fill(
        "These are the host project's, and no brief can authorize one: a task that needs one is a "
        "defect in the brief. The mechanism's own files below are the only paths an issue body may "
        "name as the target of its work.",
        width=100,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return f"""FILES YOU MUST NOT TOUCH
{listing}
{whose}
If a task genuinely requires one of these, stop and say so rather than working around it."""


def mechanism_paths_rules(mechanism: MechanismConfig | None = None) -> str:
    """The worker's "FILES OF THE MECHANISM'S OWN" paragraph, rendered from the same
    `mechanism.own_paths` `mechanism_paths_regex` is built from and injected via the
    `__MECHANISM_PATHS_RULES__` placeholder -- one list behind the paragraph the worker reads and
    the pattern `collect` judges it by, exactly as the host project's list is behind its own pair
    of halves.

    The SECOND of the two paragraphs, and the conditional half of the rule: a path in this list is
    the target of the work whenever the issue body says it is, which is what lets the mechanism be
    developed by the same machinery that protects everything else. It points back at the first
    paragraph (`forbidden_paths_rules` above), so the driver injects the pair together or not at all
    (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md).

    Empty when the mechanism names none of its own files, and then the driver takes the host
    paragraph down with it rather than render half the contrast."""
    paths = (mechanism or load_mechanism()).own_paths
    if not paths:
        return ""
    listing = textwrap.fill(
        ", ".join(paths) + ".", width=100, break_long_words=False, break_on_hyphens=False
    )
    authorization = textwrap.fill(
        "These are the machinery you run inside, not the host project's, and the tracking epic "
        "exists to change them: your diff may touch one only when the issue body names that path "
        "as the target of the work. When it does, say so in a comment before the first commit that "
        "touches one -- the path, and the body that authorizes it -- so a reviewer reads the "
        "authorization off your work instead of reconstructing it. Editing the copy in your own "
        "worktree cannot break the run you are in, which executes the main checkout's; writing "
        "there stays forbidden by the rule above that forbids writing there at all.",
        width=100,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return f"""FILES OF THE MECHANISM'S OWN
{listing}
{authorization}
If the body does not name one, stop and say so rather than working around it."""


def never_run_rules(project: ProjectConfig | None = None) -> str:
    """The "COMMANDS YOU MUST NEVER RUN" paragraph, rendered from `project.never_run` and injected
    via the `__NEVER_RUN_RULES__` placeholder into the worker's, the validator's and the refiner's
    RULES. One list for three blocks, because the same verbs spelled once per block had already
    drifted -- the validator's copy had lost one of the six (`agent_os/docs/AGENT_OS.md` §7 row (b), issue
    #363). Each bullet carries its own reason: a prohibition nobody can justify is the first one a
    task talks itself out of.

    Empty when the project forbids no command, which is why the drivers delete the placeholder's
    own line instead of substituting an empty string into it: a heading with nothing under it
    would read as a rule the agent cannot see."""
    commands = (project or load_project()).never_run
    if not commands:
        return ""
    bullets = "\n".join(
        textwrap.fill(
            f"`{item.command}` -- {item.reason}",
            width=100,
            initial_indent="- ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        for item in commands
    )
    return f"""COMMANDS YOU MUST NEVER RUN
{bullets}
The reason is part of the rule: it is what you judge an edge case against. If a task genuinely
needs one of these, stop and say so rather than working around it."""


def worker_environment_rules(project: ProjectConfig | None = None) -> str:
    """The worker's environment paragraph, rendered from `project.worker_environment` -- the very
    variables `worker_task.sh` exports into the worker's own process -- and injected via the
    `__WORKER_ENVIRONMENT_RULES__` placeholder. The NAMES come from the config and their values
    never do: a worker that needs one reads its own environment, so the sentence that used to spell
    the shared server's port is gone rather than moved, and the paragraph is now rendered from the
    same list the export loop reads instead of describing it from memory
    (`agent_os/docs/AGENT_OS.md` §7 row (a), issue #363).

    Empty when the project exports nothing, which is why the driver deletes the placeholder's own
    line instead of substituting an empty string into it: a worker told its connection is read-only
    by default would trust a guard that does not exist."""
    configuration = project or load_project()
    if not configuration.worker_environment:
        return ""
    names = ", ".join(f"`{key}`" for key in configuration.worker_environment)
    exported = (
        f"These variables are exported into your own process before you start, holding the values "
        f"the project chose: {names}. Read a value a command of yours needs -- a host, a port -- "
        f"out of the variable itself, never out of a number you remember from a prompt: what you "
        f"remember is a version of the config that may no longer be in force."
    )
    shared = (
        f"What they reach is shared, not a copy made for this run, and your own connection to it "
        f"is read-only by default for anything outside a `{configuration.test_command}` run "
        f"(above) -- an accidental write against the live data fails by itself, without you having "
        f"to remember not to make it. Measure in memory or with dry runs. If a write seems "
        f"necessary, stop and say so."
    )
    accident_guard = (
        "This is an accident guard, not an intent guard: your worktree's own configuration may "
        "still hold credentials that write, so what is exported only catches a write you did not "
        "mean to make -- the rule that actually stops a deliberate one is never override an "
        "exported variable yourself."
    )
    dead_connection = (
        "If a connection dies underneath you, say so rather than silently retrying into a "
        "half-measured result: the other agent may have restarted what you were connected to."
    )
    bullets = "\n".join(
        textwrap.fill(
            bullet,
            width=100,
            initial_indent="- ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        for bullet in (exported, shared, accident_guard, dead_connection)
    )
    return f"""THE ENVIRONMENT YOU RUN IN IS CONFIGURED FOR YOU, AND READ-ONLY BY DEFAULT
{bullets}"""


# Where every role's prompt lives: one template per role, beside the package rather than inside a
# driver's heredoc, so the text a host reads is a file it can diff and the drivers carry none of it
# (#509). `agent_os/tests/golden/` holds what each one renders to for the host that owns this
# checkout, which is what proves a move of the text changed nothing an agent reads.
PROMPTS_DIR = AGENT_OS_DIR / "prompts"
PROMPT_ROLES = ("worker", "validator", "refiner", "planner")

# The one marked extension point: where a host's own paragraphs are appended verbatim, from the
# file `project.prompt_extras` names for that role. A host that names none renders nothing there,
# and the section simply does not exist in that run's prompt.
PROJECT_EXTRAS_PLACEHOLDER = "__PROJECT_EXTRAS__"

# What a placeholder looks like, so a value nobody supplied is a refusal rather than a prompt an
# agent reads `__WORKTREE__` in. No prompt's own prose carries this shape.
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


def _substitute_block(text: str, placeholder: str, value: str) -> str:
    """One placeholder, substituted the way the shell drivers substituted it before this function
    existed: inline where it stands inside a line, and as a paragraph of its own where it stands
    alone on one -- an empty value then taking its own line AND the blank line that separated it
    with it, so a host that configures nothing keeps the single blank line every other section
    boundary has instead of a stub heading or a gap twice as wide as the one it fills."""
    if placeholder not in text:
        return text
    block = value.strip("\n")
    lines = text.split("\n")
    rendered: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() == placeholder:
            if block:
                rendered.extend(block.split("\n"))
            elif (
                rendered
                and not rendered[-1].strip()
                and index + 1 < len(lines)
                and not lines[index + 1].strip()
            ):
                # The placeholder was a paragraph between two others: drop one of the two blank
                # lines that fenced it, never both.
                rendered.pop()
            continue
        rendered.append(line.replace(placeholder, block))
    return "\n".join(rendered)


def prompt_extras_path(role: str, project: ProjectConfig | None = None) -> pathlib.Path | None:
    """The host-owned file whose text is appended at this role's extension point, or None when the
    host names none. Relative to the HOST project's root, never to this package."""
    configured = (project or load_project()).prompt_extras.get(role)
    return HOST_ROOT / configured if configured else None


def prompt_substitutions(
    project: ProjectConfig | None = None, mechanism: MechanismConfig | None = None
) -> dict[str, str]:
    """Every placeholder a role's prompt carries that config alone answers, keyed WITHOUT the
    surrounding underscores. What is missing here is what only the run knows -- the main checkout's
    path, the throwaway worktree, the line naming the backend a review was written on -- and the
    driver passes those in.

    The two ownership paragraphs are the one exception to "each stands on its own": together they
    state the rule as a contrast, so the pair renders whole or not at all (#390). The test is the
    two audit regexes, each empty exactly when its own list is, which is the same test
    `worker_task.sh` applied before the drivers stopped rendering their own prompts."""
    project = project or load_project()
    mechanism = mechanism or load_mechanism()
    both_lists_configured = bool(forbidden_paths_regex(project)) and bool(
        mechanism_paths_regex(mechanism)
    )
    return {
        "HUMAN_LOGIN": project.human_login,
        "HUMAN_MESSAGE_RULES": human_message_rules(project),
        "TEST_COMMAND": project.test_command,
        "FORBIDDEN_PATHS_RULES": forbidden_paths_rules(project) if both_lists_configured else "",
        "MECHANISM_PATHS_RULES": mechanism_paths_rules(mechanism) if both_lists_configured else "",
        "NEVER_RUN_RULES": never_run_rules(project),
        "WORKER_ENVIRONMENT_RULES": worker_environment_rules(project),
    }


def render_prompt(
    role: str,
    substitutions: dict[str, str] | None = None,
    extras_path: pathlib.Path | str | None = None,
) -> str:
    """One role's whole prompt: its template, the host's own text at the extension point, and every
    placeholder filled.

    Two refusals, both loud, because either one reaches an agent as prose it cannot act on: an
    extras file the config names and the filesystem does not have, and a placeholder still standing
    in the rendered text because nothing supplied a value for it."""
    template = PROMPTS_DIR / f"{role}.md"
    if not template.is_file():
        raise KeyError(f"no prompt template for role {role!r}: {template} does not exist")
    extras = ""
    if extras_path is not None:
        path = pathlib.Path(extras_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"project.prompt_extras names {path} for role {role!r}, and there is no such file"
            )
        extras = path.read_text()
    # The extras go in FIRST, so a host's own paragraph may carry the mechanism's placeholders --
    # `__TEST_COMMAND__` is the one the first host's worker file uses -- and is filled from the
    # same config as the template around it.
    text = _substitute_block(template.read_text(), PROJECT_EXTRAS_PLACEHOLDER, extras)
    for name, value in (substitutions or {}).items():
        text = _substitute_block(text, f"__{name}__", value)
    unresolved = sorted(set(PLACEHOLDER_RE.findall(text)))
    if unresolved:
        raise KeyError(
            f"the {role} prompt still carries {', '.join(unresolved)} after rendering: "
            "nothing supplied a value"
        )
    return text.rstrip("\n") + "\n"


def mechanism_logins(project: ProjectConfig | None = None) -> set[str]:
    """Every GitHub login the MECHANISM itself speaks as: one App slug per identity -- the
    planner's, one per worker backend, one per one-shot role -- and a GitHub App comments as
    `<slug>[bot]`. Lowercased, because GitHub logins are case-insensitive and nothing else in this
    module may depend on how a config file spelled one."""
    project = project or load_project()
    slugs = {
        project.planner_app,
        *(backend.app for backend in project.backends.values()),
        *project.role_apps.values(),
    }
    logins = set()
    for slug in slugs:
        if not slug:
            continue
        logins.add(slug.lower())
        logins.add(f"{slug.lower()}[bot]")
    return logins


def is_human_comment(author: str, project: ProjectConfig | None = None) -> bool:
    """Was this comment written by the one human, rather than by the mechanism talking to itself?

    A `status:blocked-on-human` issue is unblocked by a reply, and on 2026-09-16 the planner's own
    "I am waiting for you" comment counted as one: the issue went back into circulation with the
    question still unanswered. `project.human_login` is therefore the positive test -- only that
    login is the human -- and where a project has not named one, the fallback is the negative one:
    anybody who is not one of the mechanism's own identities. A project that configures neither
    would have no way to tell the two apart at all."""
    author = (author or "").strip().lower()
    if not author:
        return False
    project = project or load_project()
    if project.human_login:
        return author == project.human_login.lower()
    return author not in mechanism_logins(project)


def label_names(issue: dict) -> set[str]:
    """The label names of one `gh ... --json labels` row, whichever shape it came in: a listing's
    row (`labels` may be absent or null) or a single `gh issue view`'s object. One reader, because
    five copies of the same comprehension is five places for `or []` to be forgotten."""
    return {label["name"] for label in issue.get("labels") or []}


def parse_budget_line(body: str) -> str | None:
    match = BUDGET_LINE_RE.search(body or "")
    return match.group(1) if match else None


# `Blocked by #12` on a line of its own, anywhere in the body. One line per blocker: a list is a
# list, not a comma-separated sentence the parser has to guess at.
BLOCKED_BY_RE = re.compile(r"^\s*Blocked by\s+#(\d+)\s*$", re.MULTILINE | re.IGNORECASE)


def blocking_issue_numbers(body: str) -> list[int]:
    return [int(n) for n in BLOCKED_BY_RE.findall(body or "")]


# The shape of a task or bug body, in the order the sections must appear
# (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
# state.md). `.github/ISSUE_TEMPLATE/task.md` and `bug.md` scaffold exactly these, followed by the
# `<!-- budget: <class> -->` line -- the eighth section, checked separately because it also has
# to resolve against `config/agents.yaml`.
REQUIRED_SECTIONS = (
    "## Objective",
    "## Acceptance criteria",
    "## Stages",
    "## Context",
    "## Not included",
    "## Dependencies",
    "## Definition of done",
)


# A stage checklist line, `- [ ]` or `- [x]` (either case), the same shape GitHub itself renders
# as a checkbox -- shared by `section_failures`' "is there at least one" check and `parse_stages`'
# extraction so the two can never disagree about what counts as a stage line.
STAGE_CHECKLIST_LINE_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)


def _section_body(body: str, heading: str) -> str | None:
    """The text between one `## heading` line and the next `##` heading (or the end of the body).
    `None` when the heading itself is not present -- distinct from an empty section, which is `""`.
    """
    lines = (body or "").splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return None
    end = next(
        (i for i in range(start, len(lines)) if lines[i].strip().startswith("##")), len(lines)
    )
    return "\n".join(lines[start:end])


def parse_stages(body: str) -> list[str]:
    """Stage titles in order, from the checklist lines of the issue's `## Stages` section -- the
    checkbox itself is stripped, only the title text kept. Empty when the section is absent (an
    issue not yet staged) or present but empty (caught separately by `section_failures` below as
    `stages: no checklist line`)."""
    section = _section_body(body, "## Stages")
    if section is None:
        return []
    return [match.group(1) for match in STAGE_CHECKLIST_LINE_RE.finditer(section)]


def section_failures(body: str) -> list[str]:
    """One line per section that is missing, plus one line if the sections present are out of
    order, plus `stages: no checklist line` if `## Stages` is present but empty of one. Matching is
    on a heading line of its own, so a section named inside a sentence or in a fenced code block
    does not count as the section being there."""
    headings = [line.strip() for line in (body or "").splitlines() if line.strip().startswith("##")]
    failures = [f"missing section: {s}" for s in REQUIRED_SECTIONS if s not in headings]
    present = [s for s in REQUIRED_SECTIONS if s in headings]
    in_body_order = [h for h in headings if h in REQUIRED_SECTIONS]
    # A section repeated is its own defect; dict.fromkeys keeps the FIRST occurrence, which is the
    # one an out-of-order report should point at.
    if list(dict.fromkeys(in_body_order)) != present:
        failures.append(
            "sections out of order: expected "
            + " then ".join(present)
            + ", found "
            + " then ".join(dict.fromkeys(in_body_order))
        )
    if "## Stages" in headings and not parse_stages(body):
        failures.append("stages: no checklist line")
    return failures


def stages_completed(commit_subjects: list[str]) -> int:
    """The highest stage N a `stage N/M: <title>` commit subject claims done, 0 if none of them
    match. Progress is read off the branch's own commits, never written by an agent
    (agent_os/docs/adr/2026-09-14-driver-writes-mechanical-state-agent-writes-cooperative-state.md) --
    `stage_total_from_subjects` is not needed alongside this because the total is `len(
    parse_stages(body))`, already known from the issue body without touching git."""
    completed = 0
    for subject in commit_subjects:
        match = re.match(r"^stage (\d+)/(\d+):", subject or "")
        if match:
            completed = max(completed, int(match.group(1)))
    return completed


def cumulative_cost_usd(
    jsonl_paths: list[pathlib.Path], *, parser: StreamParser | None = None
) -> float:
    """Sum of `total_cost_usd` across many stage `.jsonl` logs -- what `max_cost_usd` is checked
    against for the whole issue, as the sum of its stage processes (#375), archived files under
    `.cache/spend/<issue>/` plus the live one. A file with no terminal `result` event (killed
    before finishing) contributes 0, read by the same stream parser a single log's report uses."""
    parser = parser or backend_stream_parser()
    total = 0.0
    for path in jsonl_paths:
        result = parser.result_usage(read_events(path))
        if result:
            total += float(result.cost_usd or 0)
    return total


def cumulative_total_tokens(
    jsonl_paths: list[pathlib.Path], *, parser: StreamParser | None = None
) -> int:
    """Sum of the terminal `result` events' tokens across many stage `.jsonl` logs -- what
    `max_total_tokens` is checked against for the whole issue, the same scope
    `cumulative_cost_usd` above has, and the one of the two ceilings a Qwen class can cross
    because its `result` event reports no `total_cost_usd` at all (#387).

    A file with no terminal `result` event contributes 0, exactly as the dollar sum does, so the
    figure UNDERCOUNTS an issue that had a stage cut before it finished: the tokens that stage
    spent are in its archived log and nothing here reads them. Stated rather than papered over --
    the alternative is trusting a per-turn sum, which no terminal event corroborates."""
    parser = parser or backend_stream_parser()
    total = 0
    for path in jsonl_paths:
        result = parser.result_usage(read_events(path))
        if result:
            total += result.total_tokens
    return total


def budget_failures(body: str, task_classes: dict[str, TaskClass]) -> list[str]:
    task_class = parse_budget_line(body or "")
    if not task_class:
        return ["missing the `<!-- budget: <class> -->` line"]
    if task_class not in task_classes:
        known = ", ".join(sorted(task_classes)) or "none"
        return [
            f"budget class '{task_class}' is not defined in config/agents.yaml (known: {known})"
        ]
    return []


def validate_issue_body(
    body: str, *, task_classes: dict[str, TaskClass], open_issue_numbers: set[int]
) -> list[str]:
    """Every mechanical reason this body is not a brief an agent could start from, one failure per
    line, empty when it is one. THE single implementation: `issues.py validate` prints these lines
    and `is_dispatchable` below asks only whether the list is empty, so "Ready for AI" and "the
    validator passes" can never drift apart
    (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
    state.md).

    "Closed" for a blocker is read as "not in the set of open issue numbers", so one listing
    answers it for every issue at once instead of one `gh issue view` per blocker; a number that
    does not exist reads as closed, which is the safe direction (a human still looks at the issue).
    """
    failures = section_failures(body) + budget_failures(body, task_classes)
    failures += [
        f"blocked by #{number}, which is still open"
        for number in blocking_issue_numbers(body or "")
        if number in open_issue_numbers
    ]
    return failures


def is_dispatchable(
    issue: dict,
    *,
    task_classes: dict[str, TaskClass],
    open_issue_numbers: set[int],
    labels: LabelVocabulary | None = None,
) -> bool:
    """Pure: does this issue, as `gh issue list --json number,state,labels,body` describes it,
    meet every mechanical condition for a worker to be started on it right now
    (agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md)?

    Open, labeled `status:ready`, not `status:blocked-on-human`, and `validate_issue_body` above
    finding nothing wrong with the body -- which is what "Ready for AI" is defined as
    (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
    state.md): the same function `issues.py validate N` runs, never a second copy of the rules.

    Note this says nothing about whether a *slot* is free -- one running issue per `module:`
    label, one issue per backend -- which is the planner's own dispatch rule, not a property of
    the issue.
    """
    labels = labels or LabelVocabulary()
    if (issue.get("state") or "OPEN").upper() != "OPEN":
        return False
    names = label_names(issue)
    if labels.ready not in names or labels.blocked_on_human in names:
        return False
    return not validate_issue_body(
        issue.get("body") or "",
        task_classes=task_classes,
        open_issue_numbers=open_issue_numbers,
    )


def needs_refinement(
    issue: dict,
    *,
    task_classes: dict[str, TaskClass],
    open_issue_numbers: set[int],
    labels: LabelVocabulary | None = None,
) -> bool:
    """Does this issue need the REFINER before anything else can happen to it: open, carrying
    `status:refine`, not waiting on a human, and its body has a STRUCTURAL defect the refiner can
    actually fix -- a missing/misordered section or an unresolvable budget line
    (`section_failures` + `budget_failures`, never the full `validate_issue_body`)
    (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md).

    `open_issue_numbers` is accepted only for call-site compatibility with `is_dispatchable` and
    `promotable_to_ready`, which do need it -- an issue an open `Blocked by #N` line makes
    undispatchable is not a defect the refiner wrote or can rewrite away, so counting it here
    would relaunch the refiner on an issue #356 already looked correct (its body was template-
    conformant, just blocked on #323), for no gain. An issue that already validates in full while
    carrying `status:refine` is not *in need of* refining -- it is a candidate for
    `promotable_to_ready` below instead, which still requires no open blocker."""
    del open_issue_numbers  # structural failures only -- see docstring
    labels = labels or LabelVocabulary()
    if (issue.get("state") or "OPEN").upper() != "OPEN":
        return False
    names = label_names(issue)
    if labels.refine not in names or labels.blocked_on_human in names:
        return False
    body = issue.get("body") or ""
    return bool(section_failures(body) + budget_failures(body, task_classes))


def promotable_to_ready(
    issue: dict,
    *,
    parent_labels: set[str] | None,
    task_classes: dict[str, TaskClass],
    open_issue_numbers: set[int],
    labels: LabelVocabulary | None = None,
) -> bool:
    """Can this refined issue be moved to `status:ready` mechanically, with no human looking at it:
    open, carrying `status:refine`, not waiting on a human, its body passing `validate_issue_body`
    with NO failures, AND its parent carrying `auto-ready`. An issue with no parent is never
    promotable this way -- a raw issue the refiner split by hand has no feature above it to have
    opted in, so the human promotes it
    (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md)."""
    labels = labels or LabelVocabulary()
    if (issue.get("state") or "OPEN").upper() != "OPEN":
        return False
    names = label_names(issue)
    if labels.refine not in names or labels.blocked_on_human in names:
        return False
    if parent_labels is None or labels.auto_ready not in parent_labels:
        return False
    return not validate_issue_body(
        issue.get("body") or "",
        task_classes=task_classes,
        open_issue_numbers=open_issue_numbers,
    )


def read_events(path: pathlib.Path | str) -> list[dict]:
    events = []
    for line in pathlib.Path(path).read_text(errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def backend_stream_parser(
    backend: str | None = None, *, project: ProjectConfig | None = None
) -> StreamParser:
    """`backend`'s parser -- its `backends.<name>.stream` entry, validated at config load (#514) --
    or the default one for a log whose backend the caller does not know, or whose backend is no
    longer configured: how every log was read before the parsers had names."""
    if backend is None:
        return get_stream_parser(DEFAULT_STREAM_PARSER)
    configured = (project or load_project()).backends.get(backend)
    return get_stream_parser(configured.stream if configured else DEFAULT_STREAM_PARSER)


def usage_summary(events: list[dict], *, parser: StreamParser | None = None) -> UsageSummary:
    return (parser or backend_stream_parser()).turn_usage(events)


def usage_failed(summary: UsageSummary) -> bool:
    """A run the API refused before the first turn reports `subtype: "success"` -- the refusal
    arrives as the RESULT TEXT, not as an error subtype. A run with no turns produced nothing,
    whatever the subtype says."""
    if summary.turns == 0:
        return True
    if not summary.result:
        return False
    text = summary.result.get("result") or ""
    return bool(summary.result.get("is_error")) or text.startswith("[API Error")


def quota_exhausted(events: list[dict], *, parser: StreamParser | None = None) -> str | None:
    """The reason string the backend refused the run with, or None if the quota looks open --
    the stream parser's `quota_verdict` (`agent_os.streams`), which carries the authority order the
    ADR sets."""
    return (parser or backend_stream_parser()).quota_verdict(events).reason


def quota_status(
    events: list[dict], *, parser: StreamParser | None = None
) -> Literal["allowed", "exhausted"]:
    """The same verdict as `quota_exhausted` above, as the word the guard's tick compares between
    ticks and the driver's stage gate reads off a finished stage's log (#375). Per
    agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md --
    Qwen's stream carries no `rate_limit_event`, so its runs read `allowed` unless their terminal
    `result` itself carries the refusal."""
    return (parser or backend_stream_parser()).quota_verdict(events).status


# -------------------------------------------------------------------------------------------------
# Which backend a role's launch gets (#425). The verdict is the guard's own persisted one, read off
# disk, and never an agent's claim about its own quota
# (agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md): the
# role has not run yet when this is decided, so there is no stream of its own to read, and the one
# authority on Claude's window that exists at that moment is what the guard last saw in the stream
# of the last Claude run it watched.
# -------------------------------------------------------------------------------------------------

QUOTA_VERDICT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class QuotaVerdict:
    """The guard's persisted verdict on one backend's quota, and how long ago it was observed.

    `age_seconds` is None exactly when there is no verdict to age -- no file, or a file carrying no
    `last_quota_status` -- which is why `status` then reads `unknown` rather than `allowed`: the
    absence of a refusal is not a permission."""

    status: Literal["allowed", "exhausted", "unknown"]
    age_seconds: float | None
    source: pathlib.Path | None
    reason: str


def quota_verdict_file(
    backend: str, *, cache_dir: pathlib.Path | str | None = None
) -> pathlib.Path:
    """`.cache/agent_guard_<backend>.json` -- the guard's own per-backend bookkeeping file, written
    by `agent_guard._save_bookkeeping` and named here rather than in a driver so that the reader and
    the writer can never drift about where the verdict lives. `cache_dir` (or the `WORKER_CACHE_DIR`
    environment, the same override `agent_guard.cache_dir` and `worker_task.sh` honour) relocates
    it, so a test reads a sandbox instead of the live `.cache` (#360)."""
    if cache_dir is None:
        cache_dir = os.environ.get("WORKER_CACHE_DIR") or (HOST_ROOT / ".cache")
    return pathlib.Path(cache_dir) / f"agent_guard_{backend}.json"


def _verdict_age_phrase(age_seconds: float | None) -> str:
    if age_seconds is None:
        return ""
    minutes = age_seconds / 60
    return "<1 min old" if minutes < 1 else f"{minutes:.0f} min old"


def read_persisted_quota_verdict(
    backend: str,
    *,
    cache_dir: pathlib.Path | str | None = None,
    now: datetime | None = None,
) -> QuotaVerdict:
    """What the guard last saw of `backend`'s quota, and how long ago it saw it.

    The age is the FILE'S mtime, and that is not an approximation: the guard module is the file's
    one writer (#429), and each write leaves the mtime at the moment of the observation it records
    -- a live worker's tick at its own now (`agent_guard._tick_backend`), a role run at the moment its
    backend exited, off the run's exit marker (`agent_guard.fold_role_quota_observations`). This
    function only reads. A file nobody
    has rewritten for a while is therefore a verdict nobody has refreshed for exactly that long --
    including the case that matters most, where the run ended and the guard stopped having a stream
    to look at.

    Anything short of a readable verdict reads as `unknown`: a missing file (no run of this backend
    since the cache was cleared), an unparseable one, or one written before `last_quota_status`
    existed. `unknown` is never `allowed` and never `exhausted` -- it is the absence of a
    measurement, and the launch treats it as its own case."""
    path = quota_verdict_file(backend, cache_dir=cache_dir)
    if not path.is_file():
        return QuotaVerdict(
            QUOTA_VERDICT_UNKNOWN, None, path, f"no {backend} quota verdict on disk at {path}"
        )
    try:
        recorded = json.loads(path.read_text())
        mtime = path.stat().st_mtime
    except (OSError, ValueError) as error:
        return QuotaVerdict(QUOTA_VERDICT_UNKNOWN, None, path, f"{path} is unreadable: {error}")
    status = recorded.get("last_quota_status") if isinstance(recorded, dict) else None
    if status not in ("allowed", "exhausted"):
        return QuotaVerdict(
            QUOTA_VERDICT_UNKNOWN,
            None,
            path,
            f"{path} carries no last_quota_status the guard ever wrote",
        )
    observed_at = datetime.fromtimestamp(mtime, UTC)
    age = ((now or datetime.now(UTC)) - observed_at).total_seconds()
    return QuotaVerdict(
        status,
        age,
        path,
        f"the guard's persisted {backend} quota verdict reads {status} "
        f"({_verdict_age_phrase(age)})",
    )


@dataclass(frozen=True)
class RoleLaunch:
    """The answer one role's launch acts on: which backend runs it, which model that is, whether it
    is a substitution, which ceilings bind the run, and the sentence a driver prints so a human
    reading the log can see the decision was made and on what."""

    backend: str
    model: str
    substituted: bool
    ceilings: tuple[str, ...]
    reason: str


def role_launch_plan(
    task_class: TaskClass,
    quota_verdict: str,
    verdict_age_seconds: float | None,
    *,
    verdict_ttl_seconds: float,
) -> RoleLaunch:
    """(class, quota verdict, verdict age) -> the backend to launch, and why (#425).

    Three answers, and the order they are decided in:

    - `exhausted`, fresh, and the class declares a fallback: run the fallback. This is the whole
      point of the field -- an exhausted quota stops nothing that names a way round it.
    - `exhausted` and the verdict is older than its TTL: read as UNKNOWN and run the class's own
      backend. Claude's window reopens on its own schedule and nothing here observes that, so a
      verdict nobody has refreshed is a claim about a moment that has passed; believing it would
      substitute a review the merge gate rests on the independence of, on evidence that may be
      hours out of date. A false start costs one refused run and the page that follows it.
    - everything else -- `allowed`, `unknown`, or `exhausted` with no fallback declared: run the
      class's own backend, exactly as before this existed. The `exhausted`-with-no-fallback case is
      the guard's `quota_exhausted_no_fallback` page, which is a human's to receive, not a launch's
      to route round.
    """
    fallback = task_class.fallback
    stale = verdict_age_seconds is None or verdict_age_seconds > verdict_ttl_seconds
    age = _verdict_age_phrase(verdict_age_seconds)
    ttl = f"{verdict_ttl_seconds / 60:.0f} min"
    if quota_verdict == "exhausted" and fallback is not None and not stale:
        return RoleLaunch(
            backend=fallback.backend,
            model=fallback.model,
            substituted=True,
            ceilings=tuple(fallback.ceilings),
            reason=(
                f"the guard's verdict reads exhausted ({age}), inside its {ttl} TTL, and the class "
                f"declares {fallback.backend} as its fallback -- substituting {fallback.model} "
                f"for {task_class.model}"
            ),
        )
    if quota_verdict == QUOTA_VERDICT_UNKNOWN:
        reason = f"no verdict to read -- launching {task_class.backend}"
    elif quota_verdict != "exhausted":
        reason = f"the guard's verdict reads {quota_verdict} ({age})"
        reason += f" -- launching {task_class.backend}"
    elif fallback is None:
        reason = (
            f"the guard's verdict reads exhausted ({age}) and the class declares no fallback -- "
            f"launching {task_class.backend}; the page that follows is the human's to receive"
        )
    else:
        reason = (
            f"the guard's verdict reads exhausted but its {age} is past the {ttl} TTL -- reading "
            f"it as unknown and launching {task_class.backend}"
        )
    return RoleLaunch(
        backend=task_class.backend,
        model=task_class.model,
        substituted=False,
        ceilings=tuple(CEILING_NAMES),
        reason=reason,
    )


def role_launch(
    role: str,
    *,
    path: pathlib.Path | str = DEFAULT_AGENTS_CONFIG,
    cache_dir: pathlib.Path | str | None = None,
    now: datetime | None = None,
) -> tuple[str, RoleLaunch, QuotaVerdict]:
    """One role's launch decision, resolved end to end: `(class name, plan, verdict)`. The thin
    composition the two drivers call through `role-backend` below, so the class lookup, the verdict
    read and the TTL all have one implementation and a shell never assembles them itself."""
    class_name, task_class = load_role_class(role, path)
    verdict = read_persisted_quota_verdict(task_class.backend, cache_dir=cache_dir, now=now)
    ttl_seconds = load_mechanism(path).quota_verdict_ttl_minutes * 60
    plan = role_launch_plan(
        task_class, verdict.status, verdict.age_seconds, verdict_ttl_seconds=ttl_seconds
    )
    if verdict.status == QUOTA_VERDICT_UNKNOWN:
        # There was nothing to decide from, so the explanation a human reads is the verdict's own:
        # which file was looked for and why it answered nothing.
        plan = RoleLaunch(plan.backend, plan.model, plan.substituted, plan.ceilings, verdict.reason)
    return class_name, plan, verdict


RUNS_TSV_HEADER = "ts\tcontext\tmodel\tnum_turns\ttotal_cost_usd"

# A role run's exit marker (#429): `<stamp>.log.exited`, one ISO-8601 UTC timestamp, written by the
# driver the moment the backend process returns. The log itself is no clock for that moment: the
# detached half keeps appending to it after the backend's `result` -- the exit hook's `wake`, which
# runs a whole planner synchronously, and the worktree's removal -- so its mtime can be minutes
# later than the refusal it records, and later than the planner run it started.
ROLE_RUN_EXIT_MARKER_SUFFIX = ".exited"


def role_run_exit_marker(log_path: pathlib.Path | str) -> pathlib.Path:
    log_path = pathlib.Path(log_path)
    return log_path.with_name(log_path.name + ROLE_RUN_EXIT_MARKER_SUFFIX)


def write_role_run_exit_marker(
    log_path: pathlib.Path | str, *, now: datetime | None = None
) -> pathlib.Path:
    """Written once per run and never again: a second call for the same log is a driver bug, and
    it would move the observation's time, so it is refused rather than overwritten."""
    marker = role_run_exit_marker(log_path)
    with marker.open("x") as handle:
        handle.write((now or datetime.now(UTC)).isoformat() + "\n")
    return marker


def read_role_run_exit_marker(log_path: pathlib.Path | str) -> datetime | None:
    """When the run's backend exited, or None: no marker (the run is still going, it died before
    its backend returned, or it predates the marker), or one that does not read as an aware
    timestamp -- which dates nothing, and so observes nothing."""
    try:
        text = role_run_exit_marker(log_path).read_text().strip()
        exited_at = datetime.fromisoformat(text)
    except (OSError, ValueError):
        return None
    return exited_at if exited_at.tzinfo is not None else None


def last_result_event(log_text: str) -> dict | None:
    """The last `{"type":"result",...}` line of a `claude -p --output-format stream-json` log.
    The log also carries the driver's own plain-text lines (identity, model, context), so it is
    scanned line by line and anything that is not JSON is skipped rather than failing the parse."""
    result = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def planner_run_row(log_text: str, *, ts: str, context: str, model: str) -> str:
    """One `.cache/planner/runs.tsv` line per planner run: what it was woken for and what it cost
    (agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md).
    A run whose log has no terminal `result` (killed, crashed, a stub backend) still gets its
    line, with empty cost fields -- a missing row would read as "the run never happened"."""
    result = last_result_event(log_text) or {}
    turns = result.get("num_turns")
    cost = result.get("total_cost_usd")
    flat = " ".join((context or "").split())[:120]
    return "\t".join(
        [
            ts,
            flat,
            model,
            "" if turns is None else str(turns),
            "" if cost is None else f"{float(cost):.4f}",
        ]
    )


def _resolve_max_context(body: str | None) -> tuple[int | None, str | None]:
    """The `max_context` ceiling of the class the issue's own `<!-- budget: --> ` line resolves
    to, for the report below -- the same resolution `_resolve_budget` uses, so the two can never
    name different classes for the same issue (#483). `None` plus a reason (no issue body on disk,
    a class the config does not define) means there is no ceiling to report, never a silent
    fall back to another class's number or a backend default."""
    if not body:
        return None, "no issue body recorded for this run"
    failures = budget_failures(body, load_task_classes())
    if failures:
        return None, "; ".join(failures)
    return load_task_classes()[parse_budget_line(body)].max_context, None


def _print_usage_report(events_path: str, body: str | None) -> None:
    events = read_events(events_path)
    if not events:
        print("  (no events yet)")
        return
    summary = usage_summary(events)
    budget, unresolved_reason = _resolve_max_context(body)
    print(f"  session   {summary.session_id}")
    print(f"  turns     {summary.turns}")
    if budget is None:
        print(f"  context   {summary.context:,} tokens  (budget unresolved -- {unresolved_reason})")
    else:
        over = summary.context > budget
        print(
            f"  context   {summary.context:,} tokens  (budget {budget:,}) "
            f"{'** OVER BUDGET -- finish the piece and restart on the rest **' if over else 'ok'}"
        )
    print(f"  output    {summary.output_tokens:,} tokens")
    if summary.result:
        stats = summary.result.get("usage") or {}
        text = summary.result.get("result") or ""
        failed = usage_failed(summary)
        total = result_total_tokens(summary.result)
        cost = summary.result.get("total_cost_usd")
        print(
            f"  RESULT    {summary.result.get('subtype')}  "
            f"{summary.result.get('duration_ms', 0) / 1000:.0f}s  "
            f"total {total:,} tokens, cached {stats.get('cache_read_input_tokens', 0):,}"
            + (f", ${cost:.2f}" if cost is not None else "")
            + ("  ** THE RUN PRODUCED NOTHING **" if failed else "")
        )
        if failed and text:
            print(f"  WHY       {text[:300]}")


def _resolve_budget(body: str, field: str = "name") -> int:
    """Used by `worker_task.sh start` to refuse a dispatch whose issue has no resolvable budget
    (agent_os/docs/adr/2026-09-14-agent-spend-is-tokens-not-time-and-needs-a-written-budget.md). Same
    `budget_failures` the validator runs, so the driver and `issues.py validate` cannot disagree
    about what a resolvable budget is. `--field` prints one field of the resolved class instead of
    its name -- the driver's stage gate asks for `max_cost_usd` and for `max_total_tokens`, the
    issue's two whole-issue ceilings, and cuts on whichever the log can actually report (#375,
    #387)."""
    failures = budget_failures(body, load_task_classes())
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    name = parse_budget_line(body)
    print(name if field == "name" else getattr(load_task_classes()[name], field))
    return 0


def _print_role_backend(role: str, *, cache_dir: str | None) -> int:
    """One TAB-separated line for the launch gate of a one-shot role and of the planner: the
    backend that runs it, the model that is, whether the run is a substitution, the ceilings that
    bind it, and the sentence saying why -- `agent_task.sh` and `planner_task.sh` read the four
    first fields with `IFS=$'\\t' read` and print the last one as an identity line (#425).

    Exits 1 with the reason on stderr, exactly as `backend-executable` does: a config that does not
    load, or a role no class claims, stops the driver where it can still be read as a refusal
    instead of becoming an empty command line the shell answers 127 with."""
    try:
        _class_name, plan, _verdict = role_launch(role, cache_dir=cache_dir)
    except (KeyError, ValidationError, yaml.YAMLError, OSError) as error:
        print(
            f"cannot resolve the backend for role '{role}': "
            f"{DEFAULT_AGENTS_CONFIG} does not answer\n{error}",
            file=sys.stderr,
        )
        return 1
    # The reason is prose a human reads, and a TAB inside it would shift every field after it.
    print(
        "\t".join(
            (
                plan.backend,
                plan.model,
                "yes" if plan.substituted else "no",
                ",".join(plan.ceilings),
                " ".join(plan.reason.split()),
            )
        )
    )
    return 0


def _print_worker_environment() -> None:
    """One `KEY<TAB>VALUE` line per `project.worker_environment` entry, for `worker_task.sh` to
    `export` before it launches the backend CLI -- never a literal environment variable name or
    value in the mechanism itself (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-
    and-configured-not-coded.md). `worker_environment_rules` renders the paragraph the worker reads
    about this same list, names and no values, so the export and the prose cannot drift."""
    for key, value in load_project().worker_environment.items():
        print(f"{key}\t{value}")


def _print_worktree_backends() -> None:
    """One backend name per line, every configured backend that has a worktree -- so `worker_task.sh
    start` can count alive workers across all of them without a hardcoded pair of names
    (agent_os/docs/adr/2026-09-15-parallelism-is-a-configured-cap-enforced-by-the-driver.md)."""
    for name, backend in load_project().backends.items():
        if backend.worktree:
            print(name)


BACKEND_FIELDS = ("command", "worktree", "app", "stream", "quota")


def _print_backend_value(name: str, field: str, *, as_path: bool) -> int:
    """One capability of one backend for the shell drivers (#514), so no bash branches on a
    backend's name: exit 0 with the value, 2 when `name` is not a configured backend (the driver's
    usage error), 1 when the config does not load (a loud stop, as `backend-executable`).
    `command` prints what `backend_executable` resolves, never the raw, possibly empty, field;
    `worktree --path` resolves it against the repository root."""
    try:
        project = load_project()
    except (ValidationError, yaml.YAMLError, OSError) as error:
        print(
            f"cannot read backend '{name}': {DEFAULT_AGENTS_CONFIG} does not load\n{error}",
            file=sys.stderr,
        )
        return 1
    try:
        backend = backend_config(name, project=project)
    except KeyError as error:
        print(error.args[0], file=sys.stderr)
        return 2
    if field == "command":
        print(backend_executable(name, project=project))
    elif field == "worktree" and as_path:
        print(worktree_path(name, project=project) if backend.worktree else "")
    else:
        print(getattr(backend, field))
    return 0


def _project_value(key: str, *, as_path: bool) -> str:
    """One field of the `project:` section for the shell drivers, `a.b` reaching into a mapping
    (`worktrees.claude`, `worker_apps.qwen`). `--path` resolves the value against the repository
    root, which is what `worktrees:` entries are relative to."""
    value = load_project()
    for part in key.split("."):
        value = value[part] if isinstance(value, dict) else getattr(value, part)
    return str((HOST_ROOT / str(value)).resolve()) if as_path else str(value)


def _planner_value(key: str) -> str:
    """One field of the `planner:` section for the shell drivers, the same shape as
    `_project_value` above. `relaunch_cap` is the first field of this section a bash driver reads
    directly -- every other one (`idle_wake_minutes`, `max_runs_per_day`, `refiner_unattended`) is
    only ever read from Python, inside `agent_guard.py` (#362)."""
    return str(getattr(load_planner_config(), key))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("usage-report")
    report.add_argument("events")
    report.add_argument(
        "body_file",
        nargs="?",
        default=None,
        help="the issue's body, to resolve its own budget class's max_context (#483)",
    )
    resolve = sub.add_parser("resolve-budget")
    resolve.add_argument(
        "--field", default="name", help="a field of the resolved class (default: its name)"
    )
    exited = sub.add_parser("mark-backend-exited")
    exited.add_argument("log")
    row = sub.add_parser("planner-run-row")
    row.add_argument("log")
    row.add_argument("--ts", required=True)
    row.add_argument("--context", default="")
    row.add_argument("--model", default="")
    role = sub.add_parser("role-class")
    role.add_argument("role", help="validator, refiner or planner")
    role.add_argument(
        "--field", default="model", help="which field of the class to print (default: model)"
    )
    role_app = sub.add_parser("role-app")
    role_app.add_argument("role")
    role_backend = sub.add_parser("role-backend")
    role_backend.add_argument("role", help="validator, refiner or planner")
    role_backend.add_argument(
        "--cache-dir",
        default=None,
        help="where the guard's bookkeeping lives (default: $WORKER_CACHE_DIR, else .cache)",
    )
    project = sub.add_parser("project-value")
    project.add_argument(
        "key", help="a field of the project: section, dotted into a mapping: worktrees.claude"
    )
    project.add_argument(
        "--path", action="store_true", help="resolve the value against the repository root"
    )
    planner = sub.add_parser("planner-value")
    planner.add_argument("key", help="a field of the planner: section, e.g. relaunch_cap")
    executable = sub.add_parser("backend-executable")
    executable.add_argument(
        "name",
        help="a backend or CLI command name -- a key of project.backends or project.executables",
    )
    sub.add_parser("human-message-rules")
    sub.add_parser("forbidden-paths-rules")
    sub.add_parser("forbidden-paths-regex")
    sub.add_parser("forbidden-paths-merge-audit-regex")
    sub.add_parser("forbidden-paths-merge-audit-violations")
    sub.add_parser("mechanism-paths-rules")
    sub.add_parser("mechanism-paths-regex")
    sub.add_parser("never-run-rules")
    sub.add_parser("worker-environment")
    sub.add_parser("worker-environment-rules")
    render = sub.add_parser("render-prompt")
    render.add_argument("role", help="worker, validator, refiner or planner")
    render.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_values",
        metavar="NAME=VALUE",
        help="a placeholder only the run knows: MAIN_CHECKOUT, WORKTREE, REVIEW_BACKEND_LINE",
    )
    sub.add_parser("worktree-backends")
    worktree = sub.add_parser("worktree-path")
    worktree.add_argument("backend", help="a key of project.backends that has a worktree")
    backend_value = sub.add_parser("backend-value")
    backend_value.add_argument("backend", help="a key of project.backends")
    backend_value.add_argument("field", choices=BACKEND_FIELDS)
    backend_value.add_argument(
        "--path", action="store_true", help="resolve `worktree` against the repository root"
    )
    backend_model = sub.add_parser("backend-model")
    backend_model.add_argument("backend", help="a key of project.backends")
    sub.add_parser("stages-completed")
    stage_titles = sub.add_parser("stage-titles")
    stage_titles.add_argument("body_file")
    cumulative_cost = sub.add_parser("cumulative-cost")
    cumulative_cost.add_argument("paths", nargs="+")
    cumulative_tokens = sub.add_parser("cumulative-tokens")
    cumulative_tokens.add_argument("paths", nargs="+")
    quota = sub.add_parser("quota-status")
    quota.add_argument("events")
    args = parser.parse_args()
    if args.command == "usage-report":
        body_path = pathlib.Path(args.body_file) if args.body_file else None
        body = body_path.read_text() if body_path and body_path.is_file() else None
        _print_usage_report(args.events, body)
    elif args.command == "resolve-budget":
        sys.exit(_resolve_budget(sys.stdin.read(), args.field))
    elif args.command == "mark-backend-exited":
        write_role_run_exit_marker(args.log)
    elif args.command == "planner-run-row":
        log_text = pathlib.Path(args.log).read_text(errors="replace")
        print(planner_run_row(log_text, ts=args.ts, context=args.context, model=args.model))
    elif args.command == "role-class":
        try:
            name, task_class = load_role_class(args.role)
        except KeyError as error:
            sys.exit(str(error))
        print(name if args.field == "name" else getattr(task_class, args.field))
    elif args.command == "role-app":
        print(role_app_slug(args.role))
    elif args.command == "role-backend":
        sys.exit(_print_role_backend(args.role, cache_dir=args.cache_dir))
    elif args.command == "project-value":
        print(_project_value(args.key, as_path=args.path))
    elif args.command == "planner-value":
        print(_planner_value(args.key))
    elif args.command == "backend-executable":
        # A broken config is a loud stop, never a silent fallback to whatever PATH holds: the
        # drivers check this exit status and refuse to launch anything (#380).
        try:
            print(backend_executable(args.name))
        except (ValidationError, yaml.YAMLError, OSError, KeyError) as error:
            sys.exit(
                f"cannot resolve the executable for '{args.name}': "
                f"{DEFAULT_AGENTS_CONFIG} does not load\n{error}"
            )
    elif args.command == "human-message-rules":
        print(human_message_rules())
    elif args.command == "forbidden-paths-rules":
        print(forbidden_paths_rules())
    elif args.command == "forbidden-paths-regex":
        print(forbidden_paths_regex())
    elif args.command == "forbidden-paths-merge-audit-regex":
        print(forbidden_paths_merge_audit_regex())
    elif args.command == "forbidden-paths-merge-audit-violations":
        # One changed path per line on stdin, exactly what `gh pr diff N --name-only` prints --
        # condition 3's own reading of the merge-audit subset (`agent_os/docs/AGENT_OS.md` §2.4).
        for path in forbidden_paths_merge_audit_violations(sys.stdin.read().splitlines()):
            print(path)
    elif args.command == "mechanism-paths-rules":
        print(mechanism_paths_rules())
    elif args.command == "mechanism-paths-regex":
        print(mechanism_paths_regex())
    elif args.command == "never-run-rules":
        print(never_run_rules())
    elif args.command == "worker-environment":
        _print_worker_environment()
    elif args.command == "worker-environment-rules":
        print(worker_environment_rules())
    elif args.command == "render-prompt":
        values = prompt_substitutions()
        for assignment in args.set_values:
            name, separator, value = assignment.partition("=")
            if not separator:
                sys.exit(f"--set takes NAME=VALUE, and {assignment!r} carries no '='")
            values[name] = value
        try:
            sys.stdout.write(render_prompt(args.role, values, prompt_extras_path(args.role)))
        except (KeyError, FileNotFoundError) as error:
            # A driver that cannot render its prompt must not launch a backend on a half-written
            # one: every caller checks this exit status. `args[0]` rather than `str(error)`, which
            # for a KeyError is the message's own repr, quotes and all.
            sys.exit(str(error.args[0]))
    elif args.command == "worktree-backends":
        _print_worktree_backends()
    elif args.command == "worktree-path":
        try:
            print(worktree_path(args.backend))
        except KeyError:
            sys.exit(f"'{args.backend}' is not a configured backend with a worktree")
    elif args.command == "backend-value":
        sys.exit(_print_backend_value(args.backend, args.field, as_path=args.path))
    elif args.command == "backend-model":
        try:
            print(backend_default_model(args.backend))
        except (KeyError, ValidationError, yaml.YAMLError, OSError) as error:
            sys.exit(f"cannot resolve a default model for backend '{args.backend}': {error}")
    elif args.command == "stages-completed":
        print(stages_completed(sys.stdin.read().splitlines()))
    elif args.command == "stage-titles":
        for title in parse_stages(pathlib.Path(args.body_file).read_text()):
            print(title)
    elif args.command == "cumulative-cost":
        print(f"{cumulative_cost_usd([pathlib.Path(p) for p in args.paths]):.4f}")
    elif args.command == "cumulative-tokens":
        # No thousands separator and no padding: the driver compares this against
        # `max_total_tokens` with bash arithmetic, which reads neither.
        print(cumulative_total_tokens([pathlib.Path(p) for p in args.paths]))
    elif args.command == "quota-status":
        print(quota_status(read_events(args.events)))


if __name__ == "__main__":
    main()
