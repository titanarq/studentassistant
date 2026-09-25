"""The deterministic guard: decides whether a worker is over budget, quota-blocked, stalled or
looping, without depending on the worker's own judgment to notice it -- the mechanism issue #341
exists to build (origin: on 2026-09-13 a subagent ended its turn mid-refactor and left the tree
broken without saying so).

Two roles, two entry points, both stateless between invocations except for the small bookkeeping
files under `.cache/` this module itself owns:

    python -m agent_os.guard check <qwen|claude>
        # the EXIT HOOK: called from inside worker_task.sh's own subshell, in the same process,
        # immediately after the backend CLI exits on its own. Nothing left to evaluate -- only the
        # terminal state to record (CUT_BY_GUARD, if a tick got there first, is never overwritten),
        # the matching worker_finished/worker_cut event to write, and `wake`.

    python -m agent_os.guard tick
        # the MONITOR TICK: a systemd --user timer, not a resident process. Checks every backend's
        # budget, quota, commit cadence and declared liveness window, cuts a run that has crossed
        # one; RECONCILES the mechanical state nobody's judgment is needed for (a closed issue
        # still carrying a status label goes to `done`, a refined child whose parent opted in is
        # promoted -- #365); and writes one event per edge it found (a cut, a quota change, a
        # human's reply, an issue that just became dispatchable, an orphan status:doing issue, a
        # one-shot role whose run died before it could announce its own end -- #400 -- or,
        # rate-limited, nothing running with something dispatchable). Then calls `wake`.
        # The tracking epic carrying status:agents-paused stops all of it.

    python -m agent_os.guard event <kind> <subject> [--detail "..."]
        # one event file, for a driver that ends outside this module: agent_os/bin/agent_task.sh
        # writes `validator_finished`/`refiner_finished` this way and then calls `wake`.

    python -m agent_os.guard wake
        # THE ONLY DOOR TO A PLANNER RUN. Under a non-blocking flock on .cache/planner.lock: hands
        # every unconsumed event to agent_os/bin/planner_task.sh run as its context, then moves them to
        # .cache/planner_events/consumed/. Exits quietly if a planner already holds the lock, and
        # refuses (paging once) past planner.max_runs_per_day.

    python -m agent_os.guard promote-refined
        # for every open status:refine issue whose body now validates AND whose parent carries
        # auto-ready, moves it to status:ready (agent_lib.promotable_to_ready) and prints what it
        # promoted, or that there was nothing to. `tick` runs this on every fire since #365, so
        # this subcommand is a manual sweep, not the only way it ever happens. Never sets
        # status:ready on an issue with no parent -- that stays a human decision
        # (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-
        # run.md).

Every decision function below is pure: it takes parsed events/state/commit info and returns a
verdict, with no filesystem or process access, so it is unit-tested over fixture data
(tests/test_agent_guard.py) without a live agent. The I/O -- reading `.state`/`progress.log`,
shelling out to `git log`, killing the process, committing, posting -- lives only in the `_tick_*`
and action functions below the pure section.

    agent_os/docs/adr/2026-09-14-a-stall-inside-budget-is-caught-by-commit-cadence.md
    agent_os/docs/adr/2026-09-14-liveness-is-judged-against-a-plan-the-worker-declares.md
    agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md
    agent_os/docs/adr/2026-09-14-driver-writes-mechanical-state-agent-writes-cooperative-state.md
    agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md
    agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md
    agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-never-as-a-standing-process.md
    agent_os/docs/adr/2026-09-14-a-humans-reply-wakes-the-planner-never-the-worker-that-asked.md
    agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md
    agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml

from agent_os.cli import AGENT_OS_DIR, agent_os_python, host_root
from agent_os.lib import (
    ROLE_RUN_EXIT_MARKER_SUFFIX,
    HumanMessageError,
    LabelVocabulary,
    StreamParser,
    TaskClass,
    UsageSummary,
    backend_quota_cuts,
    backend_stream_parser,
    cumulative_cost_usd,
    cumulative_total_tokens,
    is_dispatchable,
    is_human_comment,
    label_names,
    last_result_event,
    load_mechanism,
    load_planner_config,
    load_project,
    load_task_classes,
    needs_refinement,
    parse_budget_line,
    promotable_to_ready,
    quota_status,
    read_events,
    read_role_run_exit_marker,
    refine_queue_rank,
    refiner_pass_answered_by_the_human,
    render_human_message,
    role_app_slug,
    usage_failed,
    usage_summary,
)
from agent_os.streams.interface import event_message

# The HOST project's root, resolved rather than assumed: `$AGENT_OS_HOST_ROOT`, else the git
# checkout the call is made from. Everything a project owns hangs off it -- `config/agents.yaml`,
# `.cache/`, `.secrets/`, `.github/ISSUE_TEMPLATE/`, the `project.worktrees` entries -- and none of
# it is derived from this package's own location, which is what made the mechanism able to run
# exactly one project (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-
# never-modified.md).
HOST_ROOT = host_root()

# Same 30-minute grace period a worker that has posted no EXPECT yet gets, per the ADR: declaring
# is cheap, and silence about silence should not be rewarded.
DEFAULT_LIVENESS_CUTOFF = timedelta(minutes=30)

# Everything below that names this particular project -- the worktree of each backend, the label
# vocabulary, the tracking epic whose status:agents-paused is the full stop -- is read from
# `config/agents.yaml`'s `project:` section, never written as a literal here
# (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md). It is
# read once at import: a malformed section should fail the guard loudly, not on the tick that
# happens to need the value.
PROJECT = load_project()
# The backends a worker runs on: every `project.backends` entry with a worktree (#514).
BACKENDS: tuple[str, ...] = tuple(
    name for name, backend in PROJECT.backends.items() if backend.worktree
)
BACKEND_WORKTREES = {
    name: str((HOST_ROOT / PROJECT.backends[name].worktree).resolve()) for name in BACKENDS
}

# The roles `agent_os/bin/agent_task.sh` launches as ONE detached run each and that announce their own
# end as an event: their per-run logs and PID files live in `.cache/<role>/`, and the run removes
# its PID file as its last step, which is what makes a PID file still on disk a run that never
# reached its end (#400). The planner is not here: `agent_os/bin/planner_task.sh` launches it and writes
# no PID file, and a worker's end is the exit hook's, not a scan of this kind.
ONE_SHOT_ROLES = ("validator", "refiner")

CutReason = Literal["stall", "budget", "quota"]
StallTier = Literal["warn", "cut"]
RoleRunDeath = Literal["died_mid_run", "ended_unannounced"]

READY_LABEL = PROJECT.labels.ready
REFINE_LABEL = PROJECT.labels.refine
DOING_LABEL = PROJECT.labels.doing
BLOCKED_ON_HUMAN_LABEL = PROJECT.labels.blocked_on_human
AI_COMPLETED_LABEL = PROJECT.labels.ai_completed
AGENTS_PAUSED_LABEL = PROJECT.labels.agents_paused
WAKE_PLANNER_LABEL = PROJECT.labels.wake_planner
TRACKING_EPIC_ISSUE = str(PROJECT.tracking_epic)

# The things that can make a planner run worth its cost. Anything not on this list is not an
# event: the planner is woken by edges, never by a condition someone re-derives every tick
# (agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md).
# The one-shot roles' own ends and the refine-the-backlog edge: a validator that has posted its
# review is an edge the planner acts on (approve -> the human merges, request-changes -> resume
# the worker), exactly like a worker's end (agent_os/docs/adr/2026-09-14-a-pr-is-validated-by-a-validator-
# agent-against-the-issues-acceptance-criteria.md); `refine_pending` is `idle_dispatchable`'s
# sibling for the refiner -- a condition, not an edge, so it is rate-limited the same way
# (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md).
# `new_dispatchable` is `idle_dispatchable`'s own edge half: an issue *becoming* dispatchable is
# an edge and is not rate-limited, while a set of issues *sitting* dispatchable stays the
# rate-limited condition (agent_os/docs/adr/2026-09-16-a-newly-dispatchable-issue-is-an-edge-not-a-
# condition.md). `orphan_doing` is the tick's own reconciliation edge (#365): an issue labeled
# status:doing that no live worker is running is state nobody is acting on, and unlike the drift
# the tick merely logs, only the planner can decide what to do with it. `role_died` is that same
# shape for a one-shot role (#400): the run is over -- its PID is dead -- and it never wrote the
# `<role>_finished` event that was the only announcement of its end, so the work it owed (a review,
# a refinement) is missing and only the planner can relaunch it. `pr_merged` is the edge a merge
# never used to write (#413): on 2026-09-17 the planner declined #404 because Qwen's worktree was
# dirty, PR #411 merged and cleared exactly that, and nothing woke the planner until the
# rate-limited `idle_dispatchable` two hours later -- so a merge seen while nothing runs and
# something is dispatchable is an edge, not rate-limited. `nudged` is the human's own sanctioned
# wake (#413): the `wake:planner` label, set by the human on an open issue and removed by the
# tick that reads it; the reason is the human's latest comment on that issue, never the event.
EVENT_KINDS = (
    "worker_finished",
    "worker_cut",
    "quota_changed",
    "human_replied",
    "idle_dispatchable",
    "new_dispatchable",
    "orphan_doing",
    "validator_finished",
    "refiner_finished",
    "refine_pending",
    "role_died",
    "pr_merged",
    "nudged",
)
# The `.state` line-1 prefixes that mean the run is over, so `check` and `tick` record them
# instead of writing DONE over them. `FAILED_LAUNCH` (#381) sits beside `CUT_BY_GUARD` because a
# stage process that never started is an ending too -- and the one the planner most needs told
# apart from a stage that merely failed to commit, since relaunching is the answer to one and
# never to the other. `BLOCKED reason=<...>` is `open-pr`'s own family of endings (#389): the work
# is done and the pull request cannot be judged, so the issue carries `status:blocked-on-human`
# and the run must NOT reach the planner as `worker_finished`, which reads exactly like success.
RUN_CUT_STATES = ("CUT_BY_GUARD", "FAILED_LAUNCH", "BLOCKED")
RUN_ENDED_STATES = ("DONE", *RUN_CUT_STATES)
EVENT_TS_FORMAT = "%Y%m%dT%H%M%SZ"
EVENT_NAME_RE = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)-(?P<kind>[a-z_]+)-(?P<subject>.+)$")


# ----------------------------------------------------------------------------------------------
# Pure decision functions -- no filesystem, no subprocess, no clock reads. Every "now" or "path
# contents" is a parameter, so a fixture in tests/test_agent_guard.py exercises them directly.
# ----------------------------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([hmd])$")


def parse_duration(text: str) -> timedelta:
    """`4h`, `30m`, `1d` -- a single number and a single unit, the only grammar an EXPECT/HEARTBEAT
    line's `normal=`/`cutoff=` accepts. A compound like `1h30m` is not this grammar (#428) --
    write `90m`."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"not a duration like '30m' / '4h' / '1d': {text!r}")
    value = float(match.group(1))
    unit = match.group(2)
    return {"h": timedelta(hours=value), "m": timedelta(minutes=value), "d": timedelta(days=value)}[
        unit
    ]


# A worker's progress.log already timestamps every line `YYYY-MM-DD HH:MM  <text>` (the convention
# the injected worker rules state); EXPECT/HEARTBEAT lines carry the same prefix. The typed
# timestamp stays in the line for the human reading the log, but it is no longer what liveness is
# measured from (#419) -- see `liveness_expired`'s `observed_at` parameter.
_EXPECT_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(?:EXPECT|HEARTBEAT)\s+\S+"
    r"\s+normal=(?P<normal>\S+)\s+cutoff=(?P<cutoff>\S+)\s*$"
)


def last_declared_cutoff(progress_log_text: str) -> tuple[datetime, timedelta] | None:
    """The most recent EXPECT/HEARTBEAT line's own timestamp and cutoff, read in file order --
    last match wins, per agent_os/docs/adr/2026-09-14-liveness-is-judged-against-a-plan-the-worker-
    declares.md. `None` if the worker has not posted one yet.

    A line whose `cutoff=` cannot be parsed is skipped, never raised (#428): one malformed
    declaration, anywhere in a log that outlives the stage that wrote it, must not poison every
    later tick -- `last_declared_cutoff` still returns the most recent line it CAN read.
    `unparsable_cutoff_lines` below re-scans the same text for what this loop skipped, so a caller
    that knows the file's path can still make the defect visible."""
    last = None
    for line in progress_log_text.splitlines():
        match = _EXPECT_LINE_RE.match(line)
        if not match:
            continue
        try:
            cutoff = parse_duration(match["cutoff"])
        except ValueError:
            continue
        # Naive on purpose: progress.log's own timestamp convention has no tz field, and the
        # whole worker fleet runs on this one host -- `now`/`run_started_at` below are naive
        # the same way, so the subtraction in liveness_expired is comparing like with like.
        ts = datetime.strptime(match["ts"], "%Y-%m-%d %H:%M")  # noqa: DTZ007
        last = (ts, cutoff)
    return last


def unparsable_cutoff_lines(progress_log_text: str) -> list[str]:
    """One entry per EXPECT/HEARTBEAT line whose `cutoff=` `last_declared_cutoff` had to skip:
    1-based line number, the parse error and the raw line -- everything except the file's own
    path, which only the caller (`_tick_backend`) knows. Surviving a line the guard cannot read
    (#428) must not make the defect silent, or it recurs unnoticed until the next human sweep."""
    warnings: list[str] = []
    for line_number, line in enumerate(progress_log_text.splitlines(), start=1):
        match = _EXPECT_LINE_RE.match(line)
        if not match:
            continue
        try:
            parse_duration(match["cutoff"])
        except ValueError as error:
            warnings.append(f"line {line_number}: {error} ({line.strip()!r})")
    return warnings


def liveness_expired(
    progress_log_text: str,
    run_started_at: datetime,
    now: datetime,
    *,
    observed_at: datetime,
) -> bool:
    """Has more time passed since the guard last saw progress.log change than that change's own
    declared cutoff -- never a number the guard invents, and never the timestamp text the worker
    typed into the line (#419). `observed_at` is the guard's own reading of when the last
    declaration arrived -- the file's mtime in production -- because a worker's own clock is
    cooperative state the same way the cutoff VALUE is (agent_os/docs/adr/2026-09-14-driver-writes-
    mechanical-state-agent-writes-cooperative-state.md), but unlike the cutoff value, the arrival
    of the line is not something the worker is better placed to know than the guard is. Before the
    first EXPECT, the anchor is the run's own start (when worker_task.sh wrote STARTED/RESUMED)
    and the default 30-minute cutoff applies."""
    declared = last_declared_cutoff(progress_log_text)
    # A DECLARATION FROM BEFORE THIS RUN STARTED IS NOT THIS RUN'S. `progress.log` outlives the
    # process that wrote it: since #375 each stage is its own process, appending to the same file
    # the previous stage left behind. Anchoring on a line the previous stage wrote lets a stage be
    # cut on its very first check, for silence that happened before it existed -- which is what
    # killed stage 4 of #363 five minutes in, while it was reading code normally. A stage that has
    # not declared anything yet gets the default grace from its own start, exactly like a first
    # run. Expressed against the OBSERVED anchor (#419): if nothing has been appended to
    # progress.log since this run started, `observed_at` itself still predates `run_started_at`
    # and the same fallback applies -- the file not changing is exactly what "declared nothing new"
    # looks like from outside.
    if declared is not None and observed_at >= run_started_at:
        anchor, cutoff = observed_at, declared[1]
    else:
        anchor, cutoff = run_started_at, DEFAULT_LIVENESS_CUTOFF
    return now - anchor > cutoff


