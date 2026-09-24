"""`agent_os/prompts/` and `agent_os.lib.render_prompt` -- where every role's prompt lives now, and
the proof that moving it out of the drivers' heredocs changed nothing an agent reads.

The golden test below renders all four prompts through `config.example.yaml`, plus the two
paragraphs roedor's own `config/agent_prompts/` supplies -- COPIED in below rather than read live
from this checkout, the same way `agent_os/tests/golden/*.md` itself is allowed to carry a host's
literal text (#509, #510, #512): both are snapshots of one host's rendering, not text a second host
reads through this suite. `AGENT_OS_HOST_ROOT` points every launched driver at a throwaway
directory carrying only those two files and a `.git` of its own (`worker_task.sh`'s
`main_checkout()` needs a real repository to name, not just a config to read), so the capture is
exactly as reproducible outside this checkout as everywhere else in this suite (#512 -- this was
the one test left reading a host's real `config/agents.yaml`, through `capture_golden.sh`'s bare
invocation).

Pure filesystem and subprocess: nothing here launches a backend, mints an identity or touches a
database.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
from conftest import EXAMPLE_CONFIG

from agent_os.cli import AGENT_OS_DIR
from agent_os.lib import (
    PROJECT_EXTRAS_PLACEHOLDER,
    PROMPT_ROLES,
    PROMPTS_DIR,
    render_prompt,
)

CAPTURE = AGENT_OS_DIR / "tests" / "capture_golden.sh"
GOLDEN_DIR = AGENT_OS_DIR / "tests" / "golden"

# roedor's own two extension-point paragraphs, copied as they read today (`config/agent_prompts/
# worker.md` and `refiner.md`) -- a snapshot, not a live read, so a second checkout with no such
# files still captures the same golden text. Re-copy by hand if roedor ever edits either one and
# re-runs `capture_golden.sh` for real.
ROEDOR_WORKER_EXTRAS = """RUNNING PYTHON AND TESTS
- PYTHONPATH is already exported to your worktree and every test run needs it. `cd` alone is NOT
  enough: pytest resolves `roedor` through the editable install in the main checkout, so without
  the variable you are testing the other agent's code, not yours. That has already voided a full
  suite run. If you launch a shell that drops the variable, put it back.
- Use `bash __TEST_COMMAND__ <paths> -q` for anything that touches the database. A bare `pytest`
  against it stops at once with a message pointing back here rather than failing mid-migration --
  `__TEST_COMMAND__` is what reaches the owner role for the run. Before any test run, check no
  other pytest is running: `ps -eo pid,cmd | grep [p]ytest`. Never two at once -- the database is
  shared and db_sandbox writes to it for real.
"""

ROEDOR_REFINER_EXTRAS = """   EVERY worker task goes to a Qwen class: `mechanical-qwen` when the change is
   small and fully specified, `complex-qwen` in every other case. Never give a worker task a class
   whose `backend:` is `claude`
   (agent_os/docs/adr/2026-09-16-workers-run-on-qwen-and-claude-only-reviews.md).
