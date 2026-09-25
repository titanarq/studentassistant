"""`agent_os/bin/worker_task.sh start` refuses an issue that is not a brief.

The issue is the unit of work (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-
labels-are-the-mechanical-state.md): `start` takes an issue number, not a file, and an issue whose
body does not validate is not dispatchable. These tests run the real driver with a stub `gh` first
on `PATH` and a throwaway git worktree, and stop at the refusal -- they never reach the backend
CLI, so no worker is ever launched and no `.cache/worker_*` state is written.

Pure filesystem and subprocess. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import json
import os
import pathlib
import re
import subprocess
import time

import pytest
import yaml
from conftest import EXAMPLE_CONFIG, config_with_never_run

from agent_os import guard as agent_guard
from agent_os.cli import AGENT_OS_DIR, host_root
from agent_os.lib import (
    REQUIRED_SECTIONS,
    forbidden_paths_regex,
    forbidden_paths_rules,
    load_mechanism,
    load_planner_config,
    load_project,
    load_task_classes,
    mechanism_paths_rules,
    never_run_rules,
    worker_environment_rules,
)

# The HOST project this suite runs inside: not a fixed nesting under AGENT_OS_DIR (that
# breaks the moment a copy IS the mechanism's own top directory, as the out-of-tree proof
# makes it -- #512), but whatever `host_root()` itself resolves: the git checkout's toplevel,
# same as every real driver run.
ROOT = host_root()
DRIVER = AGENT_OS_DIR / "bin" / "worker_task.sh"

VALID_BODY = (
    "\n\n".join(f"{heading}\nsomething" for heading in REQUIRED_SECTIONS).replace(
        "## Stages\nsomething", "## Stages\n- [ ] Do the thing"
    )
    + "\n\n<!-- budget: complex-qwen -->"
)

GH_STUB = """#!/usr/bin/env python3
# Stands in for `gh`: answers `issue view --json body` with $GH_STUB_BODY, plus the calls
# `issues.py move` makes, every one a `gh api` since #27 (the issue and its label over REST, the
# board item over GraphQL) -- `resume` re-asserts `status:doing` since #385, and a driver test about
# something else must not turn that into a cascade of refusals. Any other call is a test failure,
# not a silent success: the driver must not reach the network.
import json
import os
import sys

args = sys.argv[1:]
if args[:2] == ["issue", "view"] and "--json" in args:
    if "-q" in args:
        print(os.environ["GH_STUB_BODY"])
    else:
        print(json.dumps({"body": os.environ["GH_STUB_BODY"], "labels": [], "state": "OPEN"}))
    sys.exit(0)
if args[:2] == ["label", "list"]:
    print(json.dumps([{"name": "status:doing"}]))
    sys.exit(0)
if args[:2] == ["project", "item-list"]:
    print(json.dumps({"items": []}))
    sys.exit(0)
issue_path = args[1].split("/") if args[:1] == ["api"] and len(args) > 1 else []
if len(issue_path) == 5 and issue_path[0] == "repos" and issue_path[3] == "issues":
    # `issues.py validate` reads the issue over REST since #70, not with `gh issue view`.
    print(json.dumps({"body": os.environ.get("GH_STUB_BODY", ""), "number": int(issue_path[4])}))
    sys.exit(0)
if args[:1] == ["api"]:
    print(json.dumps({"number": 348}))
    sys.exit(0)
print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(3)
"""


@pytest.fixture
def driver_environment(tmp_path):
    """A throwaway, clean git worktree for the backend plus a stub `gh` first on `PATH`."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # On `main`, because `start` refuses a worktree that is on neither the issue's base nor a
    # branch naming the issue (#388) -- these tests are about the refusals that come after it.
    subprocess.run(["git", "init", "-q", "-b", "main", str(worktree)], check=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        # `start` creates its cache before it gets to the refusals these tests read, so a
        # disposable one keeps that out of the checkout's real `.cache` (agent-os#25).
        WORKER_CACHE_DIR=str(tmp_path / "cache"),
        AGENT_OS_GH_REPO="owner/name",
    )
    return environment


