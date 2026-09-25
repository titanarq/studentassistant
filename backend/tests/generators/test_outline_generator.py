"""The `esquema` generator end to end: prompt, registration, `run_generator` with `FakeClaude`."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Awaitable

import pytest

from outline_drafts import DRAFT, GOLDEN, reply_outline
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.generators import (
    GenerateResult,
    OutlineGenerator,
    artifact_status,
    default_registry,
    read_artifact_meta,
    run_generator,
)
from studentassistant.generators.outline import (
    FILE_NAME,
    KIND,
    PROMPT_NAME,
    TOOL_NAME,
    OutlineDraft,
)
from studentassistant.llm import FakeClaude, LedgerBinding, load_prompt
from studentassistant.vault import GitSync, Vault, generated_directory, write_notes


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _generate(topic: ReviseTopic, fake: FakeClaude, sync: GitSync) -> GenerateResult:
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    return _run(
        run_generator(topic.vault, topic.subject, topic.topic, KIND, client=client, sync=sync)
    )


def _artifact(topic: ReviseTopic) -> str:
    path = generated_directory(topic.vault, topic.subject, topic.topic) / FILE_NAME
    return path.read_text(encoding="utf-8")


# -- prompt ----------------------------------------------------------------------------------------


def test_the_prompt_loads_and_its_example_parses_into_an_outline() -> None:
    prompt = load_prompt(PROMPT_NAME)
    assert prompt.hash.startswith("sha256:")
    assert "Spanish" in prompt.content and "anchors" in prompt.content
    [example] = re.findall(r"```json\n(.*?)```", prompt.content, flags=re.DOTALL)
    outline = OutlineDraft.model_validate(json.loads(example)).to_outline("Derivadas")
    assert [node.title for node in outline.nodes] == ["Definición de derivada", "Próximo día"]
    assert all(node.anchors for _, _, node in outline.walk())


# -- registration ----------------------------------------------------------------------------------


def test_registered_as_esquema_on_the_default_registry() -> None:
    assert default_registry.lookup(KIND) is OutlineGenerator
    assert isinstance(default_registry.get(KIND), OutlineGenerator)
    assert OutlineGenerator.title == "Esquema"


# -- running ---------------------------------------------------------------------------------------


def test_writes_esquema_md_with_the_notes_version(topic: ReviseTopic, sync: GitSync) -> None:
    sync.create_notes_tag(topic.subject, topic.topic)
    fake = FakeClaude()
    reply_outline(fake)

    result = _generate(topic, fake, sync)

    base = f"subjects/{topic.subject}/topics/{topic.topic}/generated"
    assert result.files == [f"{base}/esquema.md", f"{base}/esquema.meta.yaml"]
    assert result.notes.version == 1 and result.commit is not None
    assert result.items == 6 and result.unresolved == [] and result.warnings == []
    assert _artifact(topic) == GOLDEN.read_text(encoding="utf-8")

    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None
    assert meta.notes.version == 1 and meta.notes.tag == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert meta.prompt_hash == load_prompt(PROMPT_NAME).hash
    assert [(item.item, item.anchors) for item in meta.items][:2] == [
        ("1", ["definicion"]),
        ("1.1", ["definicion"]),
    ]

    # Claude was asked through the `generator` role, with the prompt and the notes' anchors.
    [request] = fake.requests
    assert request.role == "generator"
    assert request.prompt_hash == load_prompt(PROMPT_NAME).hash
    assert [tool["name"] for tool in request.tools] == [TOOL_NAME]
    assert "outline (esquema)" in json.dumps(request.system)
    assert "#definicion, #proximo-dia" in request.messages[0]["content"][0]["text"]


def test_inherits_stale_marking_when_the_notes_change(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    reply_outline(fake)
    _generate(topic, fake, sync)
    assert not artifact_status(topic.vault, topic.subject, topic.topic, KIND).stale

    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\n")

    status = artifact_status(topic.vault, topic.subject, topic.topic, KIND)
    assert status.stale and status.stale_reason == "Los apuntes han cambiado desde que se generó."


def test_nodes_without_a_resolvable_anchor_are_reported(topic: ReviseTopic, sync: GitSync) -> None:
    draft = copy.deepcopy(DRAFT)
    draft["nodes"][1]["anchors"] = []
    draft["nodes"][4]["anchors"] = ["inventada"]
    fake = FakeClaude()
    reply_outline(fake, draft)

    result = _generate(topic, fake, sync)

    assert [(item.item, item.anchors) for item in result.unresolved] == [
        ("1.1", []),
        ("2", ["inventada"]),
    ]
    [warning] = result.warnings
    assert "2 elementos no citan" in warning and "1.1 (sin ancla)" in warning
    text = _artifact(topic)
    # Kept in the outline, marked instead of linked.
    assert (
        "**1.1 Cociente incremental (f(a+h) - f(a)) / h** · *(sin sección de los apuntes)*" in text
    )
    assert "Apuntes: `#inventada` *(no está en los apuntes)*" in text
    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None and [item.item for item in meta.unresolved] == ["1.1", "2"]


def test_an_invalid_draft_is_asked_again_once(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    reply_outline(
        fake,
        {
            "nodes": [
                {
                    "id": "n1",
                    "parent": "nx",
                    "title": "Idea",
                    "gloss": None,
                    "anchors": ["definicion"],
                }
            ]
        },
    )
    reply_outline(fake)

    result = _generate(topic, fake, sync)

    assert len(fake.requests) == 2 and result.items == 6
    assert "not an earlier node" in json.dumps(fake.requests[1].messages[-1])
