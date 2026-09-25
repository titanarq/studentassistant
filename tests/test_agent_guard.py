"""The guard's decision functions (`agent_os.guard`): budget, liveness, commit-cadence
stall, repeated-tool-call stall, and quota -- each pure, exercised over inline fixture events and
text, no live agent needed.

Pure filesystem. This file must not request the `engine` or `db_sandbox` fixture: nothing here
touches the shared database.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_os import guard as agent_guard
from agent_os import lib as agent_lib
from agent_os.cli import AGENT_OS_DIR, host_root
from agent_os.guard import (
    DEFAULT_LIVENESS_CUTOFF,
    StallBookkeeping,
    TickResult,
    budget_exceeded,
    detect_state_drift,
    last_declared_cutoff,
    liveness_expired,
    parse_duration,
    quota_status,
    read_state_marker,
    repeated_tool_calls,
    stall_detected,
    turns_since_commit,
    unparsable_cutoff_lines,
    write_state_line1,
)
from agent_os.lib import (
    REQUIRED_SECTIONS,
    RUNS_TSV_HEADER,
    LabelVocabulary,
    PlannerConfig,
    ProjectConfig,
    TaskClass,
    UsageSummary,
    read_events,
    render_human_message,
)

# The HOST project this suite runs inside: not a fixed nesting under AGENT_OS_DIR (that
# breaks the moment a copy IS the mechanism's own top directory, as the out-of-tree proof
# makes it -- #512), but whatever `host_root()` itself resolves: the git checkout's toplevel,
# same as every real driver run.
ROOT = host_root()

# A body the validator accepts: the dispatchable predicate is "this issue validates AND it is
# labeled ready", so every fixture below starts from a valid body and breaks exactly one thing.
VALID_BODY = "\n\n".join(f"{heading}\nsomething" for heading in REQUIRED_SECTIONS).replace(
    "## Stages\nsomething", "## Stages\n- [ ] Do the thing"
)


def _task_class(**overrides) -> TaskClass:
    fields = {
        "backend": "claude",
        "model": "claude-opus-5",
        "max_context": 150_000,
        "max_cost_usd": 20.0,
        "max_total_tokens": 20_000_000,
        "commit_warn_turns": 10,
        "commit_cut_turns": 18,
    }
    fields.update(overrides)
    return TaskClass(**fields)


def _naive(*args) -> datetime:
    # The guard's liveness/stall-window datetimes are naive on purpose (agent_guard.py's own
    # module docstring): progress.log's timestamp convention has no tz field, and the whole
    # worker fleet runs on one host. One noqa here instead of one per call site below.
    return datetime(*args)  # noqa: DTZ001


# ---- parse_duration / last_declared_cutoff / liveness_expired ----


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30m", timedelta(minutes=30)),
        ("4h", timedelta(hours=4)),
        ("1d", timedelta(days=1)),
        ("1.5h", timedelta(hours=1.5)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_rejects_an_unknown_unit():
    with pytest.raises(ValueError, match="not a duration"):
        parse_duration("30s")


def test_last_declared_cutoff_reads_the_last_expect_or_heartbeat_line():
    text = (
        "2026-09-14 10:00  EXPECT rebuild-wait normal=4h cutoff=5h\n"
        "2026-09-14 11:00  free text milestone, not parsed\n"
        "2026-09-14 12:00  HEARTBEAT rebuild-wait normal=4h cutoff=5h"
    )
    ts, cutoff = last_declared_cutoff(text)
    assert ts == _naive(2026, 9, 14, 12, 0)
    assert cutoff == timedelta(hours=5)


def test_last_declared_cutoff_none_when_never_posted():
    assert last_declared_cutoff("2026-09-14 10:00  just a milestone line") is None


def test_last_declared_cutoff_skips_a_malformed_line_and_keeps_reading():
    # #428: a cutoff the guard cannot parse ('1h30m' is not this grammar -- a single number and a
    # single unit) must not poison the scan. The malformed line sits in the MIDDLE, so this also
    # proves a later valid line is not hidden behind it.
    text = (
        "2026-09-14 10:00  EXPECT stage2-pricing-script normal=50m cutoff=1h30m\n"
        "2026-09-14 11:00  HEARTBEAT stage2-pricing-script normal=50m cutoff=45m"
    )
    ts, cutoff = last_declared_cutoff(text)
    assert ts == _naive(2026, 9, 14, 11, 0)
    assert cutoff == timedelta(minutes=45)


def test_last_declared_cutoff_none_when_the_only_line_is_malformed():
    assert last_declared_cutoff("2026-09-14 10:00  EXPECT thing normal=50m cutoff=1h30m") is None


def test_unparsable_cutoff_lines_names_the_line_number_and_text():
    text = (
        "2026-09-14 10:00  just a milestone line\n"
        "2026-09-14 10:05  EXPECT stage2-pricing-script normal=50m cutoff=1h30m\n"
        "2026-09-14 11:00  HEARTBEAT stage2-pricing-script normal=50m cutoff=45m"
    )
    (warning,) = unparsable_cutoff_lines(text)
    assert warning.startswith("line 2:")
    assert "1h30m" in warning


def test_unparsable_cutoff_lines_empty_when_everything_parses():
    text = "2026-09-14 10:00  EXPECT thing normal=30m cutoff=30m"
    assert unparsable_cutoff_lines(text) == []


def test_liveness_expired_true_past_the_declared_cutoff():
    text = "2026-09-14 10:00  EXPECT thing normal=30m cutoff=30m"
    observed_at = _naive(2026, 9, 14, 10, 0)
    now = _naive(2026, 9, 14, 10, 31)
    assert liveness_expired(text, _naive(2026, 9, 14, 9, 0), now, observed_at=observed_at) is True


def test_liveness_expired_false_inside_a_long_declared_window():
    # A worker watching a 4h rebuild is silent without being stuck.
    text = "2026-09-14 10:00  EXPECT rebuild-wait normal=4h cutoff=5h"
    observed_at = _naive(2026, 9, 14, 10, 0)
    now = _naive(2026, 9, 14, 13, 0)
    assert liveness_expired(text, _naive(2026, 9, 14, 9, 0), now, observed_at=observed_at) is False


def test_liveness_expired_uses_the_30_minute_default_before_the_first_expect():
    now_ok = _naive(2026, 9, 14, 10, 29)
    now_expired = _naive(2026, 9, 14, 10, 31)
    started = _naive(2026, 9, 14, 10, 0)
    assert liveness_expired("", started, now_ok, observed_at=started) is False
    assert liveness_expired("", started, now_expired, observed_at=started) is True
    assert DEFAULT_LIVENESS_CUTOFF == timedelta(minutes=30)


def test_liveness_expired_ignores_a_future_typed_timestamp():
    # #419: a worker that types a clock an hour ahead of when the line actually arrived must not
    # buy itself extra grace. The line reads '11:00' but the guard only ever observed it at 10:00
    # (what `observed_at` stands for in production: the file's own mtime) -- it is still declared
    # stalled 30 minutes after that arrival, per its own 30-minute cutoff, not 30 minutes after the
    # invented 11:00.
    text = "2026-09-14 11:00  EXPECT thing normal=15m cutoff=30m"
    observed_at = _naive(2026, 9, 14, 10, 0)
    started = _naive(2026, 9, 14, 9, 0)
    assert (
        liveness_expired(
            text, started, observed_at + timedelta(minutes=29), observed_at=observed_at
        )
        is False
    )
    assert (
        liveness_expired(
            text, started, observed_at + timedelta(minutes=31), observed_at=observed_at
        )
        is True
    )


def test_liveness_expired_ignores_a_past_typed_timestamp():
    # #419: the mirror case -- a clock behind the actual arrival must not shorten the grace either.
    # The line reads '08:00' but really arrived (observed_at) at 10:00; 5 minutes later is still
    # inside its own 30-minute cutoff, even though 08:00 + 30m is long gone.
    text = "2026-09-14 08:00  EXPECT thing normal=15m cutoff=30m"
    observed_at = _naive(2026, 9, 14, 10, 0)
    started = _naive(2026, 9, 14, 7, 0)
    assert (
        liveness_expired(text, started, observed_at + timedelta(minutes=5), observed_at=observed_at)
        is False
    )


# ---- budget_exceeded ----


def test_budget_exceeded_on_context_alone_with_no_result_yet():
    summary = UsageSummary(session_id="s1", turns=3, context=200_000, output_tokens=10, result=None)
    assert budget_exceeded(summary, _task_class(max_context=150_000)) is True


def test_budget_exceeded_on_cost_once_the_result_reports_it():
    summary = UsageSummary(
        session_id="s1", turns=3, context=1_000, output_tokens=10, result={"total_cost_usd": 25.0}
    )
    assert budget_exceeded(summary, _task_class(max_cost_usd=20.0)) is True


def test_budget_exceeded_false_within_both_ceilings():
    summary = UsageSummary(
        session_id="s1", turns=3, context=1_000, output_tokens=10, result={"total_cost_usd": 1.0}
    )
    assert budget_exceeded(summary, _task_class()) is False


def test_budget_exceeded_sums_every_stage_process_of_the_issue(tmp_path):
    """`max_cost_usd` is the ISSUE's ceiling, not one stage process's (#375): two stages already
    archived under `.cache/spend/<issue>/` plus the live one are each well inside it and together
    over it, and it is the sum that cuts the run."""
    spend = tmp_path / ".cache" / "spend" / "347"
    spend.mkdir(parents=True)
    for name, cost in (
        ("20260915T100000Z-claude-stage1.jsonl", 8.0),
        ("20260915T120000Z-claude-stage2.jsonl", 9.5),
    ):
        (spend / name).write_text(json.dumps({"type": "result", "total_cost_usd": cost}) + "\n")
    live = tmp_path / ".cache" / "worker_claude.jsonl"
    live.write_text(json.dumps({"type": "result", "total_cost_usd": 4.0}) + "\n")

    spent = agent_guard.cost_spent_on_issue("347", live, tmp_path)
    assert spent == pytest.approx(21.5)

    summary = UsageSummary(
        session_id="s1", turns=3, context=1_000, output_tokens=10, result={"total_cost_usd": 4.0}
    )
    assert budget_exceeded(summary, _task_class(max_cost_usd=20.0)) is False
    assert budget_exceeded(summary, _task_class(max_cost_usd=20.0), issue_cost_usd=spent) is True


def _qwen_result_event(total_tokens: int) -> dict:
    """Qwen's terminal `result` as `.cache/spend/363/*.jsonl` records it, which is the shape every
    worker class now runs on (agent_os/docs/adr/2026-09-16-workers-run-on-qwen-and-claude-only-reviews.md):
    `usage.total_tokens` present, `cache_read_input_tokens` a part of `input_tokens`, and no
    `total_cost_usd` anywhere in the event (#387)."""
    output_tokens = max(total_tokens // 100, 1)
    return {
        "type": "result",
        "subtype": "success",
        "usage": {
            "input_tokens": total_tokens - output_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": total_tokens // 2,
            "total_tokens": total_tokens,
        },
    }


def _qwen_task_class(**overrides) -> TaskClass:
    return _task_class(backend="qwen", model="qwen3.8-max", **overrides)


def test_tokens_spent_on_issue_reads_the_stages_the_dollar_sum_cannot(tmp_path):
    """Four logs of one issue, and not one event in any of them carries `total_cost_usd`: the
    dollar sum over exactly these files is 0.0, an inert ceiling, while the token sum is what cuts
    (#387). Both read the same listing -- archived stages plus the live log -- and both count a
    stage that was cut before its terminal `result` as 0."""
    spend = tmp_path / ".cache" / "spend" / "387"
    spend.mkdir(parents=True)
    for name, total_tokens in (
        ("20260916T100000Z-qwen-stage1.jsonl", 30_000_000),
        ("20260916T110000Z-qwen-stage2.jsonl", 19_000_000),
    ):
        (spend / name).write_text(json.dumps(_qwen_result_event(total_tokens)) + "\n")
    # A stage cut before it finished: turns spent, no terminal event to corroborate them.
    (spend / "20260916T120000Z-qwen-stage3.jsonl").write_text(json.dumps(_assistant_event()) + "\n")
    live = tmp_path / ".cache" / "worker_qwen.jsonl"
    live.write_text(json.dumps(_qwen_result_event(1_000_000)) + "\n")

    assert len(agent_guard.issue_stage_logs("387", live, tmp_path)) == 4
    assert agent_guard.tokens_spent_on_issue("387", live, tmp_path) == 50_000_000
    assert agent_guard.cost_spent_on_issue("387", live, tmp_path) == 0.0


def test_budget_exceeded_on_tokens_alone_with_the_dollar_ceiling_untouched():
    """A class whose dollars are nowhere near spent and whose tokens are over: on Qwen this is the
    only cut that can ever happen, because no event reports a cost to compare against (#387). The
    dollar ceiling stays reachable for the roles that do report one."""
    summary = UsageSummary(
        session_id="s1",
        turns=3,
        context=1_000,
        output_tokens=10,
        result=_qwen_result_event(1_000_000),
    )
    task_class = _qwen_task_class(max_cost_usd=20.0, max_total_tokens=20_000_000)

    assert budget_exceeded(summary, task_class) is False
    assert budget_exceeded(summary, task_class, issue_total_tokens=50_000_000) is True
    assert budget_exceeded(summary, task_class, issue_cost_usd=25.0) is True


def test_budget_exceeded_falls_back_to_the_live_results_own_tokens():
    """A caller that does not know which issue this is judges the live process alone, and a result
    whose `usage` carries no `total_tokens` still counts through the four-counter fallback
    `usage-report` prints -- so the ceiling does not go inert on the backend that reports cost."""
    qwen = UsageSummary(
        session_id="s1",
        turns=3,
        context=1_000,
        output_tokens=10,
        result=_qwen_result_event(25_000_000),
    )
    assert budget_exceeded(qwen, _qwen_task_class(max_total_tokens=20_000_000)) is True

    claude = UsageSummary(
        session_id="s1",
        turns=3,
        context=1_000,
        output_tokens=10,
        result={
            "type": "result",
            "total_cost_usd": 1.0,
            "usage": {
                "input_tokens": 30,
                "output_tokens": 2,
                "cache_read_input_tokens": 20_000_000,
                "cache_creation_input_tokens": 5_000_000,
            },
        },
    )
    assert budget_exceeded(claude, _task_class(max_total_tokens=20_000_000)) is True


def test_tick_backend_cuts_on_the_token_ceiling_when_no_event_reports_a_cost(monkeypatch, tmp_path):
    """The tick's own wiring, on the shape every worker class now runs on: events that carry tokens
    and no cost, an issue whose archived stages plus live log are over `max_total_tokens`, and the
    cut that lands is `reason=budget`. Before #387 nothing in the tick could see this run at all --
    its dollar sum was 0.0 and its context was one stage's."""
    cache = tmp_path / ".cache"
    worktree = tmp_path / "example-qwen"
    spend = cache / "spend" / "387"
    spend.mkdir(parents=True)
    worktree.mkdir()
    # Both are exported into a live worker's environment (WORKER_WORKTREE by the driver), so the
    # test names them rather than inherit whatever session it happens to run in.
    monkeypatch.setenv("WORKER_CACHE_DIR", str(cache))
    monkeypatch.setenv("WORKER_WORKTREE", str(worktree))
    (cache / "worker_qwen.state").write_text("STARTED\n")
    (cache / "worker_qwen.issue").write_text("387\n")
    (spend / "20260916T100000Z-qwen-stage1.jsonl").write_text(
        json.dumps(_qwen_result_event(45_000_000)) + "\n"
    )
    (cache / "worker_qwen.jsonl").write_text(
        json.dumps(_assistant_event()) + "\n" + json.dumps(_qwen_result_event(6_000_000)) + "\n"
    )

    body = f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->"
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""),
    )
    monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: True)
    monkeypatch.setattr(agent_guard, "_commit_timestamps", lambda tree, startref: [])
    monkeypatch.setattr(
        agent_guard,
        "load_task_classes",
        lambda: {"mechanical-qwen": _qwen_task_class(max_total_tokens=50_000_000)},
    )
    cuts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard, "cut_run", lambda backend, reason, **kwargs: cuts.append((backend, reason))
    )

    result = agent_guard._tick_backend("qwen", main=tmp_path)

    assert cuts == [("qwen", "budget")]
    assert result.cut_reason == "budget"
    assert result.message == "qwen: CUT_BY_GUARD reason=budget"


def _tick_fixture(tmp_path: Path, monkeypatch, *, progress_log_lines: str) -> tuple[Path, datetime]:
    """The wiring `test_tick_backend_cuts_on_the_token_ceiling_...` above sets up, minus the spend
    that would trip the budget ceiling -- shared by the malformed-cutoff tests below (#428).
    Returns the progress.log path (already written) and the run's start time."""
    cache = tmp_path / ".cache"
    worktree = tmp_path / "example-qwen"
    (worktree / "scratchpad").mkdir(parents=True)
    cache.mkdir()
    monkeypatch.setenv("WORKER_CACHE_DIR", str(cache))
    monkeypatch.setenv("WORKER_WORKTREE", str(worktree))
    statefile = cache / "worker_qwen.state"
    statefile.write_text("STARTED\n")
    run_started_at = _naive(2026, 9, 18, 12, 0)
    os.utime(statefile, (run_started_at.timestamp(), run_started_at.timestamp()))
    (cache / "worker_qwen.issue").write_text("416\n")
    (cache / "worker_qwen.jsonl").write_text(json.dumps(_assistant_event()) + "\n")

    body = f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->"
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""),
    )
    monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: True)
    monkeypatch.setattr(agent_guard, "_commit_timestamps", lambda tree, startref: [])
    monkeypatch.setattr(
        agent_guard, "load_task_classes", lambda: {"mechanical-qwen": _qwen_task_class()}
    )

    progress_log = worktree / "scratchpad" / "progress.log"
    progress_log.write_text(progress_log_lines)
    return progress_log, run_started_at


def test_tick_backend_survives_a_progress_log_it_cannot_parse(monkeypatch, tmp_path):
    """#428: the reported crash, reproduced through the tick's own entry point rather than the pure
    function alone. Before the fix, a `cutoff=1h30m` anywhere in progress.log raised out of
    `_tick_backend` -- while a worker was alive, the whole tick died, so nothing in the fleet got a
    budget check, a stall check or a written event. `cutoff=1h30m` is exactly the line #416's stage
    2 left behind (`scratchpad/progress.log:5` on the real qwen worktree, 2026-09-18)."""
    _progress_log, run_started_at = _tick_fixture(
        tmp_path,
        monkeypatch,
        progress_log_lines="2026-09-18 00:04  EXPECT stage2-pricing-script normal=50m cutoff=1h30m\n",
    )
    cuts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard, "cut_run", lambda backend, reason, **kwargs: cuts.append((backend, reason))
    )

    # 10 minutes into the run: no declaration this tick can read, so the 30-minute default grace
    # applies and the tick must complete rather than raise.
    result = agent_guard._tick_backend(
        "qwen", main=tmp_path, now=run_started_at + timedelta(minutes=10)
    )

    assert cuts == []
    assert result.cut_reason is None


