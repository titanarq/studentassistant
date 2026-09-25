"""`config/agents.yaml` loading, the `<!-- budget: --> ` line, and the jsonl usage/quota parsers
shared with `worker_task.sh`'s `usage_report()` (`agent_os.lib`).

Pure filesystem. This file must not request the `engine` or the `db_sandbox` fixture: nothing here
touches the shared database.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest
from conftest import EXAMPLE_CONFIG
from pydantic import ValidationError

from agent_os import lib as agent_lib
from agent_os.cli import host_root
from agent_os.lib import (
    REQUIRED_SECTIONS,
    STATES,
    HumanMessageError,
    LabelVocabulary,
    MechanismConfig,
    PlannerConfig,
    ProjectConfig,
    RoleFallback,
    TaskClass,
    blocking_issue_numbers,
    cumulative_cost_usd,
    cumulative_total_tokens,
    forbidden_paths_merge_audit_regex,
    forbidden_paths_merge_audit_violations,
    forbidden_paths_regex,
    forbidden_paths_rules,
    human_message_rules,
    is_dispatchable,
    load_mechanism,
    load_planner_config,
    load_project,
    load_task_classes,
    mechanism_paths_regex,
    mechanism_paths_rules,
    needs_refinement,
    parse_budget_line,
    parse_stages,
    planner_run_row,
    promotable_to_ready,
    quota_exhausted,
    quota_verdict_file,
    read_events,
    read_persisted_quota_verdict,
    render_human_message,
    result_total_tokens,
    role_launch,
    role_launch_plan,
    section_failures,
    stages_completed,
    turn_context_tokens,
    usage_failed,
    usage_summary,
    validate_issue_body,
    worker_environment_rules,
    worktree_path,
)

# The HOST project this suite runs inside: not a fixed nesting under AGENT_OS_DIR (that
# breaks the moment a copy IS the mechanism's own top directory, as the out-of-tree proof
# makes it -- #512), but whatever `host_root()` itself resolves: the git checkout's toplevel,
# same as every real driver run.
ROOT = host_root()


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text)
    return path


# The `project:` section is required (it is what makes the mechanism project-agnostic), so every
# fixture file carries a minimal one. It stays FIRST: the tests below append to this string to
# build an invalid file, and what they append belongs to the last block.
VALID_YAML = """
project:
  repo: someone/something
  tracking_epic: 1
  board_number: 1

classes:
  mechanical-qwen:
    backend: qwen
    model: qwen3.8-max
    max_context: 400000
    max_cost_usd: 5.0
    max_total_tokens: 80000000
    commit_warn_turns: 15
    commit_cut_turns: 25
"""


def test_load_task_classes_reads_real_config():
    # The real file this issue ships, not a copy -- if it stops parsing, the driver refuses every
    # dispatch, so the test that matters most is against the actual file.
    classes = load_task_classes()
    assert "complex-qwen" in classes
    assert classes["complex-qwen"].backend == "qwen"
    assert classes["complex-qwen"].qwen_fallback_eligible is False


def test_every_class_in_the_real_config_carries_a_token_ceiling():
    # #387: Qwen's terminal `result` event reports no `total_cost_usd`, so `max_cost_usd` alone
    # leaves a worker class with no live spend backstop at all. The values themselves are
    # placeholders -- #363 summed 49,526,715 tokens over ten stage logs, and #342 calibrates them
    # -- so what is asserted here is that every class carries a ceiling that can be crossed, not
    # any particular number.
    classes = load_task_classes()
    assert classes, "the real config must declare at least one class"
    assert all(task_class.max_total_tokens > 0 for task_class in classes.values()), (
        f"classes with no token ceiling to cross: "
        f"{[n for n, c in classes.items() if c.max_total_tokens <= 0]}"
    )


def test_load_task_classes_rejects_a_class_with_no_token_ceiling(tmp_path):
    # Required, not defaulted: a class that forgets the field must fail to load. A silent default
    # would be worse than either extreme -- a ceiling nothing can cross, or one everything crosses.
    without_ceiling = "\n".join(
        line for line in VALID_YAML.splitlines() if not line.strip().startswith("max_total_tokens")
    )
    assert "max_total_tokens" not in without_ceiling, "the fixture lost the line this test removes"
    path = _write(tmp_path / "agents.yaml", without_ceiling)
    with pytest.raises(ValidationError):
        load_task_classes(path)


def test_every_worker_class_runs_on_qwen():
    # agent_os/docs/adr/2026-09-16-workers-run-on-qwen-and-claude-only-reviews.md: a class with no `role:`
    # line is a worker budget (`role` defaults to "worker"), and no worker budget may name the
    # Claude backend -- Claude's window is spent on the review and planning roles instead.
    worker_classes = {
        name: task_class
        for name, task_class in load_task_classes().items()
        if task_class.role == "worker"
    }
    assert worker_classes, "the real config must declare at least one worker class"
    assert all(task_class.backend == "qwen" for task_class in worker_classes.values()), (
        f"worker classes on a non-qwen backend: "
        f"{[n for n, c in worker_classes.items() if c.backend != 'qwen']}"
    )


def test_load_task_classes_accepts_minimal_valid_file(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    classes = load_task_classes(path)
    assert classes == {
        "mechanical-qwen": TaskClass(
            backend="qwen",
            model="qwen3.8-max",
            max_context=400000,
            max_cost_usd=5.0,
            max_total_tokens=80000000,
            commit_warn_turns=15,
            commit_cut_turns=25,
        )
    }


def test_load_task_classes_rejects_unknown_top_level_section(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML + "\nunknown_section: {}\n")
    with pytest.raises(ValidationError):
        load_task_classes(path)


def test_load_task_classes_rejects_unknown_field_inside_a_class(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML + "    extra_field: 1\n")
    with pytest.raises(ValidationError):
        load_task_classes(path)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("some text\n<!-- budget: mechanical-qwen -->", "mechanical-qwen"),
        ("<!-- budget: complex-qwen -->\ntrailing text", "complex-qwen"),
        ("no budget line here", None),
        ("<!-- Budget: mechanical-qwen -->", None),  # case-sensitive, matches issues.py's key line
    ],
)
def test_parse_budget_line(body, expected):
    assert parse_budget_line(body) == expected


def _assistant_turn(
    session_id: str, input_tokens: int, cache_read: int = 0, timestamp: str | None = None
) -> str:
    """One line exactly as Claude Code's stream-json writes an assistant turn with usage."""
    event = {
        "type": "assistant",
        "session_id": session_id,
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": 0,
                "output_tokens": 50,
            }
        },
    }
    if timestamp:
        event["timestamp"] = timestamp
    return json.dumps(event)


def _result_line(
    session_id: str,
    is_error: bool = False,
    api_error_status: int | None = None,
    turns: int = 1,
    cost: float | None = 1.5,
    usage: dict | None = None,
) -> str:
    event = {
        "type": "result",
        "session_id": session_id,
        "subtype": "success",
        "is_error": is_error,
        "num_turns": turns,
        "duration_ms": 1000,
        "usage": usage
        or {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
        },
        "result": "" if not is_error else "[API Error: rate limited]",
    }
    # `cost=None` leaves the key out entirely, which is the shape Qwen writes and the reason #387
    # exists: its terminal `result` reports no `total_cost_usd`, not a null one.
    if cost is not None:
        event["total_cost_usd"] = cost
    if api_error_status is not None:
        event["api_error_status"] = api_error_status
    return json.dumps(event)


def _rate_limit_line(session_id: str, status: str, window: str = "five_hour") -> str:
    return json.dumps(
        {
            "type": "rate_limit_event",
            "session_id": session_id,
            "rate_limit_info": {"status": status, "unifiedWindows": {window: {"utilization": 1.0}}},
        }
    )


def test_turn_context_tokens_sums_input_and_both_cache_fields():
    # A warm turn reports input_tokens: 2, cache_read_input_tokens: 18919 -- reading input_tokens
    # alone would call a 19k-token turn a 2-token one.
    usage = {
        "input_tokens": 2,
        "cache_read_input_tokens": 18919,
        "cache_creation_input_tokens": 100,
    }
    assert turn_context_tokens(usage) == 19021


def test_read_events_skips_malformed_lines(tmp_path):
    path = _write(
        tmp_path / "events.jsonl", _assistant_turn("s1", 100) + "\nnot json\n" + _result_line("s1")
    )
    events = read_events(path)
    assert len(events) == 2


def test_usage_summary_takes_the_max_turn_context_not_the_sum(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100, cache_read=0),
                _assistant_turn("s1", 2, cache_read=18919),
                _result_line("s1", turns=2),
            ]
        ),
    )
    summary = usage_summary(read_events(path))
    assert summary.session_id == "s1"
    assert summary.turns == 2
    assert summary.context == 18921
    assert summary.result is not None


def test_usage_failed_true_when_no_turns_happened(tmp_path):
    path = _write(tmp_path / "events.jsonl", _result_line("s1", turns=0))
    # No assistant turn was ever recorded, whatever the result says.
    summary = usage_summary(read_events(path))
    assert usage_failed(summary) is True


def test_usage_failed_true_on_is_error_despite_success_subtype(tmp_path):
    # subtype stays "success" even when the API refused the request -- the refusal is the RESULT
    # TEXT, not the subtype (verified against .cache/worker_claude.jsonl, 2026-09-14).
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line("s1", is_error=True, api_error_status=429),
            ]
        ),
    )
    summary = usage_summary(read_events(path))
    assert usage_failed(summary) is True


def test_usage_failed_false_on_a_clean_run(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line("s1"),
            ]
        ),
    )
    summary = usage_summary(read_events(path))
    assert usage_failed(summary) is False


# ---- cumulative_cost_usd: max_cost_usd is checked against the whole issue, not one stage -------


def test_cumulative_cost_usd_sums_the_result_event_of_each_file(tmp_path):
    first = _write(
        tmp_path / "stage1.jsonl",
        "\n".join([_assistant_turn("s1", 100), _result_line("s1", cost=1.5)]),
    )
    second = _write(
        tmp_path / "stage2.jsonl",
        "\n".join([_assistant_turn("s2", 100), _result_line("s2", cost=2.25)]),
    )
    assert cumulative_cost_usd([first, second]) == pytest.approx(3.75)


def test_cumulative_cost_usd_treats_a_file_with_no_result_as_zero(tmp_path):
    # A stage killed mid-run archives a `.jsonl` with no terminal `result` -- it must still count
    # toward the sum as 0, not raise or be skipped silently in a way that hides the file entirely.
    unfinished = _write(tmp_path / "cut.jsonl", _assistant_turn("s1", 100))
    finished = _write(
        tmp_path / "done.jsonl",
        "\n".join([_assistant_turn("s2", 100), _result_line("s2", cost=4.0)]),
    )
    assert cumulative_cost_usd([unfinished, finished]) == pytest.approx(4.0)


def test_cumulative_cost_usd_zero_for_no_files():
    assert cumulative_cost_usd([]) == 0.0


# ---- cumulative_total_tokens: the one ceiling a Qwen class can actually cross (#387) -----------