"""


def _words(text: str) -> list[str]:
    """The prompt with every run of whitespace reduced to one break: what the golden comparison is
    about is the words an agent reads and their order, never where a rendered paragraph happened to
    be re-wrapped or which line a substituted block landed on."""
    return text.split()


@pytest.fixture(scope="module")
def captured(tmp_path_factory) -> pathlib.Path:
    """Every role's prompt as its own driver resolves it today, rendered against a throwaway host
    of this test's own -- `config.example.yaml` plus roedor's two extension-point files -- so the
    capture depends on nothing outside `agent_os/` (#512)."""
    host = tmp_path_factory.mktemp("golden-host")
    extras_dir = host / "config" / "agent_prompts"
    extras_dir.mkdir(parents=True)
    (extras_dir / "worker.md").write_text(ROEDOR_WORKER_EXTRAS)
    (extras_dir / "refiner.md").write_text(ROEDOR_REFINER_EXTRAS)
    config = host / "config" / "agents.yaml"
    config.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            "  prompt_extras: {}\n",
            "  prompt_extras:\n"
            "    worker: config/agent_prompts/worker.md\n"
            "    refiner: config/agent_prompts/refiner.md\n",
            1,
        )
    )
    # A real checkout for `worker_task.sh`'s `main_checkout()` to name: without a `.git` here, the
    # rendered worker RULES would fail to render at all (#512 follow-up -- `main_checkout()` used
    # to answer a silent "." instead, which is what golden/worker.md used to certify). Same recipe
    # the out-of-tree CI step uses to make a bare copy a real repository.
    subprocess.run(["git", "init", "-q"], cwd=host, check=True)
    subprocess.run(["git", "add", "-A"], cwd=host, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=host,
        check=True,
    )

    out = tmp_path_factory.mktemp("captured-prompts")
    result = subprocess.run(
        ["bash", str(CAPTURE), str(out)],
        env={
            **os.environ,
            "AGENT_OS_HOST_ROOT": str(host),
            "AGENTS_CONFIG_PATH": str(config),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


@pytest.mark.parametrize("role", PROMPT_ROLES)
def test_the_role_renders_exactly_what_it_rendered_before_the_templates_existed(role, captured):
    """The whole point of #509: the prompt text moved out of three shell heredocs into four files
    and an extension point the host fills, and what reaches a backend is word for word what reached
    it before. A difference here is either a wording change nobody asked for or a placeholder that
    stopped being substituted."""
    rendered = (captured / f"{role}.md").read_text()
    golden = (GOLDEN_DIR / f"{role}.md").read_text()
    assert _words(rendered) == _words(golden)


@pytest.mark.parametrize("role", PROMPT_ROLES)
def test_every_template_carries_the_hosts_extension_point(role):
    """One marked place per role where a host's own paragraphs go. Without it a host has nowhere to
    put a sentence only it can write, and the sentence goes back into the mechanism's own text --
    which is the defect `agent_os/docs/AGENT_OS.md` §7 row (t) recorded."""
    assert PROJECT_EXTRAS_PLACEHOLDER in (PROMPTS_DIR / f"{role}.md").read_text()


def test_a_role_whose_host_names_no_extras_renders_no_gap_where_they_would_go():
    """An empty extension point takes its own line with it, and one of the two blank lines that
    fenced it -- never both, and never a heading with nothing under it. The same rule every other
    paragraph rendered from config has followed since #363."""
    rendered = render_prompt(
        "worker",
        {
            "HUMAN_LOGIN": "someone",
            "HUMAN_MESSAGE_RULES": "WRITING TO THE HUMAN\nin their language.",
            "TEST_COMMAND": "make test",
            "MAIN_CHECKOUT": "/somewhere",
            "FORBIDDEN_PATHS_RULES": "",
            "MECHANISM_PATHS_RULES": "",
            "NEVER_RUN_RULES": "",
            "WORKER_ENVIRONMENT_RULES": "",
        },
    )
    assert PROJECT_EXTRAS_PLACEHOLDER not in rendered
    assert "\n\n\n" not in rendered


def test_a_hosts_own_paragraph_reaches_the_prompt_and_is_substituted_like_the_text_around_it(
    tmp_path,
):
    """The extras are inserted BEFORE the placeholders are filled, so a host paragraph may use the
    mechanism's own placeholders instead of repeating a value the config already holds -- which is
    what roedor's own worker file does with `__TEST_COMMAND__`."""
    extras = tmp_path / "worker.md"
    extras.write_text("HOW THIS PROJECT RUNS ITS TESTS\n- `bash __TEST_COMMAND__ <paths>`.\n")
    rendered = render_prompt(
        "worker",
        {
            "HUMAN_LOGIN": "someone",
            "HUMAN_MESSAGE_RULES": "WRITING TO THE HUMAN\nin their language.",
            "TEST_COMMAND": "make test",
            "MAIN_CHECKOUT": "/somewhere",
            "FORBIDDEN_PATHS_RULES": "",
            "MECHANISM_PATHS_RULES": "",
            "NEVER_RUN_RULES": "",
            "WORKER_ENVIRONMENT_RULES": "",
        },
        extras,
    )
    assert "HOW THIS PROJECT RUNS ITS TESTS" in rendered
    assert "`bash make test <paths>`" in rendered


def test_the_renderer_refuses_an_extras_file_the_config_names_and_the_filesystem_lacks(tmp_path):
    """A host that names a file and then moves it must hear about it. Rendering the paragraph away
    silently would leave a prompt that is merely shorter, and nothing downstream could tell that
    from a host which configured none."""
    with pytest.raises(FileNotFoundError):
        render_prompt("worker", {}, tmp_path / "never-written.md")


def test_the_renderer_refuses_a_placeholder_nothing_answered():
    """A prompt is a contract, and `__WORKTREE__` reaching an agent as those nine characters is a
    clause it cannot act on. The refusal is what stops a driver before it launches a backend."""
    with pytest.raises(KeyError) as failure:
        render_prompt("validator", {})
    assert "__WORKTREE__" in str(failure.value)


def test_an_unknown_role_has_no_template_and_says_so():
    with pytest.raises(KeyError):
        render_prompt("archivist", {})
