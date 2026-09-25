"""`project.backends` (#514 stage 2/3): a backend is configuration. Its entry names the command,
worktree, App, stream parser and quota detector; the three parallel maps it replaced still load as
a deprecated alias with one warning; and what the mechanism does per backend is read off the entry,
never off the backend's name."""

from __future__ import annotations

import re
import typing
import warnings

import pytest
from pydantic import BaseModel, ValidationError

from agent_os import guard, lib
from agent_os.cli import AGENT_OS_DIR
from agent_os.lib import (
    DEPRECATED_BACKEND_MAPS,
    AgentsConfig,
    BackendConfig,
    DeprecatedBackendMapsWarning,
    ProjectConfig,
    TaskClass,
    UsageSummary,
    backend_default_model,
    backend_executable,
    backend_quota_cuts,
    backend_stream_parser,
    load_project,
    worktree_path,
)
from agent_os.streams import ResultUsage

REQUIRED = {"repo": "owner/name", "tracking_epic": 1, "board_number": 1}


@pytest.fixture(autouse=True)
def _every_deprecation_warning_is_new(monkeypatch):
    # The loader emits each deprecation message once per process; each test here is its own.
    monkeypatch.setattr(lib, "_EMITTED_DEPRECATION_WARNINGS", set())


def _project(**fields) -> ProjectConfig:
    return ProjectConfig(**REQUIRED, **fields)


def _deprecation_messages(caught) -> list[str]:
    return [str(w.message) for w in caught if issubclass(w.category, DeprecatedBackendMapsWarning)]


# ---- the section itself ------------------------------------------------------------------------


def test_the_example_config_declares_its_backends_in_the_new_section():
    backends = load_project().backends
    assert list(backends) == ["qwen", "claude"]
    assert backends["claude"].stream == "claude_jsonl"
    assert backends["claude"].quota == "claude_rate_limit"
    assert backends["qwen"].stream == "qwen_jsonl"
    assert backends["qwen"].quota == "none"


def test_an_unregistered_stream_parser_fails_at_config_load():
    with pytest.raises(ValidationError, match="unknown stream parser 'foo_jsonl'"):
        _project(backends={"foo": {"stream": "foo_jsonl"}})


def test_an_unregistered_quota_detector_fails_at_config_load():
    with pytest.raises(ValidationError, match="unknown quota detector 'guesswork'"):
        _project(backends={"foo": {"stream": "claude_jsonl", "quota": "guesswork"}})


def test_a_backend_entry_must_name_its_stream():
    with pytest.raises(ValidationError):
        _project(backends={"foo": {"worktree": "../foo"}})


def test_a_new_section_emits_no_deprecation_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _project(backends={"foo": {"stream": "claude_jsonl"}})
    assert _deprecation_messages(caught) == []


# ---- the deprecated alias ----------------------------------------------------------------------


def test_the_old_maps_build_the_backends_with_one_warning_naming_all_four_keys():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        project = _project(
            worktrees={"qwen": "../x-qwen", "claude": "../x-claude"},
            worker_apps={"qwen": "x-qwen", "claude": "x-claude"},
            executables={"qwen": "/opt/bin/qwen", "gh": "/opt/bin/gh"},
        )
        _project(worktrees={"qwen": "../x-qwen"})  # a second load in the same process
    messages = _deprecation_messages(caught)
    assert len(messages) == 1, messages
    for key in (
        "project.worktrees",
        "project.worker_apps",
        "project.executables",
        "project.backends",
    ):
        assert key in messages[0]
    assert project.backends == {
        "qwen": BackendConfig(
            worktree="../x-qwen", app="x-qwen", stream="qwen_jsonl", quota="none"
        ),
        "claude": BackendConfig(
            worktree="../x-claude", app="x-claude", stream="claude_jsonl", quota="claude_rate_limit"
        ),
    }
    # `executables` keeps its own meaning: `gh` is on the PATH set and is not a backend.
    assert "gh" not in project.backends
    assert project.executables["gh"] == "/opt/bin/gh"
    assert backend_executable("qwen", project=project) == "/opt/bin/qwen"