def _qwen_result_line(session_id: str, total_tokens: int) -> str:
    """The terminal `result` event as Qwen writes it, key for key against `.cache/spend/363/`:
    `usage.total_tokens` present, `cache_read_input_tokens` a part of `input_tokens` and not an
    addition to it, and no `total_cost_usd` anywhere in the event. The only invariant the fixture
    keeps is Qwen's own, `total_tokens == input_tokens + output_tokens`."""
    output_tokens = max(total_tokens // 100, 1)
    return _result_line(
        session_id,
        cost=None,
        usage={
            "input_tokens": total_tokens - output_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": total_tokens // 2,
            "total_tokens": total_tokens,
        },
    )


def test_cumulative_total_tokens_sums_the_result_event_of_each_file(tmp_path):
    first = _write(
        tmp_path / "stage1.jsonl",
        "\n".join([_assistant_turn("s1", 100), _qwen_result_line("s1", 1_000)]),
    )
    second = _write(
        tmp_path / "stage2.jsonl",
        "\n".join([_assistant_turn("s2", 100), _qwen_result_line("s2", 2_500)]),
    )
    assert cumulative_total_tokens([first, second]) == 3_500


def test_cumulative_total_tokens_reads_the_recorded_qwen_stage_the_dollar_sum_cannot(tmp_path):
    # The first archived stage log of #363 with its own figures: 30,412,334 tokens spent, and a
    # dollar sum of exactly 0.0 because the event that would carry one has no such key. This is
    # the pair of numbers that made `max_cost_usd` inert on every worker class.
    stage = _write(
        tmp_path / "20260916T082736Z-qwen-stage0.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line(
                    "s1",
                    cost=None,
                    usage={
                        "input_tokens": 30_215_389,
                        "output_tokens": 196_945,
                        "cache_read_input_tokens": 29_875_819,
                        "total_tokens": 30_412_334,
                    },
                ),
            ]
        ),
    )
    assert cumulative_total_tokens([stage]) == 30_412_334
    assert cumulative_cost_usd([stage]) == 0.0


def test_cumulative_total_tokens_falls_back_when_usage_reports_no_total_tokens(tmp_path):
    stage = _write(
        tmp_path / "stage1.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line(
                    "s1",
                    usage={
                        "input_tokens": 10,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 5,
                        "output_tokens": 7,
                    },
                ),
            ]
        ),
    )
    # 10 + 40 + 5 + 7, the four counters `usage-report` prints as `total N tokens`. Asserted
    # through both paths so the figure one stage's report shows and the figure the issue-wide
    # ceiling is measured with cannot drift apart.
    assert cumulative_total_tokens([stage]) == 62
    assert result_total_tokens(usage_summary(read_events(stage)).result) == 62


def test_cumulative_total_tokens_treats_a_file_with_no_result_as_zero(tmp_path):
    # The undercount the docstring states rather than hides: a stage cut before it finished
    # archives a log whose tokens nothing here reads, so the issue's total is short by that stage.
    unfinished = _write(tmp_path / "cut.jsonl", _assistant_turn("s1", 100))
    finished = _write(
        tmp_path / "done.jsonl",
        "\n".join([_assistant_turn("s2", 100), _qwen_result_line("s2", 4_000)]),
    )
    assert cumulative_total_tokens([unfinished, finished]) == 4_000


def test_cumulative_total_tokens_zero_for_no_files():
    assert cumulative_total_tokens([]) == 0


def _run_cli(monkeypatch, capsys, argv: list[str], stdin: str = "") -> str:
    """`agent_lib.main()` the way the shell drivers call it: argv and stdin patched, stdout read
    back. What these tests assert is the bytes `worker_task.sh` will parse, not the function
    behind them -- a subcommand registered under one name and dispatched under another passes
    every function-level test above and prints nothing at all."""
    monkeypatch.setattr(sys, "argv", ["agent_lib.py", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    exit_code = 0
    try:
        agent_lib.main()
    except SystemExit as exited:  # `resolve-budget` exits with its own status
        exit_code = int(exited.code or 0)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    return captured.out


def test_the_cumulative_tokens_cli_prints_the_sum_as_a_plain_integer(tmp_path, monkeypatch, capsys):
    first = _write(
        tmp_path / "stage1.jsonl",
        "\n".join([_assistant_turn("s1", 100), _qwen_result_line("s1", 1_234_567)]),
    )
    second = _write(
        tmp_path / "stage2.jsonl",
        "\n".join([_assistant_turn("s2", 100), _qwen_result_line("s2", 890)]),
    )
    out = _run_cli(monkeypatch, capsys, ["cumulative-tokens", str(first), str(second)])
    # Digits and a newline, nothing else: the driver's stage gate compares this against
    # `max_total_tokens` with bash arithmetic, which reads neither a thousands separator nor a
    # decimal point. `cumulative-cost` prints 4 decimals, so this one cannot borrow its format.
    assert out == "1235457\n"


def test_resolve_budget_field_max_total_tokens_prints_the_class_ceiling(monkeypatch, capsys):
    out = _run_cli(
        monkeypatch,
        capsys,
        ["resolve-budget", "--field", "max_total_tokens"],
        stdin="## Objective\n\nA body.\n\n<!-- budget: mechanical-qwen -->\n",
    )
    assert out.strip().isdigit()
    assert out.strip() == str(load_task_classes()["mechanical-qwen"].max_total_tokens)


def test_the_usage_report_context_line_reads_the_issues_own_budget_class(
    monkeypatch, capsys, tmp_path
):
    """#483: the context ceiling named in the report is the class the issue's own
    `<!-- budget: --> ` line resolves to, never a per-backend default -- a `complex-qwen` issue at
    490,887 tokens (below its own 800,000) must report `ok`, not `OVER BUDGET` against a number
    that belongs to another class."""
    events = _write(tmp_path / "events.jsonl", _assistant_turn("s1", 490_887))
    body = _write(tmp_path / "body.md", "<!-- budget: complex-qwen -->\n")
    out = _run_cli(monkeypatch, capsys, ["usage-report", str(events), str(body)])
    ceiling = load_task_classes()["complex-qwen"].max_context
    assert f"context   490,887 tokens  (budget {ceiling:,}) ok" in out, out
    assert "OVER BUDGET" not in out


def test_the_usage_report_names_its_reason_when_no_budget_class_resolves(
    monkeypatch, capsys, tmp_path
):
    """No issue body recorded for this run, and a body whose `<!-- budget: --> ` line does not
    resolve: the context line says why instead of printing a number that looks like a ceiling, and
    never claims `ok` or `OVER BUDGET` -- there is no ceiling behind either verdict (#483)."""
    events = _write(tmp_path / "events.jsonl", _assistant_turn("s1", 1_000))

    out = _run_cli(monkeypatch, capsys, ["usage-report", str(events)])
    assert (
        "context   1,000 tokens  (budget unresolved -- no issue body recorded for this run)" in out
    ), out

    body = _write(tmp_path / "body.md", "no budget line here\n")
    out = _run_cli(monkeypatch, capsys, ["usage-report", str(events), str(body)])
    assert "budget unresolved -- missing the `<!-- budget: <class> -->` line" in out, out


def test_quota_exhausted_reads_rejected_rate_limit_event(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _rate_limit_line("s1", "allowed"),
                _rate_limit_line("s1", "rejected", window="seven_day"),
            ]
        ),
    )
    reason = quota_exhausted(read_events(path))
    assert reason is not None
    assert "rejected" in reason


def test_quota_exhausted_falls_back_to_result_api_error_status(tmp_path):
    # No rate_limit_event at all -- the run ended on the refused turn directly.
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line("s1", is_error=True, api_error_status=429),
            ]
        ),
    )
    reason = quota_exhausted(read_events(path))
    assert reason is not None
    assert "429" in reason


@pytest.mark.parametrize("api_error_status", [500, 529])
def test_quota_exhausted_none_on_a_non_429_result_error(tmp_path, api_error_status):
    # A transport or server failure (5xx, 529 overloaded) is not a spent quota: no rate_limit_event
    # rejected the run, and the terminal result's api_error_status is not 429 (#530).
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line("s1", is_error=True, api_error_status=api_error_status),
            ]
        ),
    )
    assert quota_exhausted(read_events(path)) is None


def test_quota_exhausted_reads_the_2026_09_18_shape(tmp_path):
    # A rejected rate_limit_event plus a terminal result carrying api_error_status=429 -- the exact
    # shape .cache/planner/20260918T084846Z.log carried (#530).
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _rate_limit_line("s1", "rejected", window="five_hour"),
                _result_line("s1", is_error=True, api_error_status=429),
            ]
        ),
    )
    reason = quota_exhausted(read_events(path))
    assert reason is not None
    assert "rejected" in reason


def test_quota_exhausted_reads_a_rejected_rate_limit_event_whatever_the_result_says(tmp_path):
    # The rate_limit_event branch is authoritative and runs first: even a result carrying a
    # non-429 status (or none at all) does not override a rejected rate limit (#530).
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _rate_limit_line("s1", "rejected", window="five_hour"),
                _result_line("s1", is_error=True, api_error_status=500),
            ]
        ),
    )
    reason = quota_exhausted(read_events(path))
    assert reason is not None
    assert "rejected" in reason


def test_quota_exhausted_none_on_a_clean_run(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _rate_limit_line("s1", "allowed"),
                _result_line("s1"),
            ]
        ),
    )
    assert quota_exhausted(read_events(path)) is None


def test_quota_exhausted_none_for_qwen_shaped_events_with_no_signal(tmp_path):
    # Qwen emits no rate_limit_event and no api_error_status -- absence of the signal is not
    # evidence of exhaustion, it is the documented limit of what Qwen's stream reports.
    path = _write(
        tmp_path / "events.jsonl",
        "\n".join(
            [
                _assistant_turn("s1", 100),
                _result_line("s1"),
            ]
        ),
    )
    assert quota_exhausted(read_events(path)) is None


# ---- project: the section that makes the mechanism project-agnostic --------------------------


def test_load_project_reads_the_real_config():
    # Like the task classes above: the file that actually ships, because every mechanism script
    # reads it at import and a malformed section breaks the guard, not just this test.
    project = load_project()
    assert "/" in project.repo  # owner/name
    assert project.tracking_epic > 0
    assert set(project.worktrees) == {"qwen", "claude"}
    assert project.labels.ready == "status:ready"


def test_load_project_rejects_a_missing_project_section(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML.replace("project:", "not_project:", 1))
    with pytest.raises(ValidationError):
        load_project(path)


def test_worktree_path_resolves_relative_to_the_repository_root(tmp_path):
    project = load_project()
    resolved = worktree_path("qwen", main=tmp_path, project=project)
    assert resolved == (tmp_path / project.worktrees["qwen"]).resolve()


# ---- human_language / human_message_rules: a question for the human is written in their own
# language and in functional terms
# (agent_os/docs/adr/2026-09-15-a-question-for-the-human-is-written-in-their-language-and-in-functional-
# terms.md) -----------------------------------------------------------------------------------


