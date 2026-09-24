"""`agent_os/bin/agent_task.sh` -- what one role's wake hands the next role (#475).

A one-shot role runs DETACHED: the driver exports the whole run -- `AGENT_RUN_ROLE`, `SUBJECT`,
`MODEL`, `BACKEND`, `LAUNCH_BACKEND`, `RULES`, `INSTRUCTION`, `CONTEXT`, `DIR`, `LOGFILE`,
`PIDFILE`, `WORKTREE`, `NO_WAKE`, `FINISHED_SUFFIX`, plus `AGENT_DETACHED_RUN=yes` and a run-scoped
`PYTHONPATH` at the throwaway worktree -- and re-enters itself under `setsid` to live inside that
environment. Its own last step, `agent_guard.py wake`, USED TO BE called from inside it, so the
planner the wake started inherited every one of those variables, and a role THAT planner launched
was met by the detached re-entry check (`agent_os/bin/agent_task.sh`) before a single argument of
its own was parsed. It then ran the PREVIOUS role's brief against the PREVIOUS role's worktree --
frozen at the head that role cut, and removed under it -- appended to the previous role's log, and
left no row of its own in `runs.tsv`. The re-entry now also requires that the invocation carry no
argument, the two calls that LEAVE a run go out through `env -u` on the whole block, and a launch
unsets whatever block it inherited before it resolves anything (`agent_run_environment_names`,
the list those three uses share).

These tests run the whole chain and measure it: role A on one pull request, A's own wake, and role
B launched from inside that wake on a second pull request, with the branch's head moved between the
two launches the way a worker's correction moves it. What stands in for what:

- the backend is a stub on `AGENT_CLAUDE_BIN` that records the environment it was called with;
- `agent_os.guard` is a stub that records the environment of the `event` and `wake` calls
  and, on the first `wake`, does the one thing the real one does that matters here: it starts a
  process that inherits its environment and launches the next role from inside it. It writes no
  event and starts no planner, so nothing here reaches the tracker, the network or a paid backend;
  the fixture holds `planner.lock` as well, so even a chain that escaped the stubs would find the
  one door to a real planner shut;
- `git` is a stub on `PATH` that answers `ls-remote` out of a file and never reaches origin;
- the driver runs out of a DISPOSABLE stand-in for the host checkout: a symlink farm holding the
  real driver and the resolver it sources under `agent_os/bin/`, the host's `config/`, and a
  `.git` FILE naming this repository's own gitdir -- which is all `git worktree add` asks of it,
  in a linked worktree and in a plain clone alike. Since #508 the driver no longer derives its
  interpreter or its host root from its own path, so the farm names both outright:
  `AGENT_OS_HOST_ROOT` is the farm, and `AGENT_OS_PYTHON` is a one-call dispatcher that sends
  `-m agent_os.guard` to the stub above and everything else to the real interpreter.

Pure filesystem and subprocess. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest
from conftest import EXAMPLE_CONFIG

from agent_os.cli import AGENT_OS_DIR, host_root

# The HOST project this suite runs inside: not a fixed nesting under AGENT_OS_DIR (that
# breaks the moment a copy IS the mechanism's own top directory, as the out-of-tree proof
# makes it -- #512), but whatever `host_root()` itself resolves: the git checkout's toplevel,
# same as every real driver run.
ROOT = host_root()

# The validator is the role under test because it is the only one that runs anything, and so the
# only one the driver prepares a worktree for (`runs_tests=yes`): the worktree is one of the things
# a role has to resolve from its own invocation and not inherit. Both launches are the same role on
# two pull requests, which is the shape the defect was caught in -- a planner relaunching the
# validator it had just been woken by -- and the hardest case for `runs.tsv`, since the two rows
# belong in the same file.
ROLE = "validator"
FIRST_PULL_REQUEST = "352"
SECOND_PULL_REQUEST = "353"

RUN_TIMEOUT_SECONDS = 180.0
CHAIN_TIMEOUT_SECONDS = 120.0

# ---------------------------------------------------------------------------------------------
# The stubs. Everything a test asserts is recorded by one of these WHILE its run is live: the
# worktree is gone by the time the driver returns, and the environment a call was made with is
# gone with the process that made it.
# ---------------------------------------------------------------------------------------------

BACKEND_STUB = r"""#!/usr/bin/env bash
# Stands in for the claude CLI: records what this run was handed, and exits. Two of the lines are
# derived rather than copied, because the variables they come from are kilobytes of prompt that no
# assertion should ever print -- the instruction's first line, which names the pull request this
# run believes it is reviewing, and every worktree path the rules name, which is the tree this run
# was TOLD to work in.
{
  env | grep -E '^AGENT_RUN_(ROLE|SUBJECT|CONTEXT|DIR|LOGFILE|PIDFILE|WORKTREE)=' | sort
  printf 'PYTHONPATH=%s\n' "${PYTHONPATH-}"
  printf 'WORKTREE_HEAD=%s\n' "$(git -C "${PYTHONPATH-}" rev-parse HEAD 2>/dev/null || echo none)"
  printf 'INSTRUCTION_FIRST_LINE=%s\n' "${AGENT_RUN_INSTRUCTION%%$'\n'*}"
  printf 'RULES_WORKTREES=%s\n' \
    "$(printf '%s' "${AGENT_RUN_RULES-}" \
       | grep -oE 'worktree-pr[0-9]+-[0-9]{8}T[0-9]{6}Z-[0-9]+' | sort -u | paste -sd, -)"
} > "$STUB_RECORD"
exit 0
"""

GIT_STUB = r"""#!/usr/bin/env python3
# Stands in for `git` on the two calls that would reach origin -- `ls-remote` answers with the
# head written in a FILE, which is what lets the branch move between the two launches, and `fetch`
# succeeds without fetching -- and hands every other subcommand to the real git.
import os
import subprocess
import sys

