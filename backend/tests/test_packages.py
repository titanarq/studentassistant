"""Every backend module named in AGENTS.md is an importable subpackage with a one-line docstring."""

import importlib

import pytest

BACKEND_MODULE_NAMES = (
    "server",
    "stt",
    "vault",
    "sources",
    "llm",
    "observer",
    "editor",
    "generators",
    "protocol",
)


@pytest.mark.parametrize("module_name", BACKEND_MODULE_NAMES)
def test_subpackage_is_importable_and_documented(module_name: str) -> None:
    module = importlib.import_module(f"studentassistant.{module_name}")

    assert isinstance(module.__doc__, str)
    assert module.__doc__.strip()
    assert len(module.__doc__.strip().splitlines()) == 1