def test_project_config_human_language_defaults_to_english_when_absent(tmp_path):
    # Same tmp-yaml fixture pattern VALID_YAML already uses: no `human_language` key at all.
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    assert load_project(path).human_language == "English"


def test_human_message_rules_contains_the_configured_language():
    project = ProjectConfig(repo="someone/something", tracking_epic=1, board_number=1)
    rules = human_message_rules(project)
    assert "WRITING TO THE HUMAN" in rules
    assert "English" in rules


def test_human_message_rules_uses_the_projects_own_language():
    project = ProjectConfig(
        repo="someone/something", tracking_epic=1, board_number=1, human_language="French"
    )
    rules = human_message_rules(project)
    assert "French" in rules
    assert "English" not in rules


def test_human_message_rules_states_the_parsed_parts_stay_as_specified():
    # Code identifiers/paths/labels/marker/headings that the mechanism or another agent parses
    # must stay in their own spelling -- never translated or reworded into the explanation.
    rules = human_message_rules(ProjectConfig(repo="a/b", tracking_epic=1, board_number=1))
    assert "in its own spelling" in rules
    assert "## Doubts" in rules
    assert "<!-- refiner-summary -->" in rules
    assert "BLOCKED reason=" in rules


def test_human_message_rules_defaults_to_load_project_when_none_given():
    assert human_message_rules() == human_message_rules(load_project())


# ---- render_human_message: every ntfy page is a template in the human's language, never a
# literal in code (#366, `agent_os/docs/AGENT_OS.md` §7 row (g))
# (agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md) --------------


def _messages_project(**overrides) -> ProjectConfig:
    fields = {
        "repo": "someone/something",
        "tracking_epic": 1,
        "board_number": 1,
        "messages": {"review_ready": "Issue #{issue} «{title}» is waiting for you: {url}"},
    }
    return ProjectConfig(**(fields | overrides))


def test_render_human_message_fills_the_template_the_project_wrote():
    rendered = render_human_message(
        "review_ready",
        _messages_project(),
        issue=366,
        title="A title",
        url="https://example.invalid/366",
    )
    assert rendered == "Issue #366 «A title» is waiting for you: https://example.invalid/366"


def test_render_human_message_refuses_a_key_the_project_never_wrote():
    # Loud, because a page nobody receives looks exactly like a situation that never arose.
    with pytest.raises(HumanMessageError) as failure:
        render_human_message("no_such_page", _messages_project(), issue=1)
    assert "no_such_page" in str(failure.value)
    assert "review_ready" in str(failure.value)  # names what IS configured


def test_render_human_message_refuses_a_placeholder_nobody_passed():
    with pytest.raises(HumanMessageError) as failure:
        render_human_message("review_ready", _messages_project(), issue=366)
    assert "title" in str(failure.value)


@pytest.mark.parametrize(
    "template",
    [
        "Issue {} is waiting",  # positional: `format` raises IndexError, not KeyError
        "Issue {issue} costs 5{ euros",  # a stray brace: ValueError
    ],
)
def test_render_human_message_reports_a_malformed_template_as_one_failure_not_three(template):
    """`str.format` raises three different exceptions for three ways of being wrong, and a caller
    that must not die over a config typo would have had to know all three. One type does."""
    project = ProjectConfig.model_construct(messages={"review_ready": template})
    with pytest.raises(HumanMessageError) as failure:
        render_human_message("review_ready", project, issue=366, title="t", url="u")
    assert "review_ready" in str(failure.value)


@pytest.mark.parametrize(
    "template,complaint",
    [
        ("Issue {} is waiting", "positional"),
        ("Issue {0} is waiting", "not a plain name"),
        ("Issue {issue} costs 5{ euros", "not a valid template"),
        ("Issue {issue[0]} is waiting", "not a plain name"),
    ],
)
def test_project_config_refuses_a_malformed_template_when_the_config_loads(template, complaint):
    """The whole point: a literal `{` in a Spanish sentence fails when `config/agents.yaml` is
    read -- on every command, including the tests -- instead of at the one moment something
    needed to page, which is the worst moment to find a typo."""
    with pytest.raises(ValueError, match=complaint):
        _messages_project(messages={"review_ready": template})


def test_render_human_message_defaults_to_the_real_config_when_no_project_is_given():
    assert render_human_message(
        "review_ready", issue=7, title="t", url="u"
    ) == render_human_message("review_ready", load_project(), issue=7, title="t", url="u")