arguments = sys.argv[1:]
if "ls-remote" in arguments or "fetch" in arguments:
    with open(os.environ["GIT_STUB_CALLS"], "a") as calls:
        calls.write(" ".join(arguments) + "\n")
    if "ls-remote" in arguments:
        with open(os.environ["GIT_STUB_PR_HEAD_FILE"]) as head:
            print(head.read().strip() + "\trefs/pull/1/head")
    sys.exit(0)
sys.exit(subprocess.call([os.environ["REAL_GIT"], *arguments]))
"""

GUARD_STUB = r'''#!/usr/bin/env python3
"""Stands in for `agent_os.guard` at the two calls a finishing run makes: `event`, which
here is only recorded, and `wake`, which is the thing under test. It records the environment each
was called with -- that record is the whole of the first assertion below -- and on the FIRST wake
it plays the planner to the extent the defect needs: it starts a role from inside its own
environment, exactly as the planner `wake` really starts inherits it and launches from it.

It writes no event and starts no planner, so a test that reaches this file spends nothing and
cannot wake anything real."""
import json
import os
import subprocess
import sys
import time

arguments = sys.argv[1:]
call = {
    "command": arguments[0] if arguments else "",
    "run_variables": sorted(name for name in os.environ if name.startswith("AGENT_RUN_")),
    "detached": os.environ.get("AGENT_DETACHED_RUN"),
    "pythonpath": os.environ.get("PYTHONPATH"),
}
with open(os.environ["GUARD_CALL_RECORD"], "a") as record:
    record.write(json.dumps(call) + "\n")

marker = os.environ["GUARD_PLANNER_MARKER"]
if arguments[:1] == ["wake"] and not os.path.exists(marker):
    with open(marker, "w") as launched:
        launched.write("the guard stub launched the second role\n")
    # Past the next whole second, so the two runs' own stamps -- `date -u +%Y%m%dT%H%M%SZ`, which
    # names the log, the PID file and the runs.tsv row -- cannot collide. Two roles launched inside
    # one second sharing one log name is a defect of its own; it is not this issue's, and this test
    # must not fail on it.
    time.sleep(1.1)
    # The branch moves while the first review is running: two corrections landed on PR #471 between
    # the validator's cut and its sign-off, and the second review never saw them. The head the
    # second role resolves is this one, and only a launch of its own can find it.
    with open(os.environ["GIT_STUB_PR_HEAD_FILE"], "w") as head:
        head.write(os.environ["GUARD_SECOND_HEAD"] + "\n")
    environment = dict(os.environ)
    environment["STUB_RECORD"] = os.environ["GUARD_SECOND_RECORD"]
    launched_second = subprocess.run(
        ["bash", os.environ["GUARD_DRIVER"], os.environ["GUARD_SECOND_ROLE"],
         os.environ["GUARD_SECOND_SUBJECT"]],
        cwd=os.environ["GUARD_MAIN"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    with open(os.environ["GUARD_SECOND_STDOUT"], "w") as standard_output:
        standard_output.write(launched_second.stdout + launched_second.stderr)
sys.exit(0)
'''


PYTHON_DISPATCHER_STUB = """#!/usr/bin/env bash
# Stands in for the mechanism's own interpreter at ONE call: `-m agent_os.guard`, which goes to
# the guard stub above instead. Every other call -- `-m agent_os.lib ...`, the `-c` that prints
# the runs.tsv header -- goes to the real interpreter untouched, because those are the driver
# resolving its own configuration and this file has nothing to say about them.
if [ "${1-}" = -m ] && [ "${2-}" = agent_os.guard ]; then
  shift 2
  exec "$REAL_AGENT_OS_PYTHON" "$GUARD_STUB" "$@"
