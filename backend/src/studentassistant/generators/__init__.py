"""Generators: outline, quiz, flashcards, exercises/exam, slides."""

# The framework (`base`, `registry`, `run`) runs a registered `Generator` over a topic's master
# notes, stores its artifact under `generated/` with a manifest recording the notes it was built
# from and the provenance of every item, and tells when an artifact is stale. Each built-in
# generator module registers itself on `default_registry` and is imported here.

import studentassistant.generators.exam  # noqa: F401 (registers itself)
import studentassistant.generators.slides  # noqa: F401 (registers itself)
from studentassistant.generators import flashcards  # noqa: F401 (registers itself)
from studentassistant.generators.base import (
    CONVERSATION_NAME,
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
    NoOptions,
    NotesBasis,
    NoteSection,
)
from studentassistant.generators.outline import OutlineGenerator as OutlineGenerator
from studentassistant.generators.registry import (
    GeneratorRegistry,
    UnknownGeneratorError,
    default_registry,
    register,
)
from studentassistant.generators.run import (
    MATERIAL_GENERATED_KIND,
    ArtifactMeta,
    ArtifactStatus,
    GenerateResult,
    GenerationError,
    GeneratorOutputError,
    InvalidOptionsError,
    MaterialsStatus,
    NoNotesError,
    artifact_status,
    materials_status,
    notes_basis,
    read_artifact_meta,
    run_generator,
)

__all__ = [
    "CONVERSATION_NAME",
    "MATERIAL_GENERATED_KIND",
    "ArtifactMeta",
    "ArtifactStatus",
    "GenerateResult",
    "GenerationError",
    "Generator",
    "GeneratorContext",
    "GeneratorOutput",
    "GeneratorOutputError",
    "GeneratorRegistry",
    "InvalidOptionsError",
    "ItemProvenance",
    "MaterialsStatus",
    "NoNotesError",
    "NoOptions",
    "NoteSection",
    "NotesBasis",
    "UnknownGeneratorError",
    "artifact_status",
    "default_registry",
    "materials_status",
    "notes_basis",
    "read_artifact_meta",
    "register",
    "run_generator",
]

from studentassistant.generators import quiz as quiz  # noqa: E402  (registers `quiz`)