def test_tick_backend_cuts_for_stall_on_the_line_after_a_malformed_one(monkeypatch, tmp_path):
    """The acceptance case #428 names explicitly: a malformed line does not hide a later, valid
    one, and liveness is still judged -- against the observed arrival of that good line (#419),
    not the text it types."""
    progress_log, run_started_at = _tick_fixture(
        tmp_path,
        monkeypatch,
        progress_log_lines=(
            "2026-09-18 12:01  EXPECT stage2-pricing-script normal=50m cutoff=1h30m\n"
            "2026-09-18 12:05  HEARTBEAT stage2-pricing-script normal=5m cutoff=10m\n"
        ),
    )
    arrival = run_started_at + timedelta(minutes=5)
    os.utime(progress_log, (arrival.timestamp(), arrival.timestamp()))
    cuts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard, "cut_run", lambda backend, reason, **kwargs: cuts.append((backend, reason))
    )

    # 16 minutes after the run started == 11 minutes after the HEARTBEAT actually arrived, past
    # its own 10-minute cutoff.
    result = agent_guard._tick_backend(
        "qwen", main=tmp_path, now=run_started_at + timedelta(minutes=16)
    )

    assert cuts == [("qwen", "stall")]
    assert result.cut_reason == "stall"


# ---- stall_detected: Claude path (event timestamps) vs Qwen path (bookkeeping) ----


def _assistant_event(
    timestamp: str | None = None, tool_call: tuple[str, dict] | None = None
) -> dict:
    content = []
    if tool_call:
        name, tool_input = tool_call
        content.append({"type": "tool_use", "name": name, "input": tool_input})
    event = {"type": "assistant", "message": {"usage": {"input_tokens": 10}, "content": content}}
    if timestamp:
        event["timestamp"] = timestamp
    return event


def test_stall_detected_claude_path_counts_turns_after_the_last_commit():
    events = [
        _assistant_event("2026-09-14T10:00:00Z"),
        _assistant_event("2026-09-14T10:05:00Z"),
        _assistant_event("2026-09-14T10:10:00Z"),
    ]
    commit_ts = [datetime.fromisoformat("2026-09-14T10:04:00+00:00")]
    task_class = _task_class(commit_warn_turns=2, commit_cut_turns=5)
    tier, since_commit, _ = stall_detected(
        events, commit_ts, task_class, turns=3, bookkeeping=StallBookkeeping()
    )
    assert since_commit == 2  # the 10:05 and 10:10 turns, after the 10:04 commit
    assert tier == "warn"


def test_stall_detected_claude_path_cuts_past_the_higher_threshold():
    events = [_assistant_event(f"2026-09-14T10:{i:02d}:00Z") for i in range(6)]
    task_class = _task_class(commit_warn_turns=2, commit_cut_turns=5)
    tier, since_commit, _ = stall_detected(
        events, [], task_class, turns=6, bookkeeping=StallBookkeeping()
    )
    assert since_commit == 6
    assert tier == "cut"


def test_stall_detected_qwen_path_uses_bookkeeping_when_events_carry_no_timestamp():
    # Qwen events: no `timestamp` field at all.
    events = [_assistant_event() for _ in range(4)]
    task_class = _task_class(commit_warn_turns=3, commit_cut_turns=6)
    # First tick: one commit already landed, at turn 2.
    bookkeeping = StallBookkeeping(commit_count=1, turn_count_at_commit=2)
    tier, since_commit, updated = stall_detected(
        events, [_naive(2026, 9, 14, 10, 0)], task_class, turns=4, bookkeeping=bookkeeping
    )
    assert since_commit == 2  # 4 - 2
    assert tier is None
    assert updated == bookkeeping  # no new commit this tick, bookkeeping unchanged


def test_stall_detected_qwen_path_resets_the_counter_on_a_new_commit():
    events = [_assistant_event() for _ in range(10)]
    task_class = _task_class(commit_warn_turns=3, commit_cut_turns=6)
    bookkeeping = StallBookkeeping(commit_count=1, turn_count_at_commit=2)
    # A second commit has landed by this tick -- the running counter should reset from turn 10.
    commit_ts = [_naive(2026, 9, 14, 10, 0), _naive(2026, 9, 14, 10, 5)]
    tier, since_commit, updated = stall_detected(
        events, commit_ts, task_class, turns=10, bookkeeping=bookkeeping
    )
    assert since_commit == 0
    assert tier is None
    assert updated.commit_count == 2
    assert updated.turn_count_at_commit == 10


def test_turns_since_commit_qwen_path_never_goes_negative_on_another_processs_anchor():
    """#52's unit-level repro: a previous run's anchor (turn 12, 5 commits) against a new
    process at turn 3 with no commit yet. It returned -9."""
    since, updated = turns_since_commit(
        [], [], 3, StallBookkeeping(commit_count=5, turn_count_at_commit=12)
    )
    assert since == 3
    assert (updated.commit_count, updated.turn_count_at_commit) == (0, 0)


def test_turns_since_commit_qwen_path_keeps_the_quota_memory_across_a_commit():
    """A commit re-anchors the counter and nothing else: the quota memory the same tick compares
    against must not be dropped by it, nor the warning marker."""
    bookkeeping = StallBookkeeping(
        commit_count=1, turn_count_at_commit=2, warned_at_turn_count=7, last_quota_status="allowed"
    )
    _, updated = turns_since_commit([], [_naive(2026, 9, 14), _naive(2026, 9, 15)], 10, bookkeeping)
    assert (updated.commit_count, updated.turn_count_at_commit) == (2, 10)
    assert updated.last_quota_status == "allowed"
    assert updated.warned_at_turn_count == 7


def test_scoped_to_run_resets_the_stall_fields_of_another_run_and_keeps_the_quota():
    previous = StallBookkeeping(
        commit_count=5,
        turn_count_at_commit=12,
        warned_at_turn_count=9,
        last_quota_status="exhausted",
        run_identity="issue=14 startref=aaa pid=100",
    )
    scoped = agent_guard.scoped_to_run(previous, "issue=19 startref=bbb pid=200")
    assert scoped == StallBookkeeping(
        last_quota_status="exhausted", run_identity="issue=19 startref=bbb pid=200"
    )
    assert agent_guard.scoped_to_run(previous, previous.run_identity) is previous


def _stall_tick_fixture(tmp_path: Path, monkeypatch, *, turns: int, commits: int) -> Path:
    """A live Qwen run of issue #19 at `turns` turns with `commits` commits since its start ref,
    no progress.log declaration past its grace, and a guard that records instead of cutting."""
    cache = tmp_path / ".cache"
    worktree = tmp_path / "example-qwen"
    worktree.mkdir()
    cache.mkdir()
    monkeypatch.setenv("WORKER_CACHE_DIR", str(cache))
    monkeypatch.setenv("WORKER_WORKTREE", str(worktree))
    (cache / "worker_qwen.state").write_text("STARTED\n")
    (cache / "worker_qwen.issue").write_text("19\n")
    (cache / "worker_qwen.startref").write_text("bbb\n")
    (cache / "worker_qwen.pid").write_text("200\n")
    (cache / "worker_qwen.jsonl").write_text(
        "".join(json.dumps(_assistant_event()) + "\n" for _ in range(turns))
    )
    body = f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->"
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""),
    )
    monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: True)
    monkeypatch.setattr(
        agent_guard,
        "_commit_timestamps",
        lambda tree, startref: [_naive(2026, 9, 24, 10, minute) for minute in range(commits)],
    )
    monkeypatch.setattr(
        agent_guard,
        "load_task_classes",
        lambda: {"mechanical-qwen": _qwen_task_class(commit_warn_turns=8, commit_cut_turns=15)},
    )
    monkeypatch.setattr(agent_guard, "post_stall_warning", lambda *args, **kwargs: None)
    return cache / "agent_guard_qwen.json"


def test_tick_backend_does_not_measure_a_new_run_from_the_previous_runs_anchor(
    monkeypatch, tmp_path
):
    """#52 as observed: #14's run left `commit_count=5, turn_count_at_commit=12` behind, #19 was
    dispatched with a new start ref, and at its turn 9 with no commit the tick said `-7 turns`."""
    bookkeeping_file = _stall_tick_fixture(tmp_path, monkeypatch, turns=9, commits=0)
    bookkeeping_file.write_text(
        json.dumps(
            {
                "commit_count": 5,
                "turn_count_at_commit": 12,
                "warned_at_turn_count": 9,
                "last_quota_status": "allowed",
            }
        )
    )

    result = agent_guard._tick_backend("qwen", main=tmp_path)

    assert result.message == "qwen: alive, 9 turns since last commit (class mechanical-qwen)"
    saved = json.loads(bookkeeping_file.read_text())
    assert saved["commit_count"] == 0
    assert saved["turn_count_at_commit"] == 0
    # 9 turns is past the warn threshold of 8: this run's warning is its own, not suppressed by
    # the previous run's marker at the same turn count.
    assert saved["warned_at_turn_count"] == 9
    assert saved["last_quota_status"] == "allowed"
    assert saved["run_identity"] == "issue=19 startref=bbb pid=200"


def test_tick_backend_counts_a_new_runs_first_commits_below_the_previous_runs_count(
    monkeypatch, tmp_path
):
    """#52 effect 2: the previous run's `commit_count=5` hid this run's commits 1..5, so a worker
    that had just committed was measured from turn 12 and cut once `turns - 12` crossed the cut."""
    bookkeeping_file = _stall_tick_fixture(tmp_path, monkeypatch, turns=30, commits=2)
    bookkeeping_file.write_text(
        json.dumps(
            {
                "commit_count": 5,
                "turn_count_at_commit": 12,
                "warned_at_turn_count": None,
                "last_quota_status": "allowed",
                "run_identity": "issue=14 startref=aaa pid=100",
            }
        )
    )
    cuts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard, "cut_run", lambda backend, reason, **kwargs: cuts.append((backend, reason))
    )

    result = agent_guard._tick_backend("qwen", main=tmp_path)

    assert cuts == []
    assert result.message == "qwen: alive, 0 turns since last commit (class mechanical-qwen)"
    saved = json.loads(bookkeeping_file.read_text())
    assert (saved["commit_count"], saved["turn_count_at_commit"]) == (2, 30)


def test_tick_backend_keeps_the_same_runs_anchor_between_ticks(monkeypatch, tmp_path):
    bookkeeping_file = _stall_tick_fixture(tmp_path, monkeypatch, turns=6, commits=1)
    bookkeeping_file.write_text(
        json.dumps(
            {
                "commit_count": 1,
                "turn_count_at_commit": 4,
                "warned_at_turn_count": None,
                "last_quota_status": "allowed",
                "run_identity": "issue=19 startref=bbb pid=200",
            }
        )
    )

    result = agent_guard._tick_backend("qwen", main=tmp_path)

    assert result.message == "qwen: alive, 2 turns since last commit (class mechanical-qwen)"


# ---- repeated_tool_calls ----


def test_repeated_tool_calls_true_on_three_identical_calls_in_a_row():
    events = [
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
    ]
    assert repeated_tool_calls(events) is True


def test_repeated_tool_calls_false_when_arguments_differ():
    events = [
        _assistant_event(tool_call=("Bash", {"command": "pytest -q tests/a.py"})),
        _assistant_event(tool_call=("Bash", {"command": "pytest -q tests/b.py"})),
        _assistant_event(tool_call=("Bash", {"command": "pytest -q tests/c.py"})),
    ]
    assert repeated_tool_calls(events) is False


def test_repeated_tool_calls_false_under_the_streak_length():
    events = [
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
    ]
    assert repeated_tool_calls(events) is False


def test_repeated_tool_calls_counts_the_tool_call_stream_not_the_turn_stream():
    # Regardless of turn count, per the ADR: a plain-text turn in between contributes nothing to
    # the tool_use stream, so it does not break a streak that is otherwise three in a row.
    events = [
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
        _assistant_event(),  # a plain text turn, no tool_use block at all
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
        _assistant_event(tool_call=("Bash", {"command": "pytest -q"})),
    ]
    assert repeated_tool_calls(events) is True


# ---- quota_status (thin wrapper around agent_lib.quota_exhausted, verified there already) ----


def _rate_limit_event(status: str) -> dict:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": status, "unifiedWindows": {"five_hour": {}}},
    }


def test_quota_status_exhausted_on_a_rejected_rate_limit_event():
    events = [_assistant_event("2026-09-14T10:00:00Z"), _rate_limit_event("rejected")]
    assert quota_status(events) == "exhausted"


def test_quota_status_allowed_on_a_clean_run():
    events = [_assistant_event("2026-09-14T10:00:00Z"), _rate_limit_event("allowed")]
    assert quota_status(events) == "allowed"


def test_quota_status_allowed_for_qwen_shaped_events_with_no_signal():
    events = [_assistant_event()]
    assert quota_status(events) == "allowed"


def test_read_events_helper_still_used_by_the_guard_module(tmp_path):
    # Smoke check that the guard reads jsonl through agent_lib, not its own parser.
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(_assistant_event("2026-09-14T10:00:00Z")))
    events = read_events(path)
    assert len(events) == 1


# ---- StallBookkeeping stays loadable across the checkpoint 4 schema change ----


def test_stall_bookkeeping_loads_a_file_written_before_last_quota_status_existed():
    # checkpoint 2's saved bookkeeping json never had this key; the field must default rather
    # than blow up `StallBookkeeping(**json.loads(...))` in `_load_bookkeeping`.
    old = json.loads('{"commit_count": 2, "turn_count_at_commit": 5, "warned_at_turn_count": null}')
    bookkeeping = StallBookkeeping(**old)
    assert bookkeeping.last_quota_status is None


# ---- cache_dir(): WORKER_CACHE_DIR moves every path the guard derives from it (#360) ----


_WORKER_FILE_FIELDS = (
    "pidfile",
    "events",
    "startref",
    "statefile",
    "issuefile",
    "bookkeeping",
)


def _worker_files_under(paths: agent_guard.WorkerPaths) -> dict[str, Path]:
    """The per-worker files `worker_paths` derives from the cache directory, by field name: the
    whole of what the override has to move, and nothing that comes from anywhere else."""
    return {name: getattr(paths, name) for name in _WORKER_FILE_FIELDS}


def test_without_the_override_every_guard_path_stays_under_the_checkout_cache(
    tmp_path, monkeypatch, invocations
):
    """The default half of the contract. The `delenv` is what makes it a test of the default: a
    live worker session carries `WORKER_CACHE_DIR` in its environment, and inheriting that value
    would leave this asserting the override while reading as if it asserted `main / ".cache"`."""
    monkeypatch.delenv("WORKER_CACHE_DIR", raising=False)
    cache = tmp_path / ".cache"

    assert agent_guard.cache_dir(tmp_path) == cache
    assert agent_guard.events_dir(tmp_path) == cache / "planner_events"
    assert agent_guard.consumed_dir(tmp_path) == cache / "planner_events" / "consumed"
    assert _worker_files_under(agent_guard.worker_paths("qwen", tmp_path)) == {
        "pidfile": cache / "worker_qwen.pid",
        "events": cache / "worker_qwen.jsonl",
        "startref": cache / "worker_qwen.startref",
        "statefile": cache / "worker_qwen.state",
        "issuefile": cache / "worker_qwen.issue",
        "bookkeeping": cache / "agent_guard_qwen.json",
    }

    assert "no unconsumed" in agent_guard.wake(main=tmp_path)
    assert (cache / "planner.lock").is_file()
    assert invocations == []


def test_the_override_moves_the_worker_files_the_events_and_wakes_lock_out_of_the_checkout(
    tmp_path, monkeypatch, invocations
):
    """The override half, and the reason it exists: this checkout's `.cache` is a symlink shared
    by every worktree of it, so a test that lets the real exit hook fire writes into the cache a
    live run is using. With the variable set, nothing the guard touches lands there -- including
    `wake`'s lock, which is the one path here that a caller cannot pass in."""
    main = tmp_path / "checkout"
    main.mkdir()
    override = tmp_path / "cache-elsewhere"
    monkeypatch.setenv("WORKER_CACHE_DIR", str(override))

    assert agent_guard.cache_dir(main) == override
    assert agent_guard.events_dir(main) == override / "planner_events"
    assert agent_guard.consumed_dir(main) == override / "planner_events" / "consumed"
    assert _worker_files_under(agent_guard.worker_paths("claude", main)) == {
        "pidfile": override / "worker_claude.pid",
        "events": override / "worker_claude.jsonl",
        "startref": override / "worker_claude.startref",
        "statefile": override / "worker_claude.state",
        "issuefile": override / "worker_claude.issue",
        "bookkeeping": override / "agent_guard_claude.json",
    }

    assert "no unconsumed" in agent_guard.wake(main=main)
    assert (override / "planner.lock").is_file()
    assert invocations == []
    assert not (main / ".cache").exists()


# ---- the same contract end to end: the exit hook as its own process, against this checkout ----


def _cache_planner_facts(cache: Path) -> dict[str, object]:
    """What a guard run leaves behind in a cache directory, as far as this test is concerned:
    every `worker_*.state` (content AND mtime, so rewriting the same text still reads as a
    change), the event filenames under `planner_events/`, and `planner/runs.tsv` -- the line
    `planner_task.sh` appends when a planner run actually starts.

    Event filenames are compared by base name, not by path: `consume_events` moves a file into
    `planner_events/consumed/`, so a planner run that happens to be finishing while this test
    measures changes where the files are without changing which events exist, and that is not
    this test's write to catch."""
    events = cache / "planner_events"
    runs = cache / "planner" / "runs.tsv"
    return {
        "states": {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(cache.glob("worker_*.state"))
        },
        "events": sorted(path.name for path in events.rglob("*") if path.is_file())
        if events.is_dir()
        else [],
        "runs": runs.read_bytes() if runs.is_file() else None,
    }


