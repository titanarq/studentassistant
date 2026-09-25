"""`find_ant_profile`: the SDK's profile lookup on a temporary config dir; no secret is read."""

from __future__ import annotations

from pathlib import Path

from studentassistant.llm import find_ant_profile


def write_profile(config_dir: Path, name: str) -> None:
    (config_dir / "configs").mkdir(parents=True, exist_ok=True)
    (config_dir / "configs" / f"{name}.json").write_text("{}", encoding="utf-8")


def test_no_config_dir_means_no_profile(tmp_path: Path) -> None:
    assert find_ant_profile({}, tmp_path) is None


def test_the_default_profile_under_home(tmp_path: Path) -> None:
    write_profile(tmp_path / ".config" / "anthropic", "default")
    assert find_ant_profile({}, tmp_path) == "default"


def test_the_active_config_pointer_selects_the_profile(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "anthropic"
    write_profile(config_dir, "default")
    (config_dir / "active_config").write_text("work\n", encoding="utf-8")
    assert find_ant_profile({}, tmp_path) is None  # the selected profile does not exist
    write_profile(config_dir, "work")
    assert find_ant_profile({}, tmp_path) == "work"


def test_environment_overrides(tmp_path: Path) -> None:
    config_dir = tmp_path / "elsewhere"
    write_profile(config_dir, "ci")
    environ = {"ANTHROPIC_CONFIG_DIR": str(config_dir), "ANTHROPIC_PROFILE": "ci"}
    assert find_ant_profile(environ, tmp_path / "home") == "ci"
    assert find_ant_profile({**environ, "ANTHROPIC_PROFILE": "other"}, tmp_path) is None


def test_a_profile_name_cannot_escape_the_config_dir(tmp_path: Path) -> None:
    write_profile(tmp_path / ".config" / "anthropic", "default")
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    assert find_ant_profile({"ANTHROPIC_PROFILE": "../../../x"}, tmp_path) is None
    assert find_ant_profile({"ANTHROPIC_PROFILE": ".hidden"}, tmp_path) is None
