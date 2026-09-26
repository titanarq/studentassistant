"""The student editing the notes directly (`editor.direct_edit`): normalisation, stale bases,
validation, the commit, the conversation record and the event."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.direct_edit import (
    NOTES_EDITED_KIND,
    STUDENT_EDIT_RECORD,
    NotesChangedError,
    StudentEditInvalidError,
    normalise_student_text,
    save_student_edit,
)
from studentassistant.editor.notes_format import (
    notes_revision,
    parse,
    topic_source_resolver,
    validate,
)
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    put_pasted_image,
    read_conversation,
    read_notes,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def _notes(topic: ReviseTopic) -> str:
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    return notes


# -- normalisation ---------------------------------------------------------------------------------


def test_valid_notes_come_back_byte_for_byte(topic: ReviseTopic) -> None:
    assert normalise_student_text(topic.notes) == topic.notes


def test_normalising_adds_anchors_est_footnotes_and_image_footnotes() -> None:
    text = (
        "# Tema\n"
        "\n"
        "Intro del estudiante.\n"
        "\n"
        "## Causas\n"
        "\n"
        "Con fuente.[^p1]\n"
        "\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "\n"
        "- uno\n"
        "- dos\n"
        "\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\n"
        "![Imagen pegada 1](../sources/images/img-001.png)\n"
        "\n"
        "## Causas\n"
        "\n"
        "Otra.\n"
    )

    normalised = normalise_student_text(text)

    document = parse(normalised)
    assert [section.anchor for section in document.sections] == ["causas", "causas-2"]
    assert document.preamble[1].text == "Intro del estudiante.[^est]"
    blocks = document.section("causas").blocks
    assert blocks[0].text == "Con fuente.[^p1]"
    assert blocks[1].text == "- uno\n- dos[^est]"
    assert blocks[2].text.splitlines()[-1] == "| 1 | 2 [^est] |"
    assert blocks[3].text == "![Imagen pegada 1](../sources/images/img-001.png)[^img001]"
    final = document.sections[-1].blocks[-1]
    assert final.kind == "footnotes"
    assert [d.label for d in final.definitions] == ["p1", "est", "img001"]
    assert final.definitions[1].text == "Escrito por el estudiante"
    assert final.definitions[2].text == "[Imagen pegada 1](../sources/images/img-001.png)"
    # Only the final run holds definitions now.
    assert sum(1 for s in document.sections for b in s.blocks if b.kind == "footnotes") == 1
    assert validate(normalised, "estricto") == []
    assert normalise_student_text(normalised) == normalised


def test_an_image_block_already_citing_its_image_is_left_alone() -> None:
    text = (
        "# T\n\n## A {#a}\n\n![Imagen pegada 2](../sources/images/img-002.webp)[^i]\n\n"
        "[^i]: [Imagen pegada 2](../sources/images/img-002.webp)\n"
    )
    assert normalise_student_text(text) == text


def test_the_validator_accepts_est_in_both_modes_and_an_existing_pasted_image(
    topic: ReviseTopic,
) -> None:
    path = put_pasted_image(topic.vault, topic.subject, topic.topic, PNG, "image/png")
    assert path.name == "img-001.png"
    text = normalise_student_text(
        topic.notes.replace(
            "## 2. Próximo día {#proximo-dia}\n\n",
            "## 2. Próximo día {#proximo-dia}\n\nMi resumen.\n\n"
            "![Imagen pegada 1](../sources/images/img-001.png)\n\n",
        )
    )
    resolver = topic_source_resolver(topic.vault, topic.subject, topic.topic)
    assert validate(text, "estricto", resolver) == []
    assert validate(text, "ampliado", resolver) == []
    missing = text.replace("img-001.png", "img-009.png")
    assert any("img-009" in error for error in validate(missing, "estricto", resolver))


# -- saving --------------------------------------------------------------------------------


def test_a_save_commits_records_and_publishes_the_edit(topic: ReviseTopic, sync: GitSync) -> None:
    events = Sink()
    edited = topic.notes.replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^t2]\n\nMío.")

    result = _run(
        save_student_edit(
            topic.vault,
            topic.subject,
            topic.topic,
            edited,
            notes_revision(topic.notes),
            sync=sync,
            on_event=events,
        )
    )

    assert result.normalised and "Mío.[^est]" in result.notes
    assert "[^est]: Escrito por el estudiante" in result.notes
    assert _notes(topic) == result.notes
    assert result.revision == notes_revision(result.notes)
    assert result.changed_sections == ["definicion"]
    assert "+Mío.[^est]" in result.diff and result.commit
    assert _git(topic.vault, "log", "-1", "--format=%s").strip() == (
        f"Apuntes de {topic.subject}/{topic.topic} editados por el estudiante"
    )
    records = [
        r
        for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")
        if r.kind == STUDENT_EDIT_RECORD
    ]
    assert len(records) == 1 and records[0].detail is not None
    assert records[0].detail["changed_sections"] == ["definicion"]
    assert "notes" not in records[0].detail
    [(kind, payload)] = events.items
    assert kind == NOTES_EDITED_KIND and payload["origin"] == "user"
    assert payload["revision"] == result.revision and "notes" not in payload


def test_a_stale_base_revision_writes_nothing(topic: ReviseTopic, sync: GitSync) -> None:
    head = _git(topic.vault, "rev-parse", "HEAD")

    with pytest.raises(NotesChangedError) as caught:
        _run(
            save_student_edit(
                topic.vault,
                topic.subject,
                topic.topic,
                topic.notes + "\nOtra cosa.\n",
                notes_revision("otra versión"),
                sync=sync,
            )
        )

    assert caught.value.text == topic.notes
    assert caught.value.revision == notes_revision(topic.notes)
    assert _notes(topic) == topic.notes
    assert _git(topic.vault, "rev-parse", "HEAD") == head


def test_text_that_breaks_the_format_is_refused_with_spanish_errors(
    topic: ReviseTopic, sync: GitSync
) -> None:
    broken = topic.notes.replace("Se escribe $f'(x)$.[^t2]", "Se escribe.[^nada]")

    with pytest.raises(StudentEditInvalidError) as caught:
        _run(
            save_student_edit(
                topic.vault,
                topic.subject,
                topic.topic,
                broken,
                notes_revision(topic.notes),
                sync=sync,
            )
        )

    assert any("[^nada]" in error for error in caught.value.errors)
    assert _notes(topic) == topic.notes


def test_a_topic_without_notes_is_started_from_a_null_base(tmp_vault: Vault) -> None:
    sync = GitSync(tmp_vault)
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "Revolución").slug
    sync.checkpoint("fixture")

    result = _run(
        save_student_edit(
            tmp_vault, subject, topic, "# Revolución\n\n## Causas\n\nHambre.\n", None, sync=sync
        )
    )

    assert result.notes == (
        "# Revolución\n\n## Causas {#causas}\n\nHambre.[^est]\n\n"
        "[^est]: Escrito por el estudiante\n"
    )
    assert read_notes(tmp_vault, subject, topic) == result.notes
    with pytest.raises(NotesChangedError):
        _run(save_student_edit(tmp_vault, subject, topic, "# R\n", None, sync=sync))


def test_saving_the_same_text_writes_nothing(topic: ReviseTopic, sync: GitSync) -> None:
    head = _git(topic.vault, "rev-parse", "HEAD")
    events = Sink()

    result = _run(
        save_student_edit(
            topic.vault,
            topic.subject,
            topic.topic,
            topic.notes,
            notes_revision(topic.notes),
            sync=sync,
            on_event=events,
        )
    )

    assert not result.notes_changed and result.commit is None and not result.normalised
    assert _git(topic.vault, "rev-parse", "HEAD") == head and events.items == []