@contextlib.contextmanager
def _planner_locks_held(*caches: Path):
    """`wake`'s one door is a non-blocking flock on `<cache>/planner.lock`, so holding that lock in
    every cache this test can reach is what makes it unable to start a planner run: the override's,
    where the hook is supposed to look, and the repository's own, where it would look if it ever
    stopped honouring `WORKER_CACHE_DIR` -- a regression has to fail this test, not launch a run
    against the live tracker. A lock a real planner already holds protects exactly the same way, so
    finding it taken is not an error either."""
    with contextlib.ExitStack() as stack:
        for cache in caches:
            cache.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context((cache / "planner.lock").open("a"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass
        yield


def test_the_exit_hook_run_for_real_writes_only_under_the_override_and_starts_no_planner(
    tmp_path, monkeypatch
):
    """`agent_guard.py check <backend>` the way `worker_task.sh`'s subshell runs it: the real
    script, this checkout as its `main`, `WORKER_CACHE_DIR` at a throwaway directory. Three facts
    at once -- the hook did its whole job (state DONE and its own event, both under the override),
    it started no planner run, and the repository's `.cache` is the same afterwards down to the
    mtime. This is the assertion the unit tests above cannot make: they call `cache_dir`, while
    this one lets the process that ships resolve `main` from its own `__file__`, which is what
    puts the shared `.cache` in reach at all.

    There is no arm here with the variable unset, on purpose: running the real hook against the
    real cache is the write this test exists to prevent. The default arm is the unit test above.
    """
    # This process inherits the variable from its own driver, so it goes first: every path below
    # has to be measured against the cache the subprocess is being kept out of, not against the
    # override this session happens to be running under.
    monkeypatch.delenv("WORKER_CACHE_DIR", raising=False)
    real_cache = agent_guard.cache_dir(ROOT)
    # This checkout's `.cache` is a tracked symlink shared by every worktree on a machine that
    # mounts one for its data directories; a CI runner's checkout has no such mount, so the
    # symlink resolves to nothing and `real_cache` does not exist. Either way the guarantee under
    # test is the same -- the real exit hook must not create or write to it -- so both branches
    # below read this flag instead of assuming the directory is already there.
    real_cache_existed = real_cache.is_dir()

    # The backend nobody is running: a `check` for a live backend would race that backend's own
    # driver writing its state file into the same shared cache, and "what changed" is the claim.
    backend = next(
        (
            name
            for name in agent_guard.BACKENDS
            if not agent_guard._is_alive(agent_guard.worker_paths(name, ROOT).pidfile)
        ),
        agent_guard.BACKENDS[0],
    )

    override = tmp_path / "cache-elsewhere"
    # Where a planner run would be recorded if one started. Pointed away from the real cache too,
    # so that a test failure means "a run started", never "a run started and wrote there".
    planner_cache = tmp_path / "planner"
    environment = dict(os.environ)
    environment.update(
        # `scripts` is a namespace package resolved through this path, and the editable install in
        # the shared `.venv` points at the main checkout: without this the subprocess would import
        # another tree's guard and measure nothing about this one.
        PYTHONPATH=str(ROOT),
        WORKER_CACHE_DIR=str(override),
        PLANNER_CACHE_DIR=str(planner_cache),
        WORKER_WORKTREE=str(tmp_path / "worktree"),
    )

    before = _cache_planner_facts(real_cache)
    # Locked on both sides when the real cache already exists, so the event this hook writes
    # cannot start a planner run whatever directory the guard resolves, and stays on disk
    # unconsumed to be asserted on. When it does not exist (CI), there is no live planner or
    # tracker under it to protect, and `_planner_locks_held`'s `mkdir` would otherwise create it
    # wherever the symlink happens to point -- a write of the test's own making, outside
    # `tmp_path` and outside this checkout, which is exactly what real_cache_existed avoids.
    locked_caches = (override, real_cache) if real_cache_existed else (override,)
    with _planner_locks_held(*locked_caches):
        completed = subprocess.run(
            [sys.executable, "-m", "agent_os.guard", "check", backend],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    after = _cache_planner_facts(real_cache)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"{backend}: DONE" in completed.stdout
    assert "already holds .cache/planner.lock" in completed.stdout

    # The hook's own writes, all of them under the override and none of them consumed.
    statefile = override / f"worker_{backend}.state"
    assert statefile.read_text().splitlines()[0] == "DONE"
    events = sorted((override / "planner_events").glob(f"*-worker_finished-{backend}"))
    assert len(events) == 1, events
    assert "finished on its own" in events[0].read_text()
    assert not (override / "planner_events" / "consumed").exists()
    assert not planner_cache.exists()  # no planner run recorded

    # And the repository's own cache: no state rewritten, no event added, no run appended -- and,
    # if it did not exist before this run, still not created by it. (A blanket "never held a
    # worker_finished event for this backend" check used to sit here instead; on a cache that
    # already carries real history -- consumed events from real worker runs, in particular -- that
    # is not a "new file" claim, so it failed on runs this test had nothing to do with.)
    assert after["states"] == before["states"]
    assert after["events"] == before["events"]
    assert after["runs"] == before["runs"]
    assert real_cache.is_dir() == real_cache_existed


# ---- planner events, the lock, and wake: the only path to a planner run
# (agent_os/docs/adr/2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md).
# `_invoke_planner` is the one thing monkeypatched -- everything else (the lock, the event files,
# the consume, the cap) runs for real against tmp_path. ----


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# The one human, and one of the mechanism's own App identities -- read from config/agents.yaml
# rather than spelled here, so a project that renames either does not have to edit this file
# (the App comments as `<slug>[bot]`).
_HUMAN = agent_guard.PROJECT.human_login
_BOT = f"{agent_guard.PROJECT.planner_app}[bot]"


@pytest.fixture
def invocations(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        agent_guard, "_invoke_planner", lambda context, *, main: calls.append(context)
    )
    return calls


def test_write_event_names_the_file_by_timestamp_kind_and_subject(tmp_path):
    path = agent_guard.write_event(
        "worker_cut",
        "claude",
        detail="claude was cut (reason=stall)",
        main=tmp_path,
        now=_utc(2026, 9, 14, 12, 30, 5),
    )
    assert path.name == "20260914T123005Z-worker_cut-claude"
    assert path.read_text().strip() == "claude was cut (reason=stall)"
    assert agent_guard.pending_events(tmp_path) == [path]


def test_write_event_rejects_a_kind_nobody_declared(tmp_path):
    with pytest.raises(ValueError, match="not a planner event kind"):
        agent_guard.write_event("something_happened", "claude", main=tmp_path)


def test_write_event_does_not_overwrite_a_same_second_twin(tmp_path):
    now = _utc(2026, 9, 14, 12, 30, 5)
    first = agent_guard.write_event("worker_cut", "claude", detail="one", main=tmp_path, now=now)
    second = agent_guard.write_event("worker_cut", "claude", detail="two", main=tmp_path, now=now)
    assert first != second
    assert {path.read_text().strip() for path in agent_guard.pending_events(tmp_path)} == {
        "one",
        "two",
    }


def test_latest_event_at_also_looks_in_consumed(tmp_path):
    # The idle rate limit is about how often the planner is WOKEN, so consuming an event must not
    # reset its clock -- otherwise every wake would re-open the idle window immediately.
    path = agent_guard.write_event(
        "idle_dispatchable", "51", main=tmp_path, now=_utc(2026, 9, 14, 10, 0, 0)
    )
    agent_guard.consume_events([path], main=tmp_path)
    assert agent_guard.pending_events(tmp_path) == []
    assert agent_guard.latest_event_at("idle_dispatchable", main=tmp_path) == _utc(
        2026, 9, 14, 10, 0, 0
    )


def test_wake_does_nothing_without_events(tmp_path, invocations):
    assert "no unconsumed" in agent_guard.wake(main=tmp_path)
    assert invocations == []


def test_wake_runs_the_planner_with_every_event_as_context_then_consumes_them(
    tmp_path, invocations
):
    agent_guard.write_event(
        "worker_cut",
        "claude",
        detail="claude was cut (reason=stall)",
        main=tmp_path,
        now=_utc(2026, 9, 14, 12, 0, 0),
    )
    agent_guard.write_event(
        "human_replied",
        "77",
        detail="issue #77 got a reply",
        main=tmp_path,
        now=_utc(2026, 9, 14, 12, 1, 0),
    )
    agent_guard.wake(main=tmp_path, now=_utc(2026, 9, 14, 12, 2, 0))
    assert invocations == ["claude was cut (reason=stall); issue #77 got a reply"]
    assert agent_guard.pending_events(tmp_path) == []
    assert len(list(agent_guard.consumed_dir(tmp_path).iterdir())) == 2


def test_wake_exits_at_once_while_another_planner_holds_the_lock(tmp_path, invocations):
    agent_guard.write_event("worker_finished", "qwen", main=tmp_path)
    lock_path = tmp_path / ".cache" / "planner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        message = agent_guard.wake(main=tmp_path)
    assert "already holds" in message
    assert invocations == []
    # The events are still there, for the running planner's end or the next wake.
    assert len(agent_guard.pending_events(tmp_path)) == 1


def test_wake_pages_once_and_refuses_to_run_past_the_daily_cap(tmp_path, monkeypatch, invocations):
    cap = agent_guard.load_planner_config().max_runs_per_day
    runs = agent_guard.planner_dir(tmp_path)
    runs.mkdir(parents=True)
    (runs / "runs.tsv").write_text(
        RUNS_TSV_HEADER
        + "\n"
        + "\n".join(f"2026-09-14T0{i % 10}:00:00Z\tctx\tm\t3\t0.10" for i in range(cap))
        + "\n"
    )
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    agent_guard.write_event("worker_cut", "claude", main=tmp_path)

    first = agent_guard.wake(main=tmp_path, now=_utc(2026, 9, 14, 20, 0, 0))
    second = agent_guard.wake(main=tmp_path, now=_utc(2026, 9, 14, 21, 0, 0))

    assert "run cap reached" in first and "run cap reached" in second
    assert invocations == []
    assert len(paged) == 1  # once per day, not once per blocked wake
    assert len(agent_guard.pending_events(tmp_path)) == 1  # nothing consumed


def test_wake_runs_again_once_the_day_rolls_over(tmp_path, monkeypatch, invocations):
    cap = agent_guard.load_planner_config().max_runs_per_day
    runs = agent_guard.planner_dir(tmp_path)
    runs.mkdir(parents=True)
    (runs / "runs.tsv").write_text(
        "\n".join(f"2026-09-14T0{i % 10}:00:00Z\tctx\tm\t3\t0.10" for i in range(cap)) + "\n"
    )
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: None)
    agent_guard.write_event("worker_cut", "claude", main=tmp_path)
    agent_guard.wake(main=tmp_path, now=_utc(2026, 9, 15, 9, 0, 0))
    assert len(invocations) == 1


# ---- refine_pending: idle_dispatchable's sibling for the refiner, behind planner.refiner_
# unattended (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-
# dry-run.md) ------------------------------------------------------------------------------------


def _planner_config(**overrides) -> PlannerConfig:
    fields = {"idle_wake_minutes": 120, "max_runs_per_day": 12, "refiner_unattended": True}
    fields.update(overrides)
    return PlannerConfig(**fields)


def test_write_refine_pending_event_when_due(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_guard, "load_planner_config", lambda: _planner_config())
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: [61, 62])
    outcome = agent_guard._write_refine_pending_event_if_due(
        main=tmp_path, now=_utc(2026, 9, 14, 10, 0, 0)
    )
    assert outcome.written == 1 and "refine_pending written" in outcome.line
    assert len(agent_guard.pending_events(tmp_path)) == 1


def test_write_refine_pending_event_nothing_while_the_flag_is_false(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard, "load_planner_config", lambda: _planner_config(refiner_unattended=False)
    )
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: [61])
    outcome = agent_guard._write_refine_pending_event_if_due(
        main=tmp_path, now=_utc(2026, 9, 14, 10, 0, 0)
    )
    assert outcome == agent_guard.EventOutcome()
    assert agent_guard.pending_events(tmp_path) == []


def test_write_refine_pending_event_nothing_when_nothing_needs_refining(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_guard, "load_planner_config", lambda: _planner_config())
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: [])
    assert (
        agent_guard._write_refine_pending_event_if_due(
            main=tmp_path, now=_utc(2026, 9, 14, 10, 0, 0)
        )
        == agent_guard.EventOutcome()
    )


def test_write_refine_pending_event_respects_the_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard, "load_planner_config", lambda: _planner_config(idle_wake_minutes=120)
    )
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: [61])
    first = _utc(2026, 9, 14, 10, 0, 0)
    assert agent_guard._write_refine_pending_event_if_due(main=tmp_path, now=first).written == 1
    too_soon = agent_guard._write_refine_pending_event_if_due(
        main=tmp_path, now=first + timedelta(minutes=119)
    )
    assert too_soon.written == 0 and "no event written" in too_soon.line
    later = agent_guard._write_refine_pending_event_if_due(
        main=tmp_path, now=first + timedelta(minutes=121)
    )
    assert later.written == 1 and "written" in later.line


def test_tick_writes_refine_pending_when_the_flag_is_true_and_something_needs_refining(
    monkeypatch, tmp_path, invocations
):
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[])
    monkeypatch.setattr(agent_guard, "load_planner_config", lambda: _planner_config())
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: [61])
    agent_guard.tick(main=tmp_path)
    assert len(invocations) == 1
    assert "#61" in invocations[0]


def test_tick_writes_no_refine_pending_when_the_flag_is_false(monkeypatch, tmp_path, invocations):
    # Used to rest on config/agents.yaml's own value while the real flag was still false (#349
    # criterion 3). The human reviewed the refiner's dry run on #331, #338 and #104/#356 and
    # approved flipping the real file to true on 2026-09-15 (#349), so this now stubs the flag
    # explicitly -- the behaviour under `false` still has to cost nothing, not even a `gh` call,
    # whatever refinable_issues would have said, but that is no longer something the real config's
    # current value can be trusted to exercise.
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[])
    monkeypatch.setattr(
        agent_guard, "load_planner_config", lambda: _planner_config(refiner_unattended=False)
    )
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: [61])
    agent_guard.tick(main=tmp_path)
    assert invocations == []


# ---- tick(): writes the events, then wakes. Every helper below (_tick_backend, _agents_paused,
# _check_human_replies, dispatchable_issues, _is_alive) is monkeypatched, so this exercises only
# what `tick` decides to write, never a real `gh` or subprocess call. ----


def _stub_tick(
    monkeypatch,
    *,
    results,
    paused=False,
    woken=(),
    alive=False,
    dispatchable=(),
    refinable=(),
    without_worktree=None,
    orphans=(),
    dead_runs=(),
    unreviewed=(),
    merged=(),
    wake_labeled=(),
):
    monkeypatch.setattr(
        agent_guard, "_tick_backend", lambda backend, *, main, now=None: results[backend]
    )
    monkeypatch.setattr(agent_guard, "_agents_paused", lambda *, main: paused)
    monkeypatch.setattr(agent_guard, "_check_human_replies", lambda *, main: list(woken))
    monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: alive)
    scan = agent_guard.DispatchableScan(list(dispatchable), dict(without_worktree or {}))
    monkeypatch.setattr(agent_guard, "dispatchable_scan", lambda *, main: scan)
    monkeypatch.setattr(agent_guard, "dispatchable_issues", lambda *, main: list(scan.issues))
    # `tick` always calls `_write_refine_pending_event_if_due`, which reads the REAL
    # `planner.refiner_unattended` unless a test overrides `load_planner_config` itself -- and the
    # real config now ships `true` (2026-09-15, #349). Stubbing `refinable_issues` here (default:
    # nothing to refine) is what keeps every `tick` test that does not care about refine_pending
    # from making a real `gh` call regardless of that flag's current value; a test that does care
    # overrides this (and usually `load_planner_config` too) after calling `_stub_tick`.
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: list(refinable))
    # The three things `tick` reconciles on every call (#365) each make `gh` calls of their own,
    # so every tick test stubs them to "nothing to do" for the same reason `refinable_issues` is
    # stubbed above; the tests that exercise them override these afterwards.
    monkeypatch.setattr(agent_guard, "reconcile_closed_issues", lambda *, main, now: [])
    monkeypatch.setattr(agent_guard, "promote_refined", lambda *, main: [])
    monkeypatch.setattr(agent_guard, "orphan_doing_issues", lambda *, main: list(orphans))
    monkeypatch.setattr(agent_guard, "restore_doing_label_on_live_runs", lambda *, main: [])
    # `dead_role_runs` reads only the filesystem, so it needs no stub for `gh`'s sake -- it needs
    # one for the cache's: `WORKER_CACHE_DIR` in the environment of whoever runs the suite (a live
    # worker session exports it) moves `cache_dir(main)` off `tmp_path` and onto the REAL `.cache`
    # this checkout shares with every worktree of it, and a dead one-shot run found there becomes a
    # real `role_died` event in the real `planner_events/` -- one paid planner run, woken by a test.
    monkeypatch.setattr(agent_guard, "dead_role_runs", lambda *, main: list(dead_runs))
    # `unreviewed_completions` (#394) makes the same two real `gh` calls `dispatchable_scan` and
    # `orphan_doing_issues` do -- stubbed here for the same reason those already are, and for the
    # same real-`.cache` risk `dead_role_runs` is stubbed against above (this one pages, not just
    # writes a file, and a stray real page is worse than a stray real event).
    monkeypatch.setattr(agent_guard, "unreviewed_completions", lambda *, main: list(unreviewed))
    # The two #413 reads -- recently merged pull requests and issues carrying wake:planner -- are
    # real `gh` calls too, stubbed to "nothing" for the same reason as everything above.
    monkeypatch.setattr(agent_guard, "recently_merged_pull_requests", lambda *, main: list(merged))
    monkeypatch.setattr(agent_guard, "wake_labeled_issues", lambda *, main: list(wake_labeled))


def _idle_results() -> dict:
    return {
        "qwen": TickResult("qwen", "qwen: never started"),
        "claude": TickResult("claude", "claude: never started"),
    }


def test_tick_writes_worker_cut_and_wakes(monkeypatch, tmp_path, invocations):
    results = _idle_results()
    results["claude"] = TickResult(
        "claude", "claude: CUT_BY_GUARD reason=stall", cut_reason="stall"
    )
    _stub_tick(monkeypatch, results=results, alive=True)
    agent_guard.tick(main=tmp_path)
    assert len(invocations) == 1
    assert "claude was cut (reason=stall)" in invocations[0]


def test_tick_writes_quota_changed(monkeypatch, tmp_path, invocations):
    results = _idle_results()
    results["claude"] = TickResult("claude", "claude: alive", quota_changed=True)
    _stub_tick(monkeypatch, results=results, alive=True)
    agent_guard.tick(main=tmp_path)
    assert "claude's quota status changed" in invocations[0]


def test_tick_writes_human_replied(monkeypatch, tmp_path, invocations):
    _stub_tick(monkeypatch, results=_idle_results(), woken=["77"], alive=True)
    agent_guard.tick(main=tmp_path)
    assert "issue #77" in invocations[0]


def test_tick_writes_idle_dispatchable_when_nothing_runs_and_something_is_dispatchable(
    monkeypatch, tmp_path, invocations
):
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[51, 52])
    agent_guard.tick(main=tmp_path)
    assert len(invocations) == 1
    assert "#51" in invocations[0] and "dispatchable" in invocations[0]


def test_tick_writes_nothing_when_nothing_is_dispatchable(monkeypatch, tmp_path, invocations):
    # #345 in one test: an idle tick over a backlog where no issue is dispatchable must cost
    # nothing at all -- no event, no planner run.
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[])
    agent_guard.tick(main=tmp_path)
    assert invocations == []
    assert agent_guard.pending_events(tmp_path) == []


def test_tick_writes_no_idle_event_while_a_worker_is_alive(monkeypatch, tmp_path, invocations):
    _stub_tick(monkeypatch, results=_idle_results(), alive=True, dispatchable=[51])
    agent_guard.tick(main=tmp_path)
    assert invocations == []


def test_tick_respects_the_idle_rate_limit(monkeypatch, tmp_path, invocations):
    minutes = agent_guard.load_planner_config().idle_wake_minutes
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[51])

    first = _utc(2026, 9, 14, 10, 0, 0)
    agent_guard.tick(main=tmp_path, now=first)
    assert len(invocations) == 1

    # Too soon: the condition is still true, and that is exactly why it must not wake again.
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=minutes - 1))
    assert len(invocations) == 1

    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=minutes + 1))
    assert len(invocations) == 2


# ---- #426: the idle rate limit is about how often the planner is WOKEN *for it*, not how often
# the event was merely written -- a run the backend rejected before its first turn must not go on
# blocking a fresh one for the rest of the window. ----


