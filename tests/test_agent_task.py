"""`agent_os/bin/agent_task.sh` -- the one-shot role driver: what it resolves before it runs anything,
and the environment it hands the backend when it does.

Most of these tests go through `--dry-run`, which prints the resolved model, identity and prompt
and exits before minting a token or calling the backend
(agent_os/docs/adr/2026-09-14-a-pr-is-validated-by-a-validator-agent-against-the-issues-acceptance-
criteria.md). The last section runs the launch path itself against a stub backend -- see
`launch_environment` for how that stays offline, off the tracker and out of the real `.cache`.
Since #400 that launch is DETACHED, so nothing in that section can assert on a run it has not
first waited for: `_wait_for_run_to_end` is what turns "the driver returned" into "the run is
over", and the two tests after it are about the detach itself -- the run's own session, and a run
that finishes after the shell which launched it has been killed. The last two, added for #394,
are about what the run leaves behind whatever its backend exited with, holding the planner lock
the same way the kill test does so no test in this file ever opens the door to a real planner run.
Nothing in this file spends a turn.

Pure filesystem and subprocess. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import pathlib
import re
import shutil
import signal
import site
import subprocess
import tempfile
import time
import venv
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from conftest import EXAMPLE_CONFIG, config_with_never_run, config_with_no_host_text

from agent_os import guard as agent_guard
from agent_os.cli import AGENT_OS_DIR, host_root
from agent_os.lib import (
    PROMPTS_DIR,
    load_mechanism,
    load_project,
    load_role_class,
    never_run_rules,
    read_persisted_quota_verdict,
    read_role_run_exit_marker,
    role_app_slug,
    role_launch,
    role_run_exit_marker,
)

# The HOST project this suite runs inside: not a fixed nesting under AGENT_OS_DIR (that
# breaks the moment a copy IS the mechanism's own top directory, as the out-of-tree proof
# makes it -- #512), but whatever `host_root()` itself resolves: the git checkout's toplevel,
# same as every real driver run.
ROOT = host_root()
DRIVER = AGENT_OS_DIR / "bin" / "agent_task.sh"

ONE_SHOT_ROLES = ("validator", "refiner")

# The commands no role may run are ONE list in `project.never_run`, rendered into the worker's,
# the validator's and the refiner's RULES (agent_os/docs/AGENT_OS.md §7 row (b), #363).
# `tests/test_worker_task.py` asserts the worker's block reads it; these assert the other two.
NEVER_RUN_HEADING = "COMMANDS YOU MUST NEVER RUN"

# Neither the commands nor their reasons exist anywhere else in this repository, so whatever
# reaches a rendered block came from the list the test configured and from nothing left over in
# the driver's own text.
SENTINEL_COMMANDS = (
    ("sentinel-rebuild", "rebuilds the sentinel grid and moves every stamp it seals"),
    ("--sentinel-write", "persists the sentinel census; without it the command is a dry run"),
)

# A verb the real config forbids and the sentinel list does not: its absence is what proves
# neither block keeps a copy of its own -- the validator's copy had already lost one of the six.
DROPPED_COMMAND = "census-build"

# The launch gate reads the guard's persisted quota verdict off disk before it resolves a backend
# (#425), so every invocation here points `WORKER_CACHE_DIR` at a directory that holds no verdict:
# what these tests measure is the driver's own resolution, and a live `.cache` saying Claude is
# exhausted on the day the suite runs must not be what decides which backend it sees resolved.
# `cache_dir=` names a directory that does hold one. It lives outside the checkout (agent-os#25):
# it used to sit inside the real `.cache`, which is exactly where it must not write.
NO_VERDICT_CACHE_DIR = tempfile.mkdtemp(prefix="no-quota-verdict-for-tests-")
atexit.register(shutil.rmtree, NO_VERDICT_CACHE_DIR, ignore_errors=True)


def _write_quota_verdict(directory, status: str, *, age_minutes: float = 0.0) -> pathlib.Path:
    """The guard's own bookkeeping file, as `agent_guard._save_bookkeeping` writes it, aged by
    moving its mtime back -- the mtime IS the observation time, because every tick that watches a
    run of that backend re-derives `last_quota_status` and rewrites the whole file."""
    path = pathlib.Path(directory) / "agent_guard_claude.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "commit_count": 0,
                "turn_count_at_commit": 0,
                "warned_at_turn_count": None,
                "last_quota_status": status,
            }
        )
    )
    observed_at = time.time() - (age_minutes * 60)
    os.utime(path, (observed_at, observed_at))
    return path


def _run(
    *arguments, config_path=None, cache_dir=NO_VERDICT_CACHE_DIR
) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    if config_path is not None:
        environment["AGENTS_CONFIG_PATH"] = str(config_path)
    environment["WORKER_CACHE_DIR"] = str(cache_dir)
    return subprocess.run(
        ["bash", str(DRIVER), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _rules(role, subject="1", config_path=None, cache_dir=NO_VERDICT_CACHE_DIR) -> str:
    """One role's resolved RULES, read off its `--dry-run`: printed and nothing else -- no token
    minted, no backend called, no tracker touched."""
    result = _run(role, subject, "--dry-run", config_path=config_path, cache_dir=cache_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.split("--- rules ---", 1)[1]


def _flattened(rules) -> str:
    """The rules with their line breaks reduced to single spaces: `never_run_rules` wraps a bullet
    at the prompt's own width, and what a test asserts is a command beside its reason, not where
    the line happened to break."""
    return " ".join(rules.split())


def test_dry_run_resolves_the_validators_class_identity_and_prompt():
    name, task_class = load_role_class("validator")
    result = _run("validator", "352", "woken by worker_finished on #347", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"role:      validator (class {name})" in result.stdout
    assert f"model:     {task_class.model}" in result.stdout
    # The validator must not be the pull request's own author: it signs as the planner's App
    # until one of its own exists, and that is config, not a literal in the driver.
    assert f"identity:  {role_app_slug('validator')}" in result.stdout
    assert "subject:   #352" in result.stdout
    assert "woken by worker_finished on #347" in result.stdout


def test_the_validators_rules_carry_the_review_contract():
    result = _run("validator", "352", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    rules = result.stdout.split("--- rules ---", 1)[1]
    assert "gh pr review <pr> --approve" in rules
    assert "--request-changes" in rules
    assert '"$AGENT_OS_PYTHON" -m agent_os.issues move N review' in rules
    assert '"$AGENT_OS_PYTHON" -m agent_os.issues move N blocked-on-human' in rules
    # The three things it may never do, each one a line in the injected block.
    assert "You never edit, stage or commit a file anywhere" in rules
    assert "You never merge" in rules


def test_the_validators_rules_check_one_stage_commit_per_checklist_line():
    # Staged execution (#375): approving a PR means checking its branch closed every stage the
    # issue's own `## Stages` checklist names, one `stage N/M:` commit each, last N == M.
    result = _run("validator", "352", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    rules = result.stdout.split("--- rules ---", 1)[1]
    assert "stage N/M:" in rules
    assert "## Stages" in rules


# -------------------------------------------------------------------------------------------------
# The RULES name the worktree the driver prepared (#393). The run reviewed in
# .cache/validator/20260916T182534Z.log was told to make its own throwaway worktree, and the first
# thing it did to make that worktree runnable was symlink the MAIN checkout's `.venv` into it: a
# venv whose editable install points at the main checkout's package, so the suite it then ran
# measured one tree while its review reported on another. What the block must do now is name the
# prepared worktree, name the variable that keeps it resolving there, and never mention a command
# that builds or populates one -- not even to forbid it, so that "absent" stays a checkable claim.
# -------------------------------------------------------------------------------------------------

BUILD_OR_POPULATE_A_WORKTREE = (
    "git worktree add",
    "git worktree remove",
    "gh pr checkout",
    "ln -s",
    "cp -r",
)


def test_the_validators_rules_name_the_worktree_the_driver_prepared_not_one_it_builds_itself():
    rules = _flattened(_rules("validator"))

    # The worktree is the driver's, and the prompt names the one THIS run prepared. A dry run
    # prepares none and says so, rather than leaving the placeholder in the text.
    assert "the throwaway worktree the driver prepared for this run" in rules
    assert "Its path is none -- a dry run prepares no worktree" in rules
    assert "__WORKTREE__" not in rules
    # The variable whose loss silently changes what a suite measures, and the check that catches it.
    assert "Never drop `PYTHONPATH`" in rules
    assert 'echo "$PYTHONPATH"' in rules
    # Nothing left that builds a worktree or links a venv into one, and the prohibition itself.
    for command in BUILD_OR_POPULATE_A_WORKTREE:
        assert command not in rules, command
    assert "nothing that links or copies this checkout's own environment" in rules


def test_the_refiners_rules_never_mention_a_worktree_or_a_pythonpath_it_does_not_get():
    # The refiner writes issues and never runs code, so it is the one role the driver launches with
    # no worktree: a rule naming one would send it looking for a tree that was never made.
    rules = _rules("refiner")
    assert "worktree" not in rules.lower()
    assert "PYTHONPATH" not in rules


def test_dry_run_resolves_the_refiners_class_identity_and_prompt():
    # Replaces test_the_refiner_is_accepted_as_a_role_but_refuses_to_run (#349 now has a contract):
    # the refiner is a real role, resolved and printed exactly like the validator's dry run.
    name, task_class = load_role_class("refiner")
    result = _run("refiner", "349", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"role:      refiner (class {name})" in result.stdout
    assert f"model:     {task_class.model}" in result.stdout
    assert f"identity:  {role_app_slug('refiner')}" in result.stdout
    assert "subject:   #349" in result.stdout


def test_the_refiners_rules_carry_the_split_vs_rewrite_contract():
    result = _run("refiner", "349", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    rules = result.stdout.split("--- rules ---", 1)[1]
    # The two shapes it must choose between, and the loop-safety marker.
    assert "rewrite its body" in rules
    assert "create sub-issues" in rules
    assert "<!-- refiner-summary -->" in rules
    assert '"$AGENT_OS_PYTHON" -m agent_os.issues move N blocked-on-human' in rules
    # It never sets status:ready itself -- only the planner or the human does
    # (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
    # state.md).
    assert "You never set `status:ready`" in rules
    assert "--body-file" in rules


def test_the_refiners_rules_carry_the_staged_execution_contract():
    # Staged execution (#375): the refiner writes `## Stages` into every body, on its own
    # judgment, and knows a stage is sized for a fresh process, not for how much context it has.
    result = _run("refiner", "349", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    rules = result.stdout.split("--- rules ---", 1)[1]
    assert "## Stages" in rules
    assert "fresh process" in rules


def test_the_refiner_accepts_no_wake_alongside_dry_run():
    # --dry-run exits before --no-wake's own branch is reached, but the flag must still parse and
    # never change what a dry run prints.
    result = _run("refiner", "349", "--dry-run", "--no-wake")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--- rules ---" in result.stdout


def test_a_role_with_no_rules_block_is_refused_rather_than_run_empty():
    result = _run("planner", "12", "--dry-run")
    assert result.returncode == 2
    assert "agent_os/bin/planner_task.sh" in result.stdout


def test_the_subject_must_be_a_number():
    result = _run("validator", "not-a-pr", "--dry-run")
    assert result.returncode == 2
    assert "the subject is a number" in result.stdout


def test_the_planner_is_told_to_launch_the_validator_and_what_a_review_means():
    # Grep-level on purpose: the rules are a prompt, and what this asserts is that the three
    # mechanical parts of the loop are stated in it -- who launches the validator, on what, and
    # what each verdict makes the planner do next.
    rules = (PROMPTS_DIR / "planner.md").read_text()
    assert "agent_os/bin/agent_task.sh validator <pr>" in rules
    assert "status:ai-completed" in rules
    assert "resume --after manual --context" in rules
    assert "counts as an attempt under the cap below" in rules


def test_the_planner_is_told_to_launch_the_refiner_and_what_finishing_it_means():
    # Same grep-level check for the refiner's own loop: woken by refine_pending, launches at most
    # one refiner run, checks for a prior summary before relaunching -- and, since #365, does NOT
    # run `promote-refined` on the way back: the guard's tick performs it on every fire, so the
    # one step the docs call mechanical no longer depends on an LLM remembering it.
    rules = (PROMPTS_DIR / "planner.md").read_text()
    assert "refine_pending" in rules
    assert "agent_os/bin/agent_task.sh refiner <N>" in rules
    assert "<!-- refiner-summary -->" in rules
    assert "promote-refined" not in rules
    assert "refiner_finished" in rules


def test_the_planner_is_told_what_an_orphan_doing_event_means():
    # The event the tick raises for an issue the board says is running while no worker is (#365):
    # the guard detects it, the planner decides -- back to ready, or a question for the human.
    rules = (PROMPTS_DIR / "planner.md").read_text()
    assert "orphan_doing" in rules
    assert '"$AGENT_OS_PYTHON" -m agent_os.issues move <N> ready' in rules
    assert "move <N> blocked-on-human" in rules


def test_the_resolved_rules_tell_every_role_to_mention_the_human_by_name():
    """A question for the human is a mention, and the login comes from `config/agents.yaml` --
    no script under the mechanism spells a person's name, so this asserts the substitution, not
    the literal."""
    login = load_project().human_login
    assert login, "config/agents.yaml names no project.human_login"
    validator = _run("validator", "352", "--dry-run").stdout.split("--- rules ---", 1)[1]
    refiner = _run("refiner", "349", "--dry-run").stdout.split("--- rules ---", 1)[1]
    planner = subprocess.run(
        ["bash", str(AGENT_OS_DIR / "bin" / "planner_task.sh"), "rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for role, rules in (("validator", validator), ("refiner", refiner), ("planner", planner)):
        assert f"@{login}" in rules, f"the {role}'s resolved RULES never mention the human"
        assert "__HUMAN_LOGIN__" not in rules, f"the {role}'s placeholder was not substituted"


def test_the_resolved_rules_carry_the_writing_to_the_human_paragraph_in_the_configured_language():
    """A doubt reaches the human in their own language and in functional terms
    (agent_os/docs/adr/2026-09-15-a-question-for-the-human-is-written-in-their-language-and-in-functional-
    terms.md). The wording lives once, in `agent_lib.human_message_rules`, and every role's RULES
    injects it through the `__HUMAN_MESSAGE_RULES__` placeholder -- this asserts the substitution
    landed and left no placeholder behind, not the literal wording (that is
    `test_human_message_rules_*` in tests/test_agent_lib.py)."""
    language = load_project().human_language
    assert language, "config/agents.yaml names no project.human_language"
    validator = _run("validator", "352", "--dry-run").stdout.split("--- rules ---", 1)[1]
    refiner = _run("refiner", "349", "--dry-run").stdout.split("--- rules ---", 1)[1]
    planner = subprocess.run(
        ["bash", str(AGENT_OS_DIR / "bin" / "planner_task.sh"), "rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for role, rules in (("validator", validator), ("refiner", refiner), ("planner", planner)):
        assert "WRITING TO THE HUMAN" in rules, f"the {role}'s resolved RULES lack the paragraph"
        assert language in rules, f"the {role}'s resolved RULES never name {language}"
        assert "__HUMAN_MESSAGE_RULES__" not in rules, (
            f"the {role}'s placeholder was not substituted"
        )


def test_the_validator_and_the_refiner_render_the_same_configured_never_run_list(tmp_path):
    """Both blocks read `project.never_run`: a list this repository does not ship reaches each of
    them whole, every command with its own reason beside it, and a verb the list dropped is named
    in neither. That is the drift one list prevents -- the validator's own copy had already lost a
    verb the other two blocks still carried."""
    config = config_with_never_run(tmp_path, SENTINEL_COMMANDS)
    expected = never_run_rules(load_project(config))
    assert expected, "a non-empty project.never_run rendered no paragraph at all"

    for role in ONE_SHOT_ROLES:
        rules = _rules(role, config_path=config)
        assert NEVER_RUN_HEADING in rules, role
        assert expected in rules, role
        assert "__NEVER_RUN_RULES__" not in rules, role
        flattened = _flattened(rules)
        for command, reason in SENTINEL_COMMANDS:
            assert f"`{command}` -- {reason}" in flattened, f"{role}: {command}"
        assert DROPPED_COMMAND not in rules, role


def test_both_roles_carry_every_command_the_real_config_forbids():
    """What config/agents.yaml forbids is what the two roles read, item by item: a command the
    config carries and a block loses is a command that role has no stated reason not to run."""
    configured = load_project().never_run
    assert configured, "config/agents.yaml forbids no command at all"

    for role in ONE_SHOT_ROLES:
        rules = _rules(role)
        assert never_run_rules(load_project()) in rules, role
        flattened = _flattened(rules)
        for item in configured:
            assert f"`{item.command}` -- {item.reason}" in flattened, f"{role}: {item.command}"


def test_a_project_that_forbids_no_command_gets_no_paragraph_in_either_role(tmp_path):
    """An empty `project.never_run` renders no paragraph at all -- not a heading with nothing under
    it, and not a gap twice as wide as the one it filled -- while the role's own contract around it
    stays exactly as it was: what disappears is the project's list, never the mechanism's rules."""
    config = config_with_never_run(tmp_path, [])
    assert never_run_rules(load_project(config)) == ""

    for role in ONE_SHOT_ROLES:
        rules = _rules(role, config_path=config)
        assert NEVER_RUN_HEADING not in rules, role
        assert "__NEVER_RUN_RULES__" not in rules, role
        assert "\n\n\n" not in rules, role
        assert "You never edit, stage or commit a file anywhere" in rules, role
        assert "You never merge" in rules, role


# ---------------------------------------------------------------------------------------------
# No host-project literal in either block's OWN text. `project.never_run` is the one paragraph that
# legitimately carries this project's verbs and its frozen stamp, because it is rendered from that
# project's config -- so these render it empty and assert on what is left, which is the mechanism's
# own contract and nothing else (#363,
# agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
# ---------------------------------------------------------------------------------------------

# What the refiner's collision bullet cites now: the SHAPE of a project rule, where it used to name
# roedor's own frozen `m2` stamp -- an example a second project's refiner cannot act on, and one
# this project's refiner would keep reading after the freeze it names had been lifted.
HOST_NEUTRAL_RULE_EXAMPLE = "a freeze the project declares, a file it protects"

# Strings that belong to the host project and to nothing the mechanism can know: its frozen stamp,
# the port its shared server listens on, its own pipeline's nouns, its importable package, its
# database fixture, the names of its own worker classes, and the virtualenv layout only a Python
# host has. The last five were still inside the mechanism's own prompts until #509 moved them into
# files the host owns (`agent_os/docs/AGENT_OS.md` §7 row (t)).
HOST_LITERALS = (
    "m2",
    "5435",
    "census",
    "curated",
    "Postgres",
    "roedor",
    "db_sandbox",
    "mechanical-qwen",
    "complex-qwen",
    ".venv",
)

# Every role whose prompt is a template, and the driver that resolves it: the worker's and the
# planner's each print theirs with `rules`, the two one-shot roles with `--dry-run`.
ROLES_WITH_A_PROMPT = ("worker", "validator", "refiner", "planner")

# What a template still carries that belongs to a host, measured rather than passed over. #509 left
# the validator's `.venv/bin/ruff` bullet and its `.venv` prohibition here; agent-os#41 moved the
# first into `project.lint_commands` and made the second name no layout, so none is left -- an
# entry added back here is a host literal the mechanism has started shipping again.
LITERALS_A_TEMPLATE_STILL_CARRIES: dict[str, tuple[str, ...]] = {}


def _role_rules(role, config_path, cache_dir=NO_VERDICT_CACHE_DIR) -> str:
    """One role's resolved prompt, whichever driver owns it -- all four print it and run nothing."""
    environment = dict(os.environ)
    environment["AGENTS_CONFIG_PATH"] = str(config_path)
    environment["WORKER_CACHE_DIR"] = str(cache_dir)
    if role == "worker":
        command = ["bash", str(AGENT_OS_DIR / "bin" / "worker_task.sh"), "claude", "rules"]
    elif role == "planner":
        command = ["bash", str(AGENT_OS_DIR / "bin" / "planner_task.sh"), "rules"]
    else:
        command = ["bash", str(DRIVER), role, "1", "--dry-run"]
    result = subprocess.run(
        command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if role in ONE_SHOT_ROLES:
        return result.stdout.split("--- rules ---", 1)[1]
    return result.stdout


def test_the_refiner_cites_the_shape_of_a_project_rule_instead_of_one_of_its_own(tmp_path):
    """The collision rule survives -- it is the mechanism's -- while the EXAMPLE becomes the
    project's own business: what the refiner is pointed at is `AGENTS.md`, which is exactly the file
    a second project rewrites when it adopts this mechanism."""
    rules = _rules("refiner", config_path=config_with_never_run(tmp_path, []))
    flattened = _flattened(rules)

    assert "If the work collides with a rule in `AGENTS.md`" in flattened
    assert HOST_NEUTRAL_RULE_EXAMPLE in flattened


def test_no_roles_rendered_prompt_names_anything_of_this_project(tmp_path):
    """#509's own criterion, over every role rather than the two this file used to reach before it:
    with the host's lists and its extra-prompt files emptied, what a worker, a validator, a refiner
    and a planner read is the mechanism's own contract. Every host literal left in a rendered
    prompt would be one a second project has to edit a file under `agent_os/` to remove, which is
    the thing the tracking epic exists to end.

    The one string subtracted first is the main checkout's own path, which the worker's prompt
    names: that is a path the driver DERIVES from git, so a second host reads its own there, and
    this checkout's happening to have `roedor` in it says nothing about the mechanism's text."""
    config = config_with_no_host_text(tmp_path)
    assert never_run_rules(load_project(config)) == ""
    main_checkout = str(
        pathlib.Path(
            subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        ).parent
    )

    for role in ROLES_WITH_A_PROMPT:
        rules = _role_rules(role, config).replace(main_checkout, "<the host checkout>")
        known = LITERALS_A_TEMPLATE_STILL_CARRIES.get(role, ())
        for literal in HOST_LITERALS:
            if literal in known:
                # An exemption that has stopped being true is an assertion nobody is making any
                # more, so it fails here rather than sitting in the file forever.
                assert literal in rules, f"{role}: {literal} is exempt and no longer there"
                continue
            assert literal not in rules, f"{role}: {literal}"


# ---------------------------------------------------------------------------------------------
# The launch path itself (#393): the driver makes the throwaway worktree a role that runs tests
# needs, exports PYTHONPATH at it -- which is what makes the main checkout's venv, whose editable
# install points at the main checkout's package, resolve the worktree's own copy -- and removes it
# on every exit path. These are the only tests in this file that get past `--dry-run`, and they
# reach nothing: AGENT_CLAUDE_BIN is a stub that records the environment it was handed and exits,
# AGENT_CACHE_DIR moves every write under tmp_path, the config's secrets_dir points at nothing so
# no GitHub App token is minted, and --no-wake keeps the planner event and the wake unwritten.
# ---------------------------------------------------------------------------------------------

BACKEND_STUB = """#!/usr/bin/env bash
# Stands in for the claude CLI. Everything a test asserts about the worktree is recorded HERE,
# while the run is live: once the driver exits, the worktree is gone by design. The system prompt
# goes to its own file, because what the run was TOLD about that worktree is the other half of
# what the driver owes it (#393).
prompt=""
while [ $# -gt 0 ]; do
  if [ "$1" = --append-system-prompt ]; then
    prompt=${2-}
    shift
  fi
  shift
done
printf '%s' "$prompt" > "$STUB_PROMPT"
# What a test can read WHILE this run is live, and the delay that keeps it live long enough to read
# it: the session this stub is in -- the detached run's own, which is the whole of #400 -- and the
# marker saying the run has started, so a test kills the shell that launched it at a moment it
# chooses instead of racing for one. Both are opt-in; only the tests that need them set them.
if [ -n "${STUB_SESSION_FILE-}" ]; then
  printf 'SID=%s\\nPGID=%s\\nPPID=%s\\n' \\
    "$(ps -o sid= -p $$ | tr -d ' ')" \\
    "$(ps -o pgid= -p $$ | tr -d ' ')" \\
    "$(ps -o ppid= -p $$ | tr -d ' ')" > "$STUB_SESSION_FILE"
fi
if [ -n "${STUB_SLEEP-}" ]; then sleep "$STUB_SLEEP"; fi
# The backend's own stream-json, when a test hands it one: what the run log then carries is the
# record the guard reads a role's quota off (#429). Opt-in like the two above.
if [ -n "${STUB_STREAM-}" ]; then cat "$STUB_STREAM"; fi
# The worktree is PYTHONPATH's first entry: in a host that vendors the mechanism the driver exports
# the worktree's copy of the mechanism after it (agent-os#35).
worktree=${PYTHONPATH%%:*}
{
  printf 'PYTHONPATH=%s\\n' "${PYTHONPATH-}"
  printf 'WORKTREE=%s\\n' "$worktree"
  printf 'PWD=%s\\n' "$PWD"
  printf 'WORKTREE_GIT=%s\\n' "$([ -e "$worktree/.git" ] && echo yes || echo no)"
  printf 'WORKTREE_PYTEST=%s\\n' "$([ -x "$worktree/.venv/bin/pytest" ] && echo yes || echo no)"
  printf 'WORKTREE_ENV_FILE=%s\\n' "$([ -e "$worktree/.env" ] && echo yes || echo no)"
  printf 'WORKTREE_PROVISIONED=%s\\n' "$(cat "$worktree/provisioned-by-setup" 2>/dev/null || echo no)"
  printf 'WORKTREE_HEAD=%s\\n' "$(git -C "$worktree" rev-parse HEAD 2>/dev/null || echo none)"
  printf 'WORKTREE_COMMON_DIR=%s\\n' \\
    "$(git -C "$worktree" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo none)"
  printf 'SCRATCH=%s\\n' "${AGENT_RUN_SCRATCH-}"
  printf 'SCRATCH_IS_EMPTY_DIR=%s\\n' \\
    "$([ -d "${AGENT_RUN_SCRATCH-}" ] && [ -z "$(ls -A "$AGENT_RUN_SCRATCH")" ] && echo yes || echo no)"
} > "$STUB_RECORD"
# A scratch file of the role's own, the way agent-os#33's refiner wrote its draft bodies: the
# driver removes the directory whatever it holds.
if [ -d "${AGENT_RUN_SCRATCH-}" ]; then printf 'draft\\n' > "$AGENT_RUN_SCRATCH/draft.md"; fi
if [ -n "${STUB_RESOLVE_PACKAGE-}" ]; then
  # The tree the package lives in, relative to the worktree's root: `.` for a package of the
  # host's own, the mechanism's own directory for the mechanism's package in a vendoring host. Its
  # venv is the one linked there; the driver's interpreter when there is none, which is the one a
  # role falls back to (`$AGENT_OS_PYTHON`), and the record says which one answered.
  tree=$worktree/${STUB_PACKAGE_TREE:-.}
  if [ -x "$tree/.venv/bin/python" ]; then
    python="$tree/.venv/bin/python"
    printf 'PACKAGE_PYTHON=linked\\n' >> "$STUB_RECORD"
  else
    python="$AGENT_OS_PYTHON"
    printf 'PACKAGE_PYTHON=driver\\n' >> "$STUB_RECORD"
  fi
  # A marker only this worktree's copy of the package carries, dropped while the run is live: the
  # worktree is gone by the time a test reads the record, so the measurement has to happen here.
  # Dropped ONLY into a path the driver itself named a throwaway worktree -- a driver that exported
  # some real checkout's path has to fail the test, not leave a file behind in that checkout.
  case $worktree in
    */worktree-pr*)
      printf 'dropped by the stub backend\\n' > "$tree/$STUB_RESOLVE_PACKAGE/marker.txt"
      ;;
  esac
  {
    "$python" "$STUB_RESOLVER" "$STUB_RESOLVE_PACKAGE"
    # The control: the SAME interpreter with the driver's export taken away.
    env -u PYTHONPATH "$python" "$STUB_RESOLVER" "$STUB_RESOLVE_PACKAGE" | sed 's/^/INHERITED_/'
  } >> "$STUB_RECORD"
fi
exit "${STUB_EXIT_CODE:-0}"
"""

PACKAGE_RESOLVER = '''\
"""Prints where the venv resolves a package from, and whether the marker only one tree's copy
carries is visible at the place it resolved. The stub runs it twice: with the environment the
driver exported, and with that PYTHONPATH taken away -- the second answer is what the 2026-09-16
validator's suite ran on (#393).

Nothing of either tree is on sys.path here: [0] is this file's own directory under tmp_path, so the
answer comes from PYTHONPATH and from the venv's editable install alone. Those are the same two
that decide it for a suite, because pytest puts `tests/` on sys.path and never the root -- `cd`
alone is not enough (docs/modules/workers.md). A `python -c` run from the worktree's root would
have answered "the worktree" by accident, through sys.path[0], and proved nothing.
"""

import importlib.util
import os
import sys

package = sys.argv[1]
spec = importlib.util.find_spec(package)
origin = spec.origin if spec and spec.origin else ""
marker = os.path.join(os.path.dirname(origin), "marker.txt") if origin else ""
print("EXECUTABLE=" + sys.executable)
print("ORIGIN=" + (origin or "none"))
print("MARKER=" + ("yes" if marker and os.path.exists(marker) else "no"))
'''

GIT_STUB = """#!/usr/bin/env python3
# Stands in for `git` on the two calls that would reach origin -- `ls-remote` answers with the
# commit the test named as the pull request's head, and `fetch` succeeds without fetching it -- and
# hands every other subcommand to the real git. The driver resolves that head through `ls-remote`
# rather than through FETCH_HEAD, so nothing here has to write into the shared repository's own
# scratch files for the answer to reach it.
import os
import subprocess
import sys

arguments = sys.argv[1:]
if "ls-remote" in arguments or "fetch" in arguments:
    with open(os.environ["GIT_STUB_CALLS"], "a") as calls:
        calls.write(" ".join(arguments) + "\\n")
    if "ls-remote" in arguments:
        print(os.environ["GIT_STUB_PR_HEAD"] + "\\trefs/pull/352/head")
    sys.exit(0)
sys.exit(subprocess.call([os.environ["REAL_GIT"], *arguments]))
"""

PULL_REQUEST = "352"


def _config_without_secrets(tmp_path) -> pathlib.Path:
    """`config.example.yaml` with `project.secrets_dir` pointed at nothing, so
    `agent_apply_identity` degrades to its warning instead of minting a GitHub App token -- the one
    step of the launch path that would otherwise reach the network."""
    text = EXAMPLE_CONFIG.read_text()
    patched, count = re.subn(
        r"^  secrets_dir: .*$",
        "  secrets_dir: .secrets/no-such-app",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "config.example.yaml's project.secrets_dir line changed shape"
    path = tmp_path / "agents-no-secrets.yaml"
    path.write_text(patched)
    return path


def _disposable_copy_of_the_host(destination: pathlib.Path) -> pathlib.Path:
    """A git repository of its own holding this checkout's tracked files as they stand -- this
    change's drivers and package included, not whatever the last commit holds -- laid out exactly
    as the checkout is, with its gitignored `.venv` and `.env` linked in the way a worktree gets
    them.

    The launch path makes a worktree with `git -C <host root> worktree add`, which registers it
    under the gitdir of that host. Run with the checkout under test as its host, every launch here
    registered a worktree in the REAL repository -- shared with every other checkout of it -- and a
    run that never reached its own removal (a killed test, a detached run outliving its fixture,
    pytest pruning an old tmp_path) left it listed there for good (#66). Run out of this copy, it
    is registered here and goes away with tmp_path."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    for relative in filter(None, listed):
        source = ROOT / relative
        target = destination / relative
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(destination)], capture_output=True, check=True
    )
    _git(destination, "add", "-A")
    _git(destination, "commit", "-q", "--no-verify", "-m", "a disposable copy of the host")

    # Linked after the commit, like the gitignored things `git worktree add` never brings: the
    # host's own venv and `.env`, and in a host that vendors the mechanism, the mechanism's venv.
    place = _the_mechanisms_place_in_the_host()
    links = [pathlib.PurePath(".venv"), pathlib.PurePath(".env")]
    if place is not None:
        links.append(place / ".venv")
    for relative in links:
        if (ROOT / relative).exists():
            (destination / relative).symlink_to(ROOT / relative)
    return destination


def _host(environment) -> pathlib.Path:
    """The disposable host a `launch_environment` runs out of."""
    return pathlib.Path(environment["AGENT_OS_HOST_ROOT"])


def _host_script(environment, name: str = "agent_task.sh") -> pathlib.Path:
    """The host copy's own driver: the mechanism sits at the same place in that copy as in this
    checkout, so the driver resolves the copy as its host and its package directory inside it."""
    place = _the_mechanisms_place_in_the_host()
    mechanism = _host(environment) if place is None else _host(environment) / place
    return mechanism / "bin" / name


@pytest.fixture
def launch_environment(tmp_path):
    """The driver's real launch path with every write moved somewhere disposable -- the worktree's
    registration included, which is why the driver runs out of a disposable copy of the host
    (#66) -- and a PYTHONPATH of its own: a sentinel rather than an empty value, because what these
    tests assert is that the driver REPLACED what it inherited with the worktree -- an inherited
    PYTHONPATH already pointing at a checkout of this repository would let a driver that exports
    nothing pass by accident."""
    host = _disposable_copy_of_the_host(tmp_path / "host")
    cache = tmp_path / "cache"
    cache.mkdir()
    # Where the launch gate looks for the guard's persisted quota verdict (#425): empty, so a
    # launch here resolves the class's own backend unless a test writes a verdict into it.
    guard_state = tmp_path / "guard-state"
    guard_state.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "backend"
    stub.write_text(BACKEND_STUB)
    stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        AGENT_CACHE_DIR=str(cache),
        WORKER_CACHE_DIR=str(guard_state),
        AGENT_CLAUDE_BIN=str(stub),
        AGENT_WORKTREE_REF="HEAD",
        # Named outright rather than left to the driver to derive, so no launch here can resolve
        # the checkout under test as its host.
        AGENT_OS_HOST_ROOT=str(host),
        AGENTS_CONFIG_PATH=str(_config_without_secrets(tmp_path)),
        STUB_RECORD=str(tmp_path / "backend-record.txt"),
        STUB_PROMPT=str(tmp_path / "backend-prompt.txt"),
        STUB_EXIT_CODE="0",
        PYTHONPATH=str(tmp_path / "inherited-sentinel"),
    )
    return environment


@pytest.fixture
def stubbed_origin(launch_environment, tmp_path):
    """`launch_environment` with `git` stubbed on PATH and AGENT_WORKTREE_REF taken away, so the
    DEFAULT resolution -- `ls-remote` for the pull request's head, then `fetch` of it -- runs
    offline and leaves a record of what it asked origin for."""
    real_git = shutil.which("git")
    assert real_git, "no git on PATH to stand in for"
    head = subprocess.run(
        [real_git, "-C", str(_host(launch_environment)), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    binaries = tmp_path / "origin-bin"
    binaries.mkdir()
    stub = binaries / "git"
    stub.write_text(GIT_STUB)
    stub.chmod(0o755)

    launch_environment.update(
        PATH=f"{binaries}:{launch_environment['PATH']}",
        REAL_GIT=real_git,
        GIT_STUB_CALLS=str(tmp_path / "git-calls.txt"),
        GIT_STUB_PR_HEAD=head,
    )
    del launch_environment["AGENT_WORKTREE_REF"]
    return launch_environment


RUN_TIMEOUT_SECONDS = 180.0


def _launch(environment, role, subject=PULL_REQUEST, *, no_wake=True):
    arguments = [role, subject, *(["--no-wake"] if no_wake else [])]
    result = subprocess.run(
        ["bash", str(_host_script(environment)), *arguments],
        cwd=_host(environment),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    # The driver returns as soon as the run is DETACHED (#400), so everything a test asserts about
    # what the run did -- the record the stub wrote, the runs.tsv row, the worktree's removal --
    # needs the run to have ended first, and this is the one place that waits for it.
    pid = _detached_pid(result.stdout)
    if pid is not None:
        _wait_for_run_to_end(pid)
    return result


def _wait_until(condition, what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {timeout}s")
        time.sleep(0.05)


def _wait_for_file(path: pathlib.Path, timeout: float = 60.0) -> None:
    _wait_until(path.exists, f"{path.name} to be written", timeout)


def _run_is_alive(pid: int) -> bool:
    """Dead means gone, and a zombie counts as gone: the detached run is no child of this process,
    so nothing here reaps it, and a corpse nobody has buried yet must not read as a live run."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        # Gone before the open: the directory entry itself was already reaped.
        return False
    except ProcessLookupError:
        # Gone mid-read: procfs raises ESRCH, not ENOENT, for a process that exits between the
        # open and the read. Same fact -- not alive -- reached through the other syscall's errno.
        return False
    return stat.rsplit(") ", 1)[1].split()[0] != "Z"


def _wait_for_run_to_end(pid: int, timeout: float = RUN_TIMEOUT_SECONDS) -> None:
    _wait_until(lambda: not _run_is_alive(pid), f"the detached run (pid {pid}) to end", timeout)


def _detached_pid(stdout: str) -> int | None:
    """The PID the driver printed for the run it detached, or None when it never got that far."""
    match = re.search(r"^detached:\s+pid (\d+)", stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def _printed_path(stdout: str, label: str) -> pathlib.Path | None:
    match = re.search(rf"^{label}\s+(\S.*)$", stdout, flags=re.MULTILINE)
    return pathlib.Path(match.group(1).strip()) if match else None


def _pidfile(stdout: str) -> pathlib.Path:
    path = _printed_path(stdout, "pidfile:")
    assert path is not None, f"the driver printed no PID file:\n{stdout}"
    return path


def _run_log(stdout: str) -> str:
    """The run's own log: the header the driver wrote before detaching, and everything the detached
    half wrote into the same file after it -- the backend's stream-json and the driver's own lines
    from inside that session, which is where a message like the worktree's removal now lands."""
    path = _printed_path(stdout, "log")
    assert path is not None, f"the driver printed no log path:\n{stdout}"
    return path.read_text()


def _session(environment) -> dict[str, str]:
    """What the stub recorded about the session it ran in, while the run was still live."""
    path = pathlib.Path(environment["STUB_SESSION_FILE"])
    _wait_for_file(path)
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _record(environment) -> dict[str, str]:
    """What the stub backend saw while the run was live, as key=value lines."""
    path = pathlib.Path(environment["STUB_RECORD"])
    assert path.exists(), "the stub backend never ran"
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _prompt(environment) -> str:
    """The RULES the stub backend was actually handed, off its own `--append-system-prompt`."""
    path = pathlib.Path(environment["STUB_PROMPT"])
    assert path.exists(), "the stub backend never ran"
    return path.read_text()


def _the_projects_package() -> str | None:
    """A top-level package of the HOST's that the driver's export has to resolve from the worktree,
    found rather than named: the driver exports PYTHONPATH at a tree and knows no package name,
    and neither does this test
    (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
    `glob` and not `iterdir` plus a stat: the repository root holds a directory this process has no
    read access to, and finding the package does not need one.

    None when there is nothing to measure (agent-os#17): a host with no top-level package of its
    own -- one that vendors the mechanism through `git subtree` keeps the mechanism's package at
    `agent_os/agent_os/`, which is no package of the host's -- or with no `.venv` at its root for
    the driver to link. Of several, the first sorted one the venv's editable install carries,
    because that install's copy is what the control half of the measurement compares against; the
    export is one path, so one package measures it."""
    found = sorted(path.parent.name for path in ROOT.glob("*/__init__.py"))
    if not found or not (ROOT / ".venv").is_dir():
        return None
    installed = _the_venvs_editable_root()
    carried = [name for name in found if (installed / name / "__init__.py").is_file()]
    assert carried, f"the editable install at {installed} carries none of {ROOT}'s {found}"
    return carried[0]


def _the_mechanisms_place_in_the_host() -> pathlib.PurePath | None:
    """Where the mechanism's own directory sits under the host root: `agent_os` in a host that
    vendors it, None in the mechanism's own repository, where it IS the root."""
    if AGENT_OS_DIR.resolve() == ROOT.resolve():
        return None
    return AGENT_OS_DIR.resolve().relative_to(ROOT.resolve())


def _what_the_export_has_to_resolve() -> tuple[pathlib.PurePath, str, pathlib.Path] | None:
    """The package the isolation test measures, as (its tree relative to the host root, its name,
    the venv whose editable install carries it): a package of the host's own when it has one, else
    -- in a host that vendors the mechanism -- the mechanism's own package, which is what a run
    that tests a `subtree pull` has to import from the worktree (agent-os#35). None only in a host
    with neither, one that vendors the mechanism without its `.venv`."""
    package = _the_projects_package()
    if package is not None:
        return pathlib.PurePath("."), package, ROOT / ".venv"
    place = _the_mechanisms_place_in_the_host()
    # In the mechanism's own repository the package this measures is right there, and not finding
    # it is the failure.
    assert place is not None, f"no package found under the mechanism's own root {ROOT}"
    if not (AGENT_OS_DIR / ".venv").is_dir():
        return None
    return place, "agent_os", AGENT_OS_DIR / ".venv"


def _the_venvs_editable_root(venv: pathlib.Path | None = None) -> pathlib.Path:
    """The checkout the shared venv's editable install puts on `sys.path` -- one path in a `.pth`,
    and it is the MAIN checkout's, not the tree whose `.venv` is a link to it. That single line is
    the whole of #393: a worktree that links `.venv` and drops PYTHONPATH imports the code over
    there. `venv` is the host root's by default."""
    venv = ROOT / ".venv" if venv is None else venv
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    editable = sorted(site_packages.glob("_editable_impl_*.pth"))
    assert len(editable) == 1, f"one editable install expected in {site_packages}: {editable}"
    root = pathlib.Path(editable[0].read_text().strip())
    assert root.is_dir(), f"{root}: the editable install points at nothing"
    return root


def _a_venv_whose_editable_install_names(venv: pathlib.Path, root: pathlib.Path) -> None:
    """A `.venv` carrying the one `.pth` line an editable install writes, pointing at `root`."""
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "_editable_impl_host.pth").write_text(f"{root}\n")


def _a_package(directory: pathlib.Path) -> None:
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("")


def test_a_host_that_vendors_the_mechanism_and_has_no_package_of_its_own_has_none_to_resolve(
    tmp_path, monkeypatch
):
    """agent-os#17: the documented install is `git subtree add --prefix agent_os`, which puts the
    mechanism's package two levels down, and a host whose own code is not Python has no top-level
    package at all -- only plain scripts. That is a host this suite has to run in, not a broken
    one: the driver's export has nothing of the host's to resolve there."""
    _a_package(tmp_path / "agent_os" / "agent_os")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("")
    _a_venv_whose_editable_install_names(tmp_path / "agent_os" / ".venv", tmp_path / "agent_os")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)

    assert _the_projects_package() is None


def test_of_several_top_level_packages_the_one_measured_is_one_the_venv_installs(
    tmp_path, monkeypatch
):
    """Several packages is a host layout too (agent-os#17). Which one is measured is not
    arbitrary: the control half of the isolation test compares against the copy the venv's
    editable install resolves, so it has to be a package that install actually carries."""
    for name in ("alpha", "beta", "gamma"):
        _a_package(tmp_path / name)
    installed = tmp_path / "installed-checkout"
    _a_package(installed / "beta")
    _a_package(installed / "gamma")
    _a_venv_whose_editable_install_names(tmp_path / ".venv", installed)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)

    assert _the_projects_package() == "beta"


def test_run_is_alive_reads_a_pid_that_exits_between_the_kill_check_and_the_stat_read_as_dead(
    monkeypatch,
):
    """The race this helper exists to survive: `os.kill(pid, 0)` finds the process, but it is gone
    by the time `/proc/<pid>/stat` gets read. Linux's procfs returns ESRCH (ProcessLookupError) for
    that read, not ENOENT (FileNotFoundError) -- the two look identical from the caller's side, so
    both mean "not alive". Caught #477: CI's `_wait_for_run_to_end` propagated the ESRCH instead."""
    monkeypatch.setattr(os, "kill", lambda pid, signal: None)

    def _read_text_races_the_process_exit(self):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(pathlib.Path, "read_text", _read_text_races_the_process_exit)
    assert _run_is_alive(os.getpid()) is False


def test_a_role_that_runs_tests_gets_a_worktree_of_its_own_and_pythonpath_at_it(launch_environment):
    result = _launch(launch_environment, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(launch_environment)

    worktree = pathlib.Path(seen["WORKTREE"])
    assert worktree != pathlib.Path(launch_environment["PYTHONPATH"]), "the driver exported nothing"
    assert worktree.parent == pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    assert f"pr{PULL_REQUEST}" in worktree.name
    assert f"worktree:  {worktree}" in result.stdout
    # The worktree's root alone where the mechanism is the repository, and the worktree's copy of
    # the mechanism after it where a host vendors it (agent-os#35).
    place = _the_mechanisms_place_in_the_host()
    expected = str(worktree) if place is None else f"{worktree}:{worktree / place}"
    assert seen["PYTHONPATH"] == expected

    # A real checkout, populated with the two gitignored things `git worktree add` never brings:
    # the venv is the one whose absence made the 2026-09-16 run symlink the main checkout's in and
    # then measure the main checkout's code while reporting on the branch. `.env` is linked when
    # this checkout has one; not having one is not a defect in the driver. Nor is a host with no
    # `.venv` at its root (agent-os#17): one that vendors the mechanism keeps the mechanism's own
    # at `agent_os/.venv`, and a host whose own code is not Python has none of its own.
    assert seen["WORKTREE_GIT"] == "yes"
    host_has_pytest = (ROOT / ".venv" / "bin" / "pytest").exists()
    assert seen["WORKTREE_PYTEST"] == ("yes" if host_has_pytest else "no")
    assert seen["WORKTREE_ENV_FILE"] == ("yes" if (ROOT / ".env").exists() else "no")

    # The backend itself still runs from the main checkout: the worktree is where the commands it
    # runs resolve, not where it sits.
    assert seen["PWD"] == str(_host(launch_environment))


def _git_common_dir(directory: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    ).resolve()


def test_a_launch_registers_its_worktree_in_a_disposable_host_and_never_in_the_checkout_under_test(
    launch_environment, tmp_path
):
    """agent-os#66: `git worktree add` writes a registration under the gitdir of whatever checkout
    the driver resolves as its host. Resolved to the checkout running this suite, every launch
    here registered a worktree in the REAL repository -- shared with every other checkout of it --
    and any run that did not reach its own removal (a killed test, a detached run outliving its
    fixture, pytest pruning an old tmp_path) left it listed there. The launch path runs out of a
    disposable host of its own, and the worktree it makes belongs to that host's repository."""
    result = _launch(launch_environment, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(launch_environment)
    registered_in = pathlib.Path(seen["WORKTREE_COMMON_DIR"]).resolve()
    assert registered_in != _git_common_dir(ROOT), (
        f"the run registered its worktree in the checkout under test ({registered_in})"
    )
    assert registered_in.is_relative_to(tmp_path.resolve()), registered_in


# -------------------------------------------------------------------------------------------------
# agent-os#41: a host whose environment is not a root `.venv` -- a monorepo with `backend/.venv` and
# `web/node_modules` -- got an empty worktree, and a validator told the worktree was "already
# populated" could run nothing in it. The driver now provisions it the way the host configures:
# `project.worktree_setup_command`, run inside the worktree before the backend starts, and a run
# whose setup fails is refused rather than launched on an environment nobody could build.
# -------------------------------------------------------------------------------------------------


def _config_with_project_keys(environment, tmp_path, **keys) -> None:
    path = pathlib.Path(environment["AGENTS_CONFIG_PATH"])
    data = yaml.safe_load(path.read_text())
    data["project"].update(keys)
    provisioned = tmp_path / "agents-provisioned.yaml"
    provisioned.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    environment["AGENTS_CONFIG_PATH"] = str(provisioned)


def test_the_worktree_setup_command_runs_inside_the_worktree_before_the_backend_starts(
    launch_environment, tmp_path
):
    _config_with_project_keys(
        launch_environment,
        tmp_path,
        worktree_setup_command='printf "%s" "$PWD" > provisioned-by-setup',
    )
    result = _launch(launch_environment, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(launch_environment)
    assert seen["WORKTREE_PROVISIONED"] == seen["WORKTREE"], result.stdout + result.stderr


def test_a_failing_worktree_setup_command_refuses_the_run_and_removes_the_worktree(
    launch_environment, tmp_path
):
    _config_with_project_keys(
        launch_environment, tmp_path, worktree_setup_command="echo cannot-sync; exit 7"
    )
    result = _launch(launch_environment, "validator")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "project.worktree_setup_command failed" in result.stdout, result.stdout
    assert "cannot-sync" in result.stdout, result.stdout
    assert not pathlib.Path(launch_environment["STUB_RECORD"]).exists(), "the backend ran anyway"
    assert not list(pathlib.Path(launch_environment["AGENT_CACHE_DIR"]).glob("worktree-*"))


def test_the_validators_rules_carry_no_host_specific_database_or_duration_claim(tmp_path):
    # The host's own `never_run` reasons may name its database; the template itself may not.
    rules = _flattened(_rules("validator", config_path=config_with_no_host_text(tmp_path)))
    assert "shared database" not in rules
    assert "50 minutes" not in rules
    assert "already populated" not in rules


def test_the_validators_lint_bullet_renders_the_configured_commands_or_nothing(tmp_path):
    configured = _flattened(_rules("validator"))
    assert "`.venv/bin/ruff check <files>`" in configured
    assert "`.venv/bin/ruff format --check <files>`" in configured

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["project"]["lint_commands"] = ["npx eslint"]
    one_linter = tmp_path / "agents-eslint.yaml"
    one_linter.write_text(yaml.safe_dump(data, sort_keys=False))
    rendered = _flattened(_rules("validator", config_path=one_linter))
    assert "`npx eslint <files>`" in rendered
    assert "ruff" not in rendered

    data["project"]["lint_commands"] = []
    no_linter = tmp_path / "agents-no-lint.yaml"
    no_linter.write_text(yaml.safe_dump(data, sort_keys=False))
    rendered = _flattened(_rules("validator", config_path=no_linter))
    assert "ruff" not in rendered
    assert "__LINT_RULES__" not in rendered


@pytest.mark.parametrize("backend_exit_code", [0, 3])
def test_the_worktree_is_gone_and_unregistered_whatever_the_backend_exited_with(
    backend_exit_code, launch_environment
):
    launch_environment["STUB_EXIT_CODE"] = str(backend_exit_code)
    result = _launch(launch_environment, "validator")
    worktree = pathlib.Path(_record(launch_environment)["WORKTREE"])

    assert not worktree.exists(), f"the run left {worktree} behind"
    # Removing it is the DETACHED run's own last step (#400), so the line that says it happened is
    # in the run's log, not on the stdout of a shell that had already returned by then.
    assert f"worktree:  removed {worktree}" in _run_log(result.stdout), (
        result.stdout + result.stderr
    )
    listed = subprocess.run(
        ["git", "-C", str(_host(launch_environment)), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert str(worktree) not in listed.stdout, "the run left its worktree registered"


def test_a_role_that_runs_no_tests_gets_no_worktree_and_keeps_the_environment_it_inherited(
    launch_environment,
):
    result = _launch(launch_environment, "refiner", "349")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(launch_environment)

    assert seen["PYTHONPATH"] == launch_environment["PYTHONPATH"], "the refiner got a worktree"
    assert not list(pathlib.Path(launch_environment["AGENT_CACHE_DIR"]).glob("worktree-*"))
    assert "worktree:" not in result.stdout, result.stdout


def _assert_a_scratch_dir_of_the_runs_own_was_handed_and_removed(
    seen: dict[str, str], run_dir: pathlib.Path, host: pathlib.Path | None = None
) -> None:
    """agent-os#33: the refiner, told nowhere where scratch files go, wrote them under its own
    `.cache/refiner/` and then `rm -rf`'d that directory -- the driver's run log, the PID file the
    guard reads and every `runs.tsv` row with it. The driver now hands each run a scratch directory
    of its own, empty, outside that run directory and outside the checkout, and removes it at the
    run's end whatever the role left in it."""
    scratch = pathlib.Path(seen.get("SCRATCH", ""))
    assert seen.get("SCRATCH"), "the run was handed no AGENT_RUN_SCRATCH"
    assert seen["SCRATCH_IS_EMPTY_DIR"] == "yes", "the scratch dir was not an empty directory"
    assert not scratch.is_relative_to(run_dir), f"{scratch} lies inside the run dir {run_dir}"
    assert not scratch.is_relative_to(ROOT), f"{scratch} lies inside the checkout {ROOT}"
    if host is not None:
        assert not scratch.is_relative_to(host), f"{scratch} lies inside the host {host}"
    assert not scratch.exists(), f"the run left its scratch dir {scratch} behind"


@pytest.mark.parametrize("role", ONE_SHOT_ROLES)
def test_a_role_is_handed_a_scratch_dir_of_its_own_and_the_run_removes_it(role, launch_environment):
    result = _launch(launch_environment, role)
    assert result.returncode == 0, result.stdout + result.stderr
    run_dir = pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    _assert_a_scratch_dir_of_the_runs_own_was_handed_and_removed(
        _record(launch_environment), run_dir, _host(launch_environment)
    )
    # The driver's own bookkeeping is still there to read: the row, and the log it was taken from.
    assert (run_dir / "runs.tsv").is_file()
    assert _printed_path(result.stdout, "log").is_file()


def test_the_planner_is_handed_a_scratch_dir_of_its_own_and_the_run_removes_it(
    launch_environment, tmp_path
):
    planner_dir = tmp_path / "planner"
    launch_environment.update(
        PLANNER_CACHE_DIR=str(planner_dir),
        PLANNER_CLAUDE_BIN=launch_environment["AGENT_CLAUDE_BIN"],
        PLANNER_QWEN_BIN=launch_environment["AGENT_CLAUDE_BIN"],
    )
    result = subprocess.run(
        ["bash", str(_host_script(launch_environment, "planner_task.sh")), "run", "a test"],
        cwd=_host(launch_environment),
        env=launch_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_a_scratch_dir_of_the_runs_own_was_handed_and_removed(
        _record(launch_environment), planner_dir, _host(launch_environment)
    )
    assert (planner_dir / "runs.tsv").is_file()


def test_every_roles_rules_name_the_scratch_dir_and_forbid_the_drivers_own_cache():
    planner = subprocess.run(
        ["bash", str(AGENT_OS_DIR / "bin" / "planner_task.sh"), "rules"],
        cwd=ROOT,
        env={**os.environ, "WORKER_CACHE_DIR": NO_VERDICT_CACHE_DIR},
        capture_output=True,
        text=True,
        check=False,
    )
    assert planner.returncode == 0, planner.stdout + planner.stderr
    rendered = {"planner": planner.stdout, **{role: _rules(role) for role in ONE_SHOT_ROLES}}
    for role, rules in rendered.items():
        flat = _flattened(rules)
        assert "SCRATCH FILES" in flat, role
        assert "`$AGENT_RUN_SCRATCH`" in flat, role
        assert "never write, move or delete anything under `.cache/`" in flat, role


def test_by_default_the_worktree_holds_the_head_origin_names_for_that_pull_request(stubbed_origin):
    """Without AGENT_WORKTREE_REF the driver asks origin for `refs/pull/<pr>/head` and builds the
    worktree at exactly that commit -- reviewing some other commit is the failure this exists to
    prevent, and it is invisible in the review that comes out of it."""
    result = _launch(stubbed_origin, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(stubbed_origin)
    calls = pathlib.Path(stubbed_origin["GIT_STUB_CALLS"]).read_text().splitlines()

    pull_ref = f"refs/pull/{PULL_REQUEST}/head"
    assert any(f"ls-remote origin {pull_ref}" in line for line in calls), calls
    assert any(f"fetch -q origin {pull_ref}" in line for line in calls), calls
    assert seen["WORKTREE_HEAD"] == stubbed_origin["GIT_STUB_PR_HEAD"]
    assert not pathlib.Path(seen["WORKTREE"]).exists(), "the run left its worktree behind"


def test_the_backend_is_told_the_path_of_the_worktree_it_was_handed(launch_environment):
    """The half of #393 no reading of the wording settles: the RULES the backend actually receives
    name the worktree THIS run got -- the same path the driver exported as PYTHONPATH -- and carry
    no command that would build or populate another one."""
    result = _launch(launch_environment, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(launch_environment)
    prompt = _flattened(_prompt(launch_environment))

    worktree = seen["WORKTREE"]
    assert worktree != launch_environment["PYTHONPATH"], "the driver exported what it inherited"
    assert f"Its path is {worktree}." in prompt
    assert "__WORKTREE__" not in prompt
    for command in BUILD_OR_POPULATE_A_WORKTREE:
        assert command not in prompt, command


def test_a_run_that_got_no_worktree_is_told_so_instead_of_being_left_to_improvise(
    launch_environment,
):
    """The degraded path warns once and runs on, so its prompt has to say there is nothing to run
    in: an inherited PYTHONPATH -- here a sentinel pointing at nothing -- is left untouched rather
    than named as the worktree, which is what would otherwise send the agent improvising one."""
    launch_environment["AGENT_WORKTREE_REF"] = "no-such-ref-for-this-test"
    result = _launch(launch_environment, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: no throwaway worktree at" in result.stdout, result.stdout

    seen = _record(launch_environment)
    prompt = _flattened(_prompt(launch_environment))
    assert seen["PYTHONPATH"] == launch_environment["PYTHONPATH"], "the driver exported a worktree"
    assert "the driver could not prepare one" in prompt
    assert "Read the diff then, and run nothing" in prompt
    assert "__WORKTREE__" not in prompt
    for command in BUILD_OR_POPULATE_A_WORKTREE:
        assert command not in prompt, command


def _assert_the_run_resolved_the_worktrees_copy(
    seen: dict[str, str],
    host_root: pathlib.Path,
    tree: pathlib.PurePath,
    package: str,
    venv: pathlib.Path,
) -> None:
    """The measurement both isolation tests make of one stub record: `package`, in `tree` of the
    worktree, is what the venv's python resolves through the exported environment, and the
    editable install's copy is what it resolves without it."""
    assert "ORIGIN" in seen, f"the stub resolved no package; it recorded {sorted(seen)}"
    worktree = pathlib.Path(seen["WORKTREE"])
    installed = _the_venvs_editable_root(venv)

    # Only the worktree's copy ever carried the marker: not this checkout's, and not the one the
    # editable install names. The worktree itself is gone by now -- removed on every exit path, as
    # the test above asserts -- so MARKER=yes in the record is the evidence it was there.
    assert not (host_root / tree / package / "marker.txt").exists()
    assert not (installed / package / "marker.txt").exists()

    assert seen["ORIGIN"] == str(worktree / tree / package / "__init__.py")
    assert seen["MARKER"] == "yes"

    # The control, and the half that makes the two assertions above a measurement of the driver's
    # export instead of a filesystem that happened to list the worktree first: one interpreter
    # (`sys.executable` agrees across both runs), one editable install, and the only difference is
    # the variable the driver exported -- with it the worktree's copy, without it the checkout the
    # `.pth` names.
    assert seen["INHERITED_EXECUTABLE"] == seen["EXECUTABLE"]
    assert seen["INHERITED_ORIGIN"] == str(installed / package / "__init__.py")
    assert seen["INHERITED_MARKER"] == "no"
    assert not pathlib.Path(seen["INHERITED_ORIGIN"]).is_relative_to(worktree)


def test_the_environment_the_driver_exports_resolves_the_worktrees_copy_and_not_the_main_checkouts(
    launch_environment, tmp_path
):
    """The isolation #393 asks for, measured rather than read off the wording: a marker dropped
    into the worktree's copy of the package while the run is live is what the venv's own python
    resolves THROUGH the environment the driver exported -- and what it does not resolve with that
    PYTHONPATH taken away, which is the tree the 2026-09-16 validator's suite actually ran on while
    its review talked about the branch. In a host that vendors the mechanism and has no package of
    its own, the package measured is the mechanism's (agent-os#35)."""
    measured = _what_the_export_has_to_resolve()
    if measured is None:
        pytest.skip(f"{ROOT} vendors the mechanism with no .venv of its own for the export to use")
    tree, package, venv = measured
    resolver = tmp_path / "resolve_package.py"
    resolver.write_text(PACKAGE_RESOLVER)
    launch_environment["STUB_RESOLVE_PACKAGE"] = package
    launch_environment["STUB_PACKAGE_TREE"] = str(tree)
    launch_environment["STUB_RESOLVER"] = str(resolver)

    result = _launch(launch_environment, "validator")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(launch_environment)
    assert seen.get("PACKAGE_PYTHON") == "linked", seen
    _assert_the_run_resolved_the_worktrees_copy(
        seen, _host(launch_environment), tree, package, venv
    )


def _copy_of_the_mechanism(destination: pathlib.Path) -> None:
    """The mechanism's tracked files as they stand in this tree, copied to `destination`: what a
    `git subtree add --prefix <destination>` puts in a host, including this change's own drivers
    and not whatever the last commit holds."""
    listed = subprocess.run(
        ["git", "-C", str(AGENT_OS_DIR), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    for relative in filter(None, listed):
        source = AGENT_OS_DIR / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _git(directory: pathlib.Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=agent-os test",
            "-c",
            "user.email=test@example.invalid",
            *arguments,
        ],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def vendoring_host(launch_environment, tmp_path):
    """A throwaway host that vendors the mechanism as the documented install does: a git
    repository of its own with the mechanism under `agent_os/`, no package and no `.venv` at its
    root, and the mechanism's own `agent_os/.venv` whose editable install names the host's
    `agent_os/` -- the `.pth` line the mechanism's `bootstrap.sh` leaves, pointing at the MAIN
    checkout's copy. Its dependencies come from this suite's own interpreter, through a second
    `.pth` that sorts after the editable one. Returns the host root and the environment a launch
    of ITS driver runs with."""
    host = tmp_path / "vendoring-host"
    mechanism = host / "agent_os"
    _copy_of_the_mechanism(mechanism)
    (host / "scripts").mkdir()
    (host / "scripts" / "tool.sh").write_text("#!/usr/bin/env bash\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(host)], capture_output=True, check=True)
    _git(host, "add", "-A")
    _git(host, "commit", "-q", "-m", "vendor the mechanism")

    venv.EnvBuilder(symlinks=True, with_pip=False).create(mechanism / ".venv")
    site_packages = next((mechanism / ".venv" / "lib").glob("python*/site-packages"))
    (site_packages / "_editable_impl_agent_os.pth").write_text(f"{mechanism}\n")
    dependencies = [path for path in map(pathlib.Path, site.getsitepackages()) if path.is_dir()]
    (site_packages / "zz_dependencies.pth").write_text(
        "".join(f"{path}\n" for path in dependencies)
    )

    environment = dict(launch_environment)
    # Resolved by the host's own driver, exactly as a real launch there resolves them: an inherited
    # answer from whatever launched this suite would point the driver at another tree.
    for inherited in ("AGENT_OS_HOST_ROOT", "AGENT_OS_PYTHON", "AGENT_OS_DIR"):
        environment.pop(inherited, None)
    return host, environment


def test_a_vendoring_hosts_role_imports_the_worktrees_copy_of_the_mechanism(
    vendoring_host, tmp_path
):
    """agent-os#35: a validator testing a host pull request that changes `agent_os/` -- a `subtree
    pull` -- has to run the mechanism's code from that pull request. With `PYTHONPATH=<worktree>`
    alone, `<worktree>/agent_os/` is a directory with no `__init__.py`, a namespace portion, and the
    regular package the editable install puts on `sys.path` wins over it: the run imports the MAIN
    checkout's mechanism and certifies code it never executed. Measured here with the venv the
    driver links into the worktree, and with the mechanism's `agent_os/.venv/bin/python`, which is
    what `agent_os/.venv/bin/pytest agent_os/tests` runs under."""
    host, environment = vendoring_host
    resolver = tmp_path / "resolve_package.py"
    resolver.write_text(PACKAGE_RESOLVER)
    environment.update(
        STUB_RESOLVE_PACKAGE="agent_os",
        STUB_PACKAGE_TREE="agent_os",
        STUB_RESOLVER=str(resolver),
    )

    result = subprocess.run(
        ["bash", str(host / "agent_os" / "bin" / "agent_task.sh"), "validator", PULL_REQUEST]
        + ["--no-wake"],
        cwd=host,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    pid = _detached_pid(result.stdout)
    if pid is not None:
        _wait_for_run_to_end(pid)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _record(environment)

    worktree = pathlib.Path(seen["WORKTREE"])
    assert worktree.parent == pathlib.Path(environment["AGENT_CACHE_DIR"]), seen
    # The defect itself first: whichever of the two interpreters answered -- the linked venv, or
    # the driver's own when there is no link -- it is the same venv and the same editable install.
    _assert_the_run_resolved_the_worktrees_copy(
        seen, host, pathlib.PurePath("agent_os"), "agent_os", host / "agent_os" / ".venv"
    )
    # The mechanism's own venv, linked where `agent_os/.venv/bin/pytest` names it.
    assert seen["PACKAGE_PYTHON"] == "linked", seen
    assert seen["PYTHONPATH"] == f"{worktree}:{worktree / 'agent_os'}"
    assert not worktree.exists(), f"the run left {worktree} behind"


def _link_the_mechanisms_venv(host: pathlib.Path, worktree: pathlib.Path) -> str:
    """`agent_os_link_mechanism_venv` in the mode a worker's persistent worktree uses."""
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/agent_os/bin/_python.sh" && agent_os_link_mechanism_venv "$2" "$1" "$3"',
            "link",
            str(host),
            str(worktree),
            "only-if-ignored",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_a_workers_worktree_gets_the_mechanisms_venv_only_where_git_ignores_the_link(
    vendoring_host, tmp_path
):
    """A worker's worktree outlives its run, and an untracked entry in it refuses the next start.
    A symlink is not a directory to git, so a `.venv/` rule does not ignore it: on a branch cut
    before the mechanism's `.gitignore` said `.venv`, the driver leaves the link out rather than
    dirty the tree (agent-os#35). On a branch that ignores it, the link is there and the tree
    stays clean."""
    host, _ = vendoring_host
    ignoring = tmp_path / "ignoring"
    _git(host, "worktree", "add", "-q", "--detach", str(ignoring), "HEAD")
    assert "linked" in _link_the_mechanisms_venv(host, ignoring)
    assert (ignoring / "agent_os" / ".venv").is_symlink()
    assert (ignoring / "agent_os" / ".venv" / "bin" / "python").exists()
    clean = subprocess.run(
        ["git", "-C", str(ignoring), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert clean.stdout == ""

    gitignore = host / "agent_os" / ".gitignore"
    gitignore.write_text(gitignore.read_text().replace(".venv\n", ".venv/\n", 1))
    _git(host, "commit", "-q", "-am", "the ignore rule before agent-os#35")
    older = tmp_path / "older"
    _git(host, "worktree", "add", "-q", "--detach", str(older), "HEAD")
    assert _link_the_mechanisms_venv(host, older) == ""
    assert not (older / "agent_os" / ".venv").exists()


# -------------------------------------------------------------------------------------------------
# The detach itself (#400). PR #399's validator ran inside the planner's own process group: the
# planner's Bash tool moved the command to the background at its 120s timeout, the planner's turn
# ended, and the run died mid-turn -- no `result` event in its log, no `validator_finished`, no
# review on the pull request, and nothing recording that the review never happened. What these two
# measure is the fix: a launch that returns while the run goes on in a session of its own, and a
# run that finishes after the shell which launched it has been killed as a whole process group.
# -------------------------------------------------------------------------------------------------


def test_the_launch_returns_at_once_with_a_pid_that_is_the_runs_own_session(
    launch_environment, tmp_path
):
    launch_environment["STUB_SLEEP"] = "5"
    launch_environment["STUB_SESSION_FILE"] = str(tmp_path / "stub-session.txt")

    result = subprocess.run(
        ["bash", str(_host_script(launch_environment)), "validator", PULL_REQUEST, "--no-wake"],
        cwd=_host(launch_environment),
        env=launch_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    pid = _detached_pid(result.stdout)
    assert pid is not None, f"the driver printed no detached PID:\n{result.stdout}"

    # It waited for nothing. The stub was still asleep when the driver's own shell exited: the run
    # is alive, and the record the stub writes on its way out cannot exist yet.
    assert not pathlib.Path(launch_environment["STUB_RECORD"]).exists(), "the driver waited"
    assert _run_is_alive(pid), f"the driver printed a PID that was not running: {pid}"

    # The PID beside the log of the same stamp -- `.pid` for `.log` is the whole of the contract the
    # guard reads -- and it names the run's OWN session, which is what puts a teardown of the
    # launching shell out of the run's reach. Measured off the stub's own `ps`, not off the
    # driver's word for it.
    pidfile = _pidfile(result.stdout)
    assert pidfile.read_text().strip() == str(pid)
    assert pidfile.with_suffix(".log").is_file()
    assert _session(launch_environment)["SID"] == str(pid)

    _wait_for_run_to_end(pid)
    # A run that reached its own end takes its PID file with it, so a PID file left beside a log
    # with no `result` event in it is a run that DIED -- and only that.
    assert not pidfile.exists(), f"the finished run left {pidfile} behind"
    assert pathlib.Path(launch_environment["STUB_RECORD"]).is_file()


def _kill_process_group(pgid: int) -> None:
    """The teardown a session's end causes: TERM to the whole group, then KILL to whatever did not
    take it. A group already gone is the outcome wanted here, not an error."""
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, number)
        except ProcessLookupError:
            return
        time.sleep(0.5)


def test_a_run_finishes_after_the_shell_that_launched_it_is_killed(launch_environment, tmp_path):
    cache = pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    environment = dict(launch_environment)
    environment.update(
        # The event and `wake`'s lock are derived from WORKER_CACHE_DIR, the same override
        # agent_guard.py itself reads, so one value keeps this run's planner state in the sandbox.
        WORKER_CACHE_DIR=str(cache),
        STUB_SLEEP="4",
        STUB_SESSION_FILE=str(tmp_path / "stub-session.txt"),
    )

    # `wake` is the one door to a planner run and this test must not open it: holding the lock
    # exactly as a live planner holds it is what makes the detached run's last step a no-op here.
    lock = cache / "planner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = lock.open("a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    launcher_output = tmp_path / "launcher.log"
    try:
        with launcher_output.open("w") as out:
            # A session of its own, so the kill below is that shell's teardown and nobody else's;
            # and it stays alive after the driver returns, the way a planner that goes on working
            # stays alive. No `--no-wake` here: the event is one of the things under test.
            # "planner-shell" is that `bash -c`'s own $0, so $@ is the driver and its arguments.
            launcher = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'bash "$@"; sleep 60',
                    "planner-shell",
                    str(_host_script(environment)),
                    "validator",
                    PULL_REQUEST,
                ],
                cwd=_host(environment),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            # Kill it once the driver has returned -- the moment the planner's own turn ends -- and
            # while the stub is still asleep, that is, with the run unfinished.
            _wait_until(
                lambda: _detached_pid(launcher_output.read_text()) is not None,
                "the driver to print the run it detached",
            )
            _kill_process_group(launcher.pid)
        launcher.wait(timeout=30)

        printed = launcher_output.read_text()
        pid = _detached_pid(printed)
        # Held until the run is OVER, and the event is read while it is still held: `wake` is the
        # run's last step, so a lock released one line earlier is a lock that step finds free, and
        # what it then opens is the one door to a REAL planner -- a paid backend run, in this
        # repository, with this test's own environment, which consumes the very event the assertion
        # below is about (measured 2026-09-17: six turns of Opus, 0.45 USD, woken by this test).
        _wait_for_run_to_end(pid)
        events = sorted((cache / "planner_events").glob(f"*validator_finished-{PULL_REQUEST}*"))
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    # The stub ran to the end of its own sleep and wrote its record on the way out, in the session
    # the driver detached it into and not in the one that was killed.
    seen = _record(environment)
    assert _session(environment)["SID"] == str(pid)

    # And the run's own half still happened after the kill: the worktree removed, the row saying
    # the run happened, and the event that brings the planner back to the pull request. PR #399 got
    # none of the three.
    worktree = pathlib.Path(seen["WORKTREE"])
    assert not worktree.exists(), f"the killed launch left {worktree} behind"
    assert f"worktree:  removed {worktree}" in _run_log(printed)

    assert f"validator #{PULL_REQUEST}" in (cache / "runs.tsv").read_text()
    assert len(events) == 1, events
    # The door stayed shut: the run's own log carries `wake`'s refusal, which is the proof that no
    # planner -- real or stubbed -- was launched from inside a test.
    assert "events left for it" in _run_log(printed), _run_log(printed)
    assert events[0].read_text().strip() == f"the validator finished on #{PULL_REQUEST}"
    assert not _pidfile(printed).exists(), "the finished run left its PID file behind"


# -------------------------------------------------------------------------------------------------
# A one-shot role that dies before its result leaves no trace (#394). The two tests above measure
# the DETACH; these measure the other half -- BACKEND_STUB never writes a `result` event (its whole
# answer goes to STUB_RECORD, never to the run's own log), so every launch in this file already
# exercises "no terminal result"; what varies here is only what the backend exits with. Nothing in
# `agent_detached_run` checks `$?` between the backend call and its own bookkeeping (no `set -e`
# either), so both paths already reach `agent_append_run_row` and the `<role>_finished` event --
# these tests pin that rather than add to it.
# -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend_exit_code", [0, 3])
def test_a_run_whose_backend_leaves_no_result_event_still_gets_its_row_and_finished_event(
    backend_exit_code, launch_environment
):
    cache = pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    launch_environment["STUB_EXIT_CODE"] = str(backend_exit_code)
    # The event and `wake`'s lock are derived from WORKER_CACHE_DIR, the same override
    # agent_guard.py itself reads (see the kill test above): one value keeps this run's planner
    # state in the sandbox instead of the real `.cache`.
    launch_environment["WORKER_CACHE_DIR"] = str(cache)

    # Held for the run's whole duration, exactly as the kill test above holds it: `wake` is the
    # run's last step, and finding this lock taken is what stops it opening the door to a REAL
    # planner run instead of the stub answering "events left for it".
    lock = cache / "planner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = lock.open("a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _launch(launch_environment, "validator", no_wake=False)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert result.returncode == 0, result.stdout + result.stderr

    rows = (cache / "runs.tsv").read_text().splitlines()
    assert rows[0] == "ts\tcontext\tmodel\tnum_turns\ttotal_cost_usd"
    assert len(rows) == 2, rows  # header, and one row for this run -- never omitted
    _ts, context, _model, turns, cost = rows[1].split("\t")
    assert context == f"validator #{PULL_REQUEST}"
    # Recorded AS a run with no result, not silently dropped: the row exists and its cost fields
    # are the ones left empty.
    assert turns == "" and cost == ""

    events = sorted(cache.glob(f"planner_events/*-validator_finished-{PULL_REQUEST}*"))
    assert len(events) == 1, events
    assert events[0].read_text().strip() == f"the validator finished on #{PULL_REQUEST}"
    assert "events left for it" in _run_log(result.stdout), _run_log(result.stdout)


# -------------------------------------------------------------------------------------------------
# THE LAUNCH GATE (#425). Which backend a role runs on is decided before a turn is spent, from the
# guard's OWN persisted verdict on Claude's quota -- never from an agent's claim about its own
# (agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md). On
# 2026-09-18 the planner's 08:48:46Z run was rejected by Claude's five-hour rate limit in 497 ms,
# and the role that would have redispatched everything else on Qwen was that same rejected Claude
# run, so seven `status:ready` issues sat with a free Qwen allowance unused until 2026-09-20.
# -------------------------------------------------------------------------------------------------

# Stands in for one of the two backend CLIs: records the command line it was handed -- every
# argument short enough to be a flag or a model name, so the rules and the instruction stay out of
# the record -- and then behaves exactly like the stub every other launch test uses. WHICH of the
# two mark files exists after a launch is the answer to "which backend ran", and the flags in it
# are the answer to "with which command line", the one thing a real launch would fail on silently.
MARKING_STUB = """#!/usr/bin/env bash
: > "__MARK__"
for argument in "$@"; do
  if [ "${#argument}" -lt 40 ]; then printf '%s\\n' "$argument" >> "__MARK__"; fi
done
exec "__TARGET__" "$@"
"""


def _marking_stub(path: pathlib.Path, mark: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    path.write_text(MARKING_STUB.replace("__MARK__", str(mark)).replace("__TARGET__", str(target)))
    path.chmod(0o755)
    return path


def _config_with_no_fallback_declared(tmp_path) -> pathlib.Path:
    """`config.example.yaml` with every `fallback:` block taken out, so a test can measure the case
    the field's absence leaves behind: a class that declares no way round an exhausted quota
    behaves exactly as it did before the field existed."""
    text = _config_without_secrets(tmp_path).read_text()
    patched, count = re.subn(r"^    fallback:\n(?:      .*\n)+", "", text, flags=re.MULTILINE)
    assert count == 3, "config.example.yaml no longer declares exactly three role fallbacks"
    path = tmp_path / "agents-no-fallback.yaml"
    path.write_text(patched)
    return path


def test_a_dry_run_names_the_backend_it_would_launch_and_the_verdict_it_decided_on():
    _, task_class = load_role_class("validator")
    result = _run("validator", "352", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    # No verdict on disk reads as UNKNOWN, and unknown launches the class's own backend: a start
    # the quota then refuses costs one page, a substitution made on nothing costs the review the
    # independence the merge gate rests on.
    assert f"backend:   {task_class.backend}" in result.stdout, result.stdout
    assert f"model:     {task_class.model}" in result.stdout, result.stdout
    assert "launch:    " in result.stdout, result.stdout
    assert "no claude quota verdict on disk" in result.stdout, result.stdout


def test_an_exhausted_verdict_substitutes_the_fallback_the_class_declares(tmp_path):
    verdicts = tmp_path / "verdicts"
    _write_quota_verdict(verdicts, "exhausted")
    fallback = load_role_class("validator")[1].fallback
    assert fallback is not None, "config/agents.yaml declares no validator fallback"

    result = _run("validator", "352", "--dry-run", cache_dir=verdicts)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"backend:   {fallback.backend} (FALLBACK for claude" in result.stdout, result.stdout
    assert f"model:     {fallback.model}" in result.stdout, result.stdout
    # The line says what it decided ON, so a human reading the log can judge the decision.
    assert "reads exhausted" in result.stdout and "TTL" in result.stdout, result.stdout


def test_a_stale_verdict_reads_as_unknown_and_launches_the_classs_own_backend(tmp_path):
    ttl_minutes = load_mechanism().quota_verdict_ttl_minutes
    verdicts = tmp_path / "verdicts"
    _write_quota_verdict(verdicts, "exhausted", age_minutes=ttl_minutes + 5)

    result = _run("validator", "352", "--dry-run", cache_dir=verdicts)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backend:   claude" in result.stdout, result.stdout
    assert f"past the {ttl_minutes} min TTL" in result.stdout, result.stdout


def test_an_exhausted_verdict_with_no_fallback_declared_launches_the_classs_own_backend(tmp_path):
    # The guard's `quota_exhausted_no_fallback` page is for this case, and it is a human's to
    # receive: a launch with no way round the exhausted window does not invent one.
    verdicts = tmp_path / "verdicts"
    _write_quota_verdict(verdicts, "exhausted")
    config = _config_with_no_fallback_declared(tmp_path)

    result = _run("validator", "352", "--dry-run", config_path=config, cache_dir=verdicts)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backend:   claude" in result.stdout, result.stdout
    assert "declares no fallback" in result.stdout, result.stdout


def test_the_validators_review_names_the_backend_that_wrote_it_either_way(tmp_path):
    # The merge gate accepts a substituted review as the validator's approval (the human's decision
    # of 2026-09-18), so what the substitution must not lose is the record of itself -- and the
    # record lives in the review's own body, which is the one thing the tracker keeps.
    own = _flattened(_rules("validator"))
    assert "Reviewed by the validator on claude (claude-opus-5)." in own
    assert "__REVIEW_BACKEND_LINE__" not in own

    verdicts = tmp_path / "verdicts"
    _write_quota_verdict(verdicts, "exhausted")
    substituted = _flattened(_rules("validator", cache_dir=verdicts))
    assert "Reviewed by the validator on qwen (qwen3.8-max)" in substituted
    assert "IS the validator's approval for the merge gate" in substituted
    assert "__REVIEW_BACKEND_LINE__" not in substituted


def test_the_refiners_rules_carry_no_review_line_of_their_own(tmp_path):
    # The refiner posts a summary comment, not a review, so the placeholder exists in one block
    # only -- and a substitution that left it unsubstituted anywhere would be visible as the
    # placeholder's own spelling.
    verdicts = tmp_path / "verdicts"
    _write_quota_verdict(verdicts, "exhausted")
    assert "__REVIEW_BACKEND_LINE__" not in _rules("refiner", cache_dir=verdicts)


def test_the_planners_rules_tell_it_a_roles_backend_is_not_its_to_choose():
    rules = subprocess.run(
        ["bash", str(AGENT_OS_DIR / "bin" / "planner_task.sh"), "rules"],
        cwd=ROOT,
        env={**os.environ, "WORKER_CACHE_DIR": NO_VERDICT_CACHE_DIR},
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "A ROLE'S OWN BACKEND IS NOT YOURS TO CHOOSE" in rules
    # The worker paragraph it amends stays: a WORKER's redispatch is still the planner's decision.
    assert "qwen_fallback_eligible: true" in rules
    assert "COUNTS AS THE VALIDATOR'S APPROVAL" in rules


def test_an_exhausted_verdict_launches_the_fallback_backend_and_not_claude(
    launch_environment, tmp_path
):
    """The whole issue in one launch: the verdict says Claude is out, the class declares Qwen, and
    what actually runs is Qwen -- with the substitution named in the log header, in the `runs.tsv`
    row's model column, in the review line the backend was handed and in the event that brings the
    planner back."""
    cache = pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    # One directory for both the verdict and the run's own bookkeeping, so the event this run
    # writes lands in the sandbox too.
    launch_environment["WORKER_CACHE_DIR"] = str(cache)
    _write_quota_verdict(cache, "exhausted")

    stub = pathlib.Path(launch_environment["AGENT_CLAUDE_BIN"])
    qwen_mark = tmp_path / "qwen-was-launched.txt"
    claude_mark = tmp_path / "claude-was-launched.txt"
    launch_environment["AGENT_QWEN_BIN"] = str(
        _marking_stub(tmp_path / "bin" / "qwen", qwen_mark, stub)
    )
    launch_environment["AGENT_CLAUDE_BIN"] = str(
        _marking_stub(tmp_path / "bin" / "claude", claude_mark, stub)
    )

    # Held for the run's whole duration, exactly as the two tests above hold it: `wake` is the
    # run's last step, and finding this lock taken is what stops it opening the door to a REAL
    # planner run instead of the stub answering "events left for it".
    lock = cache / "planner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = lock.open("a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _launch(launch_environment, "validator", no_wake=False)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert result.returncode == 0, result.stdout + result.stderr

    fallback = load_role_class("validator")[1].fallback
    assert qwen_mark.is_file(), "the fallback backend was never launched"
    assert not claude_mark.exists(), "Claude was launched on a verdict that read exhausted"
    launched = qwen_mark.read_text().split()
    # Qwen's own flag shape, not Claude's: `-o stream-json` and `--approval-mode yolo`. A launch
    # that handed Claude's flags to the qwen CLI is a run that never started.
    assert "-o" in launched and "stream-json" in launched, launched
    assert "--approval-mode" in launched and "yolo" in launched, launched
    assert "-p" not in launched and "--dangerously-skip-permissions" not in launched, launched
    assert launched[launched.index("--model") + 1] == fallback.model, launched

    log = _run_log(result.stdout)
    assert f"backend:   {fallback.backend} (FALLBACK for claude" in log, log
    assert f"model:     {fallback.model}" in log, log
    assert f"Reviewed by the validator on {fallback.backend} ({fallback.model})" in _prompt(
        launch_environment
    )

    rows = (cache / "runs.tsv").read_text().splitlines()
    assert rows[0] == "ts\tcontext\tmodel\tnum_turns\ttotal_cost_usd"
    _ts, context, model, _turns, _cost = rows[1].split("\t")
    # The row books the run to the backend that spent it: the model column is what the control
    # plane's report reads to decide between USD and tokens (`agent_os/docs/AGENT_OS.md` §3), and an
    # unsubstituted row keeps the context it has always had.
    assert model == fallback.model, rows[1]
    assert context == f"validator #{PULL_REQUEST}", rows[1]

    events = sorted(cache.glob(f"planner_events/*-validator_finished-{PULL_REQUEST}*"))
    assert len(events) == 1, events
    assert events[0].read_text().strip() == (
        f"the validator finished on #{PULL_REQUEST} (ran on qwen, the declared fallback for claude)"
    )


# -------------------------------------------------------------------------------------------------
# END TO END (#429): a role refused by its own backend's quota teaches the launch gate, through the
# guard, with no worker anywhere. Before #429 only a live worker's stream could write the verdict,
# so the 2026-09-18 planner run refused by Claude's five-hour window left it `unknown` and the next
# role launched straight into the same window. The guard is the verdict's one writer; the drivers
# only read it.
# -------------------------------------------------------------------------------------------------

# The backend's side of a refused run, in the shape `.cache/planner/20260918T084846Z.log` carries:
# one rejected rate-limit event, an empty turn, and an error result with the HTTP status. Its
# assistant says nothing about a quota -- the verdict must come from this record alone.
REJECTED_BY_THE_QUOTA_STREAM = [
    {
        "type": "rate_limit_event",
        "session_id": "s1",
        "rate_limit_info": {
            "status": "rejected",
            "unifiedWindows": {"five_hour": {"utilization": 1, "resetsAt": 1789727400}},
        },
    },
    {
        "type": "assistant",
        "session_id": "s1",
        "message": {"content": [], "usage": {"input_tokens": 0, "output_tokens": 0}},
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "num_turns": 1,
        "api_error_status": 429,
        "terminal_reason": "api_error",
        "result": "You've hit your session limit · resets 12:30pm",
    },
]
SERVED_STREAM = [
    {
        "type": "assistant",
        "session_id": "s2",
        "message": {
            "content": [{"type": "text", "text": "review posted"}],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    },
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "ok"},
]


def _stream_file(path: pathlib.Path, events: list[dict]) -> pathlib.Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _age_by(path: pathlib.Path, seconds: float) -> None:
    """Moves a file's mtime back: for the detached driver, which reads the real clock, this is the
    clock moving forward -- the verdict's age is read off its file's mtime."""
    stat = path.stat()
    os.utime(path, (stat.st_atime - seconds, stat.st_mtime - seconds))


def _age_exit_marker_by(log: pathlib.Path, seconds: float) -> None:
    """The same clock move for a role run, whose time is its exit marker's content. The driver
    writes the marker once and never again; only the test rewrites it, to stand in for time."""
    exited_at = read_role_run_exit_marker(log)
    assert exited_at is not None, f"{log.name}: the driver wrote no exit marker"
    role_run_exit_marker(log).write_text(
        (exited_at - timedelta(seconds=seconds)).isoformat() + "\n"
    )


def _ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1e9)


def test_a_role_refused_on_quota_sends_the_next_launch_to_the_fallback_until_the_ttl(
    launch_environment, tmp_path, monkeypatch
):
    """Rejected run -> guard tick -> the next launch takes the declared fallback; the verdict
    ages past `mechanism.quota_verdict_ttl_minutes` -> the class's own backend runs again. Every
    run goes through the real detached driver; the tick is `guard.tick` itself, with only the
    tracker stubbed out (`_agents_paused`, so it stops before any event, label or planner)."""
    cache = pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    # One sandbox for the role logs, the verdict and the guard's bookkeeping, as in the test above;
    # the in-process guard is pointed at the same place the driver writes to.
    launch_environment["WORKER_CACHE_DIR"] = str(cache)
    for name, value in (("AGENT_CACHE_DIR", cache), ("WORKER_CACHE_DIR", cache)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("PLANNER_CACHE_DIR", raising=False)
    monkeypatch.setattr(agent_guard, "_agents_paused", lambda *, main: True)

    stub = pathlib.Path(launch_environment["AGENT_CLAUDE_BIN"])
    qwen_mark = tmp_path / "qwen-was-launched.txt"
    claude_mark = tmp_path / "claude-was-launched.txt"
    launch_environment["AGENT_QWEN_BIN"] = str(
        _marking_stub(tmp_path / "bin" / "qwen", qwen_mark, stub)
    )
    launch_environment["AGENT_CLAUDE_BIN"] = str(
        _marking_stub(tmp_path / "bin" / "claude", claude_mark, stub)
    )
    rejected = _stream_file(tmp_path / "rejected.jsonl", REJECTED_BY_THE_QUOTA_STREAM)
    served = _stream_file(tmp_path / "served.jsonl", SERVED_STREAM)
    _, task_class = load_role_class("validator")
    fallback = task_class.fallback
    assert fallback is not None, "config.example.yaml declares no validator fallback"
    ttl_seconds = load_mechanism().quota_verdict_ttl_minutes * 60

    def launch(stream: pathlib.Path, exit_code: str) -> subprocess.CompletedProcess:
        for mark in (qwen_mark, claude_mark):
            mark.unlink(missing_ok=True)
        result = _launch(
            {**launch_environment, "STUB_STREAM": str(stream), "STUB_EXIT_CODE": exit_code},
            "validator",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    # 1. The class's own backend runs -- nothing is on disk yet -- and its quota refuses it.
    first = launch(rejected, "1")
    assert claude_mark.is_file() and not qwen_mark.exists()
    rejected_log = _printed_path(first.stdout, "log")
    assert f"backend:   {task_class.backend}" in rejected_log.read_text()
    assert read_persisted_quota_verdict(task_class.backend).status == "unknown", (
        "the driver wrote the verdict itself -- the guard is meant to be its one writer"
    )

    # 2. One guard tick folds the refusal into the verdict, dated by the moment the refused
    # backend exited -- the marker the driver wrote then -- not by the log the run kept writing.
    agent_guard.tick(main=tmp_path)
    verdict_file = cache / f"agent_guard_{task_class.backend}.json"
    assert read_persisted_quota_verdict(task_class.backend).status == "exhausted"
    assert verdict_file.stat().st_mtime_ns == _ns(read_role_run_exit_marker(rejected_log))

    # 3. The next launch reads it and runs on the declared fallback.
    second = launch(served, "0")
    assert qwen_mark.is_file(), "the fallback backend was never launched"
    assert not claude_mark.exists(), "Claude was launched on a verdict that read exhausted"
    assert f"backend:   {fallback.backend} (FALLBACK for {task_class.backend}" in _run_log(
        second.stdout
    )

    # 4. The clock moves past the TTL. In process, through the guard's and the gate's own `now`:
    # a tick then leaves the verdict alone -- the refusal is not re-read into a fresh one -- and
    # the plan reads it as unknown and names the class's own backend.
    later = datetime.now(UTC) + timedelta(seconds=ttl_seconds + 300)
    before = verdict_file.stat().st_mtime_ns
    agent_guard.tick(main=tmp_path, now=later)
    assert verdict_file.stat().st_mtime_ns == before
    _, plan, _ = role_launch("validator", now=later)
    assert (plan.backend, plan.substituted) == (task_class.backend, False), plan

    # ...and for the detached driver, which reads the real clock, by moving the refusal's two
    # records -- its exit marker and the verdict file -- back by the same amount. A real tick in between re-reads nothing into the verdict.
    _age_exit_marker_by(rejected_log, ttl_seconds + 300)
    _age_by(verdict_file, ttl_seconds + 300)
    agent_guard.tick(main=tmp_path)
    third = launch(served, "0")
    assert claude_mark.is_file(), "the class's own backend did not run once the verdict aged out"
    assert not qwen_mark.exists()
    assert f"past the {ttl_seconds // 60} min TTL" in third.stdout, third.stdout


# Stands in for the planner's backend on the one run a validator's exit-hook `wake` starts: records
# that it ran and answers with Claude's refusal, exiting 1 the way the CLI does.
REFUSED_PLANNER_STUB = """#!/usr/bin/env bash
: > "$PLANNER_STUB_MARK"
cat "$PLANNER_STUB_STREAM"
exit 1
"""


def test_a_planner_refused_after_the_validator_that_woke_it_leaves_the_verdict_exhausted(
    launch_environment, tmp_path, monkeypatch
):
    """The review's sequence, as it happened on this host on 2026-09-21: a validator finishes
    cleanly, its exit hook's `wake` runs a planner synchronously, Claude refuses that planner, and
    the validator's log is appended to AFTER the planner has ended (the wake's own lines, the
    worktree's removal). Dated by log mtimes, the validator's `allowed` was the newest Claude
    observation and the refusal was lost. Dated by the backends' exit markers, the wake itself
    leaves the verdict `exhausted`, aged from the planner's exit, and the next role launch takes
    the fallback. Only the backends are stubs: the driver, its exit hook, `guard wake` and
    `planner_task.sh` are the real ones, in a sandboxed cache."""
    cache = pathlib.Path(launch_environment["AGENT_CACHE_DIR"])
    planner_cache = cache / "planner"
    launch_environment["WORKER_CACHE_DIR"] = str(cache)
    # `planner_task.sh` and `guard.planner_dir` must agree on where the planner's run lands; with
    # `AGENT_CACHE_DIR` set they would not unless this names it.
    launch_environment["PLANNER_CACHE_DIR"] = str(planner_cache)
    for name, value in (
        ("AGENT_CACHE_DIR", cache),
        ("WORKER_CACHE_DIR", cache),
        ("PLANNER_CACHE_DIR", planner_cache),
    ):
        monkeypatch.setenv(name, str(value))

    stub = pathlib.Path(launch_environment["AGENT_CLAUDE_BIN"])
    qwen_mark = tmp_path / "qwen-was-launched.txt"
    claude_mark = tmp_path / "claude-was-launched.txt"
    planner_mark = tmp_path / "planner-was-launched.txt"
    launch_environment["AGENT_QWEN_BIN"] = str(
        _marking_stub(tmp_path / "bin" / "qwen", qwen_mark, stub)
    )
    launch_environment["AGENT_CLAUDE_BIN"] = str(
        _marking_stub(tmp_path / "bin" / "claude", claude_mark, stub)
    )
    planner_stub = tmp_path / "bin" / "planner-backend"
    planner_stub.write_text(REFUSED_PLANNER_STUB)
    planner_stub.chmod(0o755)
    launch_environment.update(
        # Both of the planner driver's backends, so whichever the gate picks never reaches a real
        # CLI; before the refusal the gate reads the validator's `allowed` and picks claude.
        PLANNER_CLAUDE_BIN=str(planner_stub),
        PLANNER_QWEN_BIN=str(planner_stub),
        PLANNER_STUB_MARK=str(planner_mark),
        PLANNER_STUB_STREAM=str(
            _stream_file(tmp_path / "rejected.jsonl", REJECTED_BY_THE_QUOTA_STREAM)
        ),
        STUB_STREAM=str(_stream_file(tmp_path / "served.jsonl", SERVED_STREAM)),
    )
    _, task_class = load_role_class("validator")
    fallback = task_class.fallback
    assert fallback is not None, "config.example.yaml declares no validator fallback"

    # The validator runs on its own backend, is served, and its exit hook wakes the planner.
    first = _launch(launch_environment, "validator", no_wake=False)
    assert first.returncode == 0, first.stdout + first.stderr
    assert claude_mark.is_file() and not qwen_mark.exists()
    assert planner_mark.is_file(), "the exit hook's wake never ran the planner"
    validator_log = _printed_path(first.stdout, "log")
    planner_logs = sorted(planner_cache.glob("*.log"))
    assert len(planner_logs) == 1, planner_logs
    validator_exited = read_role_run_exit_marker(validator_log)
    planner_exited = read_role_run_exit_marker(planner_logs[0])
    assert validator_exited is not None and planner_exited is not None
    # The review's premise, measured: the validator's log was still being written after the
    # planner it woke had exited, while its backend had exited before that planner started.
    assert validator_exited < planner_exited
    assert validator_log.stat().st_mtime_ns > _ns(planner_exited)

    # No tick has run: the wake itself folded the planner's refusal before it returned.
    verdict = read_persisted_quota_verdict(task_class.backend)
    assert verdict.status == "exhausted", verdict.reason
    verdict_file = cache / f"agent_guard_{task_class.backend}.json"
    assert verdict_file.stat().st_mtime_ns == _ns(planner_exited)
    # And a tick after it re-reads nothing into a fresher verdict.
    monkeypatch.setattr(agent_guard, "_agents_paused", lambda *, main: True)
    agent_guard.tick(main=tmp_path)
    assert verdict_file.stat().st_mtime_ns == _ns(planner_exited)

    for mark in (qwen_mark, claude_mark):
        mark.unlink(missing_ok=True)
    second = _launch(launch_environment, "validator")
    assert second.returncode == 0, second.stdout + second.stderr
    assert qwen_mark.is_file(), "the fallback backend was never launched"
    assert not claude_mark.exists(), "Claude was launched on a verdict that read exhausted"