def test_project_config_messages_default_to_empty_so_a_project_cannot_page_by_accident(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    assert load_project(path).messages == {}


def test_project_config_refuses_a_message_that_is_not_one_line():
    # ntfy renders one line; a template with a newline would arrive mangled instead of failing
    # anywhere a person would see it.
    with pytest.raises(ValueError, match="one non-blank line"):
        _messages_project(messages={"review_ready": "first line\nsecond line"})
    with pytest.raises(ValueError, match="one non-blank line"):
        _messages_project(messages={"review_ready": "   "})


def test_the_real_config_writes_every_page_the_mechanism_can_send():
    # Six mechanical pages, one key each: the 2026-09-14 ADR's original two triggers
    # (`quota_exhausted_no_fallback`, `planner_run_cap_reached`), its 2026-09-16 third trigger
    # (`review_ready`, #366), and the second trigger's own concrete form twice over --
    # `backend_worktree_missing` (#392) and `unreviewed_pull_request` (#394) -- two more PAGES,
    # not two more triggers -- and `backend_worktree_dirty` (#86), its third concrete form.
    assert set(load_project().messages) == {
        "review_ready",
        "quota_exhausted_no_fallback",
        "planner_run_cap_reached",
        "backend_worktree_missing",
        "unreviewed_pull_request",
        "backend_worktree_dirty",
    }


def test_the_real_configs_pages_render_with_the_fields_their_call_sites_pass():
    project = load_project()
    assert render_human_message("review_ready", project, issue=1, title="t", url="u")
    assert render_human_message(
        "quota_exhausted_no_fallback", project, issue=1, backend="claude", task_class="planner"
    )
    assert render_human_message(
        "planner_run_cap_reached", project, runs=40, cap=40, pending=3, context="worker_cut claude"
    )
    assert render_human_message(
        "backend_worktree_missing",
        project,
        backend="qwen",
        worktree="/x/example-qwen",
        issue_count=2,
    )
    assert render_human_message(
        "unreviewed_pull_request", project, pr=409, issue=394, role="validator"
    )
    assert render_human_message(
        "backend_worktree_dirty",
        project,
        backend="qwen",
        worktree="/x/example-qwen",
        paths=" M README.md",
        issue_count=2,
    )


# ---- worker_environment / test_command: workers connect read-only by default (#350)
# (agent_os/docs/adr/2026-09-15-workers-connect-read-only-by-default-and-reach-the-owner-only-through-the-
# test-runner.md) --------------------------------------------------------------------------------


def test_project_config_worker_environment_defaults_to_empty_when_absent(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    assert load_project(path).worker_environment == {}


def test_project_config_test_command_defaults_to_scripts_test_sh_when_absent(tmp_path):
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    assert load_project(path).test_command == "scripts/test.sh"


def test_project_config_worker_environment_and_test_command(tmp_path):
    path = _write(
        tmp_path / "agents.yaml",
        """
project:
  repo: someone/something
  tracking_epic: 1
  board_number: 1
  test_command: bin/run-tests
  worker_environment:
    DATABASE_URL: postgresql+psycopg://app_ro:app_ro@localhost:5432/app

classes:
  mechanical-qwen:
    backend: qwen
    model: qwen3.8-max
    max_context: 400000
    max_cost_usd: 5.0
    max_total_tokens: 80000000
    commit_warn_turns: 15
    commit_cut_turns: 25
""",
    )
    project = load_project(path)
    assert project.test_command == "bin/run-tests"
    assert project.worker_environment == {
        "DATABASE_URL": "postgresql+psycopg://app_ro:app_ro@localhost:5432/app"
    }


def test_print_worker_environment_prints_one_tab_separated_line_per_entry(monkeypatch, capsys):
    project = ProjectConfig(
        repo="a/b",
        tracking_epic=1,
        board_number=1,
        worker_environment={"DATABASE_URL": "postgresql://x", "OTHER": "y"},
    )
    monkeypatch.setattr(agent_lib, "load_project", lambda: project)
    agent_lib._print_worker_environment()
    out = capsys.readouterr().out
    assert "DATABASE_URL\tpostgresql://x" in out
    assert "OTHER\ty" in out


def test_print_worker_environment_prints_nothing_when_empty(monkeypatch, capsys):
    project = ProjectConfig(repo="a/b", tracking_epic=1, board_number=1)
    monkeypatch.setattr(agent_lib, "load_project", lambda: project)
    agent_lib._print_worker_environment()
    assert capsys.readouterr().out == ""


# What the worker READS about that same list: the paragraph `worker_task.sh` injects via
# __WORKER_ENVIRONMENT_RULES__. Names, never values -- the sentence it replaced spelled the shared
# server's port in the driver's own text (`agent_os/docs/AGENT_OS.md` §7 row (a), #363).


def test_worker_environment_rules_render_nothing_for_a_project_that_exports_nothing():
    # Nothing exported, nothing claimed: a worker told its connection is read-only by default would
    # trust a guard that does not exist. This is how the driver knows to drop the whole paragraph.
    assert (
        worker_environment_rules(ProjectConfig(repo="a/b", tracking_epic=1, board_number=1)) == ""
    )


def test_worker_environment_rules_defaults_to_load_project_when_none_given():
    assert worker_environment_rules() == worker_environment_rules(load_project())


def test_worker_environment_rules_name_the_variables_and_never_a_fragment_of_a_value():
    project = ProjectConfig(
        repo="a/b",
        tracking_epic=1,
        board_number=1,
        test_command="bin/run-tests",
        worker_environment={
            "SENTINEL_URL": "postgresql://sentinel_ro:sentinel_pw@localhost:9999/sentinel_db",
            "SENTINEL_TOKEN": "sentinel-secret",
        },
    )
    rendered = worker_environment_rules(project)
    flattened = " ".join(rendered.split())

    assert rendered.startswith("THE ENVIRONMENT YOU RUN IN IS CONFIGURED FOR YOU, AND READ-ONLY")
    assert "`SENTINEL_URL`, `SENTINEL_TOKEN`" in flattened
    for fragment in ("9999", "sentinel_pw", "sentinel-secret", "postgresql://", "localhost"):
        assert fragment not in rendered, fragment
    # The one door back to the owner role is named from `project.test_command`, never hardcoded.
    assert "`bin/run-tests`" in flattened
    # A paragraph injected into a prompt stays inside the width the rest of the RULES wrap at.
    assert all(len(line) <= 100 for line in rendered.splitlines()), rendered


# ---- forbidden_paths / never_run: the protected paths and the forbidden commands are the host
# project's, read from config rather than spelled in a mechanism script (#363,
# `agent_os/docs/AGENT_OS.md` §7 rows (a) and (b)) -------------------------------------------------------


def _project_yaml(extra: str) -> str:
    """VALID_YAML with `extra` (already indented two spaces) inside its `project:` section.
    Appending to the end of the file instead would put the key inside `classes:`, where it fails
    as an unknown task class -- the right exception for the wrong reason."""
    return VALID_YAML.replace("  board_number: 1\n", "  board_number: 1\n" + extra, 1)


def test_project_config_forbidden_paths_and_never_run_default_to_empty(tmp_path):
    # No key at all: a project that protects nothing forbids nothing, and the paragraphs a driver
    # renders from an empty list are absent rather than empty.
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    project = load_project(path)
    assert project.forbidden_paths == []
    assert project.never_run == []


def test_project_config_parses_both_lists(tmp_path):
    path = _write(
        tmp_path / "agents.yaml",
        _project_yaml(
            """  forbidden_paths:
    - docs/adr/*
    - config/curated.yaml
  never_run:
    - command: "--write"
      reason: it persists the census
    - command: rebuild
      reason: it moves the stamp
"""
        ),
    )
    project = load_project(path)
    assert project.forbidden_paths == ["docs/adr/*", "config/curated.yaml"]
    assert [(item.command, item.reason) for item in project.never_run] == [
        ("--write", "it persists the census"),
        ("rebuild", "it moves the stamp"),
    ]


@pytest.mark.parametrize(
    "never_run_block",
    [
        "  never_run:\n    - command: rebuild\n",  # the reason is the point of the entry
        '  never_run:\n    - command: rebuild\n      reason: "  "\n',  # and it says something
        "  never_run:\n    - command: rebuild\n      reason: because\n      since: 2026-09-01\n",
        '  never_run:\n    - command: rebuild\n      reason: "it moves\\nthe stamp"\n',
    ],
)
def test_never_run_rejects_an_item_that_is_not_one_command_with_one_reason(
    tmp_path, never_run_block
):
    # The last case is a reason carrying a newline (a YAML double-quoted scalar decodes `\n`),
    # which would break the RULES paragraph it is injected into without anything downstream
    # failing loudly on it.
    path = _write(tmp_path / "agents.yaml", _project_yaml(never_run_block))
    with pytest.raises(ValidationError):
        load_project(path)


def test_forbidden_paths_rejects_an_entry_that_is_not_a_path_string(tmp_path):
    path = _write(
        tmp_path / "agents.yaml", _project_yaml("  forbidden_paths:\n    - path: docs/adr/*\n")
    )
    with pytest.raises(ValidationError):
        load_project(path)


# ---- `merge_audit_exempt_paths`: the delivery-directory carve-out condition 3 of the merge gate
# reads (issue #476) -- a subset of `forbidden_paths`, never a second copy of it -----------------


def test_merge_audit_exempt_paths_defaults_to_empty(tmp_path):
    # No key at all: a project with no delivery-directory split audits its whole `forbidden_paths`
    # at merge time, exactly as before this key existed.
    path = _write(tmp_path / "agents.yaml", _project_yaml("  forbidden_paths:\n    - docs/adr/*\n"))
    assert load_project(path).merge_audit_exempt_paths == []


def test_merge_audit_exempt_paths_parses_a_subset_of_forbidden_paths(tmp_path):
    path = _write(
        tmp_path / "agents.yaml",
        _project_yaml(
            "  forbidden_paths:\n"
            "    - docs/adr/*\n"
            "    - config/v0.yaml\n"
            "  merge_audit_exempt_paths:\n"
            "    - docs/adr/*\n"
        ),
    )
    project = load_project(path)
    assert project.forbidden_paths == ["docs/adr/*", "config/v0.yaml"]
    assert project.merge_audit_exempt_paths == ["docs/adr/*"]


def test_merge_audit_exempt_paths_naming_a_path_not_in_forbidden_paths_is_a_silent_no_op(tmp_path):
    # CHANGED (#516 review, PR #516): this used to `pytest.raises(ValidationError)` -- a
    # cross-field validator refused to load a config where `merge_audit_exempt_paths` named a path
    # absent from `forbidden_paths`. That validator ran on every `load_project()`, which backs
    # every role the mechanism launches (guard, planner, every worker), and CI showed it also ran
    # on the many `tests/test_worker_task.py` fixtures that shorten `forbidden_paths` for one audit
    # case without touching this key -- ten of them failed the whole config load over it (PR #516,
    # `test_the_rules_paragraph_and_the_audit_regex_are_rendered_from_one_configured_list` and
    # nine more). A single stale entry in a merge-time carve-out is not worth refusing every role's
    # config load over, host-wide, so the check is gone: an exempted path this project's
    # `forbidden_paths` does not currently carry has nothing to subtract from and is inert.
    path = _write(
        tmp_path / "agents.yaml",
        _project_yaml(
            "  forbidden_paths:\n"
            "    - docs/adr/*\n"
            "  merge_audit_exempt_paths:\n"
            "    - config/proposals/*\n"
        ),
    )
    project = load_project(path)
    assert project.merge_audit_exempt_paths == ["config/proposals/*"]
    # And it has no effect on the merge-audit pattern: `config/proposals/*` was never in
    # `forbidden_paths` here, so there is nothing for it to carve out of.
    assert re.match(forbidden_paths_merge_audit_regex(project), "docs/adr/2026-09-21-x.md")


# ---- what a driver renders from `forbidden_paths`: the RULES paragraph and the audit regex -----


def _project(forbidden_paths, merge_audit_exempt_paths=()):
    return ProjectConfig(
        repo="a/b",
        tracking_epic=1,
        board_number=1,
        forbidden_paths=forbidden_paths,
        merge_audit_exempt_paths=list(merge_audit_exempt_paths),
    )


def test_forbidden_paths_render_nothing_for_a_project_that_protects_no_path():
    empty = _project([])
    # Both halves print nothing, and that is how a driver tells "forbids nothing" from "forbids
    # something": an empty `grep -E` pattern matches every line, so auditing with one would flag
    # every file a run touched.
    assert forbidden_paths_rules(empty) == ""
    assert forbidden_paths_regex(empty) == ""


def test_forbidden_paths_rules_names_every_configured_path():
    project = _project(["docs/adr/*", "config/curated.yaml", ".claude/*"])
    rendered = forbidden_paths_rules(project)
    assert rendered.startswith("FILES YOU MUST NOT TOUCH\n")
    assert rendered.endswith("stop and say so rather than working around it.")
    for path in project.forbidden_paths:
        assert path in rendered
    # The unconditional half of the pair, saying so in its own words: "no brief can authorize one"
    # only reads as a rule because the paragraph below it names the paths a brief can (#390). Read
    # with the wrapping flattened, because where a line breaks depends on the listing's own length.
    flattened = " ".join(rendered.split())
    assert "no brief can authorize one" in flattened
    assert "a defect in the brief" in flattened
    assert "The mechanism's own files below" in flattened
    # A paragraph injected into a prompt stays inside the width the rest of the RULES wrap at.
    assert all(len(line) <= 100 for line in rendered.splitlines()), rendered


@pytest.mark.parametrize(
    ("pattern", "path", "protected"),
    [
        ("docs/adr/*", "docs/adr/2026-09-14-a-decision.md", True),
        # `*` crosses `/` -- fnmatch's semantics, not glob's: the prefix regex this list replaced
        # protected every nested file under a protected directory, and so must this one.
        (".claude/*", ".claude/agents/worker-runner.md", True),
        ("scripts/cp3?_x.sh", "scripts/cp34_x.sh", True),
        ("scripts/cp3?_x.sh", "scripts/cp345_x.sh", False),
        ("docs/[ab].md", "docs/b.md", True),
        ("docs/[ab].md", "docs/c.md", False),
        ("docs/[!a].md", "docs/b.md", True),  # glob negates a class with `!`, POSIX ERE with `^`
        ("docs/[!a].md", "docs/a.md", False),
        # A regex metacharacter in a path stays a literal one: `+` is no quantifier here.
        ("a+b(c).md", "a+b(c).md", True),
        ("a+b(c).md", "aab(c).md", False),
        ("weird[.md", "weird[.md", True),  # an unclosed `[` is a literal bracket
        # Prefix-anchored, as the literal it replaced was: an entry protects every path starting
        # with it, and never one that merely contains it.
        ("AGENTS.md", "AGENTS.md.bak", True),
        ("AGENTS.md", "docs/AGENTS.md", False),
    ],
)
def test_the_audit_regex_protects_a_path_exactly_where_the_glob_says_it_does(
    pattern, path, protected
):
    # Matched with Python's `re`, which accepts the POSIX ERE subset the translator emits;
    # `tests/test_worker_task.py` runs the derived pattern through the real `grep -E`.
    assert bool(re.match(forbidden_paths_regex(_project([pattern])), path)) is protected


# ---- `forbidden_paths_merge_audit_regex`/`_violations`: condition 3's own subset, issue #476 ----


def test_merge_audit_regex_is_empty_when_forbidden_paths_is():
    assert forbidden_paths_merge_audit_regex(_project([])) == ""


def test_merge_audit_regex_is_empty_when_every_path_is_exempted():
    # Not the same reading as `forbidden_paths_regex` on the same input -- the host list still
    # forbids the path to a worker's brief -- but condition 3's own pattern audits nothing, the
    # same "empty means skip, never match everything" rule the host regex already carries.
    project = _project(["docs/adr/*"], merge_audit_exempt_paths=["docs/adr/*"])
    assert forbidden_paths_merge_audit_regex(project) == ""
    assert forbidden_paths_regex(project) != ""  # the worker prohibition is unaffected


def test_merge_audit_regex_matches_only_the_non_exempt_paths():
    project = _project(
        ["docs/adr/*", "config/proposals/*", "app/metrics/*"],
        merge_audit_exempt_paths=["docs/adr/*", "config/proposals/*"],
    )
    audit = forbidden_paths_merge_audit_regex(project)
    assert re.match(audit, "app/metrics/x.py")
    assert not re.match(audit, "docs/adr/2026-09-21-a-decision.md")
    assert not re.match(audit, "config/proposals/y.yaml")


def test_merge_audit_violations_reports_nothing_for_an_empty_changed_list():
    project = _project(["app/metrics/*"])
    assert forbidden_paths_merge_audit_violations([], project) == []


def test_merge_audit_violations_reports_nothing_when_the_audit_pattern_is_empty():
    # The trap this function exists to close: `re.match("", anything)` matches at position 0, the
    # opposite of what an empty `grep -E` pattern is treated as everywhere else in this rule.
    project = _project(["docs/adr/*"], merge_audit_exempt_paths=["docs/adr/*"])
    assert forbidden_paths_merge_audit_violations(["anything/at/all.py"], project) == []


# ---- `mechanism.own_paths`: the second list, the mechanism's own files, which a brief may
# authorize where the host project's may not (#390,
# agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md) ----


def _mechanism_yaml(extra: str) -> str:
    """VALID_YAML with `extra` (a top-level block, already ending in a newline) inserted between
    its `project:` and `classes:` sections. Appending to the end of the file instead would nest the
    key inside the last task class, where it fails as an unknown field of `TaskClass` -- the right
    exception for the wrong reason."""
    return VALID_YAML.replace("\nclasses:", extra + "\nclasses:", 1)


def _mechanism(own_paths):
    return MechanismConfig(own_paths=own_paths)


def test_mechanism_own_paths_default_to_empty_when_the_section_is_absent(tmp_path):
    # No `mechanism:` key at all: the section is optional exactly as `planner:` is, so a config
    # that predates it still loads and audits no mechanism path -- it never fails the dispatch.
    path = _write(tmp_path / "agents.yaml", VALID_YAML)
    mechanism = load_mechanism(path)
    assert mechanism.own_paths == []
    # And "audits none" is an empty pattern, not a pattern that matches everything: `grep -E ''`
    # selects every line, so an empty list rendered as an empty pattern would report every file a
    # run touched as a violation of the mechanism's own rule.
    assert mechanism_paths_regex(mechanism) == ""


def test_mechanism_section_parses_its_own_paths(tmp_path):
    path = _write(
        tmp_path / "agents.yaml",
        _mechanism_yaml(
            """mechanism:
  own_paths:
    - scripts/worker_task.sh
    - .claude/*

"""
        ),
    )
    assert load_mechanism(path).own_paths == ["scripts/worker_task.sh", ".claude/*"]


@pytest.mark.parametrize(
    "mechanism_block",
    [
        "mechanism:\n  own_paths:\n    - path: scripts/worker_task.sh\n\n",  # not a bare string
        "mechanism:\n  own_paths: scripts/worker_task.sh\n\n",  # not a list at all
        "mechanism:\n  own_paths: []\n  forbidden_paths: []\n\n",  # the host project's key is not
        # the mechanism's, and `extra="forbid"` is what says so instead of ignoring it
    ],
)
def test_mechanism_section_rejects_an_entry_or_a_key_that_is_not_its_own(tmp_path, mechanism_block):
    path = _write(tmp_path / "agents.yaml", _mechanism_yaml(mechanism_block))
    with pytest.raises(ValidationError):
        load_mechanism(path)


def test_mechanism_paths_regex_is_rendered_the_same_way_as_the_host_projects():
    # One translator behind both lists (`agent_lib._anchored_ere`): what separates them is the rule
    # a driver applies to a match, never how a glob is read, so the same pattern has to produce the
    # same regex whichever list carries it.
    patterns = ["docs/adr/*", "scripts/cp34_?.sh", "a+b(c).md", ".claude/*"]
    assert mechanism_paths_regex(_mechanism(patterns)) == forbidden_paths_regex(_project(patterns))


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("scripts/worker_task.sh", True),
        # `*` crosses `/` -- fnmatch's semantics, not glob's -- so `.claude/*` covers a role prompt
        # nested two levels down, as the prefix regex it replaced did.
        (".claude/agents/worker-runner.md", True),
        # Prefix-anchored, never end-anchored: a path that merely contains the name stays clean.
        ("docs/scripts/worker_task.sh", False),
        ("scripts/agent_lib.py", False),
        ("config/agents.yaml", False),
    ],
)
def test_the_mechanism_regex_selects_the_paths_its_own_globs_name(path, protected):
    regex = mechanism_paths_regex(_mechanism(["scripts/worker_task.sh", ".claude/*"]))
    assert bool(re.match(regex, path)) is protected


