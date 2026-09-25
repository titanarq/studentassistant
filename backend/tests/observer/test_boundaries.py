"""ADR-0002/0004: the observer reaches the vault only through the `studentassistant.vault` root,
writes no file, never imports `anthropic`, and only the live loop (`live.py`) reaches Claude,
through `studentassistant.llm`."""

from __future__ import annotations

import ast
from pathlib import Path

import studentassistant.observer

OBSERVER_DIR = Path(studentassistant.observer.__file__).resolve().parent
# Nor does it ever reach into the server (AGENTS.md's dependency direction).
FORBIDDEN_MODULES = (
    "anthropic",
    "studentassistant.llm",
    "studentassistant.server",
    "subprocess",
    "shutil",
)
# The live loop is the observer role's Claude client (ADR-0004), so it alone may use the llm module.
LLM_MODULE = "studentassistant.llm"
LLM_ALLOWED_FILES = frozenset({"live.py"})
# The vault is reached through its public root only, never one of its submodules.
VAULT_PACKAGE = "studentassistant.vault"
FORBIDDEN_CALLS = ("open", "write_text", "write_bytes", "mkdir", "unlink", "rename", "replace")


def offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in FORBIDDEN_CALLS:
                found.append(f"{path.name}:{node.lineno} calls {name}")
            continue
        else:
            continue
        for name in names:
            if path.name in LLM_ALLOWED_FILES and (
                name == LLM_MODULE or name.startswith(LLM_MODULE + ".")
            ):
                continue
            if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
                found.append(f"{path.name}:{node.lineno} imports {name}")
            elif name.startswith(VAULT_PACKAGE + "."):
                found.append(f"{path.name}:{node.lineno} imports vault internal {name}")
    return found


def test_observer_imports_no_llm_but_in_the_live_loop_nor_vault_internals_nor_writes() -> None:
    found = [o for path in sorted(OBSERVER_DIR.rglob("*.py")) for o in offences(path)]
    assert found == []


def test_the_check_catches_what_it_forbids(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\nfrom studentassistant.llm import x\n"
        "open('f')\nPath('p').write_text('')\n"
        "from studentassistant.vault.sources import SourceKind\n"
        "import studentassistant.vault.state\n"
        "from studentassistant.vault import Event\n"
    )
    assert len(offences(sample)) == 6


def test_only_the_live_loop_may_use_the_llm_module(tmp_path: Path) -> None:
    live = tmp_path / "live.py"
    live.write_text("from studentassistant.llm import get_client\nimport anthropic\n")
    assert offences(live) == ["live.py:2 imports anthropic"]
    other = tmp_path / "context.py"
    other.write_text("from studentassistant.llm import x\nimport studentassistant.server\n")
    assert len(offences(other)) == 2
