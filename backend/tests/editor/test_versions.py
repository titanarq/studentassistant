"""Notes versions (`editor.versions`): list, read, diff by section, restore as a new version."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.versions import (
    NOTES_RESTORED_KIND,
    PREAMBLE_KEY,
    NothingToRestoreError,
    RestoreResult,
    UnknownVersionError,
    VersionError,
    compare_notes,
    diff_versions,
    list_versions,
    read_version,
    restore_version,
)
from studentassistant.vault import (
    GitSync,
    Vault,
    create_topic,
    notes_path,
    read_notes,
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


def _tag(topic: ReviseTopic, sync: GitSync, text: str | None = None) -> None:
    if text is not None:
        write_notes(topic.vault, topic.subject, topic.topic, text)
    sync.create_notes_tag(topic.subject, topic.topic, "Apuntes de prueba")


def _v2(notes: str) -> str:
    """v1 with #definicion edited, #proximo-dia moved first and renamed, a new section."""
    return (
        notes.replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$ o df/dx.[^t2]")
        .replace(
            "## 1. Definición {#definicion}",
            "## 1. Próximos pasos {#proximo-dia}\n\n"
            "- La regla de la cadena, que se verá mañana.[^t3][^p2]\n\n"
            "## 2. Definición {#definicion}",
        )
        .replace(
            "## 2. Próximo día {#proximo-dia}\n\n"
            "- La regla de la cadena, que se verá mañana.[^t3][^p2]\n\n",
            "## 3. Ejemplos {#ejemplos}\n\nLa derivada de x² es 2x.[^p1]\n\n",
        )
    )


def test_no_versions_before_the_first_tag(topic: ReviseTopic, sync: GitSync) -> None:
    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)

    assert listing.versions == []
    assert listing.has_notes and not listing.changed_since_latest


def test_versions_are_listed_oldest_first_and_the_current_one_is_marked(
    topic: ReviseTopic, sync: GitSync
) -> None:
    _tag(topic, sync)
    _tag(topic, sync, _v2(topic.notes))

    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)

    assert [(v.version, v.current) for v in listing.versions] == [(1, False), (2, True)]
    assert listing.versions[0].tag == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert listing.versions[1].message == "Apuntes de prueba"
    assert listing.versions[1].tagged_at is not None
    assert not listing.changed_since_latest

    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\nMás.[^p1]\n")
    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)
    assert [v.current for v in listing.versions] == [False, False]
    assert listing.changed_since_latest


def test_read_version_returns_the_text_as_tagged(topic: ReviseTopic, sync: GitSync) -> None:
    _tag(topic, sync)
    _tag(topic, sync, _v2(topic.notes))

    assert read_version(topic.vault, topic.subject, topic.topic, 1, sync=sync).text == topic.notes
    assert read_version(topic.vault, topic.subject, topic.topic, 2, sync=sync).text == _v2(
        topic.notes
    )
    with pytest.raises(UnknownVersionError):
        read_version(topic.vault, topic.subject, topic.topic, 3, sync=sync)


def test_versions_are_per_topic(tmp_vault: Vault, topic: ReviseTopic, sync: GitSync) -> None:
    _tag(topic, sync)
    other = create_topic(tmp_vault, topic.subject, "Integrales").slug

    listing = list_versions(tmp_vault, topic.subject, other, sync=sync)

    assert listing.versions == [] and not listing.has_notes


def test_the_diff_is_by_section_matched_by_anchor(topic: ReviseTopic, sync: GitSync) -> None:
    _tag(topic, sync)
    _tag(topic, sync, _v2(topic.notes))

    diff = diff_versions(topic.vault, topic.subject, topic.topic, 1, 2, sync=sync)

    assert not diff.identical
    by_key = {section.key: section for section in diff.sections}
    assert [s.key for s in diff.sections] == [PREAMBLE_KEY, "proximo-dia", "definicion", "ejemplos"]
    assert by_key[PREAMBLE_KEY].status == "unchanged"
    assert by_key[PREAMBLE_KEY].title_after == "Derivadas"
    definicion = by_key["definicion"]
    assert definicion.status == "changed"
    assert "-Se escribe $f'(x)$.[^t2]" in definicion.diff
    assert "+Se escribe $f'(x)$ o df/dx.[^t2]" in definicion.diff
    proximo = by_key["proximo-dia"]
    assert proximo.status == "changed" and proximo.renamed and proximo.moved
    assert (proximo.title_before, proximo.title_after) == ("2. Próximo día", "1. Próximos pasos")
    assert by_key["ejemplos"].status == "added"
    assert "+La derivada de x² es 2x.[^p1]" in by_key["ejemplos"].diff
    assert diff.footnotes.model_dump() == {"added": [], "removed": [], "changed": []}
    assert diff.diff.startswith("--- apuntes v1\n+++ apuntes v2\n")