def _planner_log(tmp_path, name: str, lines: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def test_idle_wake_rejected_by_the_backend_does_not_hold_the_rate_limit(monkeypatch, tmp_path):
    # Shaped like 2026-09-18: the tick wrote idle_dispatchable naming seven issues, the run it woke
    # was rejected by Claude's five-hour rate limit in 497ms with no turn ever executed, and the
    # next real idle_dispatchable could not be written for two hours (#426).
    rejected_log = _planner_log(
        tmp_path,
        "rejected.log",
        [
            _rate_limit_event("rejected"),
            {
                "type": "result",
                "is_error": True,
                "terminal_reason": "api_error",
                "num_turns": 1,
                "total_cost_usd": 0,
                "duration_ms": 497,
            },
        ],
    )
    monkeypatch.setattr(agent_guard, "_invoke_planner", lambda context, *, main: rejected_log)

    first = _utc(2026, 9, 18, 8, 48, 45)
    written = agent_guard._write_idle_event_if_due([424, 423, 422], main=tmp_path, now=first)
    assert written.written == 1
    # `tick` always ends by calling `wake` in the same tick that wrote the event -- this is what
    # invokes the (stubbed) planner and lets the guard learn how the run it woke ended.
    agent_guard.wake(main=tmp_path, now=first)

    too_soon = first + timedelta(minutes=5)  # well inside idle_wake_minutes (120)
    outcome = agent_guard._write_idle_event_if_due([424], main=tmp_path, now=too_soon)
    assert outcome.written == 1
    assert "rejected" in outcome.line
    # The stale, rejected event was consumed by the wake above; the fresh one is the only thing
    # left pending, for the planner's next real run.
    assert len(agent_guard.pending_events(tmp_path)) == 1


def test_idle_wake_that_executed_keeps_the_rate_limit_exactly_as_today(monkeypatch, tmp_path):
    executed_log = _planner_log(
        tmp_path,
        "executed.log",
        [
            _assistant_event("2026-09-18T08:48:45Z"),
            {"type": "result", "is_error": False, "num_turns": 3, "total_cost_usd": 0.42},
        ],
    )
    monkeypatch.setattr(agent_guard, "_invoke_planner", lambda context, *, main: executed_log)

    first = _utc(2026, 9, 18, 8, 48, 45)
    agent_guard._write_idle_event_if_due([424], main=tmp_path, now=first)
    agent_guard.wake(main=tmp_path, now=first)

    too_soon = first + timedelta(minutes=5)
    outcome = agent_guard._write_idle_event_if_due([424], main=tmp_path, now=too_soon)
    assert outcome.written == 0 and "no event written" in outcome.line
    assert "rate-limited by a run that executed" in outcome.line


def test_tick_skips_everything_when_the_epic_carries_agents_paused(
    monkeypatch, tmp_path, invocations
):
    results = _idle_results()
    results["claude"] = TickResult(
        "claude", "claude: CUT_BY_GUARD reason=budget", cut_reason="budget"
    )
    _stub_tick(monkeypatch, results=results, paused=True, alive=False, dispatchable=[51])
    agent_guard.tick(main=tmp_path)
    assert invocations == []
    assert agent_guard.pending_events(tmp_path) == []


# ---- check(): the exit hook writes the terminal event and wakes within seconds of the exit ----


def _worker_files(tmp_path, backend: str, state: str, issue: str = "341") -> None:
    cache = tmp_path / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"worker_{backend}.state").write_text(state)
    (cache / f"worker_{backend}.issue").write_text(issue)


def test_check_writes_worker_finished_and_wakes(tmp_path, invocations):
    _worker_files(tmp_path, "claude", "STARTED\n")
    assert agent_guard.check("claude", main=tmp_path) == "claude: DONE"
    assert (tmp_path / ".cache" / "worker_claude.state").read_text().strip() == "DONE"
    assert len(invocations) == 1
    assert "claude finished on its own on issue #341" in invocations[0]


def test_a_slot_freeing_wakes_the_planner_with_nothing_new_in_ready(tmp_path, invocations):
    # #436's third case: an issue `new_dispatchable` withheld for lack of a slot is not lost -- the
    # worker's own exit hook wakes the planner regardless of whether anything NEW just became
    # ready, and its own RULES are what pick the suppressed issue back up.
    _worker_files(tmp_path, "qwen", "STARTED\n")
    assert agent_guard.check("qwen", main=tmp_path) == "qwen: DONE"
    assert len(invocations) == 1
    assert "qwen finished on its own on issue #341" in invocations[0]


def test_check_writes_worker_cut_without_overwriting_the_guards_own_state(tmp_path, invocations):
    _worker_files(tmp_path, "claude", "CUT_BY_GUARD reason=stall\n")
    message = agent_guard.check("claude", main=tmp_path)
    assert message.startswith("claude: already CUT_BY_GUARD")
    assert (tmp_path / ".cache" / "worker_claude.state").read_text().strip() == (
        "CUT_BY_GUARD reason=stall"
    )
    assert "CUT_BY_GUARD reason=stall" in invocations[0]


def test_check_reports_a_failed_launch_as_the_end_it_is_and_names_the_missing_command(
    tmp_path, invocations
):
    """A stage process that never started is an ending, so the exit hook must record it rather
    than write DONE over it -- and the event detail carries the state verbatim, which is what lets
    the planner escalate the real cause (the executable) instead of the symptom (#381)."""
    _worker_files(tmp_path, "qwen", "FAILED_LAUNCH command=/opt/nvm/bin/qwen\n")
    message = agent_guard.check("qwen", main=tmp_path)
    assert message.startswith("qwen: already FAILED_LAUNCH")
    assert (tmp_path / ".cache" / "worker_qwen.state").read_text().strip() == (
        "FAILED_LAUNCH command=/opt/nvm/bin/qwen"
    )
    assert "FAILED_LAUNCH command=/opt/nvm/bin/qwen" in invocations[0]
    assert "on issue #341" in invocations[0]


# ---- .state's line 2 marker (#350 Part 4): read_state_marker, write_state_line1, the pure
# detect_state_drift, and _log_state_drift's gh-backed wiring. ---------------------------------


def test_read_state_marker_on_an_old_one_line_file_has_no_marker(tmp_path):
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text("STARTED\n")
    assert read_state_marker(statefile) == ("STARTED", None, None)


def test_read_state_marker_on_a_missing_file_has_no_marker(tmp_path):
    assert read_state_marker(tmp_path / "does-not-exist.state") == ("", None, None)


def test_read_state_marker_parses_the_issue_and_label(tmp_path):
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text("STARTED\nissue=347 label=status:doing\n")
    assert read_state_marker(statefile) == ("STARTED", 347, "status:doing")


def test_write_state_line1_preserves_an_existing_marker(tmp_path):
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text("STARTED\nissue=347 label=status:doing\n")
    write_state_line1(statefile, "DONE")
    assert read_state_marker(statefile) == ("DONE", 347, "status:doing")


def test_write_state_line1_on_a_file_with_no_marker_writes_one_line(tmp_path):
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text("STARTED\n")
    write_state_line1(statefile, "DONE")
    assert statefile.read_text() == "DONE\n"


def test_check_preserves_the_marker_when_it_writes_done(tmp_path, invocations):
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "worker_claude.state").write_text("STARTED\nissue=341 label=status:doing\n")
    (cache / "worker_claude.issue").write_text("341")
    agent_guard.check("claude", main=tmp_path)
    assert read_state_marker(cache / "worker_claude.state") == ("DONE", 341, "status:doing")


def test_cut_run_preserves_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_guard.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0)
    )
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text("STARTED\nissue=341 label=status:doing\n")
    agent_guard.cut_run("claude", "stall", worktree=tmp_path, statefile=statefile, main=tmp_path)
    assert read_state_marker(statefile) == ("CUT_BY_GUARD reason=stall", 341, "status:doing")


def test_detect_state_drift_a_running_and_github_disagrees():
    labels = LabelVocabulary()
    drift = detect_state_drift("STARTED", labels.doing, labels.review, labels=labels)
    assert drift == f"local {labels.doing}, GitHub {labels.review}"


def test_detect_state_drift_b_finished_ai_completed_marker_but_github_still_doing():
    labels = LabelVocabulary()
    drift = detect_state_drift("DONE", labels.ai_completed, labels.doing, labels=labels)
    assert drift == f"local {labels.ai_completed}, GitHub {labels.doing}"


def test_detect_state_drift_none_when_finished_run_moved_on_downstream():
    # review/done/blocked-on-human after a finished run is normal downstream movement, not drift.
    labels = LabelVocabulary()
    for github_label in (labels.review, labels.blocked_on_human):
        assert detect_state_drift("DONE", labels.ai_completed, github_label, labels=labels) is None


def test_detect_state_drift_none_when_labels_agree():
    labels = LabelVocabulary()
    assert detect_state_drift("STARTED", labels.doing, labels.doing, labels=labels) is None


def test_detect_state_drift_none_when_the_gh_lookup_failed():
    labels = LabelVocabulary()
    assert detect_state_drift("STARTED", labels.doing, None, labels=labels) is None


def test_log_state_drift_returns_none_with_no_marker(tmp_path):
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text("STARTED\n")
    assert agent_guard._log_state_drift("claude", statefile, main=tmp_path) is None


def test_log_state_drift_reports_a_running_mismatch(tmp_path, monkeypatch):
    labels = agent_guard.PROJECT.labels
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text(f"STARTED\nissue=347 label={labels.doing}\n")
    monkeypatch.setattr(agent_guard, "_current_status_label", lambda n, *, main: labels.review)
    line = agent_guard._log_state_drift("claude", statefile, main=tmp_path)
    assert line == f"claude: state drift on #347 -- local {labels.doing}, GitHub {labels.review}"


def test_log_state_drift_none_when_gh_lookup_fails(tmp_path, monkeypatch):
    labels = agent_guard.PROJECT.labels
    statefile = tmp_path / "worker_claude.state"
    statefile.write_text(f"STARTED\nissue=347 label={labels.doing}\n")
    monkeypatch.setattr(agent_guard, "_current_status_label", lambda n, *, main: None)
    assert agent_guard._log_state_drift("claude", statefile, main=tmp_path) is None


# ---- _check_human_replies / dispatchable_issues / _agents_paused: the mechanical gh-backed
# checks, with subprocess.run and _gh_api_json monkeypatched so nothing touches the network. ----


def _fake_completed(cmd: list[str], stdout) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(stdout), stderr="")


def test_guard_writes_to_the_tracker_sign_as_the_mechanism_app(monkeypatch, tmp_path):
    """The guard's stall warning used to be published under whatever `gh` auth the invoking shell
    carried, which on this machine is the human's own account -- so an issue collected comments
    attributed to a person who never wrote them. Every tracker write now carries a minted App
    token."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd and "agent_os.gh_app_token" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="ghs_minted\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(agent_guard.subprocess, "run", fake_run)
    agent_guard.post_stall_warning("77", 25, main=tmp_path)

    issues_calls = [c for c in calls if "agent_os.issues" in c[0]]
    assert issues_calls, "the warning never reached the tracker CLI"
    env = issues_calls[0][1].get("env")
    assert env is not None, "the tracker write inherited the ambient identity"
    assert env.get("GH_TOKEN") == "ghs_minted"


def test_guard_tracker_write_degrades_to_ambient_identity_when_minting_fails(monkeypatch, tmp_path):
    """A missing secret must not stop the guard from warning -- it degrades, like the bash drivers."""

    def fake_run(cmd, **kwargs):
        if cmd and "agent_os.gh_app_token" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such app")
        fake_run.seen = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    fake_run.seen = None
    monkeypatch.setattr(agent_guard.subprocess, "run", fake_run)
    agent_guard.post_stall_warning("77", 25, main=tmp_path)

    assert fake_run.seen is not None
    assert "GH_TOKEN" not in fake_run.seen["env"]


def test_the_stall_warning_carries_the_workers_own_last_progress_lines(tmp_path):
    """A warning that only says "39 turns without a commit" cannot be told apart from a worker that
    has died: the human had to open a shell on the host to see which stage it was on. The guard
    reads progress.log itself, so this costs no model turn."""
    worktree = tmp_path / "example-qwen"
    (worktree / "scratchpad").mkdir(parents=True)
    (worktree / "scratchpad" / "progress.log").write_text(
        "2026-09-16 19:10  stage1-390 VERDE: 171 tests\n"
        "2026-09-16 19:11  EXPECT read-brief normal=15m cutoff=25m\n"
        "2026-09-16 19:14  HEARTBEAT stage2-390-audit normal=35m cutoff=70m\n"
        "2026-09-16 19:31  EXPECT stage3-worker-rules normal=35m cutoff=45m\n"
    )

    body = agent_guard.stall_warning_body(39, agent_guard.recent_progress_lines(worktree))

    assert body.startswith("39 turns without a commit, still watching")
    assert "EXPECT stage3-worker-rules" in body, "the line that says what it is doing now"
    assert "stage1-390 VERDE" not in body, "only the last three lines, not the whole log"
    assert body.count("```") == 2, "the log goes in a fenced block"


def test_the_stall_warning_is_unchanged_when_there_is_no_progress_log(tmp_path):
    """A worker that never wrote one, or a worktree that is gone, must not break the warning."""
    assert agent_guard.recent_progress_lines(tmp_path / "nowhere") == []
    assert agent_guard.stall_warning_body(39, []) == ("39 turns without a commit, still watching")


def test_the_stall_warning_truncates_a_long_line(tmp_path):
    """A VERDE line is a paragraph and the warning repeats every tick."""
    worktree = tmp_path / "example-qwen"
    (worktree / "scratchpad").mkdir(parents=True)
    (worktree / "scratchpad" / "progress.log").write_text("x" * 900 + "\n")

    (line,) = agent_guard.recent_progress_lines(worktree)

    assert line.endswith(" [...]")
    assert len(line) == agent_guard.PROGRESS_LINE_CHARS + len(" [...]")


def test_a_previous_stages_declaration_does_not_anchor_this_stages_liveness():
    """progress.log outlives the process that wrote it: each stage appends to the file the last one
    left. Anchoring on a line written before this run started cut stage 4 of #363 five minutes in,
    for silence that happened while it did not yet exist. A stage that has not declared anything
    gets the default grace from its own start. Expressed against the OBSERVED anchor (#419):
    `observed_at` stands in for the file's mtime, which a stage that appended nothing of its own
    leaves exactly where the previous stage left it -- before `started`."""
    started = _naive(2026, 9, 16, 13, 23)
    stale = "2026-09-16 13:03  EXPECT lint-and-tests normal=10m cutoff=15m\n"
    stale_mtime = _naive(2026, 9, 16, 13, 3)

    # Five minutes into a run that has declared nothing of its own: alive, despite the stale line
    # having blown its own 15-minute cutoff.
    assert not liveness_expired(
        stale, started, _naive(2026, 9, 16, 13, 28), observed_at=stale_mtime
    )

    # The default grace still applies, measured from this run's start and not from the old line.
    assert liveness_expired(stale, started, _naive(2026, 9, 16, 14, 0), observed_at=stale_mtime)

    # A declaration this run made itself is honoured as before -- its own append moves the
    # observed mtime to when THIS line arrived.
    own = stale + "2026-09-16 13:24  EXPECT reading normal=1h cutoff=2h\n"
    own_mtime = _naive(2026, 9, 16, 13, 24)
    assert not liveness_expired(own, started, _naive(2026, 9, 16, 15, 0), observed_at=own_mtime)
    assert liveness_expired(own, started, _naive(2026, 9, 16, 15, 30), observed_at=own_mtime)


def test_gh_api_json_reads_with_an_explicit_get(monkeypatch, tmp_path):
    """`gh api` promotes itself to POST as soon as a `-f` field is present. Without an explicit
    method this read POSTed to the comments endpoint, which is *create a comment*, so it 422'd and
    every human reply stayed invisible. The other tests in this file monkeypatch `_gh_api_json`
    itself, so only this one sees the argv."""
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(agent_guard.subprocess, "run", fake_run)
    agent_guard._gh_api_json("repos/{owner}/{repo}/issues/77/comments", main=tmp_path)

    assert seen, "the helper never shelled out"
    argv = seen[0]
    assert argv[:2] == ["gh", "api"]
    assert "--method" in argv and argv[argv.index("--method") + 1] == "GET"
    assert argv.index("--method") < argv.index("-f"), (
        "the method must be set before the field that would otherwise imply POST"
    )


def test_check_human_replies_clears_the_label_on_a_newer_comment(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: _fake_completed(cmd, [{"number": 77}]),
    )

    def fake_gh_api_json(path, *, main):
        if path.endswith("/timeline"):
            return [
                {
                    "event": "labeled",
                    "label": {"name": agent_guard.BLOCKED_ON_HUMAN_LABEL},
                    "created_at": "2026-09-14T10:00:00Z",
                }
            ]
        if path.endswith("/comments"):
            return [
                {"created_at": "2026-09-14T09:00:00Z", "user": {"login": _HUMAN}},
                # after the label -- a reply, and the human's own
                {"created_at": "2026-09-14T11:00:00Z", "user": {"login": _HUMAN}},
            ]
        raise AssertionError(path)

    monkeypatch.setattr(agent_guard, "_gh_api_json", fake_gh_api_json)
    cleared = []
    monkeypatch.setattr(
        agent_guard, "_clear_blocked_label", lambda issue, *, main: cleared.append(issue)
    )

    woken = agent_guard._check_human_replies(main=tmp_path)
    assert woken == ["77"]
    assert cleared == ["77"]


def test_check_human_replies_leaves_the_label_when_no_comment_is_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: _fake_completed(cmd, [{"number": 77}]),
    )

    def fake_gh_api_json(path, *, main):
        if path.endswith("/timeline"):
            return [
                {
                    "event": "labeled",
                    "label": {"name": agent_guard.BLOCKED_ON_HUMAN_LABEL},
                    "created_at": "2026-09-14T10:00:00Z",
                }
            ]
        if path.endswith("/comments"):
            # Only the worker's own comment, posted before the label -- no human reply yet.
            return [{"created_at": "2026-09-14T09:59:00Z", "user": {"login": _HUMAN}}]
        raise AssertionError(path)

    monkeypatch.setattr(agent_guard, "_gh_api_json", fake_gh_api_json)
    cleared = []
    monkeypatch.setattr(
        agent_guard, "_clear_blocked_label", lambda issue, *, main: cleared.append(issue)
    )

    woken = agent_guard._check_human_replies(main=tmp_path)
    assert woken == []
    assert cleared == []


def _gh_issue_list_stub(ready_rows, open_rows):
    def run(cmd, **kwargs):
        if "--label" in cmd:
            return _fake_completed(cmd, ready_rows)
        return _fake_completed(cmd, open_rows)

    return run


def _worktrees_present(monkeypatch, present=True):
    """Whether a backend has a worktree is a fact about this machine's disk -- every test that
    only cares about the dispatchable predicate itself says so explicitly rather than depending on
    which worktrees happen to exist beside the checkout pytest is running from (#392)."""
    monkeypatch.setattr(
        agent_guard,
        "backend_worktree_present",
        (lambda backend: present) if isinstance(present, bool) else (lambda b: present[b]),
    )


def test_dispatchable_issues_keeps_only_what_a_worker_could_actually_start_on(
    monkeypatch, tmp_path
):
    ready = [
        # Dispatchable.
        {
            "number": 51,
            "state": "OPEN",
            "labels": [{"name": agent_guard.READY_LABEL}],
            "body": f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->",
        },
        # No budget class: the #345 shape -- ready-looking, undispatchable.
        {
            "number": 52,
            "state": "OPEN",
            "labels": [{"name": agent_guard.READY_LABEL}],
            "body": VALID_BODY,
        },
        # Waiting on a human.
        {
            "number": 53,
            "state": "OPEN",
            "labels": [
                {"name": agent_guard.READY_LABEL},
                {"name": agent_guard.BLOCKED_ON_HUMAN_LABEL},
            ],
            "body": f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->",
        },
        # Blocked by an issue that is still open.
        {
            "number": 54,
            "state": "OPEN",
            "labels": [{"name": agent_guard.READY_LABEL}],
            "body": f"{VALID_BODY}\nBlocked by #51\n\n<!-- budget: mechanical-qwen -->",
        },
    ]
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _gh_issue_list_stub(ready, [{"number": n} for n in (51, 52, 53, 54)]),
    )
    _worktrees_present(monkeypatch)
    assert agent_guard.dispatchable_issues(main=tmp_path) == [51]


def test_dispatchable_issues_empty_when_no_issue_carries_the_ready_label(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_guard.subprocess, "run", _gh_issue_list_stub([], [{"number": 51}]))
    _worktrees_present(monkeypatch)
    assert agent_guard.dispatchable_issues(main=tmp_path) == []


# ---- #392: an issue whose backend has no worktree is not dispatchable, and the exclusion is
# named in the journal and paged for when it leaves nothing at all ----


def _ready_row(number: int, task_class: str = "mechanical-qwen") -> dict:
    return {
        "number": number,
        "state": "OPEN",
        "labels": [{"name": agent_guard.READY_LABEL}],
        "body": f"{VALID_BODY}\n\n<!-- budget: {task_class} -->",
    }


def _one_qwen_ready(monkeypatch, numbers=(389,)):
    rows = [_ready_row(number) for number in numbers]
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _gh_issue_list_stub(rows, [{"number": number} for number in numbers]),
    )


