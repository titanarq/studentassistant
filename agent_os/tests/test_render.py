"""`agent_os.render` -- the generic `__TOKEN__` substitution `agent_os.install` uses to render
`agent_os/agents/*.md` into a host's `.claude/agents/*.md` (#510, #511). Exercised against a
fixture template under a temp directory for the renderer's own rules, and against #510's two real
templates at the bottom of this file, once that PR landed alongside this one: the renderer is
generic over whatever token names a template carries, as long as they are in `token_values`'s
known set, and the two real templates are what proves that set is actually sufficient.

Pure filesystem. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_os.cli import AGENT_OS_DIR
from agent_os.lib import ProjectConfig, load_agents_config
from agent_os.render import RenderError, render_agent_template, render_worktrees, token_values


def _project(**overrides) -> ProjectConfig:
    base = {
        "repo": "owner/name",
        "tracking_epic": 1,
        "board_number": 1,
        "guard_unit": "acme-guard",
        "human_login": "octocat",
        "test_command": "scripts/test.sh",
        "module_docs_dir": "docs/modules",
        "worktrees": {"qwen": "../acme-qwen", "claude": "../acme-claude"},
    }
    base.update(overrides)
    return ProjectConfig(**base)


def test_render_worktrees_formats_one_pair_per_backend_in_order():
    assert render_worktrees(_project()) == "qwen: ../acme-qwen, claude: ../acme-claude"


def test_render_worktrees_is_empty_when_no_backend_is_configured():
    assert render_worktrees(_project(worktrees={})) == ""


def test_token_values_covers_every_token_the_issue_names():
    values = token_values(_project())
    assert values == {
        "__GUARD_UNIT__": "acme-guard",
        "__WORKTREES__": "qwen: ../acme-qwen, claude: ../acme-claude",
        "__HUMAN_LOGIN__": "octocat",
        "__TEST_COMMAND__": "scripts/test.sh",
        "__MODULE_DOCS__": "docs/modules",
        "__REPO__": "owner/name",
        "__MERGE_METHOD__": "merge",
    }


def test_render_agent_template_substitutes_every_known_token(tmp_path):
    template = tmp_path / "worker-runner.md"
    template.write_text(
        "Guard unit: __GUARD_UNIT__\n"
        "Worktrees: __WORKTREES__\n"
        "Human: __HUMAN_LOGIN__\n"
        "Test command: __TEST_COMMAND__\n"
        "Module docs: __MODULE_DOCS__\n"
    )
    rendered = render_agent_template(template.read_text(), _project())
    assert rendered == (
        "Guard unit: acme-guard\n"
        "Worktrees: qwen: ../acme-qwen, claude: ../acme-claude\n"
        "Human: octocat\n"
        "Test command: scripts/test.sh\n"
        "Module docs: docs/modules\n"
    )
    assert "__" not in rendered


def test_render_agent_template_is_a_no_op_on_a_template_with_no_tokens(tmp_path):
    template = tmp_path / "plain.md"
    template.write_text("Nothing to substitute here.\n")
    assert (
        render_agent_template(template.read_text(), _project()) == "Nothing to substitute here.\n"
    )


def test_render_agent_template_refuses_an_unknown_token(tmp_path):
    template = tmp_path / "broken.md"
    template.write_text("Guard unit: __GUARD_UNIT__, and also __SOME_UNKNOWN_TOKEN__\n")
    with pytest.raises(RenderError, match="__SOME_UNKNOWN_TOKEN__"):
        render_agent_template(template.read_text(), _project())


def test_render_agent_template_refuses_a_typo_of_a_known_token(tmp_path):
    # One underscore short of __GUARD_UNIT__ -- exactly the kind of typo the refusal exists to
    # catch instead of silently leaving a literal token in a prompt a human is meant to trust.
    template = tmp_path / "typo.md"
    template.write_text("Guard unit: __GUARDUNIT__\n")
    with pytest.raises(RenderError, match="__GUARDUNIT__"):
        render_agent_template(template.read_text(), _project())


# ---- #510's own templates: the two real files `agent_os.install` copies ----------------------


AGENT_TEMPLATES_DIR = AGENT_OS_DIR / "agents"


@pytest.mark.parametrize("name", ["control-plane.md", "worker-runner.md"])
def test_the_real_agent_templates_render_clean_against_the_example_config(name):
    # `config.example.yaml` is the one file every key of §4.2 is filled in on, with no real
    # repository, identity or topic -- exactly the config a fresh adopter's first `agent-os
    # install` runs against, so this is the render #510's own templates have to survive.
    example = load_agents_config(AGENT_OS_DIR / "config.example.yaml").project
    template = (AGENT_TEMPLATES_DIR / name).read_text()
    rendered = render_agent_template(template, example)
    assert "__" not in rendered, f"{name} still carries a token after rendering"


# ---- #88: the control plane's merge step carries the host's configured merge method ----------


def _rendered_control_plane(project: ProjectConfig) -> str:
    return render_agent_template((AGENT_TEMPLATES_DIR / "control-plane.md").read_text(), project)


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_control_plane_merges_by_rest_with_the_configured_method(method):
    rendered = _rendered_control_plane(_project(merge_method=method))
    assert "gh api -X PUT repos/owner/name/pulls/N/merge" in rendered
    assert f"-f merge_method={method}" in rendered
    # Pinned to the head that was verified, so a push after the review cannot ride along.
    assert "-f sha=" in rendered
    assert "gh api -X DELETE repos/owner/name/git/refs/heads/" in rendered


def test_control_plane_merge_method_defaults_to_a_merge_commit():
    rendered = _rendered_control_plane(_project())
    assert "-f merge_method=merge" in rendered


def test_control_plane_no_longer_hardcodes_gh_pr_merge():
    # `gh pr merge` goes through GraphQL (secondary rate limit, #70) and fixed `--merge`, which a
    # host that squashes had to hand-edit away -- an edit `agent-os-install --force` then wiped.
    rendered = _rendered_control_plane(_project(merge_method="squash"))
    assert "gh pr merge" not in rendered
    assert "--merge" not in rendered


@pytest.mark.parametrize("value", ["fast-forward", "Squash", ""])
def test_an_unknown_merge_method_is_refused_when_the_config_loads(value):
    with pytest.raises(ValidationError, match="merge_method"):
        _project(merge_method=value)


def test_an_unknown_merge_method_in_agents_yaml_is_refused_by_load_agents_config(tmp_path):
    example = (AGENT_OS_DIR / "config.example.yaml").read_text()
    assert "\n  merge_method: merge\n" in example, "config.example.yaml documents the key"
    broken = tmp_path / "agents.yaml"
    broken.write_text(example.replace("\n  merge_method: merge\n", "\n  merge_method: octopus\n"))
    with pytest.raises(ValidationError, match="merge_method"):
        load_agents_config(broken)
