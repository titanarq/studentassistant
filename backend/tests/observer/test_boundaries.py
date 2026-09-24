"""ADR-0002/0004: the observer's state code reaches the vault only through its functions and
imports no LLM code."""

from __future__ import annotations

import ast
from pathlib import Path

import studentassistant.observer

OBSERVER_DIR = Path(studentassistant.observer.__file__).resolve().parent
FORBIDDEN_MODULES = ("anthropic", "studentassistant.llm", "subprocess", "shutil")
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
            if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
                found.append(f"{path.name}:{node.lineno} imports {name}")
    return found


def test_observer_imports_no_llm_and_writes_no_file() -> None:
    found = [o for path in sorted(OBSERVER_DIR.rglob("*.py")) for o in offences(path)]
    assert found == []


def test_the_check_catches_what_it_forbids(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\nfrom studentassistant.llm import x\n"
        "open('f')\nPath('p').write_text('')\n"
    )
    assert len(offences(sample)) == 4