def test_dispatchable_excludes_an_issue_whose_backend_has_no_worktree(monkeypatch, tmp_path):
    # The 2026-09-16 case: `gh pr merge --delete-branch` removed the Qwen worktree, every ready
    # issue resolved to Qwen, and the guard announced all six as dispatchable anyway.
    _one_qwen_ready(monkeypatch, (389, 388))
    _worktrees_present(monkeypatch, {"qwen": False, "claude": True})
    scan = agent_guard.dispatchable_scan(main=tmp_path)
    assert scan.issues == []
    assert scan.without_worktree == {"qwen": [389, 388]}


def test_dispatchable_keeps_an_issue_whose_backend_has_its_worktree(monkeypatch, tmp_path):
    _one_qwen_ready(monkeypatch, (389, 388))
    _worktrees_present(monkeypatch, {"qwen": True, "claude": True})
    scan = agent_guard.dispatchable_scan(main=tmp_path)
    assert scan.issues == [389, 388]
    assert scan.without_worktree == {}


def test_backend_worktree_present_reads_the_dot_git_entry(monkeypatch, tmp_path):
    # The same `[ -e "$worktree/.git" ]` test worker_task.sh applies before it refuses.
    monkeypatch.setattr(
        agent_guard, "BACKEND_WORKTREES", {"qwen": str(tmp_path / "gone"), "claude": str(tmp_path)}
    )
    assert agent_guard.backend_worktree_present("qwen") is False
    assert agent_guard.backend_worktree_present("claude") is False
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    assert agent_guard.backend_worktree_present("claude") is True


def test_the_tick_names_the_backend_and_the_path_it_excluded_for(monkeypatch, tmp_path, capsys):
    _stub_tick(
        monkeypatch,
        results=_idle_results(),
        alive=False,
        dispatchable=[],
        without_worktree={"qwen": [389, 388]},
    )
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: None)
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 20, 46, 0))
    printed = capsys.readouterr().out
    assert "backend qwen has no worktree at" in printed
    assert agent_guard.BACKEND_WORKTREES["qwen"] in printed
    assert "#389, #388" in printed


def test_no_idle_event_and_one_page_when_the_exclusion_leaves_nothing(
    monkeypatch, tmp_path, invocations
):
    _stub_tick(
        monkeypatch,
        results=_idle_results(),
        alive=False,
        dispatchable=[],
        without_worktree={"qwen": [389]},
    )
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))

    first = _utc(2026, 9, 16, 20, 46, 0)
    agent_guard.tick(main=tmp_path, now=first)
    assert len(paged) == 1
    assert "qwen" in paged[0] and agent_guard.BACKEND_WORKTREES["qwen"] in paged[0]
    # No event of any kind, so no planner run is spent rediscovering it.
    assert agent_guard.pending_events(tmp_path) == []
    assert invocations == []

    minutes = agent_guard.load_planner_config().idle_wake_minutes
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=minutes - 1))
    assert len(paged) == 1
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=minutes + 1))
    assert len(paged) == 2


# ---- #384: an issue BECOMING dispatchable is an edge (unthrottled); a set SITTING dispatchable
# stays the rate-limited condition (agent_os/docs/adr/2026-09-16-a-newly-dispatchable-issue-is-an-edge-
# not-a-condition.md) ----


def _tick_with(monkeypatch, dispatchable, *, alive=False):
    _stub_tick(monkeypatch, results=_idle_results(), alive=alive, dispatchable=dispatchable)


def _events_of_kind(tmp_path, kind: str) -> list:
    return [path for path in agent_guard.pending_events(tmp_path) if f"-{kind}-" in path.name]


def test_the_first_tick_records_the_backlog_as_already_seen(monkeypatch, tmp_path, invocations):
    # Installing this must not fire a burst for a backlog that was already waiting.
    _tick_with(monkeypatch, [51, 52])
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    assert _events_of_kind(tmp_path, "new_dispatchable") == []
    assert agent_guard.read_seen_dispatchable(main=tmp_path) == {51, 52}


def test_an_unchanged_set_writes_no_edge_and_keeps_one_idle_wake_per_window(
    monkeypatch, tmp_path, invocations
):
    minutes = agent_guard.load_planner_config().idle_wake_minutes
    _tick_with(monkeypatch, [51])
    first = _utc(2026, 9, 16, 10, 0, 0)
    agent_guard.tick(main=tmp_path, now=first)
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=minutes - 1))
    assert _events_of_kind(tmp_path, "new_dispatchable") == []
    # Exactly today's behaviour: one idle_dispatchable, one planner run.
    assert len(invocations) == 1


def test_a_gained_member_writes_an_edge_event_inside_the_idle_window(
    monkeypatch, tmp_path, invocations
):
    first = _utc(2026, 9, 16, 10, 0, 0)
    _tick_with(monkeypatch, [51])
    agent_guard.tick(main=tmp_path, now=first)
    assert len(invocations) == 1  # the idle wake for the standing set

    _tick_with(monkeypatch, [51, 52])
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=1))
    # Written although the idle window has not elapsed: the rate limit is the condition's, not
    # the edge's.
    assert len(invocations) == 2
    assert "became dispatchable" in invocations[1]
    assert "#52" in invocations[1] and "#51" not in invocations[1]


# ---- #436: a `status:ready` that becomes dispatchable while every parallel slot is occupied buys
# no planner pass -- a pass that can only look and leave costs as much as one that dispatches. ----


def test_new_dispatchable_is_withheld_while_every_slot_is_occupied(
    monkeypatch, tmp_path, invocations, capsys
):
    first = _utc(2026, 9, 21, 10, 0, 0)
    _tick_with(monkeypatch, [51], alive=True)
    agent_guard.tick(main=tmp_path, now=first)  # first tick: recorded as already-seen
    assert invocations == []

    _tick_with(monkeypatch, [51, 52], alive=True)
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=1))
    # `_stub_tick`'s `alive=True` makes every configured backend read as alive, so occupancy (2)
    # is at least `planner.max_parallel_issues` (1 in the real config) whatever the exact count:
    # a pass would only look and leave, so none is paid for.
    assert invocations == []
    printed = capsys.readouterr().out
    assert "#52" in printed and "already running" in printed and "max_parallel_issues" in printed


def test_new_dispatchable_still_wakes_the_planner_when_a_slot_is_free(
    monkeypatch, tmp_path, invocations
):
    first = _utc(2026, 9, 21, 10, 0, 0)
    _tick_with(monkeypatch, [51], alive=False)
    agent_guard.tick(main=tmp_path, now=first)
    assert len(invocations) == 1  # the idle wake for the standing set (nothing alive, unrelated
    # to occupancy) -- the same shape `test_a_gained_member_writes_an_edge_event_inside_the_idle_
    # window` already establishes.

    _tick_with(monkeypatch, [51, 52], alive=False)
    agent_guard.tick(main=tmp_path, now=first + timedelta(minutes=1))
    # Unchanged from today: a free slot means the edge still wakes the planner at once.
    assert len(invocations) == 2
    assert "#52" in invocations[1]


def test_an_issue_that_leaves_the_set_and_returns_counts_as_new(tmp_path):
    now = _utc(2026, 9, 16, 10, 0, 0)
    agent_guard.write_seen_dispatchable([51], main=tmp_path)

    # It leaves (a worker started on it, so it is `status:doing` now).
    assert (
        agent_guard._write_new_dispatchable_event_if_gained([], main=tmp_path, now=now)
        == agent_guard.EventOutcome()
    )
    # It comes back (the run was cut and the planner put it back to `status:ready`).
    outcome = agent_guard._write_new_dispatchable_event_if_gained(
        [51], main=tmp_path, now=now + timedelta(minutes=5)
    )
    assert outcome.written == 1 and "#51" in outcome.line
    assert len(_events_of_kind(tmp_path, "new_dispatchable")) == 1


def test_the_seen_set_lives_under_the_planner_cache_directory(monkeypatch, tmp_path):
    relocated = tmp_path / "elsewhere"
    monkeypatch.setenv("PLANNER_CACHE_DIR", str(relocated))
    agent_guard.write_seen_dispatchable([51], main=tmp_path)
    assert (relocated / "dispatchable_seen.json").is_file()
    assert agent_guard.read_seen_dispatchable(main=tmp_path) == {51}


def test_an_unreadable_seen_file_reads_as_a_first_tick(tmp_path):
    agent_guard.seen_dispatchable_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    agent_guard.seen_dispatchable_path(tmp_path).write_text("not json at all")
    assert agent_guard.read_seen_dispatchable(main=tmp_path) is None
    outcome = agent_guard._write_new_dispatchable_event_if_gained(
        [51], main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0)
    )
    assert outcome.written == 0 and "already-seen" in outcome.line
    assert _events_of_kind(tmp_path, "new_dispatchable") == []


def test_the_cli_accepts_the_new_event_kind(monkeypatch, tmp_path):
    # `agent_guard.py event <kind> <subject>` takes its choices from EVENT_KINDS itself, so this
    # is the whole of "accepted on the command line like the others" -- one more kind, no second
    # list to keep in step.
    monkeypatch.setenv("WORKER_CACHE_DIR", str(agent_guard.cache_dir(tmp_path)))
    monkeypatch.setattr(
        sys, "argv", ["agent_guard.py", "event", "new_dispatchable", "384", "--detail", "d"]
    )
    agent_guard.main()
    assert len(_events_of_kind(tmp_path, "new_dispatchable")) == 1


def test_the_page_is_not_silenced_by_work_another_backend_can_still_run(
    monkeypatch, tmp_path, invocations
):
    # One backend is stopped and the other is not: the stopped one is no less stopped for it, and
    # the first version of this check stayed silent exactly then.
    _stub_tick(
        monkeypatch,
        results=_idle_results(),
        alive=False,
        dispatchable=[51],
        without_worktree={"qwen": [389]},
    )
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 20, 46, 0))
    assert len(paged) == 1 and "qwen" in paged[0]
    # and the dispatchable issue still wakes the planner, as it did before
    assert len(invocations) == 1
    assert "#51" in invocations[0]


def test_the_page_fires_while_a_worker_is_alive_on_the_other_backend(
    monkeypatch, tmp_path, invocations
):
    _stub_tick(
        monkeypatch,
        results=_idle_results(),
        alive=True,
        dispatchable=[],
        without_worktree={"qwen": [389]},
    )
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 20, 46, 0))
    assert len(paged) == 1 and agent_guard.BACKEND_WORKTREES["qwen"] in paged[0]


def test_the_page_counts_only_the_backends_it_names(monkeypatch, tmp_path):
    # One `backend_worktree_missing` page per due backend, not one combined page: each reader
    # gets a page that names only its own backend, sorted the same way `missing_worktree_lines`
    # journals them.
    scan = agent_guard.DispatchableScan([], {"qwen": [389, 388], "claude": [51]})
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    first = _utc(2026, 9, 16, 20, 46, 0)
    line = agent_guard._page_missing_worktree_if_due(scan, main=tmp_path, now=first)
    assert len(paged) == 2  # claude first (sorted), then qwen
    assert paged[0] == render_human_message(
        "backend_worktree_missing",
        backend="claude",
        worktree=agent_guard.BACKEND_WORKTREES["claude"],
        issue_count=1,
    )
    assert paged[1] == render_human_message(
        "backend_worktree_missing",
        backend="qwen",
        worktree=agent_guard.BACKEND_WORKTREES["qwen"],
        issue_count=2,
    )
    assert "claude" in line and "qwen" in line

    # claude's page is now inside its own window; qwen's is not. The next tick's page names qwen
    # only, and its count must be qwen's two, not the three the first tick named in total.
    marker = agent_guard.cache_dir(tmp_path) / "guard" / "paged-missing-worktree-qwen"
    marker.write_text((first - timedelta(days=1)).isoformat())
    line = agent_guard._page_missing_worktree_if_due(scan, main=tmp_path, now=first)
    assert line is not None
    assert len(paged) == 3
    # The line spells qwen's worktree path, and that path says `claude` whenever the checkout sits
    # under `.claude/worktrees/`. What this measures is which backends the line names, so that
    # path is taken out first.
    named = line.replace(agent_guard.BACKEND_WORKTREES["qwen"], "<qwen's worktree>")
    assert "claude" not in named and "qwen" in named
    assert paged[2] == render_human_message(
        "backend_worktree_missing",
        backend="qwen",
        worktree=agent_guard.BACKEND_WORKTREES["qwen"],
        issue_count=2,
    )


def test_a_missing_worktree_page_nobody_can_render_still_marks_the_backend_paged(
    monkeypatch, tmp_path, capsys
):
    # A misspelled `backend_worktree_missing` must not turn a condition only a human can clear
    # into a tick that keeps retrying the same render every few minutes -- print it and still
    # write the rate-limit marker, the same as the quota and run-cap pages do (#366 review).
    _no_messages_configured(monkeypatch)
    scan = agent_guard.DispatchableScan([], {"qwen": [389]})
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    now = _utc(2026, 9, 16, 20, 46, 0)

    line = agent_guard._page_missing_worktree_if_due(scan, main=tmp_path, now=now)

    assert paged == []
    assert "no page for the missing worktree" in capsys.readouterr().out
    assert line is not None and "qwen" in line  # the English journal line still names it
    marker = agent_guard.cache_dir(tmp_path) / "guard" / "paged-missing-worktree-qwen"
    assert marker.is_file()


def test_refinable_issues_keeps_only_status_refine_issues_that_fail_validate(monkeypatch, tmp_path):
    refine = [
        # Needs refining: carries status:refine, body fails validate.
        {
            "number": 61,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": "x",
        },
        # Already a valid brief -- not IN NEED of refining, a promote-refined candidate instead.
        {
            "number": 62,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->",
        },
        # Waiting on a human -- not something to hand the refiner either.
        {
            "number": 63,
            "state": "OPEN",
            "labels": [
                {"name": agent_guard.REFINE_LABEL},
                {"name": agent_guard.BLOCKED_ON_HUMAN_LABEL},
            ],
            "body": "x",
        },
    ]
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _gh_issue_list_stub(refine, [{"number": n} for n in (61, 62, 63)]),
    )
    assert agent_guard.refinable_issues(main=tmp_path) == [61]


def test_refinable_issues_come_in_refine_queue_order_not_gh_list_order(monkeypatch, tmp_path):
    # `gh issue list` answers newest first; the refiner must see the issue closest to dispatch
    # first instead -- auto-ready parent, then priority, then unblocked, then oldest (#32).
    vocabulary = agent_guard.PROJECT.labels
    refine = [
        {
            "number": number,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}, *[{"name": n} for n in extra]],
            "body": "x",
            **({"parent": {"number": parent}} if parent else {}),
        }
        for number, extra, parent in [
            (83, [vocabulary.priorities[3]], 3),
            (74, [vocabulary.priorities[2]], 2),
            (15, [vocabulary.priorities[0]], None),
            (14, [vocabulary.priorities[0]], 2),
        ]
    ]
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _gh_issue_list_stub(refine, [{"number": n} for n in (2, 3, 14, 15, 74, 83)]),
    )
    looked_up: list[int] = []

    def parent_labels(number, *, main):
        looked_up.append(number)
        return {vocabulary.auto_ready} if number == 2 else set()

    monkeypatch.setattr(agent_guard, "_gh_issue_labels", parent_labels)
    assert agent_guard.refinable_issues(main=tmp_path) == [14, 74, 15, 83]
    assert sorted(looked_up) == [2, 3]  # one lookup per parent, not per child


def test_refine_pending_names_the_head_of_the_refine_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_guard, "load_planner_config", lambda: _planner_config())
    monkeypatch.setattr(agent_guard, "refinable_issues", lambda *, main: list(range(14, 30)))
    outcome = agent_guard._write_refine_pending_event_if_due(
        main=tmp_path, now=_utc(2026, 9, 14, 10, 0, 0)
    )
    assert "#14, #15" in outcome.line and "#23" in outcome.line and "#24" not in outcome.line


def _refiner_summary_comment(author, created_at):
    return {
        "author": {"login": author},
        "body": "<!-- refiner-summary -->\n@someone\n\nSplit into two children.\n\n## Doubts\n...",
        "createdAt": created_at,
    }


def _plain_comment(author, created_at, body="an answer"):
    return {"author": {"login": author}, "body": body, "createdAt": created_at}


def test_refinable_issues_drops_an_issue_whose_refiner_doubt_the_human_already_answered(
    monkeypatch, tmp_path
):
    # agent-os#72: the refiner split a feature, posted its summary with a doubt and parked it; the
    # human answered and put `status:refine` back. The feature's own body never conforms (a feature
    # has no template shape), so without reading the comments every idle wake named it again, and
    # the planner -- told never to refine an issue twice -- re-asked the answered question.
    refine = [
        {
            "number": 84,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": "a feature",
            "comments": [
                _refiner_summary_comment(_BOT, "2026-09-24T09:06:40Z"),
                _plain_comment(_HUMAN, "2026-09-24T10:36:32Z"),
            ],
        },
        # A summary nobody answered yet stays named: the planner's one doubt for the human.
        {
            "number": 85,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": "a feature",
            "comments": [
                _plain_comment(_HUMAN, "2026-09-24T08:00:00Z", body="please refine this"),
                _refiner_summary_comment(_BOT, "2026-09-24T09:06:40Z"),
                _plain_comment(_BOT, "2026-09-24T11:02:06Z", body="waiting for you"),
            ],
        },
        # Never refined: named as always.
        {
            "number": 86,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": "x",
            "comments": [],
        },
    ]
    commands: list[list[str]] = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return _gh_issue_list_stub(refine, [{"number": n} for n in (84, 85, 86)])(cmd, **kwargs)

    monkeypatch.setattr(agent_guard.subprocess, "run", run)
    assert agent_guard.refinable_issues(main=tmp_path) == [85, 86]
    listing = next(cmd for cmd in commands if "--label" in cmd)
    assert "comments" in listing[listing.index("--json") + 1].split(",")