def test_mechanism_paths_regex_defaults_to_load_mechanism_when_none_given():
    assert mechanism_paths_regex() == mechanism_paths_regex(load_mechanism())


def test_mechanism_paths_rules_render_nothing_for_a_mechanism_naming_none_of_its_own_files():
    # The paragraph's own version of the empty-means-absent rule the regex already has: a heading
    # with no list under it reads as a rule nobody can see. `worker_task.sh` takes the host
    # paragraph down with this one, because the two state a single contrast (#390).
    assert mechanism_paths_rules(_mechanism([])) == ""
    assert mechanism_paths_rules(MechanismConfig()) == ""


def test_mechanism_paths_rules_names_every_configured_path_and_states_the_conditional_rule():
    mechanism = _mechanism(["scripts/worker_task.sh", ".claude/*"])
    rendered = mechanism_paths_rules(mechanism)
    assert rendered.startswith("FILES OF THE MECHANISM'S OWN\n")
    assert rendered.endswith("stop and say so rather than working around it.")
    for path in mechanism.own_paths:
        assert path in rendered, path
    # The half of the rule a brief can satisfy, with the two things the worker owes when it does:
    # the path named in the body, and a word about it before the first commit that touches it. Read
    # with the wrapping flattened, because where a line breaks depends on the listing's own length.
    flattened = " ".join(rendered.split())
    assert "only when the issue body names that path" in flattened
    assert "before the first commit" in flattened
    # It points back at the other paragraph and at the rule that forbids the main checkout, so
    # neither half can be read on its own -- which is why the driver injects the pair or neither.
    assert "the rule above that forbids writing there at all" in flattened
    assert all(len(line) <= 100 for line in rendered.splitlines()), rendered


def test_mechanism_paths_rules_defaults_to_load_mechanism_when_none_given():
    assert mechanism_paths_rules() == mechanism_paths_rules(load_mechanism())


# ---- the dispatchable predicate --------------------------------------------------------------


# A body with every required section, in order, and a resolvable budget class: the shape
# `.github/ISSUE_TEMPLATE/task.md` scaffolds. The dispatchable predicate is defined as "this
# validates and it is labeled ready", so the fixture has to be a valid body for anything else the
# predicate checks to be observable at all.
VALID_BODY = (
    "\n\n".join(f"{heading}\nsomething" for heading in REQUIRED_SECTIONS).replace(
        "## Stages\nsomething", "## Stages\n- [ ] Do the thing"
    )
    + "\n\n<!-- budget: mechanical-qwen -->"
)


def _issue(**overrides) -> dict:
    issue = {
        "number": 51,
        "state": "OPEN",
        "labels": [{"name": "status:ready"}, {"name": "module:workers"}],
        "body": VALID_BODY,
    }
    issue.update(overrides)
    return issue


def _classes() -> dict[str, TaskClass]:
    return load_task_classes()


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Blocked by #12\nBlocked by #13", [12, 13]),
        ("  Blocked by #7  ", [7]),
        ("blocked by #7", [7]),  # the label vocabulary is mechanical, its casing is not
        ("This is not blocked by #7 in any way", []),  # only a line of its own counts
        ("", []),
    ],
)
def test_blocking_issue_numbers(body, expected):
    assert blocking_issue_numbers(body) == expected


def test_is_dispatchable_true_for_a_ready_budgeted_unblocked_issue():
    assert is_dispatchable(_issue(), task_classes=_classes(), open_issue_numbers=set()) is True


def test_is_dispatchable_false_without_the_ready_label():
    issue = _issue(labels=[{"name": "module:workers"}])
    assert is_dispatchable(issue, task_classes=_classes(), open_issue_numbers=set()) is False


def test_is_dispatchable_false_without_a_resolvable_budget():
    # This is the exact shape of #345: fifty open issues, not one of them carrying a budget class,
    # and the old queue check calling every one of them "queued".
    no_marker = _issue(body=VALID_BODY.replace("<!-- budget: mechanical-qwen -->", ""))
    unknown = _issue(body=VALID_BODY.replace("mechanical-qwen", "not-a-class"))
    assert is_dispatchable(no_marker, task_classes=_classes(), open_issue_numbers=set()) is False
    assert is_dispatchable(unknown, task_classes=_classes(), open_issue_numbers=set()) is False


def test_is_dispatchable_false_when_blocked_on_human():
    issue = _issue(
        labels=[{"name": "status:ready"}, {"name": "status:blocked-on-human"}],
    )
    assert is_dispatchable(issue, task_classes=_classes(), open_issue_numbers=set()) is False


def test_is_dispatchable_false_while_a_named_blocker_is_still_open():
    issue = _issue(
        body=VALID_BODY.replace("## Dependencies\nsomething", "## Dependencies\nBlocked by #40")
    )
    assert is_dispatchable(issue, task_classes=_classes(), open_issue_numbers={40}) is False
    assert is_dispatchable(issue, task_classes=_classes(), open_issue_numbers={41}) is True


def test_is_dispatchable_false_on_a_closed_issue():
    issue = _issue(state="CLOSED")
    assert is_dispatchable(issue, task_classes=_classes(), open_issue_numbers=set()) is False


def test_is_dispatchable_false_without_a_well_formed_stages_section():
    # #375: no `## Stages` checklist, no dispatch -- whoever wrote the issue, human or refiner.
    no_section = _issue(body=VALID_BODY.replace("## Stages\n- [ ] Do the thing\n\n", ""))
    no_boxes = _issue(body=VALID_BODY.replace("- [ ] Do the thing", "some prose, no boxes"))
    assert is_dispatchable(no_section, task_classes=_classes(), open_issue_numbers=set()) is False
    assert is_dispatchable(no_boxes, task_classes=_classes(), open_issue_numbers=set()) is False


def test_is_dispatchable_uses_the_configured_label_vocabulary():
    labels = LabelVocabulary(ready="queue:go", blocked_on_human="queue:ask")
    issue = _issue(labels=[{"name": "queue:go"}])
    assert (
        is_dispatchable(issue, task_classes=_classes(), open_issue_numbers=set(), labels=labels)
        is True
    )
    assert is_dispatchable(issue, task_classes=_classes(), open_issue_numbers=set()) is False


# ---- needs_refinement / promotable_to_ready: the refiner's own predicates
# (agent_os/docs/adr/2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md) ----


INVALID_BODY = "nothing here"


def _refine_issue(**overrides) -> dict:
    issue = {
        "number": 60,
        "state": "OPEN",
        "labels": [{"name": "status:refine"}],
        "body": VALID_BODY,
    }
    issue.update(overrides)
    return issue


def test_needs_refinement_true_when_the_body_fails_validation():
    issue = _refine_issue(body=INVALID_BODY)
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers=set()) is True


def test_needs_refinement_false_when_the_body_already_validates():
    # A status:refine issue whose body already validates is not IN NEED of refining -- it is a
    # candidate for promotable_to_ready instead.
    assert (
        needs_refinement(_refine_issue(), task_classes=_classes(), open_issue_numbers=set())
        is False
    )


def test_needs_refinement_false_without_the_refine_label():
    issue = _refine_issue(labels=[{"name": "type:task"}], body=INVALID_BODY)
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers=set()) is False


def test_needs_refinement_false_when_blocked_on_human():
    issue = _refine_issue(
        body=INVALID_BODY,
        labels=[{"name": "status:refine"}, {"name": "status:blocked-on-human"}],
    )
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers=set()) is False


def test_needs_refinement_false_on_a_closed_issue():
    issue = _refine_issue(state="CLOSED", body=INVALID_BODY)
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers=set()) is False


def test_needs_refinement_true_without_a_stages_section_even_if_the_rest_is_well_formed():
    # #375: an issue a human wrote before staging existed (or without it) is refinable on that
    # ground alone, so the refiner runs on every incoming task/bug regardless of who wrote it.
    issue = _refine_issue(body=VALID_BODY.replace("## Stages\n- [ ] Do the thing\n\n", ""))
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers=set()) is True