fi
exec "$REAL_AGENT_OS_PYTHON" "$@"
"""


def _config_without_secrets(directory: pathlib.Path) -> pathlib.Path:
    """`config.example.yaml` with `project.secrets_dir` pointed at nothing, so
    `agent_apply_identity` degrades to its warning instead of minting a GitHub App token -- the one
    step of the launch path that would otherwise reach the network. The same patch
    `test_agent_task.py` applies to its own launch tests."""
    text = EXAMPLE_CONFIG.read_text()
    patched, count = re.subn(
        r"^  secrets_dir: .*$",
        "  secrets_dir: .secrets/no-such-app",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "config.example.yaml's project.secrets_dir line changed shape"
    path = directory / "agents-no-secrets.yaml"
    path.write_text(patched)
    return path


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit_with_no_history(tree: str, parent: str | None) -> str:
    """A commit object holding `tree`, written straight into the object database: no ref, no
    working tree, no branch. Two of these are the chain's two heads, and they are commits rather
    than `HEAD` and `HEAD~1` because CI checks this repository out one commit deep, where a second
    revision to point at does not exist."""
    arguments = [
        "git",
        "-C",
        str(ROOT),
        "-c",
        "user.name=agent-os-tests",
        "-c",
        "user.email=tests@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        tree,
    ]
    if parent:
        arguments += ["-p", parent]
    completed = subprocess.run(
        [*arguments, "-m", "a head for agent_os/tests/test_role_run_environment_isolation.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _build_stand_in_main(directory: pathlib.Path) -> pathlib.Path:
    """The disposable HOST checkout the chain's two drivers run out of.

    The driver must be reachable at a path of this tree's own, because everything the chain
    measures is what ONE run hands the next and the only way to see it without touching the
    checkout under test is to stand in for the pieces around it. Since #508 that is
    `agent_os/bin/`: the driver plus the resolver it sources, symlinked in, beside the host's
    `config/` -- and `$AGENT_OS_HOST_ROOT` (below) names this directory as the host root rather
    than leaving it to be derived."""
    main = directory / "main"
    (main / "agent_os" / "bin").mkdir(parents=True)
    # A `.git` FILE naming this repository's gitdir, which is what a linked worktree carries and
    # what git accepts in place of the directory a plain clone has: `git -C <main> worktree add`
    # then works the same in this worktree and in CI's checkout.
    (main / ".git").write_text(f"gitdir: {_git('rev-parse', '--absolute-git-dir')}\n")
    # A copy of `config.example.yaml`, not a symlink to a host's real `config/` (#512): nothing
    # this chain measures depends on a project's own values, and a copy keeps the disposable tree
    # self-contained -- `AGENT_OS_HOST_ROOT=main` below is what points both launched roles at it,
    # whether or not a subprocess inherits this session's own `AGENTS_CONFIG_PATH`.
    (main / "config").mkdir()
    (main / "config" / "agents.yaml").write_text(EXAMPLE_CONFIG.read_text())
    for name in ("agent_task.sh", "_python.sh"):
        (main / "agent_os" / "bin" / name).symlink_to(AGENT_OS_DIR / "bin" / name)
    return main


def _parse_record(path: pathlib.Path) -> dict[str, str]:
    """One `KEY=VALUE` per line, as the backend stub writes it. A record that was never written is
    an empty dict, and the test that reads it says so with the stdout it does have."""
    if not path.is_file():
        return {}
    entries = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            entries[key] = value
    return entries


def _detached_pid(stdout: str) -> int | None:
    """The PID the driver printed for the run it detached, or None when it never got that far --
    which is itself one of the things this file measures."""
    match = re.search(r"^detached:\s+pid (\d+)", stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def _run_is_alive(pid: int) -> bool:
    """Dead means gone, and a zombie counts as gone: a detached run is no child of this process, so
    nothing here reaps it, and a corpse nobody has buried yet must not read as a live run."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(") ", 1)[1].split()[0] != "Z"


