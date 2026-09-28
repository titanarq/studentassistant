"""Crop one region of a stored page image: Sonnet locates the region (`locate_region`).

The student asks for only part of a page ("solo el diagrama de la página 3"); Claude (role
`observer`, Sonnet by default -- no model id here) receives the page as an image content block and
the description, and answers through the strict tool `crop_region` (`llm.structured`) with a
`BoundingBox`: four fractions of the image's width/height in `[0, 1]`, `x0 < x1`, `y0 < y1`. A box
out of these bounds fails validation and is re-asked by `structured` itself, never used raw. The
prompt (`prompts/editor_crop.md`) asks for a tight box that leaves out blank margins, the desk and
fingers holding the page: that is the whole reframing step.

Claude is reached only through `studentassistant.llm` (ADR-0004).
"""

from __future__ import annotations

import base64
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from studentassistant.config import Settings
from studentassistant.editor.inputs import IMAGE_MEDIA_TYPES
from studentassistant.llm import (
    LedgerBinding,
    LLMClient,
    RefusalError,
    StructuredOutputError,
    get_client,
    load_prompt,
    structured,
)

PROMPT_NAME = "editor_crop"
ROLE = "observer"
TOOL_NAME = "crop_region"
TOOL_DESCRIPTION = (
    "Give the tight bounding box of the requested region, as fractions of the image's width "
    "and height."
)
MAX_TOKENS = 300

UNSUPPORTED_IMAGE_MESSAGE = "La fuente indicada no es una imagen que se pueda recortar."
EMPTY_REGION_MESSAGE = "Indica qué parte de la página quieres recortar."
REGION_NOT_FOUND_MESSAGE = (
    "No he podido localizar la parte de la página que pides; prueba a describirla de otra forma."
)


class CropError(ValueError):
    """A crop could not be made; its message is Spanish, for the student."""


class RegionNotFoundError(CropError):
    """Claude declined, or gave no valid box even after the re-ask."""


class BoundingBox(BaseModel):
    """A region as fractions of the image: `x` of its width, `y` of its height, from top-left."""

    model_config = ConfigDict(extra="forbid")

    x0: float = Field(ge=0, le=1, description="Left edge, fraction of the image width.")
    y0: float = Field(ge=0, le=1, description="Top edge, fraction of the image height.")
    x1: float = Field(ge=0, le=1, description="Right edge, fraction of the image width.")
    y1: float = Field(ge=0, le=1, description="Bottom edge, fraction of the image height.")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.x0 < self.x1:
            raise ValueError("x0 must be smaller than x1")
        if not self.y0 < self.y1:
            raise ValueError("y0 must be smaller than y1")
        return self

    def as_list(self) -> list[float]:
        """`[x0, y0, x1, y1]`, the form stored in a crop's sidecar."""
        return [self.x0, self.y0, self.x1, self.y1]


def crop_client(
    *,
    settings: Settings | None = None,
    transport: Any | None = None,
    ledger: LedgerBinding | None = None,
) -> LLMClient:
    """The client that locates regions: role `observer` from `[llm.roles.observer]`."""
    return get_client(ROLE, settings=settings, transport=transport, ledger=ledger)


def region_request(image: bytes, media_type: str, description: str) -> list[dict[str, Any]]:
    """The user message: the page as an image content block, then the requested region."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image).decode("ascii"),
                    },
                },
                {
                    "type": "text",
                    "text": "Locate this region of the page, in the student's words: "
                    + description.strip(),
                },
            ],
        }
    ]


async def locate_region(
    client: LLMClient, image: bytes, media_type: str, description: str
) -> BoundingBox:
    """The tight box Claude gives for `description` on `image`.

    Raises:
        CropError: `media_type` is not an image Claude reads, or `description` is empty.
        RegionNotFoundError: Claude declined, or no valid box came even after the re-ask.
        LLMError: any other failure of the call (a reached cap, exhausted retries).
    """
    if media_type not in IMAGE_MEDIA_TYPES:
        raise CropError(UNSUPPORTED_IMAGE_MESSAGE)
    if not description.strip():
        raise CropError(EMPTY_REGION_MESSAGE)
    prompt = load_prompt(PROMPT_NAME)
    try:
        result = await structured(
            client,
            region_request(image, media_type, description),
            BoundingBox,
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
            system=prompt.content,
            max_tokens=MAX_TOKENS,
            prompt_hash=prompt.hash,
        )
    except (RefusalError, StructuredOutputError) as error:
        raise RegionNotFoundError(REGION_NOT_FOUND_MESSAGE) from error
    return result.value


__all__ = [
    "PROMPT_NAME",
    "ROLE",
    "TOOL_NAME",
    "BoundingBox",
    "CropError",
    "RegionNotFoundError",
    "crop_client",
    "locate_region",
    "region_request",
]