def test_needs_refinement_false_when_the_stages_section_is_well_formed():
    assert (
        needs_refinement(_refine_issue(), task_classes=_classes(), open_issue_numbers=set())
        is False
    )


def test_needs_refinement_false_for_a_template_valid_body_blocked_on_an_open_issue():
    # The real case behind this fix: #356 is template-conformant, status:refine, and carries
    # `Blocked by #323` while #323 is still open -- validate_issue_body (and so is_dispatchable)
    # correctly calls that "not dispatchable yet", but it is not a defect the REFINER wrote or can
    # rewrite away, so it must not trigger `refine_pending` and relaunch the refiner for nothing.
    issue = _refine_issue(
        body=VALID_BODY.replace("## Dependencies\nsomething", "## Dependencies\nBlocked by #40")
    )
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers={40}) is False
    # A closed blocker is moot either way: the body is fully valid, nothing to refine.
    assert needs_refinement(issue, task_classes=_classes(), open_issue_numbers={41}) is False


def test_promotable_to_ready_true_when_valid_and_the_parent_carries_auto_ready():
    assert (
        promotable_to_ready(
            _refine_issue(),
            parent_labels={"auto-ready"},
            task_classes=_classes(),
            open_issue_numbers=set(),
        )
        is True
    )


def test_promotable_to_ready_false_without_a_parent():
    # No parent means no feature above it opted in -- the human promotes it by hand.
    assert (
        promotable_to_ready(
            _refine_issue(), parent_labels=None, task_classes=_classes(), open_issue_numbers=set()
        )
        is False
    )


def test_promotable_to_ready_false_when_the_parent_lacks_auto_ready():
    assert (
        promotable_to_ready(
            _refine_issue(),
            parent_labels={"type:feature"},
            task_classes=_classes(),
            open_issue_numbers=set(),
        )
        is False
    )


def test_promotable_to_ready_false_when_the_body_still_fails_validation():
    # Passes validate is one of two conditions; a parent's auto-ready alone is not enough.
    assert (
        promotable_to_ready(
            _refine_issue(body=INVALID_BODY),
            parent_labels={"auto-ready"},
            task_classes=_classes(),
            open_issue_numbers=set(),
        )
        is False
    )


def test_promotable_to_ready_false_when_blocked_by_an_open_issue_even_under_auto_ready():
    # Unlike needs_refinement (a structural defect the refiner can fix), promotable_to_ready still
    # uses the FULL validate_issue_body: an issue with an open blocker must never be promoted to
    # status:ready mechanically, auto-ready or not -- it is not dispatchable yet regardless of who
    # wrote its body.
    issue = _refine_issue(
        body=VALID_BODY.replace("## Dependencies\nsomething", "## Dependencies\nBlocked by #40")
    )
    assert (
        promotable_to_ready(
            issue, parent_labels={"auto-ready"}, task_classes=_classes(), open_issue_numbers={40}
        )
        is False
    )
    # ... and promotable once that blocker is closed.
    assert (
        promotable_to_ready(
            issue, parent_labels={"auto-ready"}, task_classes=_classes(), open_issue_numbers={41}
        )
        is True
    )


def test_promotable_to_ready_false_when_blocked_on_human():
    issue = _refine_issue(labels=[{"name": "status:refine"}, {"name": "status:blocked-on-human"}])
    assert (
        promotable_to_ready(
            issue, parent_labels={"auto-ready"}, task_classes=_classes(), open_issue_numbers=set()
        )
        is False
    )


def test_promotable_to_ready_false_on_a_closed_issue():
    assert (
        promotable_to_ready(
            _refine_issue(state="CLOSED"),
            parent_labels={"auto-ready"},
            task_classes=_classes(),
            open_issue_numbers=set(),
        )
        is False
    )


def test_auto_ready_label_is_not_a_state_label():
    # A marker on a feature, not a mechanical state: `issues.py move` must never add or remove it.
    vocabulary = LabelVocabulary()
    assert vocabulary.auto_ready == "auto-ready"
    assert vocabulary.auto_ready not in vocabulary.state_labels


def test_planner_config_refiner_unattended_defaults_false():
    assert PlannerConfig().refiner_unattended is False


def test_planner_config_refiner_unattended_parses_true_when_set(tmp_path):
    # This used to assert `load_planner_config().refiner_unattended is False` straight off
    # config/agents.yaml, while #349 criterion 3 kept the real flag at its shipped default. The
    # human reviewed the refiner's dry run on #331, #338 and #104/#356 and approved flipping it on
    # 2026-09-15 (#349), so the real file now carries `true` -- asserting that against the real
    # file would only re-encode whatever the file happens to say today, so this checks the parsing
    # itself instead, off a throwaway file (same pattern VALID_YAML already uses for `project:`).
    path = _write(tmp_path / "agents.yaml", VALID_YAML + "\nplanner:\n  refiner_unattended: true\n")
    assert load_planner_config(path).refiner_unattended is True


def test_planner_config_relaunch_cap_defaults_to_two():
    # #362: the two-try relaunch cap the driver enforces (`worker_task.sh resume`) reads this
    # field instead of a literal in the script.
    assert PlannerConfig().relaunch_cap == 2


def test_planner_config_max_parallel_issues_defaults_to_one():
    # #374: the parallelism cap the driver enforces (`worker_task.sh start`) reads this field
    # instead of an implicit "two backends, two slots" assumption.
    assert PlannerConfig().max_parallel_issues == 1


# ---- runs.tsv: one line per planner run ------------------------------------------------------


PLANNER_LOG = """ts:        20260914T120000Z
model:     claude-opus-5
{"type":"system","subtype":"init","session_id":"s1"}
{"type":"assistant","message":{"usage":{"input_tokens":10}}}
{"type":"result","subtype":"success","num_turns":4,"total_cost_usd":0.1234,"session_id":"s1"}
"""


def test_planner_run_row_reads_turns_and_cost_from_the_last_result_event():
    row = planner_run_row(
        PLANNER_LOG, ts="2026-09-14T12:00:05Z", context="claude was cut", model="claude-opus-5"
    )
    assert row.split("\t") == [
        "2026-09-14T12:00:05Z",
        "claude was cut",
        "claude-opus-5",
        "4",
        "0.1234",
    ]


def test_planner_run_row_flattens_and_truncates_the_context():
    row = planner_run_row(PLANNER_LOG, ts="T", context="a\tb\nc " + "x" * 200, model="m")
    context = row.split("\t")[1]
    assert len(context) == 120
    assert context.startswith("a b c xxx")


def test_planner_run_row_still_writes_a_line_when_the_run_left_no_result():
    # A killed or crashed run must still leave a row -- a missing one reads as "it never ran".
    row = planner_run_row("model:     m\nnot json at all\n", ts="T", context="c", model="m")
    assert row.split("\t") == ["T", "c", "m", "", ""]


# ---- the body contract: the one implementation `issues.py validate` and the guard share --------


def test_validate_issue_body_accepts_the_template_shape():
    assert validate_issue_body(VALID_BODY, task_classes=_classes(), open_issue_numbers=set()) == []


def test_section_failures_names_every_missing_section():
    body = "## Objective\nx\n\n## Definition of done\ny\n\n<!-- budget: mechanical-qwen -->"
    assert section_failures(body) == [
        "missing section: ## Acceptance criteria",
        "missing section: ## Stages",
        "missing section: ## Context",
        "missing section: ## Not included",
        "missing section: ## Dependencies",
    ]


def test_section_failures_catches_the_right_sections_in_the_wrong_order():
    swapped = (
        VALID_BODY.replace("## Objective\nsomething", "PLACEHOLDER")
        .replace("## Context\nsomething", "## Objective\nsomething")
        .replace("PLACEHOLDER", "## Context\nsomething")
    )
    failures = section_failures(swapped)
    assert len(failures) == 1
    assert failures[0].startswith("sections out of order:")


def test_section_failures_ignores_a_heading_that_is_only_mentioned_in_a_sentence():
    body = VALID_BODY.replace("## Objective\nsomething", "the ## Objective section is missing")
    assert "missing section: ## Objective" in section_failures(body)


def test_section_failures_flags_a_stages_section_with_no_checklist_line():
    body = VALID_BODY.replace("## Stages\n- [ ] Do the thing", "## Stages\nsome prose, no boxes")
    assert "stages: no checklist line" in section_failures(body)


def test_section_failures_accepts_an_uppercase_x_checked_box():
    body = VALID_BODY.replace("## Stages\n- [ ] Do the thing", "## Stages\n- [X] Do the thing")
    assert "stages: no checklist line" not in section_failures(body)


# ---- parse_stages: stage titles off the ## Stages checklist -----------------------------------


def test_parse_stages_reads_open_and_checked_boxes_in_order():
    body = "## Stages\n- [ ] First stage\n- [x] Second stage\n\n## Context\nsomething"
    assert parse_stages(body) == ["First stage", "Second stage"]


def test_parse_stages_empty_when_the_section_is_absent():
    assert parse_stages("## Objective\nsomething") == []


def test_parse_stages_empty_when_the_section_has_no_checklist_line():
    assert parse_stages("## Stages\nsome prose, no boxes\n\n## Context\nx") == []


def test_parse_stages_stops_at_the_next_heading():
    body = "## Stages\n- [ ] Only this one\n\n## Context\n- [ ] not a stage"
    assert parse_stages(body) == ["Only this one"]


# ---- stages_completed: progress derived from commit subjects ----------------------------------


def test_stages_completed_reads_the_highest_stage_from_mixed_subjects():
    subjects = [
        "docs: unrelated tidy-up",
        "stage 1/3: scaffold the test",
        "fix a typo",
        "stage 2/3: implement the behaviour",
    ]
    assert stages_completed(subjects) == 2


def test_stages_completed_zero_when_no_subject_matches():
    assert stages_completed(["a commit", "another commit"]) == 0
    assert stages_completed([]) == 0


def test_validate_issue_body_reports_an_unresolvable_budget_class():
    body = VALID_BODY.replace("mechanical-qwen", "not-a-class")
    failures = validate_issue_body(body, task_classes=_classes(), open_issue_numbers=set())
    assert len(failures) == 1
    assert failures[0].startswith("budget class 'not-a-class' is not defined")


def test_validate_issue_body_reports_only_the_blockers_that_are_still_open():
    body = VALID_BODY.replace(
        "## Dependencies\nsomething", "## Dependencies\nBlocked by #40\nBlocked by #41"
    )
    assert validate_issue_body(body, task_classes=_classes(), open_issue_numbers={41}) == [
        "blocked by #41, which is still open"
    ]


def test_validate_issue_body_reports_every_failure_at_once():
    # One run of the validator has to name everything wrong: a caller that fixes one failure and
    # is handed the next one is what makes an issue take six round trips to become dispatchable.
    failures = validate_issue_body(
        "nothing here", task_classes=_classes(), open_issue_numbers=set()
    )
    assert len(failures) == len(REQUIRED_SECTIONS) + 1


# ---- the mechanical state vocabulary ----------------------------------------------------------