def test_refinable_issues_empty_when_no_issue_carries_the_refine_label(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_guard.subprocess, "run", _gh_issue_list_stub([], [{"number": 61}]))
    assert agent_guard.refinable_issues(main=tmp_path) == []


# ---- promote_refined: the mechanical half of the auto-ready promotion ------------------------


def test_promote_refined_moves_only_the_issue_whose_parent_carries_auto_ready(
    monkeypatch, tmp_path
):
    valid_body = f"{VALID_BODY}\n\n<!-- budget: mechanical-qwen -->"
    refine = [
        {  # promotable: valid body, parent carries auto-ready
            "number": 70,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": valid_body,
            "parent": {"number": 10},
        },
        {  # parent lacks auto-ready
            "number": 71,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": valid_body,
            "parent": {"number": 11},
        },
        {  # no parent at all -- the human promotes it by hand
            "number": 72,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": valid_body,
        },
        {  # parent carries auto-ready but the body still fails validate
            "number": 73,
            "state": "OPEN",
            "labels": [{"name": agent_guard.REFINE_LABEL}],
            "body": VALID_BODY,
            "parent": {"number": 10},
        },
    ]
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _gh_issue_list_stub(refine, [{"number": n} for n in (70, 71, 72, 73)]),
    )
    labels_by_parent = {10: {agent_guard.PROJECT.labels.auto_ready}, 11: {"type:feature"}}
    monkeypatch.setattr(
        agent_guard, "_gh_issue_labels", lambda number, *, main: labels_by_parent.get(number, set())
    )
    moved = []
    monkeypatch.setattr(
        agent_guard, "_promote_issue_to_ready", lambda issue, *, main: moved.append(issue)
    )
    assert agent_guard.promote_refined(main=tmp_path) == [70]
    assert moved == ["70"]


def test_promote_refined_nothing_when_no_issue_carries_the_refine_label(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_guard.subprocess, "run", _gh_issue_list_stub([], [{"number": 70}]))
    moved = []
    monkeypatch.setattr(
        agent_guard, "_promote_issue_to_ready", lambda issue, *, main: moved.append(issue)
    )
    assert agent_guard.promote_refined(main=tmp_path) == []
    assert moved == []


def test_agents_paused_true_when_the_epic_carries_the_label(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: _fake_completed(
            cmd, {"labels": [{"name": agent_guard.AGENTS_PAUSED_LABEL}]}
        ),
    )
    assert agent_guard._agents_paused(main=tmp_path) is True


def test_agents_paused_false_when_the_epic_does_not_carry_the_label(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: _fake_completed(cmd, {"labels": [{"name": "type:epic"}]}),
    )
    assert agent_guard._agents_paused(main=tmp_path) is False


# ---- #365: the tick reconciles mechanical state on its own -- closed-by-merge to done,
# promote-refined every tick, and an orphan status:doing issue as an event ----


def _closed_list_stub(closed_rows, doing_rows=()):
    """`gh issue list` twice over: `--state closed` for the reconciliation, `--label
    status:doing` for the orphan check. Anything else answers empty."""

    def run(cmd, **kwargs):
        if "closed" in cmd:
            return _fake_completed(cmd, closed_rows)
        if agent_guard.DOING_LABEL in cmd:
            return _fake_completed(cmd, list(doing_rows))
        return _fake_completed(cmd, [])

    return run


def test_a_closed_issue_with_a_status_label_is_reconciled_to_done(monkeypatch, tmp_path):
    # #348, #349 and #357 on 2026-09-16: closed by a merged PR's `Closes #N`, still `status:review`
    # on both the label and the board, because GitHub's auto-close never calls `issues.py move`.
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _closed_list_stub(
            [
                {"number": 348, "labels": [{"name": agent_guard.PROJECT.labels.review}]},
                {"number": 999, "labels": [{"name": "module:workers"}]},
            ]
        ),
    )
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_move_issue",
        lambda issue, state, *, main: moved.append((issue, state)) or True,
    )
    lines = agent_guard.reconcile_closed_issues(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    assert moved == [("348", "done")]
    assert lines == [
        f"issue #348: closed but still labeled {agent_guard.PROJECT.labels.review} -- moved to done"
    ]


def test_a_closed_issue_without_a_status_label_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _closed_list_stub([{"number": 999, "labels": [{"name": "type:task"}]}]),
    )
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_move_issue",
        lambda issue, state, *, main: moved.append((issue, state)) or True,
    )
    assert agent_guard.reconcile_closed_issues(main=tmp_path, now=_utc(2026, 9, 16, 10, 0)) == []
    assert moved == []


def test_the_tick_reconciles_and_promotes_on_every_call(monkeypatch, tmp_path, invocations, capsys):
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[])
    monkeypatch.setattr(
        agent_guard,
        "reconcile_closed_issues",
        lambda *, main, now: ["issue #348: ... -- moved to done"],
    )
    promoted_calls: list[int] = []
    monkeypatch.setattr(
        agent_guard, "promote_refined", lambda *, main: promoted_calls.append(1) or [61]
    )
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    printed = capsys.readouterr().out
    assert promoted_calls == [1]
    assert "issue #348" in printed
    assert f"promoted to {agent_guard.READY_LABEL}: #61" in printed


def _worker_marker(tmp_path, backend: str, issue: str) -> None:
    paths = agent_guard.worker_paths(backend, tmp_path)
    paths.issuefile.parent.mkdir(parents=True, exist_ok=True)
    paths.issuefile.write_text(f"{issue}\n")


def test_orphan_doing_skips_a_live_run_and_a_blocked_issue(monkeypatch, tmp_path):
    doing = [
        {"number": 400, "labels": [{"name": agent_guard.DOING_LABEL}]},  # live on qwen
        {
            "number": 401,
            "labels": [
                {"name": agent_guard.DOING_LABEL},
                {"name": agent_guard.BLOCKED_ON_HUMAN_LABEL},
            ],
        },
        {"number": 402, "labels": [{"name": agent_guard.DOING_LABEL}]},  # the orphan
    ]
    monkeypatch.setattr(agent_guard.subprocess, "run", _closed_list_stub([], doing))
    _worker_marker(tmp_path, "qwen", "400")
    _worker_marker(tmp_path, "claude", "402")  # a marker from a run that is NOT alive any more
    monkeypatch.setattr(
        agent_guard,
        "_is_alive",
        lambda pidfile: pidfile == agent_guard.worker_paths("qwen", tmp_path).pidfile,
    )
    assert agent_guard.orphan_doing_issues(main=tmp_path) == [402]


def test_the_orphan_event_is_rate_limited_per_issue(monkeypatch, tmp_path):
    minutes = agent_guard.load_planner_config().idle_wake_minutes
    orphans = [402]
    monkeypatch.setattr(agent_guard, "orphan_doing_issues", lambda *, main: list(orphans))
    first = _utc(2026, 9, 16, 10, 0, 0)

    outcomes = agent_guard._write_orphan_doing_events_if_due(main=tmp_path, now=first)
    assert outcomes == [agent_guard.EventOutcome("orphan_doing written (#402)", written=1)]

    outcomes = agent_guard._write_orphan_doing_events_if_due(
        main=tmp_path, now=first + timedelta(minutes=minutes - 1)
    )
    assert outcomes[0].written == 0 and "no event written" in outcomes[0].line
    assert len(_events_of_kind(tmp_path, "orphan_doing")) == 1

    # A SECOND stuck issue is a second fact: the first one's event must not hide it.
    orphans.append(403)
    outcomes = agent_guard._write_orphan_doing_events_if_due(
        main=tmp_path, now=first + timedelta(minutes=1)
    )
    assert outcomes[1] == agent_guard.EventOutcome("orphan_doing written (#403)", written=1)
    assert len(_events_of_kind(tmp_path, "orphan_doing")) == 2

    # And the window does elapse.
    outcomes = agent_guard._write_orphan_doing_events_if_due(
        main=tmp_path, now=first + timedelta(minutes=minutes + 1)
    )
    assert outcomes[0] == agent_guard.EventOutcome("orphan_doing written (#402)", written=1)


def test_the_tick_writes_the_orphan_event_and_wakes(monkeypatch, tmp_path, invocations):
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[], orphans=[402])
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    assert len(invocations) == 1
    assert "#402" in invocations[0] and agent_guard.DOING_LABEL in invocations[0]


def test_nothing_is_reconciled_while_the_epic_carries_agents_paused(monkeypatch, tmp_path):
    _stub_tick(monkeypatch, results=_idle_results(), paused=True, alive=False, orphans=[402])
    reconciled: list[int] = []
    monkeypatch.setattr(
        agent_guard, "reconcile_closed_issues", lambda *, main, now: reconciled.append(1) or []
    )
    monkeypatch.setattr(agent_guard, "promote_refined", lambda *, main: reconciled.append(2) or [])
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    assert reconciled == []
    assert agent_guard.pending_events(tmp_path) == []


# ---- #385 (tick side, absorbed by #365): an issue a worker is RUNNING always carries
# status:doing, even when a human's reply cleared the label out from under it ----


def _live_run_on(monkeypatch, tmp_path, backend: str, issue: str) -> None:
    _worker_marker(tmp_path, backend, issue)
    monkeypatch.setattr(
        agent_guard,
        "_is_alive",
        lambda pidfile: pidfile == agent_guard.worker_paths(backend, tmp_path).pidfile,
    )


def test_a_live_run_with_no_status_label_is_put_back_to_doing(monkeypatch, tmp_path):
    # #363 twice on 2026-09-16: the guard cut the run, the human replied, `_check_human_replies`
    # cleared `status:blocked-on-human`, and the issue sat with a live worker and no label at all.
    _live_run_on(monkeypatch, tmp_path, "qwen", "363")
    monkeypatch.setattr(agent_guard, "_gh_issue_labels", lambda number, *, main: {"module:workers"})
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_move_issue",
        lambda issue, state, *, main: moved.append((issue, state)) or True,
    )
    lines = agent_guard.restore_doing_label_on_live_runs(main=tmp_path)
    assert moved == [("363", "doing")]
    assert lines == [
        f"issue #363: qwen is running it with no status label -- set to {agent_guard.DOING_LABEL}"
    ]


def test_a_live_run_whose_issue_already_carries_a_state_label_is_left_alone(monkeypatch, tmp_path):
    _live_run_on(monkeypatch, tmp_path, "qwen", "363")
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_move_issue",
        lambda issue, state, *, main: moved.append((issue, state)) or True,
    )
    for held in (agent_guard.DOING_LABEL, agent_guard.PROJECT.labels.review):
        monkeypatch.setattr(agent_guard, "_gh_issue_labels", lambda number, *, main, h=held: {h})
        assert agent_guard.restore_doing_label_on_live_runs(main=tmp_path) == []
    assert moved == []


def test_a_failed_label_lookup_is_not_read_as_an_absent_label(monkeypatch, tmp_path):
    _live_run_on(monkeypatch, tmp_path, "qwen", "363")
    monkeypatch.setattr(agent_guard, "_gh_issue_labels", lambda number, *, main: None)
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_move_issue",
        lambda issue, state, *, main: moved.append((issue, state)) or True,
    )
    assert agent_guard.restore_doing_label_on_live_runs(main=tmp_path) == []
    assert moved == []


def test_nothing_is_restored_when_no_worker_is_alive(monkeypatch, tmp_path):
    _worker_marker(tmp_path, "qwen", "363")
    monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: False)
    monkeypatch.setattr(agent_guard, "_gh_issue_labels", lambda number, *, main: set())
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_move_issue",
        lambda issue, state, *, main: moved.append((issue, state)) or True,
    )
    assert agent_guard.restore_doing_label_on_live_runs(main=tmp_path) == []
    assert moved == []


# ---- a reconciliation is only reported when it actually landed, and the state file is never a
# tick's undoing ----


def test_a_failed_move_is_reported_as_a_failure_not_as_a_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        _closed_list_stub(
            [{"number": 348, "labels": [{"name": agent_guard.PROJECT.labels.review}]}]
        ),
    )
    monkeypatch.setattr(agent_guard, "_move_issue", lambda issue, state, *, main: False)
    lines = agent_guard.reconcile_closed_issues(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    assert len(lines) == 1
    assert "FAILED" in lines[0] and "still not reconciled" in lines[0]


def test_a_failed_restore_is_reported_as_a_failure(monkeypatch, tmp_path):
    _live_run_on(monkeypatch, tmp_path, "qwen", "363")
    monkeypatch.setattr(agent_guard, "_gh_issue_labels", lambda number, *, main: set())
    monkeypatch.setattr(agent_guard, "_move_issue", lambda issue, state, *, main: False)
    lines = agent_guard.restore_doing_label_on_live_runs(main=tmp_path)
    assert len(lines) == 1
    assert "FAILED" in lines[0]


def test_move_issue_reports_the_exit_status_of_issues_py(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, seen["code"], stdout="", stderr="")

    monkeypatch.setattr(agent_guard.subprocess, "run", fake_run)
    monkeypatch.setattr(agent_guard, "_mechanism_env", lambda *, main: {})
    seen["code"] = 0
    assert agent_guard._move_issue("348", "done", main=tmp_path) is True
    assert seen["cmd"][-3:] == ["move", "348", "done"]
    seen["code"] = 1
    assert agent_guard._move_issue("348", "done", main=tmp_path) is False


@pytest.mark.parametrize("content", ["not json", "{}", "[null]", '["abc"]', "[51, {}]"])
def test_an_unusable_seen_file_reads_as_a_first_tick_and_never_raises(tmp_path, content):
    # The file is written only AFTER it is read, so an exception here would repeat on every tick
    # forever -- a torn write must cost one missed edge, not the guard.
    path = agent_guard.seen_dispatchable_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert agent_guard.read_seen_dispatchable(main=tmp_path) is None
    outcome = agent_guard._write_new_dispatchable_event_if_gained(
        [51], main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0)
    )
    assert "already-seen" in outcome.line
    assert agent_guard.read_seen_dispatchable(main=tmp_path) == {51}


# ---- the closed-issue listing is scoped by close date, not by "everything closed, first page" ----


def test_the_first_reconciliation_looks_back_the_configured_window(monkeypatch, tmp_path):
    seen: dict = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _fake_completed(cmd, [])

    monkeypatch.setattr(agent_guard.subprocess, "run", run)
    days = agent_guard.load_planner_config().reconcile_closed_lookback_days
    now = _utc(2026, 9, 16, 10, 0, 0)
    assert agent_guard.closed_issues_with_status_label(main=tmp_path, now=now) == []
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    assert f"closed:>={since}" in seen["cmd"]
    assert "--state" in seen["cmd"] and "closed" in seen["cmd"]


def test_a_later_reconciliation_asks_only_about_what_closed_since_the_last_one(
    monkeypatch, tmp_path
):
    seen: dict = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _fake_completed(cmd, [])

    monkeypatch.setattr(agent_guard.subprocess, "run", run)
    first = _utc(2026, 9, 16, 10, 0, 0)
    agent_guard.reconcile_closed_issues(main=tmp_path, now=first)
    assert agent_guard.reconcile_marker_path(tmp_path).is_file()

    later = first + timedelta(days=3)
    agent_guard.closed_issues_with_status_label(main=tmp_path, now=later)
    # The marker's own day minus one: the search is day-granular, and a pass whose listing failed
    # still moved the marker.
    assert "closed:>=2026-09-15" in seen["cmd"]


def test_the_tick_counts_events_from_the_fact_not_from_the_wording(
    monkeypatch, tmp_path, invocations, capsys
):
    # The count used to be `line.startswith("idle_dispatchable written")`: rewording a log line
    # silently changed what the tick reported about itself.
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dispatchable=[51])
    monkeypatch.setattr(
        agent_guard,
        "_write_idle_event_if_due",
        lambda issues, *, main, now: agent_guard.EventOutcome("an idle wake, reworded", written=1),
    )
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 16, 10, 0, 0))
    printed = capsys.readouterr().out
    assert "an idle wake, reworded" in printed
    assert "1 event(s) written this tick" in printed


# ---- whose comment lifts status:blocked-on-human: the human's, never the mechanism's own ----


def _blocked_issue_with_comments(monkeypatch, comments):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: _fake_completed(cmd, [{"number": 387}]),
    )

    def fake_gh_api_json(path, *, main):
        if path.endswith("/timeline"):
            return [
                {
                    "event": "labeled",
                    "label": {"name": agent_guard.BLOCKED_ON_HUMAN_LABEL},
                    "created_at": "2026-09-16T10:00:00Z",
                }
            ]
        if path.endswith("/comments"):
            return comments
        raise AssertionError(path)

    monkeypatch.setattr(agent_guard, "_gh_api_json", fake_gh_api_json)
    cleared: list[str] = []
    monkeypatch.setattr(
        agent_guard, "_clear_blocked_label", lambda issue, *, main: cleared.append(issue)
    )
    return cleared


def test_the_planners_own_comment_does_not_count_as_the_humans_reply(monkeypatch, tmp_path):
    # #387, 2026-09-16: the planner commented that it was waiting for an answer, and that comment
    # unblocked the issue the question was still unanswered on.
    cleared = _blocked_issue_with_comments(
        monkeypatch,
        [{"created_at": "2026-09-16T11:00:00Z", "user": {"login": _BOT}}],
    )
    assert agent_guard._check_human_replies(main=tmp_path) == []
    assert cleared == []


def test_a_human_comment_after_the_bots_still_lifts_the_label(monkeypatch, tmp_path):
    cleared = _blocked_issue_with_comments(
        monkeypatch,
        [
            {"created_at": "2026-09-16T11:00:00Z", "user": {"login": _BOT}},
            {"created_at": "2026-09-16T12:00:00Z", "user": {"login": _HUMAN}},
        ],
    )
    assert agent_guard._check_human_replies(main=tmp_path) == ["387"]
    assert cleared == ["387"]


def test_a_bot_comment_after_the_humans_does_not_hide_the_reply(monkeypatch, tmp_path):
    # The newest comment on the issue is the mechanism's; the newest HUMAN comment is what counts,
    # and it is still newer than the label.
    cleared = _blocked_issue_with_comments(
        monkeypatch,
        [
            {"created_at": "2026-09-16T11:00:00Z", "user": {"login": _HUMAN}},
            {"created_at": "2026-09-16T11:30:00Z", "user": {"login": _BOT}},
        ],
    )
    assert agent_guard._check_human_replies(main=tmp_path) == ["387"]
    assert cleared == ["387"]


def test_a_comment_with_no_author_at_all_is_not_a_reply(monkeypatch, tmp_path):
    cleared = _blocked_issue_with_comments(monkeypatch, [{"created_at": "2026-09-16T11:00:00Z"}])
    assert agent_guard._check_human_replies(main=tmp_path) == []
    assert cleared == []


# ---- ntfy: every page renders from `project.messages`, never from a literal in code
# (#366, `agent_os/docs/AGENT_OS.md` §7 row (g);
# agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md) ---------------

# The functions that hand a string to `agent_os/bin/notify.sh`, per module. Anything reaching one of
# them must already have been rendered from the configured templates -- that is the whole claim
# this section checks, and it checks it structurally, so a new page added later cannot quietly
# reintroduce a literal.
NOTIFY_SINKS = {"guard": {"notify", "_page_run_cap_once"}, "issues": {"page_human"}}


