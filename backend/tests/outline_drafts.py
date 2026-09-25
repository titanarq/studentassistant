"""Scripted outline answers for the `esquema` generator tests.

`DRAFT` is the outline of `valid_notes` (the "Derivadas" fixture topic, anchors `definicion` and
`proximo-dia`) as Claude would give it: flat nodes naming their parents, three levels deep, with
titles that need escaping in mermaid (quotes, parentheses, `<`). `GOLDEN` is where its rendering,
`generated/esquema.md`, is checked in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from studentassistant.generators.outline import TOOL_NAME
from studentassistant.llm import FakeClaude

GOLDEN = Path(__file__).parent / "fixtures" / "generators" / "esquema.md"
TOPIC_TITLE = "Derivadas"

DRAFT: dict[str, Any] = {
    "nodes": [
        {
            "id": "n1",
            "parent": None,
            "title": "Definición de derivada",
            "gloss": "El límite del cociente incremental",
            "anchors": ["definicion"],
        },
        {
            "id": "n1a",
            "parent": "n1",
            "title": "Cociente incremental (f(a+h) - f(a)) / h",
            "gloss": None,
            "anchors": ["definicion"],
        },
        {
            "id": "n1a1",
            "parent": "n1a",
            "title": "Límite cuando h -> 0",
            "gloss": "Existe si la función es derivable en a",
            "anchors": ["#definicion"],
        },
        {
            "id": "n1b",
            "parent": "n1",
            "title": 'Notación "prima": f\'(x)',
            "gloss": "La que se usa en clase",
            "anchors": ["definicion"],
        },
        {
            "id": "n2",
            "parent": None,
            "title": "Próximo día",
            "gloss": None,
            "anchors": ["proximo-dia"],
        },
        {
            "id": "n2a",
            "parent": "n2",
            "title": "Regla de la cadena <mañana>",
            "gloss": "Se verá en la próxima clase",
            "anchors": ["proximo-dia", "definicion"],
        },
    ]
}


def reply_outline(fake: FakeClaude, draft: dict[str, Any] | None = None) -> None:
    fake.reply_tool(TOOL_NAME, draft if draft is not None else DRAFT)
