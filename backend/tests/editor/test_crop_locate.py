"""`editor.crop.locate_region`: Sonnet's box through `structured`, validated and re-asked."""

from __future__ import annotations

import asyncio
import base64

import pytest
from pydantic import ValidationError

from studentassistant.config import Settings
from studentassistant.editor.crop import (
    PROMPT_NAME,
    ROLE,
    TOOL_NAME,
    BoundingBox,
    CropError,
    RegionNotFoundError,
    crop_client,
    locate_region,
)
from studentassistant.llm import FakeClaude, load_prompt

IMAGE = b"\xff\xd8\xff\xe0 not really a jpeg, the fake never decodes it"
BOX = {"x0": 0.1, "y0": 0.2, "x1": 0.6, "y1": 0.75}


def _locate(fake: FakeClaude, description: str = "solo el diagrama") -> BoundingBox:
    client = crop_client(transport=fake)
    return asyncio.run(locate_region(client, IMAGE, "image/jpeg", description))


def test_valid_box_is_returned_from_one_observer_call() -> None:
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    box = _locate(fake)
    assert box == BoundingBox(**BOX)
    assert box.as_list() == [0.1, 0.2, 0.6, 0.75]
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.role == ROLE == "observer"
    assert request.model == Settings().llm.roles.observer.model
    assert request.prompt_hash == load_prompt(PROMPT_NAME).hash
    assert [tool["name"] for tool in request.tools] == [TOOL_NAME]
    assert request.tools[0]["strict"] is True


def test_request_carries_the_page_as_an_image_block_and_the_description() -> None:
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    _locate(fake, "  la tabla de abajo ")
    content = fake.requests[0].messages[0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(IMAGE).decode("ascii"),
        },
    }
    assert content[1]["type"] == "text"
    assert content[1]["text"].endswith("la tabla de abajo")


def test_system_prompt_asks_for_a_tight_box_without_margins_desk_or_fingers() -> None:
    fake = FakeClaude().reply_tool(TOOL_NAME, BOX)
    _locate(fake)
    system = " ".join(part["text"] for part in fake.requests[0].system).lower()
    assert "tight" in system
    for excluded in ("margins", "desk", "fingers"):
        assert excluded in system


@pytest.mark.parametrize(
    "bad",
    [
        {"x0": -0.1, "y0": 0.2, "x1": 0.6, "y1": 0.75},
        {"x0": 0.1, "y0": 0.2, "x1": 1.4, "y1": 0.75},
        {"x0": 0.6, "y0": 0.2, "x1": 0.1, "y1": 0.75},
        {"x0": 0.1, "y0": 0.5, "x1": 0.6, "y1": 0.5},
    ],
)
def test_out_of_bounds_box_is_re_asked_not_trusted(bad: dict[str, float]) -> None:
    fake = FakeClaude().reply_tool(TOOL_NAME, bad).reply_tool(TOOL_NAME, BOX)
    box = _locate(fake)
    assert box == BoundingBox(**BOX)
    assert len(fake.requests) == 2
    reask = fake.requests[1].messages[-1]["content"]
    assert reask[0]["type"] == "tool_result"
    assert reask[0]["is_error"] is True


def test_box_still_invalid_after_the_re_ask_is_a_domain_error() -> None:
    bad = {"x0": 0.5, "y0": 0.2, "x1": 0.5, "y1": 2.0}
    fake = FakeClaude()
    client = crop_client(transport=fake)
    for _ in range(1 + client.structured_reasks):
        fake.reply_tool(TOOL_NAME, bad)
    with pytest.raises(RegionNotFoundError, match="No he podido localizar"):
        asyncio.run(locate_region(client, IMAGE, "image/jpeg", "el diagrama"))
    assert len(fake.requests) == 1 + client.structured_reasks


def test_refusal_is_a_domain_error() -> None:
    fake = FakeClaude().reply_text("No puedo ayudar con eso.", stop_reason="refusal")
    with pytest.raises(RegionNotFoundError) as caught:
        _locate(fake)
    assert isinstance(caught.value, CropError)
    assert "No he podido localizar" in str(caught.value)
    assert len(fake.requests) == 1


def test_non_image_or_empty_description_is_refused_without_a_call() -> None:
    fake = FakeClaude()
    client = crop_client(transport=fake)
    with pytest.raises(CropError, match="no es una imagen"):
        asyncio.run(locate_region(client, b"%PDF-1.7", "application/pdf", "el diagrama"))
    with pytest.raises(CropError, match="Indica qué parte"):
        asyncio.run(locate_region(client, IMAGE, "image/png", "   "))
    assert fake.requests == []


def test_bounding_box_validation() -> None:
    assert BoundingBox(x0=0, y0=0, x1=1, y1=1).as_list() == [0, 0, 1, 1]
    with pytest.raises(ValidationError):
        BoundingBox(x0=0.2, y0=0, x1=0.1, y1=1)
    with pytest.raises(ValidationError):
        BoundingBox(x0=0, y0=0, x1=1, y1=1.01)
    with pytest.raises(ValidationError):
        BoundingBox.model_validate({**BOX, "extra": 1})