def test_label_for_state_covers_every_state_and_done_has_none():
    vocabulary = LabelVocabulary()
    assert [vocabulary.label_for_state(state) for state in STATES] == [
        "status:refine",
        "status:ready",
        "status:doing",
        "status:blocked-on-human",
        "status:ai-completed",
        "status:review",
        None,
    ]


def test_state_labels_are_the_six_a_move_strips_and_never_the_full_stop():
    vocabulary = LabelVocabulary()
    assert len(vocabulary.state_labels) == len(STATES) - 1
    assert vocabulary.agents_paused not in vocabulary.state_labels


def test_a_backend_with_no_configured_executable_resolves_to_its_own_bare_name():
    """The default is the PATH lookup every driver already did: a project that configures nothing
    keeps today's behaviour exactly (#380)."""
    project = ProjectConfig(repo="owner/name", tracking_epic=1, board_number=1)
    assert project.executables == {}
    assert agent_lib.backend_executable("qwen", project=project) == "qwen"


def test_a_configured_executable_is_what_the_drivers_run():
    project = ProjectConfig(
        repo="owner/name",
        tracking_epic=1,
        board_number=1,
        executables={"qwen": "/opt/nvm/bin/qwen"},
    )
    assert agent_lib.backend_executable("qwen", project=project) == "/opt/nvm/bin/qwen"
    # ... and a name the mapping does not carry still falls back, one key at a time.
    assert agent_lib.backend_executable("claude", project=project) == "claude"


def test_the_executables_mapping_is_validated_like_every_other_project_key():
    with pytest.raises(ValidationError):
        ProjectConfig(
            repo="owner/name", tracking_epic=1, board_number=1, executables={"qwen": ["a", "list"]}
        )


def test_board_columns_read_from_the_real_config_cover_every_state():
    columns = load_project().board_columns
    assert set(columns) == set(STATES)
    # Being blocked says nothing about how far the work got, so the item keeps its column.
    assert columns["blocked-on-human"] is None
    assert columns["doing"] == "In progress"


def test_label_names_reads_every_shape_a_gh_json_row_comes_in():
    # A listing row may omit `labels` entirely or carry null; a single `gh issue view` gives the
    # same key. One reader for all three, so no call site has to remember `or []`.
    assert agent_lib.label_names({"labels": [{"name": "status:ready"}, {"name": "p1"}]}) == {
        "status:ready",
        "p1",
    }
    assert agent_lib.label_names({"labels": None}) == set()
    assert agent_lib.label_names({}) == set()


def test_mechanism_logins_are_every_app_identity_as_github_spells_them():
    project = agent_lib.load_project()
    logins = agent_lib.mechanism_logins(project)
    assert f"{project.planner_app}[bot]" in logins
    for slug in project.worker_apps.values():
        assert f"{slug}[bot]" in logins
    assert all(login == login.lower() for login in logins)


def test_only_the_configured_human_writes_a_human_comment():
    project = agent_lib.load_project()
    assert project.human_login, "config/agents.yaml names no project.human_login"
    assert agent_lib.is_human_comment(project.human_login) is True
    assert agent_lib.is_human_comment(project.human_login.upper()) is True
    assert agent_lib.is_human_comment(f"{project.planner_app}[bot]") is False
    assert agent_lib.is_human_comment("") is False
    assert agent_lib.is_human_comment("somebody-else") is False


def test_with_no_human_configured_anyone_but_the_mechanism_counts():
    # A project that has not named its human still must not read the mechanism's own voice as a
    # reply; it only loses the ability to tell one outsider from another.
    project = agent_lib.load_project().model_copy(update={"human_login": ""})
    assert agent_lib.is_human_comment("somebody-else", project) is True
    assert agent_lib.is_human_comment(f"{project.planner_app}[bot]", project) is False


def _comment(author: str, body: str) -> dict:
    return {"author": {"login": author}, "body": body, "createdAt": "2026-09-24T10:00:00Z"}


def test_a_refiner_pass_counts_as_answered_only_after_a_human_reply_to_the_latest_summary():
    # agent-os#72: an answered refiner doubt must not be named for refining again.
    project = agent_lib.load_project()
    human, bot = project.human_login, f"{project.planner_app}[bot]"
    summary = f"{agent_lib.REFINER_SUMMARY_MARKER}\n@{human}\n\nSplit in two."
    answered = agent_lib.refiner_pass_answered_by_the_human
    assert answered({"comments": [_comment(bot, summary), _comment(human, "(c)")]}, project)
    # No summary at all, whoever commented.
    assert not answered({"comments": [_comment(human, "please refine")]}, project)
    assert not answered({}, project)
    # The human spoke only before the summary; the mechanism's own reply is not an answer.
    assert not answered(
        {
            "comments": [
                _comment(human, "please refine"),
                _comment(bot, summary),
                _comment(bot, "waiting for you"),
            ]
        },
        project,
    )
    # A second summary reopens the question: only a reply after the latest one counts.
    assert not answered(
        {"comments": [_comment(bot, summary), _comment(human, "(c)"), _comment(bot, summary)]},
        project,
    )


# -------------------------------------------------------------------------------------------------
# The role-launch gate (#425): which backend a role runs on is decided from the guard's OWN
# persisted quota verdict, never from an agent's claim about its own quota, and a class that
# declares a fallback runs on it instead of not running at all. On 2026-09-18 the planner's
# 08:48:46Z run was rejected by Claude's five-hour rate limit in 497 ms and the role that would
# have redispatched on Qwen was itself that rejected Claude run, so seven `status:ready` issues sat
# with a free Qwen allowance unused until 2026-09-20.
# -------------------------------------------------------------------------------------------------

VERDICT_TTL_SECONDS = 60 * 60  # `mechanism.quota_verdict_ttl_minutes: 60` in the real config


def _claude_role_class(**overrides) -> TaskClass:
    """A role class on Claude, the shape the three real ones have, built here so a case can vary
    one field -- the fallback, almost always -- without a config file per case."""
    fields: dict = {
        "role": "validator",
        "backend": "claude",
        "model": "claude-opus-5",
        "max_context": 200000,
        "max_cost_usd": 5.0,
        "max_total_tokens": 10000000,
        "commit_warn_turns": 30,
        "commit_cut_turns": 50,
    }
    fields.update(overrides)
    return TaskClass(**fields)


def _qwen_fallback(**overrides) -> RoleFallback:
    fields: dict = {
        "backend": "qwen",
        "model": "qwen3.8-max",
        "ceilings": ["max_context", "max_total_tokens"],
    }
    fields.update(overrides)
    return RoleFallback(**fields)


