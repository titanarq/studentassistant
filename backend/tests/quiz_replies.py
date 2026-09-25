"""Scripted `record_quiz` answers for the quiz generator tests.

`QUESTIONS` is a valid draft of three questions over the "Derivadas" fixture notes (anchors
`definicion` and `proximo-dia`), one of each type; `reply_quiz` scripts a FakeClaude reply.
"""

from __future__ import annotations

from typing import Any

from studentassistant.generators.quiz import TOOL_NAME
from studentassistant.llm import FakeClaude

MULTIPLE_CHOICE: dict[str, Any] = {
    "type": "multiple_choice",
    "difficulty": "easy",
    "question": "¿Qué es la derivada?",
    "options": ["Un límite", "Una integral", "Una suma"],
    "answer": "un límite",
    "explanation": "Es el límite del cociente incremental.",
    "anchors": ["#definicion"],
}
TRUE_FALSE: dict[str, Any] = {
    "type": "true_false",
    "difficulty": "medium",
    "question": "La derivada se escribe $f'(x)$.",
    "options": [],
    "answer": "verdadero",
    "explanation": "Así se escribe en los apuntes.",
    "anchors": ["definicion"],
}
SHORT_ANSWER: dict[str, Any] = {
    "type": "short_answer",
    "difficulty": "hard",
    "question": "¿Qué regla se verá el próximo día?",
    "options": [],
    "answer": "La regla de la cadena",
    "explanation": "Lo dice la última sección.",
    "anchors": ["proximo-dia"],
}
QUESTIONS = [MULTIPLE_CHOICE, TRUE_FALSE, SHORT_ANSWER]


def reply_quiz(fake: FakeClaude, *questions: dict[str, Any]) -> None:
    fake.reply_tool(TOOL_NAME, {"questions": list(questions or QUESTIONS)})