def _wait_until(condition, what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {timeout}s")
        time.sleep(0.05)


def _wait_for_run_to_end(pid: int, timeout: float = RUN_TIMEOUT_SECONDS) -> None:
    _wait_until(lambda: not _run_is_alive(pid), f"the detached run (pid {pid}) to end", timeout)


class _Chain:
    """The two roles and everything they left behind, measured once and read by every test here."""

    def __init__(
        self,
        *,
        run_directory: pathlib.Path,
        guard_calls: list[dict[str, object]],
        first: dict[str, str],
        second: dict[str, str],
        first_stdout: str,
        second_stdout: str,
        first_head: str,
        second_head: str,
    ) -> None:
        self.run_directory = run_directory
        self.guard_calls = guard_calls
        self.first = first
        self.second = second
        self.first_stdout = first_stdout
        self.second_stdout = second_stdout
        self.first_head = first_head
        self.second_head = second_head

    def role_run(self, which: str, record: dict[str, str], stdout: str) -> dict[str, str]:
        assert record, f"{which} never reached the backend; its driver said:\n{stdout}"
        return record

    @property
    def wakes(self) -> list[dict[str, object]]:
        return [call for call in self.guard_calls if call["command"] == "wake"]

    def rows(self) -> list[dict[str, str]]:
        """The role's `runs.tsv`, one dict per run, in the order the runs appended them."""
        table = self.run_directory / "runs.tsv"
        assert table.is_file(), f"no runs.tsv at {table}"
        lines = table.read_text().splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line.strip()]

    def logs(self) -> list[pathlib.Path]:
        return sorted(self.run_directory.glob("*.log"))


