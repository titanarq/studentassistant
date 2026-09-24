"""`agent_os/` carries no literal of any host project (agent_os/docs/AGENT_OS.md's own agnosticism claim,
`agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md`).

A blunt substring walk over every file under `agent_os/`, excluding `docs/` (history and the
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
    # `.git` sits under `AGENT_OS_DIR` only once the mechanism is its own repository's root, and
    # carries that repository's own URL: git's metadata, not a file of the mechanism.
    if relative_parts[0] in (".git", ".venv", "__pycache__"):
        return True
    if relative_parts[0] == "docs":
        return True
    if "tests" in relative_parts and "golden" in relative_parts:
        return True
    return relative_parts[-1] == "config.example.yaml"


def _files():
    """Every real file under `agent_os/` not excluded by its LOCATION -- `EXCLUDED_PATHS` is
    checked separately by each test below, not folded in here, so the second test can still see
    the files the first one skips."""
    for path in sorted(AGENT_OS_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(AGENT_OS_DIR)
        posix = relative.as_posix()
        if _is_excluded_by_location(relative.parts) or posix in SELF_REFERENTIAL_CHECKS:
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


def test_git_metadata_is_excluded_by_location():
    assert _is_excluded_by_location((".git", "config"))
