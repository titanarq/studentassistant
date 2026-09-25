"""Running a generator, storing its artifact in `generated/` and telling when it went stale.

`run_generator` reads the topic's `notes/apuntes.md`, records which notes they are (`NotesBasis`:
the SHA-256 of the text, the newest `apuntes-vN` tag and whether the text still is that version),
runs the generator of the kind asked for, checks its output and stores it:

- the files, under `generated/` (`vault.write_generated`); a file the previous run of the same
  kind wrote and this one did not is removed, so nothing stale of it is left behind;
- the manifest `generated/<kind>.meta.yaml` (`ArtifactMeta`): the generator's version, when and
  with which model it was built, the options, the notes basis, the files, the provenance of every
  item (the note anchors it came from) and the items whose anchors the notes do not have
  (`unresolved`: reported, never dropped silently);

and commits them in one checkpoint. Every call is recorded in the topic's
`conversations/generator.jsonl` and, through the client's `LedgerBinding`, in its cost ledger.

Staleness is computed, never stored: an artifact is stale when the current `apuntes.md` is not
the text it was built from (`sha256` differs, or the notes are gone), or when its generator's
`version` is newer than the one that built it (`artifact_status`, `materials_status`).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.editor.notes_format import parse
from studentassistant.generators.base import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
    NotesBasis,
)
from studentassistant.generators.registry import GeneratorRegistry, default_registry
from studentassistant.llm import LLMClient
from studentassistant.vault import (
    GitSync,
    NotesError,
    Vault,
    check_generated_name,
    generated_directory,
    get_topic,
    list_generated,
    notes_path,
    read_generated,
    read_notes,
    remove_generated,
    write_generated,
)

META_SUFFIX = ".meta.yaml"
MATERIAL_GENERATED_KIND = "material.generated"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- errors ----------------------------------------------------------------------------------------


class GenerationError(ValueError):
    """A generation that cannot be done; the message is Spanish, for the student."""


class NoNotesError(GenerationError):
    def __init__(self) -> None:
        super().__init__(
            "Este tema aún no tiene apuntes: genera primero los apuntes («Prepárame el tema»)."
        )


class InvalidOptionsError(GenerationError):
    def __init__(self, kind: str, problems: str) -> None:
        super().__init__(f"Opciones no válidas para «{kind}»: {problems}.")


def _validate_options(generator: Generator, options: Mapping[str, Any]) -> BaseModel:
    """The generator's options model from `options`; an unknown key is refused too."""
    model = generator.options_model
    unknown = sorted(set(options) - set(model.model_fields))
    if unknown:
        raise InvalidOptionsError(
            generator.kind, "opción desconocida " + ", ".join(f"«{key}»" for key in unknown)
        )
    try:
        return model.model_validate(dict(options))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in failure['loc']) or 'opciones'}: {failure['msg']}"
            for failure in error.errors()
        )
        raise InvalidOptionsError(generator.kind, problems) from error


class GeneratorOutputError(RuntimeError):
    """A generator returned something the framework refuses to store (a bug in the generator)."""


# -- models ----------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactMeta(_Strict):
    """`generated/<kind>.meta.yaml`: how an artifact was built and from which notes."""

    kind: str
    generator_version: int
    built_at: datetime
    model: str | None = None
    prompt_hash: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    notes: NotesBasis
    files: list[str] = Field(description="Names relative to `generated/`, sorted.")
    items: list[ItemProvenance] = Field(default_factory=list)
    unresolved: list[ItemProvenance] = Field(
        default_factory=list,
        description="Items with no anchor, or anchors the notes did not have (then listed).",
    )
    warnings: list[str] = Field(default_factory=list)


class GenerateResult(_Strict):
    """What one `run_generator` stored; also the `material.generated` conversation record."""

    subject: str
    topic: str
    kind: str
    files: list[str] = Field(description="Vault-relative paths written, the manifest last.")
    removed: list[str] = Field(default_factory=list, description="Vault-relative paths removed.")
    notes: NotesBasis
    commit: str | None = None
    items: int
    unresolved: list[ItemProvenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, description="Spanish, for the student.")
    model: str | None = None


class ArtifactStatus(_Strict):
    """One kind of material of a topic: whether it exists and whether it is stale."""

    kind: str
    title: str | None = Field(default=None, description="None for a kind no longer registered.")
    generated: bool
    stale: bool = False
    stale_reason: str | None = Field(default=None, description="Spanish, for the student.")
    files: list[str] = Field(default_factory=list, description="Vault-relative paths.")
    meta: ArtifactMeta | None = None


class MaterialsStatus(_Strict):
    subject: str
    topic: str
    has_notes: bool
    notes_sha256: str | None = None
    artifacts: list[ArtifactStatus]
    """Every registered kind (sorted), then any other kind that has a manifest."""


# -- notes basis -----------------------------------------------------------------------------------