def _sink_calls(tree: ast.AST, sinks: set[str]) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in sinks
    ]


def _is_rendered(expression: ast.expr) -> bool:
    """A message is acceptable only if it is a `render_human_message(...)` call, a slice of one
    (`[:400]`, the ntfy length guard), or a plain name -- and every plain name is then traced back
    to its own source by the test below."""
    if isinstance(expression, ast.Subscript):
        return _is_rendered(expression.value)
    if isinstance(expression, ast.Name):
        return True
    return isinstance(expression, ast.Call) and getattr(expression.func, "id", "") == (
        "render_human_message"
    )


@pytest.mark.parametrize("module", sorted(NOTIFY_SINKS))
def test_no_string_literal_in_the_code_ever_reaches_notify_sh(module):
    tree = ast.parse((AGENT_OS_DIR / "agent_os" / f"{module}.py").read_text())
    calls = _sink_calls(tree, NOTIFY_SINKS[module])
    assert calls, f"{module}.py pages nobody -- the sink names in NOTIFY_SINKS went stale"
    for call in calls:
        assert call.args, f"{module}.py: a page with no message"
        assert _is_rendered(call.args[0]), (
            f"{module}.py line {call.lineno}: the message handed to "
            f"{call.func.id} is not rendered from project.messages"
        )


@pytest.mark.parametrize("module", sorted(NOTIFY_SINKS))
def test_a_name_paged_in_the_code_comes_from_a_parameter_or_from_the_renderer(module):
    """The loophole `_is_rendered` leaves open: a bare name could hold anything. Every one of them
    is either the enclosing function's own parameter -- so its caller is the call site checked
    above -- or assigned in that function from `render_human_message`."""
    tree = ast.parse((AGENT_OS_DIR / "agent_os" / f"{module}.py").read_text())
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for call in _sink_calls(function, NOTIFY_SINKS[module]):
            message = call.args[0]
            if not isinstance(message, ast.Name):
                continue
            parameters = {
                argument.arg
                for argument in function.args.args
                + function.args.kwonlyargs
                + function.args.posonlyargs
            }
            rendered_here = {
                target.id
                for node in ast.walk(function)
                if isinstance(node, ast.Assign) and _is_rendered(node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            assert message.id in parameters | rendered_here, (
                f"{module}.py line {call.lineno}: `{message.id}` is neither a parameter of "
                f"`{function.name}` nor rendered from project.messages in it"
            )


def _rate_limited_claude_run(tmp_path, monkeypatch, *, task_class):
    """A live `claude` run whose events say the quota is rejected: the tick's one mechanical page."""
    cache = tmp_path / ".cache"
    worktree = tmp_path / "example-claude"
    cache.mkdir(parents=True)
    worktree.mkdir()
    monkeypatch.setenv("WORKER_CACHE_DIR", str(cache))
    monkeypatch.setenv("WORKER_WORKTREE", str(worktree))
    (cache / "worker_claude.state").write_text("STARTED\n")
    (cache / "worker_claude.issue").write_text("366\n")
    (cache / "worker_claude.jsonl").write_text(
        json.dumps(_assistant_event("2026-09-16T10:00:00Z"))
        + "\n"
        + json.dumps(_rate_limit_event("rejected"))
        + "\n"
    )
    body = f"{VALID_BODY}\n\n<!-- budget: mechanical-claude -->"
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""),
    )
    monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: True)
    monkeypatch.setattr(agent_guard, "_commit_timestamps", lambda tree, startref: [])
    monkeypatch.setattr(agent_guard, "load_task_classes", lambda: {"mechanical-claude": task_class})
    monkeypatch.setattr(agent_guard, "cut_run", lambda backend, reason, **kwargs: None)
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    return paged


def test_the_quota_page_is_the_configured_template_in_the_humans_language(tmp_path, monkeypatch):
    paged = _rate_limited_claude_run(
        tmp_path, monkeypatch, task_class=_task_class(qwen_fallback_eligible=False)
    )
    result = agent_guard._tick_backend("claude", main=tmp_path)
    assert result.cut_reason == "quota"
    assert paged == [
        render_human_message(
            "quota_exhausted_no_fallback",
            issue="366",
            backend="claude",
            task_class="mechanical-claude",
        )
    ]
    # And what it says is the project's own wording, not this test's: #366 exists because that
    # sentence used to be a Spanish literal a project configuring English could not reach.
    assert "366" in paged[0] and "mechanical-claude" in paged[0]


def test_a_class_with_a_qwen_fallback_is_cut_without_paging_anyone(tmp_path, monkeypatch):
    paged = _rate_limited_claude_run(
        tmp_path, monkeypatch, task_class=_task_class(qwen_fallback_eligible=True)
    )
    assert agent_guard._tick_backend("claude", main=tmp_path).cut_reason == "quota"
    assert paged == []


def test_the_daily_cap_page_names_the_events_that_are_waiting_and_the_log_line_stays_english(
    tmp_path, monkeypatch, invocations
):
    cap = agent_guard.load_planner_config().max_runs_per_day
    runs = agent_guard.planner_dir(tmp_path)
    runs.mkdir(parents=True)
    (runs / "runs.tsv").write_text(
        RUNS_TSV_HEADER
        + "\n"
        + "\n".join(f"2026-09-16T0{i % 10}:00:00Z\tctx\tm\t3\t0.10" for i in range(cap))
        + "\n"
    )
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    agent_guard.write_event("worker_cut", "claude", main=tmp_path)

    summary = agent_guard.wake(main=tmp_path, now=_utc(2026, 9, 16, 20, 0, 0))

    context = agent_guard.event_context(agent_guard.pending_events(tmp_path))
    assert paged == [
        render_human_message(
            "planner_run_cap_reached", runs=cap, cap=cap, pending=1, context=context
        )
    ]
    # The ADR requires a page to name WHICH issue and why: a bare count of waiting events is not
    # a page, it is a number. Both texts carry the same context; only the language differs.
    assert context and context in paged[0]
    # Two readers, two texts: the journal keeps the English operational line it always had.
    assert "planner run cap reached" in summary
    assert context in summary
    assert summary not in paged


def _no_messages_configured(monkeypatch) -> None:
    """A project whose `config/agents.yaml` carries no `project.messages` at all -- the same thing
    a misspelled key looks like from the renderer's side. Patched on `agent_lib`, not on
    `agent_guard`: `render_human_message` resolves `load_project` in the module it is defined in,
    so patching the importing module would have left the real config in play and proved nothing."""
    monkeypatch.setattr(
        agent_lib,
        "load_project",
        lambda *args, **kwargs: ProjectConfig(
            repo="a/b", tracking_epic=1, board_number=1, messages={}
        ),
    )


def test_a_page_nobody_can_render_never_escapes_the_tick_and_never_undoes_the_cut(
    tmp_path, monkeypatch, capsys
):
    """A misspelled key in `project.messages` used to raise out of `_tick_backend` AFTER the cut
    was committed -- taking `tick` with it, and the other backend's check along with it, over a
    config typo nothing at that point could act on (#366 review)."""
    paged = _rate_limited_claude_run(
        tmp_path, monkeypatch, task_class=_task_class(qwen_fallback_eligible=False)
    )
    _no_messages_configured(monkeypatch)

    result = agent_guard._tick_backend("claude", main=tmp_path)

    assert result.cut_reason == "quota"  # the cut still landed and is still reported
    assert paged == []
    assert "no page for the exhausted quota" in capsys.readouterr().out


def test_a_cap_page_nobody_can_render_never_stops_wake_from_reporting_the_cap(
    tmp_path, monkeypatch, capsys, invocations
):
    cap = agent_guard.load_planner_config().max_runs_per_day
    runs = agent_guard.planner_dir(tmp_path)
    runs.mkdir(parents=True)
    (runs / "runs.tsv").write_text(
        RUNS_TSV_HEADER
        + "\n"
        + "\n".join(f"2026-09-16T0{i % 10}:00:00Z\tctx\tm\t3\t0.10" for i in range(cap))
        + "\n"
    )
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    _no_messages_configured(monkeypatch)
    agent_guard.write_event("worker_cut", "claude", main=tmp_path)

    summary = agent_guard.wake(main=tmp_path, now=_utc(2026, 9, 16, 20, 0, 0))

    assert "planner run cap reached" in summary
    assert paged == []
    assert "no page for the run cap" in capsys.readouterr().out
    assert len(agent_guard.pending_events(tmp_path)) == 1  # still nothing consumed


# ---- #400: a one-shot role whose run died before announcing its own end becomes an event -------
#
# The contract under test is the one `agent_os/bin/agent_task.sh` states in its own header: the detached
# run removes its PID file as its last step, so a PID file still on disk beside a dead PID is a run
# that never got there. PR #399's validator is the case this exists for -- killed with the planner
# session that had launched it, its log stopping mid-turn with no `result`, no `validator_finished`,
# no review and nothing recording that the review never happened.

RESULT_EVENT = '{"type":"result","subtype":"success","num_turns":4,"total_cost_usd":0.42}\n'


def _dead_pid() -> int:
    """A PID nothing answers on. Measured rather than monkeypatched, because `os.kill(pid, 0)`
    inside `_is_alive` IS the detection: a stubbed `_is_alive` would leave the one call this feature
    rests on unexercised, and green."""
    candidate = os.getpid()
    while True:
        candidate += 1
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue  # somebody else's process: alive, and not this scan's to report on