def test_a_removed_section_stays_where_it_was_and_footnotes_are_compared_by_label() -> None:
    before = (
        "# T\n\n## A {#a}\n\nUno.[^p1]\n\n## B {#b}\n\nDos.[^p2]\n\n## C {#c}\n\nTres.[^p1]\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^p2]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
    )
    after = (
        "# T\n\n## A {#a}\n\nUno.[^p1]\n\n## C {#c}\n\nTres.[^p3]\n\n"
        "[^p1]: [Apuntes, página 9](../sources/notes/page-009.jpg)\n"
        "[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)\n"
    )

    sections, footnotes, _whole = compare_notes(before, after)

    assert [(s.key, s.status, s.moved) for s in sections] == [
        (PREAMBLE_KEY, "unchanged", False),
        ("a", "unchanged", False),
        ("b", "removed", False),
        ("c", "changed", False),
    ]
    assert sections[2].title_after is None and "-Dos.[^p2]" in sections[2].diff
    assert footnotes.model_dump() == {"added": ["p3"], "removed": ["p2"], "changed": ["p1"]}


def test_the_diff_against_the_current_notes(topic: ReviseTopic, sync: GitSync) -> None:
    _tag(topic, sync)

    same = diff_versions(topic.vault, topic.subject, topic.topic, 1, sync=sync)
    assert same.identical and same.to_version is None and same.diff == ""
    assert {s.status for s in same.sections} == {"unchanged"}

    write_notes(topic.vault, topic.subject, topic.topic, _v2(topic.notes))
    changed = diff_versions(topic.vault, topic.subject, topic.topic, 1, sync=sync)
    assert not changed.identical
    assert changed.diff.startswith("--- apuntes v1\n+++ apuntes actuales\n")
    with pytest.raises(UnknownVersionError):
        diff_versions(topic.vault, topic.subject, topic.topic, 5, sync=sync)


def test_a_diff_needs_notes_to_compare_with(topic: ReviseTopic, sync: GitSync) -> None:
    _tag(topic, sync)
    notes_path(topic.vault, topic.subject, topic.topic).unlink()

    with pytest.raises(VersionError, match="Todavía no hay apuntes"):
        diff_versions(topic.vault, topic.subject, topic.topic, 1, sync=sync)


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))


def _restore(
    topic: ReviseTopic, sync: GitSync, version: int, events: Sink | None = None
) -> RestoreResult:
    return _run(
        restore_version(
            topic.vault, topic.subject, topic.topic, version, sync=sync, on_event=events
        )
    )


def test_restore_writes_the_old_version_as_a_new_commit_and_tag(
    topic: ReviseTopic, sync: GitSync
) -> None:
    _tag(topic, sync)
    _tag(topic, sync, _v2(topic.notes))
    events = Sink()

    result = _restore(topic, sync, 1, events)

    assert (result.restored_version, result.version) == (1, 3)
    assert result.tag == f"{topic.subject}/{topic.topic}/apuntes-v3"
    assert result.errors == [] and result.warning is None
    assert result.notes == topic.notes
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes
    assert "-## 1. Próximos pasos {#proximo-dia}" in result.diff
    tags = sync.list_notes_tags(topic.subject, topic.topic)
    assert [t.version for t in tags] == [1, 2, 3]
    assert tags[2].commit == result.commit != tags[1].commit
    assert (
        tags[2].message == f"Apuntes v3 de {topic.subject}/{topic.topic}: restaurada la versión 1"
    )
    assert sync.read_file_at(result.commit, result.path) == topic.notes
    assert not sync.status().pending_changes
    # Nothing was rewound: v2 is still readable, and it is now the one that differs.
    assert read_version(topic.vault, topic.subject, topic.topic, 2, sync=sync).text == _v2(
        topic.notes
    )
    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)
    assert [v.current for v in listing.versions] == [True, False, True]
    assert [kind for kind, _ in events.items] == [NOTES_RESTORED_KIND]
    assert "notes" not in events.items[0][1] and events.items[0][1]["version"] == 3


def test_restoring_the_current_version_writes_nothing(topic: ReviseTopic, sync: GitSync) -> None:
    _tag(topic, sync)

    with pytest.raises(NothingToRestoreError):
        _restore(topic, sync, 1)
    with pytest.raises(UnknownVersionError):
        _restore(topic, sync, 2)

    assert [t.version for t in sync.list_notes_tags(topic.subject, topic.topic)] == [1]


def test_restore_reports_what_the_validator_says_today(topic: ReviseTopic, sync: GitSync) -> None:
    broken = topic.notes.replace("page-002.jpg", "page-099.jpg")
    _tag(topic, sync, broken)
    _tag(topic, sync, topic.notes)

    result = _restore(topic, sync, 1)

    assert result.version == 3 and result.errors and result.warning
    assert read_notes(topic.vault, topic.subject, topic.topic) == broken
