"""ADR-0004: `studentassistant.llm` is the only importer of the `anthropic` SDK."""

from __future__ import annotations

import ast
from pathlib import Path

import studentassistant

PACKAGE_ROOT = Path(studentassistant.__file__).resolve().parent
LLM_DIR = PACKAGE_ROOT / "llm"


def anthropic_imports(path: Path) -> list[int]:
    """Line numbers of every `import anthropic...` / `from anthropic... import` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        if any(name == "anthropic" or name.startswith("anthropic.") for name in names):
            lines.append(node.lineno)
    return lines


def test_no_module_outside_llm_imports_anthropic() -> None:
    offenders = [
        f"{path.relative_to(PACKAGE_ROOT)}:{line}"
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if LLM_DIR not in path.parents
        for line in anthropic_imports(path)
    ]

    assert offenders == [], f"only studentassistant.llm may import anthropic: {offenders}"


def test_the_check_sees_the_llm_module_import() -> None:
    # Guards the test itself: the llm transport does import the SDK, so the scan must find it.
    assert anthropic_imports(LLM_DIR / "transport.py")


def test_the_check_catches_both_import_forms(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import anthropic.types\nfrom anthropic import Anthropic\nimport json\n")

    assert anthropic_imports(sample) == [1, 2]
