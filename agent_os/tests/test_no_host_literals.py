"""`agent_os/` carries no literal of any host project (agent_os/docs/AGENT_OS.md's own agnosticism claim,
`agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md`).

A blunt substring walk over every file git knows under the repository root -- tracked, plus
untracked-but-not-ignored (#53: a filesystem walk also read nested agent worktrees and caches no
host ever receives) -- excluding `docs/` (history and the
module doc are allowed to name the projects they were written about), `tests/golden/` (fixture
data captured from a real run, not code) and `config.example.yaml` (its own commentary explains
the shape of a real value by naming one, `agent_os/docs/AGENT_OS.md` §4.1). Two further, narrower
exclusions:

- `SELF_REFERENTIAL_CHECKS` -- files that spell every forbidden literal ON PURPOSE, as the very
  strings their own test checks a real file or a rendered prompt against; a walk that flagged a
  literal-checking test for containing the literals it checks for would be checking nothing.
  Excluded by identity, never by `EXCLUDED_PATHS`, which is reserved for a file that still needs
  its own literal removed. Three files, each for its own reason: this file and
  `tests/test_install_templates.py` (#511) check a real shipped file against the tuple
  (`test_the_real_canonical_issue_templates_carry_no_host_literal` there); `test_agent_task.py`'s
  `HOST_LITERALS` tuple is the inverse of the same idea -- it asserts each literal is ABSENT from
  every role's rendered prompt, so the tuple has to spell "roedor"/"5435" to say so (#512's own
  fix moved it here from `EXCLUDED_PATHS` once that was noticed).
- `EXCLUDED_PATHS` -- files #510's own audit found that still fail this walk and belong to a
  sibling wave-2 issue, #512, running in parallel; the driver-side half of the same audit, #509,
  is already clean, and #511's own new modules (`install.py`, `doctor.py`, `render.py`,
  `templates/`) were scrubbed before merge, so this walk needed no exclusion for either:

  - `agent_os/tests/test_prompt_templates.py` -- its golden comparison keeps a static, documented
    copy of the host's own two `config/agent_prompts/*.md` extension-point files (`agent_os/tests/
    golden/*.md` fixture data captured from a real run, the same exception `agent_os/docs/AGENT_OS.md`
    already grants `tests/golden/`), so the literal is load-bearing fixture content, not
    incidental prose -- kept per this walk's own instructions rather than rewritten into
    something that no longer proves the golden text matches what a real host renders.

  Every other file #510's audit named -- `test_agent_guard.py`, `test_agent_lib.py`,
  `test_issues_cli.py`, `test_role_run_environment_isolation.py`, `test_worker_task.py` -- had
  #512's own fix rephrase or rename its one literal (all were incidental: example worktree names,
  a made-up forbidden-path pattern, explanatory prose), and `test_agent_task.py` moved to
  `SELF_REFERENTIAL_CHECKS` above instead, because its literal is the very thing under test.
  `test_every_exclusion_still_applies` below is what would catch a stale entry among these.

Pure filesystem. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

from agent_os.cli import AGENT_OS_DIR

FORBIDDEN = ("roedor", "MatillaM", "titanarq", "5435", "roedor_ro")

# Paths relative to `agent_os/` that spell every forbidden literal ON PURPOSE, as the strings
# their own test checks a real file against -- see the module docstring above.
SELF_REFERENTIAL_CHECKS = {
    "tests/test_no_host_literals.py",
    "tests/test_install_templates.py",
    "tests/test_agent_task.py",
}

# Paths relative to `agent_os/`, owned by #512 and not fixed here -- see the module docstring
# above. `test_every_exclusion_still_applies` below keeps this list honest: an entry the walk no
# longer needs fails that test instead of quietly shrinking what the walk actually checks.
EXCLUDED_PATHS = {
    "tests/test_prompt_templates.py",
}


def _is_excluded_by_location(relative_parts: tuple[str, ...]) -> bool:
    if relative_parts[0] == "docs":
        return True
    if "tests" in relative_parts and "golden" in relative_parts:
        return True
    return relative_parts[-1] == "config.example.yaml"


def _repository_files(root):
    """The files git knows under `root`: tracked, plus untracked-but-not-ignored so a new file
    is checked before it is ever `git add`-ed. What a host receives is what git tracks; a
    filesystem walk would also read ignored caches and nested agent worktrees (#53), whose own
    `docs/` and `tests/` escape every root-anchored exclusion. No fallback to a walk: without
    git the premise of the test is gone and it says so."""
    import subprocess

    if not (root / ".git").exists():
        raise RuntimeError(f"no .git under {root}: cannot ask git which files the mechanism ships")
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        raise RuntimeError(f"git ls-files failed under {root}: {listing.stderr.strip()}")
    # A nested repository or worktree is listed as one `dir/` entry, never its contents; a
    # tracked file deleted from the working tree is listed but no longer exists. Neither is a
    # file to read.
    entries = listing.stdout.split("\0")
    return sorted({entry for entry in entries if entry and not entry.endswith("/")})


def _files(root=AGENT_OS_DIR):
    """Every file git knows under `root` not excluded by its LOCATION -- `EXCLUDED_PATHS` is
    checked separately by each test below, not folded in here, so the second test can still see
    the files the first one skips."""
    for posix in _repository_files(root):
        path = root / posix
        if not path.is_file():
            continue
        if _is_excluded_by_location(path.relative_to(root).parts):
            continue
        if posix in SELF_REFERENTIAL_CHECKS:
            continue
        yield path, posix


def _literals_in(path):
    try:
        text = path.read_text(errors="strict")
    except (UnicodeDecodeError, OSError):
        return []
    return [literal for literal in FORBIDDEN if literal in text]


def test_no_host_literal_anywhere_under_agent_os():
    violations = [
        f"{relative}: {found}"
        for path, relative in _files()
        if relative not in EXCLUDED_PATHS
        for found in _literals_in(path)
    ]
    assert not violations, "host literal(s) found:\n" + "\n".join(violations)


def test_every_exclusion_still_applies():
    # An exclusion this list carries but the walk no longer needs is a silent regression: the
    # sibling issue's own fix landed, the entry was not removed with it, and the walk has quietly
    # been checking less than `EXCLUDED_PATHS` claims ever since.
    by_relative = {relative: path for path, relative in _files()}
    stale = [
        relative
        for relative in EXCLUDED_PATHS
        if relative in by_relative and not _literals_in(by_relative[relative])
    ]
    assert not stale, f"exclusion(s) no longer needed, remove from EXCLUDED_PATHS: {stale}"
    missing = [relative for relative in EXCLUDED_PATHS if relative not in by_relative]
    assert not missing, f"exclusion(s) naming a file that no longer exists: {missing}"


def _git(root, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _repository_with_a_literal_in(tmp_path, *relative_paths, ignore=""):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(ignore)
    (tmp_path / "clean.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    for relative in relative_paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"name = '{FORBIDDEN[0]}'\n")
    return tmp_path


def test_an_ignored_untracked_path_is_never_read(tmp_path):
    root = _repository_with_a_literal_in(tmp_path, ".cache/leak.txt", ignore=".cache/\n")
    # A nested agent worktree, not ignored here on purpose: git lists it as one `dir/` entry and
    # never its contents, whatever `.gitignore` says.
    _git(root, "worktree", "add", "-q", ".claude/worktrees/x")
    (root / ".claude/worktrees/x/leak.py").write_text(f"name = '{FORBIDDEN[0]}'\n")
    assert [relative for _path, relative in _files(root)] == [".gitignore", "clean.py"]


def test_an_untracked_file_that_is_not_ignored_is_still_scanned(tmp_path):
    root = _repository_with_a_literal_in(tmp_path, "agent_os/stray.py")
    assert "agent_os/stray.py" in [relative for _path, relative in _files(root)]


def test_a_tracked_file_is_scanned(tmp_path):
    root = _repository_with_a_literal_in(tmp_path, "agent_os/tracked.py")
    _git(root, "add", "agent_os/tracked.py")
    assert "agent_os/tracked.py" in [relative for _path, relative in _files(root)]


def test_a_root_without_git_fails_loudly(tmp_path):
    import pytest

    with pytest.raises(RuntimeError, match="git"):
        list(_files(tmp_path))
