"""`loads_tolerant`: small defects of JSON written as text are repaired, the rest raise (#320)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from studentassistant.llm.json_repair import loads_tolerant


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        # The log's case: two members (here, two ops) with no `,` between them.
        ('{"ops": [\n  {"op": "a"}\n   {"op": "b"}\n]}', {"ops": [{"op": "a"}, {"op": "b"}]}),
        ('{"a": 1\n "b": true "c": null}', {"a": 1, "b": True, "c": None}),
        ('{"a": [1, 2,], "b": {"c": 3,},}', {"a": [1, 2], "b": {"c": 3}}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Aquí está:\n{"a": 1}\nEspero que sirva.', {"a": 1}),
        ('{"t": "dijo "hola" y "adiós" al salir"}', {"t": 'dijo "hola" y "adiós" al salir'}),
        ('{"t": "una\nlínea\tcon tabulador"}', {"t": "una\nlínea\tcon tabulador"}),
        # Commas and brackets inside strings are never touched.
        ('{"t": "a, ]} b" "u": "x,}"}', {"t": "a, ]} b", "u": "x,}"}),
        ("[1 2 3]", [1, 2, 3]),
    ],
)
def test_recoverable_json_is_repaired(text: str, expected: Any) -> None:
    assert loads_tolerant(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '{"a": ',  # cut off
        '{"a": [1, 2',  # cut off
        "no json here",
        '{"a" 1}',
        "",
    ],
)
def test_unusable_json_raises_the_original_error(text: str) -> None:
    with pytest.raises(json.JSONDecodeError) as info:
        loads_tolerant(text)
    with pytest.raises(json.JSONDecodeError) as original:
        json.loads(text)
    assert str(info.value) == str(original.value)
