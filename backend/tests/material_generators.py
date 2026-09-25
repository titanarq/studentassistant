"""A sample generator for the framework tests: kind `prueba`, one Markdown list of points.

`PointsGenerator` asks Claude (through `GeneratorContext.structured`) for `Points` -- a list of
titled points, each with the note anchors it came from -- and writes them as `prueba.md`; with the
option `split`, each point also goes to `prueba/<n>.md`. `reply_points` scripts that answer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from studentassistant.generators import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    GeneratorRegistry,
    ItemProvenance,
)
from studentassistant.llm import FakeClaude, content_hash

KIND = "prueba"
TOOL = "record_points"
SYSTEM = "Resume los apuntes en puntos."


class Point(BaseModel):
    title: str
    anchors: list[str]


class Points(BaseModel):
    points: list[Point]


class PointsOptions(BaseModel):
    size: int = Field(default=3, ge=1, le=10)
    split: bool = False


class PointsGenerator(Generator):
    kind = KIND
    title = "Puntos"
    description = "Una lista de puntos de prueba."
    options_model = PointsOptions

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        options = context.options
        assert isinstance(options, PointsOptions)
        result = await context.structured(
            [{"role": "user", "content": [context.notes_block()]}],
            Points,
            tool_name=TOOL,
            tool_description="Record the points.",
            system=SYSTEM,
            prompt_hash=content_hash(SYSTEM),
        )
        points = result.value.points[: options.size]
        lines = [f"- {point.title}\n" for point in points]
        files: dict[str, str | bytes] = {f"{KIND}.md": "# Puntos\n\n" + "".join(lines)}
        if options.split:
            for number, point in enumerate(points, start=1):
                files[f"{KIND}/{number}.md"] = point.title + "\n"
        return GeneratorOutput(
            files=files,
            items=[
                ItemProvenance(item=f"p{number}", anchors=point.anchors)
                for number, point in enumerate(points, start=1)
            ],
            model=result.responses[-1].model,
            prompt_hash=content_hash(SYSTEM),
        )


def points_registry() -> GeneratorRegistry:
    registry = GeneratorRegistry()
    registry.register(PointsGenerator)
    return registry


def reply_points(fake: FakeClaude, *points: tuple[str, list[str]]) -> None:
    payload: dict[str, Any] = {
        "points": [{"title": title, "anchors": anchors} for title, anchors in points]
    }
    fake.reply_tool(TOOL, payload)


DEFAULT_POINTS: tuple[tuple[str, list[str]], ...] = (
    ("La derivada es un límite", ["definicion"]),
    ("Se verá la regla de la cadena", ["proximo-dia"]),
)