def test_the_alias_reads_a_third_backend_exactly_as_before_the_section_existed():
    """Before `backends:` a backend with no parser of its own name was read with the default shape
    and never cut on quota -- the guard's rule named one backend. The alias keeps both."""
    project = _project(worktrees={"foo": "../x-foo"})
    assert project.backends["foo"].stream == "claude_jsonl"
    assert project.backends["foo"].quota == "none"
    assert backend_quota_cuts("foo", project=project) is False


def test_the_new_section_wins_and_the_ignored_old_keys_are_named():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        project = _project(
            backends={
                "foo": {
                    "command": "/opt/foo/bin/foo",
                    "worktree": "../x-foo",
                    "stream": "claude_jsonl",
                }
            },
            worktrees={"qwen": "../x-qwen"},
            worker_apps={"qwen": "x-qwen"},
            executables={"foo": "/elsewhere/foo", "gh": "/opt/bin/gh"},
        )
    messages = _deprecation_messages(caught)
    assert len(messages) == 1, messages
    assert "project.worktrees" in messages[0]
    assert "project.worker_apps" in messages[0]
    assert "project.executables.foo" in messages[0]
    assert "gh" not in messages[0]
    assert list(project.backends) == ["foo"]
    # The deprecated maps mirror the section, so a reader that has not moved sees the same thing.
    assert project.worktrees == {"foo": "../x-foo"}
    assert project.worker_apps == {}
    assert backend_executable("foo", project=project) == "/opt/foo/bin/foo"


def test_an_absolute_backend_command_joins_the_path_set_and_a_bare_one_does_not():
    project = _project(
        backends={
            "foo": {"command": "/opt/foo/bin/foo", "stream": "claude_jsonl"},
            "bar": {"command": "bar-cli", "stream": "qwen_jsonl"},
        },
        executables={"gh": "/opt/bin/gh"},
    )
    assert project.executables == {"gh": "/opt/bin/gh", "foo": "/opt/foo/bin/foo"}
    assert backend_executable("bar", project=project) == "bar-cli"


def test_a_backend_with_no_command_resolves_through_executables_then_its_own_name():
    project = _project(
        backends={
            "foo": {"stream": "claude_jsonl"},
            "bar": {"stream": "claude_jsonl"},
        },
        executables={"foo": "/opt/foo/bin/foo"},
    )
    assert backend_executable("foo", project=project) == "/opt/foo/bin/foo"
    assert backend_executable("bar", project=project) == "bar"


# ---- capability reads ---------------------------------------------------------------------------


def test_the_quota_capability_is_what_cuts_not_the_name():
    project = _project(
        backends={
            "foo": {"stream": "claude_jsonl", "quota": "claude_rate_limit"},
            "claude": {"stream": "claude_jsonl", "quota": "none"},
        }
    )
    assert backend_quota_cuts("foo", project=project) is True
    assert backend_quota_cuts("claude", project=project) is False
    assert backend_quota_cuts("not-configured", project=project) is False


def test_the_stream_parser_is_the_configured_one_not_the_one_named_after_the_backend():
    project = _project(
        backends={"qwen": {"stream": "claude_jsonl"}, "foo": {"stream": "qwen_jsonl"}}
    )
    assert backend_stream_parser("qwen", project=project).name == "claude_jsonl"
    assert backend_stream_parser("foo", project=project).name == "qwen_jsonl"
    # A log whose backend is unknown, or no longer configured, reads with the default shape.
    assert backend_stream_parser(None, project=project).name == "claude_jsonl"
    assert backend_stream_parser("gone", project=project).name == "claude_jsonl"


def test_worktree_path_reads_the_backend_entry(tmp_path):
    project = _project(backends={"foo": {"worktree": "../x-foo", "stream": "claude_jsonl"}})
    assert worktree_path("foo", main=tmp_path / "host", project=project) == (tmp_path / "x-foo")
    with pytest.raises(KeyError):
        worktree_path("bar", main=tmp_path, project=project)


