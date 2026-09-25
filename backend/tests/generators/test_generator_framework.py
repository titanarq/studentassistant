"""The generator framework: registry, `run_generator`, the manifest and stale detection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from typing import ClassVar

import pytest
import yaml

from material_generators import (
    DEFAULT_POINTS,
    KIND,
    PointsGenerator,
    points_registry,
    reply_points,
)
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import LlmSettings, Settings
from studentassistant.generators import (
    CONVERSATION_NAME,
    MATERIAL_GENERATED_KIND,
    Generator,
    GeneratorContext,
    GeneratorOutput,
    GeneratorOutputError,
    GeneratorRegistry,
    InvalidOptionsError,
    NoNotesError,
    UnknownGeneratorError,
    artifact_status,
    materials_status,
    read_artifact_meta,
    run_generator,
)
from studentassistant.llm import (
    CostConfirmationRequiredError,
    FakeClaude,
    LedgerBinding,
    LLMClient,
)
from studentassistant.vault import (
    GitSync,
    Vault,
    create_topic,
    generated_directory,
    read_ledger,
    write_notes,
)


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


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


def _client(fake: FakeClaude, topic: ReviseTopic, settings: Settings | None = None) -> LLMClient:
    return fake.client(
        "generator",
        settings=settings,
        ledger=LedgerBinding(topic.vault, topic.subject, topic.topic),
    )


def _generate(
    topic: ReviseTopic,
    fake: FakeClaude,
    sync: GitSync,
    registry: GeneratorRegistry | None = None,
    kind: str = KIND,
    **options: object,
):
    return _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            kind,
            client=_client(fake, topic),
            sync=sync,
            registry=registry or points_registry(),
            options=options,
        )
    )


def _generated(topic: ReviseTopic, name: str) -> str:
    return (generated_directory(topic.vault, topic.subject, topic.topic) / name).read_text()


# -- registry --------------------------------------------------------------------------------------


def test_the_registry_finds_and_lists_generators() -> None:
    registry = points_registry()
    assert registry.kinds() == [KIND]
    assert KIND in registry and "otro" not in registry
    assert isinstance(registry.get(KIND), PointsGenerator)
    assert registry.lookup("otro") is None
    with pytest.raises(UnknownGeneratorError, match="Disponibles: prueba"):
        registry.get("otro")


def test_the_registry_refuses_a_bad_kind_or_a_second_class() -> None:
    registry = points_registry()
    registry.register(PointsGenerator)  # the same class again is fine

    class Other(PointsGenerator):
        pass

    with pytest.raises(ValueError, match="already registered"):
        registry.register(Other)

    class Bad(PointsGenerator):
        kind = "Mal Nombre"

    with pytest.raises(ValueError, match="not a generator kind"):
        registry.register(Bad)


# -- running ---------------------------------------------------------------------------------------


def test_generates_writes_the_artifact_and_manifest_and_commits(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    sync.create_notes_tag(topic.subject, topic.topic)
    reply_points(fake, *DEFAULT_POINTS)

    result = _generate(topic, fake, sync)

    base = f"subjects/{topic.subject}/topics/{topic.topic}/generated"
    assert result.files == [f"{base}/prueba.md", f"{base}/prueba.meta.yaml"]
    assert result.notes.version == 1 and not result.notes.changed_since_version
    assert result.items == 2 and result.unresolved == [] and result.warnings == []
    assert result.commit is not None
    assert _generated(topic, "prueba.md") == (
        "# Puntos\n\n- La derivada es un límite\n- Se verá la regla de la cadena\n"
    )
    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None
    assert meta.generator_version == 1 and meta.files == ["prueba.md"]
    assert meta.notes.tag == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert [(item.item, item.anchors) for item in meta.items] == [
        ("p1", ["definicion"]),
        ("p2", ["proximo-dia"]),
    ]
    assert meta.options == {"size": 3, "split": False}
    assert meta.model == "claude-opus-5-5"
    # The manifest is plain YAML a reader of the vault can open.
    raw = yaml.safe_load(_generated(topic, "prueba.meta.yaml"))
    assert raw["kind"] == KIND and raw["notes"]["version"] == 1
    # Committed: nothing of generated/ left pending (the last conversation record goes with the
    # next batch commit).
    assert "generated/" not in sync.git.check("status", "--porcelain").stdout
    subject = sync.git.check("log", "-1", "--format=%s").stdout.strip()
    assert subject == f"Generar prueba de {topic.subject}/{topic.topic} (apuntes v1)"
    # Through the generator role, capped and recorded in the topic's ledger.
    assert fake.requests[0].role == "generator"
    assert [entry.role for entry in read_ledger(topic.vault, topic.subject, topic.topic)] == [
        "generator"
    ]
    # The notes and their anchors are what the generator sent.
    sent = fake.requests[0].messages[0]["content"][0]["text"]
    assert "#definicion, #proximo-dia" in sent and "**Derivada**" in sent


def test_every_call_is_recorded_in_the_generator_conversation(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    _generate(topic, fake, sync)

    path = (
        topic.vault.path
        / f"subjects/{topic.subject}/topics/{topic.topic}/conversations/{CONVERSATION_NAME}.jsonl"
    )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in records] == ["user", "assistant", MATERIAL_GENERATED_KIND]
    assert all(record["detail"]["kind"] == KIND for record in records)
    assert records[1]["usage"] is not None


def test_untagged_notes_still_generate(topic: ReviseTopic, fake: FakeClaude, sync: GitSync) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    result = _generate(topic, fake, sync)
    assert result.notes.version is None and result.notes.tag is None
    assert len(result.notes.sha256) == 64


def test_notes_revised_since_the_tag_are_recorded_as_such(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    sync.create_notes_tag(topic.subject, topic.topic)
    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\nMás.[^p1]\n")
    reply_points(fake, *DEFAULT_POINTS)

    result = _generate(topic, fake, sync)

    assert result.notes.version == 1 and result.notes.changed_since_version


def test_items_without_a_resolvable_anchor_are_reported(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(
        fake,
        ("Bien", ["definicion"]),
        ("Ancla inventada", ["definicion", "no-existe"]),
        ("Sin ancla", []),
    )

    result = _generate(topic, fake, sync)

    assert [(item.item, item.anchors) for item in result.unresolved] == [
        ("p2", ["no-existe"]),
        ("p3", []),
    ]
    assert len(result.warnings) == 1 and "2 elementos" in result.warnings[0]
    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None and meta.unresolved == result.unresolved
    # Reported, not dropped: the artifact holds all three.
    assert _generated(topic, "prueba.md").count("\n- ") == 3


def test_options_are_validated_and_used(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    with pytest.raises(InvalidOptionsError, match="size"):
        _generate(topic, fake, sync, size=99)
    with pytest.raises(InvalidOptionsError, match="desconocida"):
        _generate(topic, fake, sync, desconocida=1)
    assert fake.requests == []

    reply_points(fake, *DEFAULT_POINTS)
    result = _generate(topic, fake, sync, size=1, split=True)

    assert result.items == 1
    assert _generated(topic, "prueba/1.md") == "La derivada es un límite\n"


def test_files_of_the_previous_run_not_written_again_are_removed(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    _generate(topic, fake, sync, split=True)
    reply_points(fake, *DEFAULT_POINTS)

    result = _generate(topic, fake, sync)

    base = f"subjects/{topic.subject}/topics/{topic.topic}/generated"
    assert result.removed == [f"{base}/prueba/1.md", f"{base}/prueba/2.md"]
    assert not (generated_directory(topic.vault, topic.subject, topic.topic) / KIND).exists()
    assert "generated/" not in sync.git.check("status", "--porcelain").stdout


def test_a_topic_without_notes_is_refused(
    tmp_vault: Vault, sync: GitSync, fake: FakeClaude
) -> None:
    topic = make_revise_topic(tmp_vault)
    empty = create_topic(tmp_vault, topic.subject, "Integrales").slug
    with pytest.raises(NoNotesError, match="aún no tiene apuntes"):
        _run(
            run_generator(
                tmp_vault,
                topic.subject,
                empty,
                KIND,
                client=fake.client("generator"),
                sync=sync,
                registry=points_registry(),
            )
        )
    assert fake.requests == []


def test_an_unknown_kind_is_refused(topic: ReviseTopic, fake: FakeClaude, sync: GitSync) -> None:
    with pytest.raises(UnknownGeneratorError):
        _generate(topic, fake, sync, kind="otro")


def test_a_reached_cost_cap_writes_nothing(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    client = _client(fake, topic, Settings(llm=LlmSettings(max_usd_per_day=0)))
    with pytest.raises(CostConfirmationRequiredError):
        _run(
            run_generator(
                topic.vault,
                topic.subject,
                topic.topic,
                KIND,
                client=client,
                sync=sync,
                registry=points_registry(),
            )
        )
    assert not generated_directory(topic.vault, topic.subject, topic.topic).exists()


class _Writes(Generator):
    kind = "escribe"
    title = "Escribe"
    files: ClassVar[dict[str, str | bytes]] = {}

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        return GeneratorOutput(files=dict(self.files))


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"otro.md": "x"},
        {"escribe.meta.yaml": "x"},
        {"escribe/../fuera.md": "x"},
        {"escribe-a.meta.yaml": "x"},
    ],
)
def test_output_the_framework_refuses(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync, files: dict[str, str]
) -> None:
    registry = GeneratorRegistry()
    registry.register(type("W", (_Writes,), {"files": files}))
    with pytest.raises(GeneratorOutputError):
        _generate(topic, fake, sync, registry=registry, kind="escribe")


def test_binary_and_prefixed_files_are_written(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    registry = GeneratorRegistry()
    registry.register(
        type("W", (_Writes,), {"files": {"escribe.bin": b"\x00\x01", "escribe-2.csv": "a;b\n"}})
    )
    result = _generate(topic, fake, sync, registry=registry, kind="escribe")
    directory = generated_directory(topic.vault, topic.subject, topic.topic)
    assert (directory / "escribe.bin").read_bytes() == b"\x00\x01"
    assert result.items == 0 and result.unresolved == []


# -- stale detection -------------------------------------------------------------------------------


def test_an_artifact_goes_stale_when_the_notes_change(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    registry = points_registry()
    before = artifact_status(topic.vault, topic.subject, topic.topic, KIND, registry=registry)
    assert not before.generated and not before.stale and before.title == "Puntos"

    reply_points(fake, *DEFAULT_POINTS)
    _generate(topic, fake, sync, registry)
    fresh = artifact_status(topic.vault, topic.subject, topic.topic, KIND, registry=registry)
    assert fresh.generated and not fresh.stale and fresh.stale_reason is None
    assert fresh.files == [f"subjects/{topic.subject}/topics/{topic.topic}/generated/prueba.md"]

    write_notes(topic.vault, topic.subject, topic.topic, topic.notes.replace("límite", "LÍMITE"))
    stale = artifact_status(topic.vault, topic.subject, topic.topic, KIND, registry=registry)
    assert stale.stale and stale.stale_reason == "Los apuntes han cambiado desde que se generó."

    # Back to the same text: fresh again (the hash is of the content, not of the commit).
    write_notes(topic.vault, topic.subject, topic.topic, topic.notes)
    again = artifact_status(topic.vault, topic.subject, topic.topic, KIND, registry=registry)
    assert not again.stale


def test_an_artifact_of_an_older_generator_version_is_stale(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    _generate(topic, fake, sync)

    class Newer(PointsGenerator):
        version = 2

    registry = GeneratorRegistry()
    registry.register(Newer)
    status = artifact_status(topic.vault, topic.subject, topic.topic, KIND, registry=registry)
    assert status.stale and "generador ha cambiado" in (status.stale_reason or "")


def test_the_materials_status_lists_every_kind(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    _generate(topic, fake, sync)
    registry = GeneratorRegistry()
    registry.register(_Writes)

    status = materials_status(topic.vault, topic.subject, topic.topic, registry=registry)

    assert status.has_notes and status.notes_sha256 is not None
    assert [(a.kind, a.generated, a.title) for a in status.artifacts] == [
        ("escribe", False, "Escribe"),
        # No longer registered, but its manifest is there.
        (KIND, True, None),
    ]


def test_a_broken_manifest_reads_as_stale(
    topic: ReviseTopic, fake: FakeClaude, sync: GitSync
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    _generate(topic, fake, sync)
    directory = generated_directory(topic.vault, topic.subject, topic.topic)
    (directory / "prueba.meta.yaml").write_text("kind: [", encoding="utf-8")

    status = artifact_status(
        topic.vault, topic.subject, topic.topic, KIND, registry=points_registry()
    )

    assert status.generated and status.stale and "No se puede leer" in (status.stale_reason or "")