def _write_verdict(
    cache_dir: pathlib.Path,
    status: str | None,
    *,
    age: timedelta = timedelta(),
    backend: str = "claude",
    now: datetime | None = None,
) -> pathlib.Path:
    """The guard's own bookkeeping file, as `_save_bookkeeping` writes it, with its mtime set to
    the moment the verdict was observed -- the mtime IS the observation time (every tick that
    watches a run of that backend re-derives `last_quota_status` and rewrites the whole file), so
    a test ages a verdict the only way the real thing can be aged. `status=None` writes the shape
    a file from before this field existed had."""
    path = quota_verdict_file(backend, cache_dir=cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded = {"commit_count": 0, "turn_count_at_commit": 0, "warned_at_turn_count": None}
    if status is not None:
        recorded["last_quota_status"] = status
    path.write_text(json.dumps(recorded))
    observed_at = (now or datetime.now(UTC)) - age
    os.utime(path, (observed_at.timestamp(), observed_at.timestamp()))
    return path


# ---- the declaration, in the real config --------------------------------------------------------


def test_the_real_config_declares_qwen_as_the_fallback_of_all_three_claude_roles():
    classes = load_task_classes()
    worker_model = classes["complex-qwen"].model
    for role in ("planner", "validator", "refiner"):
        task_class = classes[role]
        assert task_class.backend == "claude", role
        fallback = task_class.fallback
        assert fallback is not None, f"{role} declares no fallback"
        assert fallback.backend == "qwen", role
        # The same model the two worker classes run: one allowance, one model, and nothing here
        # invents a second Qwen model name for the roles.
        assert fallback.model == worker_model == classes["mechanical-qwen"].model, role
        # Qwen's `result` event reports no `total_cost_usd` (#387), so a dollar ceiling on a
        # substituted run is one nothing can cross: declared out, on purpose.
        assert "max_cost_usd" not in fallback.ceilings, role
        assert set(fallback.ceilings) == {"max_context", "max_total_tokens"}, role


def test_a_worker_class_declares_no_fallback_and_behaves_as_it_did_before():
    # `qwen_fallback_eligible` is the planner's redispatch of a WORKER and stays as the 2026-09-16
    # ADR left it; the new field is a role's own launch gate and no worker class carries one.
    for name in ("mechanical-qwen", "complex-qwen"):
        task_class = load_task_classes()[name]
        assert task_class.fallback is None, name
        assert task_class.qwen_fallback_eligible is False, name
        assert task_class.allows_backend_fallback is False, name


def test_a_class_with_either_route_off_its_own_backend_allows_a_fallback():
    assert _claude_role_class(fallback=_qwen_fallback()).allows_backend_fallback is True
    assert _claude_role_class(qwen_fallback_eligible=True).allows_backend_fallback is True
    assert _claude_role_class().allows_backend_fallback is False


def test_a_fallback_naming_the_classs_own_backend_is_refused_when_the_config_loads():
    # A fallback to the backend whose quota is exhausted substitutes nothing and reads as if it
    # did, so it fails here rather than at the launch that believed it.
    with pytest.raises(ValidationError, match="own backend"):
        _claude_role_class(fallback=_qwen_fallback(backend="claude"))


@pytest.mark.parametrize("ceilings", [[], ["max_context", "max_context"], ["max_turns"]])
def test_a_fallbacks_ceiling_list_has_to_name_real_ceilings_once_each(ceilings):
    with pytest.raises(ValidationError):
        _qwen_fallback(ceilings=ceilings)


def test_a_fallback_with_no_ceiling_list_gets_all_three():
    # The list is optional in the config: a project that writes `backend` and `model` and nothing
    # else gets every ceiling the class declares, and never a substituted run nothing binds.
    assert RoleFallback(backend="qwen", model="qwen3.8-max").ceilings == list(
        agent_lib.CEILING_NAMES
    )


# ---- the decision: {allowed, exhausted, unknown} x {declared, not declared} x {fresh, stale} ----


@pytest.mark.parametrize(
    "verdict,age_seconds,declares_fallback,expected",
    [
        # An open quota launches Claude whatever the class declares: the fallback is a route off an
        # exhausted window, never a cheaper backend to prefer.
        ("allowed", 60.0, True, ("claude", "claude-opus-5", False)),
        ("allowed", 60.0, False, ("claude", "claude-opus-5", False)),
        # The case #425 exists for: exhausted, the verdict fresh, a fallback declared.
        ("exhausted", 60.0, True, ("qwen", "qwen3.8-max", True)),
        # Exhausted with nothing declared is the guard's page, not a launch's detour.
        ("exhausted", 60.0, False, ("claude", "claude-opus-5", False)),
        # Exhausted but stale reads as UNKNOWN, and unknown launches Claude: a start the quota then
        # refuses costs one page, a substitution on a stale verdict costs the review's independence.
        ("exhausted", VERDICT_TTL_SECONDS + 1, True, ("claude", "claude-opus-5", False)),
        # A verdict with no age at all cannot be fresh, so it cannot authorise a substitution.
        ("exhausted", None, True, ("claude", "claude-opus-5", False)),
        ("unknown", None, True, ("claude", "claude-opus-5", False)),
        ("unknown", None, False, ("claude", "claude-opus-5", False)),
    ],
)
def test_the_launch_decision_over_the_verdict_the_age_and_the_declaration(
    verdict, age_seconds, declares_fallback, expected
):
    task_class = _claude_role_class(fallback=_qwen_fallback() if declares_fallback else None)
    plan = role_launch_plan(
        task_class, verdict, age_seconds, verdict_ttl_seconds=VERDICT_TTL_SECONDS
    )
    assert (plan.backend, plan.model, plan.substituted) == expected


def test_a_verdict_exactly_at_its_ttl_is_still_believed():
    # The boundary is inclusive on purpose: the TTL is "how long an observation stays believable",
    # and a verdict observed exactly that long ago is the last one that is.
    plan = role_launch_plan(
        _claude_role_class(fallback=_qwen_fallback()),
        "exhausted",
        float(VERDICT_TTL_SECONDS),
        verdict_ttl_seconds=VERDICT_TTL_SECONDS,
    )
    assert plan.substituted is True and plan.backend == "qwen"


def test_the_substituted_run_carries_the_fallbacks_ceilings_and_the_other_its_classs():
    task_class = _claude_role_class(fallback=_qwen_fallback())
    substituted = role_launch_plan(
        task_class, "exhausted", 60.0, verdict_ttl_seconds=VERDICT_TTL_SECONDS
    )
    own = role_launch_plan(task_class, "allowed", 60.0, verdict_ttl_seconds=VERDICT_TTL_SECONDS)
    assert substituted.ceilings == ("max_context", "max_total_tokens")
    assert own.ceilings == tuple(agent_lib.CEILING_NAMES)


def test_every_plan_says_why_in_one_line_a_log_can_carry():
    for verdict, age in (("allowed", 60.0), ("exhausted", 60.0), ("unknown", None)):
        for declares in (True, False):
            plan = role_launch_plan(
                _claude_role_class(fallback=_qwen_fallback() if declares else None),
                verdict,
                age,
                verdict_ttl_seconds=VERDICT_TTL_SECONDS,
            )
            assert plan.reason and "\n" not in plan.reason and "\t" not in plan.reason
            assert plan.backend in plan.reason, plan.reason


# ---- reading the verdict the guard persisted ----------------------------------------------------


def test_the_verdict_comes_off_the_guards_own_file(tmp_path):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    path = _write_verdict(tmp_path, "exhausted", age=timedelta(minutes=12), now=now)
    verdict = read_persisted_quota_verdict("claude", cache_dir=tmp_path, now=now)
    assert verdict.status == "exhausted"
    assert verdict.source == path
    assert verdict.age_seconds == pytest.approx(12 * 60, abs=1)
    assert "12 min old" in verdict.reason


def test_no_file_on_disk_reads_as_unknown_never_as_allowed(tmp_path):
    verdict = read_persisted_quota_verdict("claude", cache_dir=tmp_path)
    assert verdict.status == "unknown"
    assert verdict.age_seconds is None
    assert str(tmp_path) in verdict.reason


def test_a_file_the_guard_wrote_before_it_persisted_a_verdict_reads_as_unknown(tmp_path):
    # The shape `.cache/agent_guard_qwen.json` had on disk the day this landed: bookkeeping, and no
    # `last_quota_status` in it. Reading that as `allowed` would be an invented permission.
    path = tmp_path / "agent_guard_claude.json"
    path.write_text(json.dumps({"commit_count": 6, "turn_count_at_commit": 5}))
    verdict = read_persisted_quota_verdict("claude", cache_dir=tmp_path)
    assert verdict.status == "unknown"
    assert "last_quota_status" in verdict.reason


def test_an_unparseable_verdict_file_reads_as_unknown_rather_than_raising(tmp_path):
    # A launch gate that raises turns a truncated write into a role that never runs at all, which
    # is the failure this whole issue exists to remove.
    (tmp_path / "agent_guard_claude.json").write_text('{"last_quota_status": "exhau')
    verdict = read_persisted_quota_verdict("claude", cache_dir=tmp_path)
    assert verdict.status == "unknown"
    assert "unreadable" in verdict.reason


def test_the_verdict_file_honours_the_same_cache_override_the_guard_does(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_CACHE_DIR", str(tmp_path / "sandbox"))
    assert quota_verdict_file("claude") == tmp_path / "sandbox" / "agent_guard_claude.json"
    assert read_persisted_quota_verdict("claude").status == "unknown"


def test_the_verdict_is_read_for_the_classs_own_backend(tmp_path):
    # Qwen's stream carries no comparable signal, so a Qwen class reads `allowed` always
    # (`quota_status` docstring) -- and its own bookkeeping file, not Claude's, is what it reads.
    _write_verdict(tmp_path, "exhausted", backend="qwen")
    assert read_persisted_quota_verdict("qwen", cache_dir=tmp_path).status == "exhausted"
    assert read_persisted_quota_verdict("claude", cache_dir=tmp_path).status == "unknown"


# ---- end to end, and the line the two drivers parse ---------------------------------------------


def test_role_launch_resolves_the_class_the_verdict_and_the_configured_ttl(tmp_path):
    name, plan, verdict = role_launch("validator", cache_dir=tmp_path)
    assert name == "validator"
    assert verdict.status == "unknown", "a test must not depend on the live .cache"
    assert (plan.backend, plan.substituted) == ("claude", False)

    _write_verdict(tmp_path, "exhausted")
    name, plan, verdict = role_launch("validator", cache_dir=tmp_path)
    assert verdict.status == "exhausted"
    assert (name, plan.backend, plan.model, plan.substituted) == (
        "validator",
        "qwen",
        load_task_classes()["validator"].fallback.model,
        True,
    )


def test_the_ttl_the_decision_uses_is_the_configured_one_not_a_literal(tmp_path):
    # Same verdict, same age, two configs: one whose TTL the verdict is inside and one whose it is
    # past. The answer moves with the file, which is what "a threshold lives in config" means.
    config = tmp_path / "agents.yaml"
    text = EXAMPLE_CONFIG.read_text()
    config.write_text(
        re.sub(
            r"^  quota_verdict_ttl_minutes: .*$",
            "  quota_verdict_ttl_minutes: 5",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    )
    assert load_mechanism(config).quota_verdict_ttl_minutes == 5
    _write_verdict(tmp_path, "exhausted", age=timedelta(minutes=30))
    _name, plan, _verdict = role_launch("validator", path=config, cache_dir=tmp_path)
    assert (plan.backend, plan.substituted) == ("claude", False)
    assert "past the 5 min TTL" in plan.reason

    _name, plan, _verdict = role_launch("validator", cache_dir=tmp_path)
    assert (plan.backend, plan.substituted) == ("qwen", True)


def test_the_role_backend_cli_prints_the_five_fields_a_driver_reads(tmp_path, monkeypatch, capsys):
    _write_verdict(tmp_path, "exhausted")
    out = _run_cli(monkeypatch, capsys, ["role-backend", "validator", "--cache-dir", str(tmp_path)])
    backend, model, substituted, ceilings, reason = out.rstrip("\n").split("\t")
    assert (backend, model, substituted) == ("qwen", "qwen3.8-max", "yes")
    assert ceilings == "max_context,max_total_tokens"
    assert "exhausted" in reason and "fallback" in reason


def test_the_role_backend_cli_answers_claude_on_an_open_quota(tmp_path, monkeypatch, capsys):
    _write_verdict(tmp_path, "allowed")
    out = _run_cli(monkeypatch, capsys, ["role-backend", "planner", "--cache-dir", str(tmp_path)])
    backend, model, substituted, _ceilings, _reason = out.rstrip("\n").split("\t")
    task_class = load_task_classes()["planner"]
    assert (backend, model, substituted) == (task_class.backend, task_class.model, "no")


def test_the_role_backend_cli_stops_on_a_role_no_single_class_claims(tmp_path, monkeypatch):
    # A driver reads this line into `read`: an empty answer would become an empty command line, so
    # the refusal has to be loud, exactly as `backend-executable`'s is (#380).
    monkeypatch.setattr(sys, "argv", ["agent_lib.py", "role-backend", "worker"])
    with pytest.raises(SystemExit) as exited:
        agent_lib.main()
    assert int(exited.value.code or 0) == 1


# ---- refine_queue_rank: which refine-needing issue the refiner should see first (#32) ----------


def _refine_row(number, *labels, body="x"):
    return {
        "number": number,
        "state": "OPEN",
        "labels": [{"name": n} for n in labels],
        "body": body,
    }


def test_refine_queue_rank_puts_the_issues_closest_to_dispatch_first():
    vocabulary = agent_lib.LabelVocabulary()
    auto_ready_parent = {vocabulary.auto_ready}
    open_numbers = {5, 14, 20, 30, 74, 83, 90}
    rows_and_parents = [
        # Newest, lowest priority, parent not opted in: last -- the order `gh issue list` gives.
        (_refine_row(83, vocabulary.priorities[3]), set()),
        (_refine_row(74, vocabulary.priorities[2]), auto_ready_parent),
        # Parent opted in, p1, but waiting on an open blocker.
        (_refine_row(30, vocabulary.priorities[0], body="x\n\nBlocked by #5\n"), auto_ready_parent),
        # Parent opted in, p1, a blocker that is already closed counts as no blocker.
        (_refine_row(20, vocabulary.priorities[0], body="x\n\nBlocked by #6\n"), auto_ready_parent),
        # The root of the critical path: parent opted in, p1, nothing blocking.
        (_refine_row(14, vocabulary.priorities[0]), auto_ready_parent),
        # No priority label at all sorts after every priority, and a failed parent lookup (None)
        # reads as "not opted in" rather than breaking the order.
        (_refine_row(90), None),
    ]
    ranked = sorted(
        rows_and_parents,
        key=lambda pair: agent_lib.refine_queue_rank(
            pair[0], parent_labels=pair[1], open_issue_numbers=open_numbers, labels=vocabulary
        ),
    )
    assert [row["number"] for row, _ in ranked] == [14, 20, 30, 74, 83, 90]


def test_refine_queue_rank_reads_the_priority_scale_from_config():
    vocabulary = agent_lib.LabelVocabulary(priorities=["urgent", "later"])
    high = _refine_row(40, "later")
    low = _refine_row(41, "urgent")
    ranked = sorted(
        [high, low],
        key=lambda row: agent_lib.refine_queue_rank(
            row, parent_labels=set(), open_issue_numbers=set(), labels=vocabulary
        ),
    )
    assert [row["number"] for row in ranked] == [41, 40]