def notes_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def notes_basis(vault: Vault, subject: str, topic: str, text: str, sync: GitSync) -> NotesBasis:
    """The basis of `text` (the current `apuntes.md`): hash, newest tag, whether it is that tag."""
    tags = sync.list_notes_tags(subject, topic)
    if not tags:
        return NotesBasis(sha256=notes_sha256(text))
    latest = tags[-1]
    relative = notes_path(vault, subject, topic).relative_to(vault.path).as_posix()
    tagged = sync.read_file_at(latest.commit, relative)
    return NotesBasis(
        sha256=notes_sha256(text),
        version=latest.version,
        tag=latest.name,
        changed_since_version=tagged != text,
    )


# -- running ---------------------------------------------------------------------------------------


def meta_name(kind: str) -> str:
    return f"{kind}{META_SUFFIX}"


def _check_output(kind: str, output: GeneratorOutput) -> None:
    if not output.files:
        raise GeneratorOutputError(f"generator {kind!r} returned no files")
    for name in output.files:
        try:
            check_generated_name(name)
        except NotesError as error:
            raise GeneratorOutputError(f"generator {kind!r}: {error}") from error
        if name == meta_name(kind) or name.endswith(META_SUFFIX):
            raise GeneratorOutputError(f"generator {kind!r} may not write {name!r}")
        if not (name.startswith(f"{kind}.") or name.startswith(f"{kind}-")) and not (
            name.startswith(f"{kind}/")
        ):
            raise GeneratorOutputError(
                f"generator {kind!r} wrote {name!r}: its files must start with {kind!r}"
            )


def _unresolved(items: list[ItemProvenance], anchors: set[str]) -> list[ItemProvenance]:
    """Each item with no anchor (listed as is) or with anchors the notes lack (those listed)."""
    found = []
    for item in items:
        if not item.anchors:
            found.append(item)
            continue
        missing = [anchor for anchor in item.anchors if anchor not in anchors]
        if missing:
            found.append(ItemProvenance(item=item.item, anchors=missing))
    return found


def _unresolved_warning(unresolved: list[ItemProvenance]) -> str:
    listing = ", ".join(
        f"{item.item} ({', '.join('#' + a for a in item.anchors) or 'sin ancla'})"
        for item in unresolved[:10]
    )
    more = f" y {len(unresolved) - 10} más" if len(unresolved) > 10 else ""
    count = len(unresolved)
    subject = "1 elemento no cita" if count == 1 else f"{count} elementos no citan"
    return f"{subject} una sección existente de los apuntes: {listing}{more}."


def read_artifact_meta(vault: Vault, subject: str, topic: str, kind: str) -> ArtifactMeta | None:
    """The manifest of `kind`, or `None` when there is none.

    Raises:
        GenerationError: the manifest exists but is not an `ArtifactMeta`.
        VaultError: the topic cannot be read.
    """
    data = read_generated(vault, subject, topic, meta_name(kind))
    if data is None:
        return None
    try:
        return ArtifactMeta.model_validate(yaml.safe_load(data.decode("utf-8")))
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError) as error:
        raise GenerationError(f"No se puede leer el registro de «{kind}»: {error}") from error


def _save(
    vault: Vault,
    sync: GitSync,
    subject: str,
    topic: str,
    meta: ArtifactMeta,
    files: Mapping[str, str | bytes],
    version_label: str,
) -> tuple[list[str], list[str], str | None]:
    """Write the files and the manifest, remove leftovers, commit; blocking."""
    root = vault.path
    try:
        previous = read_artifact_meta(vault, subject, topic, meta.kind)
    except GenerationError:
        previous = None
    removed = []
    for name in previous.files if previous is not None else []:
        if name not in files and remove_generated(vault, subject, topic, name):
            removed.append(
                (generated_directory(vault, subject, topic) / name).relative_to(root).as_posix()
            )
    written = []
    for name in sorted(files):
        path = write_generated(vault, subject, topic, name, files[name])
        written.append(path.relative_to(root).as_posix())
    document = yaml.safe_dump(
        meta.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=1000
    )
    path = write_generated(vault, subject, topic, meta_name(meta.kind), document)
    written.append(path.relative_to(root).as_posix())
    sync.note_change()
    commit = sync.checkpoint(f"Generar {meta.kind} de {subject}/{topic} ({version_label})")
    return written, removed, commit


