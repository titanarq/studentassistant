"""The grounding check: generated items scored against the note sections they cite."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime

import pytest
import yaml

from material_generators import KIND, PointsGenerator, reply_points
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import DEFAULT_GROUNDING_MIN_SUPPORT, GeneratorsSettings
from studentassistant.editor.notes_format import parse
from studentassistant.generators import (
    ArtifactMeta,
    GeneratorContext,
    GeneratorOutput,
    GeneratorRegistry,
    NotesBasis,
    UngroundedItem,
    read_artifact_meta,
    run_generator,
)
from studentassistant.generators.grounding import (
    content_words,
    find_ungrounded,
    section_text,
    support,
    ungrounded_warning,
)
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, generated_directory

NOTES = (
    "# Derivadas\n"
    "\n"
    "## 1. Definición {#definicion}\n"
    "\n"
    "La **derivada** de f en a es el límite del cociente incremental cuando h tiende a 0.[^p1]\n"
    "\n"
    "### 1.1 Notación {#notacion}\n"
    "\n"
    "Se escribe f'(a) o df/dx.[^p1]\n"
    "\n"
    "## 2. Reglas {#reglas}\n"
    "\n"
    "- La regla de la cadena deriva funciones compuestas.[^p1]\n"
    "\n"
    "[^p1]: [Apuntes, página 7](../sources/notes/page-007.jpg)\n"
)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


# -- scoring ---------------------------------------------------------------------------------------


def test_content_words_fold_accents_drop_stop_words_and_keep_numbers() -> None:
    assert content_words("La derivada es el LÍMITE cuando h → 0, según los apuntes.") == {
        "derivada",
        "limite",
        "0",
    }
    assert content_words("") == set()


def test_a_grounded_text_is_fully_supported() -> None:
    section = "La derivada de f en a es el límite del cociente incremental."
    assert support("Es el límite del cociente incremental.", [section]) == 1.0
    # Accents, case and stop words do not matter.
    assert support("el LIMITE del Cociente Incremental", [section]) == 1.0


def test_a_partially_grounded_text_scores_its_share() -> None:
    section = "La derivada es el límite del cociente incremental."
    # limite, cociente found; integral, riemann not.
    assert support("El límite del cociente, como la integral de Riemann.", [section]) == 0.5
    # Numbers count: 6 is not in the section.
    assert support("El límite: 6.", [section]) == 0.5


def test_an_empty_text_claims_nothing() -> None:
    assert support("", ["La derivada."]) == 1.0
    assert support("es la de los", ["La derivada."]) == 1.0
    assert support("La derivada.", []) == 0.0


def test_a_section_includes_its_subsections_but_not_the_provenance_links() -> None:
    notes = parse(NOTES)
    text = section_text(notes, "definicion")
    assert text is not None
    assert "cociente incremental" in text and "df/dx" in text and "cadena" not in text
    assert "página 7" not in (section_text(notes, "reglas") or "")
    assert (section_text(notes, "notacion") or "").strip() == (
        "1.1 Notación\nSe escribe f'(a) o df/dx.[^p1]"
    )
    assert section_text(notes, "no-existe") is None


def test_find_ungrounded_skips_items_without_a_resolvable_anchor_or_text() -> None:
    notes = parse(NOTES)
    items = [
        ("a", ["definicion"]),  # grounded
        ("b", ["reglas"]),  # not grounded
        ("c", []),  # no anchor: `unresolved`'s
        ("d", ["no-existe"]),  # no anchor the notes have: `unresolved`'s
        ("e", ["reglas", "no-existe"]),  # scored on #reglas only
        ("f", ["reglas"]),  # no text given: not checked
    ]
    texts = {
        "a": "El límite del cociente incremental; se escribe df/dx.",
        "b": "Las integrales impropias convergen.",
        "c": "Nada que ver.",
        "d": "Nada que ver.",
        "e": "La regla de la cadena, las integrales y las series.",
    }
    found = find_ungrounded(notes, items, texts)
    assert found == [
        UngroundedItem(item="b", support=0.0, anchors=["reglas"]),
        UngroundedItem(item="e", support=0.5, anchors=["reglas"]),
    ]
    assert find_ungrounded(notes, items, texts, min_support=0.5) == found[:1]


def test_the_warning_is_spanish_and_lists_at_most_ten() -> None:
    one = [UngroundedItem(item="q3", support=0.5, anchors=["reglas"])]
    assert ungrounded_warning(one) == (
        "1 elemento no se apoya claramente en los apuntes: q3 (50 %, #reglas)."
        " Revísalos antes de estudiar con ellos."
    )
    many = [UngroundedItem(item=f"c{n}", support=0.1, anchors=["a"]) for n in range(12)]
    warning = ungrounded_warning(many)
    assert warning.startswith("12 elementos no se apoyan claramente")
    assert "c9 " in warning and "c10 " not in warning and "y 2 más" in warning


def test_the_threshold_is_configurable() -> None:
    assert GeneratorsSettings().grounding_min_support == DEFAULT_GROUNDING_MIN_SUPPORT == 0.6
    assert GeneratorsSettings(grounding_min_support=0.8).grounding_min_support == 0.8
    with pytest.raises(ValueError):
        GeneratorsSettings(grounding_min_support=1.5)


# -- manifest --------------------------------------------------------------------------------------


def _meta(**fields: object) -> dict[str, object]:
    return {
        "kind": "quiz",
        "generator_version": 1,
        "built_at": datetime(2026, 9, 25, tzinfo=UTC).isoformat(),
        "notes": NotesBasis(sha256="0" * 64).model_dump(mode="json"),
        "files": ["quiz.yaml"],
        **fields,
    }


def test_the_manifest_round_trips_ungrounded_and_old_ones_still_load() -> None:
    entry = {"item": "q1", "support": 0.25, "anchors": ["definicion"]}
    meta = ArtifactMeta.model_validate(_meta(ungrounded=[entry]))
    dumped = yaml.safe_load(yaml.safe_dump(meta.model_dump(mode="json"), allow_unicode=True))
    assert dumped["ungrounded"] == [entry]
    assert ArtifactMeta.model_validate(dumped) == meta

    old = ArtifactMeta.model_validate(_meta())  # written before the check existed
    assert old.ungrounded == []


# -- a run -----------------------------------------------------------------------------------------


class CheckedPointsGenerator(PointsGenerator):
    """The framework's sample generator, giving each point's title as the text to check."""

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        output = await super().generate(context)
        markdown = str(output.files[f"{KIND}.md"])
        points = [line.removeprefix("- ") for line in markdown.splitlines() if line[:2] == "- "]
        output.item_texts = {
            item.item: text for item, text in zip(output.items, points, strict=True)
        }
        return output


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def test_a_run_reports_the_ungrounded_item_in_the_manifest_and_a_warning(
    topic: ReviseTopic,
) -> None:
    registry = GeneratorRegistry()
    registry.register(CheckedPointsGenerator)
    fake = FakeClaude()
    reply_points(
        fake,
        ("La derivada es el límite del cociente incremental", ["definicion"]),
        ("Las integrales de Riemann son áreas", ["definicion"]),
        ("Un punto sin sección", []),
    )
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    result = _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            KIND,
            client=client,
            sync=GitSync(topic.vault),
            registry=registry,
        )
    )

    assert result.items == 3
    assert [entry.item for entry in result.unresolved] == ["p3"]
    assert result.ungrounded == [UngroundedItem(item="p2", support=0.0, anchors=["definicion"])]
    assert result.warnings[-1] == (
        "1 elemento no se apoya claramente en los apuntes: p2 (0 %, #definicion)."
        " Revísalos antes de estudiar con ellos."
    )
    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None and meta.ungrounded == result.ungrounded
    # Kept, never dropped.
    listed = (
        generated_directory(topic.vault, topic.subject, topic.topic) / f"{KIND}.md"
    ).read_text()
    assert "Las integrales de Riemann son áreas" in listed