def _role_run_on_disk(
    directory: Path, stamp: str, subject: str, *, role: str = "validator", log_tail: str = ""
) -> Path:
    """One one-shot run exactly as the driver leaves it on disk: the log header it writes BEFORE the
    detach, whatever the backend appended, and the PID file the run itself was to remove. Returns
    the PID file's path. `directory` comes from `role_run_dir`, never from a path spelled out here,
    so the scan and its fixture cannot disagree about where a run lives."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stamp}.log").write_text(
        f"ts:        {stamp}\n"
        f"role:      {role} (class {role})\n"
        f"model:     a-model\n"
        f"subject:   #{subject}\n"
        f"context:   {role} #{subject}\n"
        f"pid:       {_dead_pid()} (detached)\n" + log_tail
    )
    pidfile = directory / f"{stamp}.pid"
    pidfile.write_text(f"{_dead_pid()}\n")
    return pidfile


def test_role_run_death_is_none_while_the_run_is_alive():
    # A PID file is written for a run that is merely UNFINISHED: a validator twelve minutes into a
    # review and one that died are the same bytes on disk until the process behind them is gone.
    assert agent_guard.role_run_death(pid_alive=True, log_text="") is None
    assert agent_guard.role_run_death(pid_alive=True, log_text=RESULT_EVENT) is None


def test_role_run_death_reads_a_missing_result_event_as_a_run_that_died_mid_turn():
    log = 'subject:   #399\n{"type":"assistant","message":{"content":[]}}\n'
    assert agent_guard.role_run_death(pid_alive=False, log_text=log) == "died_mid_run"
    # No log at all is the same verdict, not an error: the launch that never happened (#400 stage 1)
    # leaves a PID file and a header, and nothing else.
    assert agent_guard.role_run_death(pid_alive=False, log_text="") == "died_mid_run"


def test_role_run_death_reads_a_result_event_as_an_end_nobody_announced():
    assert (
        agent_guard.role_run_death(pid_alive=False, log_text=f"subject:   #399\n{RESULT_EVENT}")
        == "ended_unannounced"
    )


def test_a_run_that_reached_its_own_end_leaves_the_scan_nothing(tmp_path):
    # The negative control the whole feature is judged by: a finished run's log is still there, with
    # its `result` event, and its PID file is gone because the run removed it -- so a validator that
    # did its job is never reported as one that died.
    directory = agent_guard.role_run_dir("validator", tmp_path)
    directory.mkdir(parents=True)
    (directory / "20260916T182534Z.log").write_text(f"subject:   #395\n{RESULT_EVENT}")
    (directory / "runs.tsv").write_text(RUNS_TSV_HEADER + "\n")
    assert agent_guard.dead_role_runs(main=tmp_path) == []


def test_the_scan_finds_the_dead_runs_of_both_roles_and_skips_a_live_one(tmp_path):
    validator_dir = agent_guard.role_run_dir("validator", tmp_path)
    dead = _role_run_on_disk(validator_dir, "20260916T230556Z", "399")
    live = _role_run_on_disk(validator_dir, "20260917T090000Z", "400")
    live.write_text(f"{os.getpid()}\n")  # this test's own process: alive by construction
    refiner = _role_run_on_disk(
        agent_guard.role_run_dir("refiner", tmp_path), "20260917T091000Z", "336", role="refiner"
    )
    # A PID file whose log never got written: still a run that died, named by its own stamp because
    # there is no header left to read a subject out of.
    no_log = validator_dir / "20260917T092000Z.pid"
    no_log.write_text(f"{_dead_pid()}\n")

    runs = agent_guard.dead_role_runs(main=tmp_path)

    assert [(run.role, run.subject, run.death) for run in runs] == [
        ("validator", "399", "died_mid_run"),
        ("validator", "20260917T092000Z", "died_mid_run"),
        ("refiner", "336", "died_mid_run"),
    ]
    assert [run.pidfile for run in runs] == [dead, no_log, refiner], "oldest run first"
    assert [run.logfile.name for run in runs[:1]] == ["20260916T230556Z.log"]
    assert live.exists(), "a live run's PID file is not this scan's to touch"


def test_a_dead_run_becomes_one_event_and_its_pid_file_goes_with_it(tmp_path):
    directory = agent_guard.role_run_dir("validator", tmp_path)
    pidfile = _role_run_on_disk(directory, "20260916T230556Z", "399")
    logfile = directory / "20260916T230556Z.log"
    now = _utc(2026, 9, 17, 6, 0, 0)

    outcomes = agent_guard._write_role_died_events(main=tmp_path, now=now)

    assert outcomes == [
        agent_guard.EventOutcome(
            "role_died written (validator on #399: died_mid_run, PID file removed)", written=1
        )
    ]
    events = _events_of_kind(tmp_path, "role_died")
    assert len(events) == 1 and events[0].name.endswith("-role_died-399")
    detail = events[0].read_text()
    assert "the validator on #399 died mid-run" in detail
    assert "no validator_finished was written" in detail
    assert "Relaunch it" in detail and str(logfile) in detail
    assert not pidfile.exists(), (
        "the reported run's PID file is what makes this an edge, not a condition"
    )
    assert logfile.is_file(), "the log is the evidence and stays where the run wrote it"

    # The tick fires every five minutes and each event can wake a PAID planner run: the same dead
    # run reported twice is money spent twice on a fact nobody has acted on yet.
    assert agent_guard._write_role_died_events(main=tmp_path, now=now + timedelta(minutes=5)) == []
    assert len(_events_of_kind(tmp_path, "role_died")) == 1


def test_a_run_that_died_after_its_backend_finished_says_so(tmp_path):
    directory = agent_guard.role_run_dir("validator", tmp_path)
    _role_run_on_disk(directory, "20260917T055108Z", "399", log_tail=RESULT_EVENT)

    outcomes = agent_guard._write_role_died_events(main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0))

    assert outcomes[0].line == (
        "role_died written (validator on #399: ended_unannounced, PID file removed)"
    )
    detail = _events_of_kind(tmp_path, "role_died")[0].read_text()
    assert "before writing validator_finished" in detail
    assert "read" in detail.lower() and "relaunch it if it did not" in detail


def test_a_run_whose_log_carries_no_subject_is_named_by_its_own_stamp(tmp_path):
    directory = agent_guard.role_run_dir("validator", tmp_path)
    directory.mkdir(parents=True)
    (directory / "20260917T092000Z.pid").write_text(f"{_dead_pid()}\n")

    outcomes = agent_guard._write_role_died_events(main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0))

    assert outcomes[0].line == (
        "role_died written (validator run 20260917T092000Z: died_mid_run, PID file removed)"
    )
    detail = _events_of_kind(tmp_path, "role_died")[0].read_text()
    assert "the validator run 20260917T092000Z died mid-run" in detail
    # Never "#20260917T092000Z": the planner reads this line as what to relaunch, and a stamp is a
    # log to open, not a subject to relaunch on.
    assert "#20260917T092000Z" not in detail


def test_the_scan_reads_the_directory_the_driver_itself_writes_to(monkeypatch, tmp_path):
    # `AGENT_CACHE_DIR` is the driver's own override, and it replaces `.cache/<role>` WHOLE. A guard
    # that resolved the runs anywhere else would scan a directory nothing writes to and report
    # nothing, forever -- the failure this feature exists to end, reintroduced by a path.
    relocated = tmp_path / "relocated"
    monkeypatch.setenv("AGENT_CACHE_DIR", str(relocated))
    assert agent_guard.role_run_dir("validator", tmp_path / "elsewhere") == relocated
    _role_run_on_disk(relocated, "20260916T230556Z", "399")
    _role_run_on_disk(relocated, "20260917T010343Z", "336", role="refiner")

    runs = agent_guard.dead_role_runs(main=tmp_path)

    # ONE finding per dead run and never one per role: this override resolves every role to the same
    # directory, so scanning it per role would report the same run twice -- two events, two paid
    # planner wakes, one dead validator. The role each finding carries comes from its own log header.
    assert [(run.role, run.subject) for run in runs] == [("validator", "399"), ("refiner", "336")]

    monkeypatch.delenv("AGENT_CACHE_DIR")
    main = tmp_path / "checkout"
    assert agent_guard.role_run_dir("validator", main) == agent_guard.cache_dir(main) / "validator"
    assert agent_guard.dead_role_runs(main=main) == []


def test_the_tick_writes_role_died_and_wakes(monkeypatch, tmp_path, invocations):
    pidfile = _role_run_on_disk(
        agent_guard.role_run_dir("validator", tmp_path), "20260916T230556Z", "399"
    )
    # The real scan, before `_stub_tick` replaces it: what the tick is then given is what the disk
    # actually held, so this asserts the wiring and the scan in one run.
    dead = agent_guard.dead_role_runs(main=tmp_path)
    assert len(dead) == 1
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, dead_runs=dead)

    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0))

    assert len(invocations) == 1
    assert "the validator on #399 died mid-run" in invocations[0]
    assert not pidfile.exists()


def test_a_paused_epic_holds_the_report_until_the_pause_is_lifted(
    monkeypatch, tmp_path, invocations
):
    pidfile = _role_run_on_disk(
        agent_guard.role_run_dir("validator", tmp_path), "20260916T230556Z", "399"
    )
    dead = agent_guard.dead_role_runs(main=tmp_path)
    _stub_tick(monkeypatch, results=_idle_results(), paused=True, dead_runs=dead)

    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0))

    # The pause is the human's full stop, so no event and no planner -- but the PID file stays, and
    # that is what makes the pause reversible: the dead run is reported on the first tick after the
    # pause is lifted instead of being swallowed by it.
    assert invocations == []
    assert agent_guard.latest_event_at("role_died", main=tmp_path) is None
    assert pidfile.exists()

    monkeypatch.setattr(agent_guard, "_agents_paused", lambda *, main: False)
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 6, 5, 0))

    # `latest_event_at`, not the pending listing: this tick's `wake` handed the event to the planner
    # and moved it to `consumed/`, which is the proof it was delivered rather than merely written.
    assert agent_guard.latest_event_at("role_died", subject="399", main=tmp_path) == _utc(
        2026, 9, 17, 6, 5, 0
    )
    assert len(invocations) == 1
    assert "the validator on #399 died mid-run" in invocations[0]
    assert not pidfile.exists()


# ---- #394: independent of the PID-file bookkeeping above -- an issue labeled status:ai-completed
# whose pull request carries no review and no one-shot role is alive on it right now. This exists
# for the run `dead_role_runs` cannot see at all: PR #391's own validator (2026-09-16) left no PID
# file, no runs.tsv row and no event, so there was nothing on disk for that scan to find dead. Here
# the mechanism reads the OUTCOME instead -- a review that was owed and never happened. --------


def _gh_reads(monkeypatch, *, ai_completed=(), pull_requests=()):
    """Stubs the two real `gh` calls `unreviewed_completions` makes, in the shape `_gh_issue_list`
    and `_gh_pr_list` already return them."""
    monkeypatch.setattr(
        agent_guard,
        "_gh_issue_list",
        lambda fields, *, main, extra=None, state="open": [{"number": n} for n in ai_completed],
    )
    monkeypatch.setattr(agent_guard, "_gh_pr_list", lambda fields, *, main: list(pull_requests))


def test_role_run_is_alive_reads_the_pid_files_own_liveness_and_subject(tmp_path):
    directory = agent_guard.role_run_dir("validator", tmp_path)
    live = _role_run_on_disk(directory, "20260917T090000Z", "409")
    live.write_text(f"{os.getpid()}\n")  # this test's own process: alive by construction

    assert agent_guard.role_run_is_alive("409", main=tmp_path) is True
    assert agent_guard.role_run_is_alive("410", main=tmp_path) is False  # alive, wrong subject

    _role_run_on_disk(directory, "20260916T230556Z", "399")  # a dead one beside it
    assert agent_guard.role_run_is_alive("399", main=tmp_path) is False

    assert agent_guard.role_run_is_alive("409", main=tmp_path / "elsewhere") is False


def test_unreviewed_completions_finds_an_ai_completed_issue_with_no_review_and_no_live_role(
    monkeypatch, tmp_path
):
    _gh_reads(
        monkeypatch,
        ai_completed=[394],
        pull_requests=[{"number": 409, "body": "Closes #394\n\nmore text", "reviews": []}],
    )
    assert agent_guard.unreviewed_completions(main=tmp_path) == [
        agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator")
    ]


def test_unreviewed_completions_skips_a_pull_request_that_already_has_a_review(
    monkeypatch, tmp_path
):
    _gh_reads(
        monkeypatch,
        ai_completed=[394],
        pull_requests=[{"number": 409, "body": "Closes #394", "reviews": [{"state": "APPROVED"}]}],
    )
    assert agent_guard.unreviewed_completions(main=tmp_path) == []


def test_unreviewed_completions_skips_an_issue_the_pull_request_does_not_close(
    monkeypatch, tmp_path
):
    _gh_reads(
        monkeypatch,
        ai_completed=[51],  # #394 itself is not labeled ai-completed here
        pull_requests=[{"number": 409, "body": "Closes #394", "reviews": []}],
    )
    assert agent_guard.unreviewed_completions(main=tmp_path) == []


def test_unreviewed_completions_skips_a_pull_request_with_no_closes_line(monkeypatch, tmp_path):
    _gh_reads(
        monkeypatch,
        ai_completed=[394],
        pull_requests=[{"number": 409, "body": "see #394", "reviews": []}],
    )
    assert agent_guard.unreviewed_completions(main=tmp_path) == []


def test_unreviewed_completions_skips_a_pull_request_a_validator_is_alive_on(monkeypatch, tmp_path):
    directory = agent_guard.role_run_dir("validator", tmp_path)
    live = _role_run_on_disk(directory, "20260917T090000Z", "409")
    live.write_text(f"{os.getpid()}\n")
    _gh_reads(
        monkeypatch,
        ai_completed=[394],
        pull_requests=[{"number": 409, "body": "Closes #394", "reviews": []}],
    )
    assert agent_guard.unreviewed_completions(main=tmp_path) == []


def test_unreviewed_completions_sorts_by_pull_request_number(monkeypatch, tmp_path):
    _gh_reads(
        monkeypatch,
        ai_completed=[10, 20],
        pull_requests=[
            {"number": 500, "body": "Closes #20", "reviews": []},
            {"number": 400, "body": "Closes #10", "reviews": []},
        ],
    )
    found = agent_guard.unreviewed_completions(main=tmp_path)
    assert [item.pull_request for item in found] == [400, 500]


def test_unreviewed_completion_lines_names_the_issue_the_pull_request_and_the_role():
    lines = agent_guard.unreviewed_completion_lines(
        [agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator")]
    )
    assert lines == [
        (
            "issue #394: pull request #409 carries no review and no validator run is alive -- "
            "the review it owed never happened"
        )
    ]


def test_page_unreviewed_completions_names_the_pr_the_issue_and_the_role(monkeypatch, tmp_path):
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    found = [agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator")]

    line = agent_guard._page_unreviewed_completions_if_due(
        found, main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0)
    )

    assert paged == [
        render_human_message("unreviewed_pull_request", pr=409, issue=394, role="validator")
    ]
    assert line is not None and "#409" in line and "#394" in line


def test_page_unreviewed_completions_is_rate_limited_per_pull_request(monkeypatch, tmp_path):
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    found = [agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator")]
    first = _utc(2026, 9, 17, 6, 0, 0)

    agent_guard._page_unreviewed_completions_if_due(found, main=tmp_path, now=first)
    assert len(paged) == 1

    too_soon = agent_guard._page_unreviewed_completions_if_due(
        found, main=tmp_path, now=first + timedelta(minutes=1)
    )
    assert too_soon is None
    assert len(paged) == 1

    minutes = agent_guard.load_planner_config().idle_wake_minutes
    later = agent_guard._page_unreviewed_completions_if_due(
        found, main=tmp_path, now=first + timedelta(minutes=minutes)
    )
    assert later is not None
    assert len(paged) == 2


def test_page_unreviewed_completions_pages_each_pull_request_once(monkeypatch, tmp_path):
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    found = [
        agent_guard.UnreviewedCompletion(issue=51, pull_request=400, role="validator"),
        agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator"),
    ]
    line = agent_guard._page_unreviewed_completions_if_due(
        found, main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0)
    )
    assert len(paged) == 2
    assert line is not None and "#400" in line and "#409" in line


def test_an_unreviewed_page_nobody_can_render_still_marks_it_paged(monkeypatch, tmp_path, capsys):
    _no_messages_configured(monkeypatch)
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
    found = [agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator")]

    line = agent_guard._page_unreviewed_completions_if_due(
        found, main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0)
    )

    assert paged == []
    assert "no page for the unreviewed pull request" in capsys.readouterr().out
    assert line is not None and "#409" in line  # the English journal line still names it
    marker = agent_guard.cache_dir(tmp_path) / "guard" / "paged-unreviewed-409"
    assert marker.is_file()


def test_the_tick_reports_and_pages_an_unreviewed_completion(monkeypatch, tmp_path, capsys):
    found = [agent_guard.UnreviewedCompletion(issue=394, pull_request=409, role="validator")]
    _stub_tick(monkeypatch, results=_idle_results(), alive=False, unreviewed=found)
    paged: list[str] = []
    monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))

    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 6, 0, 0))

    printed = capsys.readouterr().out
    assert "issue #394: pull request #409 carries no review" in printed
    assert paged == [
        render_human_message("unreviewed_pull_request", pr=409, issue=394, role="validator")
    ]
    # No new event kind (`planner_task.sh` already relaunches a validator on every
    # status:ai-completed issue with no review each time it runs at all): the page must never
    # itself wake a planner run.
    assert agent_guard.pending_events(tmp_path) == []


# ---- #413: a merge is an edge (`pr_merged`), and `wake:planner` is the human's own wake
# (`nudged`). On 2026-09-17 PR #411 merged and cleared the dirty worktree the planner had declined
# #404 over, and nothing woke the planner for two hours. -------------------------------------------


def _merged_pr(number: int, merged_at: str, title: str = "a merged change") -> dict:
    return {"number": number, "mergedAt": merged_at, "title": title}


def _merged_edge(monkeypatch, tmp_path, rows, *, dispatchable=(51,), alive=False):
    monkeypatch.setattr(agent_guard, "recently_merged_pull_requests", lambda *, main: list(rows))
    return agent_guard._write_pr_merged_event_if_gained(
        list(dispatchable), any_alive=alive, main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0)
    )


def _seed_merged_seen(tmp_path, stamp: str = "2026-09-17T08:00:00Z") -> None:
    agent_guard.write_merged_seen(datetime.fromisoformat(stamp), main=tmp_path)


def test_pr_merged_first_tick_records_the_newest_merge_and_writes_nothing(monkeypatch, tmp_path):
    outcome = _merged_edge(
        monkeypatch,
        tmp_path,
        [_merged_pr(409, "2026-09-17T07:00:00Z"), _merged_pr(411, "2026-09-17T09:00:00Z")],
    )
    assert outcome.written == 0 and "already-seen" in outcome.line
    assert _events_of_kind(tmp_path, "pr_merged") == []
    assert agent_guard.read_merged_seen(main=tmp_path) == _utc(2026, 9, 17, 9, 0, 0)


def test_pr_merged_writes_one_event_when_nothing_runs_and_something_is_dispatchable(
    monkeypatch, tmp_path
):
    _seed_merged_seen(tmp_path)
    outcome = _merged_edge(
        monkeypatch,
        tmp_path,
        [
            _merged_pr(411, "2026-09-17T09:30:00Z", "fix: diary never committed"),
            _merged_pr(410, "2026-09-17T09:00:00Z", "fix: dead role run"),
            _merged_pr(409, "2026-09-17T07:00:00Z"),  # already seen
        ],
        dispatchable=(404, 402),
    )
    assert outcome.written == 1
    events = _events_of_kind(tmp_path, "pr_merged")
    assert len(events) == 1 and events[0].name.endswith("-pr_merged-411")
    assert events[0].read_text().strip() == (
        "2 pull request(s) merged since the last tick: #411 fix: diary never committed, "
        "#410 fix: dead role run; nothing is running and 2 issue(s) are dispatchable: #404, #402"
    )
    assert agent_guard.read_merged_seen(main=tmp_path) == _utc(2026, 9, 17, 9, 30, 0)


def test_pr_merged_writes_no_event_while_a_worker_is_alive_but_advances_the_state(
    monkeypatch, tmp_path
):
    _seed_merged_seen(tmp_path)
    outcome = _merged_edge(
        monkeypatch, tmp_path, [_merged_pr(411, "2026-09-17T09:30:00Z")], alive=True
    )
    assert outcome.written == 0 and "worker is running" in outcome.line
    assert _events_of_kind(tmp_path, "pr_merged") == []
    assert agent_guard.read_merged_seen(main=tmp_path) == _utc(2026, 9, 17, 9, 30, 0)


def test_pr_merged_writes_no_event_when_nothing_is_dispatchable_but_advances_the_state(
    monkeypatch, tmp_path
):
    _seed_merged_seen(tmp_path)
    outcome = _merged_edge(
        monkeypatch, tmp_path, [_merged_pr(411, "2026-09-17T09:30:00Z")], dispatchable=()
    )
    assert outcome.written == 0 and "nothing is dispatchable" in outcome.line
    assert _events_of_kind(tmp_path, "pr_merged") == []
    assert agent_guard.read_merged_seen(main=tmp_path) == _utc(2026, 9, 17, 9, 30, 0)


def test_pr_merged_leaves_the_state_untouched_when_gh_fails(monkeypatch, tmp_path):
    _seed_merged_seen(tmp_path)
    before = agent_guard.merged_seen_path(tmp_path).read_text()
    # The real listing, with `gh` failing: it must read as "nothing learnt", not "nothing merged".
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    outcome = agent_guard._write_pr_merged_event_if_gained(
        [51], any_alive=False, main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0)
    )
    assert outcome.written == 0 and outcome.line is None
    assert agent_guard.merged_seen_path(tmp_path).read_text() == before
    assert _events_of_kind(tmp_path, "pr_merged") == []


def test_pr_merged_lists_merged_pull_requests_through_gh_pr_list(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return _fake_completed(cmd, [_merged_pr(411, "2026-09-17T09:30:00Z")])

    monkeypatch.setattr(agent_guard.subprocess, "run", fake_run)
    rows = agent_guard.recently_merged_pull_requests(main=tmp_path)
    assert rows == [_merged_pr(411, "2026-09-17T09:30:00Z")]
    assert seen[0][:3] == ["gh", "pr", "list"]
    assert seen[0][seen[0].index("--state") + 1] == "merged"
    assert seen[0][seen[0].index("--json") + 1] == "number,mergedAt,title"


def test_pr_merged_reads_a_corrupt_state_file_as_a_first_tick(monkeypatch, tmp_path):
    path = agent_guard.merged_seen_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for torn in (
        "not json at all",
        '["2026-09-17T08:00:00Z"]',
        '"yesterday"',
        '"2026-09-17T08:00"',
    ):
        path.write_text(torn)
        assert agent_guard.read_merged_seen(main=tmp_path) is None
    outcome = _merged_edge(monkeypatch, tmp_path, [_merged_pr(411, "2026-09-17T09:30:00Z")])
    assert outcome.written == 0 and "already-seen" in outcome.line
    assert _events_of_kind(tmp_path, "pr_merged") == []
    assert agent_guard.read_merged_seen(main=tmp_path) == _utc(2026, 9, 17, 9, 30, 0)


def test_the_tick_writes_pr_merged_and_no_idle_event_in_the_same_tick(
    monkeypatch, tmp_path, invocations
):
    _stub_tick(
        monkeypatch,
        results=_idle_results(),
        alive=False,
        dispatchable=[404],
        merged=[_merged_pr(411, "2026-09-17T09:30:00Z")],
    )
    _seed_merged_seen(tmp_path)
    agent_guard.write_seen_dispatchable([404], main=tmp_path)  # #404 is not NEW, only waiting

    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0))

    assert len(invocations) == 1
    assert "#411" in invocations[0] and "#404" in invocations[0]
    assert agent_guard.latest_event_at("pr_merged", main=tmp_path) is not None
    # One wake reason is enough, and the idle clock is not spent on a wake the merge caused.
    assert agent_guard.latest_event_at("idle_dispatchable", main=tmp_path) is None


def _wake_label_timeline(actor: str | None) -> list[dict]:
    return [
        {
            "event": "labeled",
            "label": {"name": agent_guard.WAKE_PLANNER_LABEL},
            "created_at": "2026-09-17T09:55:00Z",
            "actor": {"login": actor} if actor is not None else None,
        }
    ]


def _nudge(monkeypatch, tmp_path, *, timeline) -> tuple[list[agent_guard.EventOutcome], list]:
    monkeypatch.setattr(agent_guard, "wake_labeled_issues", lambda *, main: ["404"])
    monkeypatch.setattr(agent_guard, "_gh_api_json", lambda path, *, main: timeline)
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_guard,
        "_remove_label",
        lambda issue, label, *, main: removed.append((issue, label)),
    )
    outcomes = agent_guard._write_nudged_events(main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0))
    return outcomes, removed


def test_a_wake_label_set_by_the_human_is_cleared_and_writes_one_nudged_event(
    monkeypatch, tmp_path
):
    outcomes, removed = _nudge(monkeypatch, tmp_path, timeline=_wake_label_timeline(_HUMAN))
    assert removed == [("404", agent_guard.WAKE_PLANNER_LABEL)]
    assert sum(outcome.written for outcome in outcomes) == 1
    events = _events_of_kind(tmp_path, "nudged")
    assert len(events) == 1 and events[0].name.endswith("-nudged-404")
    assert events[0].read_text().strip() == (
        f"issue #404 carried {agent_guard.WAKE_PLANNER_LABEL}, set by {_HUMAN} at "
        "2026-09-17T09:55:00Z -- label cleared; the latest human comment on #404 says why"
    )


def test_a_wake_label_set_by_the_mechanism_is_cleared_and_ignored(monkeypatch, tmp_path):
    # The planner must not be able to label its way into its own next run.
    outcomes, removed = _nudge(monkeypatch, tmp_path, timeline=_wake_label_timeline(_BOT))
    assert removed == [("404", agent_guard.WAKE_PLANNER_LABEL)]
    assert [outcome.written for outcome in outcomes] == [0]
    assert "not the human" in outcomes[0].line
    assert _events_of_kind(tmp_path, "nudged") == []


def test_a_wake_label_nobody_is_named_for_is_cleared_and_ignored(monkeypatch, tmp_path):
    outcomes, removed = _nudge(monkeypatch, tmp_path, timeline=_wake_label_timeline(None))
    assert removed == [("404", agent_guard.WAKE_PLANNER_LABEL)]
    assert [outcome.written for outcome in outcomes] == [0]
    assert _events_of_kind(tmp_path, "nudged") == []


def test_a_wake_label_whose_timeline_cannot_be_read_is_left_for_the_next_tick(
    monkeypatch, tmp_path
):
    outcomes, removed = _nudge(monkeypatch, tmp_path, timeline=None)
    assert removed == []
    assert [outcome.written for outcome in outcomes] == [0]
    assert _events_of_kind(tmp_path, "nudged") == []


def test_a_failed_wake_label_listing_does_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_guard.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )

    def never(*args, **kwargs):
        raise AssertionError("nothing may be read or removed after a failed listing")

    monkeypatch.setattr(agent_guard, "_gh_api_json", never)
    monkeypatch.setattr(agent_guard, "_remove_label", never)
    assert agent_guard.wake_labeled_issues(main=tmp_path) == []
    assert agent_guard._write_nudged_events(main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0)) == []
    assert _events_of_kind(tmp_path, "nudged") == []


def test_the_tick_turns_a_human_wake_label_into_a_planner_run(monkeypatch, tmp_path, invocations):
    _stub_tick(monkeypatch, results=_idle_results(), alive=True, wake_labeled=["404"])
    monkeypatch.setattr(
        agent_guard, "_gh_api_json", lambda path, *, main: _wake_label_timeline(_HUMAN)
    )
    monkeypatch.setattr(agent_guard, "_remove_label", lambda issue, label, *, main: None)
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0))
    assert len(invocations) == 1 and "carried wake:planner" in invocations[0]


def test_a_paused_mechanism_leaves_the_wake_label_in_place(monkeypatch, tmp_path, invocations):
    _stub_tick(monkeypatch, results=_idle_results(), paused=True, wake_labeled=["404"])
    removed = []
    monkeypatch.setattr(
        agent_guard, "_remove_label", lambda issue, label, *, main: removed.append(issue)
    )
    agent_guard.tick(main=tmp_path, now=_utc(2026, 9, 17, 10, 0, 0))
    assert removed == [] and invocations == []


def test_the_remove_label_helper_still_clears_blocked_on_human(monkeypatch, tmp_path):
    removed = []
    monkeypatch.setattr(
        agent_guard,
        "_remove_label",
        lambda issue, label, *, main: removed.append((issue, label)),
    )
    agent_guard._clear_blocked_label("77", main=tmp_path)
    assert removed == [("77", agent_guard.BLOCKED_ON_HUMAN_LABEL)]