def budget_exceeded(
    summary: UsageSummary,
    task_class: TaskClass,
    *,
    issue_cost_usd: float | None = None,
    issue_total_tokens: int | None = None,
    parser: StreamParser | None = None,
) -> bool:
    """True when ANY of the three ceilings a task class names is passed (agent_os/docs/adr/2026-09-14-agent-
    spend-is-tokens-not-time-and-needs-a-written-budget.md), which since #375 are measured over
    different things: `max_context` is per stage process -- a fresh process starts from an empty
    context, which is the whole point of staging -- while `max_cost_usd` and `max_total_tokens` are
    the issue's, the sum of every stage process it has taken. `issue_cost_usd` and
    `issue_total_tokens` carry those sums when the caller knows which issue this is
    (`cost_spent_on_issue` and `tokens_spent_on_issue` below); without either, the live process's
    own terminal `result` is all there is to judge, and both figures are only known at all once the
    backend has reported one.

    The token ceiling is the one that always bites: Qwen's `result` event carries
    `usage.total_tokens` and no `total_cost_usd`, so a Qwen class can never cross the dollar one
    and would run with no live spend backstop at all (#387). Dollars stay a second ceiling because
    the Claude roles do report them.

    The live process's own figures are read off its terminal `result` by `parser`, the backend's
    own stream parser (#514) -- the default shape when the caller does not know the backend."""
    if summary.context > task_class.max_context:
        return True
    # `summary.result` IS the terminal event, whatever its own `type` field says, so the parser is
    # handed it as one.
    own_result = (
        (parser or backend_stream_parser()).result_usage([{**summary.result, "type": "result"}])
        if summary.result
        else None
    )
    cost = issue_cost_usd
    if cost is None and own_result is not None:
        cost = own_result.cost_usd
    if cost is not None and cost > task_class.max_cost_usd:
        return True
    tokens = issue_total_tokens
    if tokens is None and own_result is not None:
        tokens = own_result.total_tokens
    return tokens is not None and tokens > task_class.max_total_tokens


@dataclass
class StallBookkeeping:
    """Guard-internal, distinct from the driver's `.state` and the agent's `progress.log`
    (agent_os/docs/adr/2026-09-14-driver-writes-mechanical-state-agent-writes-cooperative-state.md). Only
    Qwen's fallback path below actually advances it -- Claude's events carry their own timestamp,
    so turns-since-commit never needs a running counter for that backend."""

    commit_count: int = 0
    turn_count_at_commit: int = 0
    warned_at_turn_count: int | None = None
    # The tick's own memory of "allowed"/"exhausted" from the previous tick, so `tick` can tell a
    # freshly-observed quota state from a *changed* one -- checkpoint 4's third planner trigger
    # (agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md).
    # `None` before the first tick has ever observed this backend, so that first observation is
    # never itself reported as a change.
    last_quota_status: str | None = None
    # Which worker process the three stall fields above were counted in (#52): `_run_identity`'s
    # issue, start ref and PID. The fields are per-run -- Qwen's `turns` restarts at 0 with every
    # stage process and the commit count is `<startref>..HEAD` -- but the file outlives every run
    # because `last_quota_status` must. `None` (a file written before this field existed) never
    # matches a live run, so such a file is re-scoped on its first tick.
    run_identity: str | None = None


def scoped_to_run(bookkeeping: StallBookkeeping, run_identity: str) -> StallBookkeeping:
    """The bookkeeping as the run `run_identity` names should see it: unchanged when it was
    counted in that run, otherwise with the stall fields back to a fresh run's and only the quota
    memory kept -- a previous run's commit count and turn anchor mean nothing against this run's
    stream (#52)."""
    if bookkeeping.run_identity == run_identity:
        return bookkeeping
    return StallBookkeeping(
        last_quota_status=bookkeeping.last_quota_status, run_identity=run_identity
    )


def turns_since_commit(
    events: list[dict],
    commit_timestamps: list[datetime],
    turns: int,
    bookkeeping: StallBookkeeping,
) -> tuple[int, StallBookkeeping]:
    """Claude's `assistant` events carry their own `timestamp`, so this is exact: count turns
    after the last commit's own timestamp. Qwen's carry none, so it falls back to a running
    counter that only advances when the commit count itself has grown since the last tick."""
    claude_timestamps = [
        datetime.fromisoformat(event["timestamp"])
        for event in events
        if event.get("type") == "assistant" and event.get("timestamp")
    ]
    if claude_timestamps:
        last_commit = max(commit_timestamps) if commit_timestamps else None
        since = sum(1 for ts in claude_timestamps if last_commit is None or ts > last_commit)
        return since, bookkeeping

    commit_count = len(commit_timestamps)
    if bookkeeping.turn_count_at_commit > turns:
        # An anchor this process has not reached: it was counted in another process (#52). The
        # caller scopes the bookkeeping to the run first; this is the backstop that keeps a stale
        # file from ever yielding a negative count, anchored at the one point this process's
        # stream can vouch for, its own start.
        bookkeeping = replace(bookkeeping, commit_count=commit_count, turn_count_at_commit=0)
    elif commit_count > bookkeeping.commit_count:
        # `replace`, not a fresh instance: the quota memory and the warning marker survive a commit.
        bookkeeping = replace(bookkeeping, commit_count=commit_count, turn_count_at_commit=turns)
    return turns - bookkeeping.turn_count_at_commit, bookkeeping


def stall_detected(
    events: list[dict],
    commit_timestamps: list[datetime],
    task_class: TaskClass,
    turns: int,
    bookkeeping: StallBookkeeping,
) -> tuple[StallTier | None, int, StallBookkeeping]:
    """Turns since the worker's last commit, judged against the task class's two thresholds
    (agent_os/docs/adr/2026-09-14-a-stall-inside-budget-is-caught-by-commit-cadence.md): past the lower one
    it is a warning still worth watching, past the higher one it cuts. Returns the tier (if any),
    the turn count itself (for the warning message), and the bookkeeping to persist."""
    since_commit, bookkeeping = turns_since_commit(events, commit_timestamps, turns, bookkeeping)
    if since_commit >= task_class.commit_cut_turns:
        return "cut", since_commit, bookkeeping
    if since_commit >= task_class.commit_warn_turns:
        return "warn", since_commit, bookkeeping
    return None, since_commit, bookkeeping


def repeated_tool_calls(events: list[dict], streak: int = 3) -> bool:
    """Three identical `(name, input)` tool_use blocks in a row is a loop by definition, checked
    regardless of turn count -- independent of the commit-cadence stall tier above."""
    calls = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event_message(event).get("content") or []:
            if block.get("type") == "tool_use":
                calls.append((block.get("name"), json.dumps(block.get("input"), sort_keys=True)))
    return any(len(set(calls[i : i + streak])) == 1 for i in range(len(calls) - streak + 1))


def detect_state_drift(
    state_line: str,
    marker_label: str,
    github_label: str | None,
    *,
    labels: LabelVocabulary | None = None,
) -> str | None:
    """Does the `.state` marker's label disagree with GitHub in a way that means the driver's own
    `issues.py move` never landed -- never a normal downstream move the driver had no part in (the
    validator's approval, the human merging)? `github_label` is `None` when the lookup itself
    failed, which is a `gh` hiccup, not evidence of drift.

    (a) the run is still alive (`STARTED`/`RESUMED`) and GitHub's label differs from what the
        driver last recorded -- something relabeled the issue out from under a running worker.
    (b) the run has finished (`DONE`/`CUT_BY_GUARD`/`FAILED_LAUNCH`), the driver's own marker
        says it moved the
        issue to `ai_completed` (the `open-pr` move), and GitHub still shows `doing` -- that move
        never landed. Any other label on a finished run (`review`, `done`, `blocked-on-human`, a
        human relabeling it by hand, ...) is normal downstream movement, not drift.

    Returns the `local <label>, GitHub <label>` fragment `_log_state_drift` prints, or `None`."""
    labels = labels or LabelVocabulary()
    if github_label is None or github_label == marker_label:
        return None
    running = state_line.startswith(("STARTED", "RESUMED"))
    finished = state_line.startswith(RUN_ENDED_STATES)
    if running:
        return f"local {marker_label}, GitHub {github_label}"
    if finished and marker_label == labels.ai_completed and github_label == labels.doing:
        return f"local {marker_label}, GitHub {github_label}"
    return None


def role_run_death(*, pid_alive: bool, log_text: str) -> RoleRunDeath | None:
    """How a one-shot role's run ended, from the only two facts on disk about it: whether the PID
    its PID file names is alive, and whether its log carries the backend's terminal `result` event.
    `None` while the run is alive -- a PID file is written for a run that is merely UNFINISHED, and
    a validator twelve minutes into a review looks exactly like one that died until the process
    behind it is gone.

    `died_mid_run` is #400's own case, measured on PR #399 on 2026-09-17: the validator went down
    with the planner session that had launched it, its log stops mid-turn with no `result`, and what
    that left was no review, no `validator_finished` and nothing recording that the review never
    happened. `ended_unannounced` is the narrower one: the backend did finish and the run then died
    inside its own bookkeeping -- the runs.tsv row, the event, the wake, the PID file's removal --
    so the work it owed may well have been delivered and the planner still hears nothing. Both are a
    run whose end was never announced; which of the two it was only changes the wording."""
    if pid_alive:
        return None
    return "died_mid_run" if last_result_event(log_text) is None else "ended_unannounced"


# ----------------------------------------------------------------------------------------------
# I/O: paths, the exit hook, the tick, and the two actions a tick can take.
# ----------------------------------------------------------------------------------------------


@dataclass
class WorkerPaths:
    backend: str
    main: Path
    worktree: Path
    pidfile: Path
    events: Path
    startref: Path
    statefile: Path
    issuefile: Path
    bookkeeping: Path


def cache_dir(main: Path = HOST_ROOT) -> Path:
    """Where the driver's per-worker files and the planner's events live. `WORKER_CACHE_DIR` moves
    all of them at once -- the same override `worker_task.sh` honours, and honoured here too so
    that a test can let the real exit hook fire without writing into a `.cache` a live run is
    using (this checkout's `.cache` is shared with every worktree of it by symlink)."""
    override = os.environ.get("WORKER_CACHE_DIR")
    return Path(override) if override else main / ".cache"


def stage_spend_dir(issue: str, main: Path = HOST_ROOT) -> Path:
    """Where `worker_task.sh` archives one stage process's `.jsonl` when it ends, and where #367's
    spend report will read them from: `.cache/spend/<issue>/<ts>-<backend>-stage<N>.jsonl`."""
    return cache_dir(main) / "spend" / str(issue)


def issue_stage_logs(issue: str, live_events: Path, main: Path = HOST_ROOT) -> list[Path]:
    """Every stage log this issue has spent from: the archived ones plus whatever the live process
    has collected since (#375). ONE listing for both issue-wide ceilings, so the dollar sum and the
    token sum can never be measured over a different set of files (#387)."""
    logs = sorted(stage_spend_dir(issue, main).glob("*.jsonl"))
    if live_events.is_file():
        logs.append(live_events)
    return logs


def cost_spent_on_issue(
    issue: str, live_events: Path, main: Path = HOST_ROOT, *, backend: str | None = None
) -> float:
    """What this issue has cost so far across every stage process. This, not the live log alone, is
    what `max_cost_usd` is checked against -- and on a Qwen class it is always 0.0, because that
    backend's `result` event reports no `total_cost_usd` at all (#387)."""
    return cumulative_cost_usd(
        issue_stage_logs(issue, live_events, main), parser=backend_stream_parser(backend)
    )


def tokens_spent_on_issue(
    issue: str, live_events: Path, main: Path = HOST_ROOT, *, backend: str | None = None
) -> int:
    """What this issue has spent in tokens across every stage process: the same scope the dollar
    sum above has, and the one of the two that means anything on Qwen (#387). It UNDERCOUNTS an
    issue that had a stage cut before it finished, for the reason `cumulative_total_tokens`
    states -- that stage's tokens are in its archived log and no terminal event corroborates them."""
    return cumulative_total_tokens(
        issue_stage_logs(issue, live_events, main), parser=backend_stream_parser(backend)
    )


def worker_paths(backend: str, main: Path = HOST_ROOT) -> WorkerPaths:
    worktree = Path(os.environ.get("WORKER_WORKTREE") or BACKEND_WORKTREES[backend])
    cache = cache_dir(main)
    return WorkerPaths(
        backend=backend,
        main=main,
        worktree=worktree,
        pidfile=cache / f"worker_{backend}.pid",
        events=cache / f"worker_{backend}.jsonl",
        startref=cache / f"worker_{backend}.startref",
        statefile=cache / f"worker_{backend}.state",
        issuefile=cache / f"worker_{backend}.issue",
        bookkeeping=cache / f"agent_guard_{backend}.json",
    )


# `issue=<N> label=<status:...>` -- worker_task.sh's own line 2, written only after a successful
# `issues.py move` (#350 Part 4).
STATE_MARKER_RE = re.compile(r"^issue=(?P<issue>\d+)\s+label=(?P<label>\S+)$")


def read_state_marker(statefile: Path) -> tuple[str, int | None, str | None]:
    """(line 1, issue, label) of `.cache/worker_<backend>.state` -- the one Python reader of this
    file, so every consumer here goes through it rather than re-parsing lines of its own (shell
    consumers read line 1 alone with `head -n1`, per docs/modules/workers.md). Line 1 is always
    the driver/guard-written run state (`STARTED`, `RESUMED after=...`, `DONE`,
    `CUT_BY_GUARD reason=...`); an optional line 2, written by `worker_task.sh` only after a
    successful `issues.py move`, records the issue and the status:* label it wrote. A one-line
    file (old state files, or a run that never reached a move) reads as `(state, None, None)`: no
    marker to check drift against, not a broken one."""
    if not statefile.is_file():
        return "", None, None
    lines = statefile.read_text().splitlines()
    state_line = lines[0].strip() if lines else ""
    if len(lines) < 2:
        return state_line, None, None
    match = STATE_MARKER_RE.match(lines[1].strip())
    if not match:
        return state_line, None, None
    return state_line, int(match["issue"]), match["label"]


def write_state_line1(statefile: Path, line1: str) -> None:
    """Overwrite line 1 (the run-state line this module writes: `DONE`,
    `CUT_BY_GUARD reason=...`) while preserving line 2 (the issue/label marker) if one is already
    there -- every writer of line 1, here and in `worker_task.sh`'s own `write_state`, must never
    clobber the other's line (#350 Part 4)."""
    _, issue, label = read_state_marker(statefile)
    text = f"{line1}\n"
    if issue is not None and label is not None:
        text += f"issue={issue} label={label}\n"
    statefile.write_text(text)