def _start(environment, *arguments, backend="claude"):
    return subprocess.run(
        ["bash", str(DRIVER), backend, "start", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_start_refuses_an_issue_whose_body_does_not_validate(driver_environment):
    driver_environment["GH_STUB_BODY"] = "just a sentence, no sections, no budget"
    result = _start(driver_environment, "347")
    assert result.returncode == 1
    assert "does not validate" in result.stdout
    assert "missing section: ## Objective" in result.stdout


def test_start_refuses_the_retired_brief_first_form(driver_environment):
    # `start <brief.md> <issue>` is gone: the first argument is the issue number.
    driver_environment["GH_STUB_BODY"] = VALID_BODY
    result = _start(driver_environment, "scratchpad/brief_x.md", "347")
    assert result.returncode == 2
    assert "start <issue> [extra-brief.md]" in result.stdout


def test_start_refuses_a_supplement_that_does_not_exist(driver_environment):
    driver_environment["GH_STUB_BODY"] = VALID_BODY
    result = _start(driver_environment, "347", "no/such/file.md")
    assert result.returncode == 1
    assert "no such supplement" in result.stdout


# ---------------------------------------------------------------------------------------------
# A fresh `start` must never inherit the previous run's issue/label marker (#350 follow-up): if
# `move doing` fails, the file must not still carry the old issue's marker, or the guard's tick
# reports false drift against an issue this run has nothing to do with. Unlike the refusals
# above, this test runs far enough to write `.cache/worker_*` state, so it points
# WORKER_CACHE_DIR at a throwaway directory rather than reusing `driver_environment`.
# ---------------------------------------------------------------------------------------------

GH_STUB_MOVE_FAILS = """#!/usr/bin/env python3
# Answers `issue view --json body...` (validate, brief -- which also needs `number` in the
# response, unlike GH_STUB above) with $GH_STUB_BODY, and so does the REST read of the issue
# that `validate` makes since #70. Every other `gh api` call fails deterministically -- `issues.py move` reads the current labels with a REST `GET` (#27), and the
# label check before it is REST too -- so `move doing` itself fails, simulating a transient gh/API
# hiccup, which is the case this test is about: the marker must be dropped whatever the reason
# the move failed.
import json
import os
import sys

args = sys.argv[1:]
issue_path = args[1].split("/") if args[:1] == ["api"] and len(args) > 1 else []
if len(issue_path) == 5 and issue_path[0] == "repos" and issue_path[3] == "issues":
    # `issues.py validate` reads the issue over REST since #70, not with `gh issue view`.
    print(json.dumps({"body": os.environ.get("GH_STUB_BODY", ""), "number": int(issue_path[4])}))
    sys.exit(0)
if args[:1] == ["api"]:
    print("simulated gh failure", file=sys.stderr)
    sys.exit(7)
if args[:2] == ["issue", "view"] and "--json" in args:
    number = int(args[2])
    if "-q" in args:
        print(os.environ["GH_STUB_BODY"])
    else:
        print(json.dumps({"body": os.environ["GH_STUB_BODY"], "number": number}))
    sys.exit(0)
print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(3)
"""


# Never exits: `start` backgrounds the backend CLI and, once IT exits, the same subshell calls
# `open-pr` and then `agent_guard.py check` -- the exit hook. Both honour WORKER_CACHE_DIR, so that
# hook writes into this test's throwaway cache (#377), not the repository's real one; what it would
# still do is write `DONE` over the `STARTED` line 1 of the `worker_claude.state` this test asserts
# on, at a moment the test does not choose. Blocking the stub keeps the backend "alive" until the
# test stops it by PID through the driver's own `stop`.
CLAUDE_STUB_NEVER_EXITS = "#!/usr/bin/env bash\nexec sleep 600\n"


def _stop(environment, backend="claude"):
    return subprocess.run(
        ["bash", str(DRIVER), backend, "stop"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_start_drops_a_stale_marker_when_the_move_to_doing_fails(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(worktree)], check=True)

    # The previous run's own marker -- a different issue, already finished (DONE) and moved on to
    # ai-completed. A worker that inherits this into the new run's STARTED line would make the
    # tick report drift against issue #100, which this run never touches.
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "worker_claude.state").write_text("DONE\nissue=100 label=status:ai-completed\n")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh_stub = binaries / "gh"
    gh_stub.write_text(GH_STUB_MOVE_FAILS)
    gh_stub.chmod(0o755)
    # Stubbed so `start` never reaches the real Claude CLI -- see CLAUDE_STUB_NEVER_EXITS above.
    claude_stub = binaries / "claude"
    claude_stub.write_text(CLAUDE_STUB_NEVER_EXITS)
    claude_stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        AGENT_OS_GH_REPO="owner/name",
        GH_STUB_BODY=VALID_BODY,
        GH_TOKEN="stub-token-so-no-app-is-minted",
    )
    try:
        result = _start(environment, "347")
        assert "could not move #347 to doing" in result.stdout, result.stdout + result.stderr

        lines = (cache / "worker_claude.state").read_text().splitlines()
        assert lines[0] == "STARTED"
        assert not any(line.startswith("issue=") for line in lines), lines
    finally:
        # The stubbed backend blocks forever on purpose (above) -- stop it by PID, through the
        # driver's own `stop`, so no process is left sleeping past this test.
        _stop(environment)


def test_the_resolved_rules_name_the_main_checkout_by_derivation_not_a_literal():
    """The worker's own checkout is a worktree, so a hardcoded main-checkout path in its RULES was
    wrong the day this repository's clone moved -- it is derived from git now (same style as the
    __HUMAN_LOGIN__ substitution), and this asserts the substitution, not the literal."""
    common_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    main_checkout = str(pathlib.Path(common_dir).parent)
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert main_checkout in result.stdout
    assert "__MAIN_CHECKOUT__" not in result.stdout


def test_a_host_root_that_is_not_a_git_repository_fails_loudly_and_renders_no_rules(tmp_path):
    """`main_checkout()` derives the checkout from `git rev-parse --git-common-dir`, and a host
    root with no `.git` at all must not resolve to a bare "." -- the defect a golden comparison
    caught once already (#512 follow-up): under `set -uo pipefail` (no `-e`), `dirname` on a
    failed substitution still exits 0, so an unchecked call renders a half-written prompt instead
    of stopping the driver. This asserts the driver now stops before rendering anything at all and
    says why on stderr, the regression this golden certified without anyone noticing."""
    host = tmp_path / "host"
    cache = host / ".cache"
    cache.mkdir(parents=True)
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "rules"],
        cwd=host,
        env={
            **os.environ,
            "AGENT_OS_HOST_ROOT": str(host),
            "WORKER_CACHE_DIR": str(cache),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "main_checkout" in result.stderr
    assert "not a git repository" in result.stderr


def test_the_resolved_rules_name_the_test_command_by_substitution_not_a_literal():
    """#350: the worker's own connection is read-only by default, and the rules point it back to
    the owner role through `project.test_command` -- injected the same way as __HUMAN_LOGIN__ and
    __MAIN_CHECKOUT__, so the mechanism itself carries no `scripts/test.sh` literal
    (agent_os/docs/adr/2026-09-15-workers-connect-read-only-by-default-and-reach-the-owner-only-through-
    the-test-runner.md)."""
    test_command = load_project().test_command
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert test_command in result.stdout
    assert "read-only by default" in result.stdout
    assert "__TEST_COMMAND__" not in result.stdout


def test_the_resolved_rules_carry_the_writing_to_the_human_paragraph_in_the_configured_language():
    """Same substitution as __HUMAN_LOGIN__ and __MAIN_CHECKOUT__ above, for the shared
    WRITING TO THE HUMAN wording (`agent_lib.human_message_rules`,
    agent_os/docs/adr/2026-09-15-a-question-for-the-human-is-written-in-their-language-and-in-functional-
    terms.md): the worker's BLOCKED question comment is written in `project.human_language`, and
    the placeholder that injects the rule leaves nothing behind."""
    language = load_project().human_language
    assert language, "config/agents.yaml names no project.human_language"
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WRITING TO THE HUMAN" in result.stdout
    assert language in result.stdout
    assert "__HUMAN_MESSAGE_RULES__" not in result.stdout


def test_the_worker_rules_forbid_committing_the_diary_and_ask_for_it_nowhere():
    """#407: the diary reached `main` inside a worker's own pull request, and from then on every
    worker branch conflicted with `main` on it. What keeps it out of a commit is the worker's own
    instruction, so the rendered RULES have to forbid staging it -- and nothing in them may still
    tell the worker to commit the file they forbid committing."""
    flattened = _flattened(_rendered_rules())

    assert "Never stage or commit `scratchpad/progress.log`" in flattened

    # Every sentence that names the diary either says nothing about staging it or forbids it.
    # Read over the flattened prompt because the prohibition wraps across the prompt's own line
    # breaks, and over sentences rather than the whole text because a blanket "no commit verb
    # anywhere near the diary" would also fail on the RULES' own `WIP: cut by guard` prose.
    mentions = [
        sentence for sentence in re.split(r"(?<=[.!?])\s+", flattened) if "progress.log" in sentence
    ]
    assert len(mentions) >= 2, mentions
    for sentence in mentions:
        if re.search(r"\b(stage|staged|staging|commit|committed)\b", sentence):
            assert re.search(r"\b(never|not|no)\b", sentence), sentence


# ---------------------------------------------------------------------------------------------
# `project.forbidden_paths` and `mechanism.own_paths`: the two lists behind the ownership rule, and
# each one is behind two halves of it -- the paragraph injected into the worker's RULES and the
# regex `collect` audits the run's changed paths with. Two copies of the single list this replaced
# had already drifted apart, and the doc's prose named four paths fewer than the regex protected
# (agent_os/docs/AGENT_OS.md §7 rows (a) and (s), #363). What separates the two lists is the rule a match
# triggers, and the two paragraphs state them as a pair (#390).
# ---------------------------------------------------------------------------------------------

SENTINEL_PATHS = ("sentinel/keep-out/*", "sentinel/OWNERS.md")

# The paragraph's own heading and closing line, both rendered by `agent_lib.forbidden_paths_rules`
# from the configured list -- what a test splits on to read the list back out of the prompt.
FORBIDDEN_HEADING = "FILES YOU MUST NOT TOUCH"
FORBIDDEN_CLOSING = (
    "If a task genuinely requires one of these, stop and say so rather than working around it."
)

# The second paragraph's heading and closing line, rendered the same way by
# `agent_lib.mechanism_paths_rules` from `mechanism.own_paths`. The two paragraphs are the one
# ownership rule stated as a contrast, so the second one's closing line is the whole section's last
# line, and that is what the boundary assertions further down split on (#390).
MECHANISM_HEADING = "FILES OF THE MECHANISM'S OWN"
MECHANISM_CLOSING = "If the body does not name one, stop and say so rather than working around it."

# The never-run paragraph's own heading, rendered the same way from `project.never_run` by
# `agent_lib.never_run_rules` -- the split point a test reads the configured commands out of, and
# the paragraph that follows the two ownership ones in the worker's RULES.
NEVER_RUN_HEADING = "COMMANDS YOU MUST NEVER RUN"
NEVER_RUN_CLOSING = (
    "The reason is part of the rule: it is what you judge an edge case against. If a task "
    "genuinely\nneeds one of these, stop and say so rather than working around it."
)


def _patch_path_list(text, key, paths):
    """One `  <key>:` list of a config/agents.yaml copy replaced by `paths` -- both halves of the
    ownership rule are configured in the same shape, so both are patched the same way."""
    block = (
        f"  {key}: []\n" if not paths else f"  {key}:\n" + "".join(f"    - {p}\n" for p in paths)
    )
    patched, count = re.subn(
        rf"^  {key}:\n(?:    - .*\n)+", block, text, count=1, flags=re.MULTILINE
    )
    assert count == 1, f"config.example.yaml's {key} block changed shape"
    return patched


def _config_with_path_lists(tmp_path, forbidden_paths, own_paths):
    """A config where BOTH audited lists are the test's: `project.forbidden_paths`, which no body
    can authorize, and `mechanism.own_paths`, which only a body that names the path can."""
    text = _patch_path_list(EXAMPLE_CONFIG.read_text(), "forbidden_paths", forbidden_paths)
    path = tmp_path / "agents.yaml"
    path.write_text(_patch_path_list(text, "own_paths", own_paths))
    return path


def _config_with_forbidden_paths(tmp_path, paths):
    """A copy of config.example.yaml with `project.forbidden_paths` replaced by `paths` --
    the same `AGENTS_CONFIG_PATH` isolation `_config_with_max_parallel_issues` uses, so a test can
    render the worker's RULES from a list this repository does not ship."""
    text = _patch_path_list(EXAMPLE_CONFIG.read_text(), "forbidden_paths", paths)
    path = tmp_path / "agents.yaml"
    path.write_text(text)
    return path


def _rendered_rules(config_path=None):
    environment = dict(os.environ)
    if config_path is not None:
        environment["AGENTS_CONFIG_PATH"] = str(config_path)
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "rules"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _paragraph(rules, heading):
    """One RULES paragraph's body: what stands between its heading and the blank line that ends it.
    Every paragraph these tests read is rendered by one `agent_lib` function and injected whole, so
    the blank line is always its boundary."""
    assert heading in rules, heading
    return rules.split(heading + "\n")[1].split("\n\n")[0]


def _grep_matches(pattern, corpus):
    """The paths of `corpus` the pattern matches, through the real `grep -E` the driver audits
    with -- so a pattern only POSIX ERE would reject cannot pass here."""
    result = subprocess.run(
        ["grep", "-E", pattern],
        input="\n".join(corpus),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr  # 1 is grep's "no line selected"
    assert not result.stderr, result.stderr
    return set(result.stdout.splitlines())


def test_the_rules_paragraph_and_the_audit_regex_are_rendered_from_one_configured_list(tmp_path):
    """The two halves of the ownership rule read the SAME `project.forbidden_paths`: a list this
    repository does not ship shows up verbatim in the paragraph the worker is given, and the regex
    `collect` audits with matches those very paths and nothing else. Neither half can now name a
    path the other does not protect."""
    config = _config_with_forbidden_paths(tmp_path, SENTINEL_PATHS)
    rules = _rendered_rules(config)

    assert FORBIDDEN_HEADING in rules
    assert "__FORBIDDEN_PATHS_RULES__" not in rules
    listing = _paragraph(rules, FORBIDDEN_HEADING).splitlines()[0]
    assert sorted(listing.rstrip(".").split(", ")) == sorted(SENTINEL_PATHS)
    # A path the sentinel list dropped is no longer named anywhere in the prompt, which is what
    # proves the paragraph is rendered from the list rather than left over from a literal.
    assert "config/cik_chains.yaml" not in rules

    derived = forbidden_paths_regex(load_project(config))
    assert derived, "a non-empty forbidden_paths rendered an empty audit regex"
    assert _grep_matches(
        derived,
        ("sentinel/keep-out/deeper/nested.md", "sentinel/OWNERS.md", "config/cik_chains.yaml"),
    ) == {"sentinel/keep-out/deeper/nested.md", "sentinel/OWNERS.md"}


def test_a_project_that_forbids_no_path_gets_no_paragraph_and_no_stub_heading(tmp_path):
    """An empty `project.forbidden_paths` renders nothing at all -- not a heading with an empty
    list under it, and not the placeholder that would have carried one -- and the sections around
    it stay separated by exactly the one blank line every other section boundary has."""
    rules = _rendered_rules(_config_with_forbidden_paths(tmp_path, []))
    assert FORBIDDEN_HEADING not in rules
    assert "__FORBIDDEN_PATHS_RULES__" not in rules
    assert forbidden_paths_regex(load_project(tmp_path / "agents.yaml")) == ""
    # The paragraph's own line and the blank line that followed it are both gone: what is left
    # between the section above it and the paragraph below it is the one blank line every other
    # boundary has.
    between = rules.split("next process to build on.\n")[1].split(NEVER_RUN_HEADING)[0]
    assert between == "\n", repr(between)


def test_the_worker_rules_carry_both_paragraphs_of_the_ownership_rule(tmp_path):
    """Two lists, two paragraphs, one rule each: the host project's paths, which no brief can
    authorize, and the mechanism's own files, which a brief can. Each paragraph is rendered from the
    very list `collect` audits that half with, so neither can state a rule over paths the audit
    judges by the other one (#390)."""
    config = _config_with_path_lists(tmp_path, SENTINEL_PATHS, SENTINEL_MECHANISM_PATHS)
    rules = _rendered_rules(config)

    assert "__FORBIDDEN_PATHS_RULES__" not in rules
    assert "__MECHANISM_PATHS_RULES__" not in rules
    # The rule comes before its exception: a worker that stops reading at the first heading has read
    # the half that refuses, not the half that permits.
    assert rules.index(FORBIDDEN_HEADING) < rules.index(MECHANISM_HEADING)

    host = _paragraph(rules, FORBIDDEN_HEADING)
    mechanism = _paragraph(rules, MECHANISM_HEADING)
    assert sorted(host.splitlines()[0].rstrip(".").split(", ")) == sorted(SENTINEL_PATHS)
    assert sorted(mechanism.splitlines()[0].rstrip(".").split(", ")) == sorted(
        SENTINEL_MECHANISM_PATHS
    )
    # Neither list reaches into the other's paragraph: a path in both would be one the worker is
    # told two contradictory rules about, and the audit resolves that by refusing it.
    for path in SENTINEL_PATHS:
        assert path not in mechanism, path
    for path in SENTINEL_MECHANISM_PATHS:
        assert path not in host, path


def test_each_paragraph_states_its_own_rule_in_its_own_words():
    """The wording the criterion asks for, read out of the prompt the worker actually receives: a
    host path is a defect in the brief and the worker stops and says so; a mechanism path the body
    names is allowed, and the worker says it is touching one before the first commit that does."""
    rules = _rendered_rules()
    host = _flattened(_paragraph(rules, FORBIDDEN_HEADING))
    mechanism = _flattened(_paragraph(rules, MECHANISM_HEADING))

    assert "no brief can authorize one" in host, host
    assert "a defect in the brief" in host, host
    assert FORBIDDEN_CLOSING in host, host
    assert "only when the issue body names that path" in mechanism, mechanism
    assert "before the first commit that touches one" in mechanism, mechanism
    assert MECHANISM_CLOSING in mechanism, mechanism
    # Each half points at the other, which is why the driver injects the pair or neither.
    assert "The mechanism's own files below" in host, host
    assert "the rule above that forbids writing there at all" in mechanism, mechanism


def test_the_workers_two_paragraphs_are_the_configured_lists_verbatim():
    """What the real config/agents.yaml protects is what the worker reads, list by list: a path the
    config carries and the prompt loses is a path whose rule the worker was never told, so this goes
    path by path rather than trusting the two headings to be there."""
    host_paths = load_project().forbidden_paths
    mechanism_paths = load_mechanism().own_paths
    assert host_paths and mechanism_paths, "config/agents.yaml ships an empty path list"
    rules = _rendered_rules()

    host = _paragraph(rules, FORBIDDEN_HEADING)
    mechanism = _paragraph(rules, MECHANISM_HEADING)
    for path in host_paths:
        assert path in host, path
    for path in mechanism_paths:
        assert path in mechanism, path


def test_a_mechanism_that_names_none_of_its_own_files_renders_neither_paragraph(tmp_path):
    """The pair renders whole or not at all. With no `mechanism.own_paths` there is no exception to
    state, and a paragraph saying "no brief can authorize these" beside nothing that says which
    briefs can is half a rule -- so neither heading reaches the worker, no stub is left behind, and
    the sections around the gap keep the single blank line every other boundary has. What the gate
    narrows is what the worker reads, never what it is judged by: the host list is still configured
    and `collect` still refuses a diff that touches it."""
    config = _config_with_path_lists(tmp_path, SENTINEL_PATHS, [])
    rules = _rendered_rules(config)

    assert FORBIDDEN_HEADING not in rules
    assert MECHANISM_HEADING not in rules
    assert "__FORBIDDEN_PATHS_RULES__" not in rules
    assert "__MECHANISM_PATHS_RULES__" not in rules
    assert "\n\n\n" not in rules
    between = rules.split("next process to build on.\n")[1].split(NEVER_RUN_HEADING)[0]
    assert between == "\n", repr(between)
    assert forbidden_paths_rules(load_project(config)), "the sentinel host list rendered nothing"
    assert mechanism_paths_rules(load_mechanism(config)) == ""

    audit = _collect_environment(tmp_path / "still-audited", "sentinel/keep-out/x.md", config)
    assert "VIOLATION" in audit, audit


def _collect_environment(run_dir, changed_path, config_path, issue_body=None):
    """A throwaway worktree whose one commit touches `changed_path`, the driver's cache moved
    somewhere disposable and its start ref at that commit's parent -- everything `collect` reads
    to run its ownership audit over a real diff. `run_dir` is this call's own: one per audit.
    `issue_body` is the body the run would already have read into its cache, which is the only
    thing that can authorize a mechanism file."""
    worktree = run_dir / "worktree"
    worktree.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    start_ref = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    touched = worktree / changed_path
    touched.parent.mkdir(parents=True, exist_ok=True)
    touched.write_text("the worker wrote this\n")
    _git("add", changed_path, cwd=worktree)
    _git("commit", "-qm", "the work", cwd=worktree)

    cache = run_dir / "cache"
    cache.mkdir(parents=True)
    (cache / "worker_claude.startref").write_text(start_ref)
    if issue_body is not None:
        (cache / "worker_claude.body.md").write_text(issue_body)

    environment = dict(os.environ)
    environment.update(
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        AGENTS_CONFIG_PATH=str(config_path),
    )
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "collect"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.split("=== ownership audit ===\n")[1].split("=== ")[0]


def test_collect_flags_a_path_the_configured_list_protects_and_not_one_it_does_not(tmp_path):
    """The audit's verdict end to end: the regex built from `project.forbidden_paths` is what
    decides, through the driver's own `collect`, over a real commit's real changed files."""
    config = _config_with_forbidden_paths(tmp_path, SENTINEL_PATHS)

    violation = _collect_environment(
        tmp_path / "violation", "sentinel/keep-out/deeper/nested.md", config
    )
    assert "VIOLATION" in violation, violation
    assert "sentinel/keep-out/deeper/nested.md" in violation, violation

    clean = _collect_environment(tmp_path / "clean", "sentinel/open/nested.md", config)
    assert "clean: nothing outside the agent's own paths" in clean, clean


def test_collect_skips_the_audit_when_the_project_forbids_no_path(tmp_path):
    """An empty pattern matches every line, so `collect` must say the project forbids nothing
    rather than report every file the run touched as a violation."""
    config = _config_with_forbidden_paths(tmp_path, [])
    audit = _collect_environment(
        tmp_path / "nothing-forbidden", "sentinel/keep-out/deeper/nested.md", config
    )
    assert "nothing forbidden: project.forbidden_paths is empty" in audit, audit
    assert "VIOLATION" not in audit, audit


# The second list in the same sentinel vocabulary as SENTINEL_PATHS, and one concrete path under
# it: the audit's mechanism half runs over the same fnmatch reading its host half does.
SENTINEL_MECHANISM_PATHS = ("sentinel/machinery/*",)
MECHANISM_FILE = "sentinel/machinery/driver.sh"


def test_collect_refuses_a_host_path_even_when_the_issue_body_names_it(tmp_path):
    """The host project's list holds whatever the brief says: a body naming one of its paths is a
    defect in the brief, not an authorization, and the audit refuses the diff all the same (#390)."""
    config = _config_with_path_lists(tmp_path, SENTINEL_PATHS, SENTINEL_MECHANISM_PATHS)
    audit = _collect_environment(
        tmp_path / "host-named",
        "sentinel/keep-out/deeper/nested.md",
        config,
        issue_body="## Objective\nRewrite sentinel/keep-out/deeper/nested.md in full.\n",
    )
    assert "VIOLATION -- these paths were off limits" in audit, audit
    assert "sentinel/keep-out/deeper/nested.md" in audit, audit
    assert "allowed" not in audit, audit
    assert "clean: no file of the mechanism's own touched" in audit, audit


def test_collect_refuses_a_mechanism_path_the_issue_body_does_not_name(tmp_path):
    """The mechanism's own list is conditional in the other direction: touching the machinery is
    the violation when the body this run already read does not name the path."""
    config = _config_with_path_lists(tmp_path, SENTINEL_PATHS, SENTINEL_MECHANISM_PATHS)
    audit = _collect_environment(
        tmp_path / "mechanism-unnamed",
        MECHANISM_FILE,
        config,
        issue_body="## Objective\nRewrite the sentinel corpus reader.\n",
    )
    assert "VIOLATION -- files of the mechanism's own the issue body does not name" in audit, audit
    assert MECHANISM_FILE in audit, audit
    # The two verdicts stay independent: the host half is clean and says so.
    assert "clean: nothing outside the agent's own paths" in audit, audit


def test_collect_allows_a_mechanism_path_the_issue_body_names(tmp_path):
    """The same diff, with the path in the body -- allowed, and named as allowed so the reviewer
    reads the authorization off the audit instead of having to reconstruct it."""
    config = _config_with_path_lists(tmp_path, SENTINEL_PATHS, SENTINEL_MECHANISM_PATHS)
    audit = _collect_environment(
        tmp_path / "mechanism-named",
        MECHANISM_FILE,
        config,
        issue_body="## Objective\nThe audit in `sentinel/machinery/driver.sh` reads both lists.\n",
    )
    assert "allowed -- the issue body names these files of the mechanism's own" in audit, audit
    assert MECHANISM_FILE in audit, audit
    assert "VIOLATION" not in audit, audit


def test_collect_refuses_a_mechanism_path_when_no_issue_body_was_recorded(tmp_path):
    """Nothing can have authorized a mechanism file if the run never wrote its body down, and the
    audit says that is why instead of reporting a path the body merely failed to name."""
    config = _config_with_path_lists(tmp_path, SENTINEL_PATHS, SENTINEL_MECHANISM_PATHS)
    audit = _collect_environment(tmp_path / "mechanism-nobody", MECHANISM_FILE, config)
    assert "VIOLATION -- files of the mechanism's own, and no issue body at" in audit, audit
    assert MECHANISM_FILE in audit, audit


# ---------------------------------------------------------------------------------------------
# `project.never_run`: the commands no role may run, each with the reason its prohibition rests
# on. ONE list behind the worker's, the validator's and the refiner's RULES -- the same six verbs
# used to be spelled once per block, and the validator's copy had already lost one of them
# (agent_os/docs/AGENT_OS.md §7 row (b), #363). `tests/test_agent_task.py` asserts the other two blocks
# read this very same list.
# ---------------------------------------------------------------------------------------------

# Neither the commands nor their reasons exist anywhere else in this repository, so whatever
# reaches the rendered RULES came from the list the test configured and from nothing left over in
# the driver's own text. Each one is short enough to render as a single bullet.
SENTINEL_COMMANDS = (
    ("sentinel-rebuild", "rebuilds the sentinel grid and moves every stamp it seals"),
    ("--sentinel-write", "persists the sentinel census; without it the command is a dry run"),
)

# A verb the real config forbids and the sentinel list does not: its absence from a prompt
# rendered from that list is what proves the driver keeps no copy of its own.
DROPPED_COMMAND = "census-build"


def _flattened(rules):
    """The prompt with its line breaks reduced to single spaces: `never_run_rules` wraps a bullet
    at the prompt's own width, and what a test asserts is a command beside its reason, not where
    the line happened to break."""
    return " ".join(rules.split())


def test_the_worker_rules_render_every_configured_command_with_its_own_reason(tmp_path):
    """The paragraph comes from `project.never_run` and from nothing else: a list this repository
    does not ship reaches the worker's prompt whole -- one bullet per item, each carrying the
    reason that justifies it -- and a verb the list dropped is named nowhere in it."""
    config = config_with_never_run(tmp_path, SENTINEL_COMMANDS)
    rules = _rendered_rules(config)

    assert NEVER_RUN_HEADING in rules
    assert "__NEVER_RUN_RULES__" not in rules
    assert never_run_rules(load_project(config)) in rules
    flattened = _flattened(rules)
    for command, reason in SENTINEL_COMMANDS:
        assert f"`{command}` -- {reason}" in flattened, command
    assert DROPPED_COMMAND not in rules


def test_the_workers_paragraph_is_the_configured_list_verbatim():
    """What the real config/agents.yaml forbids is what the worker reads. A command the config
    carries and the prompt loses is a command the worker has no stated reason not to run, so this
    goes item by item rather than trusting the paragraph to be there."""
    configured = load_project().never_run
    assert configured, "config/agents.yaml forbids no command at all"
    rules = _rendered_rules()

    assert never_run_rules(load_project()) in rules
    flattened = _flattened(rules)
    for item in configured:
        assert f"`{item.command}` -- {item.reason}" in flattened, item.command


def test_a_project_that_forbids_no_command_gets_no_paragraph_and_no_stub_heading(tmp_path):
    """An empty `project.never_run` renders nothing at all -- not a heading with nothing under it,
    and not a gap twice as wide as the one the paragraph filled: the placeholder's own line goes
    with it, and what is left around it is the single blank line every other boundary has.

    `config.example.yaml` names no `prompt_extras` either, so the next paragraph the template
    actually renders is the environment one -- ENVIRONMENT_HEADING, not a host's own extension-
    point text, is what a config-agnostic test can rely on following it (#512)."""
    rules = _rendered_rules(config_with_never_run(tmp_path, []))

    assert NEVER_RUN_HEADING not in rules
    assert "__NEVER_RUN_RULES__" not in rules
    assert "\n\n\n" not in rules
    between = rules.split(MECHANISM_CLOSING + "\n")[1].split(ENVIRONMENT_HEADING)[0]
    assert between == "\n", repr(between)


# ---------------------------------------------------------------------------------------------
# `project.worker_environment`: the variables exported into the worker's own process, and ONE list
# behind both halves of that rule too -- the export loop `start` runs before launching the backend,
# and the paragraph telling the worker what it is running inside. The sentence this replaced spelled
# the shared server's port in the driver's own text, which is a second copy of the config and a
# stale one the day the port moves (agent_os/docs/AGENT_OS.md §7 row (a), #363).
# ---------------------------------------------------------------------------------------------

# A name and a value that exist nowhere else in this repository, so whatever reaches the rendered
# RULES came from the mapping the test configured and from nothing left over in the driver's text.
SENTINEL_ENVIRONMENT = {
    "SENTINEL_URL": "postgresql+psycopg://sentinel_ro:sentinel_pw@localhost:9999/sentinel_db",
}

# The paragraph's own heading, rendered by `agent_lib.worker_environment_rules` from the configured
# keys -- what a test splits on. It follows `__PROJECT_EXTRAS__` in the template
# (`agent_os/prompts/worker.md`), which a host with none configured (`config.example.yaml`) renders
# as nothing, so this heading is the mechanism's own next fixed thing after the ownership rule and
# `never_run` (#512).
ENVIRONMENT_HEADING = "THE ENVIRONMENT YOU RUN IN IS CONFIGURED FOR YOU, AND READ-ONLY BY DEFAULT"

# The variable the real config exports, whose absence from a prompt rendered from the sentinel
# mapping is what proves the driver keeps no name of its own.
CONFIGURED_VARIABLE = "DATABASE_URL"


def _config_with_worker_environment(tmp_path, environment):
    """A copy of config.example.yaml with `project.worker_environment` replaced by
    `environment` -- nothing at all for an empty mapping -- the same `AGENTS_CONFIG_PATH` isolation
    the two helpers above use, so a test can render the worker's RULES from a mapping this
    repository does not ship."""
    text = EXAMPLE_CONFIG.read_text()
    block = (
        "  worker_environment: {}\n"
        if not environment
        else "  worker_environment:\n"
        + "".join(f"    {key}: {value}\n" for key, value in environment.items())
    )
    patched, count = re.subn(
        r"^  worker_environment:\n(?:    .*\n)+",
        lambda _match: block,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "config.example.yaml's project.worker_environment block changed shape"
    path = tmp_path / "agents.yaml"
    path.write_text(patched)
    return path


def _value_fragments(value):
    """The fragments of one configured value that must never reach a prompt: the value whole, every
    `:port` in it, and its credentials. Derived from the value rather than spelled out here, so the
    assertion follows config/agents.yaml when the port or the role changes."""
    fragments = [value, *(port.lstrip(":") for port in re.findall(r":\d+", value))]
    if "@" in value:
        fragments.append(value.split("@", 1)[0].split("//", 1)[-1])
    return fragments


def test_the_environment_paragraph_names_the_configured_variables_and_never_their_values(tmp_path):
    """The paragraph comes from `project.worker_environment` and from nothing else: a mapping this
    repository does not ship reaches the worker's prompt by NAME, no fragment of any value reaches
    it at all, and the name the real config exports is gone with it. A worker that needs a host or a
    port reads it out of its own environment -- which is what makes spelling one unnecessary rather
    than merely moved somewhere else."""
    config = _config_with_worker_environment(tmp_path, SENTINEL_ENVIRONMENT)
    rules = _rendered_rules(config)

    assert ENVIRONMENT_HEADING in rules
    assert "__WORKER_ENVIRONMENT_RULES__" not in rules
    assert worker_environment_rules(load_project(config)) in rules
    flattened = _flattened(rules)
    for key, value in SENTINEL_ENVIRONMENT.items():
        assert f"`{key}`" in flattened, key
        for fragment in _value_fragments(value):
            assert fragment not in rules, fragment
    assert CONFIGURED_VARIABLE not in rules


def test_the_paragraph_the_real_config_renders_names_its_keys_and_spells_no_port():
    """What the real config/agents.yaml exports is what the worker reads: every key by name, and no
    fragment of any value. This is the assertion that #363 removed the hardcoded port rather than
    re-typing it from the config -- the prompt and the exported variable cannot disagree about a
    number, because only one of them carries it."""
    configured = load_project().worker_environment
    assert configured, "config/agents.yaml exports nothing, so there is nothing to render"
    rules = _rendered_rules()

    assert ENVIRONMENT_HEADING in rules
    assert worker_environment_rules(load_project()) in rules
    flattened = _flattened(rules)
    for key, value in configured.items():
        assert f"`{key}`" in flattened, key
        for fragment in _value_fragments(value):
            assert fragment not in rules, fragment


def test_a_project_that_exports_no_environment_gets_no_paragraph_and_no_stub_heading(tmp_path):
    """An empty `project.worker_environment` renders nothing at all -- no heading, no placeholder,
    and no gap twice as wide as the one the paragraph filled -- and with it goes every claim that
    the worker's own connection is read-only: a worker told about a guard nobody exported would
    trust one that does not exist.

    Split on `NEVER_RUN_CLOSING`, the mechanism's own fixed closing line, rather than on any host's
    extension-point text: `config.example.yaml` names no `prompt_extras`, so nothing renders at
    that placeholder here, and `never_run_rules`' own paragraph is what actually precedes the gap
    this test measures (#512)."""
    config = _config_with_worker_environment(tmp_path, {})
    assert worker_environment_rules(load_project(config)) == ""
    rules = _rendered_rules(config)

    assert ENVIRONMENT_HEADING not in rules
    assert "read-only by default" not in rules
    assert "__WORKER_ENVIRONMENT_RULES__" not in rules
    assert "\n\n\n" not in rules
    # The sections around it stay separated by the one blank line every other boundary has.
    between = rules.split(NEVER_RUN_CLOSING + "\n")[1].split("SPLIT THE WORK")[0]
    assert between == "\n", repr(between)


# ---------------------------------------------------------------------------------------------
# `open-pr`: the worker's end, where the branch becomes a pull request.
# ---------------------------------------------------------------------------------------------

GH_PR_STUB = """#!/usr/bin/env python3
# Stands in for `gh` for the whole open-pr step and the `issues.py move` that follows it. Every
# call is appended to $GH_CALLS, so the test asserts on what the driver actually asked for; the
# `pr list` answer is derived from that same file, which is how "a PR already exists" is
# simulated without a second stub.
import json
import os
import pathlib
import sys

args = sys.argv[1:]
calls = pathlib.Path(os.environ["GH_CALLS"])
previous = calls.read_text() if calls.is_file() else ""
calls.write_text(previous + "\\t".join(args) + "\\n")


def out(text):
    print(text)
    sys.exit(0)


if args[:2] == ["issue", "view"] and "title" in args:
    out(os.environ["GH_STUB_TITLE"])
if args[:2] == ["issue", "view"] and "body" in args:
    out(os.environ["GH_STUB_BODY"])
if args[:2] == ["issue", "view"]:
    out(json.dumps({"labels": [], "state": "OPEN"}))
if args[:2] == ["pr", "list"]:
    out("7" if "pr\\tcreate" in previous else "")
if args[:2] == ["pr", "create"]:
    out("https://github.com/owner/name/pull/7")
if args[:2] == ["pr", "view"]:
    out(os.environ.get("GH_STUB_MERGEABLE", "MERGEABLE"))
if args[:2] == ["label", "list"]:
    out(json.dumps([{"name": "status:ai-completed"}, {"name": "status:blocked-on-human"}]))
if args[:2] == ["project", "item-list"]:
    out(json.dumps({"items": []}))
issue_path = args[1].split("/") if args[:1] == ["api"] and len(args) > 1 else []
if len(issue_path) == 5 and issue_path[0] == "repos" and issue_path[3] == "issues":
    # `issues.py validate` reads the issue over REST since #70, not with `gh issue view`.
    print(json.dumps({"body": os.environ.get("GH_STUB_BODY", ""), "number": int(issue_path[4])}))
    sys.exit(0)
if args[0] == "api":
    out(json.dumps({"number": 348}))
print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(3)
"""


def _git(*arguments, cwd):
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _git_status(worktree):
    return subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def worker_at_its_end(tmp_path):
    """A worktree on a branch with one commit ahead of a real (bare, local) origin, the driver's
    own `.cache` moved somewhere disposable, and a stub `gh` first on `PATH`. Nothing here talks
    to GitHub and nothing writes into the checkout's real `.cache`."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    _git("remote", "add", "origin", str(remote), cwd=worktree)
    _git("push", "-q", "-u", "origin", "main", cwd=worktree)
    _git("checkout", "-q", "-b", "claude/348-pr", cwd=worktree)
    (worktree / "delivered.py").write_text("x = 1\n")
    _git("add", "delivered.py", cwd=worktree)
    _git("commit", "-qm", "the work", cwd=worktree)

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "worker_claude.state").write_text("STARTED\n")
    (cache / "worker_claude.issue").write_text("348\n")
    started_from = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (cache / "worker_claude.startref").write_text(started_from)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "gh"
    stub.write_text(GH_PR_STUB)
    stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        AGENT_OS_GH_REPO="owner/name",
        GH_CALLS=str(tmp_path / "gh_calls.tsv"),
        GH_STUB_TITLE="The worker opens its own pull request",
        GH_STUB_BODY="## Objective\nsomething",
        GH_TOKEN="stub-token-so-no-app-is-minted",
    )
    return environment, worktree, remote, cache, tmp_path / "gh_calls.tsv"


def _open_pr(environment):
    return subprocess.run(
        ["bash", str(DRIVER), "claude", "open-pr"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_open_pr_pushes_the_branch_and_opens_one_pr_that_closes_the_issue(worker_at_its_end):
    environment, _worktree, remote, _cache, calls = worker_at_its_end
    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr

    pushed = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "claude/348-pr"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pushed.returncode == 0, "the branch was never pushed to origin"

    recorded = calls.read_text()
    created = [line for line in recorded.splitlines() if line.startswith("pr\tcreate")]
    assert len(created) == 1, recorded
    assert "--base\tmain" in created[0]
    assert "The worker opens its own pull request (#348)" in created[0]
    assert "Closes #348" in created[0]
    # ... and the issue is moved on to the state the planner hands the validator.
    assert any("labels[]=status:ai-completed" in line for line in recorded.splitlines()), recorded


def test_open_pr_writes_the_state_marker_after_the_move_succeeds(worker_at_its_end):
    # #350 Part 4: line 2 of .state records the issue and the label this driver just wrote for
    # it, preserving whatever line 1 (STARTED, here) already said -- the guard's tick reads this
    # to detect drift between local state and GitHub.
    environment, _worktree, _remote, cache, _calls = worker_at_its_end
    assert _open_pr(environment).returncode == 0
    lines = (cache / "worker_claude.state").read_text().splitlines()
    assert lines[0] == "STARTED"
    assert lines[1] == "issue=348 label=status:ai-completed"


def test_open_pr_does_not_open_a_second_pr_for_a_branch_that_already_has_one(worker_at_its_end):
    environment, _worktree, _remote, _cache, calls = worker_at_its_end
    assert _open_pr(environment).returncode == 0
    second = _open_pr(environment)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already open" in second.stdout
    created = [line for line in calls.read_text().splitlines() if line.startswith("pr\tcreate")]
    assert len(created) == 1


def test_open_pr_publishes_nothing_for_a_run_the_guard_cut(worker_at_its_end):
    environment, _worktree, _remote, cache, calls = worker_at_its_end
    (cache / "worker_claude.state").write_text("CUT_BY_GUARD reason=stall\n")
    result = _open_pr(environment)
    assert result.returncode == 0
    assert "CUT_BY_GUARD" in result.stdout
    assert not calls.is_file() or "pr\tcreate" not in calls.read_text()


def test_open_pr_publishes_nothing_when_the_branch_is_not_ahead_of_its_base(worker_at_its_end):
    environment, worktree, _remote, _cache, calls = worker_at_its_end
    _git("reset", "-q", "--hard", "origin/main", cwd=worktree)
    result = _open_pr(environment)
    assert result.returncode == 0
    assert "no commits ahead" in result.stdout
    assert not calls.is_file() or "pr\tcreate" not in calls.read_text()


def test_open_pr_honours_a_base_the_issue_names(worker_at_its_end):
    environment, _worktree, _remote, _cache, calls = worker_at_its_end
    environment["GH_STUB_BODY"] = "## Objective\nstacked work\n\nBase: feature/347-parent\n"
    assert _open_pr(environment).returncode == 0
    created = [line for line in calls.read_text().splitlines() if line.startswith("pr\tcreate")]
    assert "--base\tfeature/347-parent" in created[0]


def test_open_pr_refuses_a_branch_whose_diff_carries_the_diary(worker_at_its_end):
    """#407: `scratchpad/progress.log` reached `main` inside a worker's pull request, and every
    worker branch after that conflicted with `main` on it -- and a conflicting pull request gets no
    CI at all. So `open-pr` refuses to push one that carries it, names the file and the commits
    that do, and writes nothing: no push, no pull request, no move of the issue, no state."""
    environment, worktree, remote, cache, calls = worker_at_its_end
    (worktree / "scratchpad").mkdir()
    (worktree / "scratchpad" / "progress.log").write_text(
        "2026-09-17 10:00  EXPECT a-stage normal=10m cutoff=20m\n"
    )
    _git("add", "scratchpad/progress.log", cwd=worktree)
    _git("commit", "-qm", "stage 1/1: the work, and the diary with it", cwd=worktree)

    result = _open_pr(environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scratchpad/progress.log" in result.stdout, result.stdout
    assert "the diary with it" in result.stdout, result.stdout
    pushed = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "claude/348-pr"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pushed.returncode != 0, "the branch was pushed although its diff carries the diary"
    recorded = calls.read_text() if calls.is_file() else ""
    assert "pr\tcreate" not in recorded, recorded
    assert "status:ai-completed" not in recorded, recorded
    assert (cache / "worker_claude.state").read_text() == "STARTED\n"
    # Refused, not repaired: the commit is still on the branch for whoever picks it up.
    assert _subjects(worktree)[0] == "stage 1/1: the work, and the diary with it"


def test_open_pr_pushes_a_branch_that_drops_the_diary_it_inherited(worker_at_its_end):
    """The other side of the same rule (#407): a branch forked while `main` still tracked the diary
    carries it in its tree, and the diff that removes it is the branch getting clean -- the one
    change to that file `open-pr` must let through, or no old branch could ever be published."""
    environment, worktree, _remote, _cache, calls = worker_at_its_end
    # A base that still tracked the diary, pushed to origin because that is what `open-pr` measures
    # the diff against, and then merged into the branch: the branch inherits the file without any
    # commit of its own adding it.
    _git("checkout", "-q", "main", cwd=worktree)
    (worktree / "scratchpad").mkdir()
    (worktree / "scratchpad" / "progress.log").write_text("inherited from the base\n")
    _git("add", "scratchpad/progress.log", cwd=worktree)
    _git("commit", "-qm", "a base that still tracked the diary", cwd=worktree)
    _git("push", "-q", "origin", "main", cwd=worktree)
    _git("checkout", "-q", "claude/348-pr", cwd=worktree)
    _git("merge", "-q", "--no-edit", "main", cwd=worktree)
    # The repair #407 leaves to a hand: take the diary out of the branch that inherited it.
    _git("rm", "-q", "scratchpad/progress.log", cwd=worktree)
    _git("commit", "-qm", "drop the diary the base left behind", cwd=worktree)

    result = _open_pr(environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "adds or modifies" not in result.stdout, result.stdout
    created = [line for line in calls.read_text().splitlines() if line.startswith("pr\tcreate")]
    assert len(created) == 1, calls.read_text()


def test_the_pr_exists_before_the_worker_finished_event_is_written():
    # The planner is woken by that event and its first act is to hand the pull request to the
    # validator, so the order of these two lines in the run's own subshell is the contract.
    # One pair per launch dialect; since #514 the backend is `"$WORKER_BACKEND"`, not a literal.
    driver = DRIVER.read_text()
    pr_steps = [
        match.start()
        for match in re.finditer(
            r'"\$AGENT_OS_DIR/bin/worker_task.sh" "\$WORKER_BACKEND" open-pr', driver
        )
    ]
    exit_hooks = [
        match.start()
        for match in re.finditer(r'-m agent_os.guard check "\$WORKER_BACKEND"', driver)
    ]
    assert len(pr_steps) == len(exit_hooks) == 2
    for pr_step, exit_hook in zip(pr_steps, exit_hooks, strict=True):
        assert pr_step < exit_hook


# ---------------------------------------------------------------------------------------------
# `resume`: the relaunch cap the driver enforces itself, not the planner counting commits (#362,
# agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md). Both
# `claude` and `qwen` fakes sit first on PATH regardless of which backend the test drives, on
# purpose: a real backend must never be reachable from these tests even by accident.
# ---------------------------------------------------------------------------------------------

NEVER_EXITS_STUB = "#!/usr/bin/env bash\nexec sleep 600\n"


def _worktree_with_branch_subjects(tmp_path, subjects, *, issue="348", config_path=None):
    """A real (bare, local) origin with a `main` branch, and the worker's own branch forked from
    it carrying `subjects` as its own commits -- exactly the range the driver's cap check counts
    between `merge-base main HEAD` and `HEAD`. `config_path` is the `AGENTS_CONFIG_PATH` isolation
    `_config_with_max_parallel_issues` uses, for a test that needs a `planner.relaunch_cap` other
    than the shipped one."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    _git("remote", "add", "origin", str(remote), cwd=worktree)
    _git("push", "-q", "-u", "origin", "main", cwd=worktree)
    _git("checkout", "-q", "-b", f"claude/{issue}-relaunch", cwd=worktree)
    for number, subject in enumerate(subjects):
        (worktree / f"wip-{number}.txt").write_text("x\n")
        _git("add", f"wip-{number}.txt", cwd=worktree)
        _git("commit", "-qm", subject, cwd=worktree)

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "worker_claude.issue").write_text(f"{issue}\n")
    (cache / "worker_claude.session").write_text("fake-session-id\n")
    (cache / "worker_claude.brief.md").write_text("brief\n")
    (cache / "worker_claude.state").write_text("CUT_BY_GUARD reason=stall\n")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, contents in (
        ("gh", GH_STUB),
        ("claude", NEVER_EXITS_STUB),
        ("qwen", NEVER_EXITS_STUB),
    ):
        stub = binaries / name
        stub.write_text(contents)
        stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        AGENT_OS_GH_REPO="owner/name",
        GH_STUB_BODY="## Objective\nsomething\n",
        GH_TOKEN="stub-token-so-no-app-is-minted",
    )
    if config_path is not None:
        environment["AGENTS_CONFIG_PATH"] = str(config_path)
    return environment, cache


def _worktree_with_cut_commits(tmp_path, cut_commit_count, *, issue="348"):
    """`cut_commit_count` real guard cuts, one per attempt the guard made -- every one of them a
    subject `worker_task.sh`'s `subject_is_a_guard_cut` counts."""
    return _worktree_with_branch_subjects(
        tmp_path,
        tuple(f"WIP: cut by guard (stall attempt {n})" for n in range(cut_commit_count)),
        issue=issue,
    )


def _resume(environment, backend="claude"):
    return subprocess.run(
        ["bash", str(DRIVER), backend, "resume"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resume_proceeds_below_the_relaunch_cap(tmp_path):
    environment, _cache = _worktree_with_cut_commits(tmp_path, 1)
    try:
        result = _resume(environment)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "resume refused" not in result.stdout
        # Reached the fake backend -- proof the driver did not stop at the cap check.
        assert "started pid" in result.stdout
    finally:
        # The stub blocks forever on purpose (NEVER_EXITS_STUB) -- stop it by PID, through the
        # driver's own `stop`, so no process is left sleeping past this test.
        _stop(environment)


def test_resume_refuses_at_the_relaunch_cap_and_writes_nothing(tmp_path):
    # The cap is a configured number, not a literal: reading it here is what keeps this test
    # honest when config/agents.yaml changes it (it went 2 -> 3 on 2026-09-16).
    cap = load_planner_config().relaunch_cap
    environment, cache = _worktree_with_cut_commits(tmp_path, cap)
    result = _resume(environment)
    assert result.returncode != 0
    assert "resume refused" in result.stdout
    assert "#348" in result.stdout
    assert str(cap) in result.stdout
    assert not (cache / "worker_claude.pid").exists()
    # Refusing writes nothing: the pre-existing state from the prior cut is untouched.
    assert (cache / "worker_claude.state").read_text() == "CUT_BY_GUARD reason=stall\n"
    assert not (cache / "worker_claude.jsonl").exists()


def test_a_pre_merge_freeze_is_not_one_of_the_cuts_the_relaunch_cap_counts(tmp_path):
    """#407: `open-pr` freezes the tree before it merges the base in, and that freeze carried the
    subject of a guard cut -- so a branch whose run was never cut had spent one of
    `planner.relaunch_cap`'s attempts (#369's 81bfae4). One pre-merge freeze and one real cut count
    as ONE cut. The refusal prints the count it measured, and the cap is patched to 1 so that a
    branch carrying a single real cut still reaches the refusal: the number in that message is the
    measurement, and it read 2 while the freeze was counted."""
    environment, cache = _worktree_with_branch_subjects(
        tmp_path,
        ("WIP: cut by guard (before_merge)", "WIP: cut by guard (stall attempt 0)"),
        config_path=_config_with_relaunch_cap(tmp_path, 1),
    )
    result = _resume(environment)
    assert result.returncode != 0
    assert "resume refused" in result.stdout
    assert "has 1 commits cut by guard" in result.stdout, result.stdout
    # Refusing writes nothing, whatever the count that decided it.
    assert not (cache / "worker_claude.pid").exists()
    assert not (cache / "worker_claude.jsonl").exists()
    assert (cache / "worker_claude.state").read_text() == "CUT_BY_GUARD reason=stall\n"


def test_a_branch_whose_only_freeze_is_the_pre_merge_one_still_resumes(tmp_path):
    """The same distinction from the other side: a run that reached `open-pr` finished every stage,
    so its freeze alone may not spend the one attempt a `planner.relaunch_cap` of 1 allows --
    `resume` goes on to launch, exactly as it does for a branch with no freeze on it (#407)."""
    environment, cache = _worktree_with_branch_subjects(
        tmp_path,
        ("WIP: cut by guard (before_merge)",),
        config_path=_config_with_relaunch_cap(tmp_path, 1),
    )
    try:
        # Held so that nothing this run ends up firing can wake a real planner: `wake`'s one door
        # is a non-blocking flock on this file.
        with _planner_lock_held(cache):
            result = _resume(environment)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "resume refused" not in result.stdout
            assert "started pid" in result.stdout
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# `start`: the parallelism cap and the module exclusion, both enforced by the driver itself,
# before it writes anything (#374, agent_os/docs/adr/2026-09-15-parallelism-is-a-configured-cap-enforced-
# by-the-driver.md). `claude` and `qwen` fakes sit first on PATH regardless of which backend the
# test drives, same as the relaunch-cap tests above -- a real backend must never be reachable
# even by accident.
# ---------------------------------------------------------------------------------------------

GH_STUB_FULL = """#!/usr/bin/env python3
# Full enough to drive `start` all the way to launching the fake backend: answers every call the
# driver plus `issues.py validate`/`brief`/`move` make along that path. $GH_STUB_BODY is the body
# of the issue being started (the only one ever queried for its body); $GH_STUB_LABELS_JSON maps
# issue number (as a string) to a list of label names, so the issue starting and another backend's
# already-running issue can carry different `module:` labels. Any call this does not recognize is
# a test failure, not a silent success -- the driver must never reach the network.
import json
import os
import sys

args = sys.argv[1:]
labels_by_issue = json.loads(os.environ.get("GH_STUB_LABELS_JSON", "{}"))
body = os.environ["GH_STUB_BODY"]


def out(text):
    print(text)
    sys.exit(0)


if args[:2] == ["issue", "view"]:
    number = args[2]
    json_arg = args[args.index("--json") + 1] if "--json" in args else ""
    if json_arg == "labels" and "-q" in args:
        for name in labels_by_issue.get(number, []):
            print(name)
        sys.exit(0)
    if json_arg == "labels,state":
        out(json.dumps({"labels": [], "state": "OPEN"}))
    # "body" (with or without -q), or "number,title,body,url,parent" -- validate_issue and
    # fetch_brief_sources both only ever read .body (plus .number/.title/.url, which default fine
    # when absent).
    if "-q" in args:
        out(body)
    out(json.dumps({"body": body, "number": int(number)}))

if args[:2] == ["label", "list"]:
    # The `doing` label already exists, so `move`'s `ensure_labels` creates nothing.
    out(json.dumps([{"name": "status:doing"}]))

issue_path = args[1].split("/") if args[:1] == ["api"] and len(args) > 1 else []
if len(issue_path) == 5 and issue_path[0] == "repos" and issue_path[3] == "issues":
    # `issues.py validate` reads the issue over REST since #70, not with `gh issue view`.
    print(json.dumps({"body": os.environ.get("GH_STUB_BODY", ""), "number": int(issue_path[4])}))
    sys.exit(0)
if args[:1] == ["api"]:
    out(json.dumps({}))

if args[:2] == ["project", "item-list"]:
    # No board item for this issue: `mirror_board_column` stops right there, needing no further
    # board-field or `item-edit` stub.
    out(json.dumps({"items": []}))

print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(3)
"""

NEVER_EXITS_BACKEND_STUB = "#!/usr/bin/env bash\nexec sleep 600\n"


def _config_with_max_parallel_issues(tmp_path, cap):
    """A copy of config.example.yaml with `planner.max_parallel_issues` patched to `cap`
    -- `AGENTS_CONFIG_PATH` (`agent_os.lib`) exists so a test can exercise a cap other
    than the real file's shipped default (1), the same way `WORKER_CACHE_DIR` isolates `.cache/`.
    Everything else (worktrees, apps, task classes) stays the real file's own."""
    text = EXAMPLE_CONFIG.read_text()
    patched, count = re.subn(r"max_parallel_issues:\s*\d+", f"max_parallel_issues: {cap}", text)
    assert count == 1, "config.example.yaml's planner.max_parallel_issues line changed shape"
    path = tmp_path / "agents.yaml"
    path.write_text(patched)
    return path


def _config_with_relaunch_cap(tmp_path, cap):
    """A copy of config.example.yaml with `planner.relaunch_cap` patched to `cap`. The
    shipped one is 3, and the count a refusal reports is only ever printed when it reaches the cap,
    so a test that reads the number off the message needs a cap it can reach with the commits it
    builds. Same `AGENTS_CONFIG_PATH` isolation `_config_with_max_parallel_issues` uses."""
    text = EXAMPLE_CONFIG.read_text()
    patched, count = re.subn(r"relaunch_cap:\s*\d+", f"relaunch_cap: {cap}", text)
    assert count == 1, "config.example.yaml's planner.relaunch_cap line changed shape"
    path = tmp_path / "agents-relaunch-cap.yaml"
    path.write_text(patched)
    return path


def _config_with_max_total_tokens(tmp_path, cap):
    """A copy of config.example.yaml with `complex-qwen`'s `max_total_tokens` patched to
    `cap` -- `complex-qwen` being the class `_staged_body` dispatches under. The shipped token
    ceilings are placeholders #342 owns the calibration of, so a test that asserted against one of
    them would break on that calibration instead of on the behaviour it is about; the same
    `AGENTS_CONFIG_PATH` isolation `_config_with_max_parallel_issues` uses keeps it off the real
    file. `max_cost_usd` is deliberately left at its shipped value: the point of the token gate is
    that the dollar one is untouched while it fires."""
    text = EXAMPLE_CONFIG.read_text()
    patched, count = re.subn(
        r"(^  complex-qwen:\n(?:    .*\n)*?    max_total_tokens: )\d+",
        rf"\g<1>{cap}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "config.example.yaml's complex-qwen block changed shape"
    path = tmp_path / "agents.yaml"
    path.write_text(patched)
    return path


def _spawn_alive_other_backend(cache, backend, issue):
    """A real, long-lived process standing in for `backend`'s worker: `alive_pidfile` (the driver)
    reads its pidfile with `kill -0`, exactly like a real run's. Terminated directly by PID in the
    caller's `finally` -- never through `worker_task.sh stop`'s process-group kill, which assumes
    a `setsid` child this plain one is not."""
    process = subprocess.Popen(["sleep", "600"])
    (cache / f"worker_{backend}.pid").write_text(f"{process.pid}\n")
    (cache / f"worker_{backend}.issue").write_text(f"{issue}\n")
    return process


def _parallel_cap_environment(tmp_path, *, config_path=None, labels_by_issue):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    cache = tmp_path / "cache"
    cache.mkdir()

    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, contents in (
        ("gh", GH_STUB_FULL),
        ("claude", NEVER_EXITS_BACKEND_STUB),
        ("qwen", NEVER_EXITS_BACKEND_STUB),
    ):
        stub = binaries / name
        stub.write_text(contents)
        stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        AGENT_OS_GH_REPO="owner/name",
        GH_STUB_BODY=VALID_BODY,
        GH_STUB_LABELS_JSON=json.dumps(labels_by_issue),
        GH_TOKEN="stub-token-so-no-app-is-minted",
    )
    if config_path is not None:
        environment["AGENTS_CONFIG_PATH"] = str(config_path)
    return environment, cache


def test_start_refuses_at_cap_one_with_another_backend_alive(tmp_path):
    # The real file's shipped default (1): no AGENTS_CONFIG_PATH override needed.
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"], "500": ["module:prices"]}
    )
    other = _spawn_alive_other_backend(cache, "qwen", 500)
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "worker(s) already running" in result.stdout
        assert "max_parallel_issues=1" in result.stdout
        assert not (cache / "worker_claude.pid").exists()
        assert not (cache / "worker_claude.issue").exists()
    finally:
        other.terminate()
        other.wait(timeout=5)


def test_start_proceeds_at_cap_two_with_another_backend_alive_on_a_different_module(tmp_path):
    config_path = _config_with_max_parallel_issues(tmp_path, 2)
    environment, cache = _parallel_cap_environment(
        tmp_path,
        config_path=config_path,
        labels_by_issue={"347": ["module:workers"], "500": ["module:prices"]},
    )
    other = _spawn_alive_other_backend(cache, "qwen", 500)
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worker(s) already running" not in result.stdout
        assert "shares module label" not in result.stdout
        # Reached the fake backend -- proof the driver did not stop at either check.
        assert "started pid" in result.stdout
    finally:
        _stop(environment)
        other.terminate()
        other.wait(timeout=5)


def test_start_refuses_at_cap_two_when_the_other_backend_shares_a_module(tmp_path):
    config_path = _config_with_max_parallel_issues(tmp_path, 2)
    environment, cache = _parallel_cap_environment(
        tmp_path,
        config_path=config_path,
        labels_by_issue={"347": ["module:workers"], "500": ["module:workers"]},
    )
    other = _spawn_alive_other_backend(cache, "qwen", 500)
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "shares module label" in result.stdout
        assert "#500" in result.stdout
        assert not (cache / "worker_claude.pid").exists()
        assert not (cache / "worker_claude.issue").exists()
    finally:
        other.terminate()
        other.wait(timeout=5)


# ---------------------------------------------------------------------------------------------
# SCRATCH IS NOT DIRT, A TRACKED DIARY STILL IS (#407, #86). No commit carries the diary. #407 read
# its uncommitted lines as "a run is not over" and refused `start`, `resume` and `branch` over
# them; every run state then needed its own exemption (#18, #22, #75, #84), and the one not yet
# covered was the next deadlock only a human could clear. Since #86 the driver hides `scratchpad/`
# from git in the worktree, so an UNTRACKED diary no longer refuses anything -- liveness is `alive`
# and `.state`'s to say. The TRACKED-and-modified shape, what a branch forked before `main` stopped
# tracking the file (5a827d9) carries, is still work git reports, and still refuses.
# ---------------------------------------------------------------------------------------------

DIARY_HEARTBEAT = "2026-09-17 10:00  HEARTBEAT still-working normal=10m cutoff=20m\n"


def _diary_with_an_uncommitted_line(worktree, *, tracked):
    """The worktree's diary holding one line no commit carries. `tracked` reproduces a branch
    forked while `main` still tracked the file: the diary is committed once and then appended to,
    so the entry `git status` reports is ` M` rather than `??`."""
    diary = worktree / "scratchpad" / "progress.log"
    diary.parent.mkdir(exist_ok=True)
    if tracked:
        diary.write_text("2026-09-17 09:00  EXPECT a-base-that-tracked-it normal=1m cutoff=2m\n")
        _git("add", "scratchpad/progress.log", cwd=worktree)
        _git("commit", "-qm", "a base that still tracked the diary", cwd=worktree)
    with diary.open("a") as handle:
        handle.write(DIARY_HEARTBEAT)
    return diary


def test_start_is_not_refused_over_an_untracked_diary(tmp_path):
    # No `.state` at all: the shape #407 refused. Hidden since #86, the diary is not dirt.
    environment, _cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    diary = _diary_with_an_uncommitted_line(worktree, tracked=False)
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worktree is dirty" not in result.stdout, result.stdout
        assert "started pid" in result.stdout, result.stdout
        # Hidden, not moved: a run the driver never saw end has nothing to archive.
        assert "still-working" in diary.read_text()
        assert (worktree / "scratchpad" / ".gitignore").read_text() == "*\n"
        assert "scratchpad" not in _git_status(worktree)
    finally:
        _stop(environment)


def test_start_leaves_a_scratchpad_gitignore_the_host_tracks_alone(tmp_path):
    environment, _cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    own = worktree / "scratchpad" / ".gitignore"
    own.parent.mkdir(exist_ok=True)
    own.write_text("*.tmp\n")
    _git("add", "scratchpad/.gitignore", cwd=worktree)
    _git("commit", "-qm", "the host's own scratchpad ignore rules", cwd=worktree)
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert own.read_text() == "*.tmp\n"
    finally:
        _stop(environment)


def test_start_refuses_a_worktree_whose_tracked_diary_holds_uncommitted_lines(tmp_path):
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    diary = _diary_with_an_uncommitted_line(tmp_path / "worktree", tracked=True)
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "worktree is dirty" in result.stdout, result.stdout
        # Tracked, so git reports the file itself rather than a collapsed directory.
        assert "scratchpad/progress.log" in result.stdout, result.stdout
        assert not (cache / "worker_claude.pid").exists()
        assert not (cache / "worker_claude.issue").exists()
        assert "still-working" in diary.read_text()
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# AFTER A CUT, THE DIARY IS THE RESUMED RUN'S OWN HISTORY (#22). The signal above is #407's, and it
# does not hold for `resume` over a run the guard cut: nothing is alive (checked first), the freeze
# has committed everything else, and the lines are the ones the run being resumed wrote. So
# `resume` over `CUT_BY_GUARD` starts on a worktree whose only dirt is the diary -- in both shapes
# -- and leaves the file where it is, untouched. Anything else dirty, or a `.state` that is not a
# cut, still refuses.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tracked", [False, True], ids=["untracked", "tracked"])
@pytest.mark.parametrize(
    "state_line",
    # `DONE` too (#84): a run that opened its PR and exited is the one the planner resumes with
    # the validator's request-changes review (`prompts/planner.md`, CHANGES REQUESTED).
    ["CUT_BY_GUARD reason=stall", "DONE"],
    ids=["cut", "done"],
)
def test_resume_after_a_cut_starts_over_the_diary_of_the_run_it_continues(
    tmp_path, tracked, state_line
):
    # One cut commit, so the relaunch cap is not what could refuse this.
    environment, cache = _worktree_with_cut_commits(tmp_path, 1)
    (cache / "worker_claude.state").write_text(f"{state_line}\n")
    diary = _diary_with_an_uncommitted_line(tmp_path / "worktree", tracked=tracked)
    before = diary.read_text()
    try:
        result = _resume(environment)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worktree is dirty" not in result.stdout, result.stdout
        assert "started pid" in result.stdout, result.stdout
        # Left on disk exactly as the cut run wrote it, where the monitor reads it and the resumed
        # run appends to it: not archived, not staged, not rewritten.
        assert diary.read_text() == before
        assert _archived_diaries(cache) == []
    finally:
        _stop(environment)


@pytest.mark.parametrize("state_line", ["CUT_BY_GUARD reason=stall", "DONE"], ids=["cut", "done"])
def test_resume_starts_over_other_scratch_beside_the_diary(tmp_path, state_line):
    # #22 and #84 refused this: a draft beside the diary in the same untracked `scratchpad/`.
    # Hidden since #86, it is the resumed run's own scratch, left where that run can read it.
    environment, cache = _worktree_with_cut_commits(tmp_path, 1)
    (cache / "worker_claude.state").write_text(f"{state_line}\n")
    worktree = tmp_path / "worktree"
    diary = _diary_with_an_uncommitted_line(worktree, tracked=False)
    (worktree / "scratchpad" / "notes.md").write_text("a draft the worker never committed\n")
    try:
        result = _resume(environment)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "started pid" in result.stdout, result.stdout
        assert "still-working" in diary.read_text()
        assert (worktree / "scratchpad" / "notes.md").is_file()
        assert _archived_diaries(cache) == []
    finally:
        _stop(environment)


def test_resume_after_a_cut_still_refuses_a_modified_file_beside_the_tracked_diary(tmp_path):
    environment, cache = _worktree_with_cut_commits(tmp_path, 1)
    worktree = tmp_path / "worktree"
    diary = _diary_with_an_uncommitted_line(worktree, tracked=True)
    (worktree / "README.md").write_text("an edit nothing froze\n")
    try:
        result = _resume(environment)
        assert result.returncode != 0
        assert "worktree is dirty" in result.stdout, result.stdout
        assert "README.md" in result.stdout, result.stdout
        # Only the other work is named: the diary is no longer what the refusal is about.
        assert "progress.log" not in result.stdout, result.stdout
        assert not (cache / "worker_claude.pid").exists()
        assert "still-working" in diary.read_text()
    finally:
        _stop(environment)


@pytest.mark.parametrize(
    "state_line",
    [
        "STARTED",
        "RESUMED after=guard_cut",
        "FAILED_LAUNCH command=claude status=127",
        "BLOCKED reason=merge_failed base=main",
    ],
)
def test_resume_still_refuses_a_tracked_diary_when_the_state_is_not_a_cut(tmp_path, state_line):
    # `resume` continues a run the guard cut or one that finished (#84). Over a run the driver
    # never saw end, one that never launched, or one that blocked at `open-pr`, a diary git TRACKS
    # is an edit git reports, and still refuses; #86 hides only the untracked shape.
    environment, cache = _worktree_with_cut_commits(tmp_path, 1)
    (cache / "worker_claude.state").write_text(f"{state_line}\n")
    diary = _diary_with_an_uncommitted_line(tmp_path / "worktree", tracked=True)
    try:
        result = _resume(environment)
        assert result.returncode != 0
        assert "worktree is dirty" in result.stdout, result.stdout
        assert "scratchpad/progress.log" in result.stdout, result.stdout
        assert not (cache / "worker_claude.pid").exists()
        assert (cache / "worker_claude.state").read_text() == f"{state_line}\n"
        assert "still-working" in diary.read_text()
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# A FINISHED RUN'S DIARY IS NOT THE NEXT RUN'S DIRT (#18). The signal above holds for a run the
# driver never saw end; once `.state` line 1 records an ending, the untracked diary is the previous
# run's leftover, and `start`/`branch` archive it into `$cache/diaries/` instead of refusing the
# next dispatch over it. Observed on a host with no ignore rule for the file: the first dispatch to
# a backend after every completed issue was refused.
# ---------------------------------------------------------------------------------------------


def _finished_previous_run(cache, state_line, previous_issue="37"):
    (cache / "worker_claude.state").write_text(f"{state_line}\nissue={previous_issue} label=done\n")
    (cache / "worker_claude.issue").write_text(f"{previous_issue}\n")


def _archived_diaries(cache):
    return (
        sorted((cache / "diaries").glob("*.progress.log")) if (cache / "diaries").is_dir() else []
    )


@pytest.mark.parametrize("ending", ["DONE", "CUT_BY_GUARD reason=stall", "BLOCKED reason=ci"])
def test_start_archives_a_finished_runs_diary_and_dispatches(tmp_path, ending):
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    _diary_with_an_uncommitted_line(tmp_path / "worktree", tracked=False)
    _finished_previous_run(cache, ending)
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worktree is dirty" not in result.stdout, result.stdout
        assert "started pid" in result.stdout, result.stdout
        # Archived, not deleted, and named after the run that wrote it.
        [archived] = _archived_diaries(cache)
        assert archived.name.startswith("worker_claude-issue37-"), archived.name
        assert "still-working" in archived.read_text()
    finally:
        _stop(environment)


def test_start_neither_refuses_nor_archives_a_diary_whose_run_the_driver_never_saw_end(tmp_path):
    # #18 refused this; since #86 the diary is hidden, and with no ending recorded it is not
    # archived either -- it stays exactly where it is.
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    diary = _diary_with_an_uncommitted_line(tmp_path / "worktree", tracked=False)
    _finished_previous_run(cache, "STARTED")
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worktree is dirty" not in result.stdout, result.stdout
        assert "still-working" in diary.read_text()
        assert _archived_diaries(cache) == []
    finally:
        _stop(environment)


def test_start_leaves_a_finished_runs_diary_alone_when_other_work_is_also_left(tmp_path):
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    diary = _diary_with_an_uncommitted_line(worktree, tracked=False)
    (worktree / "scratchpad" / "notes.md").write_text("scratch the run left beside its diary\n")
    # Outside `scratchpad/`: work, not the run's scratch, whatever `.state` says.
    (worktree / "draft.py").write_text("a module the worker never committed\n")
    _finished_previous_run(cache, "DONE")
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "worktree is dirty" in result.stdout, result.stdout
        assert "draft.py" in result.stdout, result.stdout
        # A refusal writes nothing: neither the diary nor the scratch beside it is moved.
        assert "still-working" in diary.read_text()
        assert (worktree / "scratchpad" / "notes.md").is_file()
        assert _archived_diaries(cache) == []
        assert _archived_scratch(cache) == []
    finally:
        _stop(environment)


def test_start_still_refuses_a_modified_tracked_file_under_scratchpad(tmp_path):
    # Only UNTRACKED scratch is the run's leftover: a change to a file git tracks under
    # `scratchpad/` -- a committed deliverable -- is an edit of work, and still refuses.
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    deliverable = worktree / "scratchpad" / "report.md"
    deliverable.parent.mkdir(exist_ok=True)
    deliverable.write_text("the committed report\n")
    _git("add", "scratchpad/report.md", cwd=worktree)
    _git("commit", "-qm", "a deliverable under scratchpad/", cwd=worktree)
    deliverable.write_text("an edit nothing committed\n")
    (worktree / "scratchpad" / "notes.md").write_text("scratch\n")
    _finished_previous_run(cache, "DONE")
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "worktree is dirty" in result.stdout, result.stdout
        assert "scratchpad/report.md" in result.stdout, result.stdout
        assert (worktree / "scratchpad" / "notes.md").is_file()
        assert _archived_scratch(cache) == []
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# A FINISHED RUN'S SCRATCH IS NOT THE NEXT RUN'S DIRT EITHER (#75). The diary is not the only file
# a run leaves in `scratchpad/`: the worker's RULES send intermediate results there, and a run that
# ended BLOCKED on the host #75 came from left a script, a commit message draft and a
# `__pycache__/` there -- no diary at all. `git status` reported the one collapsed `?? scratchpad/`,
# #18's "the diary is the only dirty path" did not match, and two unrelated dispatches were refused
# until a human moved the directory out by hand. Once `.state` records the run's end and nothing is
# alive, every UNTRACKED file under `scratchpad/` is that run's leftover and is archived with the
# diary; anything dirty elsewhere, or tracked, still refuses and moves nothing.
# ---------------------------------------------------------------------------------------------


def _archived_scratch(cache):
    diaries = cache / "diaries"
    if not diaries.is_dir():
        return []
    return sorted(
        path.relative_to(archive).as_posix()
        for archive in diaries.glob("*.scratchpad")
        for path in archive.rglob("*")
        if path.is_file()
    )


def _stray_scratch(worktree):
    """The shape #75 observed: a finished run's `scratchpad/` holding no diary, only scratch."""
    scratchpad = worktree / "scratchpad"
    (scratchpad / "__pycache__").mkdir(parents=True, exist_ok=True)
    (scratchpad / "check_symbols.py").write_text("print('an ad hoc check')\n")
    (scratchpad / "commit-msg-stage6.txt").write_text("a commit message draft\n")
    (scratchpad / "__pycache__" / "check_symbols.cpython-312.pyc").write_bytes(b"\x00bytecode")


@pytest.mark.parametrize("ending", ["DONE", "CUT_BY_GUARD reason=stall", "BLOCKED reason=push"])
def test_start_archives_a_finished_runs_stray_scratch_without_a_diary(tmp_path, ending):
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    _stray_scratch(worktree)
    _finished_previous_run(cache, ending)
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worktree is dirty" not in result.stdout, result.stdout
        assert "started pid" in result.stdout, result.stdout
        # Archived, not deleted, under the name of the run that wrote them, paths kept.
        [archive] = sorted((cache / "diaries").glob("*.scratchpad"))
        assert archive.name.startswith("worker_claude-issue37-"), archive.name
        assert _archived_scratch(cache) == [
            "__pycache__/check_symbols.cpython-312.pyc",
            "check_symbols.py",
            "commit-msg-stage6.txt",
        ]
        assert not (worktree / "scratchpad" / "check_symbols.py").exists()
        # The ignore file is the driver's own, not the run's scratch (#86): it stays, and keeps
        # hiding the next run's.
        assert (worktree / "scratchpad" / ".gitignore").read_text() == "*\n"
        assert "scratchpad" not in _git_status(worktree)
    finally:
        _stop(environment)


def test_start_archives_a_finished_runs_diary_and_the_scratch_beside_it(tmp_path):
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    _diary_with_an_uncommitted_line(worktree, tracked=False)
    (worktree / "scratchpad" / "notes.md").write_text("scratch the run left beside its diary\n")
    _finished_previous_run(cache, "DONE")
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "started pid" in result.stdout, result.stdout
        # The diary keeps #18's archive name; the rest lands beside it, under the same run.
        [diary] = _archived_diaries(cache)
        assert "still-working" in diary.read_text()
        assert _archived_scratch(cache) == ["notes.md"]
        [archive] = sorted((cache / "diaries").glob("*.scratchpad"))
        assert archive.name.removesuffix(".scratchpad") == diary.name.removesuffix(".progress.log")
    finally:
        _stop(environment)


def test_start_neither_refuses_nor_archives_stray_scratch_whose_run_never_ended(tmp_path):
    environment, cache = _parallel_cap_environment(
        tmp_path, labels_by_issue={"347": ["module:workers"]}
    )
    worktree = tmp_path / "worktree"
    _stray_scratch(worktree)
    _finished_previous_run(cache, "STARTED")
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "worktree is dirty" not in result.stdout, result.stdout
        assert (worktree / "scratchpad" / "check_symbols.py").is_file()
        assert _archived_scratch(cache) == []
    finally:
        _stop(environment)


def test_branch_archives_a_finished_runs_stray_scratch_before_switching(tmp_path):
    _remote, worktree = _worktree_with_origin(tmp_path)
    _stray_scratch(worktree)
    environment = _branch_environment(tmp_path, worktree)
    cache = tmp_path / "cache"
    _finished_previous_run(cache, "BLOCKED reason=push_rejected")

    result = _branch(environment, "task/81-next-issue")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is now on task/81-next-issue" in result.stdout, result.stdout
    assert "check_symbols.py" in _archived_scratch(cache)


def test_branch_switches_over_scratch_whose_run_never_ended(tmp_path):
    # The same deadlock on `branch` (#86): scratch with no recorded ending refused the switch.
    _remote, worktree = _worktree_with_origin(tmp_path)
    _stray_scratch(worktree)
    environment = _branch_environment(tmp_path, worktree)
    cache = tmp_path / "cache"
    _finished_previous_run(cache, "STARTED")

    result = _branch(environment, "task/81-next-issue")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is now on task/81-next-issue" in result.stdout, result.stdout
    assert (worktree / "scratchpad" / "check_symbols.py").is_file()
    assert _archived_scratch(cache) == []


def test_branch_archives_a_finished_runs_diary_before_switching(tmp_path):
    _remote, worktree = _worktree_with_origin(tmp_path)
    _diary_with_an_uncommitted_line(worktree, tracked=False)
    environment = _branch_environment(tmp_path, worktree)
    cache = tmp_path / "cache"
    _finished_previous_run(cache, "DONE")

    result = _branch(environment, "task/81-next-issue")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is now on task/81-next-issue" in result.stdout, result.stdout
    [archived] = _archived_diaries(cache)
    assert "still-working" in archived.read_text()


# ---------------------------------------------------------------------------------------------
# STAGES (#375): one stage per process, chained by the driver. The fake backend below is the only
# thing standing in for a model -- `claude` and `qwen` are both stubbed on PATH regardless of
# which backend a test drives, so a real one can never be reached even by accident.
# ---------------------------------------------------------------------------------------------

FAKE_BACKEND = '''#!/usr/bin/env python3
"""Stands in for `claude` and `qwen`. $FAKE_BACKEND_MODE decides what it does with the prompt the
driver handed it (always the last argument):

    stage_commit          -- make exactly the `stage N/M: <title>` commit that prompt asks for
    clean_exit_no_commit  -- change a tracked file, append to the diary, commit nothing, exit 0
    hang                  -- wait for SIGTERM, so a test can read the prompt and stop it by PID

Every mode appends the prompt to $FAKE_BACKEND_PROMPTS and prints one `result` event, which the
driver redirects into the run's own .jsonl -- the file the spend archive and the budget read.
$FAKE_BACKEND_STAGE_DIARY, read by `clean_exit_no_commit` alone, also stages the diary before
exiting: the worker that committed the one path it must never commit (#407).
$FAKE_BACKEND_UNTRACKED, read by `clean_exit_no_commit` alone, is a comma-separated list of paths
it writes and never adds: the stage #416 lost, and the one #417 is about.
"""
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

prompt = sys.argv[-1]
with pathlib.Path(os.environ["FAKE_BACKEND_PROMPTS"]).open("a") as handle:
    handle.write(prompt + "\\n=== end of prompt ===\\n")

mode = os.environ.get("FAKE_BACKEND_MODE", "hang")
if mode == "stage_commit":
    goal = re.search(r"^Your only goal in this process: (stage \\d+/\\d+: .+)$", prompt, re.M)
    if not goal:
        print("fake backend: the prompt names no stage goal", file=sys.stderr)
        sys.exit(9)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", goal.group(1)], check=True)
elif mode == "clean_exit_no_commit":
    pathlib.Path("README.md").write_text("touched by the worker, never committed\\n")
    for relative in filter(None, os.environ.get("FAKE_BACKEND_UNTRACKED", "").split(",")):
        # A stage cut halfway through writes files it never gets to `git add`: what #416 lost.
        new_file = pathlib.Path(relative)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_text(f"{relative}, written by the stage and never added\\n")
    # What a real worker leaves behind as well: its own diary, appended to and never committed
    # (#407). Untracked here unless the test tracks it first, which is the branch-forked-early case.
    diary = pathlib.Path("scratchpad/progress.log")
    diary.parent.mkdir(exist_ok=True)
    with diary.open("a") as diary_handle:
        diary_handle.write("2026-09-17 10:00  HEARTBEAT still-working normal=10m cutoff=20m\\n")
    if os.environ.get("FAKE_BACKEND_STAGE_DIARY"):
        # A worker that read "commit as you go" and staged the one path that must never be
        # committed: the freeze has to take it back out of the index, not only skip adding it.
        with diary.open("a") as diary_handle:
            diary_handle.write("2026-09-17 10:05  HEARTBEAT staged-by-hand normal=5m cutoff=9m\\n")
        subprocess.run(["git", "add", str(diary)], check=True)
elif mode == "hang":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(0.2)

print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "fake-session",
    "total_cost_usd": float(os.environ.get("FAKE_BACKEND_COST", "0.5")),
    "usage": {"input_tokens": 10, "output_tokens": 5},
}))
'''

GH_STUB_STAGED = """#!/usr/bin/env python3
# Everything the staged driver asks `gh` for, from `start` through `open-pr`: the issue's body and
# labels, the tracking epic's labels (the chain's agents-paused gate), the label/project calls
# `issues.py move` makes, the comment POST, and the pull request. Every call is appended to
# $GH_CALLS so a test can assert on what the driver actually asked for. Any call this does not
# recognize is a test failure, not a silent success.
import json
import os
import pathlib
import sys

args = sys.argv[1:]
calls = pathlib.Path(os.environ["GH_CALLS"])
previous = calls.read_text() if calls.is_file() else ""
calls.write_text(previous + "\\t".join(args) + "\\n")

labels_by_issue = json.loads(os.environ.get("GH_STUB_LABELS_JSON", "{}"))
body = os.environ["GH_STUB_BODY"]


def out(text):
    print(text)
    sys.exit(0)


if args[:2] == ["issue", "view"]:
    number = args[2]
    json_arg = args[args.index("--json") + 1] if "--json" in args else ""
    if json_arg == "labels":
        names = labels_by_issue.get(number, [])
        if "-q" in args:
            out("\\n".join(names))
        out(json.dumps({"labels": [{"name": name} for name in names]}))
    if json_arg == "labels,state":
        out(json.dumps({"labels": [], "state": "OPEN"}))
    if json_arg == "title":
        out(os.environ.get("GH_STUB_TITLE", "A staged issue"))
    if "-q" in args:
        out(body)
    out(json.dumps({"body": body, "number": int(number)}))

if args[:2] == ["label", "list"]:
    out(json.dumps([
        {"name": "status:doing"},
        {"name": "status:ai-completed"},
        {"name": "status:blocked-on-human"},
    ]))
if args[:2] == ["api", "graphql"] and "projectV2(" in " ".join(args):
    # The board's single-select fields, one bounded query (#27).
    fields = [{"id": "FIELD1", "name": "Status", "options": [
        {"id": "OPT_DOING", "name": "In progress"},
    ]}]
    project = {"id": "PROJECT1", "fields": {"nodes": fields}}
    out(json.dumps({"data": {"repositoryOwner": {"projectV2": project}}}))
if args[:2] == ["api", "graphql"]:
    # The issue's own `projectItems` (#14). $GH_STUB_BOARD_ITEM is the issue number that has an
    # item on board 1 of `owner`; unset, no issue has one and `mirror_board_column` says so
    # instead of editing anything.
    on_board = os.environ.get("GH_STUB_BOARD_ITEM")
    nodes = []
    if on_board and f"number={on_board}" in args:
        nodes = [{"id": "ITEM1", "project": {"number": 1, "owner": {"login": "owner"}}}]
    out(json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}}))
if args[:2] == ["project", "item-edit"]:
    out("")
if args[:2] == ["pr", "list"]:
    out("7" if "pr\\tcreate" in previous else "")
if args[:2] == ["pr", "create"]:
    out("https://github.com/owner/name/pull/7")
if args[:2] == ["pr", "view"]:
    out(os.environ.get("GH_STUB_MERGEABLE", "MERGEABLE"))
issue_path = args[1].split("/") if args[:1] == ["api"] and len(args) > 1 else []
if len(issue_path) == 5 and issue_path[0] == "repos" and issue_path[3] == "issues":
    # `issues.py validate` reads the issue over REST since #70, not with `gh issue view`.
    print(json.dumps({"body": os.environ.get("GH_STUB_BODY", ""), "number": int(issue_path[4])}))
    sys.exit(0)
if args[:1] == ["api"]:
    out(json.dumps({"number": 347}))

print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(3)
"""


def _staged_body(*stage_titles):
    checklist = "\n".join(f"- [ ] {title}" for title in stage_titles)
    return (
        "\n\n".join(f"{heading}\nsomething" for heading in REQUIRED_SECTIONS).replace(
            "## Stages\nsomething", f"## Stages\n{checklist}"
        )
        + "\n\n<!-- budget: complex-qwen -->"
    )


def _staged_environment(
    tmp_path, *, stage_titles, mode, subjects=(), labels_by_issue=None, cost="0.5", config_path=None
):
    """A worktree forked from a real (bare, local) origin carrying `subjects` as commits, the fake
    backend and the `gh` stub first on PATH, and every cache path moved somewhere disposable --
    `WORKER_CACHE_DIR` moves the driver's files and, since #375, the guard's events and planner
    lock too, so the real exit hook can fire here without touching the checkout's own `.cache`
    (which every worktree of it shares by symlink)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    _git("remote", "add", "origin", str(remote), cwd=worktree)
    _git("push", "-q", "-u", "origin", "main", cwd=worktree)
    _git("checkout", "-q", "-b", "claude/347-staged", cwd=worktree)
    for subject in subjects:
        _git("commit", "-q", "--allow-empty", "-m", subject, cwd=worktree)

    # The driver points PYTHONPATH at the worker's own checkout before launching anything, so a
    # tree that has to look like a checkout of this repository carries the mechanism where this
    # one does. Since #508 the mechanism resolves through its OWN interpreter and this link is no
    # longer what makes `agent_os.lib` importable -- it keeps the throwaway tree shaped like the
    # real one. Excluded from git so it never shows up as work the worker left behind.
    (worktree / "agent_os").symlink_to(AGENT_OS_DIR)
    exclude = worktree / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"{exclude.read_text() if exclude.is_file() else ''}\nagent_os\n")

    cache = tmp_path / "cache"
    cache.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, contents in (
        ("gh", GH_STUB_STAGED),
        ("claude", FAKE_BACKEND),
        ("qwen", FAKE_BACKEND),
        # The fictitious third backend of #514: Claude-shaped events, a name nothing compares.
        ("foo", FAKE_BACKEND),
    ):
        stub = binaries / name
        stub.write_text(contents)
        stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        PLANNER_CACHE_DIR=str(tmp_path / "planner"),
        AGENT_OS_GH_REPO="owner/name",
        GH_STUB_BODY=_staged_body(*stage_titles),
        GH_STUB_LABELS_JSON=json.dumps(labels_by_issue or {}),
        GH_CALLS=str(tmp_path / "gh_calls.tsv"),
        GH_TOKEN="stub-token-so-no-app-is-minted",
        FAKE_BACKEND_MODE=mode,
        FAKE_BACKEND_PROMPTS=str(tmp_path / "prompts.txt"),
        FAKE_BACKEND_COST=cost,
    )
    if config_path is not None:
        environment["AGENTS_CONFIG_PATH"] = str(config_path)
    return environment, cache, worktree, tmp_path


@contextlib.contextmanager
def _planner_lock_held(cache):
    """The guard's exit hook ends every run by calling `wake`, whose one door is a non-blocking
    flock on `planner.lock`. Holding it here is what keeps a test from starting a real planner
    run: `wake` finds the lock taken, leaves the events on disk -- which is exactly what these
    tests read -- and returns."""
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "planner.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _wait_until(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def _events_of_kind(cache, kind):
    directory = cache / "planner_events"
    return (
        [path for path in directory.glob(f"*-{kind}-*") if path.is_file()]
        if directory.is_dir()
        else []
    )


def _subjects(worktree):
    return subprocess.run(
        ["git", "-C", str(worktree), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_the_driver_counts_done_stages_from_the_branchs_own_commits(tmp_path):
    """Two of the three stages are committed, among a guard freeze and an unrelated commit that
    are not stage commits at all -- the driver's count is what the `stage N/M:` subjects say and
    nothing else."""
    environment, cache, _worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass", "Document it"),
        mode="hang",
        subjects=(
            "stage 1/3: Write the failing test",
            "WIP: cut by guard (stall)",
            "chore: not a stage at all",
            "stage 2/3: Make it pass",
        ),
    )
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (cache / "worker_claude.stage").read_text().strip() == "2/3"
        status = subprocess.run(
            ["bash", str(DRIVER), "claude", "status"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "stages:    2/3" in status.stdout, status.stdout
        assert "this process: stage 3" in status.stdout, status.stdout
    finally:
        _stop(environment)


def test_the_prompt_names_the_next_stage_as_the_sole_goal_and_the_done_ones_as_done(tmp_path):
    environment, _cache, _worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass", "Document it"),
        mode="hang",
        subjects=("stage 1/3: Write the failing test", "stage 2/3: Make it pass"),
    )
    try:
        assert _start(environment, "347").returncode == 0
        prompt = (tmp / "prompts.txt").read_text()
        assert "Your only goal in this process: stage 3/3: Document it" in prompt
        assert "Stages already done: 1..2 of 3" in prompt
        assert "stage 2/3: Make it pass" in prompt  # the last three commit subjects
        # The stage it is on is the only one it is told to close, and nothing invites the next.
        assert "stage 4/3" not in prompt
    finally:
        _stop(environment)


def test_a_relaunched_stage_is_told_to_build_on_the_guards_wip_commit(tmp_path):
    environment, _cache, _worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="hang",
        subjects=("stage 1/2: Write the failing test", "WIP: cut by guard (budget)"),
    )
    try:
        assert _start(environment, "347").returncode == 0
        prompt = (tmp / "prompts.txt").read_text()
        assert "Your only goal in this process: stage 2/2: Make it pass" in prompt
        assert "build on\nit, do not start the stage over." in prompt
    finally:
        _stop(environment)


def test_a_pre_merge_freeze_is_not_the_wip_a_relaunched_stage_is_told_to_build_on(tmp_path):
    """`has_wip_after_last_stage_commit` decides whether the next process is told its stage is
    unfinished. Above a finished stage, `open-pr`'s pre-merge freeze says the opposite -- every
    stage landed and the run reached its pull request -- so the walk sees through it to the
    `stage 1/2:` commit below and hands the next stage over as a new one (#407). The freeze that IS
    a cut is the test above this one."""
    environment, _cache, _worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="hang",
        subjects=("stage 1/2: Write the failing test", "WIP: cut by guard (before_merge)"),
    )
    try:
        assert _start(environment, "347").returncode == 0
        prompt = (tmp / "prompts.txt").read_text()
        assert "Your only goal in this process: stage 2/2: Make it pass" in prompt
        assert "build on\nit, do not start the stage over." not in prompt
    finally:
        _stop(environment)


def test_a_clean_exit_without_a_stage_commit_is_a_cut_that_freezes_what_was_left(tmp_path):
    """The process was given one stage and ended without the commit that closes it: that is a cut,
    not a finished stage, and what it left uncommitted is frozen for the next process."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="clean_exit_no_commit",
    )
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            state = (cache / "worker_claude.state").read_text().splitlines()
            assert state[0] == "CUT_BY_GUARD reason=no_stage_commit", state
            assert _subjects(worktree)[0] == "WIP: cut by guard (no_stage_commit)"
            detail = _events_of_kind(cache, "worker_cut")[0].read_text()
            assert "no_stage_commit" in detail
            assert not any(subject.startswith("stage ") for subject in _subjects(worktree)), (
                "nothing claimed a stage, so nothing may look like one"
            )
    finally:
        _stop(environment)


def test_the_freeze_leaves_the_diary_out_of_the_wip_commit(tmp_path):
    """#407: on a branch forked before `main` stopped tracking the diary, the file is tracked, so
    the freeze's `git add -u` swept it into the `WIP: cut by guard` commit -- and from there into
    the pull request, onto `main`, and into every later branch's conflict with it. What the run left
    behind is still frozen; the diary is not, and its own lines stay in the worktree."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="clean_exit_no_commit",
    )
    (worktree / "scratchpad").mkdir()
    (worktree / "scratchpad" / "progress.log").write_text(
        "2026-09-17 09:00  EXPECT a-stage normal=10m cutoff=20m\n"
    )
    _git("add", "scratchpad/progress.log", cwd=worktree)
    _git("commit", "-qm", "a branch forked before main stopped tracking the diary", cwd=worktree)
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            assert _subjects(worktree)[0] == "WIP: cut by guard (no_stage_commit)"

            frozen = subprocess.run(
                ["git", "-C", str(worktree), "show", "--name-only", "--format=", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            assert "README.md" in frozen, frozen
            assert "scratchpad/progress.log" not in frozen, frozen
            # Not committed and not discarded: the lines the run wrote are still in the worktree,
            # which is what keeps it dirty for `start`/`resume` to refuse a relaunch over.
            status = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "scratchpad/progress.log" in status, status
            assert "still-working" in (worktree / "scratchpad" / "progress.log").read_text()
    finally:
        _stop(environment)


def test_the_freeze_leaves_the_diary_out_when_the_worker_staged_it_itself(tmp_path):
    """The exclusion has to reach the index, not only the `git add -u` that fills it: a worker that
    staged the diary by hand before it was cut put it there itself, and the freeze commits whatever
    the index holds (#407)."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="clean_exit_no_commit",
    )
    environment["FAKE_BACKEND_STAGE_DIARY"] = "1"
    (worktree / "scratchpad").mkdir()
    (worktree / "scratchpad" / "progress.log").write_text(
        "2026-09-17 09:00  EXPECT a-stage normal=10m cutoff=20m\n"
    )
    _git("add", "scratchpad/progress.log", cwd=worktree)
    _git("commit", "-qm", "a branch forked before main stopped tracking the diary", cwd=worktree)
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            frozen = subprocess.run(
                ["git", "-C", str(worktree), "show", "--name-only", "--format=", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            assert "README.md" in frozen, frozen
            assert "scratchpad/progress.log" not in frozen, frozen
            # Unstaged, not thrown away: both lines the run appended are still in the file.
            diary_text = (worktree / "scratchpad" / "progress.log").read_text()
            assert "still-working" in diary_text and "staged-by-hand" in diary_text
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# WHAT A FREEZE TAKES (#417). `git add -u` reaches only paths git already tracks, so a stage cut
# after writing a new file left that file untracked -- behind its own freeze, in the one state
# `resume` refuses. The three tests below are the whole contract: a cut leaves nothing for a human
# to add and `resume` starts on it, an ignore rule still keeps a file out, and a freeze that swept
# nothing in says nothing about untracked files.
# ---------------------------------------------------------------------------------------------


def _frozen_paths(worktree):
    """The paths the newest commit carries -- what a freeze took, and so what it left out."""
    return subprocess.run(
        ["git", "-C", str(worktree), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def _newest_commit_body(worktree):
    """The freeze commit's body, which is where it names the files it swept in untracked."""
    return subprocess.run(
        ["git", "-C", str(worktree), "log", "-1", "--format=%b"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _work_left_behind(worktree):
    """`git status --porcelain` minus the two paths a freeze leaves alone by design: the diary
    (#407), which plain `--porcelain` reports as its directory while nothing tracks it, and the
    `.env` link `launch_stage` creates itself (#404). Anything still listed here is work a cut left
    uncommitted, which is exactly what makes `resume` refuse the worktree it just froze."""
    left_by_design = {"?? scratchpad/", "?? scratchpad/progress.log", "?? .env"}
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [line for line in status if line not in left_by_design]


def _run_is_over(cache, backend="claude"):
    """The launcher subshell has exited. `stage-exit`, `open-pr` and the guard's exit hook all run
    inside it, so a cut is not over when its event lands -- and this is the driver's own `alive`
    check, the first thing `resume` asks."""
    pidfile = cache / f"worker_{backend}.pid"
    if not pidfile.is_file():
        return True
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
    except (OSError, ValueError):
        return True
    return False


def _config_with_backend_foo(tmp_path):
    """A copy of config.example.yaml declaring a fictitious backend `foo` that reads as
    `claude_jsonl` and is cut on the Claude quota detector, plus one worker class on it -- the
    config a third CLI costs, and nothing else (#514). No code anywhere knows the name."""
    config = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    config["project"]["backends"]["foo"] = {
        "worktree": "../example-foo",
        "app": "example-foo",
        "stream": "claude_jsonl",
        "quota": "claude_rate_limit",
    }
    worker_class = dict(config["classes"]["mechanical-qwen"], backend="foo", model="foo-model-1")
    config["classes"]["mechanical-foo"] = worker_class
    path = tmp_path / "agents-foo.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


@pytest.mark.parametrize("backend", ["claude", "foo"])
def test_a_cut_stage_leaves_nothing_untracked_and_resume_starts_without_a_human(tmp_path, backend):
    """#417: #416's stage 2 wrote two new files, some 1,200 lines, and was cut on budget before it
    added them; the freeze took the four tracked ones, `resume` then refused the worktree as dirty,
    and the task waited for a human to commit by hand what the mechanism had just produced. A cut
    now leaves a tree `resume` accepts, and its own commit names what it swept in.

    Run for `foo` too (#514): a backend that exists only as a config entry naming `stream:
    claude_jsonl` is dispatched, cut and resumed exactly like `claude`, by a stub on PATH."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="clean_exit_no_commit",
        config_path=_config_with_backend_foo(tmp_path) if backend == "foo" else None,
    )
    environment["FAKE_BACKEND_UNTRACKED"] = "price_candidates.py,tests/test_candidates_pricing.py"
    # NO `.git/info/exclude` entry for the diary (#22). The original host keeps one by hand (PR
    # #406) and this test used to add it too, which hid that `resume` refused every relaunch after
    # a cut over the diary the cut run itself had written. The fake backend appends to the diary,
    # so the cut leaves it untracked here, exactly as on a host that never made that manual step.
    exclude = worktree / ".git" / "info" / "exclude"
    assert "progress.log" not in exclude.read_text()
    diary = worktree / "scratchpad" / "progress.log"
    try:
        with _planner_lock_held(cache):
            started = _start(environment, "347", backend=backend)
            assert started.returncode == 0, started.stdout + started.stderr
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / f"worker_{backend}.log"
            ).read_text()
            assert _subjects(worktree)[0] == "WIP: cut by guard (no_stage_commit)"

            frozen = _frozen_paths(worktree)
            assert "price_candidates.py" in frozen, frozen
            assert "tests/test_candidates_pricing.py" in frozen, frozen
            assert "README.md" in frozen, frozen
            # Not the link `launch_stage` put there itself: the freeze drops it exactly as
            # `uncommitted_work` does, and only while it holds a symlink (#404).
            assert ".env" not in frozen, frozen
            # Named in the body so a reader of the branch can tell swept-in work from work the
            # agent committed -- and only the swept-in paths: `README.md` was already tracked.
            body = _newest_commit_body(worktree)
            assert "price_candidates.py" in body, body
            assert "tests/test_candidates_pricing.py" in body, body
            assert "README.md" not in body, body
            # Nothing left for a human to add, and the files themselves are what the stage wrote.
            assert _work_left_behind(worktree) == []
            assert "never added" in (worktree / "price_candidates.py").read_text()

            assert _wait_until(lambda: _run_is_over(cache, backend)), (
                "the launcher subshell never exited"
            )
            # The one thing still dirty is the cut run's own diary -- the case #22 is about.
            assert "scratchpad/progress.log" not in frozen, frozen
            diary_before = diary.read_text()
            assert "still-working" in diary_before
            resumed = _resume(environment, backend)
            assert resumed.returncode == 0, resumed.stdout + resumed.stderr
            assert "worktree is dirty" not in resumed.stdout, resumed.stdout
            assert "resume refused" not in resumed.stdout, resumed.stdout
            # Reached the backend -- proof `resume` accepted the tree the cut left, with no
            # manual `git add` and no `--force` anywhere in this test.
            assert "started pid" in resumed.stdout, resumed.stdout
            # And the diary is where the cut left it, for the monitor and the resumed run.
            assert diary.read_text().startswith(diary_before)
            if backend == "foo":
                # The class on `foo` named the model, and the run's own events are `foo`'s.
                assert "model:     foo-model-1" in started.stdout, started.stdout
                assert (cache / "worker_foo.jsonl").is_file()
            _stop(environment, backend)
    finally:
        _stop(environment, backend)


def test_the_freeze_leaves_a_file_that_is_untracked_for_a_reason_alone(tmp_path):
    """Untracked because an ignore rule says so stays untracked: a `.gitignore` entry and a
    `.git/info/exclude` entry are both left out of the freeze commit and left on disk, while a new
    file with no such reason is swept in beside them. `--exclude-standard` is the whole rule -- the
    freeze knows no ignore pattern of its own."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="clean_exit_no_commit",
    )
    environment["FAKE_BACKEND_UNTRACKED"] = "minted.token,notes.local.md,price_candidates.py"
    (worktree / ".gitignore").write_text("*.token\n")
    _git("add", ".gitignore", cwd=worktree)
    _git("commit", "-qm", "a .gitignore this branch carries", cwd=worktree)
    exclude = worktree / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "notes.local.md\n")
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            frozen = _frozen_paths(worktree)
            assert "price_candidates.py" in frozen, frozen
            assert "minted.token" not in frozen, frozen
            assert "notes.local.md" not in frozen, frozen
            body = _newest_commit_body(worktree)
            assert "minted.token" not in body, body
            assert "notes.local.md" not in body, body
            # Not committed and not cleaned either: an ignore rule is no licence to delete a file.
            assert (worktree / "minted.token").is_file()
            assert (worktree / "notes.local.md").is_file()
            # An ignored file is not what `git status` reports, so the tree still reads clean.
            assert _work_left_behind(worktree) == []
    finally:
        _stop(environment)


def test_a_freeze_that_took_only_tracked_paths_says_nothing_about_untracked_ones(tmp_path):
    """The body exists to name what a freeze swept in, so a freeze that swept nothing in carries no
    body. The diary is untracked here -- its state on every branch cut since `main` stopped tracking
    it -- and it stays the one untracked path a freeze must leave alone (#407), which is what makes
    "no body" and "no diary in the commit" the same assertion from two sides."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="clean_exit_no_commit",
    )
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            assert _subjects(worktree)[0] == "WIP: cut by guard (no_stage_commit)"
            assert _frozen_paths(worktree) == ["README.md"]
            assert _newest_commit_body(worktree).strip() == ""
            assert "still-working" in (worktree / "scratchpad" / "progress.log").read_text()
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# THE GUARD'S CUT REACHES THE SAME FREEZE (#482). `cut_run` kept a freeze of its own -- `git add
# -u`, then a commit of whatever that staged -- so it never received #417's untracked half above:
# #457's cut carried one modified file and left another source file and its test, some 900
# lines, untracked behind their own `WIP: cut by guard` commit, for a human to move out of the
# worktree by hand before anything could run there again. The three tests below call the guard's
# real `cut_run` -- no `_start`, no backend, no events -- and read the tree it leaves behind.
# ---------------------------------------------------------------------------------------------


def _cut_worktree(tmp_path, monkeypatch):
    """What a cut stage leaves in its worktree -- a tracked file it modified, a new file it never
    added, an untracked diary, and the `.env` link the driver creates itself -- beside the isolated
    cache the cut has to run under. `WORKER_CACHE_DIR` is the load-bearing half of that: `cut_run`
    stops the run before it freezes it, and `stop` kills whatever pid `$cache/worker_<backend>.pid`
    names, which on the cache this test process inherits is a run that is actually alive."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    _git("checkout", "-q", "-b", "claude/482-cut", cwd=worktree)

    (worktree / "README.md").write_text("base\nthe stage edited a tracked file\n")
    (worktree / "price_candidates.py").write_text("the stage wrote this and never added it\n")
    diary = worktree / "scratchpad" / "progress.log"
    diary.parent.mkdir()
    diary.write_text(DIARY_HEARTBEAT)
    (worktree / ".env").symlink_to(tmp_path / "main.env")

    cache = tmp_path / "cache"
    cache.mkdir()
    statefile = cache / "worker_claude.state"
    statefile.write_text("STARTED\nissue=482 label=status:doing\n")
    monkeypatch.setenv("WORKER_CACHE_DIR", str(cache))
    return worktree, statefile


def test_a_guard_cut_freezes_the_file_the_stage_never_added(tmp_path, monkeypatch):
    """The freeze a tick's cut reaches is the driver's, so a cut leaves nothing for a human to add
    and its own commit says what it swept in -- the three outcomes #482 asks for, on one tree."""
    worktree, statefile = _cut_worktree(tmp_path, monkeypatch)
    subjects_before = _subjects(worktree)

    agent_guard.cut_run("claude", "stall", worktree=worktree, statefile=statefile, main=ROOT)

    assert _subjects(worktree)[0] == "WIP: cut by guard (stall)"
    assert len(_subjects(worktree)) == len(subjects_before) + 1
    frozen = _frozen_paths(worktree)
    assert "README.md" in frozen, frozen
    assert "price_candidates.py" in frozen, frozen
    # The diary (#407) and the driver's own link (#404) are the two paths a freeze leaves alone:
    # out of the commit, and still on disk for the monitor and the next launch to read.
    assert "scratchpad/progress.log" not in frozen, frozen
    assert ".env" not in frozen, frozen
    assert "still-working" in (worktree / "scratchpad" / "progress.log").read_text()
    assert (worktree / ".env").is_symlink()
    # Named in the body, and only the swept-in path: `README.md` was already tracked.
    body = _newest_commit_body(worktree)
    assert "price_candidates.py" in body, body
    assert "README.md" not in body, body
    # Nothing else is left behind, which is what makes `resume` accept the tree a cut just froze.
    assert _work_left_behind(worktree) == []
    # The terminal state is written whatever the freeze did, line 2's marker intact.
    assert agent_guard.read_state_marker(statefile) == (
        "CUT_BY_GUARD reason=stall",
        482,
        "status:doing",
    )


def test_a_guard_cut_leaves_the_runs_scratch_out_of_the_freeze(tmp_path, monkeypatch):
    # #417's sweep took every untracked file but the diary, scratch drafts included, into the
    # cut's WIP commit on the PR branch. Hidden since #86 -- also on a worktree whose run started
    # before `start` hid it, which is this one -- they stay on disk and out of the commit.
    worktree, statefile = _cut_worktree(tmp_path, monkeypatch)
    (worktree / "scratchpad" / "notes.md").write_text("a draft the stage wrote for itself\n")

    agent_guard.cut_run("claude", "stall", worktree=worktree, statefile=statefile, main=ROOT)

    frozen = _frozen_paths(worktree)
    assert "price_candidates.py" in frozen, frozen
    assert not [path for path in frozen if path.startswith("scratchpad/")], frozen
    assert (worktree / "scratchpad" / "notes.md").is_file()
    assert "scratchpad" not in _newest_commit_body(worktree)
    assert _work_left_behind(worktree) == []


def test_a_guard_cut_on_a_tree_with_nothing_to_freeze_writes_no_commit(tmp_path, monkeypatch):
    """The clean arm, which is what a cut did before #482 and does again: no empty `WIP` commit --
    every one of those is an attempt spent against `planner.relaunch_cap` on a run nothing was tried
    on -- and the terminal state written all the same, so a cut run never reads as alive."""
    worktree, statefile = _cut_worktree(tmp_path, monkeypatch)
    agent_guard.cut_run("claude", "stall", worktree=worktree, statefile=statefile, main=ROOT)
    subjects = _subjects(worktree)
    statefile.write_text("STARTED\nissue=482 label=status:doing\n")

    agent_guard.cut_run("claude", "stall", worktree=worktree, statefile=statefile, main=ROOT)

    assert _subjects(worktree) == subjects
    assert agent_guard.read_state_marker(statefile) == (
        "CUT_BY_GUARD reason=stall",
        482,
        "status:doing",
    )


def _git_commands(script):
    """Every `git` invocation a shell script actually runs. Comment lines go, and what is left is
    split on the separators a command can sit behind, so a staging command is found wherever it
    hides. Prose is not a command and no segment of it starts with `git` -- the RULES heredoc tells
    the worker never to run `git add -A`, and that sentence is the rule, not a breach of it."""
    commands = []
    for line in script.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for segment in re.split(r"&&|\|\||[;|]|\$\(|\bthen\b|\bdo\b", line):
            segment = segment.strip()
            if segment.startswith("git "):
                commands.append(segment)
    return commands


def test_no_cut_stages_itself_and_no_freeze_reaches_for_git_add_all():
    """The two halves of the rule the trees above cannot show. `cut_run` issues no git command of
    its own -- that is what reaching the freeze through the driver means, and it is why a swept-in
    path can be asserted at all -- and nothing the driver runs stages with `git add -A`, which would
    freeze the same files and take the `.cache` symlink with it: the paths arrive one by one from
    `ls-files --others`, so what an ignore rule keeps out stays out."""
    guard = ast.parse((AGENT_OS_DIR / "agent_os" / "guard.py").read_text())
    cut = next(
        node
        for node in ast.walk(guard)
        if isinstance(node, ast.FunctionDef) and node.name == "cut_run"
    )
    literals = {
        node.value
        for node in ast.walk(cut)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not literals & {"git", "add", "-u", "-A", "--all"}, sorted(literals)

    staging = [
        command
        for command in _git_commands((AGENT_OS_DIR / "bin" / "worker_task.sh").read_text())
        if re.search(r"\badd\b", command)
    ]
    assert staging, "the driver stages nothing: this audit has drifted away from the code"
    assert not [c for c in staging if "-A" in c or "--all" in c], staging


def test_the_chain_runs_every_stage_in_its_own_process_and_ends_in_worker_finished(tmp_path):
    """Two stages, two processes: the first one's exit chains the second, each one's spend is
    archived under `.cache/spend/<issue>/`, and only the last one's exit writes `worker_finished`.
    """
    environment, cache, worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="stage_commit",
    )
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_finished")), (
                cache / "worker_claude.log"
            ).read_text()
            assert _subjects(worktree)[:2] == [
                "stage 2/2: Make it pass",
                "stage 1/2: Write the failing test",
            ]
            archived = sorted(path.name for path in (cache / "spend" / "347").glob("*.jsonl"))
            assert len(archived) == 2, archived
            assert archived[0].endswith("-claude-stage1.jsonl")
            assert archived[1].endswith("-claude-stage2.jsonl")
            assert (cache / "worker_claude.stage").read_text().strip() == "2/2"
            assert not _events_of_kind(cache, "worker_cut")
            # Two processes, two prompts, and the second one knew the first stage was done.
            prompts = (tmp / "prompts.txt").read_text()
            assert prompts.count("=== end of prompt ===") == 2
            assert "Your only goal in this process: stage 2/2: Make it pass" in prompts
    finally:
        _stop(environment)


def test_a_relaunch_with_nothing_left_to_launch_writes_no_worker_finished_event(tmp_path):
    """#443: an issue whose `## Stages` are ALL already committed is a REFUSAL -- nothing runs --
    and used to write `worker_finished` anyway. That event reads exactly like the genuine one the
    exit hook writes on a real completion (`test_the_chain_runs_every_stage_in_its_own_process_
    and_ends_in_worker_finished` above), so the planner woke and spent a pass on work that never
    happened. The message, `write_state DONE` and exit 0 are still owed and asserted here too."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test",),
        mode="hang",
        subjects=("stage 1/1: Write the failing test",),
    )
    result = _start(environment, "347")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all 1 stage(s) of #347 are already committed -- nothing to launch" in result.stdout
    assert (cache / "worker_claude.state").read_text().splitlines()[0] == "DONE"
    assert not (cache / "worker_claude.pid").exists()
    assert "started pid" not in result.stdout
    # The branch itself never gained a new commit: the refusal really launched nothing.
    assert _subjects(worktree)[0] == "stage 1/1: Write the failing test"
    assert not _events_of_kind(cache, "worker_finished")


def test_an_unreadable_issue_cuts_the_run_instead_of_declaring_it_finished(tmp_path):
    """A GitHub App installation token lasts an hour, so a multi-stage run outlives the one it was
    launched with and `gh issue view` starts answering 401. That read failing is NOT the issue
    having no stages: treating the two alike declared #363 finished and threw away three committed
    stages. It is a cut, which the planner can relaunch, and a cut opens no pull request."""
    environment, cache, _worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="hang",
        subjects=("stage 1/2: Write the failing test",),
    )
    (cache / "worker_claude.issue").write_text("347\n")
    (cache / "worker_claude.stage").write_text("1/2\n")

    # `gh issue view` answers 401 and everything else still works -- what an expired installation
    # token actually looks like, rather than a `gh` that is broken outright.
    stub_dir = environment["PATH"].split(os.pathsep)[0]
    expired = tmp_path / "expired_token_bin"
    expired.mkdir()
    (expired / "gh").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "issue" ] && [ "$2" = "view" ]; then\n'
        '  echo "gh: Bad credentials (HTTP 401)" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{stub_dir}/gh" "$@"\n'
    )
    (expired / "gh").chmod(0o755)
    environment = dict(environment)
    environment["PATH"] = f"{expired}{os.pathsep}{environment['PATH']}"
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "stage-exit"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not read issue #347" in result.stdout, result.stdout
    assert "not finished" in result.stdout
    state = (cache / "worker_claude.state").read_text().splitlines()
    assert state[0] == "CUT_BY_GUARD reason=issue_unreadable", state

    # And the caller's next step must not offer half an issue for review.
    opened = subprocess.run(
        ["bash", str(DRIVER), "claude", "open-pr"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "CUT_BY_GUARD reason=issue_unreadable -- no pull request" in opened.stdout, opened.stdout
    calls = tmp / "gh_calls.tsv"
    recorded = calls.read_text().splitlines() if calls.exists() else []
    assert not any(line.startswith("pr\t") for line in recorded), "a cut run opened a pull request"


def test_the_progress_comment_reaches_the_issue_when_the_last_stage_lands(tmp_path):
    """One comment per run end, naming stages done / total, the branch, the last commit and what
    happened to anything uncommitted -- posted by the driver, never by the agent."""
    environment, cache, _worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="hang",
        subjects=("stage 1/2: Write the failing test", "stage 2/2: Make it pass"),
    )
    (cache / "worker_claude.issue").write_text("347\n")
    (cache / "worker_claude.stage").write_text("1/2\n")
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "stage-exit"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    # Non-zero is the signal to its caller that the run ends here, not a failure.
    assert result.returncode == 1, result.stdout + result.stderr
    assert "was the last one" in result.stdout
    comments = [
        line
        for line in (tmp / "gh_calls.tsv").read_text().splitlines()
        if line.startswith("api\t") and "comments" in line
    ]
    assert len(comments) == 1, comments
    assert "Stages 2/2 done on claude/347-staged" in comments[0]
    assert "tree clean" in comments[0]


def test_the_chain_stops_with_a_paused_cut_when_the_epic_carries_agents_paused(tmp_path):
    """The human's full stop outranks everything: the stage that landed is kept, the next one is
    never launched, and the planner is told why."""
    epic = str(load_project().tracking_epic)
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="stage_commit",
        labels_by_issue={epic: [load_project().labels.agents_paused]},
    )
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            assert (cache / "worker_claude.state").read_text().splitlines()[0] == (
                "CUT_BY_GUARD reason=paused"
            )
            # The first stage is committed and kept; the second was never launched.
            assert _subjects(worktree)[0] == "stage 1/2: Write the failing test"
            assert len(sorted((cache / "spend" / "347").glob("*.jsonl"))) == 1
            assert not _events_of_kind(cache, "worker_finished")
    finally:
        _stop(environment)


def test_a_run_that_ends_inside_the_start_check_is_not_a_failed_launch(tmp_path):
    """The settling check after a launch asks "is this process alive", and a run shorter than it
    is not -- a one-turn stage, or a backend refused at once, ends exactly that fast. What tells a
    short run from a launch that never happened is the driver's own state line: the run has
    already replaced it with a terminal one. CI caught this when a whole fake run finished inside
    the window and `start` reported "failed to start" with the exit hook's output as its evidence,
    so what this asserts is the property that does not depend on which side of the race won: the
    launch reports success either way, and the run reaches its terminal state.
    """
    environment, cache, _worktree, _tmp = _staged_environment(
        tmp_path, stage_titles=("Do the thing",), mode="clean_exit_no_commit"
    )
    try:
        with _planner_lock_held(cache):
            # No recorded issue, so the whole tail after the backend -- stage-exit, open-pr, the
            # exit hook -- is as short as it can be, which is what puts the end of the run inside
            # the window. `launch-stage` is the chain's own door: it skips `start`'s gates.
            result = subprocess.run(
                ["bash", str(DRIVER), "claude", "launch-stage"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "failed to start" not in result.stdout, result.stdout
            assert _wait_until(
                lambda: (cache / "worker_claude.state").read_text().startswith("DONE")
            ), (cache / "worker_claude.log").read_text()
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# The issue's whole spend in the shape Qwen reports it (#387): `usage.total_tokens` on the
# terminal `result` event, and no `total_cost_usd` anywhere in the file -- which is what made the
# dollar ceiling, the only one the stage gate read before #387, measure 0.0000 on every worker
# class.
# ---------------------------------------------------------------------------------------------


def _qwen_stage_log(*, total_tokens, session="qwen-stage"):
    """Two events rather than one, so a report reading it has a turn to take a per-stage context
    size off as well as the run's own total: one `assistant` turn, a third of the run, and the
    terminal `result` carrying the whole of it."""
    turn_input = max(total_tokens // 3, 1)
    return "".join(
        json.dumps(event) + "\n"
        for event in (
            {
                "type": "assistant",
                "session_id": session,
                "message": {"usage": {"input_tokens": turn_input, "output_tokens": 40}},
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": session,
                "duration_ms": 1000,
                "usage": {
                    "input_tokens": total_tokens - 40,
                    "output_tokens": 40,
                    "total_tokens": total_tokens,
                },
            },
        )
    )


def _archive_qwen_stage(cache, backend, issue, stage, *, total_tokens):
    """One stage's log filed exactly where `archive_stage_events` files it, as if that stage's
    process had ended and been archived before the one now running."""
    spend = cache / "spend" / str(issue)
    spend.mkdir(parents=True, exist_ok=True)
    path = spend / f"20260916T10{stage:02d}00Z-{backend}-stage{stage}.jsonl"
    path.write_text(_qwen_stage_log(total_tokens=total_tokens))
    return path


def test_the_stage_gate_refuses_to_chain_once_the_issue_passes_its_token_ceiling(tmp_path):
    """Neither of the two stages that landed reports a cost, so the issue's dollar sum is 0.0000
    against a ceiling of 20.0 and the dollar half of the gate passes untouched. Its token sum is
    past its own ceiling, and that alone ends the chain: before #387 nothing here could see this
    run's spend at all, so a Qwen worker chained stage after stage with an inert backstop."""
    environment, cache, worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass", "Document it"),
        mode="hang",
        subjects=("stage 1/3: Write the failing test", "stage 2/3: Make it pass"),
    )
    environment["AGENTS_CONFIG_PATH"] = str(_config_with_max_total_tokens(tmp_path, 1000))
    archived = [
        _archive_qwen_stage(cache, "qwen", 347, 1, total_tokens=600),
        _archive_qwen_stage(cache, "qwen", 347, 2, total_tokens=900),
    ]
    assert all("total_cost_usd" not in path.read_text() for path in archived), (
        "the shape under test is a backend that reports no cost"
    )
    (cache / "worker_qwen.issue").write_text("347\n")
    (cache / "worker_qwen.stage").write_text("1/3\n")

    result = subprocess.run(
        ["bash", str(DRIVER), "qwen", "stage-exit"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    # Non-zero is the caller's signal that the run ends here, not a failure of the driver.
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not chaining stage 3 -- budget" in result.stdout, result.stdout
    # Which of the two ceilings fired, with both measurements: the dollars nowhere near theirs,
    # the tokens past theirs. This is the line #342 reads when it calibrates the placeholders.
    assert "spent $0.0000 of $20.0 and 1500 of 1000 tokens" in result.stdout, result.stdout
    assert (cache / "worker_qwen.state").read_text().splitlines()[0] == (
        "CUT_BY_GUARD reason=budget"
    )
    # Both stages that landed are kept, and no process was launched for the third.
    assert _subjects(worktree)[:2] == [
        "stage 2/3: Make it pass",
        "stage 1/3: Write the failing test",
    ]
    assert not (tmp / "prompts.txt").exists(), "the chain launched a stage process anyway"
    assert not _events_of_kind(cache, "worker_finished")


def test_the_status_report_prints_the_issues_token_total_next_to_the_stage_context_line(tmp_path):
    """`status` has always printed the per-stage context size against `max_context`, this stage
    process's own ceiling. The issue's whole token spend is the figure the stage gate cuts on, and
    a report showing only one of the two scopes leaves the one that decides the chain invisible
    (#387). No process is launched: the driver's own files are written as a run would leave them,
    including the live log of the stage in flight."""
    stage_titles = ("Write the failing test", "Make it pass", "Document it")
    environment, cache, _worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=stage_titles,
        mode="hang",
        subjects=("stage 1/3: Write the failing test",),
    )
    (cache / "worker_qwen.issue").write_text("347\n")
    (cache / "worker_qwen.stage").write_text("1/3\n")
    (cache / "worker_qwen.body.md").write_text(_staged_body(*stage_titles) + "\n")
    _archive_qwen_stage(cache, "qwen", 347, 1, total_tokens=600)
    (cache / "worker_qwen.jsonl").write_text(_qwen_stage_log(total_tokens=900))

    status = subprocess.run(
        ["bash", str(DRIVER), "qwen", "status"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0, status.stdout + status.stderr
    lines = status.stdout.splitlines()
    context = next(i for i, line in enumerate(lines) if line.startswith("  context"))
    task_class = load_task_classes()["complex-qwen"]
    # The per-stage half of the report is unchanged: one turn's input side, against this stage
    # process's own ceiling -- the ISSUE's own class (`complex-qwen`), not qwen's per-backend
    # default of 400,000 (#483). Before the fix this asserted
    # `"  context   300 tokens  (budget 400,000) ok"`: it read as `ok` only because the stub
    # backend's own default happened to be well above 300 tokens either way, and the number was
    # never the issue's class in the first place -- `complex-qwen`'s own ceiling is 800,000.
    assert lines[context] == f"  context   300 tokens  (budget {task_class.max_context:,}) ok", (
        lines[context]
    )
    assert lines[context + 1] == (
        f"  issue     1500 tokens across every stage of #347  (ceiling {task_class.max_total_tokens})"
    ), status.stdout
    # This backend reports no cost, so the report invents none: no dollar figure anywhere in it.
    assert "$" not in status.stdout, status.stdout


# ---------------------------------------------------------------------------------------------
# THE BACKEND'S EXECUTABLE COMES FROM CONFIG, NOT FROM THE LAUNCHER'S PATH (#380). The incident:
# on the first unattended dispatch (#363, 2026-09-16) `qwen` lived under nvm, the PATH the systemd
# user manager hands the guard's own service unit did not carry that directory, and the stage process
# died in under a second. These tests drive the driver with TWO binaries of the same name -- one
# first on PATH, one named by `project.executables` -- and assert which of them ran.
# ---------------------------------------------------------------------------------------------


def _config_with_executable(tmp_path, name, path):
    """A copy of config.example.yaml with `project.executables` carrying one entry. The
    shipped file leaves the mapping empty on purpose -- a configured absolute path outranks the
    PATH stubbing every driver test protects itself with -- so a test that wants one writes its
    own, through the same `AGENTS_CONFIG_PATH` isolation the cap fixtures use."""
    text = EXAMPLE_CONFIG.read_text()
    assert "\n  executables: {}\n" in text, "config.example.yaml's project.executables line changed"
    patched = text.replace("\n  executables: {}\n", f"\n  executables:\n    {name}: {path}\n", 1)
    config = tmp_path / "executables.yaml"
    config.write_text(patched)
    return config


def test_the_configured_executable_wins_over_a_same_named_binary_earlier_on_path(tmp_path):
    """The whole point of the key: the driver must not care what PATH the process that launched it
    carried. The fake backend is moved OFF PATH and named only by `project.executables`, and a
    different binary of the same name is left first on PATH with a marker that says if it ran."""
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path, stage_titles=("Do the thing",), mode="stage_commit"
    )
    configured = tmp_path / "elsewhere" / "claude"
    configured.parent.mkdir()
    configured.write_text(FAKE_BACKEND)
    configured.chmod(0o755)

    # First on PATH, and never to be run: it would exit 1 without making the stage's commit.
    marker = tmp_path / "the-binary-on-path-ran"
    on_path = tmp_path / "bin" / "claude"
    on_path.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit 1\n")
    on_path.chmod(0o755)

    environment["AGENTS_CONFIG_PATH"] = str(_config_with_executable(tmp_path, "claude", configured))
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_finished")), (
                cache / "worker_claude.log"
            ).read_text()
            assert _subjects(worktree)[0] == "stage 1/1: Do the thing"
            assert not marker.exists(), "the binary first on PATH ran instead of the configured one"
    finally:
        _stop(environment)


def test_every_driver_resolves_its_backend_through_the_one_configured_lookup():
    """One resolver, three drivers: the worker backends and the role drivers' own `claude` all go
    through `agent_lib backend-executable`, so an unattended dispatch and a shell dispatch can
    never run a different binary. Asserted on the scripts' text because the two role drivers reach
    their CLI only by spending a turn on it."""
    driver = DRIVER.read_text()
    assert 'backend_bin=$(agent_executable "$backend") || exit 1' in driver
    assert 'WORKER_BACKEND_BIN" --model' in driver
    # ... and the one implementation of `agent_executable` itself asks `agent_lib`.
    shared = (AGENT_OS_DIR / "bin" / "agent_task.sh").read_text()
    assert "-m agent_os.lib backend-executable" in shared
    for script in ("agent_task.sh", "planner_task.sh"):
        text = (AGENT_OS_DIR / "bin" / script).read_text()
        assert 'agent_executable "$launch_backend"' in text, script
        assert '"${AGENT_CLAUDE_BIN:-claude}"' not in text
        assert '"${PLANNER_CLAUDE_BIN:-claude}"' not in text


# ---------------------------------------------------------------------------------------------
# A STAGE PROCESS THAT NEVER STARTED IS A FAILED LAUNCH, NOT A STAGE CUT FOR NOT COMMITTING
# (#381). The incident: on #363 `qwen: orden no encontrada` in the run's log surfaced as
# `CUT_BY_GUARD reason=no_stage_commit` with `stages: 0/5` -- mechanically true, and the wrong
# cause. The signal is the shell's own 127 rather than the message, which is written in whatever
# language the shell speaks.
# ---------------------------------------------------------------------------------------------


def test_a_backend_command_that_does_not_exist_is_a_failed_launch_not_a_cut(tmp_path):
    """Its contrast is `test_a_clean_exit_without_a_stage_commit_is_a_cut_that_freezes_what_was
    _left` above, which drives a backend that really ran and committed nothing: that one still
    reports `CUT_BY_GUARD reason=no_stage_commit` and still freezes the tree. This one must do
    neither, because a `WIP: cut by guard` commit is what `resume` counts against
    `planner.relaunch_cap`, and nothing was tried here."""
    missing = tmp_path / "nowhere" / "claude"
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="stage_commit",
        config_path=_config_with_executable(tmp_path, "claude", missing),
    )
    subjects_before = _subjects(worktree)
    try:
        with _planner_lock_held(cache):
            result = _start(environment, "347")
            assert _wait_until(
                lambda: (cache / "worker_claude.state").read_text().startswith("FAILED_LAUNCH")
            ), (cache / "worker_claude.log").read_text()

            state = (cache / "worker_claude.state").read_text().splitlines()
            assert state[0] == f"FAILED_LAUNCH command={missing} status=127", state
            # `start` itself reports the launch as made: its two-second settling check asks
            # whether the backgrounded subshell is alive, and that subshell outlives the backend
            # CLI by design. What the planner reads is the state and the event below.
            assert result.returncode == 0, result.stdout + result.stderr

            # Nothing was tried, so nothing may be spent: no freeze commit, hence no relaunch
            # attempt consumed, and no pull request for a run that produced nothing.
            assert _subjects(worktree) == subjects_before
            assert not any("WIP: cut by guard" in subject for subject in _subjects(worktree))
            calls = tmp_path / "gh_calls.tsv"
            assert not calls.is_file() or "pr\tcreate" not in calls.read_text()

            # ... and the planner is woken with the real cause on its first tick.
            assert _wait_until(lambda: _events_of_kind(cache, "worker_cut")), (
                cache / "worker_claude.log"
            ).read_text()
            detail = _events_of_kind(cache, "worker_cut")[0].read_text()
            assert "FAILED_LAUNCH" in detail and str(missing) in detail, detail
            assert "no_stage_commit" not in detail, detail
    finally:
        _stop(environment)


def test_status_prints_the_failed_launch_state_and_the_command_that_was_missing(tmp_path):
    environment, cache, _worktree, _tmp = _staged_environment(
        tmp_path, stage_titles=("Do the thing",), mode="hang"
    )
    (cache / "worker_claude.state").write_text("FAILED_LAUNCH command=/nowhere/qwen\n")
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "status"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "FAILED_LAUNCH command=/nowhere/qwen" in result.stdout, result.stdout
    assert "never started" in result.stdout
    assert "project.executables" in result.stdout


# ---------------------------------------------------------------------------------------------
# `start` REFUSES A WORKTREE THAT IS NOT WHERE THIS ISSUE'S WORK BELONGS (#388). The evidence:
# after #363 finished, a stale worktree from a finished issue stayed on its branch, nothing in `start`
# looked, and the next dispatch would have written its commits inside that already-open pull
# request. Accepted: the issue's own base (`Base: <branch>`, `main` when it names none) or any
# branch whose name carries the issue number.
# ---------------------------------------------------------------------------------------------


def _base_check_environment(tmp_path, *, branch, body=VALID_BODY):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    if branch != "main":
        _git("checkout", "-q", "-b", branch, cwd=worktree)

    cache = tmp_path / "cache"
    cache.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, contents in (
        ("gh", GH_STUB_FULL),
        ("claude", NEVER_EXITS_BACKEND_STUB),
        ("qwen", NEVER_EXITS_BACKEND_STUB),
    ):
        stub = binaries / name
        stub.write_text(contents)
        stub.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
        ROEDOR_GH_REPO="owner/name",
        GH_STUB_BODY=body,
        GH_STUB_LABELS_JSON=json.dumps({"347": ["module:workers"]}),
        GH_TOKEN="stub-token-so-no-app-is-minted",
    )
    return environment, cache, worktree


def test_start_refuses_a_worktree_left_on_the_previous_issues_branch(tmp_path):
    environment, cache, worktree = _base_check_environment(
        tmp_path, branch="task/363-prompts-config-literals"
    )
    result = _start(environment, "347")
    assert result.returncode == 1
    assert "refusing to dispatch" in result.stdout
    assert str(worktree) in result.stdout
    assert "task/363-prompts-config-literals" in result.stdout
    assert "main" in result.stdout
    # A refusal writes nothing -- the same shape as the dirty-worktree and parallelism refusals.
    assert list(cache.iterdir()) == []


def test_start_accepts_a_branch_whose_name_carries_this_issues_number(tmp_path):
    environment, _cache, _worktree = _base_check_environment(tmp_path, branch="task/347-the-work")
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "refusing to dispatch" not in result.stdout
        assert "started pid" in result.stdout
    finally:
        _stop(environment)


def test_start_accepts_a_hyphenated_prefix_and_its_refusal_names_the_shape(tmp_path):
    """agent-os#16: the planner branched `agent-os/37-gradle-skeleton` and `start 37` refused it
    while telling it to use "a branch naming #37" -- which it was. The anchoring that stops a wrong
    branch is on the number (`/<issue>` then `-`, `/` or the end), not on the prefix being letters
    only; so a hyphenated prefix passes, a hyphenated prefix does NOT let `…/387-close-the-390-gap`
    through for #390, and the refusal spells out the shape it accepts instead of paraphrasing it."""
    (tmp_path / "accepted").mkdir()
    environment, _cache, _worktree = _base_check_environment(
        tmp_path / "accepted", branch="agent-os/347-the-work"
    )
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "started pid" in result.stdout
    finally:
        _stop(environment)

    (tmp_path / "refused").mkdir()
    environment, cache, _worktree = _base_check_environment(
        tmp_path / "refused", branch="agent-os/387-close-the-390-gap"
    )
    refused = _start(environment, "390")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "refusing to dispatch" in refused.stdout
    assert "<word>/390-<slug>" in refused.stdout, refused.stdout
    assert list(cache.iterdir()) == []


def test_start_accepts_the_worktree_sitting_on_the_base_the_issue_names(tmp_path):
    # `Base: feature/340-parent` -- a stacked issue, whose work does not belong on the trunk. The
    # same resolution `open-pr` uses, so the gate and the pull request cannot disagree.
    environment, _cache, worktree = _base_check_environment(
        tmp_path,
        branch="feature/340-parent",
        body=VALID_BODY + "\n\nBase: feature/340-parent\n",
    )
    try:
        result = _start(environment, "347")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "started pid" in result.stdout
    finally:
        _stop(environment)
    # ... and `main` is no longer good enough for an issue that names another base.
    _git("checkout", "-q", "main", cwd=worktree)
    refused = _start(environment, "347")
    assert refused.returncode == 1
    assert "feature/340-parent" in refused.stdout


def test_start_with_force_skips_the_base_branch_check_and_says_so(tmp_path):
    environment, _cache, _worktree = _base_check_environment(
        tmp_path, branch="task/363-prompts-config-literals"
    )
    try:
        result = _start(environment, "347", "--force")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "base-branch check skipped (--force)" in result.stdout
        assert "task/363-prompts-config-literals" in result.stdout
    finally:
        _stop(environment)


def test_the_base_branch_check_runs_before_the_parallelism_cap_and_the_module_exclusion(tmp_path):
    """Ordering as a property, not as a line number: with BOTH a wrong branch and another backend
    alive on a module this issue shares, the refusal that comes out is the base-branch one -- the
    check that costs no `gh` call, before the one that does."""
    environment, cache, _worktree = _base_check_environment(
        tmp_path, branch="task/363-prompts-config-literals"
    )
    other = _spawn_alive_other_backend(cache, "qwen", 500)
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "but issue #347's base is" in result.stdout
        assert "worker(s) already running" not in result.stdout
        assert "shares module label" not in result.stdout
    finally:
        other.terminate()
        other.wait(timeout=5)


# ---------------------------------------------------------------------------------------------
# THE `.env` LINK THE DRIVER CREATES IS NOT UNCOMMITTED WORK (#404). `launch_stage` links
# `$main/.env` into a worktree that has none, and the dirty check read that link as an untracked
# file -- so a relaunch was refused for the driver's own doing, and the base-branch test above
# failed every run on a machine whose checkout has a `.env`: its second `start`, which has to be
# refused for sitting on `main`, was refused as `?? .env` instead. This repository ignores `.env`
# (.gitignore:14), which is why the real worktrees never showed it; a temporary repository with no
# ignore rule does, and that is what these tests build.
# ---------------------------------------------------------------------------------------------


def test_a_restart_is_not_refused_by_the_env_link_the_first_start_created(tmp_path):
    environment, _cache, worktree = _base_check_environment(tmp_path, branch="task/347-the-work")
    try:
        first = _start(environment, "347")
        assert first.returncode == 0, first.stdout + first.stderr
        # The link that first launch left behind. Made by hand when this checkout has no `.env` for
        # the driver to link, so what is measured is the dirty check and not the machine the suite
        # happens to run on -- the convention test_agent_task.py sets for the very same link.
        link = worktree / ".env"
        if not link.is_symlink():
            link.symlink_to(tmp_path / "the-main-checkouts-env")
    finally:
        _stop(environment)

    # Git does count the link as untracked work in this repository, so the restart below proceeds
    # in spite of an entry `git status` reports -- which is the whole of the fix, and what makes
    # the test say something on a machine where the driver created no link either.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "?? .env" in status.stdout, status.stdout

    second = _start(environment, "347")
    try:
        assert "worktree is dirty" not in second.stdout, second.stdout
        assert second.returncode == 0, second.stdout + second.stderr
        assert "started pid" in second.stdout
    finally:
        _stop(environment)


def test_a_real_env_file_is_still_uncommitted_work(tmp_path):
    """What the dirty check drops is the driver's own LINK, not the path: a `.env` that is a file
    -- secrets a worker wrote and must never commit -- refuses as any other untracked file does."""
    environment, cache, worktree = _base_check_environment(tmp_path, branch="task/347-the-work")
    (worktree / ".env").write_text("SEC_USER_AGENT=someone@example.invalid\n")
    try:
        result = _start(environment, "347")
        assert result.returncode == 1
        assert "worktree is dirty" in result.stdout, result.stdout
        assert "?? .env" in result.stdout, result.stdout
        # A refusal writes nothing -- the shape every other refusal in this file has.
        assert not (cache / "worker_claude.pid").exists()
    finally:
        _stop(environment)


# ---------------------------------------------------------------------------------------------
# `open-pr` MERGES THE BASE BEFORE PUSHING (#389). GitHub creates `refs/pull/N/merge` only for a
# pull request it can merge, and an `on: pull_request` workflow checks out exactly that ref: a
# conflicting pull request therefore gets no workflow RUN at all, carries zero checks rather than
# failing ones, and can never meet the merge conditions. Measured on PR #386 (2026-09-16).
# ---------------------------------------------------------------------------------------------


def _advance_the_base(remote, *, path, contents, subject):
    """One commit pushed to `origin/main` from a throwaway clone -- the base moving ahead while
    the worker was busy, which is the only way a pull request is born in conflict."""
    clone = remote.parent / f"advance-{path}"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    # The bare origin's own HEAD still names the branch `git init --bare` invented, so the clone
    # checks nothing out: put it on `main` explicitly.
    _git("checkout", "-q", "-B", "main", "origin/main", cwd=clone)
    _git("config", "user.email", "other@example.invalid", cwd=clone)
    _git("config", "user.name", "other", cwd=clone)
    (clone / path).write_text(contents)
    _git("add", path, cwd=clone)
    _git("commit", "-qm", subject, cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)


def _head_subject(worktree):
    return _subjects(worktree)[0]


# ---------------------------------------------------------------------------------------------
# `branch` WITH NO EXPLICIT BASE STARTS FROM `origin/main`, NOT THE LOCAL `main` THE SHARED `.git`
# HAPPENS TO HOLD (#435). The local ref only moves when a human's own checkout runs `pull`, so a
# merge that landed on GitHub between two dispatches left a worker building on a base already
# behind the remote -- repaired silently by `open-pr`'s own merge (#389) unless it conflicts or
# never starts, both a wasted round trip `git fetch` before branching avoids.
# ---------------------------------------------------------------------------------------------


def _worktree_with_origin(tmp_path):
    """A worktree pushed to a real (bare, local) origin, `main` on both sides -- what `branch`
    starts a new one from."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    _git("remote", "add", "origin", str(remote), cwd=worktree)
    _git("push", "-q", "-u", "origin", "main", cwd=worktree)
    return remote, worktree


def _branch_environment(tmp_path, worktree):
    cache = tmp_path / "cache"
    cache.mkdir()
    environment = dict(os.environ)
    environment.update(WORKER_WORKTREE=str(worktree), WORKER_CACHE_DIR=str(cache))
    return environment


def _branch(environment, *arguments):
    return subprocess.run(
        ["bash", str(DRIVER), "claude", "branch", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_branch_with_no_explicit_base_starts_from_the_remotes_tip_not_the_stale_local_main(
    tmp_path,
):
    """The defect itself: `_advance_the_base` moves `origin/main` ahead exactly the way a PR
    merged on GitHub does, while the worktree's own local `main` -- what `branch` used to read --
    never moves, since nothing here ever runs `git pull`. This fails on today's code, whose new
    branch forks from the stale local `main` and so never carries the remote's tip."""
    remote, worktree = _worktree_with_origin(tmp_path)
    _advance_the_base(remote, path="AHEAD.md", contents="ahead\n", subject="a merge nobody pulled")
    remote_tip = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    local_main = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert local_main != remote_tip, "the fixture is meaningless if the local ref already moved"

    result = _branch(_branch_environment(tmp_path, worktree), "task/999-thing")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "from origin/main" in result.stdout, result.stdout

    new_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "task/999-thing"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert new_head == remote_tip


def test_branch_refuses_when_the_fetch_fails_instead_of_falling_back_to_the_stale_local_main(
    tmp_path,
):
    """Offline, or the remote gone: a base nobody can name is worse than no dispatch (#435), so the
    fetch failing is a refusal with its reason, not a silent fall back to the local ref."""
    _remote, worktree = _worktree_with_origin(tmp_path)
    _git("remote", "set-url", "origin", "/no/such/path.git", cwd=worktree)

    result = _branch(_branch_environment(tmp_path, worktree), "task/999-thing")
    assert result.returncode != 0
    assert "could not fetch origin/main" in result.stdout, result.stdout

    branches = subprocess.run(
        ["git", "-C", str(worktree), "branch", "--list", "task/999-thing"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == "", "a refused fetch must not leave a branch on a base nobody named"


def test_branch_honours_an_explicit_from_verbatim(tmp_path):
    """Resuming on a named commit or an existing branch is what the argument is for, and #435 must
    not start rewriting it -- even with `origin/main` ahead, an explicit `<from>` wins."""
    remote, worktree = _worktree_with_origin(tmp_path)
    _git("checkout", "-q", "-b", "task/300-earlier", cwd=worktree)
    (worktree / "earlier.py").write_text("x = 1\n")
    _git("add", "earlier.py", cwd=worktree)
    _git("commit", "-qm", "earlier work", cwd=worktree)
    earlier_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "task/300-earlier"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _advance_the_base(remote, path="AHEAD.md", contents="ahead\n", subject="a merge nobody pulled")

    result = _branch(_branch_environment(tmp_path, worktree), "task/999-thing", "task/300-earlier")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "from task/300-earlier" in result.stdout, result.stdout

    new_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "task/999-thing"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert new_head == earlier_head


# --------------------------------------------------------------------------------------------
# `init` -- creates the worktree if absent (#392, agent_os/docs/AGENT_OS.md §7 row (r))
# --------------------------------------------------------------------------------------------
# Unlike `branch`, `init` runs before any worktree exists, so its fetch has nowhere to run but
# `$main` -- which `agent_task.sh` resolves from `AGENT_OS_HOST_ROOT` and `cd`s into. These tests
# override it with a throwaway HOST ROOT of their own, pushed to a throwaway (bare, local) origin,
# so `init`'s fetch never touches the real repository or the network.


def _minimal_host_root(tmp_path, *, name="main_checkout"):
    """The smallest `AgentsConfig` accepts, committed and pushed to a local bare origin -- `init`
    only ever reads `project.repo`/`tracking_epic`/`board_number` (required) and `classes` (an
    empty mapping is a valid, if pointless, one)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    root = tmp_path / name
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "host@example.invalid", cwd=root)
    _git("config", "user.name", "host", cwd=root)
    (root / "config").mkdir()
    (root / "config" / "agents.yaml").write_text(
        "project:\n  repo: owner/name\n  tracking_epic: 1\n  board_number: 1\nclasses: {}\n"
    )
    _git("add", "config/agents.yaml", cwd=root)
    _git("commit", "-qm", "base", cwd=root)
    _git("remote", "add", "origin", str(remote), cwd=root)
    _git("push", "-q", "-u", "origin", "main", cwd=root)
    return root


def _init_environment(tmp_path, root, worktree):
    cache = tmp_path / "cache"
    cache.mkdir()
    environment = dict(os.environ)
    environment.update(
        AGENT_OS_HOST_ROOT=str(root),
        WORKER_WORKTREE=str(worktree),
        WORKER_CACHE_DIR=str(cache),
    )
    return environment


def _init(environment):
    return subprocess.run(
        ["bash", str(DRIVER), "claude", "init"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_init_creates_the_worktree_on_a_fresh_branch_from_origin_main(tmp_path):
    root = _minimal_host_root(tmp_path)
    worktree = tmp_path / "worktree"

    result = _init(_init_environment(tmp_path, root, worktree))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "created" in result.stdout, result.stdout
    assert (worktree / ".git").exists()

    remote_tip = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "origin/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    branch_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_head == remote_tip
    branch_name = subprocess.run(
        ["git", "-C", str(worktree), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_name == "agent-os/init-claude"


def test_init_is_idempotent_on_an_already_initialized_worktree(tmp_path):
    root = _minimal_host_root(tmp_path)
    worktree = tmp_path / "worktree"
    environment = _init_environment(tmp_path, root, worktree)
    first = _init(environment)
    assert first.returncode == 0, first.stdout + first.stderr

    second = _init(environment)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already initialized" in second.stdout, second.stdout
    branch_name = subprocess.run(
        ["git", "-C", str(worktree), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_name == "agent-os/init-claude", "the second call must not have touched the branch"


def test_init_links_venv_and_env_from_the_host_root_when_present(tmp_path):
    root = _minimal_host_root(tmp_path)
    (root / ".venv").mkdir()
    (root / ".env").write_text("SECRET=1\n")
    worktree = tmp_path / "worktree"

    result = _init(_init_environment(tmp_path, root, worktree))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (worktree / ".venv").is_symlink()
    assert (worktree / ".env").is_symlink()


def _with_project_keys(environment, tmp_path, **keys):
    """`config.example.yaml`, which the suite already loads, with `keys` set under `project:`."""
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["project"].update(keys)
    config = tmp_path / "agents-provisioned.yaml"
    config.write_text(yaml.safe_dump(data, sort_keys=False))
    environment["AGENTS_CONFIG_PATH"] = str(config)


def test_init_links_the_configured_paths_and_runs_the_setup_command_in_the_worktree(tmp_path):
    """agent-os#41: a monorepo keeps its environment under subdirectories, not a root `.venv`."""
    root = _minimal_host_root(tmp_path)
    (root / "backend").mkdir()
    (root / "backend" / ".venv").mkdir()
    worktree = tmp_path / "worktree"
    environment = _init_environment(tmp_path, root, worktree)
    _with_project_keys(
        environment,
        tmp_path,
        worktree_links=["backend/.venv", ".env"],
        worktree_setup_command='printf "%s" "$PWD" > provisioned-by-setup',
    )

    result = _init(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (worktree / "backend" / ".venv").is_symlink()
    assert (worktree / "backend" / ".venv").resolve() == (root / "backend" / ".venv").resolve()
    assert not (worktree / ".venv").exists(), "a root .venv the host did not list was linked"
    assert (worktree / "provisioned-by-setup").read_text() == str(worktree)


def test_init_refuses_and_removes_the_worktree_when_the_setup_command_fails(tmp_path):
    root = _minimal_host_root(tmp_path)
    worktree = tmp_path / "worktree"
    environment = _init_environment(tmp_path, root, worktree)
    _with_project_keys(environment, tmp_path, worktree_setup_command="exit 5")

    result = _init(environment)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "project.worktree_setup_command failed" in result.stdout, result.stdout
    assert not worktree.exists()
    # A rerun starts from nothing again instead of calling a half-provisioned tree initialized.
    _with_project_keys(environment, tmp_path, worktree_setup_command="")
    again = _init(environment)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "created" in again.stdout, again.stdout


def test_init_refuses_when_the_fetch_fails_and_creates_nothing(tmp_path):
    root = _minimal_host_root(tmp_path)
    _git("remote", "set-url", "origin", "/no/such/path.git", cwd=root)
    worktree = tmp_path / "worktree"

    result = _init(_init_environment(tmp_path, root, worktree))
    assert result.returncode != 0
    assert "could not fetch origin/main" in result.stdout, result.stdout
    assert not worktree.exists()


def test_open_pr_merges_a_base_that_moved_ahead_before_pushing(worker_at_its_end):
    environment, worktree, remote, _cache, calls = worker_at_its_end
    _advance_the_base(remote, path="BASE.md", contents="moved\n", subject="the base moved ahead")

    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "merged main into claude/348-pr" in result.stdout, result.stdout

    # The merge is a commit on the branch, and its subject names the base and what it merged.
    subject = _head_subject(worktree)
    assert subject.startswith("Merge main ("), subject
    assert "into claude/348-pr before opening the pull request" in subject
    # ... the branch really carries the base's commit now, which is what produces the merge ref,
    assert (worktree / "BASE.md").is_file()
    # ... and the pushed branch does too.
    pushed = subprocess.run(
        ["git", "-C", str(remote), "log", "--format=%s", "claude/348-pr"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "the base moved ahead" in pushed
    assert "pr\tcreate" in calls.read_text()
    assert "mergeable=MERGEABLE" in result.stdout


def test_open_pr_opens_the_pull_request_anyway_when_the_merge_conflicts(worker_at_its_end):
    environment, worktree, remote, _cache, calls = worker_at_its_end
    # Both sides rewrite README.md, which the fixture's base commit created.
    (worktree / "README.md").write_text("the worker's line\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "the worker rewrote the readme", cwd=worktree)
    _advance_the_base(
        remote, path="README.md", contents="somebody else's line\n", subject="the base rewrote it"
    )
    head_before = _head_subject(worktree)

    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "conflicts on:" in result.stdout, result.stdout
    assert "README.md" in result.stdout

    # `git merge --abort` left the worktree exactly as it was: no merge commit, nothing unstaged.
    assert _head_subject(worktree) == head_before
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == "", status
    assert (worktree / "README.md").read_text() == "the worker's line\n"

    # ... and the pull request exists anyway, with the issue asking a human to resolve it.
    recorded = calls.read_text()
    assert "pr\tcreate" in recorded
    assert "labels[]=status:blocked-on-human" in recorded, recorded
    assert "labels[]=status:ai-completed" not in recorded, recorded
    # The comment body is several lines, and the stub records one call per line of argv -- so the
    # call is counted on its first line and its text read off the whole recording.
    posted = [line for line in recorded.splitlines() if "/comments\t-X\tPOST" in line]
    assert len(posted) == 1, recorded
    assert "README.md" in recorded
    assert "ZERO checks" in recorded


def test_open_pr_reads_the_pull_requests_mergeable_field_and_reports_conflicting(worker_at_its_end):
    """The local merge is what should have made it mergeable; GitHub is the authority on whether
    it did. `CONFLICTING` means no CI will run, and both stdout and the issue comment say so."""
    environment, _worktree, _remote, _cache, calls = worker_at_its_end
    environment["GH_STUB_MERGEABLE"] = "CONFLICTING"

    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mergeable=CONFLICTING" in result.stdout

    recorded = calls.read_text()
    assert "labels[]=status:blocked-on-human" in recorded
    posted = [line for line in recorded.splitlines() if "/comments\t-X\tPOST" in line]
    assert len(posted) == 1, recorded
    assert "ZERO checks" in recorded
    assert "refs/pull/N/merge" in recorded
    assert "did not conflict locally" in recorded


def test_open_pr_neither_fetches_nor_merges_when_it_was_going_to_refuse_anyway(worker_at_its_end):
    """The branch has nothing on it, which `open-pr` already refuses for its own reason. Proof
    that the whole step is skipped: the base moved on origin and the worktree's own `origin/main`
    is still where it was -- a fetch would have updated it."""
    environment, worktree, remote, _cache, calls = worker_at_its_end
    _git("reset", "-q", "--hard", "origin/main", cwd=worktree)
    before = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "origin/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    _advance_the_base(remote, path="BASE.md", contents="moved\n", subject="the base moved ahead")

    result = _open_pr(environment)
    assert result.returncode == 0
    assert "no commits ahead" in result.stdout
    after = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "origin/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before, "origin/main moved, so the fetch ran on a run that refuses anyway"
    assert not calls.is_file() or "pr\tcreate" not in calls.read_text()


# ---------------------------------------------------------------------------------------------
# AN ISSUE A WORKER IS RUNNING ALWAYS CARRIES `status:doing` (#385). `start` set it and nothing
# else did, so a relaunch and every chained stage inherited whatever label the issue happened to
# hold -- and a human's reply clearing `status:blocked-on-human` under a live worker left the board
# showing nothing at all for work that was actually happening (twice on #363, 2026-09-16).
# ---------------------------------------------------------------------------------------------


def _resumable_run(cache, *, issue="347"):
    """What `start` leaves behind and `resume` reads back: the recorded issue, the assembled
    brief, and the cut this resume is picking up from."""
    (cache / "worker_claude.issue").write_text(f"{issue}\n")
    (cache / "worker_claude.brief.md").write_text("the brief start assembled\n")
    (cache / "worker_claude.state").write_text("CUT_BY_GUARD reason=stall\n")


def test_resume_puts_the_issue_back_on_doing_and_mirrors_the_column(tmp_path):
    """The issue carries NO `status:*` label at all -- the state a human's reply leaves it in --
    and the relaunch must not depend on whoever set one last."""
    environment, cache, _worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="hang",
        labels_by_issue={"347": []},
    )
    environment["GH_STUB_BOARD_ITEM"] = "347"
    _resumable_run(cache)
    try:
        result = subprocess.run(
            ["bash", str(DRIVER), "claude", "resume"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        recorded = (tmp_path / "gh_calls.tsv").read_text()
        assert "labels[]=status:doing" in recorded, recorded
        # ... and the board column is really edited, not merely attempted.
        edits = [line for line in recorded.splitlines() if line.startswith("project\titem-edit")]
        assert len(edits) == 1, recorded
        assert "OPT_DOING" in edits[0]
        # The driver records what it wrote, so the guard's tick compares like with like.
        assert (cache / "worker_claude.state").read_text().splitlines()[1] == (
            "issue=347 label=status:doing"
        )
    finally:
        _stop(environment)


def test_every_chained_stage_asserts_the_label_rather_than_inheriting_it(tmp_path):
    """`launch-stage` is the chain's own door, and it goes through the same call: two stages, and
    `status:doing` is written for each of them, so a label cleared under a live worker is back by
    the next stage at the latest."""
    environment, cache, worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="stage_commit",
        labels_by_issue={"347": []},
    )
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(lambda: _events_of_kind(cache, "worker_finished")), (
                cache / "worker_claude.log"
            ).read_text()
            assert _subjects(worktree)[:2] == [
                "stage 2/2: Make it pass",
                "stage 1/2: Write the failing test",
            ]
            doing = [
                line
                for line in (tmp / "gh_calls.tsv").read_text().splitlines()
                if "labels[]=status:doing" in line
            ]
            # One from `start`, one from the chained stage: the second stage does not rely on the
            # first having set it.
            assert len(doing) == 2, doing
    finally:
        _stop(environment)


def test_launch_stage_asks_for_no_label_when_no_issue_was_recorded(tmp_path):
    """A run that was never staged has no issue to label, and `launch-stage` must not invent one
    -- the same guard every other issue-keyed step in the driver applies."""
    environment, cache, _worktree, tmp = _staged_environment(
        tmp_path, stage_titles=("Do the thing",), mode="clean_exit_no_commit"
    )
    try:
        with _planner_lock_held(cache):
            result = subprocess.run(
                ["bash", str(DRIVER), "claude", "launch-stage"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            calls = tmp / "gh_calls.tsv"
            assert not calls.is_file() or "labels[]=status:doing" not in calls.read_text()
    finally:
        _stop(environment)


def test_open_pr_pushes_the_merge_even_when_the_pull_request_is_already_open(worker_at_its_end):
    """The merge that keeps a pull request mergeable is a commit, and a pull request that already
    exists needs it as much as one about to be created -- the push has to happen on both paths, or
    the merge ref CI needs is never produced for exactly the run that was repaired (#389)."""
    environment, _worktree, remote, _cache, calls = worker_at_its_end
    assert _open_pr(environment).returncode == 0

    _advance_the_base(remote, path="BASE.md", contents="moved\n", subject="the base moved ahead")
    second = _open_pr(environment)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already open" in second.stdout
    assert "merged main into claude/348-pr" in second.stdout

    pushed = subprocess.run(
        ["git", "-C", str(remote), "log", "--format=%s", "claude/348-pr"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "the base moved ahead" in pushed, pushed
    created = [line for line in calls.read_text().splitlines() if line.startswith("pr\tcreate")]
    assert len(created) == 1


def test_a_config_that_does_not_load_stops_the_driver_instead_of_launching_an_empty_command(
    tmp_path,
):
    """`backend-executable` exits non-zero with an empty stdout when `config/agents.yaml` does not
    load, and `$(...)` swallows that status unless the caller checks it. Unchecked, the driver ran
    `"" --model ...`: the shell answers 127, and the failed-launch report then named an EMPTY
    executable -- pointing whoever is debugging at the very key the broken file made unreadable."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            "\n  backends:\n", "\n  a_key_no_schema_has: 1\n  backends:\n", 1
        )
    )
    environment, cache, _worktree = _base_check_environment(tmp_path, branch="task/347-the-work")
    environment["AGENTS_CONFIG_PATH"] = str(broken)

    result = _start(environment, "347")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot resolve the executable for 'claude'" in result.stderr, result.stderr
    assert "does not load" in result.stderr
    # Nothing was launched and nothing was written: a broken config is a refusal, not a run.
    assert "started pid" not in result.stdout
    assert list(cache.iterdir()) == []


def test_the_role_drivers_resolve_their_binary_before_they_write_anything(tmp_path):
    """The same unchecked-status shape lived in both role drivers as `${VAR:-$(...)}`, where an
    empty default is still empty. Asserted on the scripts' text: reaching their CLI for real costs
    a turn, and `--dry-run` returns before the launch."""
    for script in ("agent_task.sh", "planner_task.sh"):
        text = (AGENT_OS_DIR / "bin" / script).read_text()
        assert 'agent_executable "$launch_backend" "${' in text, script
        assert ") || exit 1" in text, script
        assert "-$(agent_executable claude)}" not in text, script
        assert '-$(agent_executable "$launch_backend")}' not in text, script


def test_a_backend_that_is_not_executable_is_a_failed_launch_too(tmp_path):
    """126, not 127: the path exists but cannot be run -- it is a directory, or the `+x` bit went
    with a `git checkout`. Before this, only 127 was classified, so 126 fell through to
    `CUT_BY_GUARD reason=no_stage_commit`; and on a clean tree that cut freezes no commit, so the
    relaunch cap never rose and the planner relaunched into the same broken binary forever."""
    not_executable = tmp_path / "not-executable-claude"
    not_executable.write_text("#!/usr/bin/env bash\nexit 0\n")
    not_executable.chmod(0o644)
    environment, cache, worktree, _tmp = _staged_environment(
        tmp_path,
        stage_titles=("Write the failing test", "Make it pass"),
        mode="stage_commit",
        config_path=_config_with_executable(tmp_path, "claude", not_executable),
    )
    subjects_before = _subjects(worktree)
    try:
        with _planner_lock_held(cache):
            assert _start(environment, "347").returncode == 0
            assert _wait_until(
                lambda: (cache / "worker_claude.state").read_text().startswith("FAILED_LAUNCH")
            ), (cache / "worker_claude.log").read_text()
            state = (cache / "worker_claude.state").read_text().splitlines()[0]
            assert state == f"FAILED_LAUNCH command={not_executable} status=126", state
            # Still not a cut, so still no attempt spent.
            assert _subjects(worktree) == subjects_before
    finally:
        _stop(environment)


def test_open_pr_freezes_a_dirty_tree_so_the_merge_can_start(worker_at_its_end):
    """`git merge` refuses to start with tracked files modified, and the success arm of
    `stage-exit` is the one exit path that never freezes -- so the tree here is dirty by
    construction. A refused merge leaves no unmerged paths, so it used to read as "no conflict"
    and the branch was pushed UNMERGED: the silent no-CI case this step exists to prevent."""
    environment, worktree, remote, _cache, _calls = worker_at_its_end
    (worktree / "delivered.py").write_text("x = 2  # left uncommitted by the worker\n")
    _advance_the_base(remote, path="BASE.md", contents="moved\n", subject="the base moved ahead")

    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "froze uncommitted work in a WIP commit before merging main" in result.stdout
    assert "merged main into claude/348-pr" in result.stdout
    pushed = subprocess.run(
        ["git", "-C", str(remote), "log", "--format=%s", "claude/348-pr"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "the base moved ahead" in pushed, pushed


def test_open_pr_stops_instead_of_pushing_when_the_merge_never_starts(worker_at_its_end):
    """An untracked file the incoming base would overwrite: `git merge` refuses before doing
    anything, so there are no unmerged paths and it is NOT a conflict. Pushing here would publish
    a branch unmerged with its base while reporting nothing wrong."""
    environment, worktree, remote, cache, calls = worker_at_its_end
    _advance_the_base(
        remote, path="NEW.md", contents="from the base\n", subject="the base added it"
    )
    (worktree / "NEW.md").write_text("untracked, and in the way\n")

    result = _open_pr(environment)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "never started" in result.stdout
    assert "NOT pushing" in result.stdout
    state = (cache / "worker_claude.state").read_text().splitlines()
    assert state[0] == "BLOCKED reason=merge_failed base=main", state
    assert not calls.is_file() or "pr\tcreate" not in calls.read_text()
    remote_has = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "claude/348-pr"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert remote_has.returncode != 0, "the branch was pushed unmerged"


def test_open_pr_reports_an_unverified_mergeable_rather_than_assuming_it(worker_at_its_end):
    """GitHub computes `mergeable` asynchronously and answers UNKNOWN for a second or two after
    `pr create`, so a single read took the "not conflicting" branch on timing alone."""
    environment, _worktree, _remote, _cache, _calls = worker_at_its_end
    environment["GH_STUB_MERGEABLE"] = "UNKNOWN"
    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mergeable=UNKNOWN" in result.stdout
    assert "NOT VERIFIED" in result.stdout


def test_collect_audits_the_branchs_own_commits_and_not_a_base_merged_into_it(tmp_path):
    """Since #389 the branch may carry a merge of its own base, and a plain `diff <start>..HEAD`
    then reports the worker for every file the base moved -- `docs/adr/*` among them, so a clean
    run failed the very audit that protects those paths."""
    run_dir = tmp_path / "run"
    worktree = run_dir / "worktree"
    worktree.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=worktree)
    _git("config", "user.email", "worker@example.invalid", cwd=worktree)
    _git("config", "user.name", "worker", cwd=worktree)
    (worktree / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "base", cwd=worktree)
    start_ref = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # The base moves ahead, touching a path no brief can authorize ...
    _git("checkout", "-q", "-b", "the-base", cwd=worktree)
    (worktree / "docs" / "adr").mkdir(parents=True)
    (worktree / "docs" / "adr" / "2026-09-16-somebody-elses.md").write_text("not the worker\n")
    _git("add", "docs/adr/2026-09-16-somebody-elses.md", cwd=worktree)
    _git("commit", "-qm", "a decision somebody else recorded", cwd=worktree)

    # ... the worker's branch writes one innocent file and then merges that base in.
    _git("checkout", "-q", "-b", "claude/348-work", start_ref.strip(), cwd=worktree)
    (worktree / "delivered.py").write_text("x = 1\n")
    _git("add", "delivered.py", cwd=worktree)
    _git("commit", "-qm", "the work", cwd=worktree)
    _git(
        "merge",
        "--no-edit",
        "-q",
        "-m",
        "Merge the-base into claude/348-work",
        "the-base",
        cwd=worktree,
    )

    cache = run_dir / "cache"
    cache.mkdir(parents=True)
    (cache / "worker_claude.startref").write_text(start_ref)
    environment = dict(os.environ)
    environment.update(WORKER_WORKTREE=str(worktree), WORKER_CACHE_DIR=str(cache))
    result = subprocess.run(
        ["bash", str(DRIVER), "claude", "collect"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    audit = result.stdout.split("=== ownership audit ===\n")[1].split("=== ")[0]
    assert "VIOLATION" not in audit, audit
    assert "clean" in audit
    changed = result.stdout.split("=== changed files ===\n")[1].split("=== ")[0]
    assert "delivered.py" in changed
    assert "2026-09-16-somebody-elses.md" not in changed, changed


def test_a_base_merged_into_the_branch_is_not_read_as_this_issues_progress(tmp_path):
    """The fork point used to resolve against the LOCAL base first while `open-pr` merged
    `origin/<base>`, and the stage count is a maximum over `stage N/M:` subjects with no issue
    filter -- so after that merge every stage commit on the base counted as this issue's, and the
    next process skipped a stage or declared the issue done."""
    environment, cache, worktree, tmp = _staged_environment(
        tmp_path, stage_titles=("First", "Second"), mode="hang"
    )
    remote = tmp / "remote.git"
    _advance_the_base(
        remote, path="OTHER.md", contents="x\n", subject="stage 5/5: another issue's last stage"
    )
    _git("fetch", "-q", "origin", "main", cwd=worktree)
    _git("merge", "--no-edit", "-q", "-m", "Merge main into the branch", "FETCH_HEAD", cwd=worktree)
    _resumable_run(cache)

    try:
        result = subprocess.run(
            ["bash", str(DRIVER), "claude", "resume"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "stage:     1/2 of issue #347" in result.stdout, result.stdout
        prompt = (tmp / "prompts.txt").read_text()
        assert "Your only goal in this process: stage 1/2: First" in prompt
        assert "Stages already done: none of 2" in prompt, prompt
    finally:
        _stop(environment)


def test_start_refuses_a_branch_that_merely_contains_the_issue_number(tmp_path):
    """The acceptance was an unanchored token search: `task/387-close-the-390-gap` passed for
    #390 and `chore/2026-09-16-cleanup` for #16. Accepting a wrong branch is the whole failure
    this gate exists to stop, so it is anchored on the planner's own `<word>/<issue>-<slug>`."""
    for branch, issue in (
        ("task/387-close-the-390-gap", "390"),
        ("chore/2026-09-16-cleanup", "16"),
    ):
        run_dir = tmp_path / branch.replace("/", "_")
        run_dir.mkdir()
        environment, cache, _worktree = _base_check_environment(run_dir, branch=branch)
        result = _start(environment, issue)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "refusing to dispatch" in result.stdout, result.stdout
        assert list(cache.iterdir()) == []


def test_start_stops_when_the_issue_body_cannot_be_read_instead_of_defaulting_to_main(tmp_path):
    """A swallowed `gh` failure resolved the base to `main`, which refused a correctly-placed
    stacked dispatch AND accepted a worktree wrongly left on `main` -- wrong in both directions."""
    environment, cache, _worktree = _base_check_environment(tmp_path, branch="feature/340-parent")
    # The stub answers `issue view` only while this is set; unset, every read fails.
    environment["GH_STUB_BODY_FAILS"] = "1"
    broken_stub = tmp_path / "bin" / "gh"
    broken_stub.write_text("#!/usr/bin/env bash\necho 'simulated gh failure' >&2\nexit 7\n")
    broken_stub.chmod(0o755)

    result = _start(environment, "347")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not read issue #347" in result.stdout, result.stdout
    assert list(cache.iterdir()) == []


def test_open_pr_stops_and_leaves_the_issue_alone_when_the_push_is_rejected(worker_at_its_end):
    """A human updated the remote branch: the push is rejected, a fast-forward is not possible,
    and moving the issue on would call finished a pull request whose head is not what this run
    produced."""
    environment, worktree, remote, cache, calls = worker_at_its_end
    _git("push", "-q", "origin", "HEAD:claude/348-pr", cwd=worktree)
    other = remote.parent / "somebody-else"
    subprocess.run(
        ["git", "clone", "-q", "-b", "claude/348-pr", str(remote), str(other)], check=True
    )
    _git("config", "user.email", "human@example.invalid", cwd=other)
    _git("config", "user.name", "human", cwd=other)
    (other / "HUMAN.md").write_text("a human pushed this\n")
    _git("add", "HUMAN.md", cwd=other)
    _git("commit", "-qm", "the human's own commit", cwd=other)
    _git("push", "-q", "origin", "claude/348-pr", cwd=other)
    # ... and this worktree grows a commit of its own, so no fast-forward is possible.
    (worktree / "delivered.py").write_text("x = 3\n")
    _git("add", "delivered.py", cwd=worktree)
    _git("commit", "-qm", "more work", cwd=worktree)

    result = _open_pr(environment)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not push" in result.stdout
    state = (cache / "worker_claude.state").read_text().splitlines()[0]
    assert state == "BLOCKED reason=push_rejected branch=claude/348-pr", state
    recorded = calls.read_text() if calls.is_file() else ""
    assert "labels[]=status:ai-completed" not in recorded, recorded
    # ... but never silently in `doing` (#61): the issue says why and waits for a human.
    assert "labels[]=status:blocked-on-human" in recorded, recorded
    posted = [line for line in recorded.splitlines() if "/comments\t-X\tPOST" in line]
    assert len(posted) == 1, recorded
    assert "rejected" in recorded
    assert "pr\tcreate" not in recorded


def _reject_pushes_like_github_without_workflows_permission(remote):
    """GitHub's own refusal, as the remote says it: a pre-receive hook on the local bare origin
    prints the message a GitHub App without `workflows` permission gets, and declines the push."""
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'refusing to allow a GitHub App to create or update workflow "
        "`.github/workflows/ci.yml` without `workflows` permission' >&2\n"
        "exit 1\n"
    )
    hook.chmod(0o755)


def test_open_pr_classifies_a_workflows_permission_rejection_and_asks_a_human(worker_at_its_end):
    """A stale branch pushed by an App without `workflows` permission is refused because its tree
    differs from the default branch under `.github/workflows/` (#61). That is not a diverged
    remote: it used to be read as one, end in `BLOCKED reason=push_rejected`, and leave finished
    work in `doing` with no pull request, no comment and no `blocked-on-human`."""
    environment, _worktree, remote, cache, calls = worker_at_its_end
    _reject_pushes_like_github_without_workflows_permission(remote)

    result = _open_pr(environment)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "was ahead" not in result.stdout, result.stdout
    assert "workflows" in result.stdout, result.stdout
    state = (cache / "worker_claude.state").read_text().splitlines()
    assert state[0] == "BLOCKED reason=workflows_permission branch=claude/348-pr", state
    assert state[1] == "issue=348 label=status:blocked-on-human", state

    recorded = calls.read_text()
    assert "pr\tcreate" not in recorded, recorded
    assert "labels[]=status:ai-completed" not in recorded, recorded
    assert "labels[]=status:blocked-on-human" in recorded, recorded
    posted = [line for line in recorded.splitlines() if "/comments\t-X\tPOST" in line]
    assert len(posted) == 1, recorded
    # The comment quotes GitHub and says what unblocks it.
    assert "refusing to allow a GitHub App to create or update workflow" in recorded
    assert "merge `origin/main` into `claude/348-pr`" in recorded, recorded
    assert "workflows: write" in recorded


def test_open_pr_names_the_conflict_when_the_stale_branch_is_refused(worker_at_its_end):
    """The conflict path is the one that pushes a stale branch, so it is the one that hits the
    workflows refusal: the comment names the conflicting paths the human has to resolve."""
    environment, worktree, remote, cache, calls = worker_at_its_end
    (worktree / "README.md").write_text("the worker's line\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-qm", "the worker rewrote the readme", cwd=worktree)
    _advance_the_base(
        remote, path="README.md", contents="somebody else's line\n", subject="the base rewrote it"
    )
    _reject_pushes_like_github_without_workflows_permission(remote)

    result = _open_pr(environment)
    assert result.returncode == 1, result.stdout + result.stderr
    state = (cache / "worker_claude.state").read_text().splitlines()[0]
    assert state == "BLOCKED reason=workflows_permission branch=claude/348-pr", state
    recorded = calls.read_text()
    assert "- `README.md`" in recorded, recorded
    assert "labels[]=status:blocked-on-human" in recorded, recorded


def test_open_pr_clears_a_previous_blocked_line_when_it_succeeds(worker_at_its_end):
    """A human fixes what blocked `open-pr` and runs it again: the pull request opens, and the
    earlier `BLOCKED` line must not outlive it -- `write_state_marker` preserves line 1 (#61)."""
    environment, _worktree, _remote, cache, calls = worker_at_its_end
    (cache / "worker_claude.state").write_text(
        "BLOCKED reason=workflows_permission branch=claude/348-pr\n"
        "issue=348 label=status:blocked-on-human\n"
    )

    result = _open_pr(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pr\tcreate" in calls.read_text()
    state = (cache / "worker_claude.state").read_text().splitlines()
    assert state == ["DONE", "issue=348 label=status:ai-completed"], state


def test_resume_leaves_a_label_a_human_set_on_a_live_issue_alone(tmp_path):
    """Re-asserting `doing` without reading reverted a human who had taken a live issue's question
    over (`blocked-on-human`) or was looking at it (`review`) -- at the next stage boundary,
    silently. Only an issue carrying NO `status:*` label at all is put back."""
    environment, cache, _worktree, tmp = _staged_environment(
        tmp_path,
        stage_titles=("First", "Second"),
        mode="hang",
        labels_by_issue={"347": ["status:blocked-on-human"]},
    )
    _resumable_run(cache)
    try:
        result = subprocess.run(
            ["bash", str(DRIVER), "claude", "resume"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "already carries status:blocked-on-human" in result.stdout, result.stdout
        recorded = (tmp / "gh_calls.tsv").read_text()
        assert "labels[]=status:doing" not in recorded, recorded
    finally:
        _stop(environment)