@pytest.fixture(scope="module")
def woken_role_chain(tmp_path_factory) -> Iterator[_Chain]:
    """Runs the chain once -- role A, A's own wake, role B launched from inside that wake -- and
    hands every test what it recorded."""
    base = tmp_path_factory.mktemp("role-run-environment")
    main = _build_stand_in_main(base)
    cache = main / ".cache"
    cache.mkdir()
    binaries = base / "bin"
    binaries.mkdir()
    for name, text in (("backend", BACKEND_STUB), ("git", GIT_STUB)):
        stub = binaries / name
        stub.write_text(text)
        stub.chmod(0o755)
    guard = base / "agent_guard_stub.py"
    guard.write_text(GUARD_STUB)
    guard.chmod(0o755)
    # Since #508 the driver reaches the guard as `"$AGENT_OS_PYTHON" -m agent_os.guard`, not as a
    # path under the checkout it runs from, so standing in for the guard means standing in for
    # that ONE call of that interpreter and passing every other one through
    # (`-m agent_os.lib ...`, the `-c` that prints the runs.tsv header). `sys.executable` is the
    # mechanism's own interpreter here, because that is what runs this suite.
    interpreter = base / "agent-os-python"
    interpreter.write_text(PYTHON_DISPATCHER_STUB)
    interpreter.chmod(0o755)

    # The two heads: the branch A cuts its worktree at, and the one it has moved to by the time B
    # is launched. Written straight into the object database, so nothing here has a ref, a branch
    # or a working tree of its own.
    tree = _git("rev-parse", "HEAD^{tree}")
    first_head = _commit_with_no_history(tree, None)
    second_head = _commit_with_no_history(tree, first_head)
    assert first_head != second_head, "the two heads came out the same"
    head_file = base / "pull-request-head.txt"
    head_file.write_text(first_head + "\n")

    real_git = shutil.which("git")
    assert real_git, "no git on PATH to stand in for"
    environment = dict(os.environ)
    # Nothing ambient may stand in for what the driver itself exports: an AGENT_RUN_* or an
    # AGENT_DETACHED_RUN already in this process's environment would make the chain's first launch
    # behave like its second, and the test would measure the wrong thing.
    for name in list(environment):
        if name.startswith("AGENT_RUN_") or name in ("AGENT_DETACHED_RUN", "AGENT_CACHE_DIR"):
            del environment[name]
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        REAL_GIT=real_git,
        GIT_STUB_CALLS=str(base / "git-calls.txt"),
        GIT_STUB_PR_HEAD_FILE=str(head_file),
        # The launch gate reads the guard's quota verdict from here, and so does `wake`'s lock:
        # one disposable value keeps both out of the real `.cache`.
        WORKER_CACHE_DIR=str(cache),
        # The two the resolver answers with before it derives anything (#508): the stand-in
        # checkout is the host root, and the interpreter is the dispatcher that owns the guard
        # call. `agent_task.sh` re-exports both, so the detached re-entry and the second launch
        # the guard stub makes are handed the same two answers.
        AGENT_OS_HOST_ROOT=str(main),
        AGENT_OS_PYTHON=str(interpreter),
        REAL_AGENT_OS_PYTHON=sys.executable,
        GUARD_STUB=str(guard),
        AGENT_CLAUDE_BIN=str(binaries / "backend"),
        AGENTS_CONFIG_PATH=str(_config_without_secrets(base)),
        STUB_RECORD=str(base / "backend-first.txt"),
        GUARD_CALL_RECORD=str(base / "guard-calls.jsonl"),
        GUARD_PLANNER_MARKER=str(base / "planner-launched.txt"),
        GUARD_DRIVER=str(main / "agent_os" / "bin" / "agent_task.sh"),
        GUARD_MAIN=str(main),
        GUARD_SECOND_ROLE=ROLE,
        GUARD_SECOND_SUBJECT=SECOND_PULL_REQUEST,
        GUARD_SECOND_RECORD=str(base / "backend-second.txt"),
        GUARD_SECOND_STDOUT=str(base / "second-stdout.txt"),
        GUARD_SECOND_HEAD=second_head,
        # A sentinel rather than nothing: what the chain has to show is that the driver REPLACED
        # the PYTHONPATH it inherited with the worktree's own, so an inherited value that already
        # pointed at a checkout of this repository would let a driver exporting nothing pass.
        PYTHONPATH=str(base / "inherited-sentinel"),
    )

    # `wake` is the one door to a planner run and no test may open it. The guard this chain calls
    # is a stub that starts nothing, so this is the second lock on a door that is already shut:
    # were the chain ever to reach the real guard, it would find the lock held and stop, and the
    # tests below would fail on a missing record instead of spending money.
    lock_path = cache / "planner.lock"
    held = lock_path.open("a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run_directory = cache / ROLE
    try:
        launched = subprocess.run(
            ["bash", str(main / "agent_os" / "bin" / "agent_task.sh"), ROLE, FIRST_PULL_REQUEST],
            cwd=main,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        pid = _detached_pid(launched.stdout)
        assert pid is not None, f"the driver printed no detached PID:\n{launched.stdout}"
        # A's own end comes AFTER the wake it makes, and the wake is what launches B, so waiting
        # for A is what makes B's driver output readable; B's own detached run is waited for
        # separately, because a B that never detached has no PID to wait for.
        _wait_for_run_to_end(pid)
        second_stdout_path = pathlib.Path(environment["GUARD_SECOND_STDOUT"])
        _wait_until(
            second_stdout_path.is_file,
            "the wake to launch the second role",
            CHAIN_TIMEOUT_SECONDS,
        )
        second_stdout = second_stdout_path.read_text()
        second_pid = _detached_pid(second_stdout)
        if second_pid is not None:
            _wait_for_run_to_end(second_pid)

        guard_calls = [
            json.loads(line)
            for line in pathlib.Path(environment["GUARD_CALL_RECORD"]).read_text().splitlines()
            if line.strip()
        ]
        yield _Chain(
            run_directory=run_directory,
            guard_calls=guard_calls,
            first=_parse_record(pathlib.Path(environment["STUB_RECORD"])),
            second=_parse_record(second_stdout_path.with_name("backend-second.txt")),
            first_stdout=launched.stdout,
            second_stdout=second_stdout,
            first_head=first_head,
            second_head=second_head,
        )
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
        # A chain that failed on its way must not leave the shared repository with a registration
        # behind: `git worktree add` writes one under the gitdir this worktree shares with every
        # other checkout of the project.
        for leftover in cache.glob("*/worktree-*"):
            subprocess.run(
                ["git", "-C", str(ROOT), "worktree", "remove", "--force", str(leftover)],
                capture_output=True,
                check=False,
            )
        subprocess.run(
            ["git", "-C", str(ROOT), "worktree", "prune"], capture_output=True, check=False
        )


def test_the_wake_a_finishing_role_makes_carries_no_run_environment(woken_role_chain: _Chain):
    """`agent_guard.py wake` is the run's own last step and it is called from inside the
    environment the driver exported for that run, so every variable it carries is one the planner
    it starts inherits -- and one the next role that planner launches inherits too (#475)."""
    wakes = woken_role_chain.wakes
    assert wakes, "the chain never reached a wake call: {}".format(
        [call["command"] for call in woken_role_chain.guard_calls]
    )

    leaked = sorted({name for call in wakes for name in call["run_variables"]})
    assert leaked == [], f"the wake carried the run's own AGENT_RUN_* block: {leaked}"

    # Not the run-scoped worktree: `PYTHONPATH` may survive a scrub as whatever the driver
    # inherited, but as the worktree it is the tree the finished run was reviewing, and a role
    # launched from it resolves its imports there.
    run_scoped = [
        path
        for path in (call["pythonpath"] for call in wakes)
        if isinstance(path, str) and "worktree-pr" in path
    ]
    assert run_scoped == [], f"the wake carried the run-scoped PYTHONPATH: {run_scoped}"

    # `AGENT_DETACHED_RUN=yes` is one of the two halves of the signature that makes the next
    # driver skip its own arguments entirely and re-enter the detached half, the other being an
    # invocation that carries none (`agent_os/bin/agent_task.sh`) -- together they are how a second
    # role came to run a first role's brief. The wake must carry neither half: anything but "yes"
    # leaves the driver free to parse the invocation it was actually given.
    detached = [call["detached"] for call in wakes]
    assert "yes" not in detached, "the wake carried AGENT_DETACHED_RUN=yes"


def test_a_role_launched_from_another_roles_wake_runs_its_own_brief(woken_role_chain: _Chain):
    """The second role resolves its instruction, its subject, its own log and its own PID file
    from its own invocation, never from the environment the wake handed the planner."""
    first = woken_role_chain.role_run(
        "the role that made the wake", woken_role_chain.first, woken_role_chain.first_stdout
    )
    second = woken_role_chain.role_run(
        "the role the woken planner launched",
        woken_role_chain.second,
        woken_role_chain.second_stdout,
    )
    assert first["AGENT_RUN_SUBJECT"] == FIRST_PULL_REQUEST

    assert second["AGENT_RUN_ROLE"] == ROLE
    assert second["AGENT_RUN_SUBJECT"] == SECOND_PULL_REQUEST
    assert second["AGENT_RUN_CONTEXT"].split() == [ROLE, f"#{SECOND_PULL_REQUEST}"]
    assert f"#{SECOND_PULL_REQUEST}" in second["INSTRUCTION_FIRST_LINE"]
    assert f"#{FIRST_PULL_REQUEST}" not in second["INSTRUCTION_FIRST_LINE"]

    # Its own log and its own PID file: the pair the guard reads to tell a run that ended from one
    # that died. A second role that writes into the first one's log leaves a record where two runs
    # are indistinguishable from one run that was booked twice.
    assert second["AGENT_RUN_LOGFILE"] != first["AGENT_RUN_LOGFILE"]
    assert pathlib.Path(second["AGENT_RUN_LOGFILE"]).is_file()
    assert second["AGENT_RUN_PIDFILE"] != first["AGENT_RUN_PIDFILE"]
    # Both roles are the same role, so both runs.tsv live in the same directory and this one value
    # cannot tell an inherited `AGENT_RUN_DIR` from a resolved one; it is asserted because the
    # criterion names it, and the four above are what actually discriminates.
    assert second["AGENT_RUN_DIR"] == str(woken_role_chain.run_directory)


def test_a_role_launched_from_another_roles_wake_cuts_its_own_worktree(
    woken_role_chain: _Chain,
):
    """The second role cuts its own worktree at the head its own launch resolves: not the earlier
    run's path, and not the earlier run's commit. This is the criterion the record cannot show
    afterwards -- a review that read a frozen copy reads exactly like one that read the head."""
    first = woken_role_chain.role_run(
        "the role that made the wake", woken_role_chain.first, woken_role_chain.first_stdout
    )
    second = woken_role_chain.role_run(
        "the role the woken planner launched",
        woken_role_chain.second,
        woken_role_chain.second_stdout,
    )
    assert first["WORKTREE_HEAD"] == woken_role_chain.first_head
    assert f"worktree-pr{FIRST_PULL_REQUEST}-" in first["PYTHONPATH"]

    assert second["WORKTREE_HEAD"] == woken_role_chain.second_head
    assert second["PYTHONPATH"] != first["PYTHONPATH"]
    assert f"worktree-pr{SECOND_PULL_REQUEST}-" in second["PYTHONPATH"]
    # The worktree the run was handed and the one its rules name are the same tree: the `__WORKTREE__`
    # placeholder is substituted with the path the driver prepared, so a role told to work in one
    # tree while running in another is a defect the rules themselves would hide.
    assert second["AGENT_RUN_WORKTREE"] == second["PYTHONPATH"]
    assert f"worktree-pr{SECOND_PULL_REQUEST}-" in second["RULES_WORKTREES"]


def test_two_roles_launched_in_sequence_leave_one_row_each(woken_role_chain: _Chain):
    """One row per launched role in that role's `runs.tsv`, each naming its own log: the spend
    record is how a chain like the one this defect was caught in is costed, and a role that books
    itself to the run that woke it costs nothing anyone can see."""
    first = woken_role_chain.role_run(
        "the role that made the wake", woken_role_chain.first, woken_role_chain.first_stdout
    )
    second = woken_role_chain.role_run(
        "the role the woken planner launched",
        woken_role_chain.second,
        woken_role_chain.second_stdout,
    )

    rows = woken_role_chain.rows()
    assert [row["context"].strip() for row in rows] == [
        f"{ROLE} #{FIRST_PULL_REQUEST}",
        f"{ROLE} #{SECOND_PULL_REQUEST}",
    ]

    logs = woken_role_chain.logs()
    assert len(logs) == 2, f"the two runs left {[path.name for path in logs]}"
    assert {path.name for path in logs} == {
        pathlib.Path(first["AGENT_RUN_LOGFILE"]).name,
        pathlib.Path(second["AGENT_RUN_LOGFILE"]).name,
    }