def _is_alive(pidfile: Path) -> bool:
    if not pidfile.is_file():
        return False
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _commit_timestamps(worktree: Path, startref: Path) -> list[datetime]:
    if not startref.is_file():
        return []
    base = startref.read_text().strip()
    out = subprocess.run(
        ["git", "-C", str(worktree), "log", "--format=%cI", f"{base}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [datetime.fromisoformat(line) for line in out.stdout.splitlines() if line.strip()]


def _run_identity(paths: WorkerPaths, issue: str) -> str:
    """What tells one worker process from the next (#52): the issue, the start ref `start` writes
    for it, and the PID every stage process writes for itself -- so a new dispatch and a resume
    of the same issue are both a new run."""

    def contents(path: Path) -> str:
        return path.read_text().strip() if path.is_file() else ""

    return f"issue={issue} startref={contents(paths.startref)} pid={contents(paths.pidfile)}"


def _load_bookkeeping(path: Path) -> StallBookkeeping:
    if not path.is_file():
        return StallBookkeeping()
    return StallBookkeeping(**json.loads(path.read_text()))


def _save_bookkeeping(path: Path, bookkeeping: StallBookkeeping) -> None:
    """Atomic: the launch gate reads this file without the lock (`agent_lib.
    read_persisted_quota_verdict`), so it must only ever see the old file or the new one, never a
    half-written one -- a temporary beside it, then `os.replace` over it."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(asdict(bookkeeping)))
    os.replace(temporary, path)


# ----------------------------------------------------------------------------------------------
# Planner events: the only thing that wakes the planner. Neither the tick nor the exit hook calls
# planner_task.sh itself -- each writes one file under `.cache/planner_events/` and then calls
# `wake`, which is the single serialized door to a planner run
# (agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md).
# ----------------------------------------------------------------------------------------------


def events_dir(main: Path = HOST_ROOT) -> Path:
    return cache_dir(main) / "planner_events"


def consumed_dir(main: Path = HOST_ROOT) -> Path:
    return events_dir(main) / "consumed"


def planner_dir(main: Path = HOST_ROOT) -> Path:
    """Where the planner's own per-run logs and `runs.tsv` live. `PLANNER_CACHE_DIR` overrides it
    for a dry run with a stub backend, and `planner_task.sh` honours the same variable -- the two
    must never disagree about where a run was recorded."""
    override = os.environ.get("PLANNER_CACHE_DIR")
    return Path(override) if override else cache_dir(main) / "planner"


def role_run_dir(role: str, main: Path = HOST_ROOT) -> Path:
    """Where `agent_os/bin/agent_task.sh` writes one one-shot role's per-run logs, PID files and
    `runs.tsv`. `AGENT_CACHE_DIR` overrides it for a dry run with a stub backend -- and overrides it
    WHOLE, exactly as the driver's own `agent_run_dir` does, so a stubbed run of every role lands in
    one directory. The two must never disagree about where a run was recorded, or the guard scans a
    directory the driver is not writing to and a run that died is never reported."""
    override = os.environ.get("AGENT_CACHE_DIR")
    return Path(override) if override else cache_dir(main) / role


@dataclass
class EventOutcome:
    """What one `_write_*_if_due` helper did: the line for the tick's own log, and whether an
    event was actually written -- which is the fact that decides whether `wake` has anything to
    do. Both are returned, because the tick used to derive the second from the first by matching
    the English prefix of the line ("... written"), and a reworded log line would then have
    silently stopped a planner run."""

    line: str | None = None
    written: int = 0


def _event_subject(subject: str) -> str:
    """The subject is part of the filename, so it is reduced to what a filename can carry: a
    backend name or an issue number in practice, never free text (that is what `detail` is for)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.]+", "-", str(subject)).strip("-")
    return cleaned or "none"


def write_event(
    kind: str,
    subject: str,
    *,
    detail: str = "",
    main: Path = HOST_ROOT,
    now: datetime | None = None,
) -> Path:
    """One event file, `<utc-timestamp>-<kind>-<subject>`, whose content is the one line the
    planner will be given as context. Two events of the same kind and subject inside one second
    get a `-2`, `-3` suffix rather than silently overwriting each other."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"not a planner event kind: {kind!r} (known: {', '.join(EVENT_KINDS)})")
    now = now or datetime.now(UTC)
    directory = events_dir(main)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime(EVENT_TS_FORMAT)}-{kind}-{_event_subject(subject)}"
    path = directory / stem
    suffix = 1
    while path.exists():
        suffix += 1
        path = directory / f"{stem}-{suffix}"
    path.write_text(f"{detail or f'{kind} {subject}'}\n")
    return path


def pending_events(main: Path = HOST_ROOT) -> list[Path]:
    """Unconsumed events, oldest first -- the filename sorts chronologically by construction."""
    directory = events_dir(main)
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.iterdir() if path.is_file()), key=lambda p: p.name)


def latest_event_at(
    kind: str, *, subject: str | None = None, main: Path = HOST_ROOT
) -> datetime | None:
    """When an event of this kind was last written, consumed or not. The rate limit on
    `idle_dispatchable` is about how often the planner is *woken* for it, so consuming one must
    not reset the clock. With `subject`, only events about that subject count -- `orphan_doing` is
    rate-limited per issue, because two stuck issues are two facts and one must not hide the
    other."""
    wanted = _event_subject(subject) if subject is not None else None
    stamps = []
    for directory in (events_dir(main), consumed_dir(main)):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            match = EVENT_NAME_RE.match(path.name)
            if not match or match["kind"] != kind:
                continue
            # `write_event` appends `-2`, `-3` to a same-second twin, so the subject read back
            # here can carry that suffix; compare on the prefix, never on equality alone.
            if wanted is not None and match["subject"].split("-")[0] != wanted:
                continue
            stamps.append(datetime.strptime(match["ts"], EVENT_TS_FORMAT).replace(tzinfo=UTC))
    return max(stamps) if stamps else None


def event_context(paths: list[Path]) -> str:
    lines = []
    for path in paths:
        try:
            lines.append(path.read_text().strip() or path.name)
        except OSError:
            lines.append(path.name)
    return "; ".join(lines)


def consume_events(paths: list[Path], *, main: Path = HOST_ROOT) -> None:
    target = consumed_dir(main)
    target.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            path.rename(target / path.name)


def runs_today(now: datetime, *, main: Path = HOST_ROOT) -> int:
    """Planner runs already recorded in `.cache/planner/runs.tsv` for `now`'s UTC day. The driver
    appends that line itself, so a run started outside the tick (a manual `wake`) counts too."""
    path = planner_dir(main) / "runs.tsv"
    if not path.is_file():
        return 0
    today = now.strftime("%Y-%m-%d")
    return sum(
        1 for line in path.read_text(errors="replace").splitlines() if line.startswith(today)
    )


def _page_run_cap_once(message: str, *, main: Path = HOST_ROOT, now: datetime) -> bool:
    """One ntfy page the first time the daily cap is reached, never one per blocked wake -- the
    whole point of the cap is that the day stops costing anything (the marker file is what makes
    "once" mechanical rather than a thing the guard has to remember). `message` is already
    rendered by `agent_lib.render_human_message`: nothing here composes what the human reads."""
    marker = planner_dir(main) / f"paged-{now.strftime('%Y-%m-%d')}"
    if marker.exists():
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{now.isoformat()}\n")
    notify(message, main=main)
    return True


def wake(*, main: Path = HOST_ROOT, now: datetime | None = None) -> str:
    """The single door to a planner run. Takes `.cache/planner.lock` non-blockingly: if a planner
    is already running this exits at once and the events stay on disk for that run's end or the
    next wake. Otherwise it hands every unconsumed event to `planner_task.sh run` as context and
    moves them to `consumed/` when the run returns."""
    now = now or datetime.now(UTC)
    lock_path = cache_dir(main) / "planner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "a planner run already holds .cache/planner.lock -- events left for it"
        return _wake_locked(main=main, now=now)


def _wake_locked(*, main: Path, now: datetime) -> str:
    # Before anything can launch the planner: a role whose exit hook called this `wake` directly
    # may have just been refused by its backend, and no tick has run since to say so (#429).
    _fold_role_quota_observations_or_say_why(main=main, now=now)
    pending = pending_events(main)
    if not pending:
        return "no unconsumed planner events -- nothing to wake for"

    planner = load_planner_config()
    already = runs_today(now, main=main)
    if already >= planner.max_runs_per_day:
        # Two different readers, two different texts: this return value is the journal line the
        # operator greps, in English like every other log, while what reaches the phone is the
        # configured template in the human's own language. Both carry the SAME context, because
        # the ADR requires a page to name which issue and why, and a bare count names neither.
        context = event_context(pending)
        summary = (
            f"planner run cap reached ({already}/{planner.max_runs_per_day} today); "
            f"{len(pending)} event(s) waiting: {context}"
        )
        try:
            page = render_human_message(
                "planner_run_cap_reached",
                runs=already,
                cap=planner.max_runs_per_day,
                pending=len(pending),
                context=context,
            )
        except HumanMessageError as error:
            # The cap has already stopped the day; a template nobody can render must not also
            # take `wake` down with it, and this is the journal the operator reads anyway.
            print(f"ntfy: no page for the run cap -- {error}")
        else:
            _page_run_cap_once(page[:400], main=main, now=now)
        return summary

    context = event_context(pending)
    log_path = _invoke_planner(context, main=main)
    # The planner this wake just ran may itself have been refused; the next launch -- a role that
    # planner started, or the next exit hook's wake -- must not wait a tick to know (#429).
    _fold_role_quota_observations_or_say_why(main=main, now=now)
    _record_idle_wake_outcome_if_woken(pending, log_path, main=main)
    consume_events(pending, main=main)
    return f"planner run on {len(pending)} event(s): {context}"


def notify(message: str, *, main: Path = HOST_ROOT) -> None:
    """The ONE door to `agent_os/bin/notify.sh` in this module. `message` always arrives already
    rendered from `project.messages` (`agent_lib.render_human_message`): no wording an ntfy
    subscriber reads is written here, where `project.human_language` could not reach it."""
    subprocess.run([str(AGENT_OS_DIR / "bin" / "notify.sh"), message], check=False)


def cut_run(
    backend: str, reason: CutReason, *, worktree: Path, statefile: Path, main: Path = HOST_ROOT
) -> None:
    """agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md:
    stop the process and freeze what it left, both reused from worker_task.sh -- never
    reimplemented here -- then write the terminal state. The freeze is the driver's own
    `freeze_uncommitted_work`, reached through its `freeze` subcommand (#482), so a run a tick cuts
    is frozen exactly as a stage that exits without its commit is: the files it wrote and never
    added are in the `WIP: cut by guard (<reason>)` commit too, named in its body, while the diary
    and the `.env` link stay out and `git add -A` appears nowhere. The guard never restarts
    anything -- only the planner does, later."""
    driver = str(AGENT_OS_DIR / "bin" / "worker_task.sh")
    subprocess.run([driver, backend, "stop"], check=False)
    # `WORKER_WORKTREE` is the one override the driver reads its worktree from, so the tree this
    # tick measured is the tree that gets frozen; the driver passes it the same way when it
    # dispatches a sibling `open-pr`. Whatever the freeze does or fails to do, the terminal state
    # below is still written: a cut run is never left reading as alive.
    subprocess.run(
        [driver, backend, "freeze", reason],
        check=False,
        env={**os.environ, "WORKER_WORKTREE": str(worktree)},
    )
    write_state_line1(statefile, f"CUT_BY_GUARD reason={reason}")


def _mechanism_env(*, main: Path) -> dict[str, str]:
    """Every write the guard makes to the tracker -- a stall warning, a cleared label, a promotion
    to ready -- is the mechanism speaking, so it signs as the mechanism's own App rather than as
    whatever `gh` auth the invoking shell happens to carry. Without this the guard's comments were
    published under the human's personal account, attributing to a person words they never wrote
    (seen on #363, 2026-09-16). A missing or unmintable secret degrades to the ambient identity
    with no failure, exactly as `agent_apply_identity` does in the bash drivers."""
    env = os.environ.copy()
    env["AGENT_OS_HOST_ROOT"] = str(main)
    minted = subprocess.run(
        [agent_os_python(), "-m", "agent_os.gh_app_token", "--app", role_app_slug("guard")],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    token = minted.stdout.strip()
    if minted.returncode == 0 and token:
        env["GH_TOKEN"] = token
    return env


# How much of the worker's own log the stall warning carries. Three lines is one `EXPECT` plus the
# two that follow it, which is enough to tell "working on a long stage" from "stopped"; the
# character cap is because a `VERDE` line is a paragraph, and the warning repeats every tick.
PROGRESS_LINES_IN_WARNING = 3
PROGRESS_LINE_CHARS = 400


def recent_progress_lines(worktree: Path, count: int = PROGRESS_LINES_IN_WARNING) -> list[str]:
    """The worker's own last lines of `scratchpad/progress.log`. The guard reads the file itself,
    so carrying them into the warning costs no model turn on anyone's side -- the point is that
    the issue shows what the worker is doing, instead of only that it has not committed yet, and
    that reading it needs no shell on the host."""
    try:
        text = (worktree / "scratchpad" / "progress.log").read_text(errors="replace")
    except OSError:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [
        line if len(line) <= PROGRESS_LINE_CHARS else f"{line[:PROGRESS_LINE_CHARS]} [...]"
        for line in lines[-count:]
    ]


def stall_warning_body(since_commit: int, progress: list[str]) -> str:
    """Pure, so the shape of the comment is testable without a subprocess. The log goes in a fenced
    block: those lines carry backticks and parentheses of their own."""
    body = f"{since_commit} turns without a commit, still watching"
    if not progress:
        return body
    rendered = "\n".join(progress)
    return f"{body}\n\nLast lines of the worker's own `scratchpad/progress.log`:\n\n```\n{rendered}\n```"


def post_stall_warning(
    issue: str, since_commit: int, *, worktree: Path | None = None, main: Path = HOST_ROOT
) -> None:
    progress = recent_progress_lines(worktree) if worktree is not None else []
    subprocess.run(
        [
            agent_os_python(),
            "-m",
            "agent_os.issues",
            "update",
            issue,
            "--comment",
            stall_warning_body(since_commit, progress),
        ],
        cwd=main,
        env=_mechanism_env(main=main),
        check=False,
    )


def check(backend: str, *, main: Path = HOST_ROOT) -> str:
    """The exit hook. Runs inside worker_task.sh's own subshell, in the same process, right after
    the backend CLI exits on its own -- there is nothing left to evaluate, only the terminal state
    to record. A tick that cut this same run raced it (the SIGTERM that stops the run also kills
    this subshell before it reaches this call, in the common case) -- CUT_BY_GUARD is never
    overwritten regardless."""
    paths = worker_paths(backend, main)
    state, _, _ = read_state_marker(paths.statefile)
    issue = paths.issuefile.read_text().strip() if paths.issuefile.is_file() else ""
    where = f" on issue #{issue}" if issue else ""
    if state.startswith(RUN_CUT_STATES):
        # The event detail carries the state VERBATIM, which is what makes a failed launch
        # escalate its own cause on the planner's first tick: `FAILED_LAUNCH command=qwen` names
        # the missing executable where `CUT_BY_GUARD reason=no_stage_commit` named a symptom.
        write_event("worker_cut", backend, detail=f"{backend} {state}{where}", main=main)
        print(wake(main=main))
        return f"{backend}: already {state}"
    write_state_line1(paths.statefile, "DONE")
    write_event(
        "worker_finished", backend, detail=f"{backend} finished on its own{where}", main=main
    )
    print(wake(main=main))
    return f"{backend}: DONE"


@dataclass
class TickResult:
    """What one backend's check this tick found -- enough for `tick` to both print a line and
    decide, alongside the other backend's result, whether the planner needs a run."""

    backend: str
    message: str
    cut_reason: CutReason | None = None
    quota_changed: bool = False


def _tick_backend(
    backend: str, *, main: Path = HOST_ROOT, now: datetime | None = None
) -> TickResult:
    """One worker backend's check, holding that backend's verdict-file lock for its whole length:
    the worker path and `fold_role_quota_observations` are the file's two write sites inside this
    one module, and they can run in two processes at once (a timer's tick, a role's exit-hook
    `wake`), so the lock is what makes a lost update of the stall bookkeeping unreachable."""
    bookkeeping_path = worker_paths(backend, main).bookkeeping
    if not bookkeeping_path.parent.is_dir():
        # No cache directory, so no worker has ever run here and there is nothing to race on.
        return _tick_backend_locked(backend, main=main, now=now)
    with _bookkeeping_lock_path(bookkeeping_path).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _tick_backend_locked(backend, main=main, now=now)


def _tick_backend_locked(backend: str, *, main: Path, now: datetime | None) -> TickResult:
    paths = worker_paths(backend, main)
    now = now or datetime.now()  # noqa: DTZ005 -- naive, matches progress.log's own timestamps

    if not paths.statefile.is_file():
        return TickResult(backend, f"{backend}: never started")
    state, _, _ = read_state_marker(paths.statefile)
    if state.startswith(RUN_ENDED_STATES):
        return TickResult(backend, f"{backend}: {state}")

    if not _is_alive(paths.pidfile):
        # Died without the exit hook firing (crash, reboot) -- the same mechanical write, just late.
        write_state_line1(paths.statefile, "DONE")
        return TickResult(backend, f"{backend}: DONE (reaped by tick)")

    if not paths.issuefile.is_file():
        return TickResult(
            backend, f"{backend}: alive, no recorded issue -- cannot resolve a budget, skipping"
        )
    issue = paths.issuefile.read_text().strip()
    body = subprocess.run(
        ["gh", "issue", "view", issue, "--json", "body", "-q", ".body"],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if body.returncode != 0:
        return TickResult(backend, f"{backend}: alive, could not read issue #{issue}, skipping")
    task_class_name = parse_budget_line(body.stdout)
    classes = load_task_classes()
    if not task_class_name or task_class_name not in classes:
        return TickResult(
            backend, f"{backend}: alive, issue #{issue} has no resolvable budget, skipping"
        )
    task_class = classes[task_class_name]

    stream_parser = backend_stream_parser(backend)
    events = read_events(paths.events)
    summary = usage_summary(events, parser=stream_parser)
    progress_log = paths.worktree / "scratchpad" / "progress.log"
    progress_text = progress_log.read_text() if progress_log.is_file() else ""
    run_started_at = datetime.fromtimestamp(paths.statefile.stat().st_mtime)  # noqa: DTZ006 -- naive, matches progress.log
    # The guard's own observation of when the last line arrived (#419) -- mtime, not the timestamp
    # text inside the line. A file that does not exist yet has declared nothing, so its absence
    # cannot anchor anything; `run_started_at` here is inert, since `liveness_expired` only reads
    # `observed_at` when `last_declared_cutoff` found a declaration to begin with.
    progress_observed_at = (
        datetime.fromtimestamp(progress_log.stat().st_mtime)  # noqa: DTZ006 -- naive, matches progress.log
        if progress_log.is_file()
        else run_started_at
    )
    for warning in unparsable_cutoff_lines(progress_text):
        print(f"{backend}: {progress_log} {warning}")
    commit_ts = _commit_timestamps(paths.worktree, paths.startref)
    bookkeeping = scoped_to_run(_load_bookkeeping(paths.bookkeeping), _run_identity(paths, issue))
    tier, since_commit, bookkeeping = stall_detected(
        events, commit_ts, task_class, summary.turns, bookkeeping
    )

    # Tracked for both backends alike (Qwen's is always "allowed", per agent_lib's own quota_
    # exhausted docstring -- no signal, not a guess), so a change is only ever reported once a
    # prior tick has actually observed a baseline to compare against.
    current_quota = quota_status(events, parser=stream_parser)
    quota_changed = (
        bookkeeping.last_quota_status is not None and bookkeeping.last_quota_status != current_quota
    )
    bookkeeping.last_quota_status = current_quota

    reason: CutReason | None = None
    if budget_exceeded(
        summary,
        task_class,
        issue_cost_usd=cost_spent_on_issue(issue, paths.events, main, backend=backend),
        issue_total_tokens=tokens_spent_on_issue(issue, paths.events, main, backend=backend),
        parser=stream_parser,
    ):
        reason = "budget"
    # The backend's own `quota:` capability, never its name (#514): a backend whose stream carries
    # no quota signal to act on (`quota: none`) is recorded above and never cut here.
    elif backend_quota_cuts(backend, project=PROJECT) and current_quota == "exhausted":
        reason = "quota"
    elif (
        liveness_expired(progress_text, run_started_at, now, observed_at=progress_observed_at)
        or tier == "cut"
        or repeated_tool_calls(events)
    ):
        reason = "stall"

    if reason:
        cut_run(backend, reason, worktree=paths.worktree, statefile=paths.statefile, main=main)
        _save_bookkeeping(paths.bookkeeping, bookkeeping)
        # The page is for the case where nothing can proceed without a human, so it fires only for
        # a class that declares NO way round the exhausted window -- either route counts, the
        # planner's redispatch of a worker (`qwen_fallback_eligible`) and a role's own launch gate
        # (`fallback:`, #425), and one predicate answers for both so the cut and the launch can
        # never disagree about what was available. The cut above is unchanged either way: the
        # guard still cuts on the backend's own signal, and only the launch decides where the
        # work runs next
        # (agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-
        # agent.md).
        if reason == "quota" and not task_class.allows_backend_fallback:
            try:
                page = render_human_message(
                    "quota_exhausted_no_fallback",
                    issue=issue,
                    backend=backend,
                    task_class=task_class_name,
                )
            except HumanMessageError as error:
                # The cut is already committed and recorded. A misspelled key must not escape
                # into `tick`, where it would abort the OTHER backend's check as well over a
                # config typo nobody can act on from here.
                print(f"ntfy: no page for the exhausted quota -- {error}")
            else:
                notify(page, main=main)
        return TickResult(
            backend,
            f"{backend}: CUT_BY_GUARD reason={reason}",
            cut_reason=reason,
            quota_changed=quota_changed,
        )

    if tier == "warn" and bookkeeping.warned_at_turn_count != summary.turns:
        post_stall_warning(issue, since_commit, worktree=paths.worktree, main=main)
        bookkeeping.warned_at_turn_count = summary.turns
    _save_bookkeeping(paths.bookkeeping, bookkeeping)
    return TickResult(
        backend,
        f"{backend}: alive, {since_commit} turns since last commit (class {task_class_name})",
        quota_changed=quota_changed,
    )


def _gh_api_json(path: str, *, main: Path) -> list | dict | None:
    """`gh api` against this checkout's own repo (the `{owner}/{repo}` placeholders resolve from
    `cwd`), capped at one page of 100 -- blocked-on-human issues and their comment counts are not
    expected to run past that in practice, and this is a mechanical approximation like the queue
    check below, not an exhaustively paginated read.

    `--method GET` is load-bearing: `gh api` switches to POST as soon as a `-f` field is present,
    so without it this read POSTed `per_page` to the comments endpoint, got a 422 asking for a
    comment `body`, and returned None -- which made every human reply invisible and left a
    blocked-on-human issue blocked forever (#363, 2026-09-16)."""
    result = subprocess.run(
        ["gh", "api", "--method", "GET", path, "-f", "per_page=100"],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _parse_gh_timestamp(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _label_last_set(
    issue: str, label: str, *, main: Path, timeline: list | dict | None = None
) -> tuple[datetime, str] | None:
    """When a label was last put on an issue and WHO put it: the timeline's last `labeled` event
    for that label, as `(created_at, actor_login)`. The actor is what `nudged` needs (#413) -- a
    wake the mechanism set on itself must not count -- and a deleted account's event carries no
    actor, which reads as the empty login, never as the human. `timeline` lets a caller that has
    already read it (and needs to tell a failed read from an empty one) pass it in."""
    if timeline is None:
        timeline = _gh_api_json(f"repos/{{owner}}/{{repo}}/issues/{issue}/timeline", main=main)
    if not isinstance(timeline, list):
        return None
    labeled = [
        event
        for event in timeline
        if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == label
    ]
    if not labeled:
        return None
    last = labeled[-1]
    return _parse_gh_timestamp(last["created_at"]), (last.get("actor") or {}).get("login") or ""


def _label_last_set_at(issue: str, label: str, *, main: Path) -> datetime | None:
    last = _label_last_set(issue, label, main=main)
    return last[0] if last else None


def _latest_human_comment_at(issue: str, *, main: Path) -> datetime | None:
    """When the HUMAN last commented on this issue -- never when the mechanism last did. The
    planner announcing on #387 that it was waiting for an answer was read as the answer, and the
    issue left `status:blocked-on-human` with its question untouched (2026-09-16). `agent_lib.
    is_human_comment` is the one place that decides whose voice a comment is."""
    comments = _gh_api_json(f"repos/{{owner}}/{{repo}}/issues/{issue}/comments", main=main) or []
    if not isinstance(comments, list):
        return None
    # The comments endpoint returns oldest first, so the human's newest is the last one that is
    # theirs -- not the last one on the issue.
    for comment in reversed(comments):
        if is_human_comment((comment.get("user") or {}).get("login") or ""):
            return _parse_gh_timestamp(comment["created_at"])
    return None


def _remove_label(issue: str, label: str, *, main: Path) -> None:
    """Remove one label from one issue through `issues.py update`, as the mechanism's identity."""
    subprocess.run(
        [
            agent_os_python(),
            "-m",
            "agent_os.issues",
            "update",
            issue,
            "--remove-label",
            label,
        ],
        cwd=main,
        env=_mechanism_env(main=main),
        check=False,
    )


def _clear_blocked_label(issue: str, *, main: Path) -> None:
    _remove_label(issue, BLOCKED_ON_HUMAN_LABEL, main=main)


def _check_human_replies(*, main: Path = HOST_ROOT) -> list[str]:
    """The mechanical half of agent_os/docs/adr/2026-09-14-a-humans-reply-wakes-the-planner-never-the-
    worker-that-asked.md: for every open issue still labeled status:blocked-on-human, compare the
    label's own timestamp (the timeline's last "labeled" event for it) against the latest comment.
    A newer comment BY THE HUMAN clears the label and is reported so `tick` can fold it into the
    planner's context -- the planner itself, not this check, decides what the reply means. Whose
    comment it is has to be checked: an agent's own comment on the issue it is blocked on is the
    most likely comment there is, and counting it unblocks a question nobody answered (#387,
    2026-09-16)."""
    listing = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            BLOCKED_ON_HUMAN_LABEL,
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if listing.returncode != 0:
        return []
    try:
        rows = json.loads(listing.stdout or "[]")
    except ValueError:
        return []

    woken = []
    for row in rows:
        issue = str(row["number"])
        labeled_at = _label_last_set_at(issue, BLOCKED_ON_HUMAN_LABEL, main=main)
        latest_comment_at = _latest_human_comment_at(issue, main=main)
        if labeled_at and latest_comment_at and latest_comment_at > labeled_at:
            _clear_blocked_label(issue, main=main)
            woken.append(issue)
    return woken


def wake_labeled_issues(*, main: Path = HOST_ROOT) -> list[str]:
    """Open issues carrying `wake:planner` (#413). A `gh` failure reads as none -- and since only
    the tick that reads the label removes it, a nudge missed that way is simply read next tick."""
    rows = _gh_issue_list("number", main=main, extra=["--label", WAKE_PLANNER_LABEL])
    return [str(row["number"]) for row in rows if isinstance(row, dict) and "number" in row]


def _write_nudged_events(*, main: Path, now: datetime) -> list[EventOutcome]:
    """The human's sanctioned way to wake the planner now (#413): `wake:planner` on any open issue.
    The control-plane agent authenticates `gh` as the human, so it uses the same label and is read
    as the human; before this it had no way to wake the planner except waiting for an edge.

    Every label read here is REMOVED, whoever set it, so a label can never re-fire every tick. It
    becomes a `nudged` event only when the timeline's last `labeled` event for it names the human
    (`agent_lib.is_human_comment` is the one login test); set by one of the mechanism's own
    identities, or by nobody the timeline names, it is removed and ignored -- a planner that could
    label its way into its own next run would wake itself in a loop. The one exception to "always
    removed" is a timeline that could not be read at all: then who set it is not known YET, and
    the label is left for the next tick to read rather than a human's nudge silently dropped.

    Not rate-limited: `planner.max_runs_per_day` already bounds every wake. Called after the
    status:agents-paused early return in `tick`, so a paused mechanism leaves the label in place and
    it takes effect on the first tick after the pause is lifted."""
    outcomes = []
    for issue in wake_labeled_issues(main=main):
        timeline = _gh_api_json(f"repos/{{owner}}/{{repo}}/issues/{issue}/timeline", main=main)
        if not isinstance(timeline, list):
            outcomes.append(
                EventOutcome(
                    f"issue #{issue} carries {WAKE_PLANNER_LABEL} but its timeline could not be "
                    "read -- label left in place for the next tick, no event"
                )
            )
            continue
        last = _label_last_set(issue, WAKE_PLANNER_LABEL, main=main, timeline=timeline)
        _remove_label(issue, WAKE_PLANNER_LABEL, main=main)
        actor = last[1] if last else ""
        if last is None or not is_human_comment(actor):
            who = f"set by {actor}" if actor else "set by nobody the timeline names"
            outcomes.append(
                EventOutcome(
                    f"issue #{issue} carried {WAKE_PLANNER_LABEL} {who}, not the human -- label "
                    "cleared, no event (the mechanism must not be able to wake itself)"
                )
            )
            continue
        labeled_at = last[0].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_event(
            "nudged",
            issue,
            detail=(
                f"issue #{issue} carried {WAKE_PLANNER_LABEL}, set by {actor} at {labeled_at} -- "
                f"label cleared; the latest human comment on #{issue} says why"
            ),
            main=main,
            now=now,
        )
        outcomes.append(EventOutcome(f"nudged written (#{issue}, set by {actor})", written=1))
    return outcomes


def _agents_paused(*, main: Path = HOST_ROOT) -> bool:
    """The repo-wide, human-only full stop: status:agents-paused on the tracking epic (#12) skips
    invoking the planner at all, per agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-
    never-as-a-standing-process.md. A `gh` failure here is read as "not paused" rather than
    silently blocking the planner forever on a transient API hiccup."""
    result = subprocess.run(
        ["gh", "issue", "view", TRACKING_EPIC_ISSUE, "--json", "labels"],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return False
    return AGENTS_PAUSED_LABEL in label_names(data)


def _gh_issue_list(
    fields: str, *, main: Path, extra: list[str] | None = None, state: str = "open"
) -> list[dict]:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            state,
            "--json",
            fields,
            "--limit",
            "500",
            *(extra or []),
        ],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


@dataclass
class DispatchableScan:
    """One pass over the backlog: what a worker could be started on right now, and what was left
    out only because the backend it resolves to has no worktree on disk. The two are separated
    rather than merged into one list because they need opposite treatments -- the first wakes the
    planner, the second pages a human and must never be announced as dispatchable (#392)."""

    issues: list[int]
    # backend -> the issue numbers excluded because that backend has no worktree.
    without_worktree: dict[str, list[int]]


def backend_worktree_present(backend: str) -> bool:
    """The same test `worker_task.sh` applies before it refuses a dispatch with `no worktree at
    <path>`: `[ -e "$worktree/.git" ]` -- a file in a linked worktree, a directory in a plain
    clone, absent in a directory `gh pr merge --delete-branch` removed out from under the
    mechanism (#392)."""
    path = BACKEND_WORKTREES.get(backend)
    return bool(path) and (Path(path) / ".git").exists()


def dispatchable_scan(*, main: Path = HOST_ROOT) -> DispatchableScan:
    """Every open issue a worker could be started on right now, by the mechanical predicate in
    agent_lib (`is_dispatchable`): labeled status:ready, budget class resolving against
    config/agents.yaml, not blocked on a human, no open `Blocked by #N`. Two `gh` calls, whatever
    the backlog's size -- the second one is the set of open issue numbers every `Blocked by` line
    is resolved against, instead of one `gh issue view` per blocker.

    This replaces `_queue_not_empty`, which counted *any* open issue as queued and on 2026-09-14
    woke the planner 51 times for a backlog where not one issue carried a budget marker (#345).

    An issue whose class names a backend with NO WORKTREE is not dispatchable either, however
    valid its body is: the driver would refuse it with `no worktree at <path>` and the planner run
    that discovered that is spent for nothing (#392 -- on 2026-09-16 exactly that cost 1.03 USD
    and moved #389 to blocked-on-human for a cause unrelated to its brief). It is reported apart
    from the dispatchable ones so the tick can name the condition and page for it."""
    ready = _gh_issue_list("number,state,labels,body", main=main, extra=["--label", READY_LABEL])
    if not ready:
        return DispatchableScan([], {})
    open_numbers = {int(row["number"]) for row in _gh_issue_list("number", main=main)}
    classes = load_task_classes()
    issues: list[int] = []
    without_worktree: dict[str, list[int]] = {}
    present: dict[str, bool] = {}
    for row in ready:
        if not is_dispatchable(
            row,
            task_classes=classes,
            open_issue_numbers=open_numbers,
            labels=PROJECT.labels,
        ):
            continue
        number = int(row["number"])
        # `is_dispatchable` already required the budget line to resolve, so the class is there;
        # a `None` could only come from the two disagreeing, and that is not this check's to
        # decide -- it says nothing about worktrees and the issue stays dispatchable.
        task_class = classes.get(parse_budget_line(row.get("body") or "") or "")
        if task_class is None:
            issues.append(number)
            continue
        backend = task_class.backend
        if backend not in present:
            present[backend] = backend_worktree_present(backend)
        if present[backend]:
            issues.append(number)
        else:
            without_worktree.setdefault(backend, []).append(number)
    return DispatchableScan(issues, without_worktree)


def dispatchable_issues(*, main: Path = HOST_ROOT) -> list[int]:
    """What `dispatchable_scan` above found a worker could actually be started on."""
    return dispatchable_scan(main=main).issues


def missing_worktree_lines(scan: DispatchableScan) -> list[str]:
    """One journal line per backend whose missing worktree excluded something, naming the backend,
    the configured path and the issues -- so `journalctl --user -u <guard service>` shows the
    condition itself instead of the absence of an event (#392)."""
    return [
        f"backend {backend} has no worktree at "
        f"{BACKEND_WORKTREES.get(backend, '(unconfigured)')} -- not announcing "
        f"{', '.join(f'#{number}' for number in numbers)} as dispatchable"
        for backend, numbers in sorted(scan.without_worktree.items())
    ]


def _marker_timestamp(path: Path) -> datetime | None:
    """The instant a rate-limit marker file records, or None when there is none to read. Same
    shape `latest_event_at` has for events, for a page that writes no event at all."""
    if not path.is_file():
        return None
    try:
        return datetime.fromisoformat(path.read_text().strip())
    except ValueError:
        return None


def _page_missing_worktree_if_due(
    scan: DispatchableScan, *, main: Path = HOST_ROOT, now: datetime
) -> str | None:
    """Page the human for every backend whose missing worktree is holding ready work back. This is
    the second trigger of agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-
    human.md in its concrete form, not a third rule: on that backend nothing can proceed, and only
    a human can create the worktree (§7 row (r)). Which is why it is NOT conditioned on the whole
    dispatchable set being empty -- another backend still having work of its own does not make
    this one any less stopped, and the first version of this check would have stayed silent
    exactly then. No event kind either: the planner has nothing to act on for a condition it
    cannot clear.

    Rate-limited to one page per backend per `planner.idle_wake_minutes` by a timestamp marker
    under the guard's own cache directory, the way `_page_run_cap_once` makes "once a day"
    mechanical rather than remembered. One `backend_worktree_missing` page per due backend,
    rendered by `agent_lib.render_human_message` like every other page -- nothing here composes
    what the human reads (agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-
    human.md, amended 2026-09-16, #366)."""
    if not scan.without_worktree:
        return None
    minutes = load_planner_config().idle_wake_minutes
    due = []
    for backend in sorted(scan.without_worktree):
        marker = cache_dir(main) / "guard" / f"paged-missing-worktree-{backend}"
        last = _marker_timestamp(marker)
        if last is None or now - last >= timedelta(minutes=minutes):
            due.append((backend, marker))
    if not due:
        return None
    summaries = []
    for backend, marker in due:
        held_back = scan.without_worktree[backend]
        worktree = BACKEND_WORKTREES.get(backend, "(unconfigured)")
        summaries.append(
            f"{backend}: no worktree at {worktree}, {len(held_back)} issue(s) held back"
        )
        try:
            page = render_human_message(
                "backend_worktree_missing",
                backend=backend,
                worktree=worktree,
                issue_count=len(held_back),
            )
        except HumanMessageError as error:
            # A condition only a human can clear must not also depend on a config typo being
            # fixed first -- print it and let the rate limit still record the attempt, the same
            # way the quota and run-cap pages carry on past an unrenderable template.
            print(f"ntfy: no page for the missing worktree -- {error}")
        else:
            notify(page, main=main)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{now.isoformat()}\n")
    return f"paged: {'; '.join(summaries)}"


def refinable_issues(*, main: Path = HOST_ROOT) -> list[int]:
    """Every open issue the REFINER should be pointed at right now: labeled `status:refine` and
    carrying a STRUCTURAL defect (`agent_lib.needs_refinement`: a missing/misordered section or an
    unresolvable budget line) -- the mechanical half of
    agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md. An
    issue whose body is otherwise template-conformant but carries an open `Blocked by #N` is NOT
    refinable this way: that is not something the refiner wrote or can rewrite away (#356). Nor is
    one whose `<!-- refiner-summary -->` the human already replied to
    (`agent_lib.refiner_pass_answered_by_the_human`, agent-os#72): its doubt is settled, and the
    planner would only re-ask it. Same
    two-`gh`-call shape as `dispatchable_issues` above even though `needs_refinement` itself no
    longer reads `open_numbers` -- kept for the call-site's own symmetry with
    `promotable_to_ready`, which still needs it, below.

    Returned in REFINE QUEUE ORDER (`agent_lib.refine_queue_rank`): parent carries `auto-ready`,
    then priority, then no open blocker, then oldest first -- `refine_pending` names the head of
    this list and the planner launches the refiner on the earliest of it (#32)."""
    refine = _gh_issue_list(
        "number,state,labels,body,parent,comments", main=main, extra=["--label", REFINE_LABEL]
    )
    if not refine:
        return []
    open_numbers = {int(row["number"]) for row in _gh_issue_list("number", main=main)}
    classes = load_task_classes()
    needing = [
        row
        for row in refine
        if needs_refinement(
            row, task_classes=classes, open_issue_numbers=open_numbers, labels=PROJECT.labels
        )
        # A refiner doubt the human already answered is settled, not pending (agent-os#72).
        and not refiner_pass_answered_by_the_human(row, PROJECT)
    ]
    # `gh issue list` answers newest first, which on a large backlog handed the refiner the
    # lowest-priority, last-milestone issues first (#32): rank by closeness to dispatch instead.
    parent_labels_of = _parent_labels_lookup(main=main)
    needing.sort(
        key=lambda row: refine_queue_rank(
            row,
            parent_labels=parent_labels_of(row),
            open_issue_numbers=open_numbers,
            labels=PROJECT.labels,
        )
    )
    return [int(row["number"]) for row in needing]


def _write_refine_pending_event_if_due(*, main: Path, now: datetime) -> EventOutcome:
    """`refine_pending` is `idle_dispatchable`'s sibling for the refiner: a condition, not an edge,
    so it gets the same rate limit (`planner.idle_wake_minutes`), and it is written at all only
    once a human has flipped `planner.refiner_unattended` to true
    (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md)."""
    if not load_planner_config().refiner_unattended:
        return EventOutcome()
    issues = refinable_issues(main=main)
    if not issues:
        return EventOutcome()
    minutes = load_planner_config().idle_wake_minutes
    last = latest_event_at("refine_pending", main=main)
    if last is not None and now - last < timedelta(minutes=minutes):
        return EventOutcome(
            f"{len(issues)} issue(s) need refining, but the last refine_pending wake was "
            f"{int((now - last).total_seconds() // 60)}m ago (< {minutes}m) -- no event written"
        )
    listed = ", ".join(f"#{number}" for number in issues[:10])
    write_event(
        "refine_pending",
        str(issues[0]),
        detail=f"{len(issues)} issue(s) need refining: {listed}",
        main=main,
        now=now,
    )
    return EventOutcome(f"refine_pending written ({listed})", written=1)


def _gh_issue_labels(issue_number: int, *, main: Path) -> set[str] | None:
    """A single issue's current labels, or `None` when the lookup itself failed -- distinct from an
    issue that simply carries no labels (an empty set), so a `gh` hiccup never reads as "no
    auto-ready parent" and promotes something a human never opted in."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "labels"],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return None
    return label_names(data)


def _parent_labels_lookup(*, main: Path) -> Callable[[dict], set[str] | None]:
    """A row's parent's labels (`None` for no parent or a failed lookup), one `gh issue view` per
    distinct parent however many children share it -- the refine queue's order and the auto-ready
    promotion both ask this of every `status:refine` row."""
    cache: dict[int, set[str] | None] = {}

    def parent_labels(row: dict) -> set[str] | None:
        parent_number = (row.get("parent") or {}).get("number")
        if parent_number is None:
            return None
        if parent_number not in cache:
            cache[parent_number] = _gh_issue_labels(parent_number, main=main)
        return cache[parent_number]

    return parent_labels


def _current_status_label(issue_number: int, *, main: Path) -> str | None:
    """The issue's current status:* label -- there is supposed to be exactly one, `issues.py move`
    enforces that -- or `None` when the `gh` lookup failed, or the issue carries none of the
    vocabulary's labels at all (used only by the state-drift check below, where a lookup failure
    must read as "nothing to report" rather than as a drift)."""
    labels = _gh_issue_labels(issue_number, main=main)
    if labels is None:
        return None
    return next((name for name in PROJECT.labels.state_labels if name in labels), None)


def _log_state_drift(backend: str, statefile: Path, *, main: Path) -> str | None:
    """LOG ONLY -- never writes a planner event, because an event wakes the planner and costs
    money, and drift is a thing to notice, not itself a reason to act (#350 Part 4). Reads the
    `.state` marker `worker_task.sh` writes only after a successful `issues.py move` and compares
    it against GitHub's label right now; no marker (an old state file, or a run that never reached
    a move) is skipped silently."""
    state_line, issue, marker_label = read_state_marker(statefile)
    if issue is None or marker_label is None:
        return None
    github_label = _current_status_label(issue, main=main)
    drift = detect_state_drift(state_line, marker_label, github_label, labels=PROJECT.labels)
    if drift is None:
        return None
    return f"{backend}: state drift on #{issue} -- {drift}"


def _move_issue(issue: str, state: str, *, main: Path) -> bool:
    """The one door the guard has to the tracker's mechanical state: `issues.py move`, never a
    `gh` call of its own, so the label vocabulary, the "exactly one status label" rule and the
    board mirroring have a single implementation (agent_os/docs/adr/2026-09-14-driver-writes-mechanical-
    state-agent-writes-cooperative-state.md).

    Returns whether the move actually landed. The tick's own log is the only evidence anyone has
    that a reconciliation happened, so a caller must never print one for a `gh` call that
    failed -- `check=False` keeps a failed move from killing the tick, it does not make it a
    success."""
    result = subprocess.run(
        [
            agent_os_python(),
            "-m",
            "agent_os.issues",
            "move",
            issue,
            state,
        ],
        cwd=main,
        env=_mechanism_env(main=main),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _promote_issue_to_ready(issue: str, *, main: Path) -> None:
    _move_issue(issue, "ready", main=main)


def promote_refined(*, main: Path = HOST_ROOT) -> list[int]:
    """Every open `status:refine` issue whose parent carries `auto-ready` and whose body now
    validates gets moved to `status:ready` mechanically -- `promotable_to_ready` is already the
    tested decision, this is only the `gh`-backed selection and the one action it triggers
    (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md).
    An issue with no parent is never promotable this way; the human promotes it by hand."""
    refine = _gh_issue_list(
        "number,state,labels,body,parent,comments", main=main, extra=["--label", REFINE_LABEL]
    )
    if not refine:
        return []
    open_numbers = {int(row["number"]) for row in _gh_issue_list("number", main=main)}
    classes = load_task_classes()
    parent_labels_of = _parent_labels_lookup(main=main)
    promoted: list[int] = []
    for row in refine:
        if promotable_to_ready(
            row,
            parent_labels=parent_labels_of(row),
            task_classes=classes,
            open_issue_numbers=open_numbers,
            labels=PROJECT.labels,
        ):
            promoted.append(int(row["number"]))
    for number in promoted:
        _promote_issue_to_ready(str(number), main=main)
    return promoted


def reconcile_marker_path(main: Path = HOST_ROOT) -> Path:
    """When the tick last reconciled closed issues, so the next one asks only about what has
    closed since. Under the guard's own cache directory, beside the pages' markers."""
    return cache_dir(main) / "guard" / "reconciled-closed-at"


def closed_reconcile_since(*, main: Path = HOST_ROOT, now: datetime) -> datetime:
    """The oldest close this tick asks about: the previous pass's own timestamp, or
    `planner.reconcile_closed_lookback_days` back on the first run. ONE DAY OF SLACK on the
    marker, because the search is day-granular and a pass whose `gh` call failed still moves the
    marker -- re-asking for the previous day costs a handful of rows and closes the only window
    where a close could slip past unreconciled."""
    last = _marker_timestamp(reconcile_marker_path(main))
    if last is None:
        return now - timedelta(days=load_planner_config().reconcile_closed_lookback_days)
    return last - timedelta(days=1)


def closed_issues_with_status_label(
    *, main: Path = HOST_ROOT, now: datetime
) -> list[tuple[int, str]]:
    """Every issue CLOSED SINCE THE LAST PASS that still carries a `status:*` label, with the
    labels it is stuck on. GitHub's own auto-close on a merged PR's `Closes #N` never goes through
    `issues.py move`, so the label and the board column stay wherever the last move left them --
    observed on #333, #348, #349 and #357, all closed and still reading `status:review` (#365).

    Scoped by close date rather than listing every closed issue: `gh issue list` is capped at one
    page here as everywhere in this module, and an unfiltered listing ordered by creation would,
    past that cap, silently stop reconciling an OLD issue closed TODAY -- the one case this check
    exists for. It is also the most expensive call the tick makes."""
    since = closed_reconcile_since(main=main, now=now)
    rows = _gh_issue_list(
        "number,labels",
        main=main,
        state="closed",
        extra=["--search", f"closed:>={since.strftime('%Y-%m-%d')}"],
    )
    state_labels = set(PROJECT.labels.state_labels)
    stuck = []
    for row in rows:
        held = sorted(label_names(row) & state_labels)
        if held:
            stuck.append((int(row["number"]), ", ".join(held)))
    return stuck


def reconcile_closed_issues(*, main: Path = HOST_ROOT, now: datetime) -> list[str]:
    """Move every such issue to `done` through the same path `issues.py move N done` uses -- which
    strips the state label, leaves the already-closed issue closed, and mirrors the board column --
    and report one line per reconciliation for the tick's own log. Then record the pass, so the
    next tick asks only about what has closed since."""
    lines = []
    for number, held in closed_issues_with_status_label(main=main, now=now):
        if _move_issue(str(number), "done", main=main):
            lines.append(f"issue #{number}: closed but still labeled {held} -- moved to done")
        else:
            lines.append(
                f"issue #{number}: closed and still labeled {held}, but `issues.py move "
                f"{number} done` FAILED -- still not reconciled"
            )
    marker = reconcile_marker_path(main)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{now.isoformat()}\n")
    return lines


def live_worker_issues(main: Path = HOST_ROOT) -> dict[str, int]:
    """backend -> the issue its worker is running RIGHT NOW, for every backend whose process is
    alive and whose `.cache/worker_<backend>.issue` marker is readable. The liveness half is
    load-bearing wherever this is used: the marker outlives the run that wrote it, so reading
    every marker regardless would count a finished run as a live one."""
    running = {}
    for backend in BACKENDS:
        paths = worker_paths(backend, main)
        if not _is_alive(paths.pidfile) or not paths.issuefile.is_file():
            continue
        try:
            running[backend] = int(paths.issuefile.read_text().strip())
        except ValueError:
            continue
    return running


def restore_doing_label_on_live_runs(*, main: Path = HOST_ROOT) -> list[str]:
    """An issue a worker is RUNNING always carries `status:doing` (#385). When the label is
    cleared out from under a live run -- a human's reply clearing `status:blocked-on-human` while
    the worker is still alive, seen twice on #363 on 2026-09-16 -- the board shows nothing at all
    for work that is actively happening, and it self-heals only if the run reaches `open-pr`,
    which a cut run never does. The tick puts it back.

    ONLY when the issue carries no state label at all: any other one is normal downstream movement
    (the same distinction `detect_state_drift` makes), and overwriting it would be the guard
    fighting whoever set it. A failed `gh` lookup is not evidence of an absent label and is
    skipped, never read as one."""
    lines = []
    for backend, issue in live_worker_issues(main).items():
        labels = _gh_issue_labels(issue, main=main)
        if labels is None or any(name in labels for name in PROJECT.labels.state_labels):
            continue
        if _move_issue(str(issue), "doing", main=main):
            lines.append(
                f"issue #{issue}: {backend} is running it with no status label "
                f"-- set to {DOING_LABEL}"
            )
        else:
            lines.append(
                f"issue #{issue}: {backend} is running it with no status label and `issues.py "
                f"move {issue} doing` FAILED -- the board still shows nothing for a live run"
            )
    return lines


def orphan_doing_issues(*, main: Path = HOST_ROOT) -> list[int]:
    """Every open issue labeled `status:doing` that no live worker is running and that is not
    waiting on a human: work the board says is happening and nothing is doing. `_log_state_drift`
    catches the narrower marker-vs-label mismatch and only prints it (#350 Part 4); this is the
    wider case, and unlike drift it is not a thing to notice but a thing only the planner can
    resolve -- return it to `status:ready`, or ask the human if the branch carries uncommitted
    work (`agent_os/docs/AGENT_OS.md` §7 row (o))."""
    rows = _gh_issue_list("number,labels", main=main, extra=["--label", DOING_LABEL])
    if not rows:
        return []
    # A finished run's marker must NOT hide the orphan it just created -- which is exactly the
    # shape #363 left twice on 2026-09-16 (a cut run, the label still `status:doing`, nothing
    # running), so only a LIVE worker's issue counts as running.
    running = set(live_worker_issues(main).values())
    orphans = []
    for row in rows:
        if BLOCKED_ON_HUMAN_LABEL in label_names(row):
            continue
        number = int(row["number"])
        if number not in running:
            orphans.append(number)
    return orphans


def _write_orphan_doing_events_if_due(*, main: Path, now: datetime) -> list[EventOutcome]:
    """One `orphan_doing` event per orphaned issue, at most one per issue per
    `planner.idle_wake_minutes` -- per ISSUE, because two stuck issues are two facts and the
    planner acting on one says nothing about the other."""
    issues = orphan_doing_issues(main=main)
    if not issues:
        return []
    minutes = load_planner_config().idle_wake_minutes
    outcomes = []
    for number in issues:
        last = latest_event_at("orphan_doing", subject=str(number), main=main)
        if last is not None and now - last < timedelta(minutes=minutes):
            outcomes.append(
                EventOutcome(
                    f"issue #{number} is {DOING_LABEL} with no worker running it, but the last "
                    f"orphan_doing for it was {int((now - last).total_seconds() // 60)}m ago "
                    f"(< {minutes}m) -- no event written"
                )
            )
            continue
        write_event(
            "orphan_doing",
            str(number),
            detail=(
                f"issue #{number} is labeled {DOING_LABEL} and no worker is running it "
                "(orphan: return it to ready, or ask the human if its branch has uncommitted work)"
            ),
            main=main,
            now=now,
        )
        outcomes.append(EventOutcome(f"orphan_doing written (#{number})", written=1))
    return outcomes


# `subject:   #399` and `role:      validator (class validator)` -- two of the lines
# `agent_os/bin/agent_task.sh` writes into a run's log header before it detaches the run, so what a dead
# run was about is still readable from the log it left behind.
ROLE_RUN_SUBJECT_RE = re.compile(r"^subject:\s+#(?P<subject>\d+)", re.MULTILINE)
ROLE_RUN_ROLE_RE = re.compile(r"^role:\s+(?P<role>\S+)", re.MULTILINE)


@dataclass
class DeadRoleRun:
    """One one-shot role run the scan found dead. `subject` is the issue or pull request number the
    run was for, read from that log header, or the run's own stamp when the header carries no
    number: the event has to name the run either way, and a stamp is a filename the planner can
    still open."""

    role: str
    subject: str
    pidfile: Path
    logfile: Path
    death: RoleRunDeath


def dead_role_runs(*, main: Path = HOST_ROOT) -> list[DeadRoleRun]:
    """Every one-shot role run whose PID is dead while its PID file is still on disk, oldest first.
    That PID file is the whole of the contract (#400): the run removes it as its own last step, so
    one still sitting there is a run that never reached that step, and a dead PID beside it is a run
    that never will. A LIVE run's PID file is skipped however long it has been there -- a validator
    reads a pull request for as long as reading it takes.

    Each directory is scanned ONCE, and the role is read from the log header rather than from the
    directory's name: in the normal layout the two agree, but `AGENT_CACHE_DIR` -- the override a
    dry run against a stub backend uses -- resolves every role to the same directory, where scanning
    per role would find the same dead run twice and the directory name would be the wrong role's."""
    found = []
    scanned: set[Path] = set()
    for role in ONE_SHOT_ROLES:
        directory = role_run_dir(role, main)
        if directory in scanned or not directory.is_dir():
            continue
        scanned.add(directory)
        for pidfile in sorted(directory.glob("*.pid")):
            logfile = pidfile.with_suffix(".log")
            log_text = logfile.read_text(errors="replace") if logfile.is_file() else ""
            death = role_run_death(pid_alive=_is_alive(pidfile), log_text=log_text)
            if death is None:
                continue
            header_role = ROLE_RUN_ROLE_RE.search(log_text)
            header_subject = ROLE_RUN_SUBJECT_RE.search(log_text)
            found.append(
                DeadRoleRun(
                    role=header_role["role"] if header_role else role,
                    subject=header_subject["subject"] if header_subject else pidfile.stem,
                    pidfile=pidfile,
                    logfile=logfile,
                    death=death,
                )
            )
    return found


def _write_role_died_events(*, main: Path, now: datetime) -> list[EventOutcome]:
    """One `role_died` event per dead one-shot run, and the dead run's PID file removed once that
    event is written. The removal is what makes this an EDGE and not a condition: this tick fires
    every five minutes and nothing else ever clears the file, so leaving it there would hand the
    planner the same dead validator again -- one paid run per wake -- for as long as it sat. The
    evidence does not go with it: the run's log stays where it was, and the PID the file held is a
    line inside that log."""
    outcomes = []
    for run in dead_role_runs(main=main):
        # A subject read from the log header is a number; the stamp a headerless run falls back to
        # is not, and "on #20260917T092000Z" would send the planner to relaunch a subject that is a
        # timestamp -- the one thing this detail must not be is misleading about what to relaunch.
        named = f"on #{run.subject}" if run.subject.isdigit() else f"run {run.subject}"
        if run.death == "died_mid_run":
            detail = (
                f"the {run.role} {named} died mid-run, with no terminal result event in its log: "
                f"what it owed was never delivered and no {run.role}_finished was written. "
                f"Relaunch it (log: {run.logfile})"
            )
        else:
            detail = (
                f"the {run.role} {named} reached its backend's result event and then died before "
                f"writing {run.role}_finished, so its end was never announced. Read {run.logfile} "
                f"for whether it delivered, and relaunch it if it did not"
            )
        write_event("role_died", run.subject, detail=detail, main=main, now=now)
        run.pidfile.unlink(missing_ok=True)
        outcomes.append(
            EventOutcome(
                f"role_died written ({run.role} {named}: {run.death}, PID file removed)",
                written=1,
            )
        )
    return outcomes


# ----------------------------------------------------------------------------------------------
# The wider case #394 exists for, independent of the driver's own bookkeeping above: `dead_role_runs`
# only ever finds what a PID file names, and PR #391's own validator (2026-09-16) left NO PID file,
# no runs.tsv row and no event at all -- there was nothing on disk for that scan to find dead. This
# reads the OUTCOME the mechanism promised instead of the record a run is supposed to leave: a
# `status:ai-completed` issue whose pull request carries no review and for which no one-shot role
# run is alive right now has not been reviewed and nothing is reviewing it, whatever did or did not
# happen to the run that owed it.
# ----------------------------------------------------------------------------------------------

# The one convention `worker_task.sh`'s own `open-pr` writes into every pull request it opens
# (`--body "Closes #$issue`, its own first line) and the one `agent_os/bin/planner_task.sh` already
# tells the planner to read the same way ("which PR closes which issue"). Matched case-
# insensitively at the start of a line, the way GitHub itself recognises the keyword.
CLOSES_ISSUE_RE = re.compile(r"(?im)^closes #(?P<issue>\d+)\b")


@dataclass
class UnreviewedCompletion:
    """One `status:ai-completed` issue whose pull request has had no review at all and no live
    role run right now -- the review the mechanism owes it did not happen (#394)."""

    issue: int
    pull_request: int
    role: str


def _gh_pr_list(fields: str, *, main: Path, state: str = "open", limit: int = 500) -> list[dict]:
    """Pull requests in one state (open by default), the same shape `_gh_issue_list` gives issues.
    A `gh` failure reads as the empty list, exactly as there."""
    result = subprocess.run(
        ["gh", "pr", "list", "--state", state, "--json", fields, "--limit", str(limit)],
        capture_output=True,
        text=True,
        cwd=main,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def role_run_is_alive(subject: str, *, main: Path = HOST_ROOT) -> bool:
    """Whether ANY one-shot role has a run alive for this subject right now, read the same way
    `dead_role_runs` reads a dead one -- the PID file's own liveness, and the subject from the log
    header it carries. A validator twelve minutes into a review must never read as one that has
    gone silent just because nothing else on disk names it yet."""
    scanned: set[Path] = set()
    for role in ONE_SHOT_ROLES:
        directory = role_run_dir(role, main)
        if directory in scanned or not directory.is_dir():
            continue
        scanned.add(directory)
        for pidfile in directory.glob("*.pid"):
            if not _is_alive(pidfile):
                continue
            logfile = pidfile.with_suffix(".log")
            log_text = logfile.read_text(errors="replace") if logfile.is_file() else ""
            header_subject = ROLE_RUN_SUBJECT_RE.search(log_text)
            if header_subject and header_subject["subject"] == subject:
                return True
    return False


def unreviewed_completions(*, main: Path = HOST_ROOT) -> list[UnreviewedCompletion]:
    """Every open issue labeled `status:ai-completed` whose pull request (found the same way
    `planner_task.sh` tells the planner to find it: `Closes #N` in the body) carries no review at
    all and has no validator run alive on it right now. `validator` is the only one-shot role that
    ever posts a review, so it is the only one this names."""
    ai_completed_rows = _gh_issue_list("number", main=main, extra=["--label", AI_COMPLETED_LABEL])
    if not ai_completed_rows:
        return []
    ai_completed = {int(row["number"]) for row in ai_completed_rows}
    found = []
    for pull_request in _gh_pr_list("number,body,reviews", main=main):
        match = CLOSES_ISSUE_RE.search(pull_request.get("body") or "")
        if match is None:
            continue
        issue = int(match["issue"])
        if issue not in ai_completed or pull_request.get("reviews"):
            continue
        number = int(pull_request["number"])
        if role_run_is_alive(str(number), main=main):
            continue
        found.append(UnreviewedCompletion(issue=issue, pull_request=number, role="validator"))
    return sorted(found, key=lambda completion: completion.pull_request)


def unreviewed_completion_lines(found: list[UnreviewedCompletion]) -> list[str]:
    """One journal line per unreviewed pull request, printed every tick regardless of the page's
    own rate limit below -- the same split `missing_worktree_lines` keeps from its own page."""
    return [
        f"issue #{item.issue}: pull request #{item.pull_request} carries no review and no "
        f"{item.role} run is alive -- the review it owed never happened"
        for item in found
    ]


def _page_unreviewed_completions_if_due(
    found: list[UnreviewedCompletion], *, main: Path = HOST_ROOT, now: datetime
) -> str | None:
    """Page the human once per pull request per `planner.idle_wake_minutes`, the same marker-file
    shape `_page_missing_worktree_if_due` uses. NO new event kind: `agent_os/bin/planner_task.sh`
    already relaunches a validator on every `status:ai-completed` issue with no review each time it
    runs at all (LAUNCH THE VALIDATOR ON EVERY FINISHED PIECE OF WORK) -- turning this into an
    event would only retry the exact step that already failed silently once, with nothing telling
    a human it happened twice. A human seeing it is what breaks that loop."""
    if not found:
        return None
    minutes = load_planner_config().idle_wake_minutes
    due = []
    for item in found:
        marker = cache_dir(main) / "guard" / f"paged-unreviewed-{item.pull_request}"
        last = _marker_timestamp(marker)
        if last is None or now - last >= timedelta(minutes=minutes):
            due.append((item, marker))
    if not due:
        return None
    summaries = []
    for item, marker in due:
        summaries.append(f"#{item.pull_request} (issue #{item.issue}, {item.role})")
        try:
            page = render_human_message(
                "unreviewed_pull_request",
                pr=item.pull_request,
                issue=item.issue,
                role=item.role,
            )
        except HumanMessageError as error:
            # A page nobody can render must not also stop the tick from marking this pull request
            # paged -- print it and move on, the same way the other three pages that fail open do.
            print(f"ntfy: no page for the unreviewed pull request -- {error}")
        else:
            notify(page, main=main)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{now.isoformat()}\n")
    return f"paged: {'; '.join(summaries)}"


def _invoke_planner(context: str, *, main: Path = HOST_ROOT) -> Path | None:
    """Runs the planner synchronously -- `wake` is the one door, and this is the one place that
    calls the driver -- and returns the log it just wrote, so the caller can read the run's own
    terminal result (#426). `planner_task.sh` names the log off ITS OWN timestamp, taken after this
    call starts, so the file is found by what changed on disk rather than guessed by name: `None`
    when no new `.log` file appears, which every caller must read as "no evidence about this run",
    never as "it executed" or "it was rejected" -- a stubbed `_invoke_planner` in a test returns
    nothing here on purpose, and that must not be mistaken for a run the backend refused."""
    directory = planner_dir(main)
    before = {path.name for path in directory.glob("*.log")} if directory.is_dir() else set()
    subprocess.run(
        [str(AGENT_OS_DIR / "bin" / "planner_task.sh"), "run", context], cwd=main, check=False
    )
    if not directory.is_dir():
        return None
    new_logs = sorted(path for path in directory.glob("*.log") if path.name not in before)
    return new_logs[-1] if new_logs else None


def seen_dispatchable_path(main: Path = HOST_ROOT) -> Path:
    """Where the previous tick's dispatchable set is kept, so this tick can tell an issue that
    *became* dispatchable from a set that has merely been sitting there. Under the planner cache
    directory, so `PLANNER_CACHE_DIR` relocates it like the run log and `runs.tsv`."""
    return planner_dir(main) / "dispatchable_seen.json"


def read_seen_dispatchable(*, main: Path = HOST_ROOT) -> set[int] | None:
    """The previous tick's set, or `None` when there is no readable state file at all -- the two
    are different: an empty set means the last tick saw nothing dispatchable, `None` means this
    tick is the first and has no previous set to compare against. ANY content this cannot turn
    into a set of issue numbers reads as `None` -- not valid JSON, not a list, or a list with an
    element that is not a number (a torn write leaves exactly that). Never as an empty set, which
    would announce the whole standing backlog as newly dispatchable; and never as an exception,
    which a file only this function reads before rewriting would raise on every tick forever."""
    path = seen_dispatchable_path(main)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            return None
        return {int(number) for number in data}
    except (ValueError, TypeError, OSError):
        return None


def write_seen_dispatchable(issues: list[int], *, main: Path = HOST_ROOT) -> None:
    path = seen_dispatchable_path(main)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(issues)))


def _occupied_worker_slots(*, main: Path) -> tuple[int, int]:
    """(workers alive right now, `planner.max_parallel_issues`) -- the SAME cap
    `worker_task.sh start` refuses a dispatch against
    (agent_os/docs/adr/2026-09-15-parallelism-is-a-configured-cap-enforced-by-the-driver.md, #374), read
    with the SAME `_is_alive` reading `tick`'s own `any_alive` below already uses, so a
    `new_dispatchable` edge the tick suppresses for lack of a slot can never disagree with what the
    driver would have said about the same moment (#436) -- one implementation of "is there a free
    slot", not a second one that can drift from it."""
    alive = sum(1 for backend in BACKENDS if _is_alive(worker_paths(backend, main).pidfile))
    return alive, load_planner_config().max_parallel_issues


def _write_new_dispatchable_event_if_gained(
    issues: list[int], *, main: Path, now: datetime
) -> EventOutcome:
    """An issue BECOMING dispatchable is an edge, so it wakes the planner on the next tick and is
    NOT subject to `planner.idle_wake_minutes` -- unlike a set that merely sits dispatchable,
    which stays `idle_dispatchable`'s rate-limited condition
    (agent_os/docs/adr/2026-09-16-a-newly-dispatchable-issue-is-an-edge-not-a-condition.md).

    The very first tick after the state file does not exist records everything as already-seen and
    writes nothing: installing this must not fire a burst for a backlog that was already waiting.
    An issue that leaves the set and comes back counts as new when it returns, because the set is
    rewritten every tick rather than accumulated.

    A gain earns a planner pass only when there is a slot to dispatch into (#436): every parallel
    slot already occupied means a pass can only look and leave, at the same price as one that
    dispatches, so the event is withheld and the reason -- how many slots, against what cap -- is
    printed instead of staying silent (silence would read exactly like the guard being down). The
    issue still counts as SEEN either way (`write_seen_dispatchable` above runs first, unconditionally):
    it is not lost, because the mechanism's other edges -- `worker_finished`, `worker_cut`,
    `pr_merged` -- already wake the planner the moment a slot frees, and its own RULES tell it to
    pick a dispatchable issue on every wake regardless of which event named it."""
    previous = read_seen_dispatchable(main=main)
    write_seen_dispatchable(issues, main=main)
    if previous is None:
        return EventOutcome(
            f"first tick: {len(issues)} dispatchable issue(s) recorded as already-seen "
            "-- no edge event"
        )
    gained = [number for number in issues if number not in previous]
    if not gained:
        return EventOutcome()
    listed = ", ".join(f"#{number}" for number in gained[:10])
    occupied, cap = _occupied_worker_slots(main=main)
    if occupied >= cap:
        return EventOutcome(
            f"{len(gained)} issue(s) became dispatchable ({listed}) but {occupied} worker(s) "
            f"already running (>= planner.max_parallel_issues={cap}) -- no planner pass; a slot "
            "freeing wakes it instead"
        )
    write_event(
        "new_dispatchable",
        str(gained[0]),
        detail=f"{len(gained)} issue(s) became dispatchable since the last tick: {listed}",
        main=main,
        now=now,
    )
    return EventOutcome(f"new_dispatchable written ({listed})", written=1)


def merged_seen_path(main: Path = HOST_ROOT) -> Path:
    """Where the newest `mergedAt` any tick has already seen is kept (#413), beside
    `dispatchable_seen.json` and relocated by `PLANNER_CACHE_DIR` the same way."""
    return planner_dir(main) / "merged_seen.json"


def read_merged_seen(*, main: Path = HOST_ROOT) -> datetime | None:
    """The newest merge already seen, or `None` when there is no usable state -- which means "first
    tick", never "nothing was ever merged". ANY content that is not one timezone-aware ISO
    timestamp in a JSON string reads as `None` and never raises, for the same reason
    `read_seen_dispatchable` does not: a torn write must not break every tick after it."""
    path = merged_seen_path(main)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, str):
            return None
        stamp = _parse_gh_timestamp(data)
    except (ValueError, TypeError, OSError):
        return None
    return stamp if stamp.tzinfo is not None else None


def write_merged_seen(merged_at: datetime, *, main: Path = HOST_ROOT) -> None:
    path = merged_seen_path(main)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged_at.isoformat()))


def recently_merged_pull_requests(*, main: Path = HOST_ROOT) -> list[dict]:
    """The last 30 merged pull requests -- far more than merge between two five-minute ticks. A
    `gh` failure reads as the empty list, which the caller treats as "nothing learnt", never as
    "nothing merged"."""
    return _gh_pr_list("number,mergedAt,title", main=main, state="merged", limit=30)


def _write_pr_merged_event_if_gained(
    scan_issues: list[int], *, any_alive: bool, main: Path, now: datetime
) -> EventOutcome:
    """A merge is an edge the mechanism never wrote (#413). On 2026-09-17 the planner declined #404
    because Qwen's worktree was dirty; PR #411 merged and cleared exactly that, and nothing woke
    the planner until the rate-limited `idle_dispatchable` two hours later.

    The state is the newest `mergedAt` already seen, and it advances on EVERY merge seen, whether
    or not an event is written -- a merge is announced once or never, not held back for a later
    tick. The event itself is written only when it can lead somewhere: nothing is alive (a running
    worker's own `worker_finished` re-evaluates the board when it ends) and something is
    dispatchable. Like `new_dispatchable`, the first tick with no state records and writes nothing,
    and it is not rate-limited. An empty or failed listing neither advances nor rewrites the state:
    it says nothing about what merged."""
    merged = []
    for row in recently_merged_pull_requests(main=main):
        try:
            merged_at = _parse_gh_timestamp(row["mergedAt"])
        except (KeyError, TypeError, ValueError):
            continue
        if merged_at.tzinfo is not None:
            merged.append((merged_at, row))
    if not merged:
        return EventOutcome()
    merged.sort(key=lambda pair: pair[0], reverse=True)
    newest_at, newest = merged[0]

    previous = read_merged_seen(main=main)
    if previous is None:
        write_merged_seen(newest_at, main=main)
        return EventOutcome(
            f"first tick: newest merged pull request #{newest.get('number')} recorded as "
            "already-seen -- no edge event"
        )
    gained = [row for merged_at, row in merged if merged_at > previous]
    if not gained:
        return EventOutcome()
    write_merged_seen(newest_at, main=main)

    listed = ", ".join(
        f"#{row.get('number')} {row.get('title') or ''}".rstrip() for row in gained[:10]
    )
    seen = f"{len(gained)} pull request(s) merged since the last tick: {listed}"
    if any_alive:
        return EventOutcome(
            f"{seen} -- a worker is running and its own worker_finished re-evaluates; "
            "no pr_merged event"
        )
    if not scan_issues:
        return EventOutcome(f"{seen} -- nothing is dispatchable; no pr_merged event")
    dispatchable = ", ".join(f"#{number}" for number in scan_issues[:10])
    write_event(
        "pr_merged",
        str(newest.get("number")),
        detail=(
            f"{seen}; nothing is running and {len(scan_issues)} issue(s) are dispatchable: "
            f"{dispatchable}"
        ),
        main=main,
        now=now,
    )
    return EventOutcome(
        f"pr_merged written (#{newest.get('number')}; dispatchable {dispatchable})", written=1
    )


def _event_kind_of(filename: str) -> str | None:
    match = EVENT_NAME_RE.match(filename)
    return match["kind"] if match else None


def _event_timestamp_of(filename: str) -> datetime:
    match = EVENT_NAME_RE.match(filename)
    return datetime.strptime(match["ts"], EVENT_TS_FORMAT).replace(tzinfo=UTC)


def _planner_run_produced_no_work(log_path: Path | None) -> bool:
    """Whether the planner run just invoked never reached a turn -- the backend refused it (quota,
    a rate limit, any other api_error) before the model ran at all, which is the shape #426 is
    about: `usage_failed` is the one existing predicate for "this run produced nothing" (already
    used for a worker's own usage report), applied here to the planner's log instead of a stage's.
    `log_path` is `None` when there is nothing to read -- a stubbed `_invoke_planner` in a test, or
    a launch that wrote no log at all -- and that reads as "not shown to have failed", never as
    "failed": absence of evidence must not discard a real idle_dispatchable wake."""
    if log_path is None or not log_path.is_file():
        return False
    return usage_failed(usage_summary(read_events(log_path)))


def _idle_wake_outcome_path(main: Path = HOST_ROOT) -> Path:
    """Beside `dispatchable_seen.json`, and relocated by `PLANNER_CACHE_DIR` the same way: whether
    the last `idle_dispatchable` event a planner run actually consumed came from a run that
    executed, so `_write_idle_event_if_due` can tell a working rate limit (the last wake executed
    and is still inside its window) from one a rejected run is blocking on nothing (#426)."""
    return planner_dir(main) / "idle_wake_outcome.json"


def _record_idle_wake_outcome_if_woken(
    pending: list[Path], log_path: Path | None, *, main: Path
) -> None:
    """Called once per `wake`, right after the planner run it invoked ends: if the batch it was
    handed included an `idle_dispatchable` event, remember whether THAT run executed. Nothing else
    ever writes this file, so `_write_idle_event_if_due`'s next read is always the verdict on the
    most recent `idle_dispatchable` wake, never a stale one from a kind that was not part of this
    run."""
    idle_paths = [path for path in pending if _event_kind_of(path.name) == "idle_dispatchable"]
    if not idle_paths:
        return
    latest_at = max(_event_timestamp_of(path.name) for path in idle_paths)
    executed = not _planner_run_produced_no_work(log_path)
    path = _idle_wake_outcome_path(main)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": latest_at.strftime(EVENT_TS_FORMAT), "executed": executed}))


def _read_idle_wake_outcome(*, main: Path) -> tuple[datetime, bool] | None:
    """`(event timestamp, executed)` for the last `idle_dispatchable` wake the guard has a verdict
    on, or `None` when there is no verdict at all -- not valid JSON, not the shape this writes, or
    no file yet. `None` is read by the caller as "assume it executed", exactly like every other
    torn-write fallback in this module: it must never let a stale or unreadable file leave the rate
    limit permanently open."""
    path = _idle_wake_outcome_path(main)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        at = datetime.strptime(data["at"], EVENT_TS_FORMAT).replace(tzinfo=UTC)
        return at, bool(data["executed"])
    except (ValueError, TypeError, OSError, KeyError):
        return None


def _write_idle_dispatchable(issues: list[int], *, main: Path, now: datetime) -> str:
    listed = ", ".join(f"#{number}" for number in issues[:10])
    write_event(
        "idle_dispatchable",
        str(issues[0]),
        detail=f"nothing is running and {len(issues)} issue(s) are dispatchable: {listed}",
        main=main,
        now=now,
    )
    return listed


def _write_idle_event_if_due(issues: list[int], *, main: Path, now: datetime) -> EventOutcome:
    """`idle_dispatchable` is the one event kind a *condition* rather than an edge produces, so it
    is the one kind that is rate-limited: at most one per `planner.idle_wake_minutes`, and only
    when something is actually dispatchable. `issues` is what `dispatchable_scan` found the tick
    could really start -- an issue excluded for a missing worktree is not in it, so no planner run
    is spent rediscovering a condition only a human can clear (#392).

    The rate limit is about how often the planner is WOKEN *for it*, which is not the same as how
    often the event was merely WRITTEN (#426): a run the backend rejected before its first turn
    never looked at the issues the previous event named, so that wake must not go on blocking a
    fresh one for the rest of the window. `_read_idle_wake_outcome` is what tells a wake that
    executed from one that was rejected; `_record_idle_wake_outcome_if_woken` is what records it,
    the moment the run that consumed the event ends."""
    if not issues:
        return EventOutcome()
    minutes = load_planner_config().idle_wake_minutes
    last = latest_event_at("idle_dispatchable", main=main)
    if last is not None and now - last < timedelta(minutes=minutes):
        outcome = _read_idle_wake_outcome(main=main)
        rejected_by_backend = outcome is not None and outcome[0] == last and not outcome[1]
        if not rejected_by_backend:
            return EventOutcome(
                f"{len(issues)} dispatchable issue(s) and nothing running, but the last idle wake "
                f"was {int((now - last).total_seconds() // 60)}m ago (< {minutes}m) and executed "
                "-- no event written, rate-limited by a run that executed"
            )
        listed = _write_idle_dispatchable(issues, main=main, now=now)
        return EventOutcome(
            f"idle_dispatchable written ({listed}) -- the last wake "
            f"({int((now - last).total_seconds() // 60)}m ago) was rejected by the backend before "
            "it executed a turn, writing again",
            written=1,
        )
    listed = _write_idle_dispatchable(issues, main=main, now=now)
    return EventOutcome(f"idle_dispatchable written ({listed})", written=1)


def _report(outcome: EventOutcome) -> int:
    """Print what the helper had to say, and hand back the number of events it wrote -- the tick
    adds up facts, never the shape of its own log lines."""
    if outcome.line:
        print(outcome.line)
    return outcome.written


# ----------------------------------------------------------------------------------------------
# Role rejections into the quota verdict (#429). The verdict file `agent_guard_<backend>.json` has
# ONE writer, this module: `_tick_backend` writes it from a live worker's stream, and
# `fold_role_quota_observations` below writes it from the logs the roles leave behind -- the
# planner, the validator, the refiner -- which no worker stream ever sees. The role launch paths
# (`agent_lib.role_launch`, through `agent_task.sh` and `planner_task.sh`) only ever READ it.
#
# The fold runs at the start of BOTH guard entry points that can launch a role: `tick`, before any
# check or wake, and `_wake_locked`, under `planner.lock`, before it invokes the planner -- and once
# more in `_wake_locked` right after that planner returns. The wake is the one that matters most: a
# role's exit hook writes `<role>_finished` and calls `wake` directly, with no tick in between, so a
# validator rejected by Claude's window would otherwise be followed at once by a planner launched
# on the same exhausted window; and a planner the wake ran and Claude refused is on the verdict
# before the wake returns, not a tick later.
#
# A run's observation is dated by its exit marker (`<log>.exited`, `agent_lib.role_run_exit_marker`),
# which the driver writes the moment the backend returns -- never by the log's mtime, which the
# detached half keeps moving after the `result` (the exit hook's whole planner run lands in it).
#
# What is read is the backend's own terminal record, through the same detector the worker path
# uses (`quota_status`), never what the agent wrote about itself
# (agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md).
# ----------------------------------------------------------------------------------------------

# `backend:   qwen (FALLBACK for claude -- ...)` or `backend:   claude` -- the line both role
# drivers write into a run's log header (`agent_backend_identity_line`), naming the backend that
# actually RAN, which on a substituted run is not the class's own.
ROLE_RUN_BACKEND_RE = re.compile(r"^backend:\s+(?P<backend>\S+)", re.MULTILINE)

RoleQuotaStatus = Literal["allowed", "exhausted"]


@dataclass(frozen=True)
class RoleQuotaObservation:
    """What one role run's log says about its backend's quota, and when the backend said it.

    `observed_at_ns` is the moment the run's backend process exited, read off the exit marker the
    driver wrote right then (`agent_lib.read_role_run_exit_marker`) and never rewrites. It is not
    the log's mtime: the detached half keeps appending to its log after the backend's `result` --
    the exit hook's `wake`, which runs a whole planner synchronously, and the worktree's removal --
    so the mtime of a validator's log can be later than the planner run that validator woke. Dated
    by the marker, the same run yields the same time on every fold that reads it, which is what
    lets a rejection age out instead of being refreshed, and a planner refused after the validator
    that woke it is the newer of the two."""

    backend: str
    status: RoleQuotaStatus
    observed_at_ns: int
    log: Path


def role_run_quota_status(log_path: Path) -> RoleQuotaStatus | None:
    """`exhausted`, `allowed`, or None for "this run says nothing about the quota".

    - A run with no terminal `result` -- still running, killed, crashed, empty -- is None: it ended
      before the backend could say anything, and leaves the verdict as it was.
    - A terminal run the backend refused on quota, by the worker path's own detector: `exhausted`.
    - A terminal run that worked -- no error, at least one turn (`usage_failed` false): `allowed`.
    - Any other failure (a transport error, an error result without the quota shape): None.

    The result TEXT is never read for a claim: a run whose assistant wrote "my quota is exhausted"
    and finished cleanly is an `allowed` observation, because the backend served every turn of it.

    The log is read with the parser of the backend its header names; a log from before the drivers
    wrote that header is read with the default shape."""
    header = ROLE_RUN_BACKEND_RE.search(log_path.read_text(errors="replace"))
    stream_parser = backend_stream_parser(header["backend"] if header else None)
    events = read_events(log_path)
    summary = usage_summary(events, parser=stream_parser)
    if summary.result is None:
        return None
    if quota_status(events, parser=stream_parser) == "exhausted":
        return "exhausted"
    if not usage_failed(summary):
        return "allowed"
    return None


def _role_log_directories(*, main: Path) -> dict[Path, str]:
    """Every directory a role's per-run logs land in, mapped to the backend of the class that owns
    it -- the attribution for a log written before the drivers named their backend in the header.
    Roles come from `config/agents.yaml` (every class whose `role:` is not `worker`), and the planner
    keeps its own directory override, exactly as `planner_task.sh` does."""
    directories: dict[Path, str] = {}
    for task_class in load_task_classes().values():
        if task_class.role == "worker":
            continue
        role = task_class.role
        directory = planner_dir(main) if role == "planner" else role_run_dir(role, main)
        directories.setdefault(directory, task_class.backend)
    return directories


def role_log_quota_observations(
    *, main: Path = HOST_ROOT, now: datetime | None = None
) -> dict[str, RoleQuotaObservation]:
    """The most recent role-run observation per backend, among the runs whose backend exited
    inside the verdict's TTL. A run with no exit marker is not read at all -- still running, died
    before its backend returned, or older than the marker -- and neither is one whose marker is
    older than the TTL: a verdict built from it would read `unknown` at every launch anyway, and
    writing it would only hand the next worker tick a stale baseline.

    A log that vanishes or cannot be read between the listing and the read is skipped, not raised:
    one unreadable run must not hide what the others say."""
    now = now or datetime.now(UTC)
    ttl_seconds = load_mechanism().quota_verdict_ttl_minutes * 60
    oldest_ns = int((now.timestamp() - ttl_seconds) * 1e9)
    latest: dict[str, RoleQuotaObservation] = {}
    for directory, class_backend in _role_log_directories(main=main).items():
        if not directory.is_dir():
            continue
        for marker in directory.glob(f"*.log{ROLE_RUN_EXIT_MARKER_SUFFIX}"):
            log_path = marker.with_name(marker.name.removesuffix(ROLE_RUN_EXIT_MARKER_SUFFIX))
            exited_at = read_role_run_exit_marker(log_path)
            if exited_at is None:
                continue
            observed_at_ns = int(exited_at.timestamp() * 1e9)
            if observed_at_ns < oldest_ns:
                continue
            try:
                status = role_run_quota_status(log_path)
                header = ROLE_RUN_BACKEND_RE.search(log_path.read_text(errors="replace"))
            except OSError:
                continue
            if status is None:
                continue
            backend = header["backend"] if header else class_backend
            known = latest.get(backend)
            if known is None or observed_at_ns > known.observed_at_ns:
                latest[backend] = RoleQuotaObservation(backend, status, observed_at_ns, log_path)
    return latest


def _bookkeeping_lock_path(bookkeeping: Path) -> Path:
    return bookkeeping.with_name(bookkeeping.name + ".lock")


def fold_role_quota_observations(
    *, main: Path = HOST_ROOT, now: datetime | None = None
) -> list[str]:
    """Writes each backend's newest role-run observation into that backend's verdict file, when it
    is NEWER than what the file already holds, and returns one line per write for the caller's log.

    "Newer" is measured against the file's own mtime, which is the verdict's age for every reader
    (`agent_lib.read_persisted_quota_verdict`): the write sets that mtime to the observation's time,
    not to now, so the same run read again by the next fold is not newer than itself and changes
    nothing -- and a live worker's tick, which rewrites the file at its own now, stays the most
    recent observation for as long as it runs. Only `last_quota_status` changes; the stall
    bookkeeping in the same file is carried over as it was.

    Never WRITES `quota_changed`: that trigger belongs to the worker path. It does move the baseline
    that path compares against, though, so while a worker of the same backend is alive, a fold that
    changes `last_quota_status` can make that worker's next tick see a change and emit the event --
    see `docs/modules/workers.md`. The lock is taken without blocking, and a file whose lock is
    held -- a tick checking that backend's live worker right now -- is left for the next fold rather
    than waited on, so a worker's exit hook that reaches `wake` while the tick is cutting that very
    worker can never deadlock against it. A verdict file that does not parse is left alone with a
    line: it is the worker path's to rewrite, and guessing its stall bookkeeping would be worse."""
    lines = []
    for backend, observation in sorted(role_log_quota_observations(main=main, now=now).items()):
        bookkeeping_path = worker_paths(backend, main).bookkeeping
        bookkeeping_path.parent.mkdir(parents=True, exist_ok=True)
        with _bookkeeping_lock_path(bookkeeping_path).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lines.append(f"{backend}: verdict file busy -- role observation left for next fold")
                continue
            if (
                bookkeeping_path.is_file()
                and bookkeeping_path.stat().st_mtime_ns >= observation.observed_at_ns
            ):
                continue
            try:
                bookkeeping = _load_bookkeeping(bookkeeping_path)
            except (ValueError, TypeError) as error:
                lines.append(
                    f"{backend}: {bookkeeping_path.name} unreadable ({error}) -- not folded"
                )
                continue
            bookkeeping.last_quota_status = observation.status
            _save_bookkeeping(bookkeeping_path, bookkeeping)
            os.utime(bookkeeping_path, ns=(observation.observed_at_ns, observation.observed_at_ns))
        lines.append(
            f"{backend}: quota verdict {observation.status} from role log {observation.log.name}"
        )
    return lines


# What a fold can meet on a host and must not let out of `tick` or `wake`: a file that vanishes or
# is unreadable (OSError), a verdict file or a config that does not parse (ValueError -- which
# covers JSONDecodeError and pydantic's ValidationError --, TypeError, yaml.YAMLError), and a
# config that names no such key (KeyError).
FOLD_ERRORS = (OSError, ValueError, TypeError, KeyError, yaml.YAMLError)


def _fold_role_quota_observations_or_say_why(*, main: Path, now: datetime) -> None:
    """The fold as its two callers run it: an enrichment of the verdict, never a precondition of
    the tick or the wake that follows. A failure prints one line -- the verdict stays what it was,
    which is exactly what a run that said nothing would have left -- and the caller goes on."""
    try:
        lines = fold_role_quota_observations(main=main, now=now)
    except FOLD_ERRORS as error:
        lines = [f"role quota fold skipped -- {type(error).__name__}: {error}"]
    for line in lines:
        print(line)


def tick(*, main: Path = HOST_ROOT, now: datetime | None = None) -> None:
    """The monitor tick, per agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-never-as-
    a-standing-process.md. Each backend is checked first (budget/liveness/stall/quota -- may cut a
    run); then every edge it found is written as an event and `wake` decides, under the lock,
    whether that adds up to a planner run. Nothing at all happens while the tracking epic carries
    status:agents-paused."""
    now = now or datetime.now(UTC)
    # First, so every launch this tick leads to reads a verdict that already includes the roles'
    # own rejections; each worker check below then overwrites it with its live stream (#429).
    _fold_role_quota_observations_or_say_why(main=main, now=now)
    results = [_tick_backend(backend, main=main) for backend in BACKENDS]
    for result in results:
        print(result.message)

    # LOG ONLY, never a planner event (#350 Part 4) -- run regardless of status:agents-paused
    # below, since this is visibility, not an action the pause is meant to stop.
    for backend in BACKENDS:
        drift_line = _log_state_drift(backend, worker_paths(backend, main).statefile, main=main)
        if drift_line:
            print(drift_line)

    if _agents_paused(main=main):
        print(f"epic #{TRACKING_EPIC_ISSUE} carries {AGENTS_PAUSED_LABEL} -- no events, no planner")
        return

    # MECHANICAL STATE THE TICK RECONCILES ITSELF, before anything is announced: a closed issue
    # still carrying a status label, and the promotion of a refined child whose parent opted in.
    # Neither depends on the planner remembering to run it any more (#365, `agent_os/docs/AGENT_OS.md` §7
    # rows (j) and (l)); `promote_refined` runs first so an issue it promotes is already in this
    # tick's own dispatchable scan below.
    for line in reconcile_closed_issues(main=main, now=now):
        print(line)
    promoted = promote_refined(main=main)
    if promoted:
        print(f"promoted to {READY_LABEL}: {', '.join(f'#{number}' for number in promoted)}")

    written = 0
    for result in results:
        if result.cut_reason:
            write_event(
                "worker_cut",
                result.backend,
                detail=f"{result.backend} was cut (reason={result.cut_reason})",
                main=main,
                now=now,
            )
            written += 1
        if result.quota_changed:
            write_event(
                "quota_changed",
                result.backend,
                detail=f"{result.backend}'s quota status changed",
                main=main,
                now=now,
            )
            written += 1

    # A one-shot role's run that died (#400): its PID file is still on disk because the run removes
    # it as its own last step, and the process behind it is gone. Whether it died mid-turn or after
    # its backend had finished, its `<role>_finished` event never came -- and that event is the only
    # thing that brings the planner back to the pull request the role owed a review, which is how
    # #399 was left with nothing recording that its review never happened.
    for outcome in _write_role_died_events(main=main, now=now):
        written += _report(outcome)

    # The independent safety net beside it (#394): reads the OUTCOME rather than a PID file, so a
    # run that died before ever writing one -- PR #391's own case -- still surfaces here. Reported
    # and paged whether or not `role_died` above found anything for it.
    unreviewed = unreviewed_completions(main=main)
    for line in unreviewed_completion_lines(unreviewed):
        print(line)
    unreviewed_page = _page_unreviewed_completions_if_due(unreviewed, main=main, now=now)
    if unreviewed_page:
        print(unreviewed_page)

    for issue in _check_human_replies(main=main):
        print(
            f"issue #{issue}: a newer comment landed while {BLOCKED_ON_HUMAN_LABEL} -- label cleared"
        )
        write_event(
            "human_replied",
            issue,
            detail=f"issue #{issue} got a reply while {BLOCKED_ON_HUMAN_LABEL}, label cleared",
            main=main,
            now=now,
        )
        written += 1

    # The human's own wake (#413), right after the replies: read only past the agents-paused return
    # above, so a paused mechanism leaves `wake:planner` in place until the pause is lifted.
    for outcome in _write_nudged_events(main=main, now=now):
        written += _report(outcome)

    # A live run's own label, after the replies above: clearing status:blocked-on-human is
    # exactly what leaves an alive worker's issue blank on the board (#385, tick side).
    for line in restore_doing_label_on_live_runs(main=main):
        print(line)

    # ONE scan per tick, whether or not a worker is alive: the edge below is about the backlog
    # changing, not about a slot being free, and how much may run at once is the driver's own
    # configured cap (`planner.max_parallel_issues`), never something the guard second-guesses
    # (agent_os/docs/adr/2026-09-16-a-newly-dispatchable-issue-is-an-edge-not-a-condition.md).
    scan = dispatchable_scan(main=main)
    written += _report(_write_new_dispatchable_event_if_gained(scan.issues, main=main, now=now))

    # Reported and paged for whether or not a worker is alive: a backend with no worktree is
    # stopped, and a live run on the OTHER backend says nothing about it.
    for line in missing_worktree_lines(scan):
        print(line)
    paged = _page_missing_worktree_if_due(scan, main=main, now=now)
    if paged:
        print(paged)

    # A merge (#413) and the idle condition read the same two facts -- is anything alive, what is
    # dispatchable -- so liveness is read once for both. When `pr_merged` fires, `idle_dispatchable`
    # is skipped this tick: `wake` would group both into ONE planner run anyway, so this only avoids
    # a redundant event whose content `pr_merged`'s detail already carries, and keeps the idle
    # clock (`planner.idle_wake_minutes`) from being spent on a wake the merge already caused.
    any_alive = any(_is_alive(worker_paths(backend, main).pidfile) for backend in BACKENDS)
    merged_outcome = _write_pr_merged_event_if_gained(
        scan.issues, any_alive=any_alive, main=main, now=now
    )
    written += _report(merged_outcome)
    if not any_alive and not merged_outcome.written:
        written += _report(_write_idle_event_if_due(scan.issues, main=main, now=now))

    for outcome in _write_orphan_doing_events_if_due(main=main, now=now):
        written += _report(outcome)

    written += _report(_write_refine_pending_event_if_due(main=main, now=now))

    print(f"{written} event(s) written this tick")
    print(wake(main=main, now=now))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("backend", choices=BACKENDS)
    sub.add_parser("tick")
    sub.add_parser("wake")
    sub.add_parser("promote-refined")
    # For the one-shot role drivers (agent_os/bin/agent_task.sh), which write their own end as an
    # event and then call `wake`, the same two steps the exit hook takes for a worker.
    event_parser = sub.add_parser("event")
    event_parser.add_argument("kind", choices=EVENT_KINDS)
    event_parser.add_argument("subject")
    event_parser.add_argument("--detail", default="")
    args = parser.parse_args()
    if args.command == "check":
        print(check(args.backend))
    elif args.command == "tick":
        tick()
    elif args.command == "wake":
        print(wake())
    elif args.command == "event":
        print(write_event(args.kind, args.subject, detail=args.detail))
    elif args.command == "promote-refined":
        promoted = promote_refined()
        if promoted:
            print(f"promoted to status:ready: {', '.join(f'#{n}' for n in promoted)}")
        else:
            print("nothing to promote")


if __name__ == "__main__":
    main()
