"""The guard's quota page and the role launch gate read ONE declaration (#425).

The guard still cuts a Claude run whose own stream says the quota is rejected -- that is unchanged,
and it is the backend's signal, never an agent's claim
(agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md). What
changed is the page that follows the cut: `quota_exhausted_no_fallback` exists for the case where
nothing can proceed without a human, and a class that declares a way round the exhausted window --
`qwen_fallback_eligible`, the planner's redispatch of a worker, or `fallback:`, a role's own launch
gate -- is not that case. Both routes go through `TaskClass.allows_backend_fallback`, so the cut and
the launch cannot disagree about what was available.

These live in their own module rather than in `test_agent_guard.py` because that file is the
guard's own and another issue (#426) is editing it in the same week; the harness below is the same
shape its `_rate_limited_claude_run` uses, written out here so neither branch has to import the
other's.

Pure filesystem and subprocess. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agent_os import guard as agent_guard
from agent_os.lib import (
    RoleFallback,
    TaskClass,
    load_task_classes,
    render_human_message,
    role_launch_plan,
)

# A body with every required section and a resolvable budget class: `_tick_backend` reads the class
# name off the issue body, so the stub `gh` has to answer with one.
VALID_BODY = """## Objective

Do the thing.

## Acceptance criteria

- One criterion.

## Stages

- [ ] Stage 1 - do the thing.

## Context

None.

## Not included

Nothing.

## Dependencies

none

## Definition of done

Tests pass.

<!-- budget: mechanical-claude -->"""

VERDICT_TTL_SECONDS = 60 * 60


def _assistant_event(timestamp: str) -> dict:
    return {
        "type": "assistant",
        "session_id": "s1",
        "timestamp": timestamp,
        "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }


def _rate_limit_event(status: str) -> dict:
    return {
        "type": "rate_limit_event",
        "session_id": "s1",
        "rate_limit_info": {
            "status": status,
            "unifiedWindows": {"five_hour": {"utilization": 1, "resetsAt": "2026-09-18T10:30:00Z"}},
        },
    }


def _claude_worker_class(**overrides) -> TaskClass:
    """A Claude worker class, the only kind the guard's quota cut can reach: the tick cuts
    `reason=quota` for `backend == "claude"` alone, and no worker class in the real config is on
    Claude today (agent_os/docs/adr/2026-09-16-workers-run-on-qwen-and-claude-only-reviews.md)."""
    fields: dict = {
        "backend": "claude",
        "model": "claude-opus-5",
        "max_context": 400000,
        "max_cost_usd": 5.0,
        "max_total_tokens": 80000000,
        "commit_warn_turns": 30,
        "commit_cut_turns": 50,
    }
    fields.update(overrides)
    return TaskClass(**fields)


def _qwen_fallback() -> RoleFallback:
    return RoleFallback(
        backend="qwen", model="qwen3.8-max", ceilings=["max_context", "max_total_tokens"]
    )


@pytest.fixture
def rate_limited_claude_run(tmp_path, monkeypatch):
    """A live `claude` worker whose own event stream says the quota is rejected, with every write
    moved under `tmp_path` and every call that would reach the tracker or the telephone stubbed.
    Returns the list the pages land in."""

    def arrange(task_class: TaskClass) -> list[str]:
        cache = tmp_path / ".cache"
        worktree = tmp_path / "worker-worktree"
        cache.mkdir(parents=True, exist_ok=True)
        worktree.mkdir(exist_ok=True)
        monkeypatch.setenv("WORKER_CACHE_DIR", str(cache))
        monkeypatch.setenv("WORKER_WORKTREE", str(worktree))
        (cache / "worker_claude.state").write_text("STARTED\n")
        (cache / "worker_claude.issue").write_text("366\n")
        (cache / "worker_claude.jsonl").write_text(
            json.dumps(_assistant_event("2026-09-18T10:00:00Z"))
            + "\n"
            + json.dumps(_rate_limit_event("rejected"))
            + "\n"
        )
        monkeypatch.setattr(
            agent_guard.subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=VALID_BODY, stderr=""),
        )
        monkeypatch.setattr(agent_guard, "_is_alive", lambda pidfile: True)
        monkeypatch.setattr(agent_guard, "_commit_timestamps", lambda tree, startref: [])
        monkeypatch.setattr(
            agent_guard, "load_task_classes", lambda: {"mechanical-claude": task_class}
        )
        monkeypatch.setattr(agent_guard, "cut_run", lambda backend, reason, **kwargs: None)
        paged: list[str] = []
        monkeypatch.setattr(agent_guard, "notify", lambda message, *, main: paged.append(message))
        return paged

    return arrange


def test_the_guard_still_cuts_on_the_backends_own_signal_whatever_the_class_declares(
    tmp_path, rate_limited_claude_run
):
    # The cut is the part that must not move: it is what freezes the work and what writes the
    # state the planner reads, and it happens on the stream's own `rate_limit_event` in both cases.
    for task_class in (
        _claude_worker_class(),
        _claude_worker_class(fallback=_qwen_fallback()),
        _claude_worker_class(qwen_fallback_eligible=True),
    ):
        rate_limited_claude_run(task_class)
        result = agent_guard._tick_backend("claude", main=tmp_path)
        assert result.cut_reason == "quota", task_class


def test_the_quota_page_is_silent_for_a_class_that_declares_a_fallback_backend(
    tmp_path, rate_limited_claude_run
):
    # #425: a class with a way round the exhausted window is not "nothing can proceed without a
    # human", so the page that says exactly that must not go out for it.
    paged = rate_limited_claude_run(_claude_worker_class(fallback=_qwen_fallback()))
    assert agent_guard._tick_backend("claude", main=tmp_path).cut_reason == "quota"
    assert paged == []


def test_the_quota_page_still_fires_for_a_class_with_no_way_round_it(
    tmp_path, rate_limited_claude_run
):
    paged = rate_limited_claude_run(_claude_worker_class())
    assert agent_guard._tick_backend("claude", main=tmp_path).cut_reason == "quota"
    assert paged == [
        render_human_message(
            "quota_exhausted_no_fallback",
            issue="366",
            backend="claude",
            task_class="mechanical-claude",
        )
    ]


def test_the_planners_own_redispatch_route_still_suppresses_the_page(
    tmp_path, rate_limited_claude_run
):
    # The route that existed before #425, unchanged: `qwen_fallback_eligible` is the planner's
    # redispatch of a WORKER, and it suppresses the page exactly as it did.
    paged = rate_limited_claude_run(_claude_worker_class(qwen_fallback_eligible=True))
    assert agent_guard._tick_backend("claude", main=tmp_path).cut_reason == "quota"
    assert paged == []


def test_every_real_class_pages_and_launches_on_the_same_reading_of_its_declaration():
    """The two consumers of one field, compared over the real config: for every class, "the guard
    would page a human about this one" and "the launch gate has nowhere else to put it" have to be
    the same statement. A class that pages while its role silently runs elsewhere is a page nobody
    can act on; a class that substitutes while the guard pages is a human woken for nothing."""
    for name, task_class in load_task_classes().items():
        plan = role_launch_plan(
            task_class, "exhausted", 60.0, verdict_ttl_seconds=VERDICT_TTL_SECONDS
        )
        assert plan.substituted == (task_class.fallback is not None), name
        # `qwen_fallback_eligible` suppresses the page without being a launch gate: it is the
        # planner's redispatch of a worker, which is why the predicate is the OR of the two.
        assert task_class.allows_backend_fallback == (
            plan.substituted or task_class.qwen_fallback_eligible
        ), name