async def run_generator(
    vault: Vault,
    subject: str,
    topic: str,
    kind: str,
    *,
    client: LLMClient,
    sync: GitSync,
    registry: GeneratorRegistry = default_registry,
    options: Mapping[str, Any] | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
) -> GenerateResult:
    """Run the generator of `kind` over the topic's notes, store the artifact and commit it.

    `client` is a `generator` client (`get_client("generator", ledger=LedgerBinding(...))`; tests
    use `FakeClaude`); `options` are validated against the generator's `options_model`.

    Raises:
        UnknownGeneratorError: no generator of `kind` (checked first).
        InvalidOptionsError: `options` do not fit the generator.
        NoNotesError: the topic has no `notes/apuntes.md`.
        GeneratorOutputError: the generator returned files the framework refuses.
        CostConfirmationRequiredError, RefusalError, StructuredOutputError, LLMError: from Claude;
            nothing is written.
        VaultError: the topic cannot be read or written.
    """
    generator: Generator = registry.get(kind)
    validated = _validate_options(generator, options or {})
    stored = await asyncio.to_thread(get_topic, vault, subject, topic)
    text = await asyncio.to_thread(read_notes, vault, subject, topic)
    if text is None:
        raise NoNotesError()
    basis = await asyncio.to_thread(notes_basis, vault, subject, topic, text, sync)
    context = GeneratorContext(
        vault=vault,
        subject=subject,
        topic=topic,
        topic_title=stored.topic.title,
        notes_text=text,
        notes=parse(text),
        basis=basis,
        options=validated,
        client=client,
        kind=kind,
        confirm_over_cap=confirm_over_cap,
        clock=clock,
    )
    output = await generator.generate(context)
    _check_output(kind, output)
    unresolved = _unresolved(output.items, set(context.anchors))
    warnings = list(output.warnings)
    if unresolved:
        warnings.append(_unresolved_warning(unresolved))
    meta = ArtifactMeta(
        kind=kind,
        generator_version=generator.version,
        built_at=clock(),
        model=output.model,
        prompt_hash=output.prompt_hash,
        options=validated.model_dump(mode="json"),
        notes=basis,
        files=sorted(output.files),
        items=output.items,
        unresolved=unresolved,
        warnings=warnings,
    )
    label = f"apuntes v{basis.version}" if basis.version else "apuntes sin versión"
    if basis.version and basis.changed_since_version:
        label += " con cambios"
    written, removed, commit = await asyncio.to_thread(
        _save, vault, sync, subject, topic, meta, output.files, label
    )
    result = GenerateResult(
        subject=subject,
        topic=topic,
        kind=kind,
        files=written,
        removed=removed,
        notes=basis,
        commit=commit,
        items=len(output.items),
        unresolved=unresolved,
        warnings=warnings,
        model=output.model,
    )
    await context.record(
        MATERIAL_GENERATED_KIND,
        model=output.model,
        prompt_hash=output.prompt_hash,
        detail=result.model_dump(mode="json"),
    )
    sync.note_change()  # that last record goes with the next batch commit
    return result


# -- status ----------------------------------------------------------------------------------------


def _stale_reason(meta: ArtifactMeta, current_sha: str | None, version: int | None) -> str | None:
    if current_sha is None:
        return "Los apuntes de este tema ya no existen."
    if meta.notes.sha256 != current_sha:
        return "Los apuntes han cambiado desde que se generó."
    if version is not None and meta.generator_version < version:
        return "El generador ha cambiado desde que se generó."
    return None


def artifact_status(
    vault: Vault,
    subject: str,
    topic: str,
    kind: str,
    *,
    registry: GeneratorRegistry = default_registry,
    current_sha: str | None = None,
    notes_read: bool = False,
) -> ArtifactStatus:
    """Whether `kind` was generated for the topic and whether it is stale (blocking, reads only).

    `current_sha`/`notes_read` let a caller that already hashed the notes skip reading them again.
    """
    if not notes_read:
        text = read_notes(vault, subject, topic)
        current_sha = None if text is None else notes_sha256(text)
    generator = registry.lookup(kind)
    title = generator.title if generator is not None else None
    version = generator.version if generator is not None else None
    try:
        meta = read_artifact_meta(vault, subject, topic, kind)
    except GenerationError as error:
        return ArtifactStatus(
            kind=kind, title=title, generated=True, stale=True, stale_reason=str(error)
        )
    if meta is None:
        return ArtifactStatus(kind=kind, title=title, generated=False)
    reason = _stale_reason(meta, current_sha, version)
    directory = generated_directory(vault, subject, topic).relative_to(vault.path).as_posix()
    return ArtifactStatus(
        kind=kind,
        title=title,
        generated=True,
        stale=reason is not None,
        stale_reason=reason,
        files=[f"{directory}/{name}" for name in meta.files],
        meta=meta,
    )


def materials_status(
    vault: Vault, subject: str, topic: str, *, registry: GeneratorRegistry = default_registry
) -> MaterialsStatus:
    """Every kind of material of the topic, generated or not, stale or not (blocking)."""
    get_topic(vault, subject, topic)
    text = read_notes(vault, subject, topic)
    current_sha = None if text is None else notes_sha256(text)
    kinds = registry.kinds()
    directory = generated_directory(vault, subject, topic).relative_to(vault.path).as_posix()
    for path in list_generated(vault, subject, topic):
        name = path.removeprefix(directory + "/")
        if "/" not in name and name.endswith(META_SUFFIX):
            kind = name.removesuffix(META_SUFFIX)
            if kind not in kinds:
                kinds.append(kind)
    return MaterialsStatus(
        subject=subject,
        topic=topic,
        has_notes=text is not None,
        notes_sha256=current_sha,
        artifacts=[
            artifact_status(
                vault,
                subject,
                topic,
                kind,
                registry=registry,
                current_sha=current_sha,
                notes_read=True,
            )
            for kind in kinds
        ],
    )
