"""`agent_os.install`'s stage 3: copying `.github/ISSUE_TEMPLATE/{task,bug}.md`,
`.github/workflows/ci-agent-os.yml` and the `.claude/agents/{control-plane,worker-runner}.md`
prompts (rendered from `agent_os/agents/*.md` via `agent_os.render`), all copy-if-absent
(agent_os/docs/AGENT_OS.md §7 row (h), issue #511).

`agent_os/agents/` is #510's own deliverable, landing in parallel to this one: every test here
patches the module's own directory constants rather than depending on that directory actually
existing in this checkout, so this suite passes whether or not #510 has merged yet. The one
integration point with #510 -- rendering ITS ACTUAL templates -- is verified by hand once that PR
lands, as the issue's own "Not included" says (`agent_os.install`'s docstring, the PR description).

Pure filesystem. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import pytest

import agent_os.install as install_module
from agent_os.install import plan_agent_templates, plan_ci_snippet, plan_issue_templates
from agent_os.lib import ProjectConfig
from agent_os.render import RenderError


def _project(**overrides) -> ProjectConfig:
    base = {"repo": "owner/name", "tracking_epic": 1, "board_number": 1, "guard_unit": "acme-guard"}
    base.update(overrides)
    return ProjectConfig(**base)


# --------------------------------------------------------------------------------------------
# Issue templates: `.github/ISSUE_TEMPLATE/{task,bug}.md`
# --------------------------------------------------------------------------------------------


def test_plan_issue_templates_empty_when_the_canonical_source_dir_is_absent(tmp_path):
    original = install_module.ISSUE_TEMPLATES_DIR
    install_module.ISSUE_TEMPLATES_DIR = tmp_path / "does-not-exist"
    try:
        assert plan_issue_templates(tmp_path) == []
    finally:
        install_module.ISSUE_TEMPLATES_DIR = original


def test_plan_issue_templates_copies_task_and_bug_if_absent(tmp_path):
    source_dir = tmp_path / "canonical"
    source_dir.mkdir()
    (source_dir / "task.md").write_text("a task template\n")
    (source_dir / "bug.md").write_text("a bug template\n")
    original = install_module.ISSUE_TEMPLATES_DIR
    install_module.ISSUE_TEMPLATES_DIR = source_dir
    try:
        actions = plan_issue_templates(tmp_path)
        rendered = {action.dest.name: action.content for action in actions}
        assert rendered == {"task.md": "a task template\n", "bug.md": "a bug template\n"}
        for action in actions:
            assert action.dest.parent == tmp_path / ".github" / "ISSUE_TEMPLATE"
    finally:
        install_module.ISSUE_TEMPLATES_DIR = original


def test_the_real_canonical_issue_templates_carry_no_host_literal():
    # These are checked in once, seeded from roedor's own -- already project-agnostic, per
    # agent_os/docs/AGENT_OS.md §4.1's table -- so a second host gets a working template with no edit.
    for name in ("task.md", "bug.md"):
        text = (install_module.ISSUE_TEMPLATES_DIR / name).read_text()
        for literal in ("roedor", "MatillaM", "titanarq"):
            assert literal not in text, f"{name} carries the host literal {literal!r}"


# --------------------------------------------------------------------------------------------
# CI snippet: `.github/workflows/ci-agent-os.yml`
# --------------------------------------------------------------------------------------------


def test_plan_ci_snippet_empty_when_the_source_is_absent(tmp_path):
    original = install_module.CI_SNIPPET_SOURCE
    install_module.CI_SNIPPET_SOURCE = tmp_path / "does-not-exist.yml"
    try:
        assert plan_ci_snippet(tmp_path) == []
    finally:
        install_module.CI_SNIPPET_SOURCE = original


def test_plan_ci_snippet_copies_to_github_workflows_if_absent(tmp_path):
    source = tmp_path / "ci-agent-os.yml"
    source.write_text("name: agent-os\n")
    original = install_module.CI_SNIPPET_SOURCE
    install_module.CI_SNIPPET_SOURCE = source
    try:
        [action] = plan_ci_snippet(tmp_path)
        assert action.dest == tmp_path / ".github" / "workflows" / "ci-agent-os.yml"
        assert action.content == "name: agent-os\n"
    finally:
        install_module.CI_SNIPPET_SOURCE = original


# --------------------------------------------------------------------------------------------
# `.claude/agents/{control-plane,worker-runner}.md`, rendered from `agent_os/agents/*.md`
# --------------------------------------------------------------------------------------------


def test_plan_agent_templates_reports_no_templates_dir_when_agent_os_agents_is_absent(tmp_path):
    original = install_module.AGENT_TEMPLATES_DIR
    install_module.AGENT_TEMPLATES_DIR = tmp_path / "does-not-exist"
    try:
        actions, note = plan_agent_templates(_project(), tmp_path)
        assert actions == []
        assert note == "no templates dir (agent_os/agents/), skipped"
    finally:
        install_module.AGENT_TEMPLATES_DIR = original


def test_plan_agent_templates_renders_control_plane_and_worker_runner(tmp_path):
    source_dir = tmp_path / "agents"
    source_dir.mkdir()
    (source_dir / "control-plane.md").write_text("guard: __GUARD_UNIT__\n")
    (source_dir / "worker-runner.md").write_text("human: __HUMAN_LOGIN__\n")
    original = install_module.AGENT_TEMPLATES_DIR
    install_module.AGENT_TEMPLATES_DIR = source_dir
    try:
        actions, note = plan_agent_templates(_project(human_login="octocat"), tmp_path)
        assert note is None
        rendered = {action.dest.name: action.content for action in actions}
        assert rendered == {
            "control-plane.md": "guard: acme-guard\n",
            "worker-runner.md": "human: octocat\n",
        }
        for action in actions:
            assert action.dest.parent == tmp_path / ".claude" / "agents"
    finally:
        install_module.AGENT_TEMPLATES_DIR = original


def test_plan_agent_templates_refuses_a_template_with_an_unknown_token(tmp_path):
    source_dir = tmp_path / "agents"
    source_dir.mkdir()
    (source_dir / "control-plane.md").write_text("__SOME_UNKNOWN_TOKEN__\n")
    (source_dir / "worker-runner.md").write_text("fine\n")
    original = install_module.AGENT_TEMPLATES_DIR
    install_module.AGENT_TEMPLATES_DIR = source_dir
    try:
        with pytest.raises(RenderError):
            plan_agent_templates(_project(), tmp_path)
    finally:
        install_module.AGENT_TEMPLATES_DIR = original