def test_a_worker_s_default_model_is_read_off_the_classes_on_its_backend():
    # The example config: the worker classes run on qwen; claude carries only the one-shot roles.
    assert backend_default_model("qwen") == "qwen3.8-max"
    assert backend_default_model("claude") == "claude-opus-5"
    with pytest.raises(KeyError, match="no class"):
        backend_default_model("foo")


def _task_class(**overrides) -> TaskClass:
    fields = {
        "backend": "foo",
        "model": "m",
        "max_context": 10**9,
        "max_cost_usd": 1.0,
        "max_total_tokens": 10**9,
        "commit_warn_turns": 10,
        "commit_cut_turns": 20,
    }
    return TaskClass(**{**fields, **overrides})


class _ReportsNoCost:
    """A parser whose backend reports no dollar figure -- whatever its `result` event carries."""

    name = "reports_no_cost"

    def result_usage(self, events):
        return ResultUsage(None, 5, events[-1])


def test_the_budget_reads_the_live_cost_through_the_backends_own_parser():
    summary = UsageSummary("s", 1, 10, 5, {"type": "result", "total_cost_usd": 9.0})
    task_class = _task_class(max_cost_usd=1.0)
    assert guard.budget_exceeded(summary, task_class) is True
    assert guard.budget_exceeded(summary, task_class, parser=_ReportsNoCost()) is False


# ---- the adoption checklist names live keys (agent-os#8) ---------------------------------------

# A config key as `docs/ADOPTION.md` spells it: `project.backends.<name>.app`,
# `project.labels.{a,b}`. Not preceded by a word character or `[`, so `pyproject.toml` and the
# `[project.scripts]` table of `pyproject.toml` are not read as keys of `config/agents.yaml`.
_CONFIG_KEY = re.compile(
    r"(?<![\w\[.])(project|mechanism|planner)((?:\.(?:[a-z_]+|<[a-z]+>|\{[a-z_,]+\}))+)"
)


def _unknown_segments(section: str, dotted: str) -> list[str]:
    """What of `<section><dotted>` the schema does not have, walked field by field through the
    pydantic models `AgentsConfig` is built from; a `dict[str, X]` field takes any key next."""
    node = AgentsConfig.model_fields[section].annotation
    for segment in dotted.strip(".").split("."):
        if typing.get_origin(node) is dict:
            node = typing.get_args(node)[1]
            continue
        if not (isinstance(node, type) and issubclass(node, BaseModel)):
            return [f"{segment} (below a value that has no fields)"]
        names = segment.strip("{}").split(",")
        unknown = [name for name in names if name not in node.model_fields]
        if unknown:
            return unknown
        node = node.model_fields[names[0]].annotation
    return []


def _deprecated_project_keys() -> set[str]:
    """The old per-backend maps the loader still warns about when one alone is set -- asked of the
    loader, so `executables`, which kept a live meaning of its own, is not among them."""
    deprecated = set()
    for key in DEPRECATED_BACKEND_MAPS:
        # Every key's warning is the same one message, emitted once per process: forget it
        # between keys (the autouse fixture above hands this test a set of its own).
        lib._EMITTED_DEPRECATION_WARNINGS.clear()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _project(**{key: {"somebackend": "some-value"}})
        if _deprecation_messages(caught):
            deprecated.add(key)
    return deprecated


def test_every_config_key_the_adoption_checklist_names_is_a_live_key():
    deprecated = _deprecated_project_keys()
    assert deprecated, "the loader deprecates no key any more -- drop this half of the test"
    mentions = _CONFIG_KEY.findall((AGENT_OS_DIR / "docs" / "ADOPTION.md").read_text())
    assert mentions, "ADOPTION.md names no config key -- the pattern no longer matches it"
    problems = []
    for section, dotted in mentions:
        key = f"{section}{dotted}"
        problems += [f"{key}: no field {name!r}" for name in _unknown_segments(section, dotted)]
        if section == "project" and dotted.split(".")[1] in deprecated:
            problems.append(f"{key}: deprecated")
    assert not problems, "ADOPTION.md names config keys the loader does not read:\n" + "\n".join(
        sorted(set(problems))
    )
